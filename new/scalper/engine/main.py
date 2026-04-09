"""
Main engine — the orchestrator.

Starts the socket server, initializes all components, runs the engine loop.
Single-threaded event loop. No asyncio. No threads.

Loop cycle (runs once per socket poll, ~every 1-10ms):
    1. Poll socket for messages (ticks, fills, heartbeats)
    2. On each tick:
       a. Feed tick to buffer and bar aggregator
       b. If new bar completed → update EMA, compute bar-level features
       c. Compute tick-level features
       d. If in position → check management (breakeven, trailing, time stop)
       e. If IDLE → evaluate signal → evaluate risk → send order if approved
       f. If signal says exit → send close command
    3. Log to database
    4. Periodically flush database buffer

Shutdown:
    - On SIGINT/SIGTERM: send flatten command, wait for confirmation, close socket
    - On heartbeat timeout: EA handles its own flatten (defense in depth)
"""

import os
import sys
import time
import signal
import logging
from datetime import datetime, timezone
from typing import Optional

from .config import ScalperConfig
from .state import StateMachine, BotState
from .tick_buffer import TickBuffer, BarAggregator, Tick
from .features import FeatureEngine
from .signals import SignalGenerator, SignalDirection
from .risk import RiskManager
from .session import SessionFilter
from .connection import SocketServer, Message
from .database import TradeDatabase
from .analytics import Analytics


class ScalperEngine:
    """The main engine. One instance per bot process."""

    def __init__(self, config_path: Optional[str] = None):
        # Load config
        if config_path:
            self._cfg = ScalperConfig.from_file(config_path)
        else:
            self._cfg = ScalperConfig.from_default()

        # Initialize logging
        self._setup_logging()
        self._log = logging.getLogger("engine")
        self._log.info("Scalper engine initializing...")
        self._log.info("Symbol: %s | Magic: %d", self._cfg.symbol, self._cfg.position.magic_number)

        # Core components
        self._state = StateMachine(
            cooldown_seconds=self._cfg.risk.cooldown_seconds,
            on_transition=self._on_state_transition,
        )

        self._ticks = TickBuffer(max_size=self._cfg.features.tick_buffer_size)
        self._bars = BarAggregator(
            period_minutes=self._cfg.features.atr_timeframe_minutes,
            max_bars=self._cfg.features.bar_buffer_size,
        )

        self._features = FeatureEngine(self._cfg.features, self._ticks, self._bars)
        self._features.initialize_emas(
            fast_period=self._cfg.strategy.ema_fast_period,
            slow_period=self._cfg.strategy.ema_period,
        )

        self._session = SessionFilter(self._cfg.session)

        self._risk = RiskManager(self._cfg.risk, self._cfg.position)

        self._signals = SignalGenerator(
            self._cfg.strategy, self._cfg.features, self._session, self._state,
        )

        # Database
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        db_path = os.path.join(base_dir, self._cfg.database.path)
        self._db = TradeDatabase(
            db_path=db_path,
            flush_interval=self._cfg.database.flush_interval_seconds,
            tick_sample_rate=self._cfg.database.tick_sample_rate,
        )

        self._analytics = Analytics(self._db)

        # Socket
        self._socket = SocketServer(
            host=self._cfg.socket.host,
            port=self._cfg.socket.port,
            recv_buffer=self._cfg.socket.recv_buffer_bytes,
            heartbeat_interval_ms=self._cfg.socket.heartbeat_interval_ms,
            heartbeat_timeout_ms=self._cfg.socket.heartbeat_timeout_ms,
        )

        # State
        self._running: bool = False
        self._current_ticket: int = 0
        self._current_db_id: int = 0
        self._account_balance: float = 0.0
        self._ticks_processed: int = 0
        self._last_bar_count: int = 0

        self._log.info("Engine initialized. Waiting for EA connection...")

    # ── Run ─────────────────────────────────────────────────────

    def run(self) -> None:
        """Start the engine loop. Blocks until shutdown."""
        self._running = True

        # Signal handlers for graceful shutdown
        signal.signal(signal.SIGINT, self._shutdown_handler)
        signal.signal(signal.SIGTERM, self._shutdown_handler)

        # Register socket message handlers
        self._socket.on("tick", self._handle_tick_msg)
        self._socket.on("fill", self._handle_fill_msg)
        self._socket.on("position", self._handle_position_msg)
        self._socket.on("shutdown", self._handle_shutdown_msg)

        self._socket.start()

        self._log.info("Engine loop started. Waiting for EA connection on port %d...", self._cfg.socket.port)

        try:
            while self._running:
                if not self._socket.connected:
                    time.sleep(0.1)  # Don't spin while waiting for connection
                    self._socket.poll()
                    continue

                # Main loop cycle
                messages = self._socket.poll()

                # Tick messages are handled in _handle_tick_msg callback
                # Other messages are handled by their respective callbacks

                # Periodic maintenance
                self._db.maybe_flush()

                # Tight loop — no sleep when connected (process ticks as fast as they arrive)
                # But yield CPU if no messages came in
                if not messages:
                    time.sleep(0.001)  # 1ms yield

        except Exception:
            self._log.exception("Fatal error in engine loop")
            self._emergency_shutdown()
        finally:
            self._cleanup()

    # ── Tick Processing (the hot path) ──────────────────────────

    def _handle_tick_msg(self, msg: Message) -> None:
        """Process incoming tick from MQL5 EA."""
        d = msg.data
        tick = Tick(
            bid=d["bid"],
            ask=d["ask"],
            time_ms=d.get("time", msg.timestamp),
            volume=d.get("vol", 1),
            spread=d.get("spread", 0),
        )

        # Deduplicate
        if not self._ticks.add(tick):
            return

        self._ticks_processed += 1

        # Log tick to DB (sampled)
        self._db.log_tick(tick.time_ms, tick.bid, tick.ask, tick.spread, tick.volume)

        # Feed to bar aggregator
        completed_bar = self._bars.update(tick)

        # If new bar completed → trigger bar-level processing
        if completed_bar is not None:
            self._features.on_bar_close(completed_bar)
            self._db.log_bar(
                completed_bar.time_ms, completed_bar.open, completed_bar.high,
                completed_bar.low, completed_bar.close, completed_bar.volume,
                completed_bar.tick_count, completed_bar.spread_avg,
            )
            self._on_bar_complete(completed_bar)

        # Compute features
        feat = self._features.compute(tick.time_ms)
        if feat is None:
            return  # insufficient data

        # ── Trade management (if in position) ────────────────
        if self._state.has_position:
            self._manage_position(tick, feat)
            return

        # ── Signal evaluation (if IDLE) ──────────────────────
        if not self._state.is_tradeable:
            return

        # Get bar-level context for signal generator
        bar_range = 0.0
        avg_bar_range = 0.0
        bar_volume = 0
        avg_bar_volume = 0.0

        if self._bars.count > 0:
            ranges = self._bars.get_range_array(20)
            volumes = self._bars.get_volume_array(20)
            if len(ranges) > 0:
                bar_range = float(ranges[-1])
                avg_bar_range = float(ranges.mean())
            if len(volumes) > 0:
                bar_volume = int(volumes[-1])
                avg_bar_volume = float(volumes.mean())

        sig = self._signals.evaluate(
            feat, bar_range, avg_bar_range, bar_volume, avg_bar_volume,
        )

        if sig is None:
            return

        # ── Risk evaluation ──────────────────────────────────
        direction = 1 if sig.direction == SignalDirection.BUY else -1

        verdict = self._risk.evaluate(
            direction=direction,
            atr=feat.atr,
            current_spread=feat.current_spread,
            volatility_regime=feat.volatility_regime,
            atr_multiplier_sl=self._cfg.strategy.atr_multiplier_sl,
            atr_multiplier_tp=self._cfg.strategy.atr_multiplier_tp,
        )

        # Log signal (approved or rejected)
        self._db.log_signal(
            time_ms=tick.time_ms,
            direction=sig.direction.name,
            strategy=sig.trigger.value,
            strength=sig.strength,
            bid=tick.bid, ask=tick.ask, spread=tick.spread,
            ofi=feat.ofi, delta=feat.cum_delta,
            vwap_dev=feat.vwap_dev, atr=feat.atr,
            regime=feat.volatility_regime,
            vetoed=not verdict.approved,
            veto_reason=verdict.reject_reason,
        )

        if not verdict.approved:
            return

        # ── Send order to EA ─────────────────────────────────
        self._state.transition(BotState.SIGNAL, "signal_%s" % sig.trigger.value)

        # Calculate actual SL/TP prices
        if direction > 0:  # BUY
            sl_price = tick.ask - verdict.sl_distance
            tp_price = tick.ask + verdict.tp_distance
        else:  # SELL
            sl_price = tick.bid + verdict.sl_distance
            tp_price = tick.bid - verdict.tp_distance

        # Apply scale-in if enabled
        volume = verdict.volume
        if self._cfg.strategy.scale_in_enabled:
            volume = round(volume * self._cfg.strategy.scale_in_initial_pct, 2)
            volume = max(volume, self._cfg.position.min_volume)

        action = "BUY" if direction > 0 else "SELL"
        sent = self._socket.send_signal(
            action=action,
            volume=volume,
            sl=round(sl_price, 2),
            tp=round(tp_price, 2),
            magic=self._cfg.position.magic_number,
        )

        if sent:
            self._state.transition(BotState.PENDING, "order_sent")
            self._log.info(
                "ORDER SENT | %s %.2f lots @ market | SL=%.2f TP=%.2f | trigger=%s",
                action, volume, sl_price, tp_price, sig.trigger.value,
            )
        else:
            self._state.transition(BotState.IDLE, "send_failed")

    # ── Position Management ─────────────────────────────────────

    def _manage_position(self, tick: Tick, feat) -> None:
        """Manage active position: breakeven, trailing, time stop, signal exit."""
        mid = tick.mid

        # Time stop / MAE-MFE update
        action = self._risk.on_tick_update(mid)
        if action and action["action"] == "close":
            self._close_position(action["reason"])
            return

        # Breakeven move
        new_sl = self._risk.check_breakeven(
            mid, self._cfg.strategy.breakeven_activation_points,
        )
        if new_sl is not None and self._current_ticket:
            trade = self._risk.active_trade
            tp = trade.tp if trade else 0.0
            self._socket.send_modify(self._current_ticket, new_sl, tp)
            self._log.info("BREAKEVEN | SL moved to %.2f", new_sl)

        # Trailing stop
        new_sl = self._risk.check_trailing_stop(
            mid,
            self._cfg.strategy.trail_activation_points,
            self._cfg.strategy.trail_step_points,
        )
        if new_sl is not None and self._current_ticket:
            trade = self._risk.active_trade
            tp = trade.tp if trade else 0.0
            self._socket.send_modify(self._current_ticket, new_sl, tp)
            self._log.info("TRAIL | SL moved to %.2f", new_sl)

        # Signal-based exit check
        exit_reason = self._signals.should_exit(feat)
        if exit_reason:
            self._close_position(exit_reason)

    def _close_position(self, reason: str) -> None:
        """Send close command to EA."""
        if self._current_ticket:
            self._socket.send_close(self._current_ticket, reason)
            self._state.transition(BotState.EXITING, reason)
            self._log.info("CLOSING | ticket=%d reason=%s", self._current_ticket, reason)

    # ── Fill Handling ───────────────────────────────────────────

    def _handle_fill_msg(self, msg: Message) -> None:
        """Process fill confirmation from EA."""
        d = msg.data
        fill_type = d.get("fill_type", "open")

        if fill_type == "open":
            self._current_ticket = d["ticket"]
            direction = 1 if d.get("direction", "BUY") == "BUY" else -1

            # Record in database
            self._current_db_id = self._db.log_trade_open(
                ticket=d["ticket"],
                time_ms=d.get("time", msg.timestamp),
                direction=d.get("direction", "BUY"),
                volume=d["volume"],
                price=d["price"],
                sl=d.get("sl", 0),
                tp=d.get("tp", 0),
                magic=self._cfg.position.magic_number,
            )

            # Track in risk manager
            feat = self._features.compute(msg.timestamp)
            self._risk.on_trade_open(
                db_id=self._current_db_id,
                direction=direction,
                entry_price=d["price"],
                volume=d["volume"],
                sl=d.get("sl", 0),
                tp=d.get("tp", 0),
                spread=d.get("spread", 0),
                ofi=feat.ofi if feat else 0,
                delta=feat.cum_delta if feat else 0,
                atr=feat.atr if feat else 0,
            )

            self._state.transition(BotState.ACTIVE, "fill_confirmed")
            self._log.info(
                "FILL OPEN | ticket=%d %s %.2f @ %.2f",
                d["ticket"], d.get("direction", "?"), d["volume"], d["price"],
            )

        elif fill_type == "close":
            pnl_points = d.get("pnl_points", 0)
            pnl_money = d.get("pnl_money", 0)

            # Close in database
            self._db.log_trade_close(
                db_id=self._current_db_id,
                close_time_ms=d.get("time", msg.timestamp),
                close_price=d["price"],
                pnl_points=pnl_points,
                pnl_money=pnl_money,
                commission=d.get("commission", 0),
                swap=d.get("swap", 0),
                close_reason=d.get("reason", "unknown"),
            )

            # Record metrics and update risk tracking
            tracker = self._risk.on_trade_close(pnl_points, pnl_money)
            if tracker:
                self._db.log_trade_metric(
                    trade_id=self._current_db_id,
                    mae=tracker.mae_points,
                    mfe=tracker.mfe_points,
                    duration=time.time() - tracker.entry_time,
                    entry_spread=tracker.entry_spread,
                    exit_spread=d.get("spread"),
                    entry_ofi=tracker.entry_ofi,
                    entry_delta=tracker.entry_delta,
                    entry_atr=tracker.entry_atr,
                )

            # Update account balance
            self._account_balance += pnl_money
            self._risk.set_account_balance(self._account_balance)

            # State transition
            if self._state.state == BotState.EXITING:
                self._state.transition(BotState.COOLDOWN, "trade_closed")
            elif self._state.has_position:
                self._state.transition(BotState.EXITING, "fill_close_received")
                self._state.transition(BotState.COOLDOWN, "trade_closed")

            # Check daily limit
            if self._risk.is_daily_limit_hit:
                self._state.halt("daily_loss_limit")

            self._current_ticket = 0
            self._current_db_id = 0

            self._log.info(
                "FILL CLOSE | pnl=%.1f pts ($%.2f) | daily=$%.2f | balance=$%.2f",
                pnl_points, pnl_money, self._risk.daily_pnl, self._account_balance,
            )

    # ── Other message handlers ──────────────────────────────────

    def _handle_position_msg(self, msg: Message) -> None:
        """Position sync on reconnect."""
        d = msg.data
        self._account_balance = d.get("balance", self._account_balance)
        self._risk.set_account_balance(self._account_balance)
        self._log.info("Position sync | balance=%.2f", self._account_balance)

    def _handle_shutdown_msg(self, msg: Message) -> None:
        """EA is shutting down."""
        self._log.warning("EA shutdown received")
        self._running = False

    # ── Bar-level processing ────────────────────────────────────

    def _on_bar_complete(self, bar) -> None:
        """Called when a 1M bar completes."""
        # Check session state
        session = self._session.current_session(bar.time_ms)
        if session is None and not self._state.has_position:
            # Session ended — run analytics if we traded
            if self._state.trade_count > 0:
                date_str = datetime.fromtimestamp(
                    bar.time_ms / 1000, tz=timezone.utc
                ).strftime("%Y-%m-%d")
                self._analytics.run_session_report(date_str)
                self._analytics.check_edge_decay()
                self._signals.reset_session()
                self._state.reset_session_count()

    # ── State transition callback ───────────────────────────────

    def _on_state_transition(self, old: BotState, new: BotState, reason: str, ts_ms: int) -> None:
        self._db.log_state_change(ts_ms, old.name, new.name, reason)

    # ── Shutdown ────────────────────────────────────────────────

    def _shutdown_handler(self, signum, frame) -> None:
        self._log.warning("Shutdown signal received (sig=%d)", signum)
        self._running = False

    def _emergency_shutdown(self) -> None:
        self._log.critical("EMERGENCY SHUTDOWN — flattening positions")
        if self._socket.connected:
            self._socket.send_flatten("emergency_shutdown")
            time.sleep(1)  # Give EA time to process

    def _cleanup(self) -> None:
        self._log.info("Cleaning up...")
        self._db.flush()
        self._db.close()
        self._socket.stop()
        self._log.info("Engine stopped. Processed %d ticks.", self._ticks_processed)

    # ── Logging setup ───────────────────────────────────────────

    def _setup_logging(self) -> None:
        log_cfg = self._cfg.logging
        root = logging.getLogger()
        root.setLevel(getattr(logging, log_cfg.level))

        fmt = logging.Formatter(
            "%(asctime)s.%(msecs)03d | %(name)-8s | %(levelname)-5s | %(message)s",
            datefmt="%H:%M:%S",
        )

        if log_cfg.console_output:
            ch = logging.StreamHandler(sys.stdout)
            ch.setFormatter(fmt)
            root.addHandler(ch)

        if log_cfg.file_output:
            base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            log_dir = os.path.join(base_dir, log_cfg.log_dir)
            os.makedirs(log_dir, exist_ok=True)
            log_file = os.path.join(
                log_dir,
                "scalper_%s.log" % datetime.now().strftime("%Y%m%d_%H%M%S"),
            )
            fh = logging.FileHandler(log_file)
            fh.setFormatter(fmt)
            root.addHandler(fh)


def main():
    """Entry point."""
    config_path = None
    if len(sys.argv) > 1:
        config_path = sys.argv[1]

    engine = ScalperEngine(config_path)
    engine.run()


if __name__ == "__main__":
    main()

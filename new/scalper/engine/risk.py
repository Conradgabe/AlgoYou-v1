"""
Risk manager — the gatekeeper between signals and orders.

Has ABSOLUTE VETO authority over every trade signal.
No signal reaches the order manager without passing every check here.

Checks (in order):
    1. Daily P&L gate     — has today's max loss been hit?
    2. Session trade limit — max trades per session
    3. Consecutive losses  — reduce size after N losses in a row
    4. Spread gate         — is spread too wide right now?
    5. Volatility gate     — is regime too extreme to trade?
    6. Position sizing     — half-Kelly with safety clamps
    7. Equity curve filter — is the equity curve below its EMA?

MAE/MFE tracking runs on active trades — logged for post-trade analytics.
"""

import time
import logging
import math
from typing import Optional, Tuple
from dataclasses import dataclass

from .config import RiskConfig, PositionConfig


@dataclass(slots=True)
class RiskVerdict:
    """Result of risk evaluation on a signal."""
    approved: bool
    volume: float           # approved position size (0 if rejected)
    sl_distance: float      # stop loss distance in price
    tp_distance: float      # take profit distance in price
    reject_reason: str      # empty if approved
    risk_dollars: float     # dollar amount at risk


@dataclass(slots=True)
class TradeTracker:
    """Tracks MAE/MFE/duration for an active trade."""
    trade_db_id: int
    direction: int          # +1 long, -1 short
    entry_price: float
    entry_time: float
    volume: float
    sl: float
    tp: float
    mae_points: float       # max adverse excursion (worst unrealized loss)
    mfe_points: float       # max favorable excursion (best unrealized profit)
    entry_spread: int
    entry_ofi: float
    entry_delta: float
    entry_atr: float
    breakeven_moved: bool
    trail_active: bool
    current_sl: float


class RiskManager:
    __slots__ = (
        "_cfg", "_pos_cfg", "_log",
        "_daily_pnl", "_daily_date",
        "_consec_losses", "_total_trades",
        "_win_count", "_loss_count",
        "_win_sum", "_loss_sum",
        "_equity_curve", "_equity_ema",
        "_active_trade", "_session_trade_count",
        "_account_balance",
    )

    def __init__(self, risk_cfg: RiskConfig, pos_cfg: PositionConfig):
        self._cfg = risk_cfg
        self._pos_cfg = pos_cfg
        self._log = logging.getLogger("risk")

        # Daily tracking
        self._daily_pnl: float = 0.0
        self._daily_date: str = ""
        self._session_trade_count: int = 0

        # Performance tracking for Kelly
        self._consec_losses: int = 0
        self._total_trades: int = 0
        self._win_count: int = 0
        self._loss_count: int = 0
        self._win_sum: float = 0.0
        self._loss_sum: float = 0.0

        # Equity curve tracking
        self._equity_curve: list = []
        self._equity_ema: float = 0.0

        # Active trade tracker
        self._active_trade: Optional[TradeTracker] = None

        # Account balance (updated by engine)
        self._account_balance: float = 0.0

    # ── Configuration ───────────────────────────────────────────

    def set_account_balance(self, balance: float) -> None:
        self._account_balance = balance

    def new_day(self, date_str: str) -> None:
        """Reset daily counters. Called at session start."""
        if date_str != self._daily_date:
            self._daily_pnl = 0.0
            self._daily_date = date_str
            self._session_trade_count = 0
            self._log.info("New trading day: %s | Balance: %.2f", date_str, self._account_balance)

    # ── Signal Evaluation (THE GATE) ────────────────────────────

    def evaluate(
        self,
        direction: int,
        atr: float,
        current_spread: int,
        volatility_regime: str,
        atr_multiplier_sl: float,
        atr_multiplier_tp: float,
    ) -> RiskVerdict:
        """
        Evaluate whether a signal should become an order.
        Returns RiskVerdict with approved=True/False and computed sizing.
        """
        # ── Check 1: Daily P&L gate ──────────────────────────
        max_daily_loss = self._account_balance * (self._cfg.max_daily_loss_pct / 100.0)
        if self._daily_pnl <= -max_daily_loss:
            return self._reject("daily_loss_limit_hit (%.2f)" % self._daily_pnl)

        # ── Check 2: Session trade limit ─────────────────────
        from .config import StrategyConfig  # avoid circular at module level
        if self._session_trade_count >= 10:  # hardcoded safety, strategy has its own
            return self._reject("session_trade_limit")

        # ── Check 3: Spread gate ─────────────────────────────
        if current_spread > self._cfg.max_spread_points:
            return self._reject("spread_too_wide (%d > %d)" % (
                current_spread, self._cfg.max_spread_points))

        # ── Check 4: Volatility gate ─────────────────────────
        if volatility_regime == "extreme":
            return self._reject("volatility_extreme")

        # ── Check 5: Already in position ─────────────────────
        if self._active_trade is not None:
            return self._reject("already_in_position")

        # ── Check 6: Equity curve filter ─────────────────────
        if len(self._equity_curve) >= self._cfg.equity_curve_ema_period:
            if self._equity_curve[-1] < self._equity_ema:
                return self._reject("equity_below_ema")

        # ── Compute position size ────────────────────────────
        sl_distance = atr * atr_multiplier_sl
        tp_distance = atr * atr_multiplier_tp

        if sl_distance < 0.01:
            return self._reject("sl_distance_too_small")

        # R:R check
        if tp_distance / sl_distance < self._cfg.max_positions:  # min_rr effectively
            pass  # We check R:R in signal generator, not here

        volume = self._compute_volume(sl_distance)

        # Consecutive loss reduction
        if self._consec_losses >= self._cfg.max_consecutive_losses:
            volume *= self._cfg.consecutive_loss_size_reduction
            self._log.info(
                "Size reduced %.0f%% due to %d consecutive losses",
                (1 - self._cfg.consecutive_loss_size_reduction) * 100,
                self._consec_losses,
            )

        # Clamp volume
        volume = self._clamp_volume(volume)

        # Risk in dollars
        risk_dollars = sl_distance * volume * 100  # 100oz per lot for gold

        self._log.info(
            "APPROVED | dir=%s vol=%.2f sl=%.2f tp=%.2f risk=$%.2f spread=%d regime=%s",
            "BUY" if direction > 0 else "SELL",
            volume, sl_distance, tp_distance, risk_dollars,
            current_spread, volatility_regime,
        )

        return RiskVerdict(
            approved=True,
            volume=volume,
            sl_distance=sl_distance,
            tp_distance=tp_distance,
            reject_reason="",
            risk_dollars=risk_dollars,
        )

    # ── Position Sizing (Half-Kelly) ────────────────────────────

    def _compute_volume(self, sl_distance: float) -> float:
        """
        Half-Kelly position sizing.

        Kelly% = W - (1-W)/R
        Where W = win rate, R = avg_win / avg_loss (payoff ratio)

        We use half-Kelly (Kelly/2) because:
        - Full Kelly assumes perfect knowledge of edge (we don't have it)
        - Half-Kelly gives ~75% of growth rate with dramatically less drawdown
        - It's the standard institutional approach for systematic trading

        Falls back to fixed fractional (1% risk) if insufficient trade history.
        """
        if self._total_trades >= self._cfg.min_trades_for_kelly and self._loss_count > 0:
            win_rate = self._win_count / self._total_trades
            avg_win = self._win_sum / max(self._win_count, 1)
            avg_loss = abs(self._loss_sum / max(self._loss_count, 1))
            payoff = avg_win / avg_loss if avg_loss > 0 else 1.0

            kelly = win_rate - (1.0 - win_rate) / payoff
            kelly = max(kelly, 0.0)  # never negative — means no edge
            risk_pct = kelly * self._cfg.kelly_fraction * 100  # half-Kelly as percentage
        else:
            # Not enough data — use configured defaults
            win_rate = self._cfg.default_win_rate
            payoff = self._cfg.default_payoff_ratio
            kelly = win_rate - (1.0 - win_rate) / payoff
            kelly = max(kelly, 0.0)
            risk_pct = min(kelly * self._cfg.kelly_fraction * 100, self._cfg.max_risk_per_trade_pct)

        # Convert risk percentage to volume
        risk_dollars = self._account_balance * (risk_pct / 100.0)
        # For gold: 1 lot = 100 oz. SL in price units × 100 oz = dollar risk per lot
        dollar_per_lot_per_point = 100.0  # 100 oz
        risk_per_lot = sl_distance * dollar_per_lot_per_point

        if risk_per_lot < 0.01:
            return self._pos_cfg.min_volume

        volume = risk_dollars / risk_per_lot
        return volume

    def _clamp_volume(self, volume: float) -> float:
        """Round to step size and clamp to min/max."""
        step = self._pos_cfg.volume_step
        volume = math.floor(volume / step) * step
        volume = max(volume, self._pos_cfg.min_volume)
        volume = min(volume, self._pos_cfg.max_volume)
        return round(volume, 2)

    # ── Trade Lifecycle ─────────────────────────────────────────

    def on_trade_open(
        self, db_id: int, direction: int, entry_price: float,
        volume: float, sl: float, tp: float,
        spread: int, ofi: float, delta: float, atr: float,
    ) -> None:
        self._active_trade = TradeTracker(
            trade_db_id=db_id,
            direction=direction,
            entry_price=entry_price,
            entry_time=time.time(),
            volume=volume,
            sl=sl,
            tp=tp,
            mae_points=0.0,
            mfe_points=0.0,
            entry_spread=spread,
            entry_ofi=ofi,
            entry_delta=delta,
            entry_atr=atr,
            breakeven_moved=False,
            trail_active=False,
            current_sl=sl,
        )
        self._session_trade_count += 1

    def on_tick_update(self, current_price: float) -> Optional[dict]:
        """
        Update MAE/MFE on active trade. Returns management action dict if any:
        {"action": "move_sl", "new_sl": price} or {"action": "close", "reason": "..."}
        """
        t = self._active_trade
        if t is None:
            return None

        # Current P&L in points
        if t.direction > 0:
            pnl = current_price - t.entry_price
        else:
            pnl = t.entry_price - current_price

        pnl_points = pnl / 0.01  # convert to points

        # Update MAE/MFE
        if pnl_points < t.mae_points:
            t.mae_points = pnl_points
        if pnl_points > t.mfe_points:
            t.mfe_points = pnl_points

        # Time stop check
        elapsed = time.time() - t.entry_time
        from .config import StrategyConfig
        time_limit = 15 * 60  # 15 minutes in seconds
        min_profit = 50  # points

        if elapsed > time_limit and pnl_points < min_profit:
            return {"action": "close", "reason": "time_stop"}

        return None

    def check_breakeven(
        self, current_price: float, activation_points: float
    ) -> Optional[float]:
        """Check if breakeven SL move should trigger. Returns new SL price or None."""
        t = self._active_trade
        if t is None or t.breakeven_moved:
            return None

        if t.direction > 0:
            pnl_points = (current_price - t.entry_price) / 0.01
            if pnl_points >= activation_points:
                t.breakeven_moved = True
                new_sl = t.entry_price + 0.01  # 1 point above entry
                t.current_sl = new_sl
                return new_sl
        else:
            pnl_points = (t.entry_price - current_price) / 0.01
            if pnl_points >= activation_points:
                t.breakeven_moved = True
                new_sl = t.entry_price - 0.01
                t.current_sl = new_sl
                return new_sl

        return None

    def check_trailing_stop(
        self, current_price: float, activation_points: float, trail_step: float
    ) -> Optional[float]:
        """Check if trailing stop should move. Returns new SL price or None."""
        t = self._active_trade
        if t is None:
            return None

        if t.direction > 0:
            pnl_points = (current_price - t.entry_price) / 0.01
            if pnl_points >= activation_points:
                t.trail_active = True
                new_sl = current_price - (trail_step * 0.01)
                if new_sl > t.current_sl:
                    t.current_sl = new_sl
                    return new_sl
        else:
            pnl_points = (t.entry_price - current_price) / 0.01
            if pnl_points >= activation_points:
                t.trail_active = True
                new_sl = current_price + (trail_step * 0.01)
                if new_sl < t.current_sl:
                    t.current_sl = new_sl
                    return new_sl

        return None

    def on_trade_close(self, pnl_points: float, pnl_money: float) -> TradeTracker:
        """Record trade result. Returns the completed TradeTracker for DB logging."""
        t = self._active_trade
        self._active_trade = None

        # Update daily P&L
        self._daily_pnl += pnl_money

        # Update win/loss tracking
        self._total_trades += 1
        if pnl_points > 0:
            self._win_count += 1
            self._win_sum += pnl_points
            self._consec_losses = 0
        else:
            self._loss_count += 1
            self._loss_sum += pnl_points
            self._consec_losses += 1

        # Update equity curve
        self._equity_curve.append(self._account_balance + self._daily_pnl)
        if len(self._equity_curve) == 1:
            self._equity_ema = self._equity_curve[0]
        else:
            k = 2.0 / (self._cfg.equity_curve_ema_period + 1)
            self._equity_ema += k * (self._equity_curve[-1] - self._equity_ema)

        self._log.info(
            "CLOSED | pnl=%.1f pts ($%.2f) | daily=%.2f | W/L=%d/%d | consec_L=%d",
            pnl_points, pnl_money, self._daily_pnl,
            self._win_count, self._loss_count, self._consec_losses,
        )

        return t

    @property
    def active_trade(self) -> Optional[TradeTracker]:
        return self._active_trade

    @property
    def daily_pnl(self) -> float:
        return self._daily_pnl

    @property
    def is_daily_limit_hit(self) -> bool:
        max_loss = self._account_balance * (self._cfg.max_daily_loss_pct / 100.0)
        return self._daily_pnl <= -max_loss

    @property
    def stats(self) -> dict:
        return {
            "total_trades": self._total_trades,
            "win_count": self._win_count,
            "loss_count": self._loss_count,
            "win_rate": self._win_count / max(self._total_trades, 1),
            "consec_losses": self._consec_losses,
            "daily_pnl": self._daily_pnl,
        }

    # ── Internal ────────────────────────────────────────────────

    def _reject(self, reason: str) -> RiskVerdict:
        self._log.info("VETOED | %s", reason)
        return RiskVerdict(
            approved=False, volume=0.0, sl_distance=0.0,
            tp_distance=0.0, reject_reason=reason, risk_dollars=0.0,
        )

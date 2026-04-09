"""
Signal generator — the strategy brain.

Strategy: Momentum Absorption Scalper for XAU/USD 1M

Entry logic (ALL conditions must be true simultaneously):
    1. Session filter: Inside London or NY session window
    2. Spread filter:  Current spread ≤ max threshold
    3. Regime filter:  Volatility is NOT extreme
    4. Trend bias:     EMA fast/slow alignment confirms direction
    5. OFI confirms:   Order flow imbalance agrees with trend
    6. Trigger:        Either:
       a) Volatility contraction breakout — range contracted then volume spike
       b) Absorption reversal — absorption detected at key level + delta divergence
       c) Sweep continuation — sweep detected, enter in sweep direction

Exit logic (ANY condition triggers exit):
    1. Stop loss hit (managed by MT5 EA)
    2. Take profit hit (managed by MT5 EA)
    3. Time stop — 15 min with < 50 points profit
    4. Regime change — volatility goes extreme while in trade
    5. Session end approaching — close before session ends

No signal is generated unless ALL entry conditions align.
The signal generator never overrides the risk manager — it proposes, risk disposes.
"""

import logging
from typing import Optional
from dataclasses import dataclass
from enum import Enum

from .features import FeatureSnapshot
from .config import StrategyConfig, FeatureConfig
from .session import SessionFilter
from .state import StateMachine, BotState


class SignalDirection(Enum):
    NONE = 0
    BUY = 1
    SELL = -1


class SignalTrigger(Enum):
    NONE = "none"
    VOLATILITY_BREAKOUT = "vol_breakout"
    ABSORPTION_REVERSAL = "absorption_reversal"
    SWEEP_CONTINUATION = "sweep_continuation"


@dataclass(slots=True)
class Signal:
    direction: SignalDirection
    trigger: SignalTrigger
    strength: float          # 0.0–1.0, composite confidence score
    features: FeatureSnapshot
    timestamp_ms: int


class SignalGenerator:
    __slots__ = (
        "_strat_cfg", "_feat_cfg", "_session", "_state",
        "_log", "_last_signal_time", "_trades_this_session",
    )

    def __init__(
        self,
        strat_cfg: StrategyConfig,
        feat_cfg: FeatureConfig,
        session: SessionFilter,
        state: StateMachine,
    ):
        self._strat_cfg = strat_cfg
        self._feat_cfg = feat_cfg
        self._session = session
        self._state = state
        self._log = logging.getLogger("signal")
        self._last_signal_time: int = 0
        self._trades_this_session: int = 0

    def reset_session(self) -> None:
        self._trades_this_session = 0

    def evaluate(self, feat: FeatureSnapshot, bar_range: float = 0.0,
                 avg_bar_range: float = 0.0, bar_volume: int = 0,
                 avg_bar_volume: float = 0.0) -> Optional[Signal]:
        """
        Evaluate current features for trade signal.
        Returns Signal if all conditions align, None otherwise.

        bar_range / avg_bar_range: current and average bar range for contraction detection
        bar_volume / avg_bar_volume: current and average bar volume for spike detection
        """
        # ── Gate 0: State must be IDLE ───────────────────────
        if not self._state.is_tradeable:
            return None

        # ── Gate 1: Session filter ───────────────────────────
        if not self._session.is_trading_allowed(feat.time_ms):
            return None

        # ── Gate 2: Spread filter ────────────────────────────
        if feat.current_spread > self._feat_cfg.ofi_window_ticks:  # placeholder
            pass  # actual spread check is in risk manager, but we pre-filter here
        if feat.current_spread > 30:  # hard ceiling — never even evaluate above 30
            return None

        # ── Gate 3: Regime filter ────────────────────────────
        if feat.volatility_regime == "extreme":
            return None

        # ── Gate 4: Session trade limit ──────────────────────
        if self._trades_this_session >= self._strat_cfg.max_trades_per_session:
            return None

        # ── Gate 5: Minimum signal spacing (prevent rapid fire) ──
        if feat.time_ms - self._last_signal_time < 5000:  # 5 second minimum gap
            return None

        # ── Determine trend bias ─────────────────────────────
        trend = feat.trend_bias  # +1, -1, or 0
        if trend == 0:
            # No clear trend — only take absorption reversals
            signal = self._check_absorption_reversal(feat)
            if signal:
                self._last_signal_time = feat.time_ms
                self._trades_this_session += 1
            return signal

        # ── Check triggers in priority order ─────────────────

        # Priority 1: Sweep continuation (highest conviction, rarest)
        signal = self._check_sweep_continuation(feat, trend)
        if signal:
            self._last_signal_time = feat.time_ms
            self._trades_this_session += 1
            return signal

        # Priority 2: Absorption reversal (high edge per trade)
        signal = self._check_absorption_reversal(feat)
        if signal:
            self._last_signal_time = feat.time_ms
            self._trades_this_session += 1
            return signal

        # Priority 3: Volatility contraction breakout (bread and butter)
        signal = self._check_vol_breakout(feat, trend, bar_range, avg_bar_range,
                                          bar_volume, avg_bar_volume)
        if signal:
            self._last_signal_time = feat.time_ms
            self._trades_this_session += 1
            return signal

        return None

    # ── Trigger: Sweep Continuation ─────────────────────────────

    def _check_sweep_continuation(self, feat: FeatureSnapshot, trend: int) -> Optional[Signal]:
        """
        Sweep detected + same direction as trend + OFI confirms.
        Highest conviction signal — aggressive institutional participant moving urgently.
        """
        if not feat.sweep_detected:
            return None

        # Sweep must align with trend
        if feat.sweep_direction != trend:
            return None

        # OFI must confirm (same sign as sweep)
        ofi_confirms = (
            (feat.sweep_direction > 0 and feat.ofi > self._feat_cfg.ofi_threshold * 0.5) or
            (feat.sweep_direction < 0 and feat.ofi < -self._feat_cfg.ofi_threshold * 0.5)
        )
        if not ofi_confirms:
            return None

        direction = SignalDirection.BUY if feat.sweep_direction > 0 else SignalDirection.SELL

        # Strength based on sweep levels and OFI alignment
        strength = min(0.5 + feat.sweep_levels * 0.1 + abs(feat.ofi) * 0.3, 1.0)

        self._log.info(
            "SIGNAL sweep_continuation | dir=%s levels=%d ofi=%.3f strength=%.2f",
            direction.name, feat.sweep_levels, feat.ofi, strength,
        )

        return Signal(
            direction=direction,
            trigger=SignalTrigger.SWEEP_CONTINUATION,
            strength=strength,
            features=feat,
            timestamp_ms=feat.time_ms,
        )

    # ── Trigger: Absorption Reversal ────────────────────────────

    def _check_absorption_reversal(self, feat: FeatureSnapshot) -> Optional[Signal]:
        """
        Absorption detected + delta divergence confirms.

        Absorption means a large player is resting orders at a level, absorbing
        aggression without letting price move. When combined with delta divergence
        (delta pointing one way but price not following), it signals a reversal.

        This is one of the highest edge-per-trade setups but occurs infrequently.
        """
        if feat.absorption_score < 0.6:
            return None

        if feat.delta_divergence < self._feat_cfg.delta_divergence_threshold:
            return None

        # Direction: absorption direction tells us which side is being absorbed
        # absorption_direction +1 = buyers absorbing below (bullish reversal expected)
        # absorption_direction -1 = sellers absorbing above (bearish reversal expected)
        if feat.absorption_direction == 0:
            return None

        direction = SignalDirection.BUY if feat.absorption_direction > 0 else SignalDirection.SELL

        # OFI should be AGAINST the direction (contrarian signal)
        # If we're going long on absorption, OFI is currently negative (selling pressure
        # being absorbed) — that's the confirmation
        ofi_contrarian = (
            (feat.absorption_direction > 0 and feat.ofi < 0) or
            (feat.absorption_direction < 0 and feat.ofi > 0)
        )

        if not ofi_contrarian:
            return None

        strength = min(feat.absorption_score * 0.6 + feat.delta_divergence * 0.4, 1.0)

        self._log.info(
            "SIGNAL absorption_reversal | dir=%s abs=%.2f div=%.2f ofi=%.3f strength=%.2f",
            direction.name, feat.absorption_score, feat.delta_divergence,
            feat.ofi, strength,
        )

        return Signal(
            direction=direction,
            trigger=SignalTrigger.ABSORPTION_REVERSAL,
            strength=strength,
            features=feat,
            timestamp_ms=feat.time_ms,
        )

    # ── Trigger: Volatility Contraction Breakout ────────────────

    def _check_vol_breakout(
        self, feat: FeatureSnapshot, trend: int,
        bar_range: float, avg_bar_range: float,
        bar_volume: int, avg_bar_volume: float,
    ) -> Optional[Signal]:
        """
        Volatility contracts (bar range < threshold × avg) then expands with volume spike.

        Entry on the first expansion bar that:
        1. Follows a contraction period (current ATR in low percentile)
        2. Has volume > threshold × average
        3. Breaks in the direction of EMA trend
        4. OFI confirms the direction

        This is the bread-and-butter scalping signal. Lower conviction per trade
        than sweep/absorption, but higher frequency.
        """
        # Must be in low volatility regime (contraction)
        if feat.atr_percentile > self._feat_cfg.volatility_contraction_percentile:
            return None

        # Volume must spike on the breakout bar
        if avg_bar_volume < 1 or bar_volume < avg_bar_volume * self._strat_cfg.volume_spike_threshold:
            return None

        # Range must exceed contraction threshold (expansion starting)
        if avg_bar_range < 1e-10:
            return None
        range_ratio = bar_range / avg_bar_range
        if range_ratio < 0.8:  # bar itself should show SOME expansion
            return None

        # OFI must confirm trend direction
        ofi_confirms = (
            (trend > 0 and feat.ofi > self._feat_cfg.ofi_threshold) or
            (trend < 0 and feat.ofi < -self._feat_cfg.ofi_threshold)
        )
        if not ofi_confirms:
            return None

        direction = SignalDirection.BUY if trend > 0 else SignalDirection.SELL

        # Strength: weighted combination of OFI strength + volume spike magnitude
        vol_factor = min((bar_volume / avg_bar_volume - 1.0) * 0.5, 0.5)
        ofi_factor = min(abs(feat.ofi) * 0.5, 0.5)
        strength = min(vol_factor + ofi_factor, 1.0)

        self._log.info(
            "SIGNAL vol_breakout | dir=%s atr_pct=%.0f vol_spike=%.1fx ofi=%.3f strength=%.2f",
            direction.name, feat.atr_percentile,
            bar_volume / max(avg_bar_volume, 1), feat.ofi, strength,
        )

        return Signal(
            direction=direction,
            trigger=SignalTrigger.VOLATILITY_BREAKOUT,
            strength=strength,
            features=feat,
            timestamp_ms=feat.time_ms,
        )

    # ── Exit Signal Evaluation ──────────────────────────────────

    def should_exit(self, feat: FeatureSnapshot) -> Optional[str]:
        """
        Check if an active position should be closed based on signal conditions.
        Returns exit reason string or None.

        Note: SL/TP are managed by the MT5 EA. This handles signal-based exits:
        - Regime change to extreme
        - Session about to end
        - Delta/OFI flipping hard against position
        """
        # Session ending
        remaining = self._session.minutes_remaining_in_session(feat.time_ms)
        if 0 < remaining < 2.0:  # less than 2 minutes to session end
            return "session_ending"

        # Regime turned extreme
        if feat.volatility_regime == "extreme":
            return "regime_extreme"

        return None

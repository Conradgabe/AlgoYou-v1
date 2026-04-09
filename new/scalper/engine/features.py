"""
Feature engine — computes all microstructure signals from raw tick and bar data.

Every feature is computed using numpy vectorized operations.
No loops in the hot path. No TA-Lib. No black boxes.

Features:
    OFI         — Order Flow Imbalance (Cont, Kukanov & Stoikov 2014 adapted for CFD)
    Delta       — Cumulative delta (buyer vs seller initiated ticks)
    VWAP        — Volume-weighted average price deviation
    ATR         — Average true range + volatility regime classification
    Absorption  — Large volume at a price level without price movement
    Sweep       — Rapid multi-level price traversal
    EMA         — Exponential moving averages for trend bias

All features return numpy scalars or small arrays — no objects, no allocation.
"""

import numpy as np
from typing import Optional, Tuple
from dataclasses import dataclass

from .tick_buffer import TickBuffer, BarAggregator, Bar
from .config import FeatureConfig


@dataclass(slots=True)
class FeatureSnapshot:
    """Immutable snapshot of all computed features at a point in time."""
    time_ms: int

    # Order Flow Imbalance: positive = buy pressure, negative = sell pressure
    # Normalized to [-1, 1]
    ofi: float

    # Cumulative delta: positive = net buying, negative = net selling
    # Raw value in ticks
    cum_delta: float
    # Delta-price divergence: high value = delta and price moving in opposite directions
    delta_divergence: float

    # VWAP deviation in standard deviations
    vwap_dev: float
    vwap_price: float

    # Volatility
    atr: float                  # Current ATR in price units
    atr_percentile: float       # Where current ATR sits relative to history [0-100]
    volatility_regime: str      # "low", "normal", "high", "extreme"

    # Structure
    ema_fast: float
    ema_slow: float
    trend_bias: int             # +1 bullish, -1 bearish, 0 neutral

    # Absorption: score 0-1, higher = more absorption detected
    absorption_score: float
    absorption_direction: int   # +1 = bullish absorption (sellers absorbed), -1 = bearish

    # Sweep: detected in last N ms
    sweep_detected: bool
    sweep_direction: int        # +1 = upward sweep, -1 = downward sweep
    sweep_levels: int           # number of levels swept

    # Spread
    current_spread: int         # points
    avg_spread: float           # rolling average spread


class FeatureEngine:
    """
    Stateful feature computer. Call update() on every tick.
    Recomputes features from the tick buffer and bar aggregator.
    """

    __slots__ = (
        "_cfg", "_ticks", "_bars",
        "_delta_acc", "_last_mid",
        "_price_levels", "_absorption_window",
        "_last_sweep_time", "_ema_fast", "_ema_slow",
        "_ema_fast_k", "_ema_slow_k", "_initialized",
    )

    def __init__(self, cfg: FeatureConfig, ticks: TickBuffer, bars: BarAggregator):
        self._cfg = cfg
        self._ticks = ticks
        self._bars = bars

        # Delta accumulator
        self._delta_acc: float = 0.0
        self._last_mid: float = 0.0

        # Absorption tracking: price_level → [total_volume, touch_count, first_time_ms]
        self._price_levels: dict = {}
        self._absorption_window: int = 5000  # ms to track absorption

        # Sweep tracking
        self._last_sweep_time: int = 0

        # EMA state (computed incrementally on each bar close)
        self._ema_fast: float = 0.0
        self._ema_slow: float = 0.0
        self._ema_fast_k: float = 2.0 / (cfg.ofi_window_ticks + 1)  # placeholder
        self._ema_slow_k: float = 2.0 / (cfg.ofi_window_ticks + 1)  # placeholder
        self._initialized: bool = False

    def initialize_emas(self, fast_period: int, slow_period: int) -> None:
        """Must be called after config is loaded with strategy params."""
        self._ema_fast_k = 2.0 / (fast_period + 1)
        self._ema_slow_k = 2.0 / (slow_period + 1)

    def on_bar_close(self, bar: Bar) -> None:
        """Update EMA state when a bar closes. Call from engine loop."""
        if not self._initialized:
            self._ema_fast = bar.close
            self._ema_slow = bar.close
            self._initialized = True
        else:
            self._ema_fast += self._ema_fast_k * (bar.close - self._ema_fast)
            self._ema_slow += self._ema_slow_k * (bar.close - self._ema_slow)

    def compute(self, current_time_ms: int) -> Optional[FeatureSnapshot]:
        """
        Compute all features from current buffer state.
        Returns None if insufficient data.
        """
        if self._ticks.count < 50 or self._bars.count < self._cfg.atr_period + 1:
            return None

        tick = self._ticks.last_tick
        if tick is None:
            return None

        mid = tick.mid

        # ── OFI ─────────────────────────────────────────────
        ofi = self._compute_ofi()

        # ── Cumulative Delta ────────────────────────────────
        cum_delta, delta_div = self._compute_delta(mid, current_time_ms)

        # ── VWAP ────────────────────────────────────────────
        vwap_price, vwap_dev = self._compute_vwap()

        # ── ATR + Volatility Regime ─────────────────────────
        atr, atr_pct, regime = self._compute_atr()

        # ── Absorption ──────────────────────────────────────
        abs_score, abs_dir = self._compute_absorption(mid, tick.volume, current_time_ms)

        # ── Sweep ───────────────────────────────────────────
        sweep, sweep_dir, sweep_lvl = self._compute_sweep(current_time_ms)

        # ── Spread ──────────────────────────────────────────
        _, _, _, _, spreads = self._ticks.last_n(100)
        avg_spread = float(np.mean(spreads)) if len(spreads) > 0 else float(tick.spread)

        # ── Trend bias ──────────────────────────────────────
        if self._ema_fast > self._ema_slow * 1.0001:
            trend = 1
        elif self._ema_fast < self._ema_slow * 0.9999:
            trend = -1
        else:
            trend = 0

        self._last_mid = mid

        return FeatureSnapshot(
            time_ms=current_time_ms,
            ofi=ofi,
            cum_delta=cum_delta,
            delta_divergence=delta_div,
            vwap_dev=vwap_dev,
            vwap_price=vwap_price,
            atr=atr,
            atr_percentile=atr_pct,
            volatility_regime=regime,
            ema_fast=self._ema_fast,
            ema_slow=self._ema_slow,
            trend_bias=trend,
            absorption_score=abs_score,
            absorption_direction=abs_dir,
            sweep_detected=sweep,
            sweep_direction=sweep_dir,
            sweep_levels=sweep_lvl,
            current_spread=tick.spread,
            avg_spread=avg_spread,
        )

    # ── OFI ─────────────────────────────────────────────────────

    def _compute_ofi(self) -> float:
        """
        Order Flow Imbalance based on bid/ask changes.

        Adapted from Cont, Kukanov & Stoikov (2014) for quote-driven markets.
        OFI = Σ(Δbid - Δask) over a rolling window, normalized.

        Positive OFI → buying pressure (bid rising faster than ask)
        Negative OFI → selling pressure (ask falling faster than bid)
        """
        n = self._cfg.ofi_window_ticks
        bids = self._ticks.last_n_bids(n)
        asks = self._ticks.last_n_asks(n)

        if len(bids) < 10:
            return 0.0

        # First differences
        d_bid = np.diff(bids)
        d_ask = np.diff(asks)

        # Raw OFI
        raw_ofi = np.sum(d_bid - d_ask)

        # Normalize by average absolute movement to get [-1, 1] range
        total_movement = np.sum(np.abs(d_bid)) + np.sum(np.abs(d_ask))
        if total_movement < 1e-10:
            return 0.0

        return float(np.clip(raw_ofi / total_movement, -1.0, 1.0))

    # ── Delta ───────────────────────────────────────────────────

    def _compute_delta(self, current_mid: float, current_time_ms: int) -> Tuple[float, float]:
        """
        Cumulative delta: classifies each tick as buyer or seller initiated
        based on mid price movement direction.

        Delta divergence: when delta is positive but price is falling (or vice versa),
        it signals absorption — large players absorbing aggression without letting price move.
        """
        n = self._cfg.delta_window_ticks
        bids, asks, times, vols, _ = self._ticks.last_n(n)

        if len(bids) < 10:
            return 0.0, 0.0

        mids = (bids + asks) * 0.5
        mid_changes = np.diff(mids)

        # Classify: positive mid change = buyer initiated, negative = seller
        buyer_vol = np.sum(vols[1:][mid_changes > 0])
        seller_vol = np.sum(vols[1:][mid_changes < 0])
        cum_delta = float(buyer_vol - seller_vol)

        # Delta divergence: compare delta direction vs price direction
        # over the window
        price_change = mids[-1] - mids[0] if len(mids) > 1 else 0.0

        if abs(cum_delta) < 1 or abs(price_change) < 1e-10:
            divergence = 0.0
        else:
            # Divergence is high when delta and price move in opposite directions
            delta_sign = 1.0 if cum_delta > 0 else -1.0
            price_sign = 1.0 if price_change > 0 else -1.0

            if delta_sign != price_sign:
                # They disagree — normalize divergence magnitude
                total_vol = buyer_vol + seller_vol
                divergence = float(abs(cum_delta) / max(total_vol, 1))
            else:
                divergence = 0.0

        return cum_delta, divergence

    # ── VWAP ────────────────────────────────────────────────────

    def _compute_vwap(self) -> Tuple[float, float]:
        """
        Rolling VWAP computed from bars (not ticks — too noisy).
        VWAP = Σ(typical_price × volume) / Σ(volume)
        Deviation expressed in standard deviations of price from VWAP.
        """
        n = self._cfg.vwap_period_minutes
        bars = self._bars.last_n_bars(n)

        if len(bars) < 3:
            return 0.0, 0.0

        # Typical price = (H+L+C)/3 — better than close alone for VWAP
        tp = np.array([(b.high + b.low + b.close) / 3.0 for b in bars])
        vol = np.array([b.tick_count for b in bars], dtype=np.float64)
        vol_sum = vol.sum()

        if vol_sum < 1:
            return 0.0, 0.0

        vwap = float(np.sum(tp * vol) / vol_sum)

        # Standard deviation of price around VWAP
        closes = np.array([b.close for b in bars])
        std = float(np.std(closes))

        if std < 1e-10:
            return vwap, 0.0

        current_price = bars[-1].close
        deviation = (current_price - vwap) / std

        return vwap, float(deviation)

    # ── ATR + Regime ────────────────────────────────────────────

    def _compute_atr(self) -> Tuple[float, float, str]:
        """
        ATR on 1-minute bars using Wilder's smoothing.
        Regime classification based on percentile rank of current ATR
        relative to lookback window.
        """
        period = self._cfg.atr_period
        lookback = self._cfg.volatility_lookback_bars
        n_needed = max(period + 1, lookback)
        bars = self._bars.last_n_bars(n_needed)

        if len(bars) < period + 1:
            return 0.0, 50.0, "normal"

        highs = np.array([b.high for b in bars])
        lows = np.array([b.low for b in bars])
        closes = np.array([b.close for b in bars])

        # True Range
        tr = np.maximum(
            highs[1:] - lows[1:],
            np.maximum(
                np.abs(highs[1:] - closes[:-1]),
                np.abs(lows[1:] - closes[:-1]),
            ),
        )

        if len(tr) < period:
            return 0.0, 50.0, "normal"

        # Wilder's smoothed ATR
        atr_values = np.empty(len(tr) - period + 1)
        atr_values[0] = np.mean(tr[:period])
        for i in range(1, len(atr_values)):
            atr_values[i] = (atr_values[i - 1] * (period - 1) + tr[period - 1 + i]) / period

        current_atr = float(atr_values[-1])

        # Percentile rank
        if len(atr_values) < 5:
            percentile = 50.0
        else:
            percentile = float(np.sum(atr_values < current_atr) / len(atr_values) * 100)

        # Regime
        if percentile < 20:
            regime = "low"
        elif percentile < 60:
            regime = "normal"
        elif percentile < 85:
            regime = "high"
        else:
            regime = "extreme"

        return current_atr, percentile, regime

    # ── Absorption ──────────────────────────────────────────────

    def _compute_absorption(
        self, mid: float, volume: int, current_time_ms: int
    ) -> Tuple[float, int]:
        """
        Detects price levels where large volume is transacted but price doesn't move.

        Logic:
        1. Quantize price to nearest N points
        2. Track cumulative volume at each level
        3. If volume at level > threshold × average AND price hasn't broken the level → absorption

        Absorption is institutional: a large player is resting orders and absorbing
        market orders without letting price move through their level.
        """
        tol = self._cfg.absorption_price_tolerance_points * 0.01  # convert points to price
        level = round(mid / tol) * tol

        # Clean old entries
        stale = current_time_ms - self._absorption_window
        self._price_levels = {
            k: v for k, v in self._price_levels.items()
            if v[2] > stale
        }

        # Update current level
        if level in self._price_levels:
            entry = self._price_levels[level]
            entry[0] += volume
            entry[1] += 1
        else:
            self._price_levels[level] = [volume, 1, current_time_ms]

        # Score
        if not self._price_levels:
            return 0.0, 0

        volumes = [v[0] for v in self._price_levels.values()]
        touches = [v[1] for v in self._price_levels.values()]
        avg_vol = sum(volumes) / len(volumes) if volumes else 1

        # Find level with highest absorption signal
        best_score = 0.0
        best_dir = 0
        for lvl, (vol, tch, _) in self._price_levels.items():
            if tch < self._cfg.absorption_min_touches:
                continue
            if avg_vol < 1:
                continue
            score = (vol / avg_vol) * min(tch / 5.0, 1.0)

            if score > best_score:
                best_score = score
                # If absorption is above current price → bearish (sellers absorbed = bullish reversal)
                # If below → bullish (buyers absorbed = bearish reversal)
                best_dir = 1 if lvl < mid else -1

        # Normalize score to 0-1
        normalized = min(best_score / self._cfg.absorption_volume_threshold, 1.0)
        return normalized, best_dir

    # ── Sweep ───────────────────────────────────────────────────

    def _compute_sweep(self, current_time_ms: int) -> Tuple[bool, int, int]:
        """
        Detects rapid multi-level price sweeps.

        A sweep is when price traverses N+ distinct levels within a short time window.
        This indicates an aggressive informed participant urgently entering/exiting.
        Sweeps often precede continuation in the sweep direction.
        """
        window_ms = self._cfg.sweep_time_window_ms
        cutoff = current_time_ms - window_ms

        bids, asks, times, _, _ = self._ticks.since(cutoff)

        if len(bids) < 5:
            return False, 0, 0

        mids = (bids + asks) * 0.5
        tick_size = 0.01  # gold tick size

        # Quantize to tick levels
        levels = np.round(mids / tick_size).astype(np.int64)
        unique_levels = np.unique(levels)
        n_levels = len(unique_levels)

        if n_levels < self._cfg.sweep_levels_threshold:
            return False, 0, 0

        # Direction: net movement
        direction = 1 if mids[-1] > mids[0] else -1

        return True, direction, n_levels

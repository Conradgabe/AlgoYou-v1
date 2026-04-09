"""Tests for the feature engine."""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
import numpy as np
from engine.tick_buffer import TickBuffer, BarAggregator, Tick, Bar
from engine.features import FeatureEngine, FeatureSnapshot
from engine.config import FeatureConfig


@pytest.fixture
def feat_cfg():
    return FeatureConfig(
        ofi_window_ticks=50,
        ofi_threshold=0.6,
        delta_window_ticks=100,
        delta_divergence_threshold=0.7,
        vwap_period_minutes=5,
        vwap_deviation_threshold=1.0,
        atr_period=14,
        atr_timeframe_minutes=1,
        volatility_lookback_bars=50,
        volatility_contraction_percentile=20,
        absorption_volume_threshold=3.0,
        absorption_price_tolerance_points=5,
        absorption_min_touches=3,
        sweep_levels_threshold=3,
        sweep_time_window_ms=500,
        tick_buffer_size=10000,
        bar_buffer_size=500,
    )


@pytest.fixture
def setup(feat_cfg):
    ticks = TickBuffer(max_size=10000)
    bars = BarAggregator(period_minutes=1, max_bars=500)
    engine = FeatureEngine(feat_cfg, ticks, bars)
    engine.initialize_emas(fast_period=8, slow_period=20)
    return ticks, bars, engine


def make_tick(bid, ask, time_ms, volume=1, spread=10):
    return Tick(bid=bid, ask=ask, time_ms=time_ms, volume=volume, spread=spread)


def populate_data(ticks, bars, engine, n_bars=20, base_price=2340.0):
    """Populate enough data for features to compute."""
    np.random.seed(42)
    tick_time = 60_000  # start at minute 1 (ms)
    for minute in range(n_bars + 1):
        bar_start = (minute + 1) * 60_000  # each bar at a distinct minute boundary
        for i in range(10):  # 10 ticks per bar
            price = base_price + np.random.randn() * 0.5
            t = make_tick(price, price + 0.20, bar_start + i * 100,
                          volume=1 + abs(int(np.random.randn() * 3)))
            ticks.add(t)
            bar = bars.update(t)
            if bar is not None:
                engine.on_bar_close(bar)
    tick_time = bar_start + 9 * 100  # last tick time


def test_returns_none_insufficient_data(setup):
    ticks, bars, engine = setup
    # Only add a few ticks — not enough
    for i in range(10):
        ticks.add(make_tick(2340.0, 2340.2, i + 1))
    result = engine.compute(10)
    assert result is None


def test_returns_snapshot_with_enough_data(setup):
    ticks, bars, engine = setup
    populate_data(ticks, bars, engine, n_bars=20)
    result = engine.compute(ticks.last_time_ms)
    assert result is not None
    assert isinstance(result, FeatureSnapshot)


def test_ofi_range(setup):
    ticks, bars, engine = setup
    populate_data(ticks, bars, engine, n_bars=20)
    result = engine.compute(ticks.last_time_ms)
    assert -1.0 <= result.ofi <= 1.0


def test_ofi_bullish_pressure(setup):
    """When bids are consistently rising, OFI should be positive."""
    ticks, bars, engine = setup

    # Create ticks where bids rise faster than asks (buying pressure)
    for minute in range(20):
        for i in range(10):
            time_ms = (minute + 1) * 60000 + i * 100
            bid = 2340.0 + minute * 0.5 + i * 0.02  # bids rise faster
            ask = 2340.2 + minute * 0.3 + i * 0.005  # asks rise slower
            t = make_tick(bid, ask, time_ms)
            ticks.add(t)
            bar = bars.update(t)
            if bar:
                engine.on_bar_close(bar)

    result = engine.compute(ticks.last_time_ms)
    assert result is not None
    assert result.ofi > 0  # should detect buying pressure


def test_atr_computed(setup):
    ticks, bars, engine = setup
    populate_data(ticks, bars, engine, n_bars=20)
    result = engine.compute(ticks.last_time_ms)
    assert result.atr > 0


def test_volatility_regime_valid(setup):
    ticks, bars, engine = setup
    populate_data(ticks, bars, engine, n_bars=60)
    result = engine.compute(ticks.last_time_ms)
    assert result.volatility_regime in ("low", "normal", "high", "extreme")


def test_spread_tracking(setup):
    ticks, bars, engine = setup
    populate_data(ticks, bars, engine, n_bars=20)
    result = engine.compute(ticks.last_time_ms)
    assert result.current_spread > 0
    assert result.avg_spread > 0


def test_trend_bias_values(setup):
    ticks, bars, engine = setup
    populate_data(ticks, bars, engine, n_bars=20)
    result = engine.compute(ticks.last_time_ms)
    assert result.trend_bias in (-1, 0, 1)

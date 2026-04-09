"""Tests for tick buffer and bar aggregator."""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
import numpy as np
from engine.tick_buffer import TickBuffer, BarAggregator, Tick


@pytest.fixture
def buf():
    return TickBuffer(max_size=100)


def make_tick(bid, ask, time_ms, volume=1, spread=10):
    return Tick(bid=bid, ask=ask, time_ms=time_ms, volume=volume, spread=spread)


# ── TickBuffer tests ────────────────────────────────────────────

def test_empty_buffer(buf):
    assert buf.is_empty
    assert buf.count == 0
    assert buf.last_tick is None


def test_add_tick(buf):
    t = make_tick(2340.50, 2340.70, 1000)
    assert buf.add(t)
    assert buf.count == 1
    assert not buf.is_empty


def test_deduplicate(buf):
    t1 = make_tick(2340.50, 2340.70, 1000)
    t2 = make_tick(2340.55, 2340.75, 1000)  # same timestamp
    assert buf.add(t1)
    assert not buf.add(t2)  # rejected — same time
    assert buf.count == 1


def test_reject_older(buf):
    t1 = make_tick(2340.50, 2340.70, 1000)
    t2 = make_tick(2340.55, 2340.75, 999)  # older
    assert buf.add(t1)
    assert not buf.add(t2)


def test_last_tick(buf):
    buf.add(make_tick(2340.50, 2340.70, 1000))
    buf.add(make_tick(2341.00, 2341.20, 1001))
    t = buf.last_tick
    assert t.bid == 2341.00
    assert t.time_ms == 1001


def test_last_n(buf):
    for i in range(10):
        buf.add(make_tick(2340.0 + i * 0.1, 2340.2 + i * 0.1, 1000 + i))

    bids, asks, times, vols, spreads = buf.last_n(5)
    assert len(bids) == 5
    assert bids[-1] == pytest.approx(2340.9, abs=0.01)
    assert times[0] == 1005


def test_ring_buffer_wraps():
    buf = TickBuffer(max_size=5)
    for i in range(10):
        buf.add(make_tick(float(i), float(i) + 0.2, 1000 + i))

    assert buf.count == 5  # max_size
    bids, _, _, _, _ = buf.last_n(5)
    assert bids[0] == pytest.approx(5.0)
    assert bids[-1] == pytest.approx(9.0)


def test_since(buf):
    for i in range(20):
        buf.add(make_tick(2340.0, 2340.2, 1000 + i))

    bids, _, times, _, _ = buf.since(1015)
    assert len(bids) == 5
    assert times[0] == 1015


def test_mid_price():
    t = make_tick(2340.50, 2340.70, 1000)
    assert t.mid == pytest.approx(2340.60)


# ── BarAggregator tests ────────────────────────────────────────

def test_bar_aggregation():
    agg = BarAggregator(period_minutes=1)

    # First bar: ticks within minute 0 (0-59999 ms)
    base_ms = 60000  # start at minute 1
    completed = None

    for i in range(5):
        t = make_tick(2340.0 + i * 0.5, 2340.2 + i * 0.5, base_ms + i * 100)
        completed = agg.update(t)

    assert completed is None  # bar not finished yet

    # Tick in next minute — triggers bar close
    t = make_tick(2343.0, 2343.2, base_ms + 60000)
    completed = agg.update(t)

    assert completed is not None
    assert completed.open == pytest.approx(2340.1, abs=0.01)  # mid of first tick
    assert completed.high == pytest.approx(2342.1, abs=0.01)  # mid of highest
    assert completed.low == pytest.approx(2340.1, abs=0.01)
    assert completed.tick_count == 5


def test_bar_range():
    agg = BarAggregator(period_minutes=1)
    base = 60000
    agg.update(make_tick(100.0, 100.2, base))
    agg.update(make_tick(102.0, 102.2, base + 100))
    agg.update(make_tick(99.0, 99.2, base + 200))

    # Force bar close
    bar = agg.update(make_tick(101.0, 101.2, base + 60000))
    assert bar is not None
    assert bar.range == pytest.approx(3.0, abs=0.1)  # 102.1 - 99.1


def test_bar_buffer_limit():
    agg = BarAggregator(period_minutes=1, max_bars=3)
    base = 0
    for minute in range(5):
        for i in range(2):
            agg.update(make_tick(2340.0, 2340.2, base + minute * 60000 + i * 100))

    assert agg.count <= 3


def test_ohlc_arrays():
    agg = BarAggregator(period_minutes=1)
    base = 0
    for minute in range(5):
        for i in range(2):
            agg.update(make_tick(2340.0 + minute, 2340.2 + minute,
                                 base + minute * 60000 + i * 100))

    opens, highs, lows, closes = agg.get_ohlc_arrays(3)
    assert len(opens) == 3

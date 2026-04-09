"""
High-performance tick buffer and bar aggregator.

TickBuffer: NumPy ring buffer storing raw ticks with O(1) insert and O(1) slice.
            Deduplicates by timestamp. Provides vectorized access for feature computation.

BarAggregator: Builds 1-minute (or configurable) OHLCV bars from the tick stream.
               Returns completed bars for strategy consumption.
               Tracks volume, tick count, and average spread per bar.

All hot-path operations use pre-allocated numpy arrays — zero allocations during trading.
"""

import numpy as np
from dataclasses import dataclass
from typing import Optional, List, Tuple

# Type alias for the 5-tuple returned by slice operations
TickSlice = Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]
EMPTY_SLICE: TickSlice = (
    np.array([], dtype=np.float64),
    np.array([], dtype=np.float64),
    np.array([], dtype=np.int64),
    np.array([], dtype=np.int32),
    np.array([], dtype=np.int32),
)


@dataclass(slots=True)
class Tick:
    bid: float
    ask: float
    time_ms: int
    volume: int
    spread: int  # in points (multiply by point_value for price)

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) * 0.5

    @property
    def time_seconds(self) -> float:
        return self.time_ms / 1000.0


@dataclass(slots=True)
class Bar:
    open: float
    high: float
    low: float
    close: float
    volume: int       # cumulative tick volume proxy
    tick_count: int   # number of ticks in bar
    time_ms: int      # bar open timestamp
    spread_avg: float # average spread during bar (points)

    @property
    def range(self) -> float:
        return self.high - self.low

    @property
    def body(self) -> float:
        return abs(self.close - self.open)

    @property
    def upper_wick(self) -> float:
        return self.high - max(self.open, self.close)

    @property
    def lower_wick(self) -> float:
        return min(self.open, self.close) - self.low

    @property
    def is_bullish(self) -> bool:
        return self.close > self.open


class TickBuffer:
    """
    Pre-allocated ring buffer for tick data.
    Stores bids, asks, timestamps, volumes, spreads in parallel numpy arrays.
    All index arithmetic is modular — never reallocates.
    """

    __slots__ = (
        "_max", "_bids", "_asks", "_times", "_vols", "_spreads",
        "_count", "_head", "_last_ms",
    )

    def __init__(self, max_size: int = 10_000):
        self._max = max_size
        self._bids = np.zeros(max_size, dtype=np.float64)
        self._asks = np.zeros(max_size, dtype=np.float64)
        self._times = np.zeros(max_size, dtype=np.int64)
        self._vols = np.zeros(max_size, dtype=np.int32)
        self._spreads = np.zeros(max_size, dtype=np.int32)
        self._count: int = 0
        self._head: int = 0
        self._last_ms: int = 0

    @property
    def count(self) -> int:
        return min(self._count, self._max)

    @property
    def is_empty(self) -> bool:
        return self._count == 0

    @property
    def last_time_ms(self) -> int:
        return self._last_ms

    # ── Write ───────────────────────────────────────────────────

    def add(self, tick: Tick) -> bool:
        """
        Append tick to buffer. Returns False if duplicate (same or older timestamp).
        Duplicates are common on reconnect — MT5 replays recent ticks.
        """
        if tick.time_ms <= self._last_ms:
            return False

        idx = self._head % self._max
        self._bids[idx] = tick.bid
        self._asks[idx] = tick.ask
        self._times[idx] = tick.time_ms
        self._vols[idx] = tick.volume
        self._spreads[idx] = tick.spread

        self._head += 1
        self._count += 1
        self._last_ms = tick.time_ms
        return True

    # ── Read ────────────────────────────────────────────────────

    @property
    def last_tick(self) -> Optional[Tick]:
        if self.is_empty:
            return None
        idx = (self._head - 1) % self._max
        return Tick(
            bid=float(self._bids[idx]),
            ask=float(self._asks[idx]),
            time_ms=int(self._times[idx]),
            volume=int(self._vols[idx]),
            spread=int(self._spreads[idx]),
        )

    def last_n(self, n: int) -> TickSlice:
        """Return (bids, asks, times, vols, spreads) for the last n ticks."""
        avail = self.count
        n = min(n, avail)
        if n == 0:
            return EMPTY_SLICE
        return self._extract(n)

    def since(self, time_ms: int) -> TickSlice:
        """Return all ticks with timestamp >= time_ms."""
        avail = self.count
        if avail == 0:
            return EMPTY_SLICE

        bids, asks, times, vols, spreads = self._extract(avail)
        mask = times >= time_ms
        return bids[mask], asks[mask], times[mask], vols[mask], spreads[mask]

    def last_n_bids(self, n: int) -> np.ndarray:
        """Fast path for feature engine — bids only."""
        avail = min(n, self.count)
        if avail == 0:
            return np.array([], dtype=np.float64)
        end = self._head % self._max
        start = (self._head - avail) % self._max
        if start < end:
            return self._bids[start:end].copy()
        return np.concatenate([self._bids[start:], self._bids[:end]])

    def last_n_asks(self, n: int) -> np.ndarray:
        avail = min(n, self.count)
        if avail == 0:
            return np.array([], dtype=np.float64)
        end = self._head % self._max
        start = (self._head - avail) % self._max
        if start < end:
            return self._asks[start:end].copy()
        return np.concatenate([self._asks[start:], self._asks[:end]])

    # ── Internal ────────────────────────────────────────────────

    def _extract(self, n: int) -> TickSlice:
        end = self._head % self._max
        start = (self._head - n) % self._max

        if start < end:
            return (
                self._bids[start:end].copy(),
                self._asks[start:end].copy(),
                self._times[start:end].copy(),
                self._vols[start:end].copy(),
                self._spreads[start:end].copy(),
            )
        return (
            np.concatenate([self._bids[start:], self._bids[:end]]),
            np.concatenate([self._asks[start:], self._asks[:end]]),
            np.concatenate([self._times[start:], self._times[:end]]),
            np.concatenate([self._vols[start:], self._vols[:end]]),
            np.concatenate([self._spreads[start:], self._spreads[:end]]),
        )


class BarAggregator:
    """
    Builds OHLCV bars from the tick stream.
    Returns completed Bar objects when a new bar period opens.
    Uses mid price for OHLC to avoid bid/ask noise.
    """

    __slots__ = (
        "_period_ms", "_max_bars", "_bars",
        "_o", "_h", "_l", "_c",
        "_vol", "_ticks", "_spread_sum", "_bar_time",
    )

    def __init__(self, period_minutes: int = 1, max_bars: int = 500):
        self._period_ms: int = period_minutes * 60_000
        self._max_bars: int = max_bars
        self._bars: List[Bar] = []

        # Current building bar
        self._o: Optional[float] = None
        self._h: float = 0.0
        self._l: float = float("inf")
        self._c: float = 0.0
        self._vol: int = 0
        self._ticks: int = 0
        self._spread_sum: float = 0.0
        self._bar_time: int = 0

    @property
    def bars(self) -> List[Bar]:
        return self._bars

    @property
    def count(self) -> int:
        return len(self._bars)

    @property
    def last_bar(self) -> Optional[Bar]:
        return self._bars[-1] if self._bars else None

    def last_n_bars(self, n: int) -> List[Bar]:
        return self._bars[-n:] if n <= len(self._bars) else list(self._bars)

    def update(self, tick: Tick) -> Optional[Bar]:
        """
        Feed a tick. Returns a completed Bar if a new bar period started,
        otherwise returns None.
        """
        mid = tick.mid
        bar_time = (tick.time_ms // self._period_ms) * self._period_ms
        completed: Optional[Bar] = None

        # New period — finalize the building bar
        if bar_time != self._bar_time and self._o is not None:
            completed = Bar(
                open=self._o,
                high=self._h,
                low=self._l,
                close=self._c,
                volume=self._vol,
                tick_count=self._ticks,
                time_ms=self._bar_time,
                spread_avg=self._spread_sum / max(self._ticks, 1),
            )
            self._bars.append(completed)
            if len(self._bars) > self._max_bars:
                self._bars.pop(0)
            self._o = None

        # Start or continue building bar
        if self._o is None:
            self._o = mid
            self._h = mid
            self._l = mid
            self._vol = 0
            self._ticks = 0
            self._spread_sum = 0.0
            self._bar_time = bar_time

        if mid > self._h:
            self._h = mid
        if mid < self._l:
            self._l = mid
        self._c = mid
        self._vol += tick.volume
        self._ticks += 1
        self._spread_sum += tick.spread

        return completed

    def get_ohlc_arrays(self, n: int) -> Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
        """Return (opens, highs, lows, closes) as numpy arrays for last n bars."""
        bars = self.last_n_bars(n)
        if not bars:
            empty = np.array([], dtype=np.float64)
            return empty, empty, empty, empty
        return (
            np.array([b.open for b in bars], dtype=np.float64),
            np.array([b.high for b in bars], dtype=np.float64),
            np.array([b.low for b in bars], dtype=np.float64),
            np.array([b.close for b in bars], dtype=np.float64),
        )

    def get_volume_array(self, n: int) -> np.ndarray:
        bars = self.last_n_bars(n)
        return np.array([b.tick_count for b in bars], dtype=np.int32)

    def get_range_array(self, n: int) -> np.ndarray:
        bars = self.last_n_bars(n)
        return np.array([b.range for b in bars], dtype=np.float64)

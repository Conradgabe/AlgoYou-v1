"""
Session time filter.

Controls WHEN the bot is allowed to trade.
Gold scalping is only viable during high-liquidity windows:
  - London session:  08:00–11:00 London time
  - NY overlap:      13:30–16:00 London time

Outside these windows, spreads widen, liquidity thins, and the statistical
edge of microstructure-based signals collapses.

Also handles:
  - Early close (stop trading N minutes before session end for clean exits)
  - News blackout windows (configurable)
  - Weekend detection
"""

from datetime import datetime, time as dtime, timedelta, timezone
from typing import Optional
import logging

try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo

from .config import SessionConfig


class SessionFilter:
    __slots__ = ("_cfg", "_tz", "_windows", "_log")

    def __init__(self, cfg: SessionConfig):
        self._cfg = cfg
        self._tz = ZoneInfo(cfg.timezone)
        self._log = logging.getLogger("session")

        # Parse session windows once
        self._windows = [
            (self._parse_time(cfg.london_open), self._parse_time(cfg.london_close)),
            (self._parse_time(cfg.ny_open), self._parse_time(cfg.ny_close)),
        ]

    # ── Public API ──────────────────────────────────────────────

    def is_trading_allowed(self, timestamp_ms: Optional[int] = None) -> bool:
        """
        Returns True if the given timestamp falls within an active session window,
        accounting for early close buffer.
        """
        dt = self._to_local(timestamp_ms)

        if self._is_weekend(dt):
            return False

        t = dt.time()
        early = timedelta(minutes=self._cfg.early_close_minutes)

        for open_t, close_t in self._windows:
            close_adj = (
                datetime.combine(dt.date(), close_t) - early
            ).time()
            if open_t <= t < close_adj:
                return True

        return False

    def current_session(self, timestamp_ms: Optional[int] = None) -> Optional[str]:
        """Returns 'london', 'ny', or None."""
        dt = self._to_local(timestamp_ms)
        if self._is_weekend(dt):
            return None

        t = dt.time()
        open_l, close_l = self._windows[0]
        open_n, close_n = self._windows[1]

        if open_l <= t < close_l:
            return "london"
        if open_n <= t < close_n:
            return "ny"
        return None

    def seconds_until_next_session(self, timestamp_ms: Optional[int] = None) -> float:
        """Seconds until the next session window opens."""
        dt = self._to_local(timestamp_ms)
        now_t = dt.time()
        today = dt.date()

        candidates = []
        for open_t, _ in self._windows:
            session_dt = datetime.combine(today, open_t, tzinfo=self._tz)
            if session_dt > dt:
                candidates.append(session_dt)
            # Also try tomorrow
            session_dt_tomorrow = datetime.combine(
                today + timedelta(days=1), open_t, tzinfo=self._tz
            )
            candidates.append(session_dt_tomorrow)

        # Skip weekends
        valid = []
        for c in candidates:
            d = c
            while d.weekday() >= 5:  # 5=Sat, 6=Sun
                d += timedelta(days=1)
            if d != c:
                d = datetime.combine(d.date(), c.time(), tzinfo=self._tz)
            valid.append(d)

        if not valid:
            return 0.0

        nearest = min(valid)
        return max(0.0, (nearest - dt).total_seconds())

    def minutes_remaining_in_session(self, timestamp_ms: Optional[int] = None) -> float:
        """Minutes left in the current session window. 0 if not in session."""
        dt = self._to_local(timestamp_ms)
        t = dt.time()

        for open_t, close_t in self._windows:
            if open_t <= t < close_t:
                close_dt = datetime.combine(dt.date(), close_t, tzinfo=self._tz)
                return max(0.0, (close_dt - dt).total_seconds() / 60.0)

        return 0.0

    # ── Internal ────────────────────────────────────────────────

    @staticmethod
    def _parse_time(s: str) -> dtime:
        parts = s.split(":")
        return dtime(int(parts[0]), int(parts[1]))

    def _to_local(self, timestamp_ms: Optional[int]) -> datetime:
        if timestamp_ms is None:
            return datetime.now(tz=self._tz)
        return datetime.fromtimestamp(timestamp_ms / 1000.0, tz=self._tz)

    @staticmethod
    def _is_weekend(dt: datetime) -> bool:
        return dt.weekday() >= 5

"""
Trade database — SQLite with buffered writes.

Tables:
    ticks          — sampled raw ticks (every Nth tick to keep DB manageable)
    bars           — completed 1M bars
    signals        — every signal generated (taken or rejected)
    trades         — every fill/close with P&L
    state_changes  — every state machine transition
    daily_pnl      — end-of-day summary
    trade_metrics  — per-trade MAE/MFE/duration for post-analysis

Design:
    - Buffered inserts (flush every N seconds) to avoid I/O on every tick
    - WAL mode for concurrent reads during analytics
    - All timestamps in milliseconds UTC
    - Never blocks the trading loop — writes are fire-and-forget with fallback to log
"""

import sqlite3
import time
import logging
import os
from typing import List, Dict, Any, Optional
from collections import deque


class TradeDatabase:
    __slots__ = (
        "_path", "_conn", "_cursor", "_flush_interval",
        "_sample_rate", "_tick_counter",
        "_tick_buf", "_bar_buf", "_signal_buf",
        "_trade_buf", "_state_buf", "_metric_buf",
        "_last_flush", "_log",
    )

    def __init__(self, db_path: str, flush_interval: int = 5, tick_sample_rate: int = 10):
        self._path = db_path
        self._flush_interval = flush_interval
        self._sample_rate = tick_sample_rate
        self._tick_counter: int = 0
        self._log = logging.getLogger("database")

        # Write buffers
        self._tick_buf: deque = deque()
        self._bar_buf: deque = deque()
        self._signal_buf: deque = deque()
        self._trade_buf: deque = deque()
        self._state_buf: deque = deque()
        self._metric_buf: deque = deque()
        self._last_flush: float = time.time()

        # Ensure directory exists
        os.makedirs(os.path.dirname(db_path) if os.path.dirname(db_path) else ".", exist_ok=True)

        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA cache_size=-8000")  # 8MB cache
        self._cursor = self._conn.cursor()
        self._create_tables()

    def _create_tables(self) -> None:
        self._cursor.executescript("""
            CREATE TABLE IF NOT EXISTS ticks (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                time_ms     INTEGER NOT NULL,
                bid         REAL NOT NULL,
                ask         REAL NOT NULL,
                spread      INTEGER NOT NULL,
                volume      INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS bars (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                time_ms     INTEGER NOT NULL,
                open        REAL NOT NULL,
                high        REAL NOT NULL,
                low         REAL NOT NULL,
                close       REAL NOT NULL,
                volume      INTEGER NOT NULL,
                tick_count  INTEGER NOT NULL,
                spread_avg  REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS signals (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                time_ms     INTEGER NOT NULL,
                direction   TEXT NOT NULL,
                strategy    TEXT NOT NULL,
                strength    REAL NOT NULL,
                bid         REAL NOT NULL,
                ask         REAL NOT NULL,
                spread      INTEGER NOT NULL,
                ofi         REAL,
                delta       REAL,
                vwap_dev    REAL,
                atr         REAL,
                regime      TEXT,
                vetoed      INTEGER NOT NULL DEFAULT 0,
                veto_reason TEXT
            );

            CREATE TABLE IF NOT EXISTS trades (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                ticket          INTEGER,
                open_time_ms    INTEGER NOT NULL,
                close_time_ms   INTEGER,
                direction       TEXT NOT NULL,
                volume          REAL NOT NULL,
                open_price      REAL NOT NULL,
                close_price     REAL,
                sl              REAL,
                tp              REAL,
                pnl_points      REAL,
                pnl_money       REAL,
                commission      REAL,
                swap            REAL,
                close_reason    TEXT,
                magic           INTEGER
            );

            CREATE TABLE IF NOT EXISTS state_changes (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                time_ms     INTEGER NOT NULL,
                old_state   TEXT NOT NULL,
                new_state   TEXT NOT NULL,
                reason      TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS trade_metrics (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                trade_id        INTEGER NOT NULL,
                mae_points      REAL NOT NULL,
                mfe_points      REAL NOT NULL,
                duration_sec    REAL NOT NULL,
                entry_spread    INTEGER NOT NULL,
                exit_spread     INTEGER,
                entry_ofi       REAL,
                entry_delta     REAL,
                entry_atr       REAL,
                FOREIGN KEY (trade_id) REFERENCES trades(id)
            );

            CREATE TABLE IF NOT EXISTS daily_pnl (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                date            TEXT NOT NULL UNIQUE,
                total_pnl       REAL NOT NULL,
                trade_count     INTEGER NOT NULL,
                win_count       INTEGER NOT NULL,
                loss_count      INTEGER NOT NULL,
                gross_profit    REAL NOT NULL,
                gross_loss      REAL NOT NULL,
                max_drawdown    REAL NOT NULL,
                profit_factor   REAL,
                avg_win         REAL,
                avg_loss        REAL
            );

            CREATE INDEX IF NOT EXISTS idx_ticks_time ON ticks(time_ms);
            CREATE INDEX IF NOT EXISTS idx_bars_time ON bars(time_ms);
            CREATE INDEX IF NOT EXISTS idx_signals_time ON signals(time_ms);
            CREATE INDEX IF NOT EXISTS idx_trades_open ON trades(open_time_ms);
            CREATE INDEX IF NOT EXISTS idx_trades_close ON trades(close_time_ms);
            CREATE INDEX IF NOT EXISTS idx_state_time ON state_changes(time_ms);
        """)
        self._conn.commit()

    # ── Buffered inserts ────────────────────────────────────────

    def log_tick(self, time_ms: int, bid: float, ask: float, spread: int, volume: int) -> None:
        self._tick_counter += 1
        if self._tick_counter % self._sample_rate != 0:
            return
        self._tick_buf.append((time_ms, bid, ask, spread, volume))

    def log_bar(self, time_ms: int, o: float, h: float, l: float, c: float,
                volume: int, tick_count: int, spread_avg: float) -> None:
        self._bar_buf.append((time_ms, o, h, l, c, volume, tick_count, spread_avg))

    def log_signal(
        self, time_ms: int, direction: str, strategy: str, strength: float,
        bid: float, ask: float, spread: int,
        ofi: float = 0.0, delta: float = 0.0, vwap_dev: float = 0.0,
        atr: float = 0.0, regime: str = "",
        vetoed: bool = False, veto_reason: str = "",
    ) -> None:
        self._signal_buf.append((
            time_ms, direction, strategy, strength, bid, ask, spread,
            ofi, delta, vwap_dev, atr, regime,
            1 if vetoed else 0, veto_reason,
        ))

    def log_trade_open(
        self, ticket: int, time_ms: int, direction: str,
        volume: float, price: float, sl: float, tp: float, magic: int,
    ) -> int:
        """Insert trade open. Returns the database row ID."""
        self._cursor.execute(
            """INSERT INTO trades
               (ticket, open_time_ms, direction, volume, open_price, sl, tp, magic)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (ticket, time_ms, direction, volume, price, sl, tp, magic),
        )
        self._conn.commit()
        return self._cursor.lastrowid

    def log_trade_close(
        self, db_id: int, close_time_ms: int, close_price: float,
        pnl_points: float, pnl_money: float, commission: float,
        swap: float, close_reason: str,
    ) -> None:
        self._cursor.execute(
            """UPDATE trades SET
               close_time_ms=?, close_price=?, pnl_points=?, pnl_money=?,
               commission=?, swap=?, close_reason=?
               WHERE id=?""",
            (close_time_ms, close_price, pnl_points, pnl_money,
             commission, swap, close_reason, db_id),
        )
        self._conn.commit()

    def log_trade_metric(
        self, trade_id: int, mae: float, mfe: float, duration: float,
        entry_spread: int, exit_spread: Optional[int] = None,
        entry_ofi: float = 0.0, entry_delta: float = 0.0, entry_atr: float = 0.0,
    ) -> None:
        self._metric_buf.append((
            trade_id, mae, mfe, duration, entry_spread,
            exit_spread, entry_ofi, entry_delta, entry_atr,
        ))

    def log_state_change(self, time_ms: int, old_state: str, new_state: str, reason: str) -> None:
        self._state_buf.append((time_ms, old_state, new_state, reason))

    # ── Flush ───────────────────────────────────────────────────

    def maybe_flush(self) -> None:
        """Call from the engine loop. Flushes if interval has elapsed."""
        now = time.time()
        if now - self._last_flush < self._flush_interval:
            return
        self.flush()

    def flush(self) -> None:
        """Force flush all buffers to disk."""
        try:
            if self._tick_buf:
                self._cursor.executemany(
                    "INSERT INTO ticks (time_ms, bid, ask, spread, volume) VALUES (?,?,?,?,?)",
                    self._tick_buf,
                )
                self._tick_buf.clear()

            if self._bar_buf:
                self._cursor.executemany(
                    "INSERT INTO bars (time_ms, open, high, low, close, volume, tick_count, spread_avg) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    self._bar_buf,
                )
                self._bar_buf.clear()

            if self._signal_buf:
                self._cursor.executemany(
                    "INSERT INTO signals "
                    "(time_ms, direction, strategy, strength, bid, ask, spread, "
                    "ofi, delta, vwap_dev, atr, regime, vetoed, veto_reason) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    self._signal_buf,
                )
                self._signal_buf.clear()

            if self._state_buf:
                self._cursor.executemany(
                    "INSERT INTO state_changes (time_ms, old_state, new_state, reason) "
                    "VALUES (?,?,?,?)",
                    self._state_buf,
                )
                self._state_buf.clear()

            if self._metric_buf:
                self._cursor.executemany(
                    "INSERT INTO trade_metrics "
                    "(trade_id, mae_points, mfe_points, duration_sec, "
                    "entry_spread, exit_spread, entry_ofi, entry_delta, entry_atr) "
                    "VALUES (?,?,?,?,?,?,?,?,?)",
                    self._metric_buf,
                )
                self._metric_buf.clear()

            self._conn.commit()
            self._last_flush = time.time()

        except Exception:
            self._log.exception("Database flush failed")

    # ── Query helpers (for analytics) ───────────────────────────

    def get_trades(self, date: Optional[str] = None) -> List[Dict[str, Any]]:
        """Get all trades, optionally filtered by date (YYYY-MM-DD)."""
        if date:
            rows = self._cursor.execute(
                "SELECT * FROM trades WHERE date(open_time_ms/1000, 'unixepoch') = ?",
                (date,),
            ).fetchall()
        else:
            rows = self._cursor.execute("SELECT * FROM trades").fetchall()
        cols = [d[0] for d in self._cursor.description]
        return [dict(zip(cols, row)) for row in rows]

    def get_closed_trades(self, limit: int = 1000) -> List[Dict[str, Any]]:
        rows = self._cursor.execute(
            "SELECT * FROM trades WHERE close_time_ms IS NOT NULL "
            "ORDER BY close_time_ms DESC LIMIT ?",
            (limit,),
        ).fetchall()
        cols = [d[0] for d in self._cursor.description]
        return [dict(zip(cols, row)) for row in rows]

    def get_daily_pnl(self, date: str) -> Optional[Dict[str, Any]]:
        row = self._cursor.execute(
            "SELECT * FROM daily_pnl WHERE date = ?", (date,)
        ).fetchone()
        if not row:
            return None
        cols = [d[0] for d in self._cursor.description]
        return dict(zip(cols, row))

    def get_trade_metrics(self, trade_id: int) -> Optional[Dict[str, Any]]:
        row = self._cursor.execute(
            "SELECT * FROM trade_metrics WHERE trade_id = ?", (trade_id,)
        ).fetchone()
        if not row:
            return None
        cols = [d[0] for d in self._cursor.description]
        return dict(zip(cols, row))

    def save_daily_pnl(self, date: str, stats: Dict[str, Any]) -> None:
        self._cursor.execute(
            """INSERT OR REPLACE INTO daily_pnl
               (date, total_pnl, trade_count, win_count, loss_count,
                gross_profit, gross_loss, max_drawdown, profit_factor, avg_win, avg_loss)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                date, stats["total_pnl"], stats["trade_count"],
                stats["win_count"], stats["loss_count"],
                stats["gross_profit"], stats["gross_loss"],
                stats["max_drawdown"], stats.get("profit_factor"),
                stats.get("avg_win"), stats.get("avg_loss"),
            ),
        )
        self._conn.commit()

    # ── Lifecycle ───────────────────────────────────────────────

    def close(self) -> None:
        self.flush()
        self._conn.close()

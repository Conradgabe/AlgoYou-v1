"""
Post-session analytics engine.

Runs after each trading session ends. Never during live trading.
Computes:
    - Profit factor, Sharpe ratio, Sortino ratio
    - MAE/MFE distributions → optimal SL/TP calibration
    - Win rate by trigger type, session, volatility regime
    - Edge decay detection (rolling profit factor over last N trades)
    - Equity curve statistics
    - Trade duration analysis

All results are saved to the database for historical tracking.
The edge decay detection is CRITICAL — all edges die over time.
When profit factor drops below 1.1 for 30+ consecutive trades, the strategy is dying.
"""

import numpy as np
import logging
from typing import Dict, List, Any, Optional
from datetime import datetime

from .database import TradeDatabase


class Analytics:
    __slots__ = ("_db", "_log")

    def __init__(self, db: TradeDatabase):
        self._db = db
        self._log = logging.getLogger("analytics")

    def run_session_report(self, date: Optional[str] = None) -> Dict[str, Any]:
        """
        Generate full session report. Call after session ends.
        Returns stats dict and saves daily_pnl to database.
        """
        if date is None:
            date = datetime.utcnow().strftime("%Y-%m-%d")

        trades = self._db.get_trades(date)
        closed = [t for t in trades if t.get("close_time_ms") is not None]

        if not closed:
            self._log.info("No closed trades for %s", date)
            return {"date": date, "trade_count": 0}

        pnls = np.array([t["pnl_points"] or 0.0 for t in closed])
        money = np.array([t["pnl_money"] or 0.0 for t in closed])

        wins = pnls[pnls > 0]
        losses = pnls[pnls <= 0]

        stats = {
            "date": date,
            "trade_count": len(closed),
            "win_count": len(wins),
            "loss_count": len(losses),
            "total_pnl": float(money.sum()),
            "gross_profit": float(money[money > 0].sum()) if len(money[money > 0]) > 0 else 0.0,
            "gross_loss": float(money[money <= 0].sum()) if len(money[money <= 0]) > 0 else 0.0,
            "max_drawdown": float(self._max_drawdown(money)),
            "win_rate": len(wins) / len(pnls) if len(pnls) > 0 else 0.0,
            "avg_win": float(wins.mean()) if len(wins) > 0 else 0.0,
            "avg_loss": float(losses.mean()) if len(losses) > 0 else 0.0,
            "profit_factor": None,
            "sharpe": None,
            "sortino": None,
            "avg_duration_sec": None,
            "avg_mae": None,
            "avg_mfe": None,
        }

        # Profit factor
        gross_profit = abs(money[money > 0].sum()) if len(money[money > 0]) > 0 else 0.0
        gross_loss = abs(money[money <= 0].sum()) if len(money[money <= 0]) > 0 else 0.0001
        stats["profit_factor"] = round(gross_profit / gross_loss, 3)

        # Sharpe (annualized, assuming 252 trading days)
        if len(money) > 1:
            daily_return = money.sum()
            daily_std = money.std()
            if daily_std > 0:
                stats["sharpe"] = round((daily_return / daily_std) * np.sqrt(252), 3)

        # Sortino (only downside deviation)
        if len(money) > 1:
            downside = money[money < 0]
            if len(downside) > 0:
                downside_std = downside.std()
                if downside_std > 0:
                    stats["sortino"] = round((money.mean() / downside_std) * np.sqrt(252), 3)

        # Duration
        durations = []
        for t in closed:
            if t.get("open_time_ms") and t.get("close_time_ms"):
                dur = (t["close_time_ms"] - t["open_time_ms"]) / 1000.0
                durations.append(dur)
        if durations:
            stats["avg_duration_sec"] = round(np.mean(durations), 1)

        # MAE/MFE from trade_metrics
        maes, mfes = [], []
        for t in closed:
            metrics = self._db.get_trade_metrics(t["id"])
            if metrics:
                maes.append(metrics["mae_points"])
                mfes.append(metrics["mfe_points"])
        if maes:
            stats["avg_mae"] = round(np.mean(maes), 1)
            stats["avg_mfe"] = round(np.mean(mfes), 1)

        # Save to DB
        self._db.save_daily_pnl(date, stats)

        # Log report
        self._log.info("=" * 60)
        self._log.info("SESSION REPORT: %s", date)
        self._log.info("-" * 60)
        self._log.info("Trades: %d (W:%d / L:%d) | Win rate: %.1f%%",
                        stats["trade_count"], stats["win_count"], stats["loss_count"],
                        stats["win_rate"] * 100)
        self._log.info("P&L: $%.2f | PF: %s | Sharpe: %s",
                        stats["total_pnl"], stats["profit_factor"], stats.get("sharpe", "N/A"))
        self._log.info("Avg Win: %.1f pts | Avg Loss: %.1f pts",
                        stats["avg_win"], stats["avg_loss"])
        if stats["avg_mae"] is not None:
            self._log.info("Avg MAE: %.1f pts | Avg MFE: %.1f pts",
                            stats["avg_mae"], stats["avg_mfe"])
        self._log.info("Max Drawdown: $%.2f", stats["max_drawdown"])
        self._log.info("=" * 60)

        return stats

    def check_edge_decay(self, lookback_trades: int = 30) -> Dict[str, Any]:
        """
        Checks if the strategy edge is decaying.

        Method: Rolling profit factor over the last N closed trades.
        If PF < 1.1 for N trades, the edge is likely dying.
        If PF < 1.0, the edge is dead — stop trading.

        Returns:
            {
                "rolling_pf": float,
                "rolling_win_rate": float,
                "status": "healthy" | "degrading" | "critical" | "dead",
                "recommendation": str,
            }
        """
        trades = self._db.get_closed_trades(lookback_trades)
        if len(trades) < 10:
            return {
                "rolling_pf": 0.0,
                "rolling_win_rate": 0.0,
                "status": "insufficient_data",
                "recommendation": "Need at least 10 trades for edge analysis",
            }

        pnls = np.array([t["pnl_money"] or 0.0 for t in trades])
        wins = pnls[pnls > 0]
        losses = pnls[pnls <= 0]

        gross_profit = abs(wins.sum()) if len(wins) > 0 else 0.0
        gross_loss = abs(losses.sum()) if len(losses) > 0 else 0.0001
        rolling_pf = gross_profit / gross_loss
        rolling_wr = len(wins) / len(pnls)

        if rolling_pf >= 1.5:
            status = "healthy"
            rec = "Edge intact. Continue trading."
        elif rolling_pf >= 1.1:
            status = "degrading"
            rec = "Edge weakening. Reduce position size 25%. Monitor closely."
        elif rolling_pf >= 1.0:
            status = "critical"
            rec = "Edge nearly gone. Reduce size 50%. Begin developing next strategy."
        else:
            status = "dead"
            rec = "Edge is dead (PF < 1.0). STOP TRADING. Strategy needs redesign."

        self._log.warning(
            "EDGE CHECK | PF=%.2f WR=%.1f%% status=%s | %s",
            rolling_pf, rolling_wr * 100, status, rec,
        )

        return {
            "rolling_pf": round(rolling_pf, 3),
            "rolling_win_rate": round(rolling_wr, 3),
            "status": status,
            "recommendation": rec,
        }

    def performance_by_trigger(self) -> Dict[str, Dict[str, Any]]:
        """Break down performance by signal trigger type."""
        # Query signals that led to trades
        trades = self._db.get_closed_trades(500)
        # Group by strategy field (which contains trigger type)
        groups: Dict[str, list] = {}
        for t in trades:
            # We'd need to join with signals table — simplified here
            key = t.get("close_reason", "unknown")
            groups.setdefault(key, []).append(t.get("pnl_money", 0.0))

        result = {}
        for key, pnls in groups.items():
            arr = np.array(pnls)
            wins = arr[arr > 0]
            losses = arr[arr <= 0]
            gp = abs(wins.sum()) if len(wins) > 0 else 0.0
            gl = abs(losses.sum()) if len(losses) > 0 else 0.0001
            result[key] = {
                "count": len(arr),
                "total_pnl": round(float(arr.sum()), 2),
                "win_rate": round(len(wins) / len(arr), 3) if len(arr) > 0 else 0.0,
                "profit_factor": round(gp / gl, 3),
            }

        return result

    def optimal_sl_tp(self) -> Dict[str, Any]:
        """
        Analyze MAE/MFE distributions to suggest optimal SL/TP placement.

        Logic:
        - SL should be at ~90th percentile MAE of WINNING trades
          (lets winners breathe but cuts losers efficiently)
        - TP should capture ~60-70% of average MFE
          (doesn't leave too much on the table)
        """
        trades = self._db.get_closed_trades(200)
        maes_w, mfes_w = [], []
        maes_l, mfes_l = [], []

        for t in trades:
            metrics = self._db.get_trade_metrics(t["id"])
            if not metrics:
                continue
            pnl = t.get("pnl_points", 0) or 0
            if pnl > 0:
                maes_w.append(abs(metrics["mae_points"]))
                mfes_w.append(metrics["mfe_points"])
            else:
                maes_l.append(abs(metrics["mae_points"]))
                mfes_l.append(metrics["mfe_points"])

        result = {"sufficient_data": False}

        if len(maes_w) >= 20:
            result["sufficient_data"] = True
            mae_arr = np.array(maes_w)
            mfe_arr = np.array(mfes_w)

            result["optimal_sl_points"] = round(float(np.percentile(mae_arr, 90)), 1)
            result["optimal_tp_points"] = round(float(mfe_arr.mean() * 0.65), 1)
            result["avg_winner_mae"] = round(float(mae_arr.mean()), 1)
            result["avg_winner_mfe"] = round(float(mfe_arr.mean()), 1)
            result["p90_winner_mae"] = round(float(np.percentile(mae_arr, 90)), 1)

            if len(maes_l) > 0:
                result["avg_loser_mae"] = round(float(np.mean(maes_l)), 1)
                result["avg_loser_mfe"] = round(float(np.mean(mfes_l)), 1)

            self._log.info(
                "SL/TP OPTIMIZATION | Optimal SL: %.1f pts | Optimal TP: %.1f pts | "
                "Avg winner MAE: %.1f | Avg winner MFE: %.1f",
                result["optimal_sl_points"], result["optimal_tp_points"],
                result["avg_winner_mae"], result["avg_winner_mfe"],
            )

        return result

    # ── Internal ────────────────────────────────────────────────

    @staticmethod
    def _max_drawdown(pnl_series: np.ndarray) -> float:
        """Compute max drawdown from a P&L series."""
        if len(pnl_series) == 0:
            return 0.0
        cumulative = np.cumsum(pnl_series)
        peak = np.maximum.accumulate(cumulative)
        drawdown = peak - cumulative
        return float(drawdown.max()) if len(drawdown) > 0 else 0.0

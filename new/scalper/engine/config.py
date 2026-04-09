"""
Configuration management for the scalper engine.
Loads all parameters from settings.json into typed dataclasses.
No magic numbers anywhere in the codebase — everything flows from here.
"""

import json
import os
from dataclasses import dataclass


@dataclass(frozen=True)
class SocketConfig:
    host: str
    port: int
    heartbeat_interval_ms: int
    heartbeat_timeout_ms: int
    reconnect_base_delay_ms: int
    reconnect_max_delay_ms: int
    recv_buffer_bytes: int


@dataclass(frozen=True)
class SessionConfig:
    london_open: str
    london_close: str
    ny_open: str
    ny_close: str
    timezone: str
    early_close_minutes: int
    no_trade_before_news_minutes: int
    no_trade_after_news_minutes: int


@dataclass(frozen=True)
class RiskConfig:
    max_risk_per_trade_pct: float
    max_daily_loss_pct: float
    max_consecutive_losses: int
    consecutive_loss_size_reduction: float
    kelly_fraction: float
    max_positions: int
    max_spread_points: int
    cooldown_seconds: int
    equity_curve_ema_period: int
    min_trades_for_kelly: int
    default_win_rate: float
    default_payoff_ratio: float


@dataclass(frozen=True)
class FeatureConfig:
    ofi_window_ticks: int
    ofi_threshold: float
    delta_window_ticks: int
    delta_divergence_threshold: float
    vwap_period_minutes: int
    vwap_deviation_threshold: float
    atr_period: int
    atr_timeframe_minutes: int
    volatility_lookback_bars: int
    volatility_contraction_percentile: int
    absorption_volume_threshold: float
    absorption_price_tolerance_points: int
    absorption_min_touches: int
    sweep_levels_threshold: int
    sweep_time_window_ms: int
    tick_buffer_size: int
    bar_buffer_size: int


@dataclass(frozen=True)
class StrategyConfig:
    name: str
    ema_period: int
    ema_fast_period: int
    atr_multiplier_sl: float
    atr_multiplier_tp: float
    range_contraction_threshold: float
    volume_spike_threshold: float
    time_stop_minutes: int
    time_stop_min_profit_points: int
    min_rr_ratio: float
    scale_in_enabled: bool
    scale_in_initial_pct: float
    scale_in_confirm_points: int
    trail_activation_points: float
    trail_step_points: float
    breakeven_activation_points: float
    max_trades_per_session: int


@dataclass(frozen=True)
class PositionConfig:
    default_volume: float
    min_volume: float
    max_volume: float
    volume_step: float
    magic_number: int


@dataclass(frozen=True)
class LoggingConfig:
    level: str
    log_ticks: bool
    log_features: bool
    log_signals: bool
    log_orders: bool
    log_state: bool
    log_heartbeat: bool
    console_output: bool
    file_output: bool
    log_dir: str


@dataclass(frozen=True)
class DatabaseConfig:
    path: str
    flush_interval_seconds: int
    tick_sample_rate: int
    archive_after_days: int


@dataclass(frozen=True)
class ScalperConfig:
    symbol: str
    point_value: float
    tick_size: float
    lot_size_oz: int
    socket: SocketConfig
    session: SessionConfig
    risk: RiskConfig
    features: FeatureConfig
    strategy: StrategyConfig
    position: PositionConfig
    logging: LoggingConfig
    database: DatabaseConfig

    @classmethod
    def from_file(cls, path: str) -> "ScalperConfig":
        with open(path, "r") as f:
            d = json.load(f)

        feat = d["features"]
        # Convert delta_window_seconds to ticks estimate (approx 10 ticks/sec on gold)
        delta_window_ticks = feat.pop("delta_window_seconds", 15) * 10

        return cls(
            symbol=d["symbol"],
            point_value=d["point_value"],
            tick_size=d["tick_size"],
            lot_size_oz=d["lot_size_oz"],
            socket=SocketConfig(**d["socket"]),
            session=SessionConfig(**d["session"]),
            risk=RiskConfig(**d["risk"]),
            features=FeatureConfig(delta_window_ticks=delta_window_ticks, **feat),
            strategy=StrategyConfig(**d["strategy"]),
            position=PositionConfig(**d["position"]),
            logging=LoggingConfig(**d["logging"]),
            database=DatabaseConfig(**d["database"]),
        )

    @classmethod
    def from_default(cls) -> "ScalperConfig":
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        return cls.from_file(os.path.join(base, "config", "settings.json"))

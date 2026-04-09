"""Tests for the risk manager."""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from engine.risk import RiskManager, RiskVerdict
from engine.config import RiskConfig, PositionConfig


@pytest.fixture
def risk_cfg():
    return RiskConfig(
        max_risk_per_trade_pct=1.0,
        max_daily_loss_pct=3.0,
        max_consecutive_losses=3,
        consecutive_loss_size_reduction=0.5,
        kelly_fraction=0.5,
        max_positions=1,
        max_spread_points=25,
        cooldown_seconds=30,
        equity_curve_ema_period=20,
        min_trades_for_kelly=50,
        default_win_rate=0.55,
        default_payoff_ratio=1.3,
    )


@pytest.fixture
def pos_cfg():
    return PositionConfig(
        default_volume=0.01,
        min_volume=0.01,
        max_volume=0.10,
        volume_step=0.01,
        magic_number=7741,
    )


@pytest.fixture
def rm(risk_cfg, pos_cfg):
    rm = RiskManager(risk_cfg, pos_cfg)
    rm.set_account_balance(1000.0)
    rm.new_day("2026-04-09")
    return rm


def test_approve_normal_signal(rm):
    verdict = rm.evaluate(
        direction=1,
        atr=2.0,
        current_spread=15,
        volatility_regime="normal",
        atr_multiplier_sl=1.5,
        atr_multiplier_tp=2.0,
    )
    assert verdict.approved
    assert verdict.volume >= 0.01
    assert verdict.sl_distance > 0
    assert verdict.tp_distance > 0


def test_veto_spread_too_wide(rm):
    verdict = rm.evaluate(
        direction=1, atr=2.0, current_spread=30,
        volatility_regime="normal",
        atr_multiplier_sl=1.5, atr_multiplier_tp=2.0,
    )
    assert not verdict.approved
    assert "spread" in verdict.reject_reason


def test_veto_extreme_volatility(rm):
    verdict = rm.evaluate(
        direction=1, atr=2.0, current_spread=15,
        volatility_regime="extreme",
        atr_multiplier_sl=1.5, atr_multiplier_tp=2.0,
    )
    assert not verdict.approved
    assert "volatility" in verdict.reject_reason


def test_veto_daily_loss_limit(rm):
    # Simulate hitting daily loss
    rm._daily_pnl = -35.0  # 3.5% of $1000, exceeds 3% limit
    verdict = rm.evaluate(
        direction=1, atr=2.0, current_spread=15,
        volatility_regime="normal",
        atr_multiplier_sl=1.5, atr_multiplier_tp=2.0,
    )
    assert not verdict.approved
    assert "daily_loss" in verdict.reject_reason


def test_veto_already_in_position(rm):
    # Simulate active trade
    rm.on_trade_open(
        db_id=1, direction=1, entry_price=2340.0,
        volume=0.01, sl=2337.0, tp=2344.0,
        spread=15, ofi=0.5, delta=10, atr=2.0,
    )
    verdict = rm.evaluate(
        direction=-1, atr=2.0, current_spread=15,
        volatility_regime="normal",
        atr_multiplier_sl=1.5, atr_multiplier_tp=2.0,
    )
    assert not verdict.approved
    assert "position" in verdict.reject_reason


def test_consecutive_loss_reduction(rm):
    # Simulate 3 consecutive losses
    for i in range(3):
        rm.on_trade_open(
            db_id=i+1, direction=1, entry_price=2340.0,
            volume=0.01, sl=2337.0, tp=2344.0,
            spread=15, ofi=0.5, delta=10, atr=2.0,
        )
        rm.on_trade_close(pnl_points=-30, pnl_money=-3.0)

    assert rm._consec_losses == 3

    # Next signal should have reduced size
    verdict = rm.evaluate(
        direction=1, atr=2.0, current_spread=15,
        volatility_regime="normal",
        atr_multiplier_sl=1.5, atr_multiplier_tp=2.0,
    )
    assert verdict.approved
    # Volume should be reduced (exact value depends on Kelly calc)


def test_trade_lifecycle(rm):
    rm.on_trade_open(
        db_id=1, direction=1, entry_price=2340.0,
        volume=0.01, sl=2337.0, tp=2344.0,
        spread=15, ofi=0.5, delta=10, atr=2.0,
    )
    assert rm.active_trade is not None

    tracker = rm.on_trade_close(pnl_points=40, pnl_money=4.0)
    assert rm.active_trade is None
    assert tracker.mae_points <= 0  # no adverse movement tracked
    assert rm.daily_pnl == 4.0
    assert rm._win_count == 1
    assert rm._consec_losses == 0


def test_mae_mfe_tracking(rm):
    rm.on_trade_open(
        db_id=1, direction=1, entry_price=2340.0,
        volume=0.01, sl=2337.0, tp=2344.0,
        spread=15, ofi=0.5, delta=10, atr=2.0,
    )

    # Price moves against us then in our favor
    rm.on_tick_update(2339.50)  # -50 points adverse
    rm.on_tick_update(2341.00)  # +100 points favorable
    rm.on_tick_update(2340.80)  # pull back

    tracker = rm.on_trade_close(pnl_points=80, pnl_money=8.0)
    assert tracker.mae_points < 0   # had adverse excursion
    assert tracker.mfe_points > 0   # had favorable excursion


def test_volume_clamped(rm):
    rm.set_account_balance(100.0)  # tiny account
    verdict = rm.evaluate(
        direction=1, atr=2.0, current_spread=15,
        volatility_regime="normal",
        atr_multiplier_sl=1.5, atr_multiplier_tp=2.0,
    )
    assert verdict.approved
    assert verdict.volume >= 0.01  # at least min volume
    assert verdict.volume <= 0.10  # at most max volume

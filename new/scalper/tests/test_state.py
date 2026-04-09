"""Tests for the state machine."""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from engine.state import StateMachine, BotState


@pytest.fixture
def sm():
    return StateMachine(cooldown_seconds=1)


def test_initial_state(sm):
    assert sm.state == BotState.IDLE
    assert sm.is_tradeable
    assert not sm.has_position
    assert not sm.is_halted


def test_valid_transition(sm):
    assert sm.transition(BotState.SIGNAL, "test_signal")
    assert sm.state == BotState.SIGNAL
    assert not sm.is_tradeable


def test_invalid_transition(sm):
    # IDLE → ACTIVE is not valid
    assert not sm.transition(BotState.ACTIVE, "invalid")
    assert sm.state == BotState.IDLE


def test_full_trade_cycle(sm):
    assert sm.transition(BotState.SIGNAL, "signal_detected")
    assert sm.transition(BotState.PENDING, "order_sent")
    assert sm.transition(BotState.ACTIVE, "fill_confirmed")
    assert sm.has_position
    assert sm.transition(BotState.EXITING, "tp_hit")
    assert sm.transition(BotState.COOLDOWN, "trade_closed")
    assert sm.trade_count == 1
    assert sm.session_trade_count == 1


def test_halt_from_any_state(sm):
    sm.transition(BotState.SIGNAL, "test")
    assert sm.halt("daily_loss")
    assert sm.is_halted
    assert sm.halt_reason == "daily_loss"


def test_reset_from_halted(sm):
    sm.halt("test")
    assert sm.reset()
    assert sm.state == BotState.IDLE
    assert sm.halt_reason is None


def test_reset_only_from_halted(sm):
    assert not sm.reset()  # already IDLE, not HALTED


def test_cooldown_auto_transition(sm):
    sm.transition(BotState.SIGNAL, "s")
    sm.transition(BotState.PENDING, "p")
    sm.transition(BotState.ACTIVE, "a")
    sm.transition(BotState.EXITING, "e")
    sm.transition(BotState.COOLDOWN, "c")

    # Immediately after — still in cooldown
    assert sm._state == BotState.COOLDOWN

    # After setting cooldown start to the past
    import time
    sm._cooldown_start = time.time() - 2  # 2 seconds ago, cooldown is 1 sec
    assert sm.state == BotState.IDLE  # auto-transition


def test_trade_count_increments(sm):
    for i in range(3):
        sm.transition(BotState.SIGNAL, "s")
        sm.transition(BotState.PENDING, "p")
        sm.transition(BotState.ACTIVE, "a")
        sm.transition(BotState.EXITING, "e")
        sm.transition(BotState.COOLDOWN, "c")
        import time
        sm._cooldown_start = time.time() - 2
        _ = sm.state  # trigger auto-transition

    assert sm.trade_count == 3
    assert sm.session_trade_count == 3


def test_session_count_reset(sm):
    sm.transition(BotState.SIGNAL, "s")
    sm.transition(BotState.PENDING, "p")
    sm.transition(BotState.ACTIVE, "a")
    sm.transition(BotState.EXITING, "e")
    sm.transition(BotState.COOLDOWN, "c")
    assert sm.session_trade_count == 1
    sm.reset_session_count()
    assert sm.session_trade_count == 0
    assert sm.trade_count == 1  # total is not reset


def test_callback_fires(sm):
    transitions = []
    sm2 = StateMachine(
        cooldown_seconds=1,
        on_transition=lambda o, n, r, t: transitions.append((o, n, r)),
    )
    sm2.transition(BotState.SIGNAL, "test")
    assert len(transitions) == 1
    assert transitions[0] == (BotState.IDLE, BotState.SIGNAL, "test")

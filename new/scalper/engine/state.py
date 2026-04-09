"""
State machine for the scalping bot.

States:
    IDLE      — No position, evaluating conditions
    SIGNAL    — Conditions met, preparing order
    PENDING   — Order submitted, waiting for fill
    PARTIAL   — Partially filled, managing remainder
    ACTIVE    — Full position, managing trade
    EXITING   — Exit signal triggered, closing position
    COOLDOWN  — After exit, waiting N seconds before next signal
    HALTED    — Daily loss limit hit or error condition, no trading

Every state transition is logged with timestamp, price, and reason.
Invalid transitions are rejected and logged as warnings.
The HALTED state requires explicit manual reset — the bot never auto-recovers from halt.
"""

from enum import Enum, auto
from typing import Optional, Callable, Dict, Set
import time
import logging


class BotState(Enum):
    IDLE = auto()
    SIGNAL = auto()
    PENDING = auto()
    PARTIAL = auto()
    ACTIVE = auto()
    EXITING = auto()
    COOLDOWN = auto()
    HALTED = auto()


# Adjacency map — only these transitions are legal
_VALID: Dict[BotState, Set[BotState]] = {
    BotState.IDLE:     {BotState.SIGNAL, BotState.HALTED},
    BotState.SIGNAL:   {BotState.PENDING, BotState.IDLE, BotState.HALTED},
    BotState.PENDING:  {BotState.PARTIAL, BotState.ACTIVE, BotState.IDLE, BotState.HALTED},
    BotState.PARTIAL:  {BotState.ACTIVE, BotState.EXITING, BotState.HALTED},
    BotState.ACTIVE:   {BotState.EXITING, BotState.HALTED},
    BotState.EXITING:  {BotState.COOLDOWN, BotState.IDLE, BotState.HALTED},
    BotState.COOLDOWN: {BotState.IDLE, BotState.HALTED},
    BotState.HALTED:   {BotState.IDLE},
}


# Callback signature: (old_state, new_state, reason, timestamp_ms)
TransitionCallback = Callable[[BotState, BotState, str, int], None]


class StateMachine:
    """
    Thread-safe-ish state machine with cooldown auto-transition.
    Not using locks because the bot is single-threaded by design —
    the engine loop processes ticks sequentially.
    """

    __slots__ = (
        "_state", "_cooldown_sec", "_cooldown_start",
        "_on_transition", "_halt_reason", "_entry_time",
        "_trade_count", "_session_trade_count", "_log",
    )

    def __init__(
        self,
        cooldown_seconds: int,
        on_transition: Optional[TransitionCallback] = None,
    ):
        self._state: BotState = BotState.IDLE
        self._cooldown_sec: int = cooldown_seconds
        self._cooldown_start: Optional[float] = None
        self._on_transition: Optional[TransitionCallback] = on_transition
        self._halt_reason: Optional[str] = None
        self._entry_time: float = time.time()
        self._trade_count: int = 0
        self._session_trade_count: int = 0
        self._log = logging.getLogger("state")

    # ── Properties ──────────────────────────────────────────────

    @property
    def state(self) -> BotState:
        if self._state == BotState.COOLDOWN and self._cooldown_start is not None:
            elapsed = time.time() - self._cooldown_start
            if elapsed >= self._cooldown_sec:
                self._do_transition(BotState.IDLE, "cooldown_expired")
        return self._state

    @property
    def halt_reason(self) -> Optional[str]:
        return self._halt_reason

    @property
    def is_tradeable(self) -> bool:
        return self.state == BotState.IDLE

    @property
    def has_position(self) -> bool:
        return self.state in (BotState.ACTIVE, BotState.PARTIAL)

    @property
    def is_halted(self) -> bool:
        return self._state == BotState.HALTED

    @property
    def seconds_in_state(self) -> float:
        return time.time() - self._entry_time

    @property
    def trade_count(self) -> int:
        return self._trade_count

    @property
    def session_trade_count(self) -> int:
        return self._session_trade_count

    # ── Public transitions ──────────────────────────────────────

    def transition(self, new_state: BotState, reason: str) -> bool:
        return self._do_transition(new_state, reason)

    def halt(self, reason: str) -> bool:
        return self._do_transition(BotState.HALTED, reason)

    def reset(self) -> bool:
        """Manual reset from HALTED → IDLE. Only valid from HALTED."""
        if self._state != BotState.HALTED:
            self._log.warning("reset() called but state is %s, not HALTED", self._state.name)
            return False
        return self._do_transition(BotState.IDLE, "manual_reset")

    def reset_session_count(self) -> None:
        self._session_trade_count = 0

    # ── Internal ────────────────────────────────────────────────

    def _do_transition(self, new_state: BotState, reason: str) -> bool:
        old = self._state

        if new_state not in _VALID.get(old, set()):
            self._log.warning(
                "REJECTED %s → %s  reason=%s",
                old.name, new_state.name, reason,
            )
            return False

        self._state = new_state
        now = time.time()
        self._entry_time = now

        # Cooldown bookkeeping
        if new_state == BotState.COOLDOWN:
            self._cooldown_start = now

        # Halt bookkeeping
        if new_state == BotState.HALTED:
            self._halt_reason = reason
        elif new_state == BotState.IDLE and old == BotState.HALTED:
            self._halt_reason = None

        # Trade counting — a completed trade is EXITING → COOLDOWN
        if old == BotState.EXITING and new_state == BotState.COOLDOWN:
            self._trade_count += 1
            self._session_trade_count += 1

        self._log.info("STATE  %s → %s  | %s", old.name, new_state.name, reason)

        if self._on_transition:
            ts_ms = int(now * 1000)
            try:
                self._on_transition(old, new_state, reason, ts_ms)
            except Exception:
                self._log.exception("on_transition callback failed")

        return True

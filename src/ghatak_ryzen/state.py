"""In-memory state — survived only for daemon lifetime.

Persisted state lives in SQLite (`decisions`, `processes_1s`).
On daemon restart, state is rebuilt from `SELECT ... WHERE reverted=0`.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ActiveAction:
    decision_id: int
    module: str
    pid: int
    comm: str
    action: str
    metadata: dict = field(default_factory=dict)


@dataclass
class State:
    # pid -> action taken
    suspended: dict[int, ActiveAction] = field(default_factory=dict)
    pinned: dict[int, ActiveAction] = field(default_factory=dict)
    # pid -> consecutive ticks of zero-value CPU-burn
    idle_streaks: dict[int, int] = field(default_factory=dict)
    # cpu -> governor last set by daemon
    governor_overrides: dict[int, str] = field(default_factory=dict)
    # trading-lock currently active?
    trading_lock_active: bool = False
    night_quiet_active: bool = False

    def forget_pid(self, pid: int) -> None:
        self.suspended.pop(pid, None)
        self.pinned.pop(pid, None)
        self.idle_streaks.pop(pid, None)

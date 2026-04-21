"""Main daemon loop — 1Hz sense, 0.1Hz judge.

SIGTERM / SIGINT: graceful shutdown — does NOT revert state by default.
Use `ghatak-ryzen panic` to revert everything.
"""
from __future__ import annotations

import logging
import os
import signal
import sys
import time

from . import actuators, judges, sensors
from .config import load as load_config
from .state import State
from .storage import Storage
from .topology import detect as detect_topology


log = logging.getLogger("ghatak-ryzen")


class Daemon:
    def __init__(self, config: dict) -> None:
        self.cfg = config
        self.dry_run = bool(config["daemon"]["dry_run"])
        self.tick = float(config["daemon"]["tick_interval_sec"])
        self.judge_every = int(config["daemon"]["judge_every_n_ticks"])
        self.topo = detect_topology()
        self.storage = Storage(
            db_path=config["daemon"]["db_path"],
            log_path=config["daemon"]["log_path"],
            jsonl_path=config["daemon"].get("jsonl_path"),
        )
        self.state = State()
        self.tracker = sensors.DeltaTracker()
        self._stop = False
        self._tick_n = 0

    def _install_signals(self) -> None:
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, self._on_signal)

    def _on_signal(self, signum, _frame) -> None:
        log.info(f"signal {signum} received, shutting down")
        self._stop = True

    def run(self) -> int:
        self._install_signals()
        self._rebuild_state_from_db()
        log.info(
            f"ghatak-ryzen start — dry_run={self.dry_run} "
            f"ccds={len(self.topo.ccds)} pin=cpu{self.topo.daemon_pin}"
        )
        self.storage.record_decision(
            "daemon", os.getpid(), "ghatak-ryzen", "START",
            f"daemon up — dry_run={self.dry_run} ccds={len(self.topo.ccds)}",
            state={"topology": self.topo.ccds, "cfg_dry_run": self.dry_run},
        )
        next_tick = time.time()
        while not self._stop:
            now = time.time()
            try:
                self._one_tick(now)
            except Exception:
                log.exception("tick failed")
            # pace
            next_tick += self.tick
            sleep = next_tick - time.time()
            if sleep > 0:
                time.sleep(sleep)
            else:
                next_tick = time.time()
        self.storage.record_decision(
            "daemon", os.getpid(), "ghatak-ryzen", "STOP",
            "daemon stop — in-memory state preserved on disk; run `panic` to revert",
            state={},
        )
        self.storage.close()
        return 0

    def _one_tick(self, now: float) -> None:
        self._tick_n += 1
        samples = sensors.scan_all()
        deltas = self.tracker.update(samples, now)
        ts = int(now)
        if deltas:
            self.storage.write_samples(ts, deltas)
        # check state.suspended for dead pids
        for pid in list(self.state.suspended):
            if not actuators.pid_alive(pid):
                self.state.suspended.pop(pid, None)
        # prune dead pids from pinned + idle_streaks
        live = {d.pid for d in deltas}
        for pid in list(self.state.pinned):
            if pid not in live and not actuators.pid_alive(pid):
                self.state.pinned.pop(pid, None)
        for pid in list(self.state.idle_streaks):
            if pid not in live:
                self.state.idle_streaks.pop(pid, None)

        # fire judges every N ticks
        if self._tick_n % self.judge_every == 0:
            a1 = judges.bloat_judge(deltas, self.cfg, self.state, self.storage, self.dry_run)
            a2 = judges.ccd_judge(deltas, self.cfg, self.state, self.storage, self.dry_run, self.topo)
            a3 = judges.governor_judge(self.cfg, self.state, self.storage, self.dry_run, self.topo, now)
            if a1 + a2 + a3 > 0:
                log.info(f"t={self._tick_n}s bloat={a1} ccd={a2} gov={a3}")

    def _rebuild_state_from_db(self) -> None:
        # repopulate suspended/pinned from decisions WHERE reverted=0
        for row in self.storage.active_decisions():
            _, _ts, module, pid, comm, action, _reason, _state_json = row
            if pid <= 0:
                continue
            if not actuators.pid_alive(pid):
                continue
            if module == "bloat" and action.startswith("SUSPEND") and "DRY" not in action:
                from .state import ActiveAction
                self.state.suspended[pid] = ActiveAction(
                    decision_id=row[0], module=module, pid=pid, comm=comm, action=action
                )
            elif module == "ccd" and action.startswith("PIN_CCD") and "DRY" not in action:
                from .state import ActiveAction
                self.state.pinned[pid] = ActiveAction(
                    decision_id=row[0], module=module, pid=pid, comm=comm, action=action
                )


def run_once(dry_run_override: bool | None = None) -> int:
    """One-shot tick — scan, judge once, exit. Useful for `ghatak-ryzen dryrun`."""
    cfg = load_config()
    if dry_run_override is not None:
        cfg["daemon"]["dry_run"] = dry_run_override
        cfg["daemon"]["judge_every_n_ticks"] = 1  # judge on the one tick
    d = Daemon(cfg)
    # need a warm-up tick for deltas (cpu%, io/s need t-1 snapshot)
    warm_samples = sensors.scan_all()
    d.tracker.update(warm_samples, time.time())
    time.sleep(2)
    d._one_tick(time.time())
    d.storage.close()
    return 0


def main(config_path: str | None = None) -> int:
    cfg = load_config(config_path)
    logging.basicConfig(
        level=cfg["daemon"].get("log_level", "info").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    return Daemon(cfg).run()


if __name__ == "__main__":
    sys.exit(main())

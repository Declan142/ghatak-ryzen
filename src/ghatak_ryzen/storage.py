"""SQLite store + pretty decision log writer.

Two files:
  /dev/shm/ghatak-ryzen.db   — tmpfs, hot timeline + decisions
  ~/.local/share/ghatak-ryzen/decisions.log   — pretty human log

jsonl mirror for machine parsing (Prometheus / digest).
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Iterable


SCHEMA = [
    """CREATE TABLE IF NOT EXISTS processes_1s (
        ts INTEGER,
        pid INTEGER,
        comm TEXT,
        cmdline TEXT,
        cpu_pct REAL,
        rss_kb INTEGER,
        io_write_bps REAL,
        state TEXT,
        value_score REAL,
        PRIMARY KEY (ts, pid)
    )""",
    """CREATE TABLE IF NOT EXISTS decisions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts INTEGER,
        module TEXT,
        pid INTEGER,
        comm TEXT,
        action TEXT,
        reason TEXT,
        state_json TEXT,
        reverted INTEGER DEFAULT 0,
        revert_ts INTEGER,
        revert_reason TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS idx_dec_ts ON decisions(ts)",
    "CREATE INDEX IF NOT EXISTS idx_dec_pid ON decisions(pid)",
    "CREATE INDEX IF NOT EXISTS idx_dec_module ON decisions(module)",
    "CREATE INDEX IF NOT EXISTS idx_proc_ts ON processes_1s(ts)",
]


class Storage:
    def __init__(self, db_path: str, log_path: str, jsonl_path: str | None = None) -> None:
        self.db_path = db_path
        self.log_path = Path(log_path)
        self.jsonl_path = Path(jsonl_path) if jsonl_path else None
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        if self.jsonl_path:
            self.jsonl_path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(db_path, isolation_level=None)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        for stmt in SCHEMA:
            self.db.execute(stmt)

    def write_samples(self, ts: int, deltas: Iterable) -> None:
        rows = [
            (ts, d.pid, d.comm, d.cmdline[:200], d.cpu_pct, d.rss_kb,
             d.io_write_bps, d.state,
             d.value_score if d.value_score != float("inf") else 1e12)
            for d in deltas
        ]
        self.db.executemany(
            "INSERT OR REPLACE INTO processes_1s VALUES (?,?,?,?,?,?,?,?,?)", rows
        )

    def record_decision(self, module: str, pid: int, comm: str, action: str,
                         reason: str, state: dict[str, Any] | None = None) -> int:
        ts = int(time.time())
        state_json = json.dumps(state or {}, default=str)
        cur = self.db.cursor()
        cur.execute(
            "INSERT INTO decisions (ts, module, pid, comm, action, reason, state_json) "
            "VALUES (?,?,?,?,?,?,?)",
            (ts, module, pid, comm, action, reason, state_json),
        )
        row_id = cur.lastrowid
        self._append_pretty_log(ts, module, pid, comm, action, reason, row_id)
        self._append_jsonl(ts, module, pid, comm, action, reason, state, row_id)
        return row_id

    def _append_pretty_log(self, ts, module, pid, comm, action, reason, row_id) -> None:
        stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))
        line = (f"[{stamp}] {action} {comm} pid={pid} "
                f"module={module} id={row_id}\n    reason: {reason}\n")
        try:
            with self.log_path.open("a") as f:
                f.write(line)
        except OSError:
            pass

    def _append_jsonl(self, ts, module, pid, comm, action, reason, state, row_id) -> None:
        if not self.jsonl_path:
            return
        obj = {
            "ts": ts, "module": module, "pid": pid, "comm": comm,
            "action": action, "reason": reason, "id": row_id,
            "state": state or {},
        }
        try:
            with self.jsonl_path.open("a") as f:
                f.write(json.dumps(obj, default=str) + "\n")
        except OSError:
            pass

    def mark_reverted(self, decision_id: int, reason: str) -> None:
        ts = int(time.time())
        self.db.execute(
            "UPDATE decisions SET reverted=1, revert_ts=?, revert_reason=? WHERE id=?",
            (ts, reason, decision_id),
        )
        stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))
        try:
            with self.log_path.open("a") as f:
                f.write(f"[{stamp}] REVERTED id={decision_id} reason: {reason}\n")
        except OSError:
            pass

    def get_decision(self, decision_id: int) -> tuple | None:
        return self.db.execute(
            "SELECT id, ts, module, pid, comm, action, reason, state_json, reverted "
            "FROM decisions WHERE id=?", (decision_id,),
        ).fetchone()

    def active_decisions(self, module: str | None = None) -> list[tuple]:
        q = ("SELECT id, ts, module, pid, comm, action, reason, state_json "
             "FROM decisions WHERE reverted=0")
        args: tuple = ()
        if module:
            q += " AND module=?"
            args = (module,)
        q += " ORDER BY id DESC"
        return self.db.execute(q, args).fetchall()

    def recent_decisions(self, limit: int = 50) -> list[tuple]:
        return self.db.execute(
            "SELECT id, ts, module, pid, comm, action, reason, reverted "
            "FROM decisions ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()

    def close(self) -> None:
        self.db.close()

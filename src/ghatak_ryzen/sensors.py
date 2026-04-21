"""Per-process /proc scanner + delta tracking + value-score compute.

Phase-1 value score simplification (full formula in BLUEPRINT.md §4.1):

    value = log1p(io_bytes_per_min) / cpu_seconds_per_min

Per-minute window derived from delta samples — high I/O output per CPU-sec
scores high; pure CPU-burn with zero I/O scores zero. User-focus bonus
(xdotool) is Phase 2.
"""
from __future__ import annotations

import math
import os
import re
from dataclasses import dataclass
from pathlib import Path


_CLK_TCK = os.sysconf(os.sysconf_names["SC_CLK_TCK"])
_MY_PID = os.getpid()
_VmRSS_RE = re.compile(r"^VmRSS:\s+(\d+)", re.MULTILINE)
_Uid_RE = re.compile(r"^Uid:\s+(\d+)", re.MULTILINE)
_Cpus_RE = re.compile(r"^Cpus_allowed_list:\s+(\S+)", re.MULTILINE)
_read_RE = re.compile(r"^read_bytes:\s+(\d+)", re.MULTILINE)
_write_RE = re.compile(r"^write_bytes:\s+(\d+)", re.MULTILINE)


@dataclass
class ProcSample:
    pid: int
    comm: str
    cmdline: str
    cwd: str
    uid: int
    cpu_ticks: int
    rss_kb: int
    read_bytes: int
    write_bytes: int
    state: str
    cpus_allowed: str
    tty_nr: int


@dataclass
class DeltaSample:
    pid: int
    comm: str
    cmdline: str
    cwd: str
    uid: int
    cpu_pct: float                  # across system (sum over cores, 0..N*100)
    cpu_sec: float                  # cpu-seconds consumed in window
    rss_kb: int
    io_read_bps: float
    io_write_bps: float
    state: str
    cpus_allowed: str
    tty_nr: int
    value_score: float


def read_one(pid: int) -> ProcSample | None:
    base = Path(f"/proc/{pid}")
    try:
        stat = (base / "stat").read_bytes()
        rp = stat.rindex(b")")
        lp = stat.index(b"(")
        comm = stat[lp + 1:rp].decode(errors="replace")
        rest = stat[rp + 2:].split()
        state = rest[0].decode()
        tty_nr = int(rest[4])
        utime = int(rest[11])
        stime = int(rest[12])

        cl = (base / "cmdline").read_bytes().replace(b"\x00", b" ").strip().decode(errors="replace")

        status = (base / "status").read_text()
        rss_m = _VmRSS_RE.search(status)
        uid_m = _Uid_RE.search(status)
        cpus_m = _Cpus_RE.search(status)
        rss = int(rss_m.group(1)) if rss_m else 0
        uid = int(uid_m.group(1)) if uid_m else -1
        cpus_allowed = cpus_m.group(1) if cpus_m else ""

        read_bytes = write_bytes = 0
        try:
            io = (base / "io").read_text()
            rm = _read_RE.search(io)
            wm = _write_RE.search(io)
            read_bytes = int(rm.group(1)) if rm else 0
            write_bytes = int(wm.group(1)) if wm else 0
        except (OSError, PermissionError):
            pass

        try:
            cwd = os.readlink(base / "cwd")
        except OSError:
            cwd = ""

        return ProcSample(
            pid=pid, comm=comm, cmdline=cl, cwd=cwd, uid=uid,
            cpu_ticks=utime + stime, rss_kb=rss,
            read_bytes=read_bytes, write_bytes=write_bytes,
            state=state, cpus_allowed=cpus_allowed, tty_nr=tty_nr,
        )
    except (FileNotFoundError, ProcessLookupError, OSError):
        return None


def scan_all() -> list[ProcSample]:
    samples: list[ProcSample] = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        if pid == _MY_PID:
            continue
        s = read_one(pid)
        if s is not None:
            samples.append(s)
    return samples


def compute_value_score(cpu_sec: float, io_bytes_total: float) -> float:
    """Phase-1 lite score: log1p(io) / cpu_sec. Zero I/O → 0. No CPU → +inf (idle)."""
    if cpu_sec < 0.01:
        return float("inf")
    return math.log1p(io_bytes_total) / cpu_sec


class DeltaTracker:
    """Maintains prev snapshots, yields DeltaSample each tick."""

    def __init__(self) -> None:
        self.prev: dict[int, tuple[float, ProcSample]] = {}

    def update(self, samples: list[ProcSample], now: float) -> list[DeltaSample]:
        deltas: list[DeltaSample] = []
        for s in samples:
            entry = self.prev.get(s.pid)
            if entry is not None:
                prev_ts, prev = entry
                dt = now - prev_ts
                if dt > 0:
                    d_ticks = max(0, s.cpu_ticks - prev.cpu_ticks)
                    cpu_sec = d_ticks / _CLK_TCK
                    cpu_pct = 100.0 * cpu_sec / dt
                    r_bps = max(0, s.read_bytes - prev.read_bytes) / dt
                    w_bps = max(0, s.write_bytes - prev.write_bytes) / dt
                    io_total = (r_bps + w_bps) * dt
                    vs = compute_value_score(cpu_sec, io_total)
                    deltas.append(DeltaSample(
                        pid=s.pid, comm=s.comm, cmdline=s.cmdline, cwd=s.cwd, uid=s.uid,
                        cpu_pct=cpu_pct, cpu_sec=cpu_sec, rss_kb=s.rss_kb,
                        io_read_bps=r_bps, io_write_bps=w_bps,
                        state=s.state, cpus_allowed=s.cpus_allowed, tty_nr=s.tty_nr,
                        value_score=vs,
                    ))
            self.prev[s.pid] = (now, s)
        # prune dead pids
        live = {s.pid for s in samples}
        for pid in list(self.prev):
            if pid not in live:
                del self.prev[pid]
        return deltas

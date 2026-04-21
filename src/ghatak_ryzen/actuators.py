"""Thin wrappers around OS-level actuators.

All write-side tools: taskset, renice, ionice, SIGSTOP/SIGCONT, cpufreq-set.
No actuator fires if caller passes dry_run=True — enforced in judges.py.
"""
from __future__ import annotations

import os
import signal
import subprocess
from pathlib import Path


def _run(cmd: list[str], check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, check=check, text=True,
                          capture_output=True, shell=False)


def taskset_pin(pid: int, cpu_list: str) -> None:
    """Pin PID to given CPU list (e.g. '0-3,8-11' or '4,5,6,7')."""
    _run(["taskset", "-pc", cpu_list, str(pid)], check=True)


def taskset_current_mask(pid: int) -> str:
    r = _run(["taskset", "-pc", str(pid)], check=False)
    # Output: "pid NNN's current affinity list: 0-15"
    if r.returncode != 0:
        return ""
    line = r.stdout.strip()
    if ":" in line:
        return line.rsplit(":", 1)[-1].strip()
    return ""


def renice_proc(pid: int, nice: int) -> bool:
    r = _run(["renice", str(nice), "-p", str(pid)], check=False)
    return r.returncode == 0


def ionice_proc(pid: int, cls: int) -> bool:
    """class: 1=realtime, 2=best-effort, 3=idle"""
    r = _run(["ionice", "-c", str(cls), "-p", str(pid)], check=False)
    return r.returncode == 0


def signal_stop(pid: int) -> None:
    os.kill(pid, signal.SIGSTOP)


def signal_cont(pid: int) -> None:
    os.kill(pid, signal.SIGCONT)


def signal_term(pid: int) -> None:
    os.kill(pid, signal.SIGTERM)


def signal_kill(pid: int) -> None:
    os.kill(pid, signal.SIGKILL)


def get_governor(cpu: int = 0) -> str:
    p = Path(f"/sys/devices/system/cpu/cpu{cpu}/cpufreq/scaling_governor")
    return p.read_text().strip() if p.exists() else ""


def get_cur_freq_khz(cpu: int = 0) -> int:
    p = Path(f"/sys/devices/system/cpu/cpu{cpu}/cpufreq/scaling_cur_freq")
    return int(p.read_text().strip()) if p.exists() else 0


def set_governor(cpu: int, governor: str) -> bool:
    """Write scaling_governor. Needs root — returns False if denied."""
    p = Path(f"/sys/devices/system/cpu/cpu{cpu}/cpufreq/scaling_governor")
    try:
        p.write_text(governor)
        return True
    except (PermissionError, OSError):
        return False


def set_freq_bounds(cpu: int, min_khz: int | None = None,
                     max_khz: int | None = None) -> bool:
    base = Path(f"/sys/devices/system/cpu/cpu{cpu}/cpufreq")
    ok = True
    try:
        if min_khz is not None:
            (base / "scaling_min_freq").write_text(str(min_khz))
        if max_khz is not None:
            (base / "scaling_max_freq").write_text(str(max_khz))
    except (PermissionError, OSError):
        ok = False
    return ok


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False

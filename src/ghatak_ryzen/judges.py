"""The three Phase-1 judgment modules.

bloat_judge   — track idle-streaks, SUSPEND high-CPU zero-value processes
ccd_judge     — enforce static CCD pinning for known workload comms
governor_judge — trading-lock + night-quiet (Phase 1: decision logged, actuation gated)

All three are pure functions over (deltas, config, state, storage, topology).
Dry-run is respected at the actuator call sites.
"""
from __future__ import annotations

import datetime as _dt
import re
from zoneinfo import ZoneInfo

from . import actuators
from .sensors import DeltaSample
from .state import ActiveAction, State
from .storage import Storage
from .topology import Topology


SAFE_ALWAYS_EXEMPT_COMMS = frozenset({
    "systemd", "init", "kthreadd", "kworker", "ksoftirqd", "rcu_sched",
    "migration", "watchdog", "sshd", "Xorg", "gnome-shell",
    "dbus-daemon", "NetworkManager", "systemd-logind", "systemd-journal",
})


def _is_kthread(d: DeltaSample) -> bool:
    return d.cmdline == "" and d.uid == 0


def _matches_any(needle: str, patterns: list[str]) -> bool:
    for p in patterns:
        if p and p in needle:
            return True
    return False


def _bloat_exempt(d: DeltaSample, cfg: dict) -> tuple[bool, str]:
    if d.pid <= 1:
        return True, "pid<=1"
    if _is_kthread(d):
        return True, "kthread"
    if d.uid == 0:
        # Phase-1 MVP never touches root-owned processes
        return True, "root-owned"
    if d.comm in SAFE_ALWAYS_EXEMPT_COMMS:
        return True, f"hard-exempt comm={d.comm}"
    if d.comm in cfg.get("whitelist_exact", []):
        return True, f"whitelisted comm={d.comm}"
    needle = f"{d.comm} {d.cmdline} {d.cwd}"
    if _matches_any(needle, cfg.get("whitelist_path_patterns", [])):
        return True, "whitelist path/pattern"
    if d.state in ("Z", "T"):
        # Z=zombie (reaper will clean), T=already stopped
        return True, f"state={d.state}"
    return False, ""


def bloat_judge(deltas: list[DeltaSample], cfg_all: dict, state: State,
                storage: Storage, dry_run: bool) -> int:
    cfg = cfg_all["bloat_judge"]
    if not cfg.get("enabled", True):
        return 0

    zero_thresh = float(cfg["value_score_zero_threshold"])
    min_cpu = float(cfg["suspend_cpu_pct_min"])
    suspend_after_s = int(cfg["suspend_after_idle_min"]) * 60
    actions = 0

    for d in deltas:
        if d.pid in state.suspended:
            continue
        exempt, reason = _bloat_exempt(d, cfg)
        if exempt:
            state.idle_streaks.pop(d.pid, None)
            continue

        # score == inf means idle (cpu_sec < 0.01) — not a bloat candidate
        if d.value_score == float("inf"):
            state.idle_streaks.pop(d.pid, None)
            continue

        low_value = d.value_score <= zero_thresh
        hogging = d.cpu_pct >= min_cpu

        if low_value and hogging:
            state.idle_streaks[d.pid] = state.idle_streaks.get(d.pid, 0) + 1
        else:
            state.idle_streaks.pop(d.pid, None)
            continue

        streak = state.idle_streaks[d.pid]
        if streak < suspend_after_s:
            continue

        # threshold crossed — SUSPEND
        reason_txt = (
            f"CPU {d.cpu_pct:.0f}% sustained {streak // 60}m, value_score {d.value_score:.3f} "
            f"(no output), rss={d.rss_kb // 1024}MB, cmdline={d.cmdline[:80]!r}"
        )
        action = "SUSPEND_DRY" if dry_run else "SUSPEND"
        decision_id = storage.record_decision(
            "bloat", d.pid, d.comm, action, reason_txt,
            state={"cpu_pct": d.cpu_pct, "rss_kb": d.rss_kb,
                   "streak_sec": streak, "cmdline": d.cmdline[:200]},
        )
        actions += 1
        # Dedup future ticks — even in dry-run. Fresh daemon = fresh log.
        state.suspended[d.pid] = ActiveAction(
            decision_id=decision_id, module="bloat", pid=d.pid,
            comm=d.comm, action=action,
            metadata={"dry_run": dry_run},
        )
        state.idle_streaks.pop(d.pid, None)
        if dry_run:
            continue
        if not actuators.pid_alive(d.pid):
            state.suspended.pop(d.pid, None)
            continue
        try:
            actuators.signal_stop(d.pid)
        except (ProcessLookupError, PermissionError) as e:
            state.suspended.pop(d.pid, None)
            storage.record_decision(
                "bloat", d.pid, d.comm, "SUSPEND_FAIL", f"{type(e).__name__}: {e}",
            )
    return actions


def _cpus_covered(current_mask: str, required_cpus: list[int]) -> bool:
    """True if every cpu in required_cpus is allowed by current_mask."""
    if not current_mask:
        return False
    allowed: set[int] = set()
    for part in current_mask.split(","):
        part = part.strip()
        if "-" in part:
            lo, hi = part.split("-")
            allowed.update(range(int(lo), int(hi) + 1))
        elif part.isdigit():
            allowed.add(int(part))
    return set(required_cpus).issubset(allowed) and allowed == set(required_cpus)


def _pattern_matches(pattern: str, comm: str, cmdline: str) -> bool:
    """Strict pattern matching — avoids shell sessions whose cmdline
    merely contains the pattern as a path fragment.

    Match if any of:
      - comm exactly equals pattern (`comm=ffmpeg`)
      - cmdline's first token (argv[0]) basename equals pattern
      - cmdline's first token endswith `/<pattern>` (absolute-path exec)
      - pattern endswith `.py` / `.sh` and appears as a cmdline token
    """
    if comm == pattern:
        return True
    if not cmdline:
        return False
    argv0 = cmdline.split(None, 1)[0] if cmdline else ""
    if argv0.rsplit("/", 1)[-1] == pattern:
        return True
    if argv0.endswith("/" + pattern):
        return True
    if pattern.endswith((".py", ".sh", ".js", ".ts")):
        toks = cmdline.split()
        for t in toks:
            if t == pattern or t.endswith("/" + pattern):
                return True
    return False


def ccd_judge(deltas: list[DeltaSample], cfg_all: dict, state: State,
              storage: Storage, dry_run: bool, topo: Topology) -> int:
    cfg = cfg_all["ccd_conductor"]
    if not cfg.get("enabled", True):
        return 0
    pin_patterns = cfg.get("pin_patterns", {})
    actions = 0

    for d in deltas:
        if d.pid in state.pinned:
            continue
        if d.pid in state.suspended:
            continue
        target_ccd: int | None = None
        for pattern, spec in pin_patterns.items():
            if _pattern_matches(pattern, d.comm, d.cmdline):
                target_ccd = int(spec["ccd"])
                break
        if target_ccd is None:
            continue
        if target_ccd >= len(topo.ccds):
            continue
        cpus = topo.cpus_for_ccd(target_ccd)
        cpu_list_str = topo.cpu_list_str(target_ccd)

        if _cpus_covered(d.cpus_allowed, cpus):
            # already pinned correctly — record in state so we skip next tick
            state.pinned[d.pid] = ActiveAction(
                decision_id=-1, module="ccd", pid=d.pid, comm=d.comm,
                action="ALREADY_PINNED", metadata={"prev_cpus": d.cpus_allowed},
            )
            continue

        reason = (f"pin to CCD{target_ccd} cpus={cpu_list_str} "
                  f"(was cpus_allowed={d.cpus_allowed})")
        action = "PIN_CCD_DRY" if dry_run else f"PIN_CCD{target_ccd}"
        decision_id = storage.record_decision(
            "ccd", d.pid, d.comm, action, reason,
            state={"ccd": target_ccd, "cpus": cpu_list_str,
                   "prev_cpus": d.cpus_allowed, "cmdline": d.cmdline[:200]},
        )
        actions += 1
        # Always record in state — even in dry-run — so we dedup within
        # the daemon's lifetime. Fresh run = fresh log entries again.
        state.pinned[d.pid] = ActiveAction(
            decision_id=decision_id, module="ccd", pid=d.pid, comm=d.comm,
            action=action,
            metadata={"prev_cpus": d.cpus_allowed, "ccd": target_ccd,
                      "dry_run": dry_run},
        )
        if dry_run:
            continue
        try:
            actuators.taskset_pin(d.pid, cpu_list_str)
        except Exception as e:
            # actuation failed — remove from state so we can retry
            state.pinned.pop(d.pid, None)
            storage.record_decision(
                "ccd", d.pid, d.comm, "PIN_FAIL", f"{type(e).__name__}: {e}",
            )
    return actions


def _in_window(hm: str, start: str, end: str) -> bool:
    # assume same-day window (no midnight wrap in Phase 1)
    return start <= hm <= end


def governor_judge(cfg_all: dict, state: State, storage: Storage,
                    dry_run: bool, topo: Topology, now: float) -> int:
    cfg = cfg_all["governor"]
    if not cfg.get("enabled", True):
        return 0
    actuate = cfg.get("actuate", False) and not dry_run
    actions = 0

    # compute tz-local time
    tcfg = cfg["trading_hours"]
    tz = ZoneInfo(tcfg.get("tz", "Asia/Kolkata"))
    local = _dt.datetime.fromtimestamp(now, tz=tz)
    weekday = local.strftime("%a").lower()[:3]
    hm = local.strftime("%H:%M")
    allowed_days = {d.lower()[:3] for d in tcfg.get("days", [])}

    is_trading = (weekday in allowed_days
                  and _in_window(hm, tcfg["start"], tcfg["end"]))

    ncfg = cfg.get("night_quiet") or {}
    is_night = bool(ncfg) and _in_window(hm, ncfg.get("start", "01:00"),
                                          ncfg.get("end", "06:00"))

    if is_trading and not state.trading_lock_active:
        ccd = int(tcfg["lock_ccd"])
        gov = tcfg["lock_governor"]
        cpus = topo.cpus_for_ccd(ccd)
        actual = "ACTUATED" if actuate else "LOGGED_ONLY"
        reason = (f"trading session open ({weekday} {hm} IST) — "
                  f"lock CCD{ccd} cpus={cpus} to {gov}; mode={actual}")
        storage.record_decision(
            "governor", 0, "trading-lock", f"GOV_LOCK_{gov.upper()}_CCD{ccd}",
            reason, state={"cpus": cpus, "gov": gov, "actuated": actuate},
        )
        actions += 1
        if actuate:
            for cpu in cpus:
                if actuators.set_governor(cpu, gov):
                    state.governor_overrides[cpu] = gov
        state.trading_lock_active = True

    elif state.trading_lock_active and not is_trading:
        storage.record_decision(
            "governor", 0, "trading-lock", "GOV_LOCK_RELEASE",
            f"trading session closed ({weekday} {hm}) — release governor lock",
            state={},
        )
        actions += 1
        if actuate:
            for cpu, prev_gov in list(state.governor_overrides.items()):
                actuators.set_governor(cpu, "schedutil")
            state.governor_overrides.clear()
        state.trading_lock_active = False

    if is_night and not state.night_quiet_active:
        mf = ncfg.get("max_freq_ghz", 2.2)
        reason = (f"night quiet window open ({hm}) — clamp all cores to "
                  f"{mf} GHz, gov={ncfg.get('governor', 'powersave')}; "
                  f"mode={'ACTUATED' if actuate else 'LOGGED_ONLY'}")
        storage.record_decision(
            "governor", 0, "night-quiet", "GOV_NIGHT_QUIET",
            reason, state={"max_ghz": mf, "gov": ncfg.get("governor"),
                            "actuated": actuate},
        )
        actions += 1
        state.night_quiet_active = True
    elif state.night_quiet_active and not is_night:
        storage.record_decision(
            "governor", 0, "night-quiet", "GOV_NIGHT_QUIET_RELEASE",
            f"night window closed ({hm}) — restore defaults",
            state={},
        )
        actions += 1
        state.night_quiet_active = False

    return actions

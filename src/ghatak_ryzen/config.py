"""YAML config loader with hard-coded sane defaults.

User config at ~/.config/ghatak-ryzen/config.yml *overrides* these,
but the daemon runs with defaults if config is missing.
"""
from __future__ import annotations

import copy
import os
from pathlib import Path
from typing import Any

import yaml

DEFAULT_CONFIG: dict[str, Any] = {
    "daemon": {
        "tick_interval_sec": 1.0,
        "judge_every_n_ticks": 10,          # judges fire every 10s
        "log_level": "info",
        "db_path": "/dev/shm/ghatak-ryzen.db",
        "log_path": str(Path.home() / ".local/share/ghatak-ryzen/decisions.log"),
        "jsonl_path": str(Path.home() / ".local/share/ghatak-ryzen/decisions.jsonl"),
        "dry_run": True,                    # SAFETY: ship dry, flip after review
    },
    "bloat_judge": {
        "enabled": True,
        "suspend_after_idle_min": 10,       # seconds*60 of idle streak needed
        "suspend_cpu_pct_min": 20.0,        # only suspend if CPU% above this
        "value_score_zero_threshold": 0.01, # ≤ this = effectively zero
        "kill_after_idle_hr": 2,            # phase 1: disabled (see enable_kill)
        "kill_rss_threshold_gb": 1.0,
        "enable_kill": False,               # phase 1 MVP: never kill
        "immune_while_focused_min": 5,
        "whitelist_exact": [
            "systemd", "init", "sshd", "Xorg", "gnome-shell", "gnome-terminal-",
            "dbus-daemon", "NetworkManager", "pulseaudio", "pipewire", "pipewire-pulse",
            "wireplumber", "gnome-session-b", "claude", "bash", "zsh", "fish",
            "code", "code-insiders", "code-server", "cursor", "tmux", "tmux:server",
            "python3", "node", "ssh",
        ],
        "whitelist_path_patterns": [
            "/home/aditya/ghatak-trader/",
            "/home/aditya/tools/factory-bot/",
            "/home/aditya/tools/sonnet-bridge/",
            "/home/aditya/tools/iphonecam/",
            "/home/aditya/tools/gp/",
            "/home/aditya/tools/ghatak-keylight/",
            "pocketbase",
            "mediamtx",
            "cloudflared",
            "code-server",
            "claude-code",
        ],
    },
    "ccd_conductor": {
        "enabled": True,
        "pin_patterns": {
            # comm/cmdline substring → CCD index
            "ffmpeg":     {"ccd": 0},
            "mediamtx":   {"ccd": 0},
            "monitor.py": {"ccd": 1},
            # NOTE: `claude` pattern removed 2026-04-22 — pinning Claude to CCD1
            # created contention with trading monitor.py (also CCD1). Claude now
            # floats on all 16 threads — better for parallel tool calls and
            # avoids stealing cycles from monitor.py during trading hours.
        },
    },
    "governor": {
        # Phase 1 MVP: log decisions only, no sysfs writes (polkit = phase 2)
        "enabled": True,
        "actuate": False,
        "trading_hours": {
            "days": ["mon", "tue", "wed", "thu", "fri"],
            "start": "09:14",
            "end":   "15:30",
            "tz":    "Asia/Kolkata",
            "lock_ccd": 1,
            "lock_governor": "performance",
        },
        "night_quiet": {
            "start": "01:00",
            "end":   "06:00",
            "max_freq_ghz": 2.2,
            "governor": "powersave",
        },
    },
}


def _deep_merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load(path: str | Path | None = None) -> dict[str, Any]:
    """Load user config, overlaid on defaults."""
    if path is None:
        path = Path(os.environ.get("GHATAK_RYZEN_CONFIG",
                                   Path.home() / ".config/ghatak-ryzen/config.yml"))
    path = Path(path)
    if not path.exists():
        return copy.deepcopy(DEFAULT_CONFIG)
    with path.open() as f:
        user = yaml.safe_load(f) or {}
    return _deep_merge(DEFAULT_CONFIG, user)


def default_yaml() -> str:
    return yaml.safe_dump(DEFAULT_CONFIG, sort_keys=False, default_flow_style=False)

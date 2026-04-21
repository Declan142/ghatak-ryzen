"""CCD/CCX detection via L3-cache sharing on /sys.

Zen 2 (Ryzen 3xxx, 3700X): 2 CCDs × 4 cores × 2 threads. Each CCD has its own L3.
Zen 3+ (Ryzen 5xxx): 1 or 2 CCDs, 8-core CCX (1 L3 per CCD).

Any CPU in the same `cache/index3/shared_cpu_list` shares an L3 = same CCD.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass
class Topology:
    ccds: list[list[int]]       # CPUs per CCD, ascending — includes SMT siblings
    n_cpus: int
    daemon_pin: int             # last SMT thread of last CCD (safe parking spot)

    def cpus_for_ccd(self, ccd: int) -> list[int]:
        return self.ccds[ccd]

    def ccd_of_cpu(self, cpu: int) -> int | None:
        for i, group in enumerate(self.ccds):
            if cpu in group:
                return i
        return None

    def cpu_list_str(self, ccd: int) -> str:
        return ",".join(str(c) for c in self.ccds[ccd])


def detect() -> Topology:
    sys_cpu = Path("/sys/devices/system/cpu")
    groups: dict[str, list[int]] = {}
    for cpu_dir in sorted(sys_cpu.glob("cpu[0-9]*"), key=lambda p: int(p.name[3:])):
        name = cpu_dir.name
        if not name[3:].isdigit():
            continue
        cpu = int(name[3:])
        # skip offline CPUs
        online_f = cpu_dir / "online"
        if online_f.exists() and online_f.read_text().strip() == "0":
            continue
        for idx_dir in sorted(cpu_dir.glob("cache/index*")):
            level_f = idx_dir / "level"
            if not level_f.exists():
                continue
            if level_f.read_text().strip() == "3":
                shared = (idx_dir / "shared_cpu_list").read_text().strip()
                groups.setdefault(shared, []).append(cpu)
                break
    if not groups:
        # fallback: one CCD containing every CPU
        cpus = sorted(int(p.name[3:]) for p in sys_cpu.glob("cpu[0-9]*") if p.name[3:].isdigit())
        return Topology(ccds=[cpus], n_cpus=len(cpus), daemon_pin=cpus[-1])
    ccds = sorted((sorted(v) for v in groups.values()), key=lambda g: g[0])
    n_cpus = sum(len(c) for c in ccds)
    daemon_pin = ccds[-1][-1]
    return Topology(ccds=ccds, n_cpus=n_cpus, daemon_pin=daemon_pin)

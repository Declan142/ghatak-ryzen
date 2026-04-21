"""`ghatak-ryzen` command-line front end.

Sub-commands:
    run       — start daemon (foreground)
    status    — current state: suspended pids, pinned pids, governor
    log       — tail decisions.log
    undo <id> — revert a single decision
    panic     — revert EVERY active decision + restore defaults
    dryrun    — run for 60s in dry_run mode, print decisions, exit
    config    — print default YAML
    topology  — show detected CCD map
"""
from __future__ import annotations

import json
import os
import signal
import sys
import time
from pathlib import Path

import click
from rich import box
from rich.console import Console
from rich.table import Table

from . import actuators, daemon, sensors
from .config import default_yaml, load as load_config
from .storage import Storage
from .topology import detect as detect_topology


console = Console()


def _storage(cfg: dict) -> Storage:
    return Storage(
        db_path=cfg["daemon"]["db_path"],
        log_path=cfg["daemon"]["log_path"],
        jsonl_path=cfg["daemon"].get("jsonl_path"),
    )


@click.group()
@click.option("--config", "-c", default=None, help="Path to config.yml")
@click.pass_context
def cli(ctx: click.Context, config: str | None) -> None:
    ctx.ensure_object(dict)
    ctx.obj["config_path"] = config


@cli.command()
@click.pass_context
def run(ctx: click.Context) -> None:
    """Start the daemon (foreground). Systemd runs this."""
    sys.exit(daemon.main(ctx.obj["config_path"]))


@cli.command()
@click.option("--duration", default=60, help="seconds")
@click.pass_context
def dryrun(ctx: click.Context, duration: int) -> None:
    """Run for DURATION seconds in dry-run mode — decisions printed, nothing actuated."""
    cfg = load_config(ctx.obj["config_path"])
    cfg["daemon"]["dry_run"] = True
    cfg["daemon"]["judge_every_n_ticks"] = max(1, int(cfg["daemon"]["judge_every_n_ticks"]))
    d = daemon.Daemon(cfg)
    console.print(f"[yellow]dryrun[/yellow] — {duration}s, ccds={len(d.topo.ccds)} "
                  f"pin=cpu{d.topo.daemon_pin}")

    def _stop(_signum, _frame):
        d._stop = True
    for s in (signal.SIGTERM, signal.SIGINT):
        signal.signal(s, _stop)

    start = time.time()
    next_tick = start
    while time.time() - start < duration and not d._stop:
        d._one_tick(time.time())
        next_tick += d.tick
        sleep = next_tick - time.time()
        if sleep > 0:
            time.sleep(sleep)
    # show last 30 decisions
    rows = d.storage.recent_decisions(30)
    d.storage.close()
    _render_decisions(rows, "Last 30 decisions (dry-run)")


@cli.command()
@click.pass_context
def status(ctx: click.Context) -> None:
    """Show active pinnings, suspensions, and governor state."""
    cfg = load_config(ctx.obj["config_path"])
    s = _storage(cfg)
    topo = detect_topology()

    t1 = Table(title="Topology", box=box.SIMPLE)
    t1.add_column("CCD", justify="right")
    t1.add_column("CPUs", justify="left")
    for i, ccds in enumerate(topo.ccds):
        t1.add_row(str(i), ",".join(str(c) for c in ccds))
    console.print(t1)

    # governor per-cpu
    t2 = Table(title="Governor / freq", box=box.SIMPLE)
    t2.add_column("CPU", justify="right")
    t2.add_column("Governor")
    t2.add_column("Cur MHz", justify="right")
    for cpu in range(topo.n_cpus):
        gov = actuators.get_governor(cpu) or "?"
        mhz = actuators.get_cur_freq_khz(cpu) // 1000
        t2.add_row(str(cpu), gov, str(mhz))
    console.print(t2)

    # active decisions
    rows = [r for r in s.active_decisions() if r[2] != "daemon"]
    _render_decisions(
        [(r[0], r[1], r[2], r[3], r[4], r[5], r[6], 0) for r in rows],
        "Active decisions (not reverted)",
    )
    s.close()


@cli.command(name="log")
@click.option("--follow", "-f", is_flag=True, help="tail -f style")
@click.option("--lines", "-n", default=30)
@click.pass_context
def log_cmd(ctx: click.Context, follow: bool, lines: int) -> None:
    """Tail the human-readable decision log."""
    cfg = load_config(ctx.obj["config_path"])
    path = Path(cfg["daemon"]["log_path"])
    if not path.exists():
        console.print(f"[yellow]no log yet at {path}[/yellow]")
        return
    if follow:
        os.execvp("tail", ["tail", "-n", str(lines), "-F", str(path)])
    else:
        with path.open() as f:
            # cheap tail
            data = f.readlines()
        for line in data[-lines:]:
            print(line, end="")


@cli.command()
@click.argument("decision_id", type=int)
@click.pass_context
def undo(ctx: click.Context, decision_id: int) -> None:
    """Revert a single decision (SIGCONT, unpin, etc.)."""
    cfg = load_config(ctx.obj["config_path"])
    s = _storage(cfg)
    row = s.get_decision(decision_id)
    if row is None:
        console.print(f"[red]no decision id={decision_id}[/red]")
        s.close()
        sys.exit(1)
    _id, _ts, module, pid, comm, action, reason, state_json, reverted = row
    if reverted:
        console.print(f"[yellow]id={decision_id} already reverted[/yellow]")
        s.close()
        return
    st = json.loads(state_json or "{}")
    ok, msg = _revert_one(module, action, pid, comm, st)
    if ok:
        s.mark_reverted(decision_id, msg)
        console.print(f"[green]reverted id={decision_id}:[/green] {msg}")
    else:
        console.print(f"[red]failed id={decision_id}:[/red] {msg}")
    s.close()


@cli.command()
@click.confirmation_option(prompt="Revert EVERY active decision? [panic]")
@click.pass_context
def panic(ctx: click.Context) -> None:
    """Revert every active decision. Restores SIGCONT, unpins, default governor."""
    cfg = load_config(ctx.obj["config_path"])
    s = _storage(cfg)
    rows = s.active_decisions()
    n_ok = n_fail = 0
    for r in rows:
        _id, _ts, module, pid, comm, action, _reason, state_json = r
        if module == "daemon":
            continue
        st = json.loads(state_json or "{}")
        ok, msg = _revert_one(module, action, pid, comm, st)
        if ok:
            s.mark_reverted(_id, "panic — " + msg)
            n_ok += 1
        else:
            n_fail += 1
    console.print(f"[green]panic done[/green]: reverted={n_ok}  failed={n_fail}")
    s.close()


@cli.command()
def config() -> None:
    """Print the default config YAML to stdout."""
    click.echo(default_yaml())


@cli.command()
def topology() -> None:
    """Detect and print CCD/CCX layout from /sys."""
    topo = detect_topology()
    for i, cpus in enumerate(topo.ccds):
        console.print(f"CCD{i}: cpus={cpus}")
    console.print(f"daemon will pin to cpu{topo.daemon_pin}")


def _revert_one(module: str, action: str, pid: int, comm: str,
                state: dict) -> tuple[bool, str]:
    # Dry-run decisions never actuated — revert is a no-op marker.
    if action.endswith("_DRY"):
        return True, f"dry-run action, no state to revert ({action})"

    if not actuators.pid_alive(pid) and pid > 0:
        return True, f"pid {pid} already dead"

    if module == "bloat" and action == "SUSPEND":
        try:
            actuators.signal_cont(pid)
            return True, f"SIGCONT pid={pid} comm={comm}"
        except (ProcessLookupError, PermissionError) as e:
            return False, f"{type(e).__name__}: {e}"

    if module == "ccd" and action.startswith("PIN_CCD"):
        prev = state.get("prev_cpus", "")
        # if we don't know prev, restore full-range 0..(nproc-1)
        if not prev:
            import os as _os
            prev = f"0-{_os.cpu_count() - 1}"
        try:
            actuators.taskset_pin(pid, prev)
            return True, f"unpin pid={pid} cpus={prev}"
        except Exception as e:
            return False, f"{type(e).__name__}: {e}"

    if module == "governor":
        return True, "governor revert is advisory (phase-1 logs-only by default)"

    if action.startswith("SUSPEND_DRY") or action.startswith("PIN_CCD_DRY"):
        return True, "dry-run, nothing to revert"

    return False, f"unknown module/action: {module}/{action}"


def _render_decisions(rows, title: str) -> None:
    t = Table(title=title, box=box.SIMPLE)
    t.add_column("id", justify="right")
    t.add_column("when")
    t.add_column("mod")
    t.add_column("pid", justify="right")
    t.add_column("comm")
    t.add_column("action")
    t.add_column("reason")
    for row in rows[:50]:
        if len(row) >= 8:
            _id, _ts, module, pid, comm, action, reason = row[:7]
        else:
            _id, _ts, module, pid, comm, action, reason = row
        stamp = time.strftime("%H:%M:%S", time.localtime(int(_ts)))
        reason_short = (reason or "")[:80]
        t.add_row(str(_id), stamp, module, str(pid), comm or "?", action, reason_short)
    console.print(t)


def main() -> None:
    cli(obj={})


if __name__ == "__main__":
    main()

# ghatak-ryzen

> **Claudify the CPU.** Every cycle earns its keep.

A judgment daemon for Linux that reasons about every running process like Claude reasons about a task — per-process value scoring, CCD-aware affinity, idle-harvesting, predictive prefetch, per-workload governor. Default Linux scheduler = 1970s round-robin. This replaces naive defaults with *judgment*.

## Target hardware

- **AMD Ryzen 7 3700X** (Zen 2, 8C/16T, 2 CCDs × 16MB L3, no AVX-512) — primary dev target
- Should work on any Zen 2 / Zen 3 / Zen 4 desktop chip
- Linux kernel ≥ 6.0, eBPF support required

## Status

| Phase | Module | State |
|---|---|---|
| 1 | Bloat Judge (value score + SIGSTOP/kill) | planned |
| 1 | CCD Conductor (cache-locality pin) | planned |
| 1 | Governor per-workload | planned |
| 2 | Timeline DB + statusline hook | planned |
| 3 | Session Predictor (prefetch) | planned |
| 4 | Idle Harvester (empire work in spare cycles) | planned |
| 5 | Distillation Loop (Claude → local model) | planned |

See [`BLUEPRINT.md`](BLUEPRINT.md) for full design, [`ROADMAP.md`](ROADMAP.md) for phased delivery, [`BIOS.md`](BIOS.md) for the hardware-level tuning that uncorks the software layer (15-25% perf lift before a single line of daemon code runs).

## Install (planned)

```bash
git clone https://github.com/Declan142/ghatak-ryzen.git ~/ghatak-ryzen
cd ~/ghatak-ryzen
./scripts/install.sh        # sets up systemd user service + config
systemctl --user enable --now ghatak-ryzen
ghatak-ryzen status         # see current decisions
tail -f ~/.local/share/ghatak-ryzen/decisions.log
```

## Why it exists

16 threads × 3.6 GHz = **57 billion decisions/sec**, 99% wasted on defaults. Kernel scheduler has no idea:
- This Chrome tab has been burning 81% CPU on zero output for 2 hours
- This `next-server` is a zombie from 2 days ago holding 5GB
- `ffmpeg` wants L3 locality, not round-robin across CCDs
- You always open the trading monitor at 9:14 AM — page cache could be warm

Claude-level judgment at the OS layer, running on ~1% of one core, decides.

## License

MIT — see [LICENSE](LICENSE)

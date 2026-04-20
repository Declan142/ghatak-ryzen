# ghatak-ryzen — Blueprint

> **Goal:** Every CPU cycle accountable. Every running process justified. Every resource allocation optimal. A Claude-level judgment layer over the Linux scheduler.

---

## 1. Philosophy

The Linux kernel's CFS scheduler, `schedutil` governor, and stock `irqbalance` all operate on **naive, context-free heuristics**:

- CFS gives equal time-slices to every process, regardless of whether it's producing useful output or burning cycles in a stuck JS loop
- `schedutil` ramps frequency purely on utilization, not on which workload is running or what the user is doing
- No scheduler on Linux is CCD-aware out of the box for Zen 2+ topology (ccd_prefer kernel patches exist but aren't upstream)
- No scheduler tracks "is this process producing anything?"

ghatak-ryzen inserts a **reasoning layer** between raw kernel facts and process behavior. Every 10 seconds the conductor daemon:

1. **Senses** state (processes, I/O, cache, user focus, thermals)
2. **Judges** value (output per CPU-second, cache warmth, historical pattern)
3. **Decides** action (priority, affinity, governor, suspend, kill, prefetch)
4. **Logs** the reason (auditable — like Claude's tool-call transcript)

The key shift: CPU scheduling based on **value produced**, not just fairness.

---

## 2. Hardware target — Ryzen 7 3700X (Zen 2)

| Property | Value | Relevance |
|---|---|---|
| Cores / threads | 8 / 16 | Ample parallelism — room for judge + harvester to coexist |
| CCDs | 2 | Each CCD has 4 cores (Zen 2 chiplet) |
| CCX per CCD | 2 | Each CCX has 2 cores + shared 16MB L3 |
| L3 cache | 2 × 16MB = 32MB total | Split — cross-CCX access = memory roundtrip |
| L2 cache | 4MB (512KB × 8) | Per-core |
| Infinity Fabric clock | = DDR speed / 2 | RAM speed directly affects inter-CCD latency |
| Boost | 4.4 GHz | PBO can push individual cores higher |
| TDP | 65W | Eco-mode 45W via BIOS |
| PCIe lanes | 24 (Gen 4 on X570) | Not scheduler-relevant |

### CCD/CCX topology (from `lscpu`)

```
CCD0:
  CCX0: cpu0, cpu1, cpu2, cpu3 (HT: cpu8,9,10,11)  | L3: 16MB
CCD1:
  CCX1: cpu4, cpu5, cpu6, cpu7 (HT: cpu12,13,14,15) | L3: 16MB
```

**Cross-CCD latency** (Zen 2): ~80ns vs ~8ns same-CCX. Pinning process to consistent CCD preserves L3 warmth → 2-5x speedup on cache-bound workloads.

### Real-world observation (this machine, 2026-04-21)

Current load: iphonecam 2-stage ffmpeg (~320% CPU) + 7 Claude sessions + Chrome + MediaMTX. Kernel migrates ffmpeg across CCDs every few seconds — cold L3 every migration. Conductor pins it, stops migration, ~15% perf reclaimed.

---

## 3. Architecture

```
┌─────────────────── ghatak-ryzen daemon (1 Python process) ────────────────────┐
│                                                                                │
│  ┌──── SENSE (1 Hz) ─────┐   ┌──── JUDGE (0.1 Hz) ────┐  ┌──── ACT ────────┐ │
│  │                        │   │                         │  │                  │ │
│  │  /proc/<pid>/*         │   │  Rule pipeline:         │  │  taskset         │ │
│  │  eBPF: syscalls, I/O  ├──►│   1. BloatJudge         ├─►│  chrt            │ │
│  │  perf: cache, branch  │   │   2. CCDConductor       │  │  ionice          │ │
│  │  /sys/../cpufreq      │   │   3. GovernorSwitch     │  │  kill -STOP/CONT │ │
│  │  nvidia-smi           │   │   4. SessionPredictor   │  │  cpufreq-set     │ │
│  │  wmctrl (focus)       │   │   5. IdleHarvester      │  │  vmtouch prefetch│ │
│  │  X11 idle time        │   │                         │  │  systemctl stop  │ │
│  │                        │   │  ML layer (Phase 5):    │  │                  │ │
│  │                        │   │   → distilled classifier│  │                  │ │
│  └────────────────────────┘   └─────────────────────────┘  └──────────────────┘ │
│                                         │                                       │
│                                         ▼                                       │
│                          ┌─── SQLite timeline ────┐                            │
│                          │  processes_1s          │ ←─ 1s snapshots            │
│                          │  decisions             │ ←─ every action, reasoned  │
│                          │  patterns              │ ←─ learned (daily/weekly)  │
│                          │  outcomes              │ ←─ "was that decision right"│
│                          └────────────────────────┘                            │
└────────────────────────────────────────────────────────────────────────────────┘
                                         │
                                         ▼
                  ┌─── observability ───────────────────┐
                  │  statusline integration             │
                  │  ghatak-ryzen {status|log|tune}     │
                  │  weekly digest to Telegram          │
                  │  decisions.log (auditable)          │
                  └──────────────────────────────────────┘
```

### CPU / memory budget for the daemon itself

- Target: <1% CPU averaged, <200MB RSS
- Pinned to `cpu15` (last hyperthread of CCD1), `SCHED_IDLE` priority — never steals from foreground work
- SQLite on `/dev/shm` (tmpfs) for hot timeline, flushed to disk every 60s
- eBPF programs compiled once, reused — total overhead <0.5% per probe

---

## 4. The five judgment modules

### 4.1 Bloat Judge

**Purpose:** Identify and evict processes burning CPU without producing output.

**Value formula** (per minute, per process):
```
value_score = (
    0.3 * log1p(bytes_written_to_disk)
  + 0.3 * log1p(bytes_sent_network)
  + 0.2 * log1p(stdout_bytes)
  + 0.1 * focused_window_time_sec
  + 0.1 * dom_mutations (chrome only, via perf)
) / cpu_seconds_consumed
```

**Decision table:**

| Condition | Action |
|---|---|
| `value_score == 0` for 10 min + CPU% < 0.5% | keep — it's just idle, fine |
| `value_score == 0` for 10 min + CPU% > 20% | SUSPEND (`SIGSTOP`), log reason |
| `value_score == 0` for 2 hr + RSS > 1GB | KILL (SIGTERM → 30s → SIGKILL) |
| `value_score > 5` consistently | whitelist, no further action |
| user explicitly focused the window in last 5 min | immune (never touch active work) |

**Exemptions** (hardcoded):
- PID 1, systemd, kernel threads (kthreads)
- Anything matching `/etc/ghatak-ryzen/whitelist.yml`
- User's primary shell processes
- Claude CLI sessions that are actively in a session (tracked via pts write activity)

**Chrome-specific:**
- Use `chrome-cli` or tab-via-debugger to query tab URL / last interaction
- Suspend individual renderers, not the main process
- `SIGSTOP` on a backgrounded tab is safe — Chrome handles the pause gracefully and resumes on focus

**Config (yaml):**
```yaml
bloat_judge:
  enabled: true
  suspend_after_idle_min: 10
  kill_after_idle_hr: 2
  kill_rss_threshold_gb: 1.0
  whitelist_patterns:
    - "monitor.py"
    - "ghatak-trader"
    - "pocketbase"
  dry_run: false  # if true, only logs, doesn't act
```

---

### 4.2 CCD Conductor

**Purpose:** Enforce cache-locality on Zen 2. Pin processes to a CCD once warm, migrate only under high load.

**Tracked per-process via perf:**
- `cache-references` and `cache-misses` on each CCD's L3
- Current CPU list (via `/proc/<pid>/status` → `Cpus_allowed_list`)
- Working-set size estimate (RSS - shared)

**Algorithm:**
```
for each process:
    current_ccd = infer from last 10s of runtime
    other_ccd_misses = perf counter on other CCD's L3

    if other_ccd_misses > threshold:
        # process has working set on current_ccd
        pin to current_ccd cores (taskset --cpu-list 0-3 for CCD0, 4-7 for CCD1)
        mark STICKY in state

    if current_ccd.load > 90%:
        # only then migrate the least-cache-hot process
        pick STICKY process with lowest L3 hit rate
        migrate to less-loaded CCD
        re-pin
```

**High-value pinnings (codified):**

| Workload | Pin target | Reason |
|---|---|---|
| `ffmpeg` (iphonecam pipeline, both stages) | CCD0 (0-3) | 2-stage pipe benefits from shared L3 |
| `mediamtx` | CCD0 (0-3) | Joins the ffmpeg L3 domain |
| `monitor.py` (ghatak-trader) during market hrs | CCD1 (4-7) exclusive | latency-critical, no contention |
| Claude CLI sessions | CCD1 (4-7) | isolate from video pipeline |
| Chrome GPU process | CCD0 or wherever GPU driver maps cleanly | |
| ghatak-ryzen daemon itself | cpu15 (idle HT of CCD1) | minimal displacement |

---

### 4.3 Governor per-workload

**Purpose:** Replace system-wide global governor with per-core, workload-aware frequency scaling.

Current `powerprofilesctl` has only 3 global modes. This module drives per-core.

**Logic:**
```python
for each core:
    workload = classify(processes on core, last 10s)

    match workload:
        case "interactive_foreground":
            governor = "performance"  # tight latency budget
        case "burst_compute" (ffmpeg, build, compile):
            governor = "performance"
            cpufreq_min = max_freq  # lock high
        case "steady_throughput" (training, batch):
            governor = "schedutil"
            cpufreq_min = 2.2 GHz
        case "idle":
            governor = "powersave"
            cpufreq_max = 1.8 GHz  # cool + quiet
        case "mixed/default":
            governor = "schedutil"  # current default
```

**Special modes:**
- **Trading session lock**: 9:14 AM - 15:30 IST weekdays → CCD1 cores 4-7 hard-locked to `performance`, `cpufreq_min = max_freq`. No throttling during 0-DTE sessions ever.
- **Night quiet mode**: 1 AM - 6 AM → if no foreground activity, clamp max_freq to 2.2 GHz on all cores, `powersave`. Fans spin down, heat drops.
- **iphonecam mode**: when `/dev/video10` is being written → ffmpeg cores locked to `performance` irrespective of utilization (avoid ramp-down glitches).

---

### 4.4 Session Predictor (prefetch layer)

**Purpose:** You have habits. Warm the page cache before you act.

**Learned patterns** (SQLite `patterns` table):
```
monday_0914: open ~/ghatak-trader/monitor.py  (99% confidence, 200 samples)
daily_0200:  run factory-dispatch.py          (100% confidence, cron-driven)
iphonecam_start_event: load ffmpeg, mediamtx  (observed after `iphonecam start`)
weekend_evening: open Chrome + claude-in-chrome workflows
```

**Actions:**
- `vmtouch -t <files>` to force page-cache warm
- Preload shared libraries into cache (Python `sys.modules`-level can't — but `.so` files can be `vmtouch`ed)
- Spin up any daemons that the predicted task depends on (e.g., `kite-redis` if monitor is about to open)

**Learning loop:**
- Every process start event + timestamp + CWD logged to `processes_1s`
- Nightly: aggregate last 30 days → find patterns with >70% confidence, >10 samples → commit to `patterns`
- Patterns expire after 14 days of non-occurrence

**User override:**
```bash
ghatak-ryzen predict add "09:14 mon-fri" "~/ghatak-trader/monitor.py"
ghatak-ryzen predict disable monday_0914
```

---

### 4.5 Idle Harvester

**Purpose:** CPU never truly idles. Empire work fills spare cycles invisibly.

**Trigger:** System-wide CPU utilization <20% for 60+ seconds.

**Work queue** (ordered by value):
1. Embed new conversation transcripts → atlas search index (feeds `atlas-recall`)
2. Retrain Ghatak trade predictor on the day's new trades
3. Factory QA sweep (code-diff review of today's atoms)
4. Run `dream` skill — memory consolidation
5. Compress `/var/log/journal/` older than 30 days with `journalctl --vacuum-time`
6. Run `apt list --upgradable` and cache for statusline
7. Backfill missed patterns analysis in Session Predictor

**Rules:**
- Every harvested job runs at `SCHED_IDLE`, `ionice -c idle`
- Aborts instantly if foreground CPU demand rises above 30%
- Never runs during trading hours (9:14 IST - 15:30 IST)
- Never runs on battery (if ever applicable — desktop always AC but check flag anyway)
- Weekly digest to Telegram: "this week Ryzen harvested X hours of spare compute → produced Y embeddings, Z retrainings"

---

## 5. Sensors (data sources)

| Source | Cadence | What it provides |
|---|---|---|
| `/proc/<pid>/{stat,status,io,cmdline,cwd,fd}` | 1 Hz | CPU/mem/io/topology per-process |
| eBPF `tracepoint:syscalls:sys_enter_*` | on-event | syscall rate per-PID |
| eBPF `kprobe:vfs_{read,write}` | on-event | I/O attribution per-PID |
| `perf stat -e {cache-misses,L1-dcache-miss}` | 10 Hz | cache behavior |
| `/sys/devices/system/cpu/cpu*/cpufreq/*` | 1 Hz | per-core freq, gov |
| `k10temp` (via `sensors`) | 1 Hz | Tctl, Tccd per-CCD (needs lm-sensors setup) |
| `nvidia-smi --query-gpu=...` | 1 Hz | GPU util, VRAM (for GTX 1660 usage awareness) |
| `wmctrl -l` + `xdotool getactivewindow` | 0.5 Hz | current focused window, title |
| `xprintidle` | 5 Hz | X11 idle time (user AFK detection) |
| `getent passwd`, `systemctl list-units` | on start | system context |

All sensors feed `processes_1s` timeline table.

---

## 6. Actuators (what it can do)

| Tool | Capability | Reversible? |
|---|---|---|
| `taskset -p <mask> <pid>` | Pin process to CPUs | Yes |
| `chrt -{f,r,o,b,i} -p <prio> <pid>` | Set sched policy | Yes |
| `ionice -c {1,2,3} -p <pid>` | I/O priority | Yes |
| `renice <prio> -p <pid>` | Nice value | Yes |
| `kill -STOP <pid>` / `kill -CONT <pid>` | Pause/resume | Yes |
| `kill -TERM <pid>` / `kill -KILL <pid>` | Terminate | **No** — requires confirm for first run in a week |
| `cpufreq-set -c <cpu> -g <gov>` | Per-core governor | Yes |
| `cpufreq-set -c <cpu> -u <freq> -d <freq>` | Freq bounds | Yes |
| `vmtouch -t <file>` | Page cache load | Yes |
| `sync && echo 3 > /proc/sys/vm/drop_caches` | Drop page cache | No (but harmless) |
| `systemctl {stop,start} <unit>` | Service control | Yes |
| `xdotool key <keys>` | X input | N/A (wouldn't normally use) |

All kill actions logged with full reason. Any action can be reversed via `ghatak-ryzen undo <action-id>`.

---

## 7. Data model

### `processes_1s` (hot table, tmpfs)
```sql
CREATE TABLE processes_1s (
    ts INTEGER,          -- unix epoch
    pid INTEGER,
    comm TEXT,
    cmdline TEXT,
    cpu_pct REAL,
    rss_kb INTEGER,
    bytes_read INTEGER,
    bytes_written INTEGER,
    cpus_allowed TEXT,
    state TEXT,          -- R, S, D, Z, T
    value_score REAL,
    PRIMARY KEY (ts, pid)
);
-- Retained 24 hours in tmpfs, archived daily to disk
```

### `decisions` (audit log, disk)
```sql
CREATE TABLE decisions (
    id INTEGER PRIMARY KEY,
    ts INTEGER,
    module TEXT,          -- bloat | ccd | governor | predictor | harvester
    pid INTEGER,
    comm TEXT,
    action TEXT,          -- SUSPEND | KILL | PIN_CCD0 | GOV_PERF | PREFETCH
    reason TEXT,          -- human-readable
    state_snapshot_json TEXT,
    reverted BOOLEAN DEFAULT FALSE,
    revert_reason TEXT
);
```

### `patterns` (learned)
```sql
CREATE TABLE patterns (
    id INTEGER PRIMARY KEY,
    name TEXT,
    trigger_expr TEXT,    -- "mon-fri 09:14"
    action TEXT,          -- "prefetch:~/ghatak-trader/monitor.py"
    confidence REAL,
    sample_count INTEGER,
    last_seen TIMESTAMP
);
```

### `outcomes` (for distillation)
```sql
CREATE TABLE outcomes (
    decision_id INTEGER REFERENCES decisions(id),
    outcome TEXT,         -- 'correct' | 'false_positive' | 'false_negative'
    feedback_source TEXT, -- 'user_undo' | 'claude_review' | 'auto_metric'
    notes TEXT
);
```

---

## 8. Decision log format

Every action appends a line to `~/.local/share/ghatak-ryzen/decisions.log`:

```
[2026-04-21 10:47:02.314] SUSPEND chrome-renderer pid=1067587
    reason: 81.6% CPU sustained 2h34m, 0 DOM mutations, 0 network frames,
            0 user focus events. likely stuck JS loop.
    saved: ~1.7 cores, 587MB RSS (frozen, resumable on focus)
    revert-id: a7f3c2

[2026-04-21 10:47:02.318] PIN_CCD0 ffmpeg pid=1072833,1072834
    reason: 2-stage iphonecam pipe; shared YUV frames benefit from L3 locality.
            perf: cross-CCD cache-refs was 18% of total → pinning drops to <1%
    current: cpulist=0-3,8-11 (CCD0 SMT)

[2026-04-21 10:47:03.001] KILL next-server pid=176086
    reason: idle 48h17m, 0 connections, 5.02GB RSS, cwd=~/code/taxwalaai.
            project atlas shows prod live at taxwalaai.com, dev server unneeded.
    user confirmed: yes (2026-04-21 02:20 via `ghatak-ryzen ack`)
```

Readable with `tail -f` — **CPU's thought process, transcribed**.

---

## 9. Integration with Empire

### Atlas
- Active project file: `~/.claude/atlas/projects/active/ghatak-ryzen.md`
- State / pinnings surveyed at session start
- Weekly session log: `~/.claude/atlas/projects/sessions/YYYY-MM-DD-ghatak-ryzen-*.md`

### Statusline (`~/.claude/statusline.py`)
Adds a micro-panel:
```
⚡ CCD0: ffmpeg | CCD1: monitor | 3 STOP'd | gov: perf/save | harvest: idle
```

### Telegram (factory bot)
- `/ryzen status` → current pinnings and suspensions
- `/ryzen digest` → weekly summary of decisions + cycles harvested
- Push notif if CPU-wide thermal throttle event detected
- Push notif if any kill action taken (so user can ack/undo)

### Distillation Loop (cross-project)
- Claude reviews a week of `decisions` + `outcomes` → labels false positives
- Labels train a CPU-native DistilBERT / sklearn classifier
- Classifier replaces hardcoded rules incrementally
- Goal: fewer false-positive suspensions, better CCD migration choices

### Ghatak-trader
- `monitor.py` start event → CCD Conductor sees it, pins CCD1, gov=perf
- During 0-DTE session, `ghatak-ryzen` is the *reason* monitor never gets preempted

### iphonecam
- `iphonecam start` → CCD Conductor pins ffmpeg pair + mediamtx to CCD0
- Governor module locks CCD0 cores to performance
- `iphonecam stop` → immediate un-pin, un-lock

---

## 10. Observability

### CLI
```bash
ghatak-ryzen status                 # current decisions, pinnings, suspended PIDs
ghatak-ryzen log [-f]               # tail decision log
ghatak-ryzen undo <id>              # revert a decision
ghatak-ryzen ack <id>               # confirm destructive action
ghatak-ryzen whitelist <pattern>    # never touch
ghatak-ryzen blacklist <pattern>    # always kill when idle
ghatak-ryzen pattern list           # learned patterns
ghatak-ryzen tune                   # interactive: review recent false-positives
ghatak-ryzen dryrun                 # verbose mode, prints actions without executing
ghatak-ryzen benchmark              # measures overhead of daemon itself
```

### Prometheus metrics (port 9733)
- `ghatak_ryzen_decisions_total{module, action}` counter
- `ghatak_ryzen_suspended_processes` gauge
- `ghatak_ryzen_harvested_cpu_seconds_total` counter
- `ghatak_ryzen_cache_miss_rate{ccd}` gauge
- `ghatak_ryzen_governor_state{cpu}` gauge

### Grafana dashboard (optional)
- Timeline heatmap: which process ran where, by minute
- CCD migration visualization
- Decision stream

---

## 11. Failure modes & safety

### Hard safety rails (never cross)

- **Never suspend** PID 1, kthreads, sshd, Xorg, gnome-shell, dbus-daemon, NetworkManager, systemd-*, anything in `/sbin/`, user shell, any process with terminal `tty_nr` matching user's active shell
- **Never kill** a process without 30s SIGTERM grace period
- **Never modify** governor of CPU currently executing the ghatak-ryzen daemon itself
- **Never drop caches** during a known heavy I/O workload (detection: >100 MB/s sustained writes)

### Soft safety (configurable)

- Dry-run mode default for first 48 hours after install
- Destructive actions (kill) require user ack via Telegram button for first week per-process-class
- `ghatak-ryzen panic` command — immediate stop, revert all pinnings, restore all suspended processes, re-enable default governor

### Kill switch

```bash
# One-liner that restores default Linux behavior entirely
ghatak-ryzen panic && systemctl --user stop ghatak-ryzen
```

---

## 12. Configuration

`~/.config/ghatak-ryzen/config.yml`:

```yaml
daemon:
  tick_interval_sec: 10
  sensors_hz: 1
  log_level: info
  pin_to_cpu: 15
  log_path: ~/.local/share/ghatak-ryzen/decisions.log
  db_path: /dev/shm/ghatak-ryzen.db
  db_archive_path: ~/.local/share/ghatak-ryzen/archive.db

bloat_judge:
  enabled: true
  dry_run: false
  suspend_after_idle_min: 10
  kill_after_idle_hr: 2
  kill_rss_threshold_gb: 1.0
  whitelist_exact:
    - systemd
    - init
    - sshd
    - Xorg
    - gnome-shell
    - claude
  whitelist_patterns:
    - "^/home/aditya/ghatak-trader/"
    - "pocketbase"
    - "mediamtx"
  immune_while_focused_min: 5

ccd_conductor:
  enabled: true
  pin_patterns:
    ffmpeg: { ccd: 0, exclusive_to: [ffmpeg, mediamtx] }
    mediamtx: { ccd: 0 }
    monitor.py: { ccd: 1, exclusive: true, sched: fifo, prio: 60 }
    claude: { ccd: 1 }
  migration_threshold_load_pct: 90

governor:
  enabled: true
  trading_hours:
    days: [mon, tue, wed, thu, fri]
    start: "09:14"
    end: "15:30"
    tz: "Asia/Kolkata"
    lock_ccd: 1
    lock_governor: performance
  night_quiet:
    start: "01:00"
    end: "06:00"
    max_freq_ghz: 2.2
    governor: powersave

session_predictor:
  enabled: false   # phase 3 feature
  learning_window_days: 30
  min_confidence: 0.7
  min_samples: 10

idle_harvester:
  enabled: false   # phase 4 feature
  trigger_idle_sec: 60
  max_cpu_pct: 20
  never_during_trading: true
  jobs:
    - name: embed_new_conversations
      cmd: "python3 ~/.claude/scripts/embed-new.py"
      schedule: always
    - name: retrain_ghatak
      cmd: "python3 ~/ghatak-trader/predictor/train.py --incremental"
      schedule: daily_after_1530

telegram:
  enabled: true
  chat_id_env: GHATAK_RYZEN_CHAT_ID
  bot_token_path: ~/.claude/vault/telegram-factory-bot.md
  notify_on:
    - kill
    - first_suspend_per_class
    - thermal_throttle
    - weekly_digest
```

---

## 13. Install & deployment

### Dependencies (Ubuntu 24.04+ / Debian)

```bash
sudo apt install \
    linux-tools-$(uname -r) \
    bpfcc-tools \
    linux-headers-$(uname -r) \
    lm-sensors \
    cpufrequtils \
    vmtouch \
    wmctrl xdotool \
    python3-pip
pip install psutil bcc prometheus_client pyyaml click rich
sudo sensors-detect --auto
```

### systemd user service

`~/.config/systemd/user/ghatak-ryzen.service`:
```ini
[Unit]
Description=ghatak-ryzen — CPU judgment daemon
After=graphical-session.target

[Service]
Type=simple
ExecStart=/home/aditya/.local/bin/ghatak-ryzen run
Restart=on-failure
RestartSec=5
Nice=19
CPUAffinity=15
IOSchedulingClass=idle

[Install]
WantedBy=default.target
```

User-level service — no root needed for most actions. Destructive kernel actions (cpufreq, IRQ) need a small setuid helper or polkit policy.

### Installation flow

```
./scripts/install.sh
  ├── checks dependencies
  ├── installs user-level python package (pipx)
  ├── writes default config.yml
  ├── installs systemd user unit
  ├── installs polkit policy for cpufreq-set
  ├── creates db paths
  └── runs 60-second self-test in dry-run mode, shows first decisions
```

---

## 14. Roadmap

See [`ROADMAP.md`](ROADMAP.md) for phased delivery with success criteria.

---

## 15. Non-goals (explicitly)

- **Not a kernel patch.** Userspace only. No module, no custom scheduler, no CONFIG change. Every action uses existing syscalls/sysfs/procfs.
- **Not a replacement for `nice`/`cgroups`**. Complementary — uses them as actuators.
- **Not a container orchestrator.** Single-machine, single-user focus.
- **Not a benchmark-chaser.** Value = real empire work delivered, not synthetic numbers.
- **Not cross-platform.** Linux only, Zen 2+ primary target.
- **Not a local-LLM wrapper.** Orthogonal. This makes *any* use of the machine more efficient, AI or not.

---

## 16. Inspiration & prior art

- `nohang` — OOM prevention daemon (similar shape, different purpose)
- `ananicy` — auto-nice daemon (subset of what we do)
- `uresourced` — systemd's user session resource management (kernel-side equivalent)
- macOS Activity Monitor's "Energy Impact" — the value-score idea is in this spirit
- Claude's own tool-use transcripts — the audit-log format is modeled on them

What's new here: **outcome-based decisions + per-process reasoning + distillation from Claude + CCD-native + empire-integrated**.

---

*Blueprint v1 — 2026-04-21. Authored in collaboration with Claude Opus 4.7.*

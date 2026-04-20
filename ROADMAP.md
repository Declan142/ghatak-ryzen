# ghatak-ryzen — Roadmap

Phased delivery. Each phase = merge-to-main + working on the Ryzen 7 3700X dev box before next starts.

---

## Phase 1 — MVP: rules-based judgment (1 day)

**Goal:** Zombies die. Cache-hot workloads stay put. Night quiet mode works.

- [ ] Project skeleton, CLI (`ghatak-ryzen {run,status,log,undo,panic}`)
- [ ] Sensor: `/proc` scanner (1 Hz)
- [ ] Sensor: `lm-sensors` integration (post `sensors-detect`)
- [ ] SQLite timeline — `processes_1s` table on tmpfs
- [ ] Decision log file writer
- [ ] **Bloat Judge** module
  - [ ] value score compute
  - [ ] SUSPEND + SIGCONT loop
  - [ ] dry-run mode
  - [ ] whitelist config
  - [ ] undo command
- [ ] **CCD Conductor** module (static pin rules only, no perf-event learning yet)
  - [ ] `ffmpeg` → CCD0 pin
  - [ ] `monitor.py` → CCD1 exclusive during trading hours
  - [ ] `claude` → CCD1
- [ ] **Governor** module (trading lock + night quiet only, not full per-workload)
- [ ] systemd user unit
- [ ] polkit policy for cpufreq-set
- [ ] install.sh

**Success criteria:**
- Running 1 week without user-visible regression
- ≥3 zombie processes killed (observed from decisions.log)
- ffmpeg pipeline sees <1% cross-CCD cache-refs (measured via `perf stat`)
- Daemon itself <1% CPU averaged, <150MB RSS
- `ghatak-ryzen panic` fully reverts state in <3 seconds

---

## Phase 2 — Observability (1 day)

**Goal:** See the CPU's thought process at a glance.

- [ ] `ghatak-ryzen status` — rich TUI showing current pinnings, suspensions, active governor per core
- [ ] Prometheus metrics endpoint :9733
- [ ] Statusline integration (`~/.claude/statusline.py` patch) — one-liner panel
- [ ] Telegram bot commands: `/ryzen status`, `/ryzen digest`
- [ ] Decision log structured format (JSONL alongside the pretty log)
- [ ] Weekly digest script: email + Telegram

**Success criteria:**
- Can answer "what's my CPU doing right now" in 1 second, without ssh / htop
- Weekly Telegram digest shows decisions count + harvested seconds + any issues

---

## Phase 3 — Session Predictor (3 days)

**Goal:** CPU anticipates. Page cache hot when you need it.

- [ ] Pattern mining from `processes_1s` — finds {weekday, hour} → {command, cwd} correlations
- [ ] `patterns` table + config persistence
- [ ] `vmtouch` wrapper for prefetch action
- [ ] Manual pattern add/remove CLI
- [ ] 30-day learning window
- [ ] Pattern confidence decay (unused patterns expire)

**Success criteria:**
- Top 5 daily patterns auto-detected within 2 weeks of install
- Measurable: `time python3 monitor.py` on cold vs warm cache shows 50+ ms improvement
- Zero false-positive prefetches (never prefetches something the user never opens)

---

## Phase 4 — Idle Harvester (2 days)

**Goal:** CPU never truly idles. Empire work fills spare cycles.

- [ ] Idle detector (cpu % + user X11 idle time)
- [ ] Job queue with priority + schedule rules
- [ ] SCHED_IDLE + ionice enforcement
- [ ] Foreground preemption (harvested job aborts when fg CPU >30%)
- [ ] Integration: embed-new-conversations, retrain-ghatak, factory-qa-sweep, dream skill
- [ ] Weekly digest: "this week harvested X CPU-hours → Y jobs"

**Success criteria:**
- Daily: ≥15 min of spare CPU harvested for empire work
- Zero observed interference with foreground tasks
- All 5 default harvest jobs wired and running

---

## Phase 5 — Distillation Loop (ongoing)

**Goal:** Claude teaches the conductor. Conductor gets smarter weekly without code changes.

- [ ] `outcomes` table population (user undo actions auto-labeled as false positives)
- [ ] Weekly Claude review: "Here's 100 decisions from last week — label which were wrong"
- [ ] DistilBERT or sklearn model for bloat classification (replaces hardcoded rules)
- [ ] Model versioning, A/B testing (shadow mode: new model suggests, rules still act)
- [ ] Metrics: precision/recall over time
- [ ] Automatic rollout when shadow model beats rules for 2 weeks

**Success criteria:**
- Shadow mode running by week 4
- Automatic rollout to primary by week 8, if accuracy improvement observed
- False-positive suspend rate drops to <2%

---

## Future / stretch

- [ ] Zen 3 / Zen 4 support (single CCD variants, different cache topology)
- [ ] Multi-user support (for shared dev boxes — not needed for current)
- [ ] GPU co-scheduling (when ffmpeg uses NVDEC + tensorflow wants same GPU, arbitrate)
- [ ] Nix package
- [ ] AUR package
- [ ] Kernel module exploration (scx_bpf sched_ext) — only if userspace hits ceiling
- [ ] NetBSD / FreeBSD port — academic curiosity

---

*Living document. Strike-through completed items, don't delete.*

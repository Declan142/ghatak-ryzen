# Blueprint — Phase 1 Smoke + Ship

**For:** off-peak (post-00:00 IST) execution. Sonnet/Haiku-safe if all 10 Python files under `src/ghatak_ryzen/` exist.
**Max runtime:** 10 minutes human + agent. No design decisions required.
**Repo:** `~/ghatak-ryzen` on `main`, untracked changes.

---

## Goal

Take Phase-1 code (already written, 1339 LOC) from **untracked** → **installed, smoke-tested, committed, pushed**.

## DO NOT

- DO NOT flip `dry_run` to false in config during smoke. Stays `true`.
- DO NOT enable `systemctl --user enable --now ghatak-ryzen`. Smoke only via CLI `dryrun`.
- DO NOT rewrite any existing file unless smoke surfaces a bug that blocks `dryrun --duration 30` from completing without Python exception.
- DO NOT `git push --force`. Regular push only.
- DO NOT touch `ghatak-monitor-ws.service` or `factory-bot.service`.
- DO NOT modify `~/.config/systemd/user/` or run `systemctl daemon-reload`.
- DO NOT change governor, never call `pkexec`, no sudo.

## Step-by-step

```bash
cd ~/ghatak-ryzen
```

### 1. Install

```bash
pipx install -e . --force
# → `ghatak-ryzen` in ~/.local/bin
```

If `pipx` missing: `sudo apt install -y pipx && pipx ensurepath`.
If build fails: copy the error verbatim to Aditya, stop.

### 2. Topology sanity

```bash
ghatak-ryzen topology
```

**Expected output:**
```
CCD0: cpus=[0, 1, 2, 3, 8, 9, 10, 11]
CCD1: cpus=[4, 5, 6, 7, 12, 13, 14, 15]
daemon will pin to cpu15
```

If output differs (fewer CCDs / wrong cpus), stop and report.

### 3. 30-second dry-run

```bash
ghatak-ryzen dryrun --duration 30
```

**Expected:**
- Runs 30s, no traceback.
- Prints a decisions table at the end with ≥1 row (likely `PIN_CCD_DRY` for Chrome/ffmpeg/claude if any match).
- ZERO `SUSPEND` actions (no real zombie in 30s window).

**If Python exception:** read the traceback, fix the *one* file it points to, re-run. Max 2 fix iterations; if still failing after 2, stop and dump the traceback.

### 4. Status + log

```bash
ghatak-ryzen status
ghatak-ryzen log -n 20
```

Status should show topology + governor table + any active (non-reverted) decisions.
Log file at `~/.local/share/ghatak-ryzen/decisions.log` should exist with ≥1 entry.

### 5. 60-second dry-run after copying config

```bash
mkdir -p ~/.config/ghatak-ryzen
cp config/config.example.yml ~/.config/ghatak-ryzen/config.yml
ghatak-ryzen dryrun --duration 60
```

Same expectations as 30s run, just more samples.

### 6. Git commit + push (only if steps 1-5 all green)

```bash
git status
git add -A
git diff --stat --staged     # verify sane file list
git commit -m "$(cat <<'EOF'
Phase 1 — bloat + CCD + governor judges, daemon loop, CLI

10 Python modules / 1339 LOC. dry_run=true default.
- Bloat Judge: SIGSTOP on CPU>20% + value_score=0 for 10min (no-kill phase 1)
- CCD Conductor: static taskset pin to CCD (ffmpeg->CCD0, claude/monitor.py->CCD1)
- Governor: trading-lock + night-quiet decisions logged; actuation phase-2 polkit
- CLI: run status log undo panic dryrun config topology
- Storage: SQLite tmpfs + pretty log + jsonl mirror
- Systemd user unit present; install script does NOT enable.

Dry-run smoke test green on Ryzen 7 3700X / Ubuntu 24.04 / kernel 6.17.

Co-Authored-By: Claude <noreply@anthropic.com>
EOF
)"
git push origin main
```

### 7. Final atlas note

Edit `~/.claude/atlas/projects/active/ghatak-ryzen.md`:
- Change `status:` line to `phase-1-live-dryrun-green`.
- Replace the "Paused for off-peak" paragraph with: `Phase 1 merged to main @ <new commit sha>. Ready for 24h dry-run soak, then flip dry_run=false.`

---

## If anything weird happens

- **`pkexec` or `sudo` wants password:** something went wrong, stop.
- **Actuator attempted despite dry_run:** BUG. Stop. Copy the decision log line + the code path. Leave rest untouched.
- **`/dev/shm/ghatak-ryzen.db` not created:** check `/dev/shm` is writable (`ls -la /dev/shm`).
- **Any SUSPEND action in dry-run window:** that's a finding — log pid/comm + full reason, don't touch the target PID.

## Success criteria

- [ ] `pipx install -e . --force` clean
- [ ] `ghatak-ryzen topology` shows 2 CCDs
- [ ] 30s + 60s dryrun both complete without traceback
- [ ] Commit on main, pushed to origin
- [ ] Atlas project file updated with new commit sha

Time budget: 10 min total.

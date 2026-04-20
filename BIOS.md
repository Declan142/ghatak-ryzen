# BIOS tuning — Ryzen 7 3700X

Software (ghatak-ryzen daemon) ceiling is the hardware it runs on. Current BIOS state on this machine is leaving **15-25% of the chip's perf on the table**. Fix once, get the lift across everything — daemon included.

> **Motherboard assumption:** AMD B550 / X570 / X470 (MSI / ASUS / Gigabyte / ASRock). Menu paths below use common names — yours may differ slightly. **Read carefully, one change at a time, reboot between each.**

> **Always:** before entering BIOS, save current state (`dmidecode > ~/bios-before.txt`). If a change won't POST, clear CMOS (jumper or battery pull, 30 seconds) to restore defaults.

---

## Current baseline (read before changing)

| Metric | Now | Potential | How to measure (Linux) |
|---|---|---|---|
| RAM speed | DDR4-2133 | DDR4-3000 (mixed-stick ceiling) or 3200 (matched tuning) | `sudo dmidecode -t memory \| grep -i speed` |
| Infinity Fabric (FCLK) | 1066 MHz (RAM/2) | 1500 or 1600 MHz | `sudo ryzen_monitor` or `sensors` |
| All-core boost | ~4.0 GHz | 4.2-4.3 with PBO | `watch -n1 'grep MHz /proc/cpuinfo'` |
| Single-core boost | 4.4 GHz | 4.5-4.6 with PBO CO | `cpufreq-info \| grep 'current CPU freq'` |
| Thermal (idle) | unknown (no `sensors` readout) | Tctl <45°C | fix `sensors` first — `sudo apt install lm-sensors && sudo sensors-detect` |
| Thermal (load) | unknown | Tctl <85°C target | `sensors` during stress-ng run |

**Fix `sensors` first** — you're flying blind on thermals. Takes 5 minutes:
```bash
sudo apt install lm-sensors
sudo sensors-detect --auto
sensors  # should now show k10temp: Tctl, Tccd1, Tccd2
```

---

## Priority order (biggest wins first)

### 🔥 #1 — Fix RAM speed (DDR4-2133 → DDR4-3000 or 3200)

**Expected gain:** 10-15% in memory-bound work (ffmpeg, compile, anything touching a big working set), reduces cross-CCD latency because Infinity Fabric clock = RAM clock / 2.

**Your sticks (from `dmidecode`):**
- Stick A: `CMK16GX4M1E3200C16` → Corsair Vengeance, rated DDR4-3200 CL16
- Stick B: `CMK16GX4M1D3000C16` → Corsair Vengeance, rated DDR4-3000 CL16

**Mixed kits** — always default to slower stick's rating as the safe target. Can push higher with manual tuning but it's a binary gamble.

#### Step 1 — Try DOCP/XMP (the one-click attempt)

1. Enter BIOS (Del or F2 at POST)
2. Go to **AI Tweaker** (ASUS) / **OC** (MSI) / **M.I.T.** (Gigabyte) / **OC Tweaker** (ASRock)
3. Find **DOCP / A-XMP / XMP** (same feature, different vendor names) — **enable it**
4. If it presents multiple profiles (DOCP Profile 1 / 2), pick **Profile 1**
5. **Save & Exit** (F10), reboot

Verify in Linux:
```bash
sudo dmidecode -t memory | grep -i 'configured memory speed'
# Want to see: 3000 MT/s or 3200 MT/s
```

**If it doesn't POST** (3 failed boots → auto-revert to JEDEC): you've hit the mixed-kit ceiling. Go to Step 2.

#### Step 2 — Manual tuning (safe-mid ground)

Enter BIOS → **DRAM Frequency** → set manually:

| Setting | Value | Why |
|---|---|---|
| DRAM Frequency | **DDR4-3000** (MCLK 1500 MHz) | Slower stick's rated speed, near-guaranteed stable |
| Memory Voltage (VDIMM) | **1.35 V** | Both sticks rated for this |
| SOC Voltage | **1.10 V** | Stock-safe for IF=1500 |
| Command Rate | 1T | |
| Timings | leave AUTO for now | Can tune later, diminishing returns |
| Infinity Fabric (FCLK) | **1500 MHz** | Must match MCLK (MCLK = 1500, FCLK = 1500) — keeps 1:1 ratio |
| UCLK | Auto (= MCLK) | Do not decouple |

**1:1 ratio is critical on Zen 2** — if FCLK ≠ MCLK, you get async-clock penalty. At DDR4-3000, FCLK=1500 = 1:1. At DDR4-3200, FCLK=1600 = 1:1. Anything above DDR4-3600 on Zen 2 hits FCLK wall and decouples — **don't** push above 3600.

Save & Exit. Reboot. If stable:
```bash
# run 10 min memory stress test
sudo apt install stress-ng
stress-ng --vm 8 --vm-bytes 80% --timeout 600s --metrics-brief
# also good: memtester 28G 3
```

If stable for 10 minutes memtest + 1 hr normal use → keep.

#### Step 3 — Push to DDR4-3200 (if Step 2 was stable)

Only attempt if DDR4-3000 ran clean for 24+ hours.

- DRAM Frequency: **DDR4-3200**
- VDIMM: **1.35 V** (still)
- FCLK: **1600 MHz**
- VSOC: **1.10-1.15 V** (bump if POST fails)

Expected: 50% of mixed kits will post at DDR4-3200, 50% won't. If it fails, fall back to DDR4-3000. Not worth pushing further with a mixed kit.

#### If you want to chase more

- Matching kit (buy 2×16GB DDR4-3600 CL16 matched) = ~₹8,000, unlocks FCLK=1800 ≈ another 5-8% improvement. Only worth it if you're going to do this work long-term. **Don't bother right now** — current sticks → DDR4-3000 is 90% of the wins.

---

### 🔥 #2 — Enable PBO (Precision Boost Overdrive)

**Expected gain:** +100-200 MHz all-core under load, +50-100 MHz single-core. No stability risk if kept stock PBO (Auto) — AMD built guardrails in.

1. BIOS → **Advanced** / **AMD Overclocking** / **AMD CBS**
2. Find **Precision Boost Overdrive** — set to **Enabled** (or **Advanced** if you want to tune further)
3. **PBO Limits** → **Auto** (board decides based on cooler — safe) **OR** **Motherboard** (uses board VRM limits, usually more aggressive)
4. **PBO Scalar** → **Auto** or **2X** (higher = sustains boost longer at higher voltage; thermal-bound)
5. **Max CPU Boost Clock Override** → **+50 MHz** (conservative) or **+100 MHz** (aggressive, validate thermals)
6. Save & Exit.

After reboot, verify:
```bash
# run a single-threaded benchmark, watch freq climb
for i in {1..10}; do
  cat /sys/devices/system/cpu/cpu*/cpufreq/scaling_cur_freq | sort -rn | head -3
  sleep 1
done
# should see at least one core hitting 4450+ MHz
```

If under load for 30 min `sensors` shows Tctl >90°C → PBO is pulling too hard on thermals → reduce Max Boost Override or improve cooling.

---

### 🎯 #3 — Curve Optimizer (undervolt per core)

**Expected gain:** -5 to -10°C at same freq → PBO can sustain higher boost longer → 3-5% more sustained perf. **This is the real chef's kiss.**

BIOS → **AMD Overclocking** → **Precision Boost Overdrive** → **Curve Optimizer**

- **Curve Optimizer Mode:** **Per Core**
- Apply negative offsets per core. Safe starting values for most 3700X chips:

| Core | Offset | Note |
|---|---|---|
| Core 0 | **-10** | usually the best-binned, "preferred core" |
| Core 1 | **-10** | second-best |
| Cores 2-7 | **-20** | start here for the rest |

Save, reboot. **Test for 2 hours:**
```bash
# stress test all cores
stress-ng --cpu 16 --timeout 7200s --metrics-brief
# in another terminal, watch for WHEA errors (instability sign):
sudo dmesg -w | grep -i 'WHEA\|mce\|hardware error'
```

If **no WHEA errors + no crashes** in 2 hours → bump all offsets by **-5** (i.e. Core 0/1 → -15, others → -25), repeat. Until you hit a core that crashes → back that core off by 5, leave others.

**Don't go below -30 per core** — that's the usual Zen 2 floor. Some chips top out at -20.

**If BIOS is still running on older Ryzen microcode and doesn't offer Curve Optimizer**: update BIOS first. Boards from 2020+ all have it; boards from 2019 may need a firmware update via Flashback.

---

### ⚡ #4 — Eco Mode (optional — trades 5% perf for 30% less power + heat)

Use case: you don't need peak perf, you want silent fans and lower summer temps.

BIOS → **AMD CBS** → **CPU Power** → **cTDP** → set to **45W** (instead of stock 65W)

Or in BIOS, look for **ECO Mode** toggle → Enabled.

**Don't stack Eco Mode + PBO.** Pick one. Eco cuts sustained boost, PBO pushes it. Contradictory.

**My recommendation for you:** PBO + Curve Optimizer, skip Eco. You want perf during trading + iphonecam, fans matter less.

---

### 🧊 #5 — Fan curves (if not already sane)

BIOS → **Hardware Monitor** / **Fan Control** / **Q-Fan Control**

Set fan curves to:

| CPU temp | Fan RPM % |
|---|---|
| <45°C | 30% (silent) |
| 45-60°C | 40-50% |
| 60-75°C | 60-75% |
| 75-85°C | 85% |
| >85°C | 100% |

Lower ramp at idle means silence. Higher ramp above 75°C means PBO gets thermal headroom to sustain boost.

---

## Lower-priority tweaks (nice-to-have)

### Global C-States — **keep ENABLED**
- Don't disable C-states. Old forum advice says "disable for stability" — this was a Zen 1 quirk, **fixed since AGESA 1.0.0.4**. On Zen 2+, keep **Global C-states = Auto/Enabled**. Disabling loses boost headroom.

### SMT (Hyperthreading) — **keep ENABLED**
- 16 threads vs 8 is a huge win for compile, ffmpeg, parallel anything. Don't disable.

### Re-BAR / SAM — **optional, enable if your GPU supports it**
- GTX 1660 supports Resizable BAR via recent driver. Marginal improvement in some games — not relevant for your workload. Leave at default.

### CSM / UEFI — **UEFI only**
- If anything is set to CSM / Legacy boot → switch to UEFI. Faster boot, needed for modern kernels.

### Virtualization (SVM) — **ENABLE**
- Needed for KVM, Docker (if ever), WSL (not on Linux but still fine to have).

### Power Supply Idle Control — **Typical Current**
- Some boards offer "Low Current" — disables C6. **Keep at Typical Current** (default).

---

## After-BIOS verification checklist

After each BIOS change, boot to Linux and run:

```bash
# 1. RAM speed
sudo dmidecode -t memory | grep -i 'configured memory speed'
#    want 3000 or 3200 MT/s

# 2. CPU detected correctly
grep "model name" /proc/cpuinfo | head -1
#    want: AMD Ryzen 7 3700X 8-Core Processor

# 3. All 16 threads online
nproc
#    want: 16

# 4. Boost freq ceiling
cat /sys/devices/system/cpu/cpu*/cpufreq/cpuinfo_max_freq | sort -rn | head -1
#    want: 4426171 (= 4.426 GHz, stock max) or higher with PBO +50/+100

# 5. Infinity Fabric
sudo ryzen_monitor 2>/dev/null | grep -i 'FCLK\|Fabric'
#    or via cpu-z-like tool
#    want: 1500 or 1600 MHz

# 6. Temps (after sensors-detect setup)
sensors | grep -E 'Tctl|Tccd'
#    idle: <45°C; under stress-ng 5 min: <85°C

# 7. No hardware errors since boot
sudo dmesg | grep -i 'mce\|WHEA\|hardware error'
#    want: no output

# 8. Memtest 1 hour
sudo apt install memtester
sudo memtester 28G 1     # leave some RAM for OS
#    want: all pass
```

---

## Known gotchas

- **After enabling XMP/DOCP**, some BIOSes reset SATA config → re-check if drives present in Linux
- **Windows dual-boot**: if you share the machine with Windows, Windows may not like memtest sectors → boot to Windows once after memory change, let it validate
- **First boot after CMOS clear** can take 30-60s while BIOS trains memory → not a hang
- **Kernel 6.17 + amd_pstate**: ghatak-ryzen Phase 1 will switch to `amd_pstate=guided` at install time — independent of BIOS. BIOS governs hardware, `amd_pstate` governs how Linux talks to it.
- **DDR4-3600 chase**: don't. On mixed sticks, Zen 2 3700X, you'll hit stability walls. DDR4-3000 @ 1:1 is better than DDR4-3600 @ 2:1.

---

## If you brick it

- Boot fails → CMOS reset (jumper CLR_CMOS or pull battery for 30s)
- Boots to BIOS but not OS → loosen whatever you changed last, try again
- Memory training loops forever (boot LED blinks red/amber repeatedly) → memory timing too tight → CMOS reset
- **Any three consecutive failed POSTs** → most boards auto-revert RAM to JEDEC 2133 → you're back to start, no permanent harm

Keep a **working config screenshot** in your phone before changing anything important.

---

## Recommended change order (one per session)

| Session | Change | Time | Risk |
|---|---|---|---|
| 1 | Install `lm-sensors`, run `sensors-detect` (Linux side, no reboot) | 5 min | Zero |
| 2 | Enable DOCP/XMP Profile 1 (Step 1 above) | 10 min | Low (auto-revert if fails) |
| 3 | If XMP fails → Manual DDR4-3000 @ 1:1 | 20 min | Low |
| 4 | Enable PBO (Auto limits, +50 boost) | 10 min | Low |
| 5 | Curve Optimizer -10/-20, stress test 2 hr | 3 hr (mostly passive) | Medium — can crash if too aggressive |
| 6 | Tune Curve Optimizer per core | iterative | Medium |
| 7 | Fan curves | 5 min | Zero |

Total: one afternoon of focus, 15-30% real-world perf recovered.

---

*All BIOS values above are for Ryzen 7 3700X on a typical B550/X570 board circa 2020-2024 firmware. Update BIOS to the latest AGESA before attempting Curve Optimizer (older AGESAs don't expose it).*

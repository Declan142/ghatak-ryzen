#!/usr/bin/env bash
# ghatak-ryzen — install flow for Ubuntu 22.04+ / Debian 12+
# Idempotent.  Safe to re-run.

set -euo pipefail

HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &> /dev/null && pwd)"
ROOT="$(cd -- "$HERE/.." &> /dev/null && pwd)"

echo "==> ghatak-ryzen install starting"
echo "    repo: $ROOT"

# 1. Python deps
if ! command -v pipx >/dev/null 2>&1; then
    echo "==> pipx missing — run: sudo apt install pipx"
    exit 1
fi

echo "==> installing python package via pipx (editable)"
pipx install -e "$ROOT" --force

# 2. Config
CFG_DIR="$HOME/.config/ghatak-ryzen"
mkdir -p "$CFG_DIR"
if [[ ! -f "$CFG_DIR/config.yml" ]]; then
    cp "$ROOT/config/config.example.yml" "$CFG_DIR/config.yml"
    echo "==> wrote default config to $CFG_DIR/config.yml"
else
    echo "==> config.yml already present, leaving untouched"
fi

# 3. State paths
mkdir -p "$HOME/.local/share/ghatak-ryzen"

# 4. systemd user unit
UNIT_SRC="$ROOT/systemd/ghatak-ryzen.service"
UNIT_DST_DIR="$HOME/.config/systemd/user"
mkdir -p "$UNIT_DST_DIR"
cp "$UNIT_SRC" "$UNIT_DST_DIR/ghatak-ryzen.service"
systemctl --user daemon-reload

# 5. Smoke test — 10s dry-run
echo "==> running 10s dry-run smoke test"
"$HOME/.local/bin/ghatak-ryzen" dryrun --duration 10

echo
echo "==> install complete."
echo "    config: $CFG_DIR/config.yml (dry_run=true by default)"
echo "    log:    $HOME/.local/share/ghatak-ryzen/decisions.log"
echo
echo "Next steps:"
echo "    ghatak-ryzen topology           # verify CCD detection"
echo "    ghatak-ryzen dryrun             # 60s dry-run, see decisions"
echo "    ghatak-ryzen status             # current state"
echo
echo "When ready to go live:"
echo "    sed -i 's/dry_run: true/dry_run: false/' $CFG_DIR/config.yml"
echo "    systemctl --user enable --now ghatak-ryzen"
echo "    systemctl --user status ghatak-ryzen"

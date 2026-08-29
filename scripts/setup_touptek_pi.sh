#!/usr/bin/env bash
# setup_touptek_pi.sh — Install toupcam.py and libtoupcam.so into the venv.
#
# Mirrors the sibling SmartTScope project's scripts/setup_touptek_pi.sh —
# same two-file recipe, proven working on this same Pi hardware (see
# issue #12's resolution).
#
# toupcam.py is committed at resources/touptek/toupcam.py. libtoupcam.so
# is a large proprietary binary and is NOT committed to git — this script
# looks for it in a few likely places and tells you where to put it if it
# can't find one:
#
#   1. This repo's root (drop it there: cp /path/to/libtoupcam.so .)
#   2. This script's own directory
#   3. An already-extracted ToupTek SDK under ~/touptek/ (as of writing,
#      this Pi has one at ~/touptek/toupcamsdk_*/linux/arm64/glibc/)
#   4. Another local project's venv that already has a working copy
#      (e.g. ~/astro_sw/SmartTScope/.venv) — reuses it instead of asking
#      you to re-download the SDK.
#
# After this script completes, restart CollimationTool/GuideTool.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

GREEN='\033[0;32m'; CYAN='\033[0;36m'; RED='\033[0;31m'; NC='\033[0m'
ok()   { echo -e "${GREEN}[+]${NC} $*"; }
info() { echo -e "${CYAN}[·]${NC} $*"; }
err()  { echo -e "${RED}[✗]${NC} $*" >&2; exit 1; }

# ── locate venv ───────────────────────────────────────────────────────────────
VENV_DIR="$REPO_ROOT/.venv"
[[ -x "$VENV_DIR/bin/python" ]] \
    || err "No venv at $VENV_DIR — run: python3 -m venv .venv && .venv/bin/pip install -e ."

PYTHON="$VENV_DIR/bin/python"
SITE_PACKAGES="$("$PYTHON" -c "import sysconfig; print(sysconfig.get_path('purelib'))")"
info "Venv site-packages: $SITE_PACKAGES"

# ── copy toupcam.py ───────────────────────────────────────────────────────────
TOUPCAM_SRC="$REPO_ROOT/resources/touptek/toupcam.py"
[[ -f "$TOUPCAM_SRC" ]] || err "toupcam.py not found at $TOUPCAM_SRC"

cp "$TOUPCAM_SRC" "$SITE_PACKAGES/toupcam.py"
ok "Installed toupcam.py -> $SITE_PACKAGES/toupcam.py"

# ── locate libtoupcam.so ──────────────────────────────────────────────────────
LIBSO=""
for candidate in \
    "$REPO_ROOT/libtoupcam.so" \
    "$SCRIPT_DIR/libtoupcam.so" \
    "$HOME"/touptek/toupcamsdk_*/linux/arm64/glibc/libtoupcam.so \
    "$HOME/astro_sw/SmartTScope/.venv/lib/python3."*/site-packages/libtoupcam.so \
; do
    if [[ -f "$candidate" ]]; then
        LIBSO="$candidate"
        break
    fi
done

if [[ -z "$LIBSO" ]]; then
    echo ""
    echo -e "${RED}[✗]${NC} libtoupcam.so not found in any known location."
    echo "    Download the ARM64 Linux SDK from the ToupTek website,"
    echo "    extract it, and copy libtoupcam.so to the repo root:"
    echo ""
    echo "      cp /path/to/sdk/linux/arm64/glibc/libtoupcam.so $REPO_ROOT/"
    echo ""
    echo "    Then re-run this script."
    exit 1
fi

info "Using libtoupcam.so from: $LIBSO"
cp "$LIBSO" "$SITE_PACKAGES/libtoupcam.so"
ok "Installed libtoupcam.so -> $SITE_PACKAGES/libtoupcam.so"

# ── verify import ─────────────────────────────────────────────────────────────
if "$PYTHON" -c "
from astrotool_core.camera import list_devices
devices = list_devices()
print(f'  {len(devices)} camera(s) enumerated')
for d in devices:
    print(f'    - {d.display_name} ({d.camera_id})')
"; then
    ok "import toupcam / list_devices() succeeded"
else
    err "import failed — libtoupcam.so may be the wrong architecture (need linux/arm64/glibc)."
fi

echo ""
ok "ToupTek SDK ready. Restart CollimationTool/GuideTool to apply."

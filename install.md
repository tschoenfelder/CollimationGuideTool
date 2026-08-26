# Installing and upgrading on a Raspberry Pi

Target environment: Raspberry Pi 5, Raspberry Pi OS (Debian 13 "Trixie",
64-bit), which ships Python 3.13 — matching this project's
`requires-python = ">=3.13"`. Commands below assume a normal desktop
install (not Lite) since both apps are PySide6 GUIs.

## Current status

`git clone` + `pip install -e .` already work today and give you
`astrotool_core`, `CollimationController`, `GuideController`, etc. for
scripting/testing. The `collimation-tool` / `guide-tool` desktop apps
themselves (Stage 7 of `PLAN.md`) aren't built yet, so the console
scripts and desktop menu entries described below aren't runnable yet —
this doc describes the target install once that stage lands, so it
won't need rewriting later. The directory/venv/upgrade mechanics are
already accurate today.

## One-time setup

### 1. System packages

Raspberry Pi OS's desktop image normally has everything PySide6 needs.
The one common gap:

```bash
sudo apt update
sudo apt install libxcb-cursor0
```

(If a Qt app fails to start with `Could not load the Qt platform
plugin "xcb"`, this package is what's missing.)

### 2. Clone the repository

```bash
mkdir -p ~/astro_sw
cd ~/astro_sw
git clone https://github.com/tschoenfelder/CollimationGuideTool.git
cd CollimationGuideTool
```

### 3. Create the virtual environment and install

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -e .
```

If you'll also be developing/testing on the Pi itself (not just running
the apps), install the dev extras instead:

```bash
pip install -e ".[dev]"
```

This pulls in the two runtime dependencies from their published sources
(`onstep-adapter` from a GitHub release wheel, `smarttscope-live-analysis`
from a tagged commit) — both need network access on first install.

The ToupTek camera SDK is not yet vendored into this project (see the
`touptek` extra's comment in `pyproject.toml`); until that lands, camera
capture is limited to `FakeCamera`/`ReplayCamera`.

### 4. Configuration

Hardware/session configuration lives outside the repo at
`~/.CollimationGuideTool/config.toml`, so it survives the `git reset
--hard` upgrades below untouched:

```bash
mkdir -p ~/.CollimationGuideTool
# create/edit ~/.CollimationGuideTool/config.toml with your camera/mount settings
```

(The config loader itself is planned but not yet implemented — see
`PLAN.md`. This is the path it will read from once it exists.)

### 5. Desktop menu entries

Once Stage 7 ships the two console scripts, add a `.desktop` launcher
for each so they show up as separate Pi menu entries, per the
architecture doc's "two menu entries, one shared core" design:

```bash
mkdir -p ~/.local/share/applications

cat > ~/.local/share/applications/collimation-tool.desktop <<'EOF'
[Desktop Entry]
Type=Application
Name=CollimationTool
Exec=/home/astro/astro_sw/CollimationGuideTool/.venv/bin/collimation-tool
Terminal=false
Categories=Science;
EOF

cat > ~/.local/share/applications/guide-tool.desktop <<'EOF'
[Desktop Entry]
Type=Application
Name=GuideTool
Exec=/home/astro/astro_sw/CollimationGuideTool/.venv/bin/guide-tool
Terminal=false
Categories=Science;
EOF
```

Adjust the `Exec` path if you didn't use `~/astro_sw/CollimationGuideTool`
or a different Pi user than `astro`.

## Upgrading

Both apps run directly from the cloned working tree via the editable
install, so an upgrade is just: update the code, and only reinstall if
dependencies changed.

```bash
cd ~/astro_sw/CollimationGuideTool
git fetch origin
git reset --hard origin/main   # or a specific release tag, e.g. origin/v0.1.0
source .venv/bin/activate
pip install -e .               # re-run only if pyproject.toml changed
```

`~/.CollimationGuideTool/config.toml` lives outside the repo and is
untouched by `git reset --hard`. Close and relaunch the apps from the
Pi menu (or `.venv/bin/collimation-tool` / `.venv/bin/guide-tool` from a
terminal) after upgrading — there is no background service to restart.

Before pushing a release from a dev machine, run the full release gate
(`scripts/check.sh --release` — see `CONTRIBUTING.md`) so the version
you're about to have the Pi pull has passed the acceptance-regression
suite, not just the fast per-change checks.

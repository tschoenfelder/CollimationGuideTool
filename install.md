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

The ToupTek SDK isn't pip-installable (no public wheel), so it needs one
extra step on each Pi:

```bash
bash scripts/setup_touptek_pi.sh
```

This copies the committed `resources/touptek/toupcam.py` plus a
`libtoupcam.so` into the venv's site-packages — see that script's header
for where it looks for `libtoupcam.so` (it's a large proprietary binary,
not committed to git) and how to point it at one if it can't find it
automatically. Until this step is done, the "Camera" dropdown in each
app's toolbar only ever offers "Demo camera (no hardware)" —
`touptek_adapter.list_devices()` returns an empty list whenever the SDK
can't be imported. Once it's set up, connected cameras appear in that
dropdown automatically (no config file needed to pick one); use "Connect"
to select and start streaming from one.

CollimationTool also has an "Auto exposure/gain" checkbox: while
streaming, it keeps the frame's brightest signal in the 50-70% ADU range
by adjusting exposure first, only raising gain above its 100 baseline if
exposure alone can't reach that band. Exposure is capped at a 2-second
live-view ceiling, not the camera's own (often much higher) hardware
maximum — a multi-second-per-frame exposure would defeat the point of a
*live* view, so gain takes over well before that.

CollimationTool shows two independent camera panels side by side — a
primary/collimation camera (left) and a guide camera (right) — so both
can be watched at once. Each has its own camera picker, Connect, exposure/
gain, auto-exposure, and collimation measurement; connecting a real
device on one side removes it from the other side's dropdown (a ToupTek
camera only allows one open connection at a time), though both sides can
independently use the demo camera. "Capture diagnostics" is shared
between the two panels, not duplicated.

The guide (right) panel draws a yellow rectangle showing where the main
camera's field of view falls within it. This needs the sibling
SmartTScope project's `~/.SmartTScope/config.toml` — read once at
startup as the master source for each optical train's plate scale (see
`[optical_trains.main]`/`[optical_trains.guide]`, `[telescopes]`); no
overlay is drawn if that file or the relevant train isn't found. Until
calibrated (see below), this rectangle is only centered and unrotated —
a placeholder derived purely from the config's plate scale, not a
measured alignment.

**Calibrate FOV**: click this button (with both streams running) to
replace that placeholder with a real, content-matched rectangle —
`fov_registration` locates the main camera's actual frame content within
the guide frame, allowing for rotation and a small scale correction
around the config's plate-scale ratio. Runs on a background thread so
the window stays responsive — a full search at real camera resolution
takes on the order of two real minutes (measured on the ATR585M/
GPCMOS02000KPA rig) — and reports a status message with the found
rotation/scale/score, or "no confident match" if it couldn't find one
(the previous overlay, if any, is left in place). A one-shot,
explicitly-triggered action — it does not re-run automatically per
frame, and reconnecting either camera clears a previous calibration (it
no longer matches the new frame content/size).

Color cameras (e.g. the GPCMOS02000KPA) now display their actual demosaiced
color in the live view, not the mono luma plane the collimation/donut
analysis uses internally — this was previously a bug ("guide cam is
color, but picture seems mono").

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

### Diagnostics

Both apps write local diagnostic bundles to
`~/.CollimationGuideTool/diagnostics/<uuid>/` — automatically on an
unhandled exception, or on demand via the **Capture diagnostics** button
in each toolbar. Nothing is uploaded; bundles older than 7 days (or
beyond the most recent 20) are pruned automatically. Reference the UUID
shown in the app or the logs when filing a bug report — see
CONTRIBUTING.md's "Diagnostic capture" section for the bundle format and
how to locate one locally.

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

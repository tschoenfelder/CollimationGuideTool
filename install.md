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
`TerrestrialRegistrar` (`astrotool_core.registration.terrestrial_registrar`)
locates the main camera's actual frame content within the guide frame,
allowing for rotation and a small scale correction around the config's
plate-scale ratio. Runs on a background thread so
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

CollimationTool now reads and writes this file itself for one thing —
each panel's connected camera plus its exposure/gain/auto-exposure/cooling-target-temperature
state (`[cameras.main]`/`[cameras.guide]`) — saved automatically on every
change and restored on the next launch, on the assumption that a rig's
cameras stay on the same USB ports between sessions; a saved camera no
longer plugged in just falls back to the demo camera. A broader config
loader (mount settings, other session state) is still planned but not
yet implemented — see `PLAN.md` — and can add its own tables to this
same file alongside `[cameras.*]` without conflict.

For a camera with a TEC cooler (detected automatically from the SDK's own
model report — no camera list to maintain), the panel shows the current
sensor temperature and, only for that camera, a cooling on/off toggle and
a target-temperature spinner. Cooling never remembers "on" across a
restart or a reconnect — it always starts off and must be switched on
again explicitly each session — but the target temperature you last set
is remembered like exposure/gain.

#### OnStep connection (mount + focuser)

The tool reaches the OnStep controller **only through OnStepAdapter**
(>= 0.4.0), which itself talks INDI (AGENTS.md: for this deployment,
indiserver is the sole owner of the OnStep serial port, and OnStepAdapter
uses the INDI-backed transport, so IndiMonitor and other INDI clients keep
receiving mount/focuser properties while this app runs). The focuser, the
park/unpark control and Mount Align's axis moves all share that one
connection. Connection identity comes from `[indi]` in
`~/.CollimationGuideTool/config.toml`; the observer site comes from the
SAME `~/.SmartTScope/config.toml` this tool already reads elsewhere
(`[observer] lat/lon/height_m`) -- never re-invented here:

```toml
[indi]
host = "127.0.0.1"             # default
port = 7624                    # default
device = "LX200 OnStep"        # default -- must match indiserver's driver
home_motion_enabled = false    # default -- see below
focuser_max_position = 20000   # optional; required for the focuser to move at all

[meridian]
# field-verify-pending defaults -- tune against the firmware's own
# "Minutes Past Meridian" readback before trusting the supervisor
flip_request_deg = 100.0       # default
tracking_stop_deg = 110.0      # default
flip_allowance_seconds = 120.0 # default
reserve_seconds = 30.0         # default
```

**Capability boundary as of this OnStepAdapter build** (not a temporary
bug — each item is either an explicit safety gate pending its own
supervised test, or genuinely not ported to INDI yet; each is tracked as
an OnStepAdapter enhancement request, per AGENTS.md, rather than worked
around locally):

- **`home_motion_enabled = false` (default)**: park/unpark/go-home all
  refuse until set `true` in `[indi]` — OnStepAdapter's own gate, pending
  its supervised HOME-status check. There is no more manual "Confirm at
  home" step (0.3.5's operator button is gone) — home authority is now
  established automatically from live status.
- **No tracking-enable over INDI**: `enable_tracking()` always raises;
  the Mount panel's tracking can be stopped (`stop_tracking`, which routes
  through emergency-stop) but not started programmatically yet.
- **No timed pulse, and a 720″ (0.2°) floor on axis moves**: Mount Align's
  RA/Dec motion goes through a finite, feedback-verified axis-degree move
  (a mini-GOTO, not a pulse) bounded to 720″–36000″ per move, requiring
  tracking already OFF, home authority established, and a fixed
  astronomical safety corridor. Requests below 720″ (most of this app's
  own historical calibration seeds) are refused with an explicit message,
  never silently rounded up.

Mount Align on this connection: a requested move under 720″ is refused
outright (see above); one at or above it is issued directly as a finite
axis-degree move — there is no more "timed bootstrap → measure the rate →
install it" step, since the new primitive needs no rate at all.

`indi_lx200_OnStep` must be **running** in indiserver for the OnStep
controller (this reverses 0.3.5's deployment, which required it stopped so
OnStepAdapter could own the serial port exclusively). After upgrading
OnStepAdapter, update the Pi's venv:
`.venv/bin/pip install <release wheel URL from pyproject.toml>`.

#### Filter wheel (which optical train it serves, and its filter names)

CollimationGuideTool reads which optical train the shared electronic filter
wheel (EFW) currently serves, and each slot's filter name, from the SAME
`~/.SmartTScope/config.toml` SmartTScope itself uses (`[filter_wheel]`'s
`enabled`/`active_camera_role`, and `[filters]`) -- never a separate,
CollimationGuideTool-invented copy of the same facts. This is a plain file
read: SmartTScope does not need to be running for it to work.

Where that file isn't present (e.g. a CollimationGuideTool-only install),
the SAME two tables in `~/.CollimationGuideTool/config.toml` are read
instead, extended with the EFW's INDI connection identity (SmartTScope talks
to this wheel over its own native SDK, never INDI, so it has no equivalent):

```toml
[filter_wheel]
enabled = true
active_camera_role = "main"    # which optical train the selector appears on
device = "ToupTek EFW 1"       # optional override; default is the rig's known device
host = "localhost"             # optional
port = 7624                    # optional

[filters]
luminance = 1
red       = 2
green     = 3
blue      = 4
ha        = 5
oiii      = 6
sii       = 7
```

If neither file has a `[filter_wheel]` table at all, CollimationGuideTool
falls back to this rig's own known-good state (`ToupTek EFW 1`, Main's
optical train, the 7 filters above). The slot selector only appears on a
real camera panel (Main/Guide) whose name matches `active_camera_role`; a
role naming anything else (e.g. a not-yet-implemented OAG panel) shows no
selector anywhere and logs a warning, rather than crashing or attaching to
the wrong panel. Noticing a changed `active_camera_role` requires restarting
the app, same as every other config this project reads at startup.

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

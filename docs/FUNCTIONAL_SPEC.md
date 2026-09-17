# Aithre Functional Specification

Status: **current-state capture**, drafted 2026-09-17. This describes what
`DiamondLightSource/aithre` (the real, actively-maintained I23 production
GUI — not to be confused with the separate, legacy `co2e14/aithre_lasercontrols`
personal repo) actually does today, on the `linux` branch. It is a
description, not a design document — nothing here has been changed as
part of writing it. Line references are to `bin/guiv4_prod.py` unless
stated otherwise, and will drift as the code changes; treat them as
pointers, not guarantees.

Intended use: a baseline for planning the hardware integrations coming
over the next few months (see "Open questions / extension points" at the
end, left for Armin to fill in with direction).

---

## 1. What Aithre is

Aithre is the operator-facing control GUI for the **laser shaping
system on Beamline I23** at Diamond Light Source. Operators use it to
position a sample (via stage/goniometer motion or by clicking in a live
camera feed), load/unload samples via a robot, and shape the sample with
a femtosecond laser (Carbide, via RTC6 galvo-mirror-directed cutting)
before it goes on to data collection elsewhere in the beamline.

It is a single-process **PyQt5 desktop application**
(`bin/guiv4_prod.py`, entry point, version string `"4.3.0"` at L141),
built from a Qt Designer `.ui` file compiled to `bin/gui_4_3_0.py`
(`Ui_MainWindow`). It talks to hardware/software almost entirely through
**EPICS Channel Access** (`caget`/`caput` subprocess calls via
`bin/control.py`), with three exceptions: the OAV video feed (plain
MJPEG/HTTP), the Carbide laser (direct REST, on this branch — see
§5.6), and RTC6 shape cutting (an in-process Python binding, not CA).

Two parallel control paths exist for several subsystems: a **default
"direct" path** (`ca.caput`/`ca.caget` against `bin/pv.py`'s PV list) and
an **optional Bluesky path** (`--bluesky`/`--blueapi` flags, driving
`dodal`/`mx_bluesky` plans instead). The direct path is what actually
runs day to day per the code's own comment ("Using dirty caput/get...",
L139); the Bluesky path is present but partial (see §4).

---

## 2. Execution modes and CLI flags

Parsed at the top of `guiv4_prod.py` (L9-23):

| Flag | Effect |
|---|---|
| `--dev` | Development mode: skips real EPICS calls used for feed sizing, RBV polling, and OAV setup callbacks; runs at reduced camera resolution. Intended for running the GUI outside the lab. |
| `--bluesky` | Diverts several actions (`jogSample`, `loadNextPin`, `autoCenter`, `returntozero`) through `mx_bluesky.beamlines.aithre_lasershaping` plans on a locally-created `RunEngine`, instead of direct `caput`/`caget`. Hard-`sys.exit(1)`s at startup if the `mx_bluesky`/`dodal`/`bluesky` imports fail (L134-137). |
| `--blueapi` | Additionally routes some actions (`jogSample`, `gonioRotate`) through a `BlueapiClient` REST worker (`bac.create_and_start_task(...)`) instead of (or alongside) the local RunEngine. Requires a config file at a hard-coded path, `/dls/science/groups/i23/aithre/config.yaml` (L109), prompting interactively for a path if it's missing (L111-113) — not viable non-interactively. |
| `--rtc6` | Acquires the RTC6 galvo board at startup (`cut_shapes.CutShapes()`, L413) and enables real cutting in `savePoints`. Off by default. Automatically forced off on Windows (L25-26) regardless of the flag, since the RTC6 driver is Linux-only. |
| `--beampos X,Y` | Overrides the default beam-position pixel coordinates (`1644,1232`, L149-150) used for the OAV crosshair/grid overlay and click-to-move calibration. |

Three independent control-path axes result from these flags: **direct
CA** (default) vs **Bluesky** (`--bluesky`) vs **BlueAPI** (`--blueapi`,
layered on top of `--bluesky`'s imports), and **RTC6 acquired** vs **not**
(`--rtc6`). Not every action respects every flag combination
consistently — e.g. `jogSample` checks `--blueapi` first, then falls
back through `bluesky_mode`, then direct CA (L669-735); `gonioRotate`
only distinguishes `--blueapi` vs. direct CA, with no separate
`bluesky_mode` branch (L963-975); `loadNextPin` only distinguishes
`bluesky_mode` vs. direct CA, with no `--blueapi` branch at all
(L590-606). This is worth keeping in mind when extending any of these
paths — the three flags are not applied uniformly across actions today.

---

## 3. Subsystems

### 3.1 On-axis viewing (OAV) camera stream

- `OAVThread` (L166-285), a `QThread` that opens an MJPEG stream via
  OpenCV (`cv.VideoCapture(OAVADDRESS)`, `OAVADDRESS =
  "http://bl23i-ea-serv-01.diamond.ac.uk:8080/OAV.mjpg.mjpg"`, L143),
  overlays a grid (spacing/color/width constants at L146-148) and a
  green crosshair at the configured beam position, optionally crops/
  zooms around the beam position, converts to `QImage`, and emits it via
  `ImageUpdate` for display in `MainWindow.setImage` (L917-925).
- Zoom level is set via `sliderZoom` → `handleZoom` (L635-644) →
  `zoomChanged` signal → `OAVThread.setZoomLevel` (L272-278).
- `setupOAV` (L884-903) disables a set of areaDetector plugin callbacks
  (ROI/ARR/STAT/PROC/FIMG/TIFF/HDF5) and sets the MJPG max width/height
  to 4024×3036 — done once at startup, not in dev mode.
- `oavStart`/`oavStop` (L905-915) just `caput` the camera's `Acquire`
  PV to `"Acquire"`/`"Done"`.
- `changeExposureGain` (L646-651) pushes the exposure/gain sliders to
  `oav_cam_acqtime`/`oav_cam_gain`.
- `saveSnapshot` (L927-961) writes the currently displayed frame to a
  user-chosen `.jpg` via OpenCV.
- Feed sizing (`feed_width`, `display_width/height`, L154-161) is
  computed once at import time from `oav_max_x` (skipped in `--dev`,
  using fixed fallbacks instead) and used to derive `calibrate`, the
  pixel→µm factor used by click-to-move (§3.2).

### 3.2 Stage / goniometer motion

- **Click-to-move**: `onMouse` (L760-795), bound to mouse clicks on the
  OAV widget when `canvasMode == "move"`. Converts the click position to
  a stage/goniometer move using `calibrate`, the current zoom level, and
  the current omega angle (to resolve the click's Y-offset into gonio Y/Z
  components via `sin`/`cos` of omega) — then `caput`s `stage_x`,
  `gonio_y`, `gonio_z` directly. No Bluesky path for this action.
- **Jog buttons** (up/down/left/right/Zs±/Z±): `jogSample` (L655-735).
  Direction-dependent trig identical in spirit to click-to-move, applied
  as a fixed increment (`spinBoxZJogAmount`/`spinBoxZsJogAmount`,
  divided by 1000 for µm→mm). Respects `--blueapi` / `bluesky_mode` /
  direct-CA as three separate branches (not mutually exclusive as
  written — see §2).
- **Omega rotation**: buttons for ±5/15/90/180°, a "go to ±3600°"
  (`goTopm3600`, L737-744, flips sign based on current sign of omega),
  slow/fast turn (sets `omega_velo` to 15 or 40 directly), and a
  `doubleSpinBoxOmegaJog`-driven custom increment. All via
  `gonioRotate` (L963-975) except the velocity buttons which `caput`
  `omega_velo` inline in `__init__` (L453-454).
- **Zero all**: `returntozero` (L626-633) — Bluesky path calls
  `beamline_safe.go_to_zero(wait=False)`; direct path `caput`s five
  motors to 0 in a loop (no `--blueapi` branch here either).
- **RBV display**: `RBVThread` (L289-319), a `QThread` polling 9 PVs
  every 1s (stage X/Z/Y, gonio Y/Z, omega, OAV exposure/gain RBVs,
  robot current pin) plus a pin-mounted tri-state check, emitting a list
  consumed by `updateRBVs` (L977-1003). That handler also derives a
  **beamline-safe indicator**: green iff X/Y/Z/omega/gonioY/gonioZ RBVs
  are all exactly 0 (L993), in which case it also `caput`s
  `robot_ip16_force_option` to `"On"` — i.e. this GUI actively changes a
  robot interlock-related PV as a side effect of a status display
  update, not an explicit user action (L995).
- **Auto pin-tip centring**: `autoCenter` (L1005-1017), always routes
  through Bluesky regardless of `--bluesky`/`--blueapi` flags — imports
  `dodal`'s `PinTipDetection` and `mx_bluesky`'s
  `aithre_pin_tip_centre` plan directly, builds fresh `goniometer`/`oav`
  devices via `init_devices()`, and runs on a **newly-created
  `RunEngine`** each call (not the same one `--bluesky` mode might have
  created elsewhere — there is no shared/persistent `RunEngine` in this
  file). Bound to the "AutoCenter" button (L446). Note there is also a
  separate, standalone `bin/centerpin.py` script implementing a
  different (older, OpenCV-edge-detection-based) auto-centring approach
  against a different OAV URL and calibration constants — it is not
  imported by or wired into `guiv4_prod.py` at all; see §6.

### 3.3 Sample robot

- Buttons: reset, load (by pin number, `spinToLoad`), unload, dry.
  `resetRobot` is inlined in `__init__` (`caput robot_reset`, L480);
  `loadNextPin`/`unloadPin`/`dryGripper` (L590-618) each `caput` a reset
  PV, sleep 3s, then `caput` the relevant `.PROC` field. Only
  `loadNextPin` has a Bluesky branch (builds `goniometer`/`robot`
  devices then immediately calls `goniometer.omega.stop()` — it does not
  appear to actually invoke a robot-load plan on that branch, L593-599;
  worth double-checking this is intentional rather than an
  incomplete port).
- Soak/dispose/go-home PVs exist in `pv.py` (`robot_proc_soak`,
  `robot_proc_dispose`, `robot_proc_gotohome`) but have no corresponding
  UI button/handler found in `guiv4_prod.py` — either unused, or wired
  through a `.ui` connection not captured by the `self.ui.X.clicked.connect(...)`
  pattern used everywhere else (worth confirming against the `.ui` file
  directly if these are meant to be reachable).

### 3.4 High-mag optics

- `pv.py` defines zoom/focus PVs and tweak PVs (`highmag_zoom`,
  `highmag_focus`, `.TWR`/`.TWF`) but — like the robot soak/dispose/home
  PVs above — no handler in `guiv4_prod.py` references them directly by
  name; if wired up it's via a UI element/pattern not visible in the
  Python source alone.

### 3.5 Shape drawing and RTC6 cutting

- **Draw mode**: `radioButtonDrawMode` toggles `canvasMode`
  (`toggleCanvasMode`, L746-758); while in draw mode, clicks append to
  `self.drawn_points` and redraw (`redrawPoints`, L797-810) instead of
  moving the stage.
- **Cut**: `savePoints` (L812-835) converts drawn points from
  display-pixel to beam-relative micron coordinates, repeats the point
  list `spinBoxRepetitions` times, and — only if `--rtc6` was passed —
  calls `self.rtc6.cut_polygon_from_gui(...)` on the `CutShapes` instance
  acquired at startup. Without `--rtc6`, it just logs what would have
  been cut. There is no PV-based / EPICS path for cutting at all — RTC6
  control here is entirely through the in-process `rtc6_fastcs.cut_shapes`
  Python binding, not Channel Access (contrast with `rtc6eth_*` PVs
  below, which are read-only status/speed, not the cut command path
  itself).
- **Preset shapes**: `loadPresetShape` (L837-859) opens a file picker
  rooted at a hard-coded path,
  `/dls/science/groups/i23/aithre/rtc6-fastcs/shape_protocols/`, and
  just records the selected filename in the UI — it doesn't appear to
  feed into `savePoints`/cutting itself from what's visible in this
  file (no evidence `self.preset_file_path` is read anywhere else in
  `guiv4_prod.py`).
- **RTC6 status/speed**: `rtc6Control("acquire"/"check")` (L545-557)
  connects to the board and colors an indicator from
  `rtc6eth_info_is_acquired` PV. `setRTC6Speed` (L861-881) parses a
  combo-box value like `"0.005 m/s"` and `caput`s
  `rtc6eth_control_markspeed`. Both PVs are served by the separate
  `rtc6-fastcs` FastCS IOC (`RTC6ETH:` prefix) — i.e. RTC6 has **two**
  parallel interfaces in this file: the in-process Python binding for
  the actual cut command, and Channel Access for status/speed.

### 3.6 Laser control (Carbide)

Documented in detail in memory from a previous session's migration
work; summarized here for completeness of this spec. **On the `linux`
branch as of this writing**, this subsystem still talks **directly to
the Carbide REST API**, not through EPICS:

- `commandLaser` (L559-587) instantiates
  `laserControl.carbide(endpoint=LASERENDPOINT)` fresh on every call
  (`LASERENDPOINT = "http://172.23.171.207:20010"`, L144, flagged in
  its own comment as `# this is going to change soon!`) and dispatches
  Enable/Disable/SetDivider/SetAttenuator/Startup/Standby to REST calls.
- `LaserStatusThread` (L322-395) polls 6 REST endpoints every 500ms via
  `httpx.AsyncClient` and `asyncio.gather`, running its own event loop
  inside the `QThread` (separate from the `qasync` loop the rest of the
  app uses — L1022-1027). `updateLaserStatus` (L506-530) turns that into
  UI indicator colors and text.
- A branch (`carbide-fastcs-migration`, on the `armin306/aithre` fork,
  not yet merged/tested) replaces both of these with EPICS PVs served by
  a new `carbide-fastcs` FastCS IOC, matching how RTC6 status is already
  done. Not yet deployed against real hardware — see the Diamond
  Kubernetes IOC migration status in the `carbide-fastcs`/`rtc6-fastcs`
  project memory.
- Pharos (the other laser named in the README's feature list) has no
  implemented control path anywhere in this file or `laserControl.py`/
  `laserControlAsync.py` — commented-out endpoint constants only.

### 3.7 Shutdown

`closeEvent` (L532-543) stops/quits/waits on `laserStatusThread` only —
`OAVth` and `RBVth` are not explicitly stopped on close (they're daemon-
like `QThread`s that presumably get torn down with the process, but
there's no symmetric `stop()`/`wait()` call for them here the way there
is for the laser thread).

---

## 4. Bluesky / BlueAPI integration status

`config.yaml` declares two Bluesky "sources" for a BlueAPI worker:
`dodal.beamlines.aithre` (devices) and
`mx_bluesky.beamlines.aithre_lasershaping` (plan functions), plus a
STOMP broker config (`localhost:61613`, guest/guest — looks like a local/
dev default, not a real broker address). This confirms `dodal`/
`mx_bluesky` already have an "aithre" beamline definition to build on.

Within `guiv4_prod.py` itself, Bluesky/BlueAPI usage is **partial and
inconsistent** (see the per-action notes in §3 above) — some actions
support both extra flags, some only one, some neither. `autoCenter` is
the only action that unconditionally uses Bluesky regardless of flags.
No action reuses a single long-lived `RunEngine`; each Bluesky-path
method that needs one constructs `RunEngine({})` fresh.

---

## 5. External systems and interfaces

| System | Interface | Where |
|---|---|---|
| Stage/goniometer/omega motors | EPICS CA (`LA18L-MO-LSR-01:*`) | `bin/pv.py`, `bin/control.py` |
| OAV camera (Alvium 1240M) | MJPEG over HTTP (`bl23i-ea-serv-01.diamond.ac.uk:8080`) + EPICS CA (`LA18L-DI-OAV-01:*`) for camera settings | `bin/guiv4_prod.py` (`OAVThread`), `bin/pv.py` |
| Sample robot | EPICS CA (`LA18L-MO-ROBOT-01:*`) | `bin/pv.py` |
| High-mag optics | EPICS CA (`LA18L-MO-LSR-01:ZOOM`/`:FOCUS`) — PVs defined, no confirmed caller in this file | `bin/pv.py` |
| Carbide laser | Direct REST (`172.23.171.207:20010`) on `linux` branch; migrating to EPICS CA (`CARBIDE:*` via `carbide-fastcs`) on an unmerged/untested branch | `bin/laserControl.py`, `bin/guiv4_prod.py` |
| RTC6 galvo scanhead | In-process Python binding (`rtc6_fastcs.cut_shapes.CutShapes`) for cutting; EPICS CA (`RTC6ETH:*`) for status/speed only | `bin/pv.py`, `guiv4_prod.py` |
| Bluesky plans | `mx_bluesky.beamlines.aithre_lasershaping`, local `RunEngine` per call | `guiv4_prod.py` |
| BlueAPI worker | REST (`BlueapiClient`) + STOMP, config from `config.yaml` | `config.yaml`, `guiv4_prod.py` |

---

## 6. Configuration, packaging, and deployment

- **CLI flags**: §2.
- **Hard-coded absolute paths** (all `/dls/science/groups/i23/aithre/...`):
  the BlueAPI config file (`config.yaml`, L109), the RTC6 shape-preset
  directory (L845), and the shebang line itself (L1: `#!/dls/science/
  groups/i23/aithre/aithre/.venv/bin/python`). None of these have an
  environment-variable or CLI override — they assume this exact
  deployment location.
- **Logging**: per-day file (`./DDMMYYYY.log`, CWD-relative) plus
  stdout, `DEBUG` level (L28-45).
- **Python/dependency management**: `pyproject.toml` (full dependency
  list, Linux-oriented), `.python-version` (3.11), `uv.lock`. Separate
  `requirements.txt` (Linux) and `requirements-windows.txt` (Windows,
  no Bluesky/EPICS-dependent packages) also exist — **`requirements.txt`
  on this branch currently omits `httpx` and `qasync`**, both of
  which the code imports unconditionally at module load
  (`guiv4_prod.py` L88, L91; `pyproject.toml` lists them, `requirements.txt`
  doesn't) — worth checking whether this file is actually what gets used
  to provision the production venv, or whether `pyproject.toml`/`uv.lock`
  is authoritative and this file is stale.
- **Windows packaging**: `aithre.spec` (PyInstaller), explicitly
  excludes the Bluesky/RTC6/dodal stack from the frozen EXE. Windows
  runs are forced into `--nortc6`-equivalent behavior via the
  `platform.system() == "Windows"` check (L25-26), independent of the
  PyInstaller excludes.
- **CI**: the README documents a `.github/workflows/build-windows.yml`
  GitHub Actions workflow (Windows PyInstaller build + smoke test). **On
  the `linux` branch, `.github/` does not exist** — removed by a commit
  titled "Delete .github directory". The workflow presumably still
  exists on the `windows` branch; this is a documentation/branch
  divergence worth being aware of, not a bug in the running application.

---

## 7. Observations worth keeping in mind (not fixed as part of this doc)

These are things noticed while reading, recorded here because they're
relevant to planning future work, not because they need immediate
action:

1. **`ca.caget`/`ca.caput` retry forever with no timeout** if a PV
   doesn't exist or `caget`/`caput` fails (`control.py` L10-52, bare
   `except:` + `while val is None:` loop for `caget`/`cagetstring`).
   Any new hardware integration that adds a PV before the IOC serving it
   is actually up would hang the calling thread indefinitely rather than
   erroring.
2. **`go_to_max` (L653-654)** is defined with no `self` parameter (would
   `TypeError` if called as a bound method) and is never called or
   connected to anything — dead code, likely a leftover fragment.
3. **`bin/centerpin.py`** is a standalone script (executes a Bluesky
   plan and `raise Exception()`s at import time, L211-219) implementing
   a second, independent auto-pin-centring approach against a different
   OAV URL/calibration than the one wired into the GUI's "AutoCenter"
   button. Not imported by `guiv4_prod.py`.
4. **`control.control` class** (`control.py` L55-63) is defined but has
   no callers anywhere in the codebase (only `control.ca` is used).
5. Several PVs defined in `pv.py` (robot soak/dispose/go-home, high-mag
   zoom/focus) have no corresponding handler visibly wired up in
   `guiv4_prod.py` — either dead, or connected through a `.ui`-level
   mechanism not visible from the Python source.
6. `loadNextPin`'s Bluesky-mode branch builds `goniometer`/`robot`
   devices but its only subsequent action is `goniometer.omega.stop()`
   — it's unclear whether this is intentionally a safety stop before an
   unwritten load plan, or an incomplete port from the direct-CA
   version.
7. `updateRBVs` (L993-996) mutates a robot interlock-related PV
   (`robot_ip16_force_option`) as a side effect of computing a status
   indicator's color, rather than as an explicit user action — worth
   flagging since it's a controls-affecting write hidden inside what
   reads like a pure display-update function.

---

## 8. Direction for upcoming work

Decisions from Armin, captured as they come in (2026-09-17 onward).

### Drop Windows support

**Decision (2026-09-17)**: Aithre will run exclusively on Linux going
forward. Windows is no longer a supported target.

Consequences this implies for the codebase described in §6 (not yet
acted on):
- `requirements-windows.txt`, `aithre.spec` (PyInstaller spec), and the
  `windows` branch/CI workflow (`build-windows.yml`, §6) become
  removable.
- The `platform.system() == "Windows"` checks that force `--nortc6`-
  equivalent behavior and disable RTC6 (L25-26) become dead code once
  Windows is no longer a real execution target.
- The "vendor-software workstation" mode described in the README
  (Windows machine talking to vendor laser software directly, RTC6/
  Bluesky excluded) needs a decision on where that workflow goes, if
  it's still needed at all — worth clarifying with Armin whether that
  use case disappears entirely or moves onto Linux too.
- `branches and versioning` in the README (`windows` as default branch,
  `linux` as production) will need updating — presumably `linux`
  becomes the sole/default branch.

### Carbide laser: migrate off direct REST onto carbide-fastcs

**Decision (2026-09-17, already in progress)**: the direct-REST Carbide
laser control described in §3.6/§5 (`laserControl.py`,
`commandLaser`/`LaserStatusThread` hitting `172.23.171.207:20010`
directly) is being replaced with EPICS PVs served by a new
[carbide-fastcs](https://github.com/armin306/carbide-fastcs) FastCS IOC
— the same pattern RTC6 status already uses (`RTC6ETH:*` PVs, §3.5).

This is not just planned - it's already implemented on the
`carbide-fastcs-migration` branch (pushed to `armin306/aithre`, not yet
merged into `linux`):
- `commandLaser` rewritten to `ca.caput` against new `CARBIDE:ACTIONS:*`/
  `CARBIDE:BASIC:*` PVs instead of instantiating `laserControl.carbide(...)`.
- `LaserStatusThread` rewritten from an async `httpx` polling loop to
  synchronous `ca.caget` polling (matching `RBVThread`'s existing
  pattern), against `CARBIDE:STATUS:*`/`CARBIDE:BASIC:*`.
- `laserControl.py`/`laserControlAsync.py` removed (both fully dead
  after the migration).
- A real type bug was found and fixed in `carbide-fastcs` itself along
  the way: `ActualShutterState` is a string (`"Opened"`/`"Closed"` per
  the vendor API docs), not the int it was wired up as - caught by
  cross-referencing this GUI's own working comparisons.

**Still blocking before this can merge**: untested against a running
`carbide-fastcs` IOC or real/emulated laser (only unit tests + Python
compile-checks so far), and the IOC itself needs the controls/IT group
to deploy it (Armin can't self-install) - see the deployment-questions
discussion from earlier this session. Once both are resolved, this
branch is ready to be reviewed/merged.

*(more to come — this section will keep growing with Armin's direction
on the upcoming hardware integrations)*

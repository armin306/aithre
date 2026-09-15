![Code style: black](https://img.shields.io/badge/code%20style-black-000000.svg)
![GitHub repo size](https://img.shields.io/github/repo-size/co2e14/aithre)
![GitHub top language](https://img.shields.io/github/languages/top/co2e14/aithre)
![License: BSD-3-Clause](https://img.shields.io/badge/License-BSD%203--Clause-blue.svg)
![Python](https://img.shields.io/badge/python-3.11-blue)

# Aithre — I23 Laser Shaping Controls

Aithre is the control GUI for the **laser shaping system on Beamline I23** at Diamond Light Source. It provides an operator-facing interface for on-axis sample viewing (OAV), goniometer and stage motion, robot sample handling, high-mag optics, and femtosecond laser control (Carbide / Pharos) used to shape biological samples for macromolecular crystallography.

The project wraps EPICS process variables, an RTC6 galvo scanhead (via `rtc6-fastcs`), camera streaming (OpenCV / MJPEG), and optionally Bluesky / `mx-bluesky` plan execution behind a PyQt5 UI.

![icon](https://user-images.githubusercontent.com/45949926/161590407-25c165b3-38b9-4906-93d2-ac9a1d5d4eb9.png)

---

## Table of contents

- [Features](#features)
- [Repository layout](#repository-layout)
- [Requirements](#requirements)
- [Installation](#installation)
  - [At Diamond (Linux, production)](#at-diamond-linux-production)
  - [Windows (vendor-software workstation)](#windows-vendor-software-workstation)
  - [Troubleshooting: PyQt5 / OpenCV clash](#troubleshooting-pyqt5--opencv-clash)
- [Running the GUI](#running-the-gui)
  - [Command-line flags](#command-line-flags)
- [Configuration](#configuration)
- [Hardware & services](#hardware--services)
- [Building a standalone Windows executable](#building-a-standalone-windows-executable)
- [Continuous integration](#continuous-integration)
- [Branches and versioning](#branches-and-versioning)
- [Development notes](#development-notes)
- [License](#license)

---

## Features

- **Live OAV stream** — MJPEG feed from the on-axis viewing camera with overlaid grid and beam-position crosshair; adjustable zoom centred on the beam position.
- **Move-on-click** — click in the video feed to drive the sample stage / goniometer to that point, using a pixel-to-µm calibration that accounts for camera pixel size and feed/display scaling.
- **Stage and goniometer control** — X / Y / Z linear stages, sample Y / Z, and omega rotation, all driven through EPICS PVs (see [bin/pv.py](bin/pv.py)).
- **Robot handling** — load / unload / soak / dispose / dry / home controls for the sample robot.
- **High-mag optics** — zoom and focus controls with tweak-step support.
- **Laser control** — Carbide femtosecond laser control over EPICS PVs, served by the [carbide-fastcs](https://github.com/armin306/carbide-fastcs) IOC (which owns the REST communication to the laser; see `commandLaser`/`LaserStatusThread` in [bin/guiv4_prod.py](bin/guiv4_prod.py) and [CarbideRestApi.html](CarbideRestApi.html) for the underlying vendor API). Pharos was referenced in the old REST wrapper's comments but never had an implemented control path.
- **RTC6 galvo integration** — shape cutting via `rtc6-fastcs` (Linux only).
- **Bluesky / BlueAPI modes** — optional execution of `mx_bluesky.beamlines.aithre_lasershaping` plans against a running RunEngine or a BlueAPI REST worker.
- **Cross-platform logging** — per-day log files (`DDMMYYYY.log`) written to CWD alongside stdout.

## Repository layout

```
lasershaping/
├── bin/                     Production GUI and control modules
│   ├── guiv4_prod.py        Current production entry point (v4.3.0)
│   ├── gui_4_3_0.py         Qt Designer-generated UI class
│   ├── guiv4_3_0.ui         Qt Designer UI source
│   ├── control.py           caget / caput / cagetstring wrappers
│   ├── pv.py                Central list of EPICS PVs for the beamline
│   │                        (includes CARBIDE:* PVs for carbide-fastcs)
│   ├── centerpin.py         Pin-tip centring via OpenCV + ophyd
│   ├── guiv4_2_5.py, guiv4_2_6*.py  Previous GUI versions kept for reference
│   └── *.png                UI assets (icon, arrow buttons)
├── emerita/                 Retired GUI prototypes (v1 → v4.2.3), kept for history
├── testing/                 Exploratory / scratch scripts (pymba, OpenCV, tk prototypes)
├── .github/workflows/       CI: build-windows.yml (PyInstaller build)
├── aithre.spec              PyInstaller spec for the Windows EXE
├── config.yaml              BlueAPI source / STOMP config
├── guiv4.ui                 Root-level copy of the UI bundled into the EXE
├── pyproject.toml           Project metadata and full (Linux) dependency list
├── requirements.txt         Pinned Linux runtime deps
├── requirements-windows.txt Slim Windows deps (no Bluesky stack)
├── .python-version          Python 3.11 pin (pyenv / uv)
├── uv.lock                  uv dependency lockfile
└── LICENSE                  BSD 3-Clause
```

## Requirements

- **Python 3.11** (pinned in [.python-version](.python-version) and [pyproject.toml](pyproject.toml)).
- **Linux** for full functionality (EPICS `caget`/`caput` on PATH, RTC6 hardware, Bluesky stack).
- **Windows** is supported as a *vendor-software workstation* mode: the GUI runs, RTC6 acquisition is force-disabled (see `--nortc6` below), and Bluesky is excluded.

Core Python packages (see [pyproject.toml](pyproject.toml)):

```
pyqt5, opencv-python-headless, numpy, qasync, zmq,
bluesky, ophyd, ophyd-async, softioc, mx-bluesky, blueapi   (Linux only)
```

Additional system-level dependencies on Linux:

- EPICS base (`caget`, `caput`, `cainfo` must be on PATH — [bin/control.py](bin/control.py) shells out to them).
- A local checkout of `rtc6-fastcs` installed via `pip install ../path/to/rtc6-fastcs`.
- A running [carbide-fastcs](https://github.com/armin306/carbide-fastcs) IOC reachable over Channel Access (PV prefix `CARBIDE:`) — needed for `commandLaser`/`LaserStatusThread` in [bin/guiv4_prod.py](bin/guiv4_prod.py) to control/monitor the laser. No local package install needed on this side; it's a separate process talking EPICS CA, the same way the beamline's own `LA18L-*` PVs are reached.

> **Note**: `requirements.txt`, `requirements-windows.txt`, and `uv.lock` still list `requests`/`httpx` as of this doc update — they were pyproject.toml-derived and need regenerating (`uv lock` / `uv export`) now that direct REST calls to Carbide have been removed.

## Installation

### At Diamond (Linux, production)

```bash
module load python/3.11
python -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
# Bluesky / RTC6 integration (optional, needed for --bluesky and cut_shapes):
pip install bluesky ophyd ophyd-async softioc mx-bluesky blueapi
pip install ../path/to/rtc6-fastcs
```

Alternatively, with [uv](https://github.com/astral-sh/uv) and the committed [uv.lock](uv.lock):

```bash
uv sync
```

### Windows (vendor-software workstation)

The Windows path targets a developer/operator machine that talks to the vendor laser software and does **not** load the RTC6 board or the Bluesky stack.

```powershell
py -3.11 -m venv .venv-win
.\.venv-win\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements-windows.txt
```

Windows runs are automatically forced into `--nortc6` mode by [bin/guiv4_prod.py:25-26](bin/guiv4_prod.py#L25-L26) regardless of the flag the user passes.

### Troubleshooting: PyQt5 / OpenCV clash

If the GUI fails to launch (typically silent Qt-plugin errors after a pip upgrade), the two packages' bundled Qt libraries have collided. Reinstall in this specific order:

```bash
pip uninstall -y opencv-python opencv-python-headless
pip uninstall -y PyQt5 PyQt5-sip
pip install --upgrade pip
pip install opencv-python-headless
pip install PyQt5
```

Always use the **headless** OpenCV build — it has no bundled Qt and therefore cannot fight PyQt5 for the Qt plugin path.

## Running the GUI

From an activated environment:

```bash
python bin/guiv4_prod.py [flags]
```

The production entry point is [bin/guiv4_prod.py](bin/guiv4_prod.py) (currently version **4.3.0**). The older `guiv4_2_5.py` / `guiv4_2_6.py` files remain in [bin/](bin/) for rollback purposes and should not be used for new work.

### Command-line flags

| Flag | Purpose |
|---|---|
| `--dev` | Development mode — runs the GUI outside the lab with reduced camera resolution and no EPICS calls for feed sizing. |
| `--bluesky` | Import and use `mx_bluesky.beamlines.aithre_lasershaping` plans via a local RunEngine instead of direct `caput`/`caget`. |
| `--blueapi` | Use the BlueAPI REST client (see [config.yaml](config.yaml)). Composable with `--bluesky`. |
| `--nortc6` | Skip RTC6 board acquisition. Required whenever the Windows vendor software owns the board. Forced on automatically on Windows. |
| `--beampos X,Y` | Override the default beam-position pixel coordinates (defaults: `1644,1232`). Example: `--beampos 1600,1200`. |

## Configuration

- **[config.yaml](config.yaml)** — BlueAPI `env.sources` (`dodal.beamlines.aithre`, `mx_bluesky.beamlines.aithre_lasershaping`) and the STOMP broker used by the worker.
- **Beamline PVs** — all EPICS PVs are collected in [bin/pv.py](bin/pv.py). Update that file if a PV name changes rather than editing the GUI modules.
- **Endpoints** — OAV stream hard-coded in [bin/guiv4_prod.py](bin/guiv4_prod.py) as `OAVADDRESS`: `http://bl23i-ea-serv-01.diamond.ac.uk:8080/OAV.mjpg.mjpg`. Carbide laser control no longer has a hard-coded endpoint — it goes through the `CARBIDE:*` PVs in [bin/pv.py](bin/pv.py), served by wherever the `carbide-fastcs` IOC is deployed.

## Hardware & services

| Subsystem | Interface | Code |
|---|---|---|
| Sample stages, goniometer, omega | EPICS (`LA18L-MO-LSR-01:*`) | [bin/pv.py](bin/pv.py), [bin/control.py](bin/control.py) |
| OAV camera (Alvium 1240M) | MJPEG + EPICS `LA18L-DI-OAV-01:*` | [bin/guiv4_prod.py](bin/guiv4_prod.py) |
| Sample robot | EPICS (`LA18L-MO-ROBOT-01:*`) | [bin/pv.py](bin/pv.py) |
| High-mag optics | EPICS (`LA18L-MO-LSR-01:ZOOM` / `:FOCUS`) | [bin/pv.py](bin/pv.py) |
| Carbide laser | EPICS PVs (`CARBIDE:*`, served by [carbide-fastcs](https://github.com/armin306/carbide-fastcs); vendor REST API documented in [CarbideRestApi.html](CarbideRestApi.html)) | [bin/pv.py](bin/pv.py), [bin/guiv4_prod.py](bin/guiv4_prod.py) (`commandLaser`, `LaserStatusThread`) |
| RTC6 galvo scanhead | `rtc6-fastcs` (imports `cut_shapes`) | guarded by `--nortc6` |
| Bluesky plans | `mx_bluesky.beamlines.aithre_lasershaping` | guarded by `--bluesky` |
| BlueAPI worker | REST (`BlueapiClient` + STOMP) | [config.yaml](config.yaml) |

Camera calibration is derived at startup from the camera pixel size (1.85 µm for the Alvium 1240M) divided by the feed-to-display ratio — see [bin/guiv4_prod.py:157-161](bin/guiv4_prod.py#L157-L161).

## Building a standalone Windows executable

A frozen single-file `aithre.exe` is produced by PyInstaller using [aithre.spec](aithre.spec). The spec explicitly **excludes** the Bluesky / RTC6 / dodal stack so the EXE ships only what the Windows operator needs.

Locally:

```powershell
python -m pip install -r requirements-windows.txt
pyinstaller --clean --noconfirm aithre.spec
# result: dist\aithre.exe
```

The EXE bundles the `.ui` file, icon, and arrow-button PNGs. A runtime asset resolver ([bin/guiv4_prod.py:52-77](bin/guiv4_prod.py#L52-L77)) patches `QtGui.QPixmap` to search the PyInstaller `_MEIPASS` directory and the source tree so the same code works frozen and unfrozen.

## Continuous integration

[.github/workflows/build-windows.yml](.github/workflows/build-windows.yml) runs on pushes to `windows` / `linux`, on `v*` tags, and on manual dispatch. It:

1. Sets up Python 3.11 on `windows-latest`.
2. Installs from [requirements-windows.txt](requirements-windows.txt).
3. Builds the EXE with `pyinstaller --clean --noconfirm aithre.spec`.
4. Runs a best-effort smoke test (`aithre.exe --dev --nortc6 --help`).
5. Uploads `dist/aithre.exe` as an artifact (retained 30 days).
6. Attaches the EXE to a GitHub release when the build was triggered by a `v*` tag.

## Branches and versioning

- **`windows`** — default branch; target for Windows-focused work and the CI build.
- **`linux`** — production Linux deployment branch.
- **`dev`, `blueapi`, `cothread`, `fastcam`, `azure`, `linux_4-3-0`** — feature / experiment branches.
- **Tags** — versioned `v*` tags (e.g. `v4.2.5-windows`, `v4.2.6-linux`) trigger an attached GitHub release with the bundled EXE.

The in-code version string is set in [bin/guiv4_prod.py](bin/guiv4_prod.py) (`version = "4.3.0"`).

## Development notes

- **Qt Designer** — the UI is authored in `.ui` files and compiled with `pyuic5` into `gui_4_3_0.py`. Regenerate after editing the `.ui`:
  ```bash
  pyuic5 bin/guiv4_3_0.ui -o bin/gui_4_3_0.py
  ```
- **Linting / formatting** — `ruff` is listed as a dependency in [pyproject.toml](pyproject.toml); the repo badge advertises `black` style.
- **Logging** — logs go to `./DDMMYYYY.log` in the current working directory plus stdout.
- **Asset paths** — always construct asset paths relative to `__file__` or through the `_AssetQPixmap` shim so the code works both under `python bin/guiv4_prod.py` and inside the frozen EXE.
- **Legacy code** — [emerita/](emerita/) and [testing/](testing/) are kept for archaeology. Don't add new code there.

## License

BSD 3-Clause — see [LICENSE](LICENSE). © 2025 Diamond Light Source.

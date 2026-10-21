#!/usr/bin/env python3
"""Collect OAV image series of EM grids for developing grid auto-centring.

Each run writes one folder with full-resolution lossless frames, a
positions.csv of motor readbacks per frame, and a notes.txt.

Run "python grid_sweep.py check" first: it prints the camera and file plugin
state and whether frames arrive, without moving anything.

Sweeps (see em-grid-image-collection-tasks.md):
    omega       omega from --start to --stop in --step (sweeps A, B, C)
    focus       sample moved along the beam, +-RANGE um in STEP um (sweep D)
    offset      sample offset in the image plane, 0, +-200, +-500 um in u and v (sweep E)
    background  N frames at the current position, no motion (sweep F)

Frames are written either by the OAV TIFF plugin on the IOC host
(--writer tiff) or HDF5 plugin (--writer hdf5, default; the TIFF plugin
did not receive frames on the I23 OAV IOC) on the IOC host, or read
from the ARR plugin and saved locally
(--writer array). The MJPEG stream is never used, as it is JPEG-compressed.

The laser must be closed or in standby. All moves are relative to the
position at start, and the sample is returned there at the end or on abort.

Examples:
    python grid_sweep.py omega --start 0 --stop 358 --step 2 --name A_full \\
        --grid "Cu300 Quantifoil" --temp warm --ioc-dir /dls/i23/data/grids
    python grid_sweep.py omega --start 85 --stop 91 --step 0.1 --name B_edgeon ...
    python grid_sweep.py focus --range 100 --step 10 --name D_focus ...
    python grid_sweep.py offset --name E_offset ...
    python grid_sweep.py background --frames 3 --name F_background ...
    add --dry-run to print the moves without moving or saving anything.
"""

import argparse
import csv
import math
import os
import sys
import time
from datetime import datetime

# large enough for a 4024 x 3036 16-bit frame over Channel Access
os.environ.setdefault("EPICS_CA_MAX_ARRAY_BYTES", str(64 * 1024 * 1024))
try:
    import epics  # pyepics
except ImportError:
    epics = None


class _CaTools:
    """Fallback when pyepics is missing: the EPICS caget/caput command-line tools, as in control.py.

    Enough for the TIFF writer; the array writer needs pyepics to read whole frames.
    """

    @staticmethod
    def caget(name, timeout=5, as_string=False, count=None):
        import subprocess

        cmd = ["caget", "-t", "-w", str(timeout)] + (["-S"] if as_string else ["-n"]) + [name]
        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
        if result.returncode != 0 or not result.stdout.strip():
            return None
        value = result.stdout.strip()
        if as_string:
            return value
        try:
            return float(value)
        except ValueError:
            return value

    @staticmethod
    def caput(name, value, wait=False, timeout=60):
        import subprocess

        cmd = ["caput", "-t"]
        if wait:
            cmd += ["-c", "-w", str(timeout)]

        def run(args):
            result = subprocess.run(args + [name, str(value)], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    universal_newlines=True)
            return result.returncode == 0 and "Error" not in result.stderr

        # strings go to char waveforms (FilePath, FileName) with -S, or to string records (NDArrayPort) without
        if isinstance(value, str) and run(cmd + ["-S"]):
            return 1
        return 1 if run(cmd) else None


if epics is None:
    import shutil

    if shutil.which("caget") is None or shutil.which("caput") is None:
        sys.exit("Neither pyepics nor the caget/caput tools were found. Load EPICS first "
                 "(at Diamond: 'module load epics' or 'module load controls_dev'), or run with "
                 "a Python that has pyepics installed.")
    print("pyepics not found, using the caget/caput command-line tools (install pyepics for --writer array)")
    epics = _CaTools



class pv:
    """The PVs this script uses, copied from bin/pv.py so the script runs on its own."""

    stage_x = "LA18L-MO-LSR-01:X"
    stage_x_rbv = "LA18L-MO-LSR-01:X.RBV"
    stage_y_rbv = "LA18L-MO-LSR-01:Y.RBV"
    stage_z_rbv = "LA18L-MO-LSR-01:Z.RBV"
    gonio_y = "LA18L-MO-LSR-01:SAMPY"
    gonio_y_rbv = "LA18L-MO-LSR-01:SAMPY.RBV"
    gonio_z = "LA18L-MO-LSR-01:SAMPZ"
    gonio_z_rbv = "LA18L-MO-LSR-01:SAMPZ.RBV"
    omega = "LA18L-MO-LSR-01:OMEGA"
    omega_rbv = "LA18L-MO-LSR-01:OMEGA.RBV"
    highmag_zoom_rbv = "LA18L-MO-LSR-01:ZOOM.RBV"
    highmag_focus_rbv = "LA18L-MO-LSR-01:FOCUS.RBV"
    oav_cam_acqtime_rbv = "LA18L-DI-OAV-01:CAM:AcquireTime_RBV"
    oav_cam_gain_rbv = "LA18L-DI-OAV-01:CAM:Gain_RBV"


OAV = "LA18L-DI-OAV-01:"
FULL_SIZE = (4024, 3036)
MAX_OFFSET_UM = 1000  # refuse any sweep that moves the sample further than this from start
MOVE_TIMEOUT = 120

CSV_FIELDS = [
    "file", "omega_deg", "stage_x", "stage_y", "stage_z", "gonio_y", "gonio_z",
    "zoom", "focus", "exposure_s", "gain", "set_u_um", "set_v_um", "set_w_um", "timestamp",
]


def get(name):
    value = epics.caget(name, timeout=5)
    if value is None:
        raise RuntimeError(f"caget failed: {name}")
    return value


def put(name, value, wait=True):
    status = epics.caput(name, value, wait=wait, timeout=MOVE_TIMEOUT)
    if status != 1:
        raise RuntimeError(f"caput failed or timed out: {name} = {value}")


def read_positions():
    return {
        "omega_deg": get(pv.omega_rbv),
        "stage_x": get(pv.stage_x_rbv),
        "stage_y": get(pv.stage_y_rbv),
        "stage_z": get(pv.stage_z_rbv),
        "gonio_y": get(pv.gonio_y_rbv),
        "gonio_z": get(pv.gonio_z_rbv),
        "zoom": get(pv.highmag_zoom_rbv),
        "focus": get(pv.highmag_focus_rbv),
        "exposure_s": get(pv.oav_cam_acqtime_rbv),
        "gain": get(pv.oav_cam_gain_rbv),
    }


def sample_targets(start, omega_deg, u_um=0.0, v_um=0.0, w_um=0.0):
    """Motor targets for an offset (u, v, w) in um from the start position.

    u and v follow aithre's click-to-move (MainWindow.onMouse): u is stage X,
    v is gonio Y/Z by sin/cos(omega). w, along the beam, is the perpendicular
    gonio Y/Z direction; its sign is not yet calibrated (spec C4).
    """
    om = math.radians(omega_deg)
    u, v, w = u_um / 1000, v_um / 1000, w_um / 1000
    return {
        pv.stage_x: start["stage_x"] + u,
        pv.gonio_y: start["gonio_y"] + math.sin(om) * v + math.cos(om) * w,
        pv.gonio_z: start["gonio_z"] + math.cos(om) * v - math.sin(om) * w,
    }


class FrameWriter:
    def __init__(self, mode, local_dir, ioc_dir, dry_run):
        self.mode = mode
        self.local_dir = local_dir
        self.ioc_dir = ioc_dir
        self.dry_run = dry_run
        self.plugin = OAV + {"tiff": "TIFF:", "hdf5": "HDF5:", "array": "ARR:"}[mode]
        self.ext = ".h5" if mode == "hdf5" else ".tif"

    def setup(self):
        if self.dry_run:
            return
        self.reconnect()
        width = int(get(self.plugin + "ArraySize0_RBV"))
        height = int(get(self.plugin + "ArraySize1_RBV"))
        if (width, height) != FULL_SIZE:
            print(f"WARNING: {self.plugin} receives {width} x {height}, not full resolution {FULL_SIZE}. "
                  "Check its NDArrayPort and any ROI/binning upstream.")
        if self.mode in ("tiff", "hdf5"):
            put(self.plugin + "CreateDirectory", -1)  # create the sweep folder on the IOC host
            put(self.plugin + "FilePath", self.ioc_dir)
            if int(get(self.plugin + "FilePathExists_RBV")) != 1:
                raise RuntimeError(f"IOC cannot see directory {self.ioc_dir}")
            put(self.plugin + "FileTemplate", "%s%s" + self.ext)
            put(self.plugin + "FileWriteMode", 1)  # Capture: each save grabs the next frame and writes it
            put(self.plugin + "NumCapture", 1)
            put(self.plugin + "AutoSave", 1)
            if self.mode == "hdf5":
                # without LazyOpen, Capture needs an earlier array of the same size and type (ADCore NDPluginFile docs)
                put(self.plugin + "LazyOpen", 1)
            put(self.plugin + "AutoIncrement", 0)

    def reconnect(self):
        """Make sure the plugin is enabled and receiving frames.

        Only EnableCallbacks is written. Toggling it or re-writing NDArrayPort stopped the
        frame flow on the I23 OAV IOC (2026-10-21), so neither is done here.
        """
        port = epics.caget(self.plugin + "NDArrayPort", as_string=True)
        if int(get(self.plugin + "EnableCallbacks")) != 1:
            put(self.plugin + "EnableCallbacks", 1)
        try:
            self.wait_fresh_frame(timeout=10)
        except RuntimeError:
            raise RuntimeError(f"{self.plugin} receives no frames from {port}; run 'grid_sweep.py check'")

    def wait_fresh_frame(self, frames=2, timeout=30):
        """Wait until the plugin has received `frames` new frames, so the image was exposed after the move."""
        counter = self.plugin + "ArrayCounter_RBV"
        target = int(get(counter)) + frames
        t0 = time.time()
        while int(get(counter)) < target:
            if time.time() - t0 > timeout:
                raise RuntimeError("no new OAV frames; is the camera acquiring?")
            time.sleep(0.05)

    def save(self, stem):
        if self.dry_run:
            return stem + self.ext
        try:
            self.wait_fresh_frame(timeout=5)
        except RuntimeError:
            print(f"  {self.plugin} received no new frame within 5 s, checking it is enabled")
            self.reconnect()
        if self.mode in ("tiff", "hdf5"):
            put(self.plugin + "FileName", stem)
            put(self.plugin + "Capture", 1)  # completes once the frame is captured and written
            if int(get(self.plugin + "WriteStatus")) != 0:
                raise RuntimeError(f"{self.plugin} write failed: {epics.caget(self.plugin + 'WriteMessage', as_string=True)}")
            return stem + self.ext
        width = int(get(self.plugin + "ArraySize0_RBV"))
        height = int(get(self.plugin + "ArraySize1_RBV"))
        data = epics.caget(self.plugin + "ArrayData", count=width * height, timeout=10)
        if data is None:
            raise RuntimeError("could not read ARR:ArrayData")
        import numpy as np

        image = np.asarray(data).reshape(height, width)
        image = image.astype(np.uint16 if image.max() > 255 else np.uint8)
        import cv2 as cv

        file_name = f"{stem}.tif"
        if not cv.imwrite(os.path.join(self.local_dir, file_name), image):
            raise RuntimeError(f"could not write {file_name}")
        return file_name


class MjpegWriter:
    """Saves frames from the OAV MJPEG stream, the same stream aithre displays.

    No PV is written, so the IOC's file plugins are not involved. Frames are JPEG-compressed
    by the MJPG plugin; set LA18L-DI-OAV-01:MJPG:Quality to 100 to keep the loss small.
    """

    def __init__(self, url, local_dir, dry_run, skip=2):
        self.url = url
        self.local_dir = local_dir
        self.dry_run = dry_run
        self.skip = skip  # frames to discard after a move, so the saved one was exposed after it

    def setup(self):
        if self.dry_run:
            return
        quality = epics.caget(OAV + "MJPG:Quality", timeout=5)
        if quality is not None and float(quality) < 100:
            print(f"Note: MJPG:Quality is {quality}; 100 gives the least JPEG loss")
        self.grab()  # fail early if the stream cannot be read

    def grab(self, timeout=20):
        """Return the JPEG bytes of the frame after `skip` frames, read straight from the stream."""
        from urllib.request import urlopen

        buffer = b""
        frames = 0
        t0 = time.time()
        with urlopen(self.url, timeout=timeout) as stream:
            while time.time() - t0 < timeout:
                chunk = stream.read(65536)
                if not chunk:
                    break
                buffer += chunk
                while True:
                    start = buffer.find(b"\xff\xd8")
                    end = buffer.find(b"\xff\xd9", start + 2) if start >= 0 else -1
                    if end < 0:
                        break
                    jpeg = buffer[start:end + 2]
                    buffer = buffer[end + 2:]
                    frames += 1
                    if frames > self.skip:
                        return jpeg
        raise RuntimeError(f"no frames from {self.url}")

    def save(self, stem):
        if self.dry_run:
            return stem + ".jpg"
        file_name = stem + ".jpg"
        with open(os.path.join(self.local_dir, file_name), "wb") as f:
            f.write(self.grab())
        return file_name


def build_steps(args, start):
    """List of (file stem, omega, u, v, w) for the chosen sweep."""
    omega0 = start["omega_deg"]
    if args.sweep == "omega":
        n = int(round((args.stop - args.start) / args.step))
        omegas = [round(args.start + i * args.step, 3) for i in range(n + 1)]
        return [(f"omega_{om:+07.2f}", om, 0, 0, 0) for om in omegas]
    if args.sweep == "focus":
        n = int(round(args.range / args.step))
        return [(f"w_{w:+05.0f}um", omega0, 0, 0, w) for w in (i * args.step for i in range(-n, n + 1))]
    if args.sweep == "offset":
        offsets = [(0, 0)] + [(d, 0) for d in args.offsets] + [(-d, 0) for d in args.offsets] \
            + [(0, d) for d in args.offsets] + [(0, -d) for d in args.offsets]
        return [(f"u{u:+05.0f}_v{v:+05.0f}um", omega0, u, v, 0) for u, v in offsets]
    return [(f"background_{i:02d}", omega0, 0, 0, 0) for i in range(args.frames)]


def move_to(start, omega, u, v, w, dry_run):
    targets = sample_targets(start, omega, u, v, w)
    if dry_run:
        moves = ", ".join(f"{k.split(':')[-1]}={val:.4f}" for k, val in targets.items())
        print(f"    omega={omega:.3f}, {moves}")
        return
    put(pv.omega, omega)
    for name, value in targets.items():
        put(name, round(value, 4), wait=False)
    for name in targets:  # wait for all three linear moves
        t0 = time.time()
        while int(get(name + ".DMOV")) != 1:
            if time.time() - t0 > MOVE_TIMEOUT:
                raise RuntimeError(f"{name} did not finish moving")
            time.sleep(0.05)
    time.sleep(args_settle)


def restore(start, dry_run):
    print("Returning sample to start position")
    move_to(start, start["omega_deg"], 0, 0, 0, dry_run)


def check_camera():
    """Print the OAV state that frame saving depends on, to find where acquisition stalls."""

    def show(name):
        value = epics.caget(name, timeout=5, as_string=True)
        print(f"  {name:45s} {value if value is not None else '<no connection>'}")
        return value

    print("Camera (expected: Acquire = Acquire/1, ImageMode = Continuous, TriggerMode = Off)")
    for field in ["Acquire", "ImageMode", "TriggerMode", "ArrayCallbacks", "ArraySize0_RBV", "ArraySize1_RBV",
                  "DataType_RBV", "PortName_RBV"]:
        show(OAV + "CAM:" + field)
    for plugin in ["TIFF:", "HDF5:", "ARR:"]:
        print(f"Plugin {plugin} (NDArrayPort should be the camera port above; EnableCallbacks = Enable)")
        for field in ["EnableCallbacks", "NDArrayPort", "BlockingCallbacks", "ArraySize0_RBV", "ArraySize1_RBV",
                      "DroppedArrays_RBV", "MinCallbackTime", "QueueSize", "QueueFree", "PortName_RBV",
                      "NDArrayAddress", "MaxByteRate", "ExecutionTime_RBV"]:
            show(OAV + plugin + field)
        if plugin in ("TIFF:", "HDF5:"):
            for field in ["FilePath", "FilePathExists_RBV", "FileName", "FileTemplate", "FileWriteMode",
                          "NumCapture", "AutoSave", "WriteStatus", "WriteMessage", "FullFileName_RBV"]:
                show(OAV + plugin + field)
    print("Other plugins in the chain (which source does the working MJPEG stream use?)")
    for plugin in ["MJPG:", "ROI:", "PROC:", "OVER:"]:
        for field in ["EnableCallbacks", "NDArrayPort", "ArraySize0_RBV", "ArraySize1_RBV"]:
            show(OAV + plugin + field)
    print("Frame counters over 2 s (each must increase):")
    counters = [OAV + p + "ArrayCounter_RBV" for p in ["CAM:", "TIFF:", "ARR:", "MJPG:", "ROI:", "HDF5:"]]
    before = [epics.caget(c, timeout=5) for c in counters]
    time.sleep(2)
    after = [epics.caget(c, timeout=5) for c in counters]
    for name, b, a in zip(counters, before, after):
        state = "OK" if (a is not None and b is not None and float(a) > float(b)) else "NOT INCREASING"
        print(f"  {name:45s} {b} -> {a}  {state}")


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "check":
        check_camera()
        return
    global args_settle
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("sweep", choices=["omega", "focus", "offset", "background"])
    parser.add_argument("--name", required=True, help="sweep name, for example A_full")
    parser.add_argument("--grid", required=True, help="grid type, for example 'Cu300 Quantifoil R2/2'")
    parser.add_argument("--temp", choices=["warm", "cold"], required=True)
    parser.add_argument("--notes", default="", help="free text for notes.txt (mounting, frost, ...)")
    parser.add_argument("--start", type=float, help="omega sweep start, deg")
    parser.add_argument("--stop", type=float, help="omega sweep stop, deg (inclusive)")
    parser.add_argument("--step", type=float, help="omega step in deg, or focus step in um")
    parser.add_argument("--range", type=float, default=100, help="focus sweep half-range, um")
    parser.add_argument("--offsets", type=float, nargs="+", default=[200, 500], help="offset sweep distances, um")
    parser.add_argument("--frames", type=int, default=3, help="background frame count")
    parser.add_argument("--settle", type=float, default=0.3, help="wait after each move, s")
    parser.add_argument("--writer", choices=["mjpeg", "tiff", "hdf5", "array"], default="mjpeg",
                        help="mjpeg (default): save frames from the OAV MJPEG stream, no PV writes")
    parser.add_argument("--mjpeg-url", default="http://bl23i-ea-serv-01.diamond.ac.uk:8080/OAV.mjpg.mjpg",
                        help="OAV MJPEG stream, as in aithre")
    parser.add_argument("--ioc-dir", help="directory as seen by the OAV IOC (tiff writer)")
    parser.add_argument("--out", default=".", help="local parent directory for the sweep folder")
    parser.add_argument("--beampos", default="1644,1232", help="beamX,beamY in use, recorded in notes.txt")
    parser.add_argument("--dry-run", action="store_true", help="print the moves only")
    parser.add_argument("-y", "--yes", action="store_true", help="do not ask for confirmation")
    args = parser.parse_args()
    args_settle = args.settle

    if args.sweep == "omega" and None in (args.start, args.stop, args.step):
        parser.error("omega sweep needs --start, --stop and --step")
    if args.sweep == "focus" and args.step is None:
        parser.error("focus sweep needs --step (um)")
    if args.writer == "array" and epics is _CaTools:
        parser.error("--writer array needs pyepics (pip install pyepics); use --writer tiff or hdf5 instead")
    if args.writer in ("tiff", "hdf5") and not args.ioc_dir and not args.dry_run:
        parser.error("--writer tiff/hdf5 needs --ioc-dir (or use --writer array)")
    if args.sweep == "background":
        print("Background: remove the grid from view first (or move it out of the field by hand).")

    start = read_positions()
    steps = build_steps(args, start)
    largest = max(max(abs(u), abs(v), abs(w)) for _, _, u, v, w in steps)
    if largest > MAX_OFFSET_UM:
        sys.exit(f"Refusing: sweep moves the sample {largest} um from start (limit {MAX_OFFSET_UM} um)")

    folder = f"{datetime.now():%Y%m%d}_{args.grid.replace(' ', '-').replace('/', '-')}_{args.temp}_{args.name}"
    local_dir = os.path.join(args.out, folder)
    ioc_dir = os.path.join(args.ioc_dir, folder) if args.ioc_dir and args.writer in ("tiff", "hdf5") else None

    print(f"{args.sweep} sweep, {len(steps)} images, into {folder}")
    print(f"Start: omega {start['omega_deg']:.3f}, X {start['stage_x']:.4f}, "
          f"gonio Y {start['gonio_y']:.4f}, Z {start['gonio_z']:.4f}")
    if not args.dry_run and not args.yes:
        if input("Laser closed and area clear? Type 'yes' to start: ").strip() != "yes":
            sys.exit("Aborted")

    if not args.dry_run:
        os.makedirs(local_dir, exist_ok=True)
        if ioc_dir:
            os.makedirs(ioc_dir, exist_ok=True)  # works when the IOC directory is also mounted here
    if args.writer == "mjpeg":
        writer = MjpegWriter(args.mjpeg_url, local_dir, args.dry_run)
    else:
        writer = FrameWriter(args.writer, local_dir, ioc_dir, args.dry_run)
    writer.setup()

    rows = []
    try:
        for i, (stem, omega, u, v, w) in enumerate(steps, 1):
            print(f"[{i}/{len(steps)}] {stem}")
            move_to(start, omega, u, v, w, args.dry_run)
            file_name = writer.save(stem)
            row = {"file": file_name, "set_u_um": u, "set_v_um": v, "set_w_um": w,
                   "timestamp": datetime.now().isoformat(timespec="milliseconds")}
            row.update(start if args.dry_run else read_positions())
            rows.append(row)
    except BaseException as e:
        print(f"Stopping: {e!r}")
        if not args.dry_run:
            for name in (pv.stage_x, pv.gonio_y, pv.gonio_z, pv.omega):
                epics.caput(name + ".STOP", 1)
            time.sleep(0.5)
        if not isinstance(e, KeyboardInterrupt):
            raise
    finally:
        restore(start, args.dry_run)
        if not args.dry_run:
            with open(os.path.join(local_dir, "positions.csv"), "w", newline="") as f:
                out = csv.DictWriter(f, fieldnames=CSV_FIELDS)
                out.writeheader()
                out.writerows(rows)
            with open(os.path.join(local_dir, "notes.txt"), "w") as f:
                f.write(f"grid: {args.grid}\ntemperature: {args.temp}\nsweep: {args.sweep} {args.name}\n")
                f.write(f"command: {' '.join(sys.argv)}\nbeampos: {args.beampos}\n")
                f.write(f"writer: {args.writer}, frames in {ioc_dir or local_dir}\n")
                f.write(f"start positions: {start}\nimages saved: {len(rows)} of {len(steps)}\n")
                f.write(f"notes: {args.notes}\n")
            print(f"Saved {len(rows)} images; metadata in {local_dir}")


args_settle = 0.3

if __name__ == "__main__":
    main()

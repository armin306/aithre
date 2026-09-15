#!/dls/science/groups/i23/aithre/aithre/.venv/bin/python
import sys
import argparse
import logging
import os
import platform
from datetime import datetime

parser = argparse.ArgumentParser()
parser.add_argument("--dev", help="Development mode for running the GUI outside the lab.", action="store_true")
parser.add_argument("--bluesky", help="Use Bluesky client instead of messy caput/get.", action="store_true")
parser.add_argument("--blueapi", help="Option to use blueapi client instead/aswell as bluesky directly.", action="store_true")
parser.add_argument("--rtc6", help="Acquire the RTC6 board. Off by default; not supported on Windows.", action="store_true")
parser.add_argument("--beampos", help="Override beam position, format: X,Y (e.g. --beampos 1644,1232)", type=str, default=None)
args = parser.parse_args()

beampos_override = None
if args.beampos is not None:
    try:
        _bx, _by = args.beampos.split(",")
        beampos_override = (int(_bx), int(_by))
    except ValueError:
        parser.error("--beampos must be in the form X,Y with integer values (e.g. --beampos 1644,1232)")

if platform.system() == "Windows":
    args.rtc6 = False

# Setup logging
log_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'logs')
log_dir = os.getcwd()
os.makedirs(log_dir, exist_ok=True)
log_filename = datetime.now().strftime('%d%m%Y.log')
log_filepath = os.path.join(log_dir, log_filename)
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(log_filepath, mode='a'),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)

if platform.system() == "Windows":
    logger.info("Windows detected — disabling RTC6 (unsupported on Windows)")

from PyQt5 import QtCore, QtGui, QtWidgets

def _resolve_asset(path):
    if not isinstance(path, str) or not path:
        return path
    if os.path.isabs(path) and os.path.exists(path):
        return path
    bases = [
        getattr(sys, '_MEIPASS', None),
        os.path.dirname(os.path.abspath(__file__)),
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        os.getcwd(),
    ]
    for base in bases:
        if not base:
            continue
        candidate = os.path.normpath(os.path.join(base, path))
        if os.path.exists(candidate):
            return os.path.abspath(candidate)
    return path

_OriginalQPixmap = QtGui.QPixmap
class _AssetQPixmap(_OriginalQPixmap):
    def __init__(self, *a, **kw):
        if a and isinstance(a[0], str):
            a = (_resolve_asset(a[0]),) + a[1:]
        super().__init__(*a, **kw)
QtGui.QPixmap = _AssetQPixmap

import cv2 as cv
from control import ca
import pv
if args.rtc6:
    from rtc6_fastcs import cut_shapes
import math
import numpy as np
import time
from gui_4_3_0 import Ui_MainWindow
import asyncio
from qasync import QEventLoop

import warnings
warnings.filterwarnings("ignore", message="sipPyTypeDict.*")

if args.blueapi:
    from blueapi.client.client import BlueapiClient
    from blueapi.client.rest import BlueapiRestClient
    from blueapi.cli.format import OutputFormat
    from blueapi.worker import Task
    from blueapi.config import ConfigLoader, ApplicationConfig
    from pathlib import Path

    bac = BlueapiClient(BlueapiRestClient())
    plans = bac.get_plans()
    OutputFormat.COMPACT.display(plans)

    config_loader = ConfigLoader(ApplicationConfig)
    config_file = Path("/dls/science/groups/i23/aithre/config.yaml")

    while not config_file.is_file():
        logger.error("Config file not found")
        config_file = input("Please enter config filepath:\n")

    config_loader.use_values_from_yaml(config_file)

dev_mode = args.dev
if dev_mode:
    logger.info("Running in development mode...")

if not args.rtc6:
    logger.info("Running without RTC6 board acquisition")

bluesky_mode = args.bluesky
if bluesky_mode:
    try:
        from mx_bluesky.beamlines.aithre_lasershaping import goniometer_controls
        from mx_bluesky.beamlines.aithre_lasershaping import beamline_safe
        from mx_bluesky.beamlines.aithre_lasershaping.pin_tip_centring import aithre_pin_tip_centre
        from dodal.devices.aithre_lasershaping import goniometer
        from bluesky.run_engine import RunEngine
        from dodal.beamlines import aithre
        from ophyd_async.core import init_devices
    except ImportError as e:
        logger.error("Failed to import mx_bluesky module. Ensure it is installed and accessible.")
        logger.error(f"ImportError: {e}")
        sys.exit(1)
else:
    logger.info("Using dirty caput/get...")

version = "4.3.0"
logger.info(f"Aithre - Version {version}")
OAVADDRESS = "http://bl23i-ea-serv-01.diamond.ac.uk:8080/OAV.mjpg.mjpg"
# Set grid/beam position/scale.
line_width = 2
line_spacing = 115  # depends on pixel size, 60 for MANTA507B
line_color = (140, 140, 140)  # greyness
beamX = 1644
beamY = 1232
if beampos_override is not None:
    beamX, beamY = beampos_override
    logger.info(f"Beam position overridden via --beampos: beamX={beamX}, beamY={beamY}")
feed_width = 4024 if dev_mode else int(ca.caget(pv.oav_max_x)) # reason for keeping full res is to save high def images
display_width = 600 if dev_mode else 2012  # 2012 - emit at half res as too big for display
display_height = 240 if dev_mode else 1528  # 1518
camera_pixel_size = 1.85  # Alvium1240M
feed_display_ratio = feed_width / display_width # should be 2
calibrate = (
    camera_pixel_size / feed_display_ratio
) / 1000  # play around with the end number to find correct

#client = client

# separate thread for OAV
class OAVThread(QtCore.QThread):
    """Thread to handle OAV streaming and processing.
    Emits a signal with the updated QImage for display.
    """
    ImageUpdate = QtCore.pyqtSignal(QtGui.QImage)

    def __init__(self):
        """Initializes the OAVThread with default parameters.
        """
        super(OAVThread, self).__init__()
        self.ThreadActive = False
        self.zoomLevel = 1
        self.beamX = beamX
        self.beamY = beamY
        self.line_width = line_width
        self.line_spacing = line_spacing
        self.line_color = line_color

    def run(self):
        """Main loop for capturing and processing OAV frames.
        Captures frames from the OAV stream, overlays grid lines and beam position,
        applies zoom if necessary, and emits the processed frame as a QImage.
        """
        logger.info("OAVThread started")
        self.ThreadActive = True
        self.cap = cv.VideoCapture(OAVADDRESS)
        while self.ThreadActive:
            ret, frame = self.cap.read()
            if self.ThreadActive and ret:
                for i in range(beamX % line_spacing, frame.shape[1], line_spacing):
                    cv.line(frame, (i, 0), (i, frame.shape[0]), line_color, line_width)
                for i in range(beamY % line_spacing, frame.shape[0], line_spacing):
                    cv.line(frame, (0, i), (frame.shape[1], i), line_color, line_width)

                cv.line(
                    frame,
                    (beamX - 20, beamY),  # bigness
                    (beamX + 20, beamY),
                    (0, 255, 0),  # color
                    2,  # thickness
                )
                cv.line(
                    frame,
                    (beamX, beamY - 20),
                    (beamX, beamY + 20),
                    (0, 255, 0),
                    2,
                )

                if self.zoomLevel != 1:
                    new_width = int(frame.shape[1] / self.zoomLevel)
                    new_height = int(frame.shape[0] / self.zoomLevel)

                    x1 = max(self.beamX - new_width // 2, 0)
                    y1 = max(self.beamY - new_height // 2, 0)
                    x2 = min(self.beamX + new_width // 2, frame.shape[1])
                    y2 = min(self.beamY + new_height // 2, frame.shape[0])

                    x1, x2 = self.adjust_roi_boundaries(
                        x1, x2, frame.shape[1], new_width
                    )
                    y1, y2 = self.adjust_roi_boundaries(
                        y1, y2, frame.shape[0], new_height
                    )

                    cropped_frame = frame[y1:y2, x1:x2]

                    frame = cv.resize(cropped_frame, (frame.shape[1], frame.shape[0]))

                rgbImage = cv.cvtColor(
                    frame, cv.COLOR_BGR2RGB
                )  
                convertToQtFormat = QtGui.QImage(
                    rgbImage.data,
                    rgbImage.shape[1],
                    rgbImage.shape[0],
                    QtGui.QImage.Format_RGB888,
                )
                p = convertToQtFormat
                p = convertToQtFormat.scaled(
                    display_width, display_height, QtCore.Qt.KeepAspectRatio
                ) 
                self.ImageUpdate.emit(p)

    def adjust_roi_boundaries(self, start, end, max_value, window_size):
        """Adjusts the ROI boundaries to ensure they stay within valid limits.

        Args:
            start (int): Starting coordinate of the ROI.
            end (int): Ending coordinate of the ROI.
            max_value (int): Maximum allowable value for the coordinate.
            window_size (int): Target size of the ROI, h or w

        Returns:
            int, int: Adjusted start and end coordinates.
        """
        if start < 0:
            end -= start
            start = 0
        if end > max_value:
            start -= end - max_value
            end = max_value
        if (end - start) < window_size and (start + window_size) <= max_value:
            end = start + window_size
        return start, end

    def setZoomLevel(self, zoomLevel):
        """Handler to update the zoom level.

        Args:
            zoomLevel (int): New zoom level to set.
        """
        self.zoomLevel = zoomLevel

    def stop(self):
        """Stops the OAV thread and releases resources.
        """
        logger.info("Stopping OAVThread")
        self.ThreadActive = False
        self.cap.release()


# separate thread to run caget for RBVs
class RBVThread(QtCore.QThread):
    """Thread to periodically fetch and emit readback values (RBVs) from EPICS PVs.
    Emits a signal with a list of RBV values.
    """
    rbvUpdate = QtCore.pyqtSignal(list)

    def run(self):
        """Main loop for fetching RBVs.
        Periodically fetches RBV values from predefined PVs and emits them."""
        logger.info("RBVThread started")
        while not dev_mode:
            time.sleep(1)
            allRBVsList = []
            allRBVsList += [str(ca.caget(pv.stage_x_rbv))]
            allRBVsList += [str(ca.caget(pv.gonio_y_rbv))]
            allRBVsList += [str(ca.caget(pv.gonio_z_rbv))]
            allRBVsList += [str(ca.caget(pv.omega_rbv))]
            allRBVsList += [str(ca.caget(pv.oav_cam_acqtime_rbv))]
            allRBVsList += [str(ca.caget(pv.oav_cam_gain_rbv))]
            allRBVsList += [str(ca.caget(pv.robot_current_pin_rbv))]
            if (
                ca.caget(pv.robot_pin_mounted) is True
            ):  # need to work out what this pv returns
                allRBVsList += "\u2714"
            elif ca.caget(pv.robot_pin_mounted) is False:
                allRBVsList += "\u274C"
            else:
                allRBVsList += "\u003F"
            allRBVsList += [str(ca.caget(pv.stage_z_rbv))]
            allRBVsList += [str(ca.caget(pv.stage_y_rbv))]
            self.rbvUpdate.emit(allRBVsList)


class LaserStatusThread(QtCore.QThread):
    """Thread to periodically fetch and emit laser status from carbide-fastcs PVs.
    """
    statusUpdate = QtCore.pyqtSignal(dict)

    def __init__(self):
        """Initializes the LaserStatusThread with default parameters.
        """
        super().__init__()
        self.interval = 500
        self._is_running = True

    def fetchStatus(self):
        """Fetches the current laser status from carbide-fastcs PVs.

        Returns:
            dict: A dictionary with status field names as keys and their corresponding values.
        """
        return {
            "IsOutputEnabled": str(ca.caget(pv.carbide_status_is_output_enabled)),
            "ActualShutterState": str(ca.caget(pv.carbide_basic_actual_shutter_state)),
            "ActualOutputFrequency": str(
                ca.caget(pv.carbide_basic_actual_output_frequency)
            ),
            "ActualAttenuatorPercentage": str(
                ca.caget(pv.carbide_basic_actual_attenuator_percentage)
            ),
            "ActualPpDivider": str(ca.caget(pv.carbide_basic_actual_pp_divider)),
            "ActualStateName": str(ca.caget(pv.carbide_status_actual_state_name)),
        }

    def run(self):
        """Main loop for fetching laser status.
        Periodically fetches laser status from carbide-fastcs PVs and emits it.
        """
        logger.info("LaserStatusThread started")
        while self._is_running:
            try:
                status_dict = self.fetchStatus()
                self.statusUpdate.emit(status_dict)
            except Exception as e:
                logger.error(f"Error fetching status: {str(e)}")
            QtCore.QThread.msleep(self.interval)

    def stop(self):
        """Stops the LaserStatusThread.
        """
        logger.info("Stopping LaserStatusThread")
        self._is_running = False


class MainWindow(QtWidgets.QMainWindow):
    """Main application window for the Aithre GUI.
    """
    zoomChanged = QtCore.pyqtSignal(int)

    def __init__(self):
        """Initializes the MainWindow with UI components and connects signals to slots.
        """
        logger.info("Initializing MainWindow")
        super(MainWindow, self).__init__()
        self.ui = Ui_MainWindow()
        self.ui.setupUi(self)
        self.drawn_points = []
        if not dev_mode and args.rtc6:
            logger.info("Acquiring RTC6 board")
            self.rtc6 = cut_shapes.CutShapes()
            self.rtc6Control("acquire")
            self.rtc6Control("check")
            pass
        else:
            logger.info("RTC6 board not acquired (dev mode or --rtc6 flag not set)")
            self.rtc6 = None


        # menus
        self.ui.actionExit.triggered.connect(self.quit)
        # sliders and sensors
        self.ui.sliderExposure.setProperty(
            "value", 0 if dev_mode else str(round(float(ca.caget(pv.oav_cam_acqtime_rbv)) * 100))
        )
        self.ui.sliderGain.setProperty(
            "value", 0 if dev_mode else str(round(float(ca.caget(pv.oav_cam_gain_rbv))))
        )
        # OAV zoom setup
        self.ui.sliderZoom.valueChanged.connect(self.handleZoom)
        # OAV connections thread
        self.zoomLevel = 1
        self.setupOAV()
        logger.info("Starting OAVThread")
        self.OAVth = OAVThread()
        self.OAVth.ImageUpdate.connect(self.setImage)
        self.OAVth.start()
        self.zoomChanged.connect(self.OAVth.setZoomLevel)
        self.canvasMode = "move"
        self.ui.oav_stream.mousePressEvent = self.onMouse
        self.ui.start.clicked.connect(self.oavStart)
        self.ui.stop.clicked.connect(self.oavStop)
        self.ui.snapshot.clicked.connect(self.saveSnapshot)
        self.ui.AutoCenter.clicked.connect(self.autoCenter)
        # RBV updating connections thread
        logger.info("Starting RBVThread")
        RBVth = RBVThread()
        RBVth.rbvUpdate.connect(self.updateRBVs)
        RBVth.start()
        # gonio rotation buttons
        self.ui.buttonSlowOmegaTurn.clicked.connect(lambda: ca.caput(pv.omega_velo, 15))
        self.ui.buttonFastOmegaTurn.clicked.connect(lambda: ca.caput(pv.omega_velo, 40))
        self.ui.plusMinus3600.clicked.connect(self.goTopm3600)
        self.ui.minus180.clicked.connect(lambda: self.gonioRotate(-180))
        self.ui.plus180.clicked.connect(lambda: self.gonioRotate(180))
        self.ui.minus90.clicked.connect(lambda: self.gonioRotate(-90))
        self.ui.plus90.clicked.connect(lambda: self.gonioRotate(90))
        self.ui.minus15.clicked.connect(lambda: self.gonioRotate(-15))
        self.ui.plus15.clicked.connect(lambda: self.gonioRotate(15))
        self.ui.minus5.clicked.connect(lambda: self.gonioRotate(-float(self.ui.doubleSpinBoxOmegaJog.value())))
        self.ui.plus5.clicked.connect(lambda: self.gonioRotate(float(self.ui.doubleSpinBoxOmegaJog.value())))
        self.ui.zero.clicked.connect(lambda: self.gonioRotate(0))
        # jog buttons
        self.ui.up.clicked.connect(lambda: self.jogSample("up"))
        self.ui.down.clicked.connect(lambda: self.jogSample("down"))
        self.ui.left.clicked.connect(lambda: self.jogSample("left"))
        self.ui.right.clicked.connect(lambda: self.jogSample("right"))
        self.ui.pushButtonZsMinus.clicked.connect(lambda: self.jogSample("ZsMinus"))
        self.ui.pushButtonZsPlus.clicked.connect(lambda: self.jogSample("ZsPlus"))
        self.ui.pushButtonZsZero.clicked.connect(lambda: self.jogSample("ZsZero"))
        self.ui.pushButtonZMinus.clicked.connect(lambda: self.jogSample("ZMinus"))
        self.ui.pushButtonZPlus.clicked.connect(lambda: self.jogSample("ZPlus"))
        # exposure and gain sliders
        self.ui.sliderExposure.valueChanged.connect(self.changeExposureGain)
        self.ui.sliderGain.valueChanged.connect(self.changeExposureGain)
        self.ui.zeroAll.clicked.connect(self.returntozero)
        # robot buttons
        self.ui.resetRobot.clicked.connect(lambda: ca.caput(pv.robot_reset, 1))
        self.ui.load.clicked.connect(self.loadNextPin)
        self.ui.unload.clicked.connect(self.unloadPin)
        self.ui.dry.clicked.connect(self.dryGripper)
        # laser buttons
        self.ui.pushButtonDisableLaser.clicked.connect(lambda: self.commandLaser("Disable"))
        self.ui.pushButtonEnableLaser.clicked.connect(lambda: self.commandLaser("Enable"))
        self.ui.pushButtonSetDivider.clicked.connect(lambda: self.commandLaser("SetDivider"))
        self.ui.pushButtonSetAttenuator.clicked.connect(lambda: self.commandLaser("SetAttenuator"))
        self.ui.pushButtonStartupLaser.clicked.connect(lambda: self.commandLaser("Startup"))
        self.ui.pushButtonStandbyLaser.clicked.connect(lambda: self.commandLaser("Standby"))
        # move/draw options
        self.ui.radioButtonMoveMode.toggled.connect(lambda: self.toggleCanvasMode("move"))
        self.ui.radioButtonDrawMode.toggled.connect(lambda: self.toggleCanvasMode("draw"))
        self.ui.pushButtonClear.clicked.connect(lambda: self.drawn_points.clear())
        self.ui.pushButtonCut.clicked.connect(self.savePoints)
        self.ui.pushButtonLoadPreset.clicked.connect(self.loadPresetShape)
        # RTC6 speed control
        self.ui.comboBoxSpeed.currentTextChanged.connect(self.setRTC6Speed)

        if not dev_mode:
            logger.info("Starting LaserStatusThread")
            self.laserStatusThread = LaserStatusThread()
            self.laserStatusThread.statusUpdate.connect(self.updateLaserStatus)
            self.laserStatusThread.start()

    def updateLaserStatus(self, status_dict):
        """Updates the laser status indicators in the UI based on the provided status dictionary.

        Args:
            status_dict (dict): A dictionary containing laser status information.
        """
        if status_dict["IsOutputEnabled"] == "Enabled":
            self.ui.labOUTPUT.setStyleSheet("background-color: green")
        else:
            self.ui.labOUTPUT.setStyleSheet("background-color: red")

        if status_dict["ActualShutterState"] == "Opened":
            self.ui.labEMISSION.setStyleSheet("background-color: green")
        elif status_dict["ActualShutterState"] == "Closed":
            self.ui.labEMISSION.setStyleSheet("background-color: red")
        else:
            self.ui.labEMISSION.setStyleSheet("background-color: yellow")

        self.outputDivider = status_dict["ActualPpDivider"]
        self.outputFrequency = status_dict["ActualOutputFrequency"]
        self.DivFreq = f"{(self.outputDivider)} / {str(np.round(float(self.outputFrequency), 2))} Hz"
        self.ui.labDividerRBV.setText(self.DivFreq)
        self.outputAttenuator = status_dict["ActualAttenuatorPercentage"]
        self.ui.labAttenuatorRBV.setText(str(np.round(float(self.outputAttenuator), 1)))
        self.ui.labLaserStatus.setText(status_dict["ActualStateName"])

    def closeEvent(self, event):
        """Handles the close event for the main window.

        Args:
            event (QCloseEvent): The close event.
        """
        logger.info("MainWindow closing, stopping threads...")
        self.laserStatusThread.stop()
        self.laserStatusThread.quit()
        self.laserStatusThread.wait()
        logger.info("All threads stopped, application closing")
        event.accept()

    def rtc6Control(self, command):
        if command == "acquire":
            logger.info("RTC6: Connecting to RTC6 board")
            self.rtc6.connect_to_rtc()
        if command == "check":
            is_acquired = ca.caget(pv.rtc6eth_info_is_acquired)
            logger.info(f"RTC6: Board acquisition status - {is_acquired}")
            if is_acquired == "True":
                self.ui.labRTC6Acquired.setStyleSheet("background-color: green")
            elif is_acquired == "False":
                self.ui.labRTC6Acquired.setStyleSheet("background-color: red")
            else:
                pass

    def commandLaser(self, command):
        """Sends a command to the laser control system.

        Args:
            command (str): The command to send to the laser. Options include "Enable", "Disable",
                           "SetDivider", "SetAttenuator", "Startup", and "Standby".
        """
        logger.info(f"Laser: Sending command '{command}'")
        if command == "Enable":
            logger.info("Laser: Enabling output")
            ca.caput(pv.carbide_actions_enable_output, 1, True)
        elif command == "Disable":
            logger.info("Laser: Disabling output")
            ca.caput(pv.carbide_actions_close_output, 1, True)
        elif command == "SetDivider":
            divider = int(self.ui.spinBoxDivider.value())
            logger.info(f"Laser: Setting divider to {divider}")
            ca.caput(pv.carbide_basic_target_pp_divider, divider)
        elif command == "SetAttenuator":
            percentage = float(self.ui.doubleSpinBoxAttenuator.value())
            logger.info(f"Laser: Setting attenuator to {percentage}%")
            ca.caput(pv.carbide_basic_target_attenuator_percentage, percentage)
        elif command == "Startup":
            logger.info("Laser: Startup")
            ca.caput(pv.carbide_basic_selected_preset_index, 5)
            ca.caput(pv.carbide_actions_apply_selected_preset, 1, True)
        elif command == "Standby":
            logger.info("Laser: Standby")
            ca.caput(pv.carbide_actions_go_to_standby, 1, True)


    def loadNextPin(self):
        pin_number = int(self.ui.spinToLoad.value())
        logger.info(f"Robot: Loading pin {pin_number}")
        if bluesky_mode:
            with init_devices():
                gonio = aithre.goniometer()
                robot = aithre.robot()

            RE = RunEngine({})
            goniometer.omega.stop()
        else:
            ca.caput(pv.robot_reset, 1, True)
            time.sleep(3)
            ca.caput(pv.robot_next_pin, pin_number)
            time.sleep(3)
            ca.caput(pv.robot_proc_load, 1, True)
            logger.info(f"Robot: Load command sent for pin {pin_number}")

    def unloadPin(self):
        logger.info("Robot: Unloading pin")
        ca.caput(pv.robot_reset, 1, True)
        time.sleep(3)
        ca.caput(pv.robot_proc_unload, 1, True)

    def dryGripper(self):
        logger.info("Robot: Drying gripper")
        ca.caput(pv.robot_reset, 1, True)
        time.sleep(3)
        ca.caput(pv.robot_proc_dry, 1, True)

    def quit(self):
        """Quits the application gracefully.
        """
        logger.info("Aithre shutting down... Bye!")
        sys.exit()

    def returntozero(self):
        if bluesky_mode:
            logger.info("Bluesky - go to zero")
            beamline_safe.go_to_zero(wait=False)
        else:
            logger.info("Moving all motors to zero")
            for motor in [pv.gonio_y, pv.gonio_z, pv.stage_x, pv.stage_z, pv.omega]:
                ca.caput(motor, 0)

    def handleZoom(self, zoomValue):
        """Handles the zoom level change from the slider.
        
        Args:
            zoomValue (int): The new zoom level from the slider.
        """
        logger.debug(f"Zoom level changed to {zoomValue}")
        self.zoomLevel = zoomValue
        self.ui.currentZoom.setText(str(self.zoomLevel))
        self.zoomChanged.emit(self.zoomLevel)

    def changeExposureGain(self):
        exposure = self.ui.sliderExposure.value() / 100
        gain = self.ui.sliderGain.value()
        logger.debug(f"OAV: Changing exposure to {exposure}, gain to {gain}")
        ca.caput(pv.oav_cam_acqtime, exposure)
        ca.caput(pv.oav_cam_gain, gain)

    def go_to_max():
        bac.create_and_start_task(Task(name="go_to_furthest_maximum"))
    def jogSample(self, direction, amount=0.005):

        if direction == "ZsPlus" or "ZsMinus" or "ZsZero":
            jogVal = float(self.ui.spinBoxZsJogAmount.value() / 1000)
            if direction == "ZsPlus":
                logger.debug(f"Stage: Moving Z+ by {jogVal}")
                ca.caput(pv.stage_z, (float(ca.caget(pv.stage_z_rbv)) + jogVal))
            elif direction == "ZsMinus":
                logger.debug(f"Stage: Moving Z- by {jogVal}")
                ca.caput(pv.stage_z, (float(ca.caget(pv.stage_z_rbv)) - jogVal))
            elif direction == "ZsZero":
                logger.debug(f"Stage: Moving Z to zero")
                ca.caput(pv.stage_z, float(0))

        if args.blueapi:
            bac.create_and_start_task(Task(
                name="jog_sample",
                params={"direction": direction, "increment_size": amount},
            ))
        if bluesky_mode:
            with init_devices():
                gonio = aithre.goniometer()
            RE = RunEngine({})
            RE(goniometer_controls.jog_sample(direction=direction, increment_size=amount, goniometer=gonio))

        else:
            jogVal = float(self.ui.spinBoxZJogAmount.value() / 1000)
            if direction == "right":
                ca.caput(pv.stage_x, (float(ca.caget(pv.stage_x_rbv)) + jogVal))
            elif direction == "left":
                ca.caput(pv.stage_x, (float(ca.caget(pv.stage_x_rbv)) - jogVal))
            elif direction == "up":
                ca.caput(
                    pv.gonio_y,
                    (float(ca.caget(pv.gonio_y_rbv)))
                    + ((math.sin(math.radians(float(ca.caget(pv.omega_rbv)))))) * jogVal,
                )
                ca.caput(
                    pv.gonio_z,
                    (float(ca.caget(pv.gonio_z_rbv)))
                    + ((math.cos(math.radians(float(ca.caget(pv.omega_rbv)))))) * jogVal,
                )
            elif direction == "down":
                ca.caput(
                    pv.gonio_y,
                    (float(ca.caget(pv.gonio_y_rbv)))
                    - ((math.sin(math.radians(float(ca.caget(pv.omega_rbv)))))) * jogVal,
                )
                ca.caput(
                    pv.gonio_z,
                    (float(ca.caget(pv.gonio_z_rbv)))
                    - ((math.cos(math.radians(float(ca.caget(pv.omega_rbv)))))) * jogVal,
                )
            elif direction == "ZPlus":
                ca.caput(
                    pv.gonio_y,
                    (float(ca.caget(pv.gonio_y_rbv)))
                    - ((math.cos(math.radians(float(ca.caget(pv.omega_rbv)))))) * jogVal,
                )
                ca.caput(
                    pv.gonio_z,
                    (float(ca.caget(pv.gonio_z_rbv)))
                    + ((math.sin(math.radians(float(ca.caget(pv.omega_rbv)))))) * jogVal,
                )
            elif direction == "ZMinus":
                ca.caput(
                    pv.gonio_y,
                    (float(ca.caget(pv.gonio_y_rbv)))
                    + ((math.cos(math.radians(float(ca.caget(pv.omega_rbv)))))) * jogVal,
                )
                ca.caput(
                    pv.gonio_z,
                    (float(ca.caget(pv.gonio_z_rbv)))
                    - ((math.sin(math.radians(float(ca.caget(pv.omega_rbv)))))) * jogVal,
                )
            # elif direction == "ZPlus":
            #     ca.caput(pv.stage_z, (float(ca.caget(pv.stage_z_rbv)) + 0.05))
            # elif direction == "ZMinus":
            #     ca.caput(pv.stage_z, (float(ca.caget(pv.stage_z_rbv)) - 0.05))
            else:
                pass

    def goTopm3600(self):
        gonio_current = float(ca.caget(pv.omega_rbv))
        if gonio_current <= 0:
            gonio_request = 3600
        else:
            gonio_request = -3600
        logger.info(f"Moving gonio omega to {gonio_request}")
        ca.caput(pv.omega, gonio_request)

    def toggleCanvasMode(self, mode):
        """Toggles the canvas mode between 'move' and 'draw'.

        Args:
            mode (str): The mode to set, either 'move' or 'draw'.
        """
        logger.debug(f"Canvas mode changed to '{mode}'")
        if mode == "move":
            self.canvasMode = "move"
        elif mode == "draw":
            self.canvasMode = "draw"
        else:
            self.canvasMode = "move"

    def onMouse(self, event):
        """Handles mouse click events on the OAV stream for moving the stage or drawing points.

        Args:
            event (QMouseEvent): The mouse event containing position information.
        """
        if self.canvasMode == "move":
            self.zoomclickcal = int(self.ui.sliderZoom.value())
            if self.zoomclickcal == 1:
                self.xcent = beamX
                self.ycent = beamY
            else:
                self.xcent = 2012
                self.ycent = 1518
            x = event.pos().x()
            x = x * feed_display_ratio
            y = event.pos().y()
            y = y * feed_display_ratio
            x_curr = float(ca.caget(pv.stage_x_rbv))
            #print(x_curr)
            y_curr = float(ca.caget(pv.gonio_y_rbv))
            z_curr = float(ca.caget(pv.gonio_z_rbv))
            omega = float(ca.caget(pv.omega_rbv))
            logger.info(f"Clicked at x={x}, y={y}")
            Xmove = x_curr + ((x - self.xcent) * (calibrate / self.zoomclickcal))
            Ymove = y_curr + (math.sin(math.radians(omega)) * ((y - self.ycent) * (calibrate / self.zoomclickcal)))
            Zmove = z_curr + (math.cos(math.radians(omega)) * ((y - self.ycent) * (calibrate / self.zoomclickcal)))
            logger.info(f"Moving to X={Xmove}, Y={Ymove}, Z={Zmove}")
            ca.caput(pv.stage_x, round(Xmove, 4))
            ca.caput(pv.gonio_y, round(Ymove, 4))
            ca.caput(pv.gonio_z, round(Zmove, 4))
        elif self.canvasMode == "draw":
            self.drawn_points.append(event.pos())
            self.redrawPoints()
        else:
            pass

    def redrawPoints(self):
        """Redraws the drawn points on the current image and updates the display.
        """
        if self.image is not None:
            painter = QtGui.QPainter(self.image)
            painter.setPen(QtGui.QPen(QtGui.QColor(255, 0, 0), 2))
            if len(self.drawn_points) < 2:
                for point in self.drawn_points:
                    painter.drawPoint(point)
            if len(self.drawn_points) > 1:
                for i in range(len(self.drawn_points) - 1):
                    painter.drawLine(self.drawn_points[i], self.drawn_points[i + 1])
            painter.end()
            self.ui.oav_stream.setPixmap(QtGui.QPixmap.fromImage(self.image))

    def savePoints(self):
        """Sends drawn points to the RTC6 for cutting.
        """
        points_list = []
        now = datetime.now()
        filename = now.strftime("%Y%m%d_%H%M%S_points.txt")
        for i, point in enumerate(self.drawn_points):
            correctedX = -((beamX / feed_display_ratio) - point.x()) * camera_pixel_size
            correctedY = ((beamY / feed_display_ratio) - point.y()) * camera_pixel_size 
            if i == 0:
                points_list.append((correctedX, correctedY, False))
            else:
                points_list.append((correctedX, correctedY, True))
        
        if points_list:
            self.points_list = points_list * self.ui.spinBoxRepetitions.value() + ([(0, 0, False)])
            if args.rtc6:
                self.rtc6.cut_polygon_from_gui(self.points_list)
                logger.info(f"Cutting polygon: {self.points_list}")
            else:
                logger.info("Running in no RTC6 mode, but here are the points that would have been cut:")
                logger.info(f"Points: {self.points_list}")
        else:
            logger.warning("No shapes to cut...")
        
    def loadPresetShape(self):
        """Opens a file dialog to load a preset shape file and displays the filename.
        """
        options = QtWidgets.QFileDialog.Options()
        options |= QtWidgets.QFileDialog.DontUseNativeDialog
        file_name, _ = QtWidgets.QFileDialog.getOpenFileName(
            self.ui.centralwidget,
            "Select Preset Shape File",
            "/dls/science/groups/i23/aithre/rtc6-fastcs/shape_protocols/",
            "Text Files (*.txt);;All Files (*)",
            options=options,
        )
        
        if file_name:
            display_name = os.path.basename(file_name)
            display_name = os.path.splitext(display_name)[0]
            if display_name.startswith("RTCExecutionlist_"):
                display_name = display_name[len("RTCExecutionlist_"):]
            self.ui.labPresetShapeFile.setText(display_name)
            self.preset_file_path = file_name
            logger.info(f"Preset shape file loaded: {file_name}")
        else:
            logger.debug("File selection cancelled")
    
    def setRTC6Speed(self, speed_text):
        """Sets the RTC6 mark speed based on the comboBox selection.
        
        Args:
            speed_text (str): The text from the comboBox (e.g., "0.005 m/s" or "Default")
            This is converted from m/s to bits at fastcs level.
        """
        if speed_text == "Default" or not speed_text:
            logger.debug("RTC6 speed: Default selected, no caput performed")
            return
        
        if speed_text.endswith(" m/s"):
            speed_value = speed_text[:-len(" m/s")]
            try:
                speed_float = float(speed_value)
                logger.info(f"RTC6: Setting mark speed to {speed_float} m/s")
                ca.caput(pv.rtc6eth_control_markspeed, speed_float)
            except ValueError:
                logger.error(f"RTC6: Invalid speed value '{speed_value}'")
        else:
            logger.warning(f"RTC6: Unexpected speed format '{speed_text}'")
        
                    
    def setupOAV(self):
        """Sets up the OAV camera parameters and disables unnecessary callbacks if not in development mode.
        """
        logger.info("Setting up OAV camera")
        if not dev_mode:
            logger.info("Disabling OAV callbacks")
            for callback in (
                pv.oav_roi_ecb,
                pv.oav_arr_ecb,
                pv.oav_stat_ecb,
                pv.oav_proc_ecb,
                pv.oav_fimg_ecb,
                pv.oav_tiff_ecb,
                pv.oav_hdf5_ecb,
                #pv.oav_pva_ecb,
            ):
                ca.caput(callback, "Disable")
            ca.caput(pv.oav_mjpg_maxw, 4024)
            ca.caput(pv.oav_mjpg_maxh, 3036)
            logger.info("OAV camera setup complete")

    def oavStart(self):
        """Starts the OAV acquisition by setting the appropriate EPICS PV.
        """
        logger.info("OAV: Starting acquisition")
        ca.caput(pv.oav_acquire, "Acquire")

    def oavStop(self):
        """Stops the OAV acquisition by setting the appropriate EPICS PV.
        """
        logger.info("OAV: Stopping acquisition")
        ca.caput(pv.oav_acquire, "Done")

    def setImage(self, image):
        """Sets the current image to be displayed in the OAV stream.

        Args:
            image (QImage): The QImage to display.
        """
        self.image = image
        self.redrawPoints()
        self.ui.oav_stream.setPixmap(QtGui.QPixmap.fromImage(image))

    def saveSnapshot(self):
        """Saves the current OAV image as a JPEG file.
        Prompts the user for a file name and saves the image using OpenCV.
        """
        image = self.image
        logger.debug(f"Q image format: {image.format()}")
        logger.debug(f"Q image bytes: {image.byteCount()}")
        logger.debug(f"Q image bytes per line: {image.bytesPerLine()}")
        width = image.width()
        height = image.height()
        bytesPerLine = image.bytesPerLine()
        data = image.bits().asstring(height * bytesPerLine)
        arr = np.frombuffer(data, dtype=np.uint8).reshape((height, width, 3))
        options = QtWidgets.QFileDialog.Options()
        options |= QtWidgets.QFileDialog.DontUseNativeDialog
        file_name, _ = QtWidgets.QFileDialog.getSaveFileName(
            self.ui.centralwidget,
            "QFileDialog.getSaveFileName()",
            "",
            "JPEG Files (*.jpg);;All Files (*)",
            options=options,
        )

        if file_name:
            _, file_extension = os.path.splitext(file_name)
            if not file_extension:
                file_name += ".jpg"
            try:
                result = cv.imwrite(file_name, arr)
                if result:
                    logger.info(f"Image saved successfully to {file_name}")
                else:
                    logger.error("Failed to save image. Try as a .jpg")
            except Exception as e:
                logger.error(f"An error occurred while saving the image: {e}")

    def gonioRotate(self, amount):
        if args.blueapi:
            bac.create_and_start_task(
                Task(name="rotate_gonio_relative", params={"value": amount})
            )
        else:
            gonio_current = float(ca.caget(pv.omega_rbv))
            if amount == 0:
                gonio_request = 0
            else:
                gonio_request = gonio_current + amount
            logger.info(f"Moving gonio omega to {gonio_request}")
            ca.caput(pv.omega, gonio_request)

    def updateRBVs(self, rbvs):
        # stagex, gony, gonz, omega, oavexp, oavgain, currentsamp, goniosens, stagez, stagey
        self.ui.stagex_rbv.setText(
            str(round(float(rbvs[0]), 3))
        )  # x and z may be confused
        self.ui.stagez_rbv.setText(str(round(float(rbvs[8]), 3)))
        self.ui.gony_rbv.setText(str(round(float(rbvs[1]), 3)))
        self.ui.gonz_rbv.setText(str(round(float(rbvs[2]), 3)))
        # stop -0.0 to 0.0 jitter on GUI
        if round(float(rbvs[3]), 0) == -0.0:
            self.ui.omega_rbv.setText("0.0")
        else:
            self.ui.omega_rbv.setText(str(round(float(rbvs[3]), 0)))
        self.ui.exposure_rbv.setText(str(round(float(rbvs[4]), 3)))
        self.ui.gain_rbv.setText(str(int(rbvs[5])))
        self.ui.currentSamp.setText(str(rbvs[6]))
        blsafe = all(round(float(rbvs[x]), 3) == 0.00 for x in [0, 1, 2, 3, 8, 9])
        if blsafe:
            ca.caput(pv.robot_ip16_force_option, "On")
            self.ui.indicatorBeamlineSafe.setStyleSheet("background-color: green")
        else:
            # ca.caput(pv.robot_ip16_force_option, "No")
            self.ui.indicatorBeamlineSafe.setStyleSheet("background-color: red")
        if ca.caget(pv.robot_pin_mounted) == "Yes":
            self.ui.indicatorGonioSensor.setStyleSheet("background-color: green")
        else:
            self.ui.indicatorGonioSensor.setStyleSheet("background-color: red")

    def autoCenter(self):
        logger.info("Auto-centering pin tip")
        from dodal.devices.oav.pin_image_recognition import PinTipDetection
        with init_devices():
            gonio = aithre.goniometer()
            oav = aithre.oav()
            p_t_d = PinTipDetection("LA18L-DI-OAV-01:", "pin_detect")

        RE = RunEngine({})
        RE(aithre_pin_tip_centre(gonio=gonio, oav=oav, pin_tip_detection=p_t_d, tip_offset_microns=0))
        logger.info("Auto-center complete")
    
        return None


if __name__ == "__main__":
    app = QtWidgets.QApplication(sys.argv)
    loop = QEventLoop(app)  # Create QEventLoop
    asyncio.set_event_loop(loop)
    mainWin = MainWindow()
    mainWin.show()
    with loop:
        loop.run_forever()
    sys.exit(app.exec_())


## TO DO:
# work out why RTC6 is always acquired.
# change rtc6-fastcs to take the file from shape_protocols rather than translating. this will be faster for multi passes.

import win32com.client
from typing import TypeAlias
import logging
import astropy.units as u
from enum import Flag, Enum
from utils import AscomDriverInfo, RepeatTimer, return_with_status
from utils import Activities, init_log, TimeStamped
from mastapi import Mastapi

CameraType: TypeAlias = "Camera"


class CameraState(Enum):
    """
    Camera states as per https://ascom-standards.org/Help/Developer/html/T_ASCOM_DeviceInterface_CameraStates.htm
    """
    Idle = 0
    Waiting = 1
    Exposing = 2
    Reading = 3
    Download = 4
    Error = 5


class CameraActivities(Flag):
    Idle = 0
    CoolingDown = (1 << 0)
    WarmingUp = (1 << 1)
    Exposing = (1 << 2)
    ShuttingDown = (1 << 3)
    StartingUp = (1 << 4)
    ReadingOut = (1 << 5)


class CameraStatus(TimeStamped):

    is_operational: bool
    temperature: float
    cooler_power: float  # percent
    state: CameraState

    reasons: list[str]

    def __init__(self, c: CameraType):
        self.ascom = AscomDriverInfo(c.ascom)
        self.reasons = list()
        self.is_powered = True
        self.is_operational = False
        if self.is_powered:
            self.is_connected = c.connected
            if self.is_connected:
                set_point = c.operational_set_point
                self.temperature = c.ascom.CCDTemperature
                self.is_operational = abs(self.temperature - set_point) <= 0.5
                if not self.is_operational:
                    self.reasons.append(f'temperature: abs({self.temperature} - {set_point}) > 0.5 deg')
                self.cooler_power = c.ascom.CoolerPower
                self.state_verbal = str(CameraState(c.ascom.CameraState))
            else:
                self.reasons.append('not-connected')
        else:
            self.is_operational = False
            self.is_connected = False
            self.reasons.append('not-powered')
            self.reasons.append('not-connected')
        self.activities = c.activities
        self.activities_verbal = self.activities.name
        self.timestamp()


class Camera(Mastapi, Activities):

    logger: logging.Logger
    _connected: bool = False
    _is_exposing: bool = False
    operational_set_point = -25
    warm_set_point = 5  # temperature at which the camera is considered warm
    _image_width: int = None
    _image_height: int = None
    PixelSizeX: int
    PixelSizeY: int
    NumX: int
    NumY: int
    RadX: float
    RadY: float
    ascom = None
    timer: RepeatTimer
    image = None
    last_state: CameraState = None
    activities: CameraActivities = CameraActivities.Idle
    is_powered: bool = True

    def __init__(self, driver: str):
        self.logger = logging.getLogger('mast.unit.camera')
        init_log(self.logger)
        try:
            self.ascom = win32com.client.Dispatch(driver)
        except Exception as ex:
            self.logger.exception(ex)
            raise ex

        # PoweredDevice.__init__(self, 'Camera', self)

        self.timer = RepeatTimer(1, function=self.ontimer)
        self.timer.name = 'camera-timer-thread'
        self.timer.start()
        self.logger.info('initialized')

    @property
    def connected(self) -> bool:
        return self.ascom and self.ascom.Connected

    @connected.setter
    def connected(self, value: bool):
        if not self.is_powered:
            return

        if self.ascom is not None:
            self.ascom.connected = value
        if value:
            self.PixelSizeX = self.ascom.PixelSizeX
            self.PixelSizeY = self.ascom.PixelSizeY
            self.NumX = self.ascom.NumX
            self.NumY = self.ascom.NumY
            self.RadX = (self.PixelSizeX * self.NumX * u.arcsec).to(u.rad).value
            self.RadY = (self.PixelSizeY * self.NumY * u.arcsec).to(u.rad).value
        self.logger.info(f'connected = {value}')

    @return_with_status
    def connect(self):
        """
        Connects to the **MAST** camera

        :mastapi:
        Returns
        -------

        """
        if self.is_powered:
            self.connected = True

    @return_with_status
    def disconnect(self):
        """
        Disconnects from the **MAST* camera

        :mastapi:
        """
        if self.is_powered:
            self.connected = False

    @return_with_status
    def start_exposure(self, seconds: int):
        """
        Starts a **MAST** camera exposure

        Parameters
        ----------
        seconds
            Exposure length in seconds

        :mastapi:
        """
        if self.connected:
            self.start_activity(CameraActivities.Exposing, self.logger)
            self.image = None

            # readout mode, binning, gain?

            self.ascom.StartExposure(seconds, True)
            self.logger.info(f'exposure started (seconds={seconds})')

    @return_with_status
    def abort_exposure(self):
        """
        Aborts the current **MAST** camera exposure. No image readout.

        :mastapi:
        """
        if not self.connected:
            return

        if self.ascom.CanAbortExposure:
            try:
                self.ascom.AbortExposure()
            except Exception as ex:
                self.logger .exception(f'failed to stop exposure', ex)
        else:
            self.logger.info(f'ASCOM camera "{self.ascom.Name}" cannot stop exposure')
        self.end_activity(CameraActivities.Exposing, self.logger)

    @return_with_status
    def stop_exposure(self):
        """
        Stops the current **MAST** camera exposure.  An image readout is initiated

        :mastapi:
        """
        if not self.connected:
            return

        if self.is_active(CameraActivities.Exposing):
            self.ascom.StopExposure()  # the timer will read the image

    def status(self) -> CameraStatus:
        """
        Gets the **MAST** camera status

        :mastapi:
        Returns
        -------

        """
        return CameraStatus(self)

    @return_with_status
    def startup(self):
        """
        Starts the **MAST** camera up (cooling down , if needed)

        :mastapi:

        """
        self.start_activity(CameraActivities.StartingUp, self.logger)
        if not self.connected:
            self.connect()
        if self.connected:
            self.ascom.CoolerOn = True
            if abs(self.ascom.CCDTemperature - self.operational_set_point) > 0.5:
                self.cooldown()

    @return_with_status
    def cooldown(self):
        if not self.ascom.Connected:
            return

        self.start_activity(CameraActivities.CoolingDown, self.logger)
        # Turn on cooler
        if not self.ascom.CoolerOn:
            self.logger.info(f'cool-down: cooler ON')
            self.ascom.CoolerOn = True

        if self.ascom.CanSetCCDTemperature:
            self.logger.info(f'cool-down: setting set-point to {self.operational_set_point:.1f}')
            self.ascom.SetCCDTemperature = self.operational_set_point

    @return_with_status
    def shutdown(self):
        """
        Shuts the **MAST** camera down (warms up, if needed)

        :mastapi:
        """
        if self.connected:
            self.start_activity(CameraActivities.ShuttingDown, self.logger)
            if abs(self.ascom.CCDTemperature - self.warm_set_point) > 0.5:
                self.warmup()

    @return_with_status
    def warmup(self):
        """
        Warms the **MAST** camera up, to prevent temperature shock
        """
        if not self.connected:
            return

        if self.ascom.CanSetCCDTemperature:
            self.start_activity(CameraActivities.WarmingUp, self.logger)
            temp = self.ascom.CCDTemperature

            self.logger.info(
                f'warm-up started: current temp: {temp:.1f}, setting set-point to {self.warm_set_point:.1f}')
            self.ascom.SetCCDTemperature(self.warm_set_point)

    def abort(self):
        """
        :mastapi:
        Returns
        -------

        """
        if self.is_active(CameraActivities.Exposing):
            self.ascom.AbortExposure()
            self.end_activity(CameraActivities.Exposing, self.logger)

    def ontimer(self):
        """
        Called by timer, checks if any ongoing activities have changed state
        """
        if not self.connected:
            return

        if self.last_state is None:
            self.last_state = self.ascom.CameraState
            self.logger.info(f'state changed from None to {CameraState(self.last_state)}')
        else:
            state = self.ascom.CameraState
            if not state == self.last_state:
                percent = ''
                if state == CameraState.Exposing or state == CameraState.Waiting or state == CameraState.Reading or \
                        state == CameraState.Download:
                    percent = f'{self.ascom.PercentCompleted} %'
                self.logger.info(f'state changed from {CameraState(self.last_state)} to {CameraState(state)} {percent}')
                self.last_state = state

        if self.is_active(CameraActivities.Exposing) and self.ascom.ImageReady:
            self.image = self.ascom.ImageArray
            self.logger.info(f'image acquired (shutter was open for {self.ascom.LastExposureDuration} seconds)')
            self.end_activity(CameraActivities.Exposing, self.logger)

        if self.is_active(CameraActivities.CoolingDown):
            temp = self.ascom.CCDTemperature
            if temp <= self.operational_set_point:
                self.end_activity(CameraActivities.CoolingDown, self.logger)
                self.end_activity(CameraActivities.StartingUp, self.logger)
                self.logger.info(f'cool-down: done (temperature={temp:.1f}, set-point={self.operational_set_point})')

        if self.is_active(CameraActivities.WarmingUp):
            temp = self.ascom.CCDTemperature
            if temp >= self.warm_set_point:
                self.ascom.CoolerOn = False
                self.logger.info('turned cooler OFF')
                self.end_activity(CameraActivities.WarmingUp, self.logger)
                self.end_activity(CameraActivities.ShuttingDown, self.logger)
                self.logger.info(f'warm-up done (temperature={temp:.1f}, set-point={self.warm_set_point})')

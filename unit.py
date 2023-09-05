import psutil
import logging
import subprocess
import socket
from multiprocessing.shared_memory import SharedMemory
from utils import ensure_process_is_running, RepeatTimer, find_process, Activities, return_with_status, init_log
import time
import os
import guiding
import json
from enum import Flag
from threading import Thread
from camera import Camera, CameraActivities
import numpy as np
import datetime
from PlaneWave import pwi4_client


class UnitActivities(Flag):
    Idle = 0
    Autofocusing = (1 << 0)
    Guiding = (1 << 1)
    StartingUp = (1 << 2)
    ShuttingDown = (1 << 3)


class Unit(Activities):

    logger: logging.Logger
    MAX_UNITS = 20

    _is_guiding: bool = False

    pw: pwi4_client.PWI4
    timer: RepeatTimer
    plate_solver_process: psutil.Process | subprocess.Popen

    # Stuff for plate solving
    image_shm: SharedMemory = None
    was_tracking_before_guiding: bool
    sock_to_solver = None

    GUIDING_EXPOSURE_SECONDS = 5
    GUIDING_INTER_EXPOSURE_SECONDS = 30

    def __init__(self, unit_id: int):
        self.logger = logging.getLogger('mast.unit')
        init_log(self.logger)
        if unit_id < 0 or unit_id > self.MAX_UNITS:
            raise f'Unit id must be between 0 and {self.MAX_UNITS}'

        self.id = unit_id
        try:
            self.camera = Camera('ASCOM.PlaneWaveVirtual.Camera')
        except Exception as ex:
            self.logger.exception(msg='could not create a Unit', exc_info=ex)
            raise ex

        self.pw = pwi4_client.PWI4()
        self.activities = UnitActivities.Idle
        self.timer = RepeatTimer(2, function=self.ontimer)
        self.timer.name = 'unit-timer-thread'
        self.timer.start()
        self.logger.info('initialized')

    def ontimer(self):

        if self.is_active(UnitActivities.StartingUp):
            if not (self.camera.is_active(CameraActivities.StartingUp)):
                self.end_activity(UnitActivities.StartingUp, self.logger)

        if self.is_active(UnitActivities.ShuttingDown):
            if not (self.camera.is_active(CameraActivities.ShuttingDown)):
                self.end_activity(UnitActivities.ShuttingDown, self.logger)

    def end_lifespan(self):
        self.logger.info('unit end lifespan')
        if 'plate_solver_process' in self.__dict__.keys() and self.plate_solver_process:
            try:
                self.plate_solver_process.kill()
            except psutil.NoSuchProcess:
                pass

        # self.image_shm.close()
        # self.image_shm.unlink()

        self.camera.ascom.Connected = False

    def start_lifespan(self):
        self.logger.debug('unit start lifespan')
        # ensure_process_is_running(pattern='PWI4',
        #                           cmd='C:/Program Files (x86)/PlaneWave Instruments/PlaneWave Interface 4/PWI4.exe',
        #                           logger=self.logger)
        # ensure_process_is_running(pattern='PWShutter',
        #                           cmd="C:/Program Files (x86)/PlaneWave Instruments/PlaneWave Shutter Control/PWShutter.exe",
        #                           logger=self.logger,
        #                           shell=True)

    def do_startup(self):
        self.start_activity(UnitActivities.StartingUp, self.logger)
        self.camera.startup()

    @return_with_status
    def startup(self):
        """
        Starts the **MAST** ``unit`` subsystem.  Makes it ``operational``.

        Returns
        -------

        :mastapi:
        """
        if self.is_active(UnitActivities.StartingUp):
            return

        Thread(name='startup-thread', target=self.do_startup).start()

    def do_shutdown(self):
        self.start_activity(UnitActivities.ShuttingDown, self.logger)
        self.camera.shutdown()

    @return_with_status
    def shutdown(self):
        """
        Shuts down the **MAST** ``unit`` subsystem.  Makes it ``idle``.

        :mastapi:
        """
        if not self.connected:
            self.connect()

        if self.is_active(UnitActivities.ShuttingDown):
            return

        Thread(name='shutdown-thread', target=self.do_shutdown).start()
    def end_guiding(self):
        try:
            self.plate_solver_process.kill()
        except:
            pass

        self.sock_to_solver.shutdown(socket.SHUT_RDWR)
        self.logger.info(f'guiding ended')

    def do_guide(self):

        proc = find_process(patt='PSSimulator')
        if proc:
            proc.kill()
            self.logger.info(f'killed existing plate solving simulator process (pid={proc.pid})')

        sim_dir = os.path.join(os.getcwd(), './PlateSolveSimulator')
        subprocess.Popen(os.path.join(sim_dir, 'run.bat'), cwd=sim_dir, shell=True)
        while True:
            self.plate_solver_process = find_process(patt='PSSimulator')
            if self.plate_solver_process:
                break
            else:
                time.sleep(1)

        self.logger.info(f'plate solver simulator process pid={self.plate_solver_process.pid}')

        # self.logger.info(f"creating server socket ...")
        server = socket.create_server(guiding.guider_address_port, family=socket.AF_INET)
        self.logger.info(f"listening on {guiding.guider_address_port}")
        server.listen()
        # self.logger.info(f"accepting on server socket")
        self.sock_to_solver, address = server.accept()
        # self.logger.info("accepted on server socket")

        # self.logger.info("receiving on server socket")
        s = self.sock_to_solver.recv(1024)
        # self.logger.info(f"received '{s}' on server socket")
        hello = json.loads(s.decode(encoding='utf-8'))
        if not hello['ready']:
            pass  # TBD

        self.logger.info(f'plate solver simulator is ready')

        while self.is_active(UnitActivities.Guiding):
            self.logger.info(f'starting {self.GUIDING_EXPOSURE_SECONDS} seconds guiding exposure')
            self.camera.start_exposure(seconds=self.GUIDING_EXPOSURE_SECONDS)
            while self.camera.is_active(CameraActivities.Exposing):
                if not self.is_active(UnitActivities.Guiding):
                    self.end_guiding()
                    return
                time.sleep(2)

            if not self.is_active(UnitActivities.Guiding):
                self.end_guiding()
                return

            self.logger.info(f'guiding exposure done, getting the image from the camera')
            shared_image = np.ndarray((self.camera.NumX, self.camera.NumY), dtype=np.uint32, buffer=self.image_shm.buf)
            shared_image[:] = self.camera.image[:]
            self.logger.info(f'copied image to shared memory')

            if not self.is_active(UnitActivities.Guiding):
                self.end_guiding()
                return

            pw_status = self.pw.status()
            # try to fool the plate solver by skewing ra and dec ?!?
            ra = pw_status.mount.ra_j2000_hours
            dec = pw_status.mount.dec_j2000_degs

            request = {
                'ra': ra + (20 / (24 * 60 * 60)),
                'dec': dec + (15 / (360 * 60 * 60)),
                'width': self.camera.NumX,
                'height': self.camera.NumY,
            }
            self.sock_to_solver.send(json.dumps(request).encode('utf-8'))

            # plate solver is now solving

            if not self.is_active(UnitActivities.Guiding):
                self.end_guiding()
                return

            # block till the solver is done

            b = self.sock_to_solver.recv(1024)
            response = json.loads(b)

            if not response['success']:
                self.logger.warning(f"solver could not solve, reason '{response.reasons}")
                continue

            # self.logger.info('parsing plate solving result')

            if response['success']:
                self.logger.info(f"plate solving succeeded")
                solved_ra = response['ra']
                solved_dec = response['dec']
                pw_status = self.pw.status()
                mount_ra = pw_status.mount.ra_j2000_hours
                mount_dec = pw_status.mount.dec_j2000_degs

                delta_ra = solved_ra - mount_ra      # mind sign and mount offset direction
                delta_dec = solved_dec - mount_dec   # ditto

                delta_ra_arcsec = delta_ra / (60 * 60)
                delta_dec_arcsec = delta_dec / (60 * 60)

                self.logger.info(f'telling mount to offset by ra={delta_ra_arcsec:.10f}arcsec, '
                                 f'dec={delta_dec_arcsec:.10f}arcsec')
                self.pw.mount_offset(ra_add_arcsec=delta_ra_arcsec, dec_add_arcsec=delta_dec_arcsec)
            else:
                pass  # TBD

            self.logger.info(f"done solving cycle, sleeping {self.GUIDING_INTER_EXPOSURE_SECONDS} seconds ...")
            # avoid sleeping for a long time, for better agility at sensing that guiding was stopped
            td = datetime.timedelta(seconds=self.GUIDING_INTER_EXPOSURE_SECONDS)
            start = datetime.datetime.now()
            while (datetime.datetime.now() - start) <= td:
                if not self.is_active(UnitActivities.Guiding):
                    self.end_guiding()
                    return
                time.sleep(1)

    @property
    def connected(self):
        return self.camera.connected

    @connected.setter
    def connected(self, value):
        """
        Should connect/disconnect anything that needs connecting/disconnecting

        """

        self.camera.connected = value

        if value:
            # it's only at this stage that we know the imager size
            try:
                self.image_shm = SharedMemory(name='PlateSolving_Image')
            except FileNotFoundError:
                size = self.camera.ascom.NumX * self.camera.ascom.NumY * 4
                self.image_shm = SharedMemory(name='PlateSolving_Image', create=True, size=size)

    @return_with_status
    def connect(self):
        """
        Connects the **MAST** ``unit`` subsystems to all its ancillaries.

        """
        self.connected = True

    @return_with_status
    def disconnect(self):
        """
        Disconnects the **MAST** ``unit`` subsystems from all its ancillaries.

        """
        self.connected = False

    @return_with_status
    def start_guiding(self):
        """
        Starts the ``autoguide`` routine

        :mastapi:
        """
        if not self.connected:
            # self.camera.connected = True
            # self.camera.startup()
            self.connected = True

        # if self.is_active(UnitActivities.Guiding):
            # return

        pw_stat = self.pw.status()
        self.was_tracking_before_guiding = pw_stat.mount.is_tracking
        if not self.was_tracking_before_guiding:
            self.pw.mount_tracking_on()
            self.logger.info('started mount tracking')

        self.start_activity(UnitActivities.Guiding, self.logger)
        if not self.image_shm:
            self.image_shm = SharedMemory(name='PlateSolving_Image', create=True,
                                          size=(self.camera.NumX * self.camera.NumY * 4))
        Thread(name='guiding-thread', target=self.do_guide).start()

    @return_with_status
    def stop_guiding(self):
        """
        Stops the ``autoguide`` routine

        :mastapi:
        """
        if not self.connected:
            self.logger.warning('Cannot stop guiding - not-connected')
            return

        if self.is_active(UnitActivities.Guiding):
            self.end_activity(UnitActivities.Guiding, self.logger)

        if self.plate_solver_process:
            try:
                self.plate_solver_process.kill()
                self.logger.info(f'killed plate solving process pid={self.plate_solver_process.pid}')
            except psutil.NoSuchProcess:
                pass

        # if not self.was_tracking_before_guiding:
        #     self.mount.stop_tracking()
        #     self.logger.info('stopped tracking')

    def is_guiding(self) -> bool:
        if not self.connected:
            return False

        return self.is_active(UnitActivities.Guiding)

    @property
    def guiding(self) -> bool:
        return self.is_active(UnitActivities.Guiding)

    class SolverResponse:
        solved: bool
        reason: str
        ra: float
        dec: float

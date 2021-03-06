import time
from warnings import warn
from threading import Event
from threading import Timer
from threading import Lock
from contextlib import suppress

import numpy as np

from astropy import units as u

from pocs.camera.sdk import AbstractSDKCamera
from pocs.camera.libfli import FLIDriver
from pocs.camera import libfliconstants as c
from pocs.utils.images import fits as fits_utils
from pocs.utils import error


class Camera(AbstractSDKCamera):
    _driver = None
    _cameras = {}
    _assigned_cameras = set()

    def __init__(self,
                 name='FLI Camera',
                 set_point=25 * u.Celsius,
                 *args, **kwargs):
        kwargs['set_point'] = set_point
        super().__init__(name, FLIDriver, *args, **kwargs)
        self.logger.info('{} initialised'.format(self))

    def __del__(self):
        with suppress(AttributeError):
            handle = self._handle
            self._driver.FLIClose(handle)
            self.logger.debug('Closed FLI camera handle {}'.format(handle.value))
        super().__del__()

# Properties

    @property
    def ccd_temp(self):
        """
        Current temperature of the camera's image sensor.
        """
        return self._driver.FLIGetTemperature(self._handle)

    @property
    def ccd_set_point(self):
        """
        Current value of the CCD set point, the target temperature for the camera's
        image sensor cooling control.

        Can be set by assigning an astropy.units.Quantity.
        """
        return self._set_point

    @ccd_set_point.setter
    def ccd_set_point(self, set_point):
        if not isinstance(set_point, u.Quantity):
            set_point = set_point * u.Celsius
        self.logger.debug("Setting {} cooling set point to {}".format(self, set_point))
        self._driver.FLISetTemperature(self._handle, set_point)
        self._set_point = set_point

    @property
    def ccd_cooling_enabled(self):
        """
        Current status of the camera's image sensor cooling system (enabled/disabled).

        Note: For FLI cameras this is always True, and cannot be set.
        """
        return True

    @ccd_cooling_enabled.setter
    def ccd_cooling_enabled(self, enable):
        # Cooling is always enabled on FLI cameras
        if not enable:
            raise error.NotSupported("Cannot disable cooling on {}".format(self.name))

    @property
    def ccd_cooling_power(self):
        """
        Current power level of the camera's image sensor cooling system (as
        a percentage of the maximum).
        """
        return self._driver.FLIGetCoolerPower(self._handle)

    @property
    def is_exposing(self):
        """ True if an exposure is currently under way, otherwise False """
        return bool(self._driver.FLIGetExposureStatus(self._handle).value)

# Methods

    def connect(self):
        """
        Connect to FLI camera.

        Gets a 'handle', serial number and specs/capabilities from the driver
        """
        self.logger.debug('Connecting to {}'.format(self))
        self._handle = self._driver.FLIOpen(port=self._address)
        if self._handle == c.FLI_INVALID_DEVICE:
            message = 'Could not connect to {} on {}!'.format(self.name, self._camera_address)
            raise error.PanError(message)
        self._get_camera_info()
        self.model = self._info['camera model']
        self._connected = True

# Private Methods

    def _start_exposure(self, seconds, filename, dark, header, *args, **kwargs):
        self._driver.FLISetExposureTime(self._handle, exposure_time=seconds)

        if dark:
            frame_type = c.FLI_FRAME_TYPE_DARK
        else:
            frame_type = c.FLI_FRAME_TYPE_NORMAL
        self._driver.FLISetFrameType(self._handle, frame_type)

        # For now set to 'visible' (i.e. light sensitive) area of image sensor.
        # Can later use this for windowed exposures.
        self._driver.FLISetImageArea(self._handle,
                                     self._info['visible corners'][0],
                                     self._info['visible corners'][1])

        # No on chip binning for now.
        self._driver.FLISetHBin(self._handle, bin_factor=1)
        self._driver.FLISetVBin(self._handle, bin_factor=1)

        # No pre-exposure image sensor flushing, either.
        self._driver.FLISetNFlushes(self._handle, n_flushes=0)

        # In principle can set bit depth here (16 or 8 bit) but most FLI cameras don't support it.

        # Start exposure
        self._driver.FLIExposeFrame(self._handle)

        readout_args = (filename,
                        self._info['visible width'],
                        self._info['visible height'],
                        header)
        return readout_args

    def _readout(self, filename, width, height, header):
        # Use FLIGrabRow for now at least because I can't get FLIGrabFrame to work.
        # image_data = self._FLIDriver.FLIGrabFrame(self._handle, width, height)
        image_data = np.zeros((height, width), dtype=np.uint16)
        rows_got = 0
        try:
            for i in range(image_data.shape[0]):
                image_data[i] = self._driver.FLIGrabRow(self._handle, image_data.shape[1])
                rows_got += 1
        except RuntimeError as err:
            message = 'Readout error on {}, expected {} rows, got {}: {}'.format(
                self, image_data.shape[0], rows_got, err)
            raise error.PanError(message)
        else:
            fits_utils.write_fits(image_data, header, filename, self.logger, self._exposure_event)

    def _fits_header(self, seconds, dark):
        header = super()._fits_header(seconds, dark)

        header.set('CAM-HW', self._info['hardware version'], 'Camera hardware version')
        header.set('CAM-FW', self._info['firmware version'], 'Camera firmware version')
        header.set('XPIXSZ', self._info['pixel width'].value, 'Microns')
        header.set('YPIXSZ', self._info['pixel height'].value, 'Microns')

        return header

    def _get_camera_info(self):

        serial_number = self._driver.FLIGetSerialString(self._handle)
        camera_model = self._driver.FLIGetModel(self._handle)
        hardware_version = self._driver.FLIGetHWRevision(self._handle)
        firmware_version = self._driver.FLIGetFWRevision(self._handle)

        pixel_width, pixel_height = self._driver.FLIGetPixelSize(self._handle)
        ccd_corners = self._driver.FLIGetArrayArea(self._handle)
        visible_corners = self._driver.FLIGetVisibleArea(self._handle)

        self._info = {
            'serial number': serial_number,
            'camera model': camera_model,
            'hardware version': hardware_version,
            'firmware version': firmware_version,
            'pixel width': pixel_width,
            'pixel height': pixel_height,
            'array corners': ccd_corners,
            'array height': ccd_corners[1][1] - ccd_corners[0][1],
            'array width': ccd_corners[1][0] - ccd_corners[0][0],
            'visible corners': visible_corners,
            'visible height': visible_corners[1][1] - visible_corners[0][1],
            'visible width': visible_corners[1][0] - visible_corners[0][0]
        }

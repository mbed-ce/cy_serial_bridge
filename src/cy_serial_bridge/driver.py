from __future__ import annotations

import contextlib
import sys
import time
from dataclasses import dataclass
from enum import Enum
from math import ceil
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from types import TracebackType

    from typing_extensions import Self

    from cy_serial_bridge.cy_scb_context import CyScbContext

import usb1  # from 'libusb1' package

from cy_serial_bridge.configuration_block import ConfigurationBlock
from cy_serial_bridge.usb_constants import *
from cy_serial_bridge.utils import ByteSequence, CySerialBridgeError, DiscoveredDevice, log

"""
Module containing the logic for communicating with the CY7C652xx USB device.
Note that this does NOT include logic for (a) scanning the device tree to find
the correct USB device or (b) manipulating configuration blocks.  Those get their own
modules.
"""


# Exceptions for recoverable I2C errors
class I2CNACKError(CySerialBridgeError):
    """
    This is thrown when an I2C operation is not acknowledged.
    """

    # For write operations, this stores the number of bytes (not including the address byte)
    # successfully written before the NACK was encountered.
    # For read operations, this is not meaningful because NACKs can only happen on the address byte.
    bytes_written: int = 0


class I2CBusError(CySerialBridgeError):
    """
    This is thrown when a bus error occurs during an I2C operation
    """


class I2CArbLostError(CySerialBridgeError):
    """
    This is thrown when arbitration is lost during an I2C operation
    """


class CySerBridgeBase:
    """
    Base class containing functionality common to all modes of a CY7C652xx
    """

    def __init__(
        self, context: CyScbContext, discovered_dev: DiscoveredDevice, cy_type: CyType, scb_index: int, timeout: int
    ):
        """
        Create a CySerBridgeBase.

        :param discovered_dev: Discovered device to open (from list_devices())
        :param cy_type: Type to open the device as.
        :param scb_index: Index of the SCB to open, for multi-port devices
        :param timeout: Timeout to use for USB operations in milliseconds
        """
        if scb_index > 1:
            message = "scb_index cannot be higher than 1!"
            raise ValueError(message)

        self.context = context
        self.cy_type = cy_type
        self.scb_index = scb_index
        self.discovered_dev = discovered_dev

        self.timeout = timeout

        self.ep_in = None
        self.ep_out = None

        if self.cy_type == CyType.MFG and self.discovered_dev.curr_cytype == CyType.UART_CDC:
            if sys.platform == "darwin":
                import os

                if os.geteuid() != 0:
                    message = (
                        "Access to the manufacturer interface of a device in UART CDC mode on MacOS requires elevation. "
                        "Try rerunning the command with sudo.  See the cy_serial_bridge README for details."
                    )
                    raise CySerialBridgeError(message)

        if self.cy_type != CyType.MFG:
            if self.discovered_dev.scb_interface_settings is None:
                message = "Opening this CyType requires an SCB interface to be present on the USB device"
                raise CySerialBridgeError(message)

            # grab EPs from SCB endpoint
            ep: usb1.USBEndpoint
            for ep in self.discovered_dev.scb_interface_settings:
                ep_attr = ep.getAttributes()
                ep_addr = ep.getAddress()
                if ep_attr == EP_BULK:
                    if ep_addr & EP_IN:
                        self.ep_in = ep_addr
                    else:
                        self.ep_out = ep_addr
                elif ep_attr == EP_INTR:
                    self.ep_intr = ep_addr

        # Check that we got the expected endpoints (though the manufacturer interface doesn't have them)
        if cy_type != CyType.MFG and (self.ep_in is None or self.ep_out is None or self.ep_intr is None):
            message = "Failed to find CY7C652xx USB endpoints in USB device -- not a Cypress serial bridge device?"
            raise CySerialBridgeError(message)

        log.info("Discovered USB endpoints successfully")

    def __enter__(self) -> Self:
        if self.context.has_opened_driver:
            message = "This CyScbContext instance already has a driver open!  Close it or create a new context!"
            raise CySerialBridgeError(message)

        # Create temporary exit stack to open other context managers
        with contextlib.ExitStack() as temp_stack:
            # open USBDeviceHandle
            try:
                self.dev = self.discovered_dev.usb_device.open()

            except usb1.USBErrorNotFound as ex:
                if sys.platform == "win32":
                    message = "Failed to open USB device, ensure that WinUSB driver has been loaded for it using Zadig"
                    raise CySerialBridgeError(message) from ex
                else:
                    raise

        try:
            if usb1.hasCapability(usb1.CAP_SUPPORTS_DETACH_KERNEL_DRIVER):
                # In order to access the interface we need, we need to detach all kernel drivers from
                # any interfaces they are attached to.  It's not enough to just detach it from the interface
                # we need!
                # https://elixir.bootlin.com/linux/latest/source/drivers/usb/core/devio.c#L1543
                all_interfaces = (
                    self.discovered_dev.mfg_interface_settings,
                    self.discovered_dev.scb_interface_settings,
                    self.discovered_dev.usb_cdc_interface_settings,
                    self.discovered_dev.usb_cdc_interface_settings,
                )

                for interface in filter(lambda iface: iface is not None, all_interfaces):
                    if self.dev.kernelDriverActive(interface.getNumber()):
                        self.dev.detachKernelDriver(interface.getNumber())

            if self.cy_type == CyType.MFG:
                # Interface to use is the manufacturer interface
                target_interface = self.discovered_dev.mfg_interface_settings
            else:
                # Interface to use is the SCB interface
                target_interface = self.discovered_dev.scb_interface_settings

            #
            # NOTE:
            # Windows and others seems to differ in expected order of
            # when to claim interface and when to set configuration.
            #
            self.dev.setConfiguration(self.discovered_dev.usb_configuration.getConfigurationValue())
            if target_interface.getAlternateSetting() > 0:
                self.dev.setInterfaceAltSetting(target_interface.getNumber(), target_interface.getAlternateSetting())

            temp_stack.enter_context(self.dev.claimInterface(target_interface.getNumber()))

            # Check the device signature
            signature = bytes(self.get_signature())
            log.info("Device signature: %s", repr(signature))
            if signature != b"CYUS":
                self.dev.close()

                message = "Invalid signature for CY7C652xx device"
                raise CySerialBridgeError(message)

            # Get and print the firmware version
            firmware_version = self.get_firmware_version()
            print(
                "Connected to %s interface of CY7C652xx device, firmware version %d.%d.%d build %d"
                % (self.cy_type.name, *firmware_version)
            )

            # Creates a new exit stack with ownership of the USB device "moved" into it
            self.exit_stack = temp_stack.pop_all()

        except usb1.USBErrorNotSupported as ex:
            if sys.platform == "win32":
                message = "Failed to claim USB device, ensure that WinUSB driver has been loaded for it using Zadig"
                raise CySerialBridgeError(message) from ex
            else:
                raise

        # Mark this context as having opened a driver.
        # Note that there's a 1 context <--> 1 thread relationship so
        self.context.has_opened_driver = True

        return self

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, traceback: TracebackType | None
    ) -> None:
        # On Linux, calling reset_device() causes this error to be raised when we try to close the device.
        # Ignore it so that we can close the device without an error.
        with contextlib.suppress(usb1.USBErrorNoDevice):
            self.exit_stack.pop_all()

        if self.dev:
            self.dev.close()
        self.dev = None

        if not self.context.has_opened_driver:
            message = "...what? Context thinks the driver isn't open?"
            raise CySerialBridgeError(message)
        self.context.has_opened_driver = False

    def _wait_for_notification(self, event_notification_len: int, timeout: int) -> None:
        """
        Wait for a transfer complete notification from the serial bridge.

        This is used for background serial transfers.

        :param event_notification_len: Length of event notification
        :param timeout: Transfer timeout in milliseconds. 0 to disable.
        """
        transfer = self.dev.getTransfer(0)
        transfer.setInterrupt(self.ep_intr, event_notification_len, timeout=timeout)
        transfer.submit()

    # Common functions which work in all interface modes --------------------------------
    def get_firmware_version(self) -> tuple[int, int, int, int]:
        """
        This API retrieves the firmware version of the USB Serial device.

        :return: Firmware version as a 4-tuple of (major ver, minor ver, patch ver, build number)
        """
        bm_request_type = CY_VENDOR_REQUEST | EP_IN
        bm_request = CyVendorCmds.CY_GET_VERSION_CMD
        w_value = 0
        w_index = 0
        w_length = CY_GET_FIRMWARE_VERSION_LEN

        firmware_version_bytes = self.dev.controlRead(
            bm_request_type, bm_request, w_value, w_index, w_length, self.timeout
        )

        # C definition:
        # typedef struct _CY_FIRMWARE_VERSION {
        #
        #     UINT8 majorVersion;                 /*Major version of the Firmware*/
        #     UINT8 minorVersion;                 /*Minor version of the Firmware*/
        #     UINT16 patchNumber;                 /*Patch Number of the Firmware*/
        #     UINT32 buildNumber;                 /*Build Number of the Firmware*/
        #
        # } CY_FIRMWARE_VERSION, *PCY_FIRMWARE_VERSION;

        return cast(tuple[int, int, int, int], struct.unpack("<BBHI", firmware_version_bytes))

    def get_signature(self) -> ByteSequence:
        """
        This API is used to get the signature of the device.

        It would be CYUS when we are in actual device mode and CYBL when we are bootloader mode.
        """
        bm_request_type = CY_VENDOR_REQUEST | EP_IN
        bm_request = CyVendorCmds.CY_GET_SIGNATURE_CMD
        w_value = 0
        w_index = 0
        w_length = CY_GET_SIGNATURE_LEN

        return cast(
            ByteSequence, self.dev.controlRead(bm_request_type, bm_request, w_value, w_index, w_length, self.timeout)
        )

    def reset_device(self) -> None:
        """
        The API will reset the device by sending a vendor request to the firmware. The device will be re-enumerated.

        After calling this function, the serial bridge object that you called it on will become
        nonfunctional and should be closed.  You must open a new instance of the driver
        after the device re-enumerates.
        """
        bm_request_type = CY_VENDOR_REQUEST | EP_IN
        bm_request = CyVendorCmds.CY_DEVICE_RESET_CMD
        w_value = 0xA6B6
        w_index = 0xADBA
        data = b""

        # Resetting the device always seems to result in a pipe error on windows and
        # a no device error on Linux -- it seems like the
        # low level USB control operation always returns USBD_STATUS_XACT_ERROR (0xc0000011)
        try:
            self.dev.controlWrite(bm_request_type, bm_request, w_value, w_index, data, self.timeout)
        except usb1.USBErrorPipe:
            pass
        except usb1.USBErrorNoDevice:
            pass

    def program_user_flash(self, addr: int, buff: ByteSequence) -> None:
        """
        The API programs user flash area. The total space available is 512 bytes.

        The flash area address offset is from 0x0000 to 0x00200 and should be written
        in even pages (page size is 128 bytes)

        :param addr: Address to start writing data at.  Must be a multiple of 128 and between 0 and 384.
        :param buff: Buffer of data to write.  Must be a multiple of 128 bytes long.
        """
        if addr % USER_FLASH_PAGE_SIZE != 0 or len(buff) % USER_FLASH_PAGE_SIZE != 0 or len(buff) == 0:
            message = "Program operation not aligned correctly!"
            raise ValueError(message)
        if addr < 0 or len(buff) + addr > USER_FLASH_SIZE:
            message = "Program operation outside user flash bounds!"
            raise ValueError(message)

        num_pages = len(buff) // USER_FLASH_PAGE_SIZE
        for page_idx in range(num_pages):
            first_byte_idx = page_idx * USER_FLASH_PAGE_SIZE
            bytes_to_send = buff[first_byte_idx : first_byte_idx + USER_FLASH_PAGE_SIZE]
            self.dev.controlWrite(
                request_type=CY_VENDOR_REQUEST_DEVICE_TO_HOST,
                request=CyVendorCmds.CY_PROG_USER_FLASH_CMD,
                value=0,
                index=addr + first_byte_idx,
                data=bytes_to_send,
                timeout=self.timeout,
            )

    def read_user_flash(self, addr: int, size: int) -> bytearray:
        """
        Read from the user flash area.

        This area can be programmed with program_user_flash(), see that function for more details.

        :param addr: Address to start reading data from.  Must be a multiple of 128 and between 0 and 384.
        :param size: Count of data bytes to read.  Must be a multiple of 128.
        """
        if addr % USER_FLASH_PAGE_SIZE != 0 or size % USER_FLASH_PAGE_SIZE != 0 or size == 0:
            message = "Read operation not aligned correctly!"
            raise ValueError(message)
        if addr < 0 or size + addr > USER_FLASH_SIZE:
            message = "Read operation outside user flash bounds!"
            raise ValueError(message)

        result_bytes = bytearray()

        num_pages = size // USER_FLASH_PAGE_SIZE
        for page_idx in range(num_pages):
            page_bytes = self.dev.controlRead(
                request_type=CY_VENDOR_REQUEST_DEVICE_TO_HOST,
                request=CyVendorCmds.CY_READ_USER_FLASH_CMD,
                value=0,
                index=addr + page_idx * USER_FLASH_PAGE_SIZE,
                length=USER_FLASH_PAGE_SIZE,
                timeout=self.timeout,
            )
            result_bytes.extend(page_bytes)

        return result_bytes


    def set_gpio(self, gpio_nr: int, value: bool) -> None:
        """
        Set the value of a GPIO pin.

        :param pin: GPIO pin number to set
        :param value: Value to set the pin to
        """

        self.dev.controlWrite(
                request_type=CY_VENDOR_REQUEST_HOST_TO_DEVICE,
                request=CyVendorCmds.CY_GPIO_SET_VALUE_CMD,
                value=gpio_nr,
                index=1 if value==True else 0,
                data=[],
                timeout=self.timeout
            )
        # TODO check for errors

        
    def get_gpio(self, gpio_nr: int) -> bool:
        """
        Get the value of a GPIO pin.
        :param pin: GPIO pin number to get
        :return: Value of the pin
        """

        result_bytes = self.dev.controlRead(
                request_type=CY_VENDOR_REQUEST_DEVICE_TO_HOST,
                request=CyVendorCmds.CY_GPIO_GET_VALUE_CMD,
                value=gpio_nr,
                index=0,
                length=CY_GET_GPIO_LEN,
                timeout=self.timeout
            )
        if len(result_bytes) != CY_GET_GPIO_LEN or result_bytes[0] != 0:
            message = f"Error getting GPIO {gpio_nr}"
            raise CySerialBridgeError(message)
        return (result_bytes[1]==1)


class CyMfgrIface(CySerBridgeBase):
    """
    Class allowing access to a CY7C652xx in the manufacturing interface mode.

    This is the interface that USCU uses to configure the device, and is reverse-engineered from
    the operation of that program.
    """

    def __init__(
        self, context: CyScbContext, discovered_dev: DiscoveredDevice, scb_index: int = 0, timeout: int = 1000
    ):
        """
        Create a CySerBridgeBase.

        Note: You would not normally call this constructor directly.  Instead, you should call
        CyScbContext.open_device().

        :param context: Context to open the device with.
        :param discovered_dev: Discovered device to open (from list_devices())
        :param scb_index: Index of the SCB to open, for multi-port devices
        :param timeout: Timeout to use for USB operations in milliseconds
        """
        super().__init__(context, discovered_dev, CyType.MFG, scb_index, timeout)

    ######################################################################
    # Non-public APIs still under experimental stage
    ######################################################################

    def ping(self) -> int:
        """Send whatever USCU sends on startup"""
        bm_request_type = CY_VENDOR_REQUEST | EP_OUT
        bm_request = 203
        w_value = 0
        w_index = 0
        w_buffer = bytearray(0)

        return cast(int, self.dev.controlWrite(bm_request_type, bm_request, w_value, w_index, w_buffer, self.timeout))

    def probe0(self) -> int:
        """Send whatever USCU sends on startup - get silicon ID as we found out"""
        bm_request_type = CY_VENDOR_REQUEST | EP_IN
        bm_request = CyVendorCmds.CY_BOOT_CMD_GET_SILICON_ID
        w_value = 0
        w_index = 0
        w_length = 4

        return cast(int, self.dev.controlRead(bm_request_type, bm_request, w_value, w_index, w_length, self.timeout))

    # Note that there used to be a "probe1" function here for another mystery sequence, but that was revealed to be just
    # getting the firmware version (equivalent to get_firmware_version())

    def connect(self) -> int:
        """
        Enter manufacturing mode, allowing the configuration block to be accessed and reprogrammed.
        """
        bm_request_type = CY_VENDOR_REQUEST | EP_OUT
        bm_request = CyVendorCmds.CY_VENDOR_ENTER_MFG_MODE
        w_value = 0xA6BC
        w_index = 0xB1B0
        w_buffer = bytearray(0)

        return cast(int, self.dev.controlWrite(bm_request_type, bm_request, w_value, w_index, w_buffer, self.timeout))

    def disconnect(self) -> int:
        """
        Exit manufacturing mode, blocking updates to memory.
        """
        bm_request_type = CY_VENDOR_REQUEST | EP_OUT
        bm_request = CyVendorCmds.CY_VENDOR_ENTER_MFG_MODE
        w_value = 0xA6BC
        w_index = 0xB9B0
        w_buffer = bytearray(0)

        return cast(int, self.dev.controlWrite(bm_request_type, bm_request, w_value, w_index, w_buffer, self.timeout))

    def read_config(self) -> ByteSequence:
        """
        Read the configuration block from the device.

        connect() must be called first.
        """
        bm_request_type = CY_VENDOR_REQUEST | EP_IN
        bm_request = CyVendorCmds.CY_BOOT_CMD_READ_CONFIG
        w_value = 0
        w_index = 0
        w_length = 512

        return cast(
            bytearray, self.dev.controlRead(bm_request_type, bm_request, w_value, w_index, w_length, self.timeout)
        )

    def write_config(self, config: ConfigurationBlock) -> int:
        """
        Write the configuration block to the device.

        connect() must be called first.
        """
        bm_request_type = CY_VENDOR_REQUEST | EP_OUT
        bm_request = CyVendorCmds.CY_BOOT_CMD_PROG_CONFIG
        w_value = 0
        w_index = 0

        w_buffer = config.config_bytes

        return cast(int, self.dev.controlWrite(bm_request_type, bm_request, w_value, w_index, w_buffer, self.timeout))

    def change_type(self, new_type: CyType) -> None:
        """
        Changes the CyType of the device.  Also makes other config changes needed to make that work.

        Note: After calling this function, the new CyType will not take effect until
        you reset the device
        """
        self.connect()

        try:
            buffer = self.read_config()

            config_block = ConfigurationBlock(block_bytes=buffer)
            log.info("Read the following configuration from the device: %s", str(config_block))

            # Change the type
            config_block.device_type = new_type

            if config_block.pid % 2 == 0 and new_type == CyType.UART_CDC:
                config_block.pid += 1
                log.info("Changing to UART_CDC and device has even PID.  Changing PID to 0x%x", config_block.pid)
            elif config_block.pid % 2 == 1 and new_type != CyType.UART_CDC:
                config_block.pid -= 1
                log.info("Changing to non-UART_CDC and device has odd PID.  Changing PID to 0x%x", config_block.pid)

            # I did notice an issue where the region from offset 0x27 to 0x2f seems to be used to store
            # mode-specific settings, and the serial bridge internal-errors and doesn't respond to USB requests
            # in some cases if these settings are invalid.
            # I have not implemented encoding/decoding for this block, but we can program in known good values.
            if new_type == CyType.SPI:
                # Confirmed working for SPI (and probably I2C)
                config_block.config_bytes[0x27:0x30] = b"\x00\x08\x00\x00\x01\x01\x01\x00\x00"
            else:
                # Confirmed working for UART_CDC and I2C
                config_block.config_bytes[0x27:0x30] = b"\x00\x02\x08\x01\x00\x00\x00\x00\x00"

            log.info("Writing the following configuration to the device: %s", str(config_block))

            self.write_config(config_block)

            self.disconnect()

            log.info("Type has been changed.")

        except Exception:
            self.disconnect()
            raise


@dataclass
class CyI2CConfig:
    frequency: int = 400000  # I2C frequency in Hz


class CyI2CControllerBridge(CySerBridgeBase):
    """
    Driver which uses a Cypress serial bridge in I2C controller (master) mode.
    """

    def __init__(
        self, context: CyScbContext, discovered_dev: DiscoveredDevice, scb_index: int = 0, timeout: int = 1000
    ):
        """
        Create a CyI2CControllerBridge.

        Note: You would not normally call this constructor directly.  Instead, you should call
        CyScbContext.open_device().

        :param context: Context to open the device with.
        :param discovered_dev: Discovered device to open (from list_devices())
        :param scb_index: Index of the SCB to open, for multi-port devices
        :param timeout: Timeout to use for general USB operations in milliseconds
        """
        super().__init__(context, discovered_dev, CyType.I2C, scb_index, timeout)

        self._curr_frequency: int | None = None

    def __enter__(self) -> Self:
        super().__enter__()

        # Reset the I2C peripheral in case it was in a bad state (e.g. if a previous errored operation
        # was not cleaned up)
        self._i2c_reset(CyI2c.MODE_READ)
        self._i2c_reset(CyI2c.MODE_WRITE)

        # Should be in a good state now
        if self._get_i2c_status(CyI2c.MODE_READ)[0] & CyI2c.ERROR_BIT:
            message = "I2C read interface is not ready!"
            raise CySerialBridgeError(message)
        if self._get_i2c_status(CyI2c.MODE_WRITE)[0] & CyI2c.ERROR_BIT:
            message = "I2C write interface is not ready!"
            raise CySerialBridgeError(message)

        return self

    def _get_i2c_status(self, mode: CyI2c) -> ByteSequence:
        """
        Get the I2C status flag from the chip.

        This is a 4 byte bitfield (whose values are mostly not documented) which is used by the I2C code to
        check what the chip is doing.

        :param mode: Either CyI2c.MODE_WRITE or CyI2c.MODE_READ
        """
        return cast(
            ByteSequence,
            self.dev.controlRead(
                request_type=CY_VENDOR_REQUEST_DEVICE_TO_HOST,
                request=CyVendorCmds.CY_I2C_GET_STATUS_CMD,
                value=(self.scb_index << CY_SCB_INDEX_POS) | mode,
                index=0,
                length=CyI2c.GET_STATUS_LEN,
                timeout=self.timeout,
            ),
        )

    def _i2c_reset(self, mode: CyI2c) -> None:
        """
        This API resets the read or write I2C module whenever there is an error in a data transaction.

        :param mode: Either CyI2c.MODE_WRITE or CyI2c.MODE_READ
        """
        self.dev.controlWrite(
            request_type=CY_VENDOR_REQUEST_HOST_TO_DEVICE,
            request=CyVendorCmds.CY_I2C_RESET_CMD,
            value=(self.scb_index << CY_SCB_INDEX_POS) | mode,
            index=0,
            data=b"",
            timeout=self.timeout,
        )

    def set_i2c_configuration(self, config: CyI2CConfig) -> None:
        """
        This API configures the I2C module of USB Serial device.

        Currently the only setting configurable for I2C master is the frequency.

        You should always call this function after first opening the device because the configuration rewriting part of
        the module does not know how to set the default I2C settings in config and they may be garbage.

        Note: Using this API during an active transaction of I2C may result in data loss.
        """
        self._curr_frequency = config.frequency

        # Per the datasheet, the frequency has to be between 1kHz and 400kHz
        if config.frequency < CyI2c.MIN_FREQUENCY or config.frequency > CyI2c.MAX_FREQUENCY:
            message = "Invalid frequency!"
            raise ValueError(message)

        binary_configuration = struct.pack(
            CY_USB_I2C_CONFIG_STRUCT_LAYOUT,
            config.frequency,
            0,  # sAddress - seems to be ignored in master mode
            1,  # isMsbFirst - Driver always sets this to 1
            1,  # isMaster - set to true for master mode
            0,  # sIgnore - seems to be ignored in master mode
            0,  # clockStretch - seems to be ignored in master mode
            0,  # isLoopback - Driver always sets this to 0
        )

        self.dev.controlWrite(
            request_type=CY_VENDOR_REQUEST_HOST_TO_DEVICE,
            request=CyVendorCmds.CY_I2C_SET_CONFIG_CMD,
            value=(self.scb_index << CY_SCB_INDEX_POS),
            index=0,
            data=binary_configuration,
            timeout=self.timeout,
        )

    def read_i2c_configuration(self) -> CyI2CConfig:
        """
        Read the current I2C master mode configuration from the device.
        """
        config_bytes = self.dev.controlRead(
            request_type=CY_VENDOR_REQUEST_DEVICE_TO_HOST,
            request=CyVendorCmds.CY_I2C_GET_CONFIG_CMD,
            value=(self.scb_index << CY_SCB_INDEX_POS),
            index=0,
            length=CyI2c.CONFIG_LENGTH,
            timeout=self.timeout,
        )

        config_unpacked = struct.unpack(CY_USB_I2C_CONFIG_STRUCT_LAYOUT, config_bytes)
        config = CyI2CConfig(frequency=config_unpacked[0])

        self._curr_frequency = config.frequency

        return config

    def i2c_read(
        self, periph_addr: int, size: int, relinquish_bus: bool = True, io_timeout: int | None = None
    ) -> ByteSequence:
        """
        Perform an I2C read from the given peripheral device.

        If the device does not acknowledge the read, an I2CNACKError will be raised.

        Note: Unlike write operations, the peripheral cannot NACK a read after the address byte
        has been ACKed.  So read operations are always all-or-nothing at the electrical level:
        either all the data is read, or none of it is.

        :param periph_addr: 7-bit I2C address of the peripheral to read from
        :param size: Number of bytes to read
        :param relinquish_bus: If true, give up the bus at the end.  Otherwise, a stop condition will not be generated,
            so a repeated start will be performed on the next transfer.
        :param io_timeout: Timeout for the transfer in ms.  Leave empty to compute a reasonable timeout automatically.
            Set to 0 to wait forever.
        """
        if self._curr_frequency is None:
            message = "Must call set_i2c_configuration() before reading or writing data!"
            raise CySerialBridgeError(message)

        if periph_addr > CyI2c.MAX_VALID_ADDRESS:
            message = "Invalid peripheral addr, must be a 7 bit address!"
            raise ValueError(message)

        if size < 1:
            # I tested this and the bridge device does not handle 0-size reads
            message = "Read size must be >= 1"
            raise ValueError(message)

        # For a reasonable timeout, assume it takes 10 bit times per byte sent,
        # and also allow 1 extra second for any USB overhead.
        if io_timeout is None:
            io_timeout = 1000 + ceil(1000 * size * (1 / self._curr_frequency) * 10)

        initial_status = self._get_i2c_status(CyI2c.MODE_READ)

        if initial_status[0] & CyI2c.ERROR_BIT:
            message = "Device is busy but tried to start another read!"
            raise CySerialBridgeError(message)

        # Bits 0 and 1 of the value control stop bit generation and NAK generation at the end of the read.
        # We always want to NAK the slave at the end of the read as it's required by the standard...
        value = (self.scb_index << 15) | (periph_addr << 8) | 0b10 | (1 if relinquish_bus else 0)

        # Set up transfer
        self.dev.controlWrite(
            request_type=CY_VENDOR_REQUEST_HOST_TO_DEVICE,
            request=CyVendorCmds.CY_I2C_READ_CMD,
            value=value,
            index=size,
            data=b"",
            timeout=io_timeout,
        )

        # Get data
        try:
            read_data: bytearray = self.dev.bulkRead(self.ep_in, size, timeout=io_timeout)
            post_transfer_status = self.dev.interruptRead(self.ep_intr, CyI2c.EVENT_NOTIFICATION_LEN, io_timeout)

        except usb1.USBErrorPipe as ex:
            # Attempt to handle pipe errors similarly to how the original driver did.
            # Basically, we reset the hardware and re-query the status.

            # Try and reset the endpoint
            self.dev.clearHalt(self.ep_in)

            # Recheck the status
            post_transfer_status = self._get_i2c_status(CyI2c.MODE_READ)

            # The status should indicate some sort of error
            if not post_transfer_status[0] & CyI2c.ERROR_BIT:
                message = "Operation failed with pipe error, but did not detect an I2C comms error?"
                raise CySerialBridgeError(message) from ex

            raise

        if post_transfer_status[0] & CyI2c.ERROR_BIT:
            # First reset the read logic
            self._i2c_reset(CyI2c.MODE_READ)

            # Finally, handle the error
            if post_transfer_status[0] & CyI2c.ARBITRATION_ERROR_BIT:
                raise I2CArbLostError
            elif post_transfer_status[0] & CyI2c.NAK_ERROR_BIT:
                error = I2CNACKError()
                error.bytes_written = 0
                raise error
            elif post_transfer_status[0] & CyI2c.BUS_ERROR_BIT:
                raise I2CBusError
            else:
                message = "I2C operation failed with status " + repr(post_transfer_status)
                raise CySerialBridgeError(message)

        return read_data

    def i2c_write(
        self, periph_addr: int, data: ByteSequence, relinquish_bus: bool = True, io_timeout: int | None = None
    ) -> None:
        """
        Perform an I2C write to the given peripheral device.

        If the device does not acknowledge the write, an I2CNACKError will be raised.  The
        bytes_written field of the exception can be used to determine where in the write the
        NACK happened.

        NOTE: Due to what seems to be a bridge chip issue, a NACK error will not be raised for failed writes of
        only one byte.  Need to look into this more...

        :param periph_addr: 7-bit I2C address of the peripheral to read from
        :param data: Data to write
        :param relinquish_bus: If true, give up the bus at the end.  Otherwise, a stop condition will not be generated,
            so a repeated start will be performed on the next transfer.
        :param io_timeout: Timeout for the transfer in ms.  Leave empty to compute a reasonable timeout automatically.
            Set to 0 to wait forever.
        """
        if self._curr_frequency is None:
            message = "Must call set_i2c_configuration() before reading or writing data!"
            raise CySerialBridgeError(message)

        if periph_addr > CyI2c.MAX_VALID_ADDRESS:
            message = "Invalid peripheral addr, must be a 7 bit address!"
            raise ValueError(message)

        # For a reasonable timeout, assume it takes 10 bit times per byte sent,
        # and also allow 1 extra second for any USB overhead.
        if io_timeout is None:
            io_timeout = 1000 + ceil(1000 * len(data) * (1 / self._curr_frequency) * 10)

        initial_status = self._get_i2c_status(CyI2c.MODE_WRITE)

        if initial_status[0] & CyI2c.ERROR_BIT:
            message = "Device is busy but tried to start another write!"
            raise CySerialBridgeError(message)

        # Bit 0 of the value controls stop bit generation
        value = (self.scb_index << 15) | (periph_addr << 8) | (1 if relinquish_bus else 0)

        # Set up transfer
        self.dev.controlWrite(
            request_type=CY_VENDOR_REQUEST_HOST_TO_DEVICE,
            request=CyVendorCmds.CY_I2C_WRITE_CMD,
            value=value,
            index=len(data),
            data=b"",
            timeout=io_timeout,
        )

        # Send data
        try:
            self.dev.bulkWrite(self.ep_out, data, timeout=io_timeout)
            post_transfer_status = self.dev.interruptRead(self.ep_intr, CyI2c.EVENT_NOTIFICATION_LEN, io_timeout)
        except usb1.USBErrorPipe as ex:
            # Attempt to handle pipe errors similarly to how the original driver did.
            # Basically, we reset the hardware and re-query the status.

            # Try and reset the endpoint
            self.dev.clearHalt(self.ep_out)

            # Recheck the status
            post_transfer_status = self._get_i2c_status(CyI2c.MODE_WRITE)

            # The status should indicate some sort of error
            if not post_transfer_status[0] & CyI2c.ERROR_BIT:
                message = "Operation failed with pipe error, but did not detect an I2C comms error?"
                raise CySerialBridgeError(message) from ex

        if post_transfer_status[0] & CyI2c.ERROR_BIT:
            partial_transfer_len = struct.unpack("<H", post_transfer_status[1:3])[0]

            # First reset the write logic
            self._i2c_reset(CyI2c.MODE_WRITE)

            # Finally, handle the error
            if post_transfer_status[0] & CyI2c.ARBITRATION_ERROR_BIT:
                raise I2CArbLostError
            elif post_transfer_status[0] & CyI2c.NAK_ERROR_BIT:
                error = I2CNACKError()
                error.bytes_written = partial_transfer_len
                raise error
            elif post_transfer_status[0] & CyI2c.BUS_ERROR_BIT:
                raise I2CBusError
            else:
                message = "I2C operation failed with status " + repr(post_transfer_status)
                raise CySerialBridgeError(message)


class CySPIMode(Enum):
    """
    Enumeration defining SPI protocol types supported by USB Serial SPI module.

    Values have the form (protocol enum value, CPHA value, CPOL value).
    Note that for "regular" SPI, you probably want one of the MOTOROLA modes.
    """

    # In master mode, when not transmitting data (SELECT is inactive), SCLK is stable at CPOL.
    # In slave mode, when not selected, SCLK is ignored; i.e. it can be either stable or clocking.
    # In master mode, when there is no data to transmit (TX FIFO is empty), SELECT is inactive.
    MOTOROLA_MODE_0 = (0, 0, 0)
    MOTOROLA_MODE_1 = (0, 0, 1)
    MOTOROLA_MODE_2 = (0, 1, 0)
    MOTOROLA_MODE_3 = (0, 1, 1)

    # In master mode, when not transmitting data, SCLK is stable at '0'.
    # In slave mode, when not selected, SCLK is ignored - i.e. it can be either stable or clocking.
    # In master mode, when there is no data to transmit (TX FIFO is empty), SELECT is inactive -
    # i.e. no pulse is generated.
    # *** It supports only mode 1 whose polarity values are
    # CPOL = 0
    # CPHA = 1
    TI = (1, 0, 1)

    # In master mode, when not transmitting data, SCLK is stable at '0'. In slave mode,
    # when not selected, SCLK is ignored; i.e. it can be either stable or clocking.
    # In master mode, when there is no data to transmit (TX FIFO is empty), SELECT is inactive.
    # *** It supports only mode 0 whose polarity values are
    # CPOL = 0
    # CPHA = 0
    NATIONAL_MICROWIRE = (2, 0, 0)


@dataclass
class CySPIConfig:
    # SCLK frequency in Hz.  Must be between 1kHz and 3MHz, inclusive.
    frequency: int = 1000000

    # Size of one data word in bits.  Must be between 4 and 16, inclusive.
    word_size: int = 8

    # SPI mode to use
    mode: CySPIMode = CySPIMode.MOTOROLA_MODE_0

    # If true, the MSBit of each word is sent first on the wire (standard)
    # If false, the LSBit is sent first
    msbit_first: bool = True

    # If true, the SSEL line is kept activated for the entire transaction.
    # If false, the chip is deselected after each word.
    continuous_ssel: bool = True

    # Used in TI mode only.
    # true - The start pulse precedes the first data
    # false - The start pulse is in sync with first data.
    ti_select_precede: bool = True


class CySPIControllerBridge(CySerBridgeBase):
    """
    Driver which uses a Cypress serial bridge in SPI controller (master) mode.
    """

    def __init__(
        self, context: CyScbContext, discovered_dev: DiscoveredDevice, scb_index: int = 0, timeout: int = 1000
    ):
        """
        Create a CySPIControllerBridge.

        Note: You would not normally call this constructor directly.  Instead, you should call
        CyScbContext.open_device().

        :param context: Context to open the device with.
        :param discovered_dev: Discovered device to open (from list_devices())
        :param scb_index: Index of the SCB to open, for multi-port devices
        :param timeout: Timeout to use for general USB operations in milliseconds
        """
        super().__init__(context, discovered_dev, CyType.SPI, scb_index, timeout)

        self._curr_frequency: int | None = None

    def __enter__(self) -> Self:
        super().__enter__()

        # Just in case the SPI module is in a bad state, reset it
        self._spi_reset()

        return self

    def _compute_timeout(self, transaction_size_bytes: int) -> int:
        """
        Compute a reasonable timeout for an SPI transaction.
        """
        # Assume 9 bit times per byte plus 1 second wiggle room
        return 1000 + ceil(1000 * transaction_size_bytes * (1 / cast(int, self._curr_frequency)) * 9)

    def _spi_reset(self) -> None:
        """
        This API resets the SPI module whenever there is an error in a data transaction.
        """
        self.dev.controlWrite(
            request_type=CY_VENDOR_REQUEST_HOST_TO_DEVICE,
            request=CyVendorCmds.CY_SPI_RESET_CMD,
            value=self.scb_index << CY_SCB_INDEX_POS,
            index=0,
            data=b"",
            timeout=self.timeout,
        )

    def _spi_is_write_done(self) -> bool:
        """
        Poll the SPI status indicator to determine if a write is done
        """
        spi_status: bytearray = self.dev.controlRead(
            request_type=CY_VENDOR_REQUEST_DEVICE_TO_HOST,
            request=CyVendorCmds.CY_SPI_GET_STATUS_CMD,
            value=self.scb_index << CY_SCB_INDEX_POS,
            index=0,
            length=CySpi.GET_STATUS_LEN,
            timeout=self.timeout,
        )

        return spi_status == b"\x00\x00\x00\x00"

    def set_spi_configuration(self, config: CySPIConfig) -> None:
        """
        This API configures the SPI module of USB Serial device.

        You should always call this function after first opening the device because the configuration rewriting part of
        the module does not know how to set the default SPI settings in config and they may be garbage.

        Note: Using this API during an active transaction of SPI may result in data loss.
        """
        # Check structure
        if config.frequency < CySpi.MIN_FREQUENCY or config.frequency > CySpi.MAX_MASTER_FREQUENCY:
            message = "Frequency out of valid range"
            raise ValueError(message)
        elif config.word_size < CySpi.MIN_WORD_SIZE or config.word_size > CySpi.MAX_WORD_SIZE:
            message = "Word size out of valid range"
            raise ValueError(message)

        self._curr_frequency = config.frequency

        binary_configuration = struct.pack(
            CY_USB_SPI_CONFIG_STRUCT_LAYOUT,
            config.frequency,  # frequency
            config.word_size,  # dataWidth
            config.mode.value[0],  # mode
            0,  # xferMode (seems unused in Cypress driver)
            config.msbit_first,  # isMsbFirst
            1,  # isMaster (always set to 1 here)
            config.continuous_ssel,  # isContinuous
            config.ti_select_precede,  # isSelectPrecede
            config.mode.value[1],  # cpha
            config.mode.value[2],  # cpol
            0,  # isLoopback (seems unused in Cypress driver)
        )

        self.dev.controlWrite(
            request_type=CY_VENDOR_REQUEST_HOST_TO_DEVICE,
            request=CyVendorCmds.CY_SPI_SET_CONFIG_CMD,
            value=(self.scb_index << CY_SCB_INDEX_POS),
            index=0,
            data=binary_configuration,
            timeout=self.timeout,
        )

    def read_spi_configuration(self) -> CySPIConfig:
        """
        Read the current SPI master mode configuration from the device.
        """
        config_bytes = self.dev.controlRead(
            request_type=CY_VENDOR_REQUEST_DEVICE_TO_HOST,
            request=CyVendorCmds.CY_SPI_GET_CONFIG_CMD,
            value=(self.scb_index << CY_SCB_INDEX_POS),
            index=0,
            length=CySpi.CONFIG_LEN,
            timeout=self.timeout,
        )

        config_unpacked = struct.unpack(CY_USB_SPI_CONFIG_STRUCT_LAYOUT, config_bytes)

        # Find the correct mode enum value based on the settings
        standard = config_unpacked[2]
        cpha = config_unpacked[8]
        cpol = config_unpacked[9]
        spi_mode: CySPIMode | None = None
        for mode_value in CySPIMode:
            if mode_value.value == (standard, cpha, cpol):
                spi_mode = mode_value

        if spi_mode is None:
            message = "Invalid SPI mode data read from hardware, can't convert to enum"
            raise CySerialBridgeError(message)

        config = CySPIConfig(
            frequency=config_unpacked[0],
            word_size=config_unpacked[1],
            mode=spi_mode,
            msbit_first=config_unpacked[4] != 0,
            continuous_ssel=config_unpacked[6] != 0,
            ti_select_precede=config_unpacked[7] != 0,
        )

        self._curr_frequency = config.frequency

        return config

    def spi_write(self, tx_data: ByteSequence, io_timeout: int | None = None) -> None:
        """
        Perform an SPI write-only operation to the peripheral device.  Read data is discarded.

        :param tx_data: Data to write
        :param io_timeout: Timeout for the transfer in ms.  Leave empty to compute a reasonable timeout automatically.
            Set to 0 to wait forever.
        """
        if self._curr_frequency is None:
            message = "Must call set_spi_configuration() before reading or writing data!"
            raise CySerialBridgeError(message)

        if io_timeout is None:
            io_timeout = self._compute_timeout(len(tx_data))

        # Set up transfer
        self.dev.controlWrite(
            request_type=CY_VENDOR_REQUEST_HOST_TO_DEVICE,
            request=CyVendorCmds.CY_SPI_READ_WRITE_CMD,
            value=(self.scb_index << CY_SCB_INDEX_POS) | CySpi.WRITE_BIT,
            index=len(tx_data),
            data=b"",
            timeout=io_timeout,
        )

        # Send data
        try:
            write_start_time = time.time()
            self.dev.bulkWrite(self.ep_out, tx_data, timeout=io_timeout)

            # Poll for completion.  Oddly, unlike I2C, there is no interrupt functionality to tell when
            # the transfer is complete.
            while not self._spi_is_write_done():
                time.sleep(0.001)

                if time.time() > write_start_time + io_timeout:
                    message = "Timeout waiting for SPI write completion!"
                    raise CySerialBridgeError(message)

        except usb1.USBErrorPipe:
            # Attempt to handle pipe errors similarly to how the original driver did.
            # Basically, we reset the hardware and reset SPI

            self._spi_reset()
            self.dev.clearHalt(self.ep_out)
            raise

        except usb1.USBErrorTimeout:
            self._spi_reset()
            raise

    def spi_read(self, read_len: int, io_timeout: int | None = None) -> ByteSequence:
        """
        Perform an SPI read-only operation from the peripheral device.

        Note: When you do a read-only operation, the data sent out of the MOSI line to the peripheral
        seems to be undefined -- it could literally be any garbage bytes that the serial bridge had laying around
        in memory.  So, unless your MOSI line is not hooked up, you probably want to use spi_transfer() instead.

        :param read_len: Length to read, in words
        :param io_timeout: Timeout for the transfer in ms.  Leave empty to compute a reasonable timeout automatically.
            Set to 0 to wait forever.

        :return: Bytes read from the device
        """
        if self._curr_frequency is None:
            message = "Must call set_spi_configuration() before reading or writing data!"
            raise CySerialBridgeError(message)

        if io_timeout is None:
            io_timeout = self._compute_timeout(read_len)

        # Set up transfer
        self.dev.controlWrite(
            request_type=CY_VENDOR_REQUEST_HOST_TO_DEVICE,
            request=CyVendorCmds.CY_SPI_READ_WRITE_CMD,
            value=(self.scb_index << CY_SCB_INDEX_POS) | CySpi.READ_BIT,
            index=read_len,
            data=b"",
            timeout=io_timeout,
        )

        # Get data.
        # It seems like the hardware can send multiple packets.
        try:
            # Note: the Cypress driver had special logic that would, on Mac, split the bulk transfer into
            # 64 byte read chunks.  The comments said it was to work around a libusb bug.  No idea
            # if this is still an issue, but for now I decided to KISS by not doing that.

            result: bytearray = self.dev.bulkRead(self.ep_in, read_len, timeout=io_timeout)

            if len(result) != read_len:
                message = f"Expected {read_len} bytes but only received {len(result)} bytes from bulk read!"
                raise CySerialBridgeError(message)

            return result

        except Exception:
            # If anything went wrong, try and reset the SPI module so that the next transaction works
            self._spi_reset()
            raise

    def spi_transfer(self, tx_data: ByteSequence, io_timeout: int | None = None) -> ByteSequence:
        """
        Perform an SPI read-and-write operation to the peripheral device.

        The bytes in tx_data will be sent, and the response by the peripheral to each
        byte will be recorded and returned.

        Note: This operation will always read and write the same length of data.  So, you may need to add
        additional padding to your tx_data to account for additional bytes that you want to read.

        :param tx_data: Data to write
        :param io_timeout: Timeout for the transfer in ms.  Leave empty to compute a reasonable timeout automatically.
            Set to 0 to wait forever.
        """
        if self._curr_frequency is None:
            message = "Must call set_spi_configuration() before reading or writing data!"
            raise CySerialBridgeError(message)

        if io_timeout is None:
            io_timeout = self._compute_timeout(len(tx_data))

        # Set up transfer
        self.dev.controlWrite(
            request_type=CY_VENDOR_REQUEST_HOST_TO_DEVICE,
            request=CyVendorCmds.CY_SPI_READ_WRITE_CMD,
            value=(self.scb_index << CY_SCB_INDEX_POS) | CySpi.WRITE_BIT | CySpi.READ_BIT,
            index=len(tx_data),
            data=b"",
            timeout=io_timeout,
        )

        try:
            # Send and receive data at the same time using async API
            tx_transfer = self.dev.getTransfer()
            rx_transfer = self.dev.getTransfer()

            tx_transfer.setBulk(self.ep_out, tx_data, timeout=io_timeout)
            rx_transfer.setBulk(self.ep_in, len(tx_data), timeout=io_timeout)

            tx_transfer.submit()
            rx_transfer.submit()

            start_time = time.time()

            # Wait for both transfers to finish, polling libusb until they are.
            while tx_transfer.isSubmitted() or rx_transfer.isSubmitted():
                with contextlib.suppress(
                    usb1.USBErrorInterrupted
                ):  # Suppressing this exception is recommended by the python-libusb1 docs
                    # Note: the best way to do this is to use libusb_handle_events_completed(),
                    # which allows handling events until a specific transfer is completed.
                    # That would allow us to cleanly block until the transfers are done.
                    # However, python-libusb1 currently doesn't provide an abstraction for that
                    # function.  Sadness.  So, we have to just keep polling instead.
                    # This is will work OK, but only as long as libusb is not used from another
                    # thread at the same time.
                    # Reference: https://libusb.sourceforge.io/api-1.0/libusb_mtasync.html#Using
                    self.context.usb_context.handleEvents()

                if (time.time() - start_time) > io_timeout:
                    raise usb1.USBErrorTimeout

            if tx_transfer.getStatus() == usb1.TRANSFER_STALL:
                # Attempt to handle pipe errors similarly to how the original driver did.
                self.dev.clearHalt(self.ep_out)

            if tx_transfer.getStatus() != usb1.TRANSFER_COMPLETED:
                message = "Tx transfer failed with error " + repr(tx_transfer.getStatus())
                raise CySerialBridgeError(message)

            if rx_transfer.getStatus() != usb1.TRANSFER_COMPLETED:
                message = "Rx transfer failed with error " + repr(rx_transfer.getStatus())
                raise CySerialBridgeError(message)

            if rx_transfer.getActualLength() != len(tx_data):
                message = f"Expected {len(tx_data)} bytes but only received {rx_transfer.getActualLength()} bytes from bulk read!"
                raise CySerialBridgeError(message)

            # Poll for write completion.  Oddly, unlike I2C, there is no interrupt functionality to tell when
            # the transfer is complete.
            while not self._spi_is_write_done():
                time.sleep(0.001)

                if time.time() > start_time + io_timeout:
                    message = "Timeout waiting for SPI write completion!"
                    raise CySerialBridgeError(message)

            return cast(ByteSequence, rx_transfer.getBuffer())

        except Exception:
            # If anything went wrong, try and reset the SPI module so that the next transaction works
            self._spi_reset()
            raise

from __future__ import annotations

import sys
import time
from enum import Enum
from typing import TYPE_CHECKING, Union, cast

if TYPE_CHECKING:
    from collections.abc import Sequence, Set

import serial
import usb1
from serial.tools import list_ports, list_ports_common

from cy_serial_bridge import driver
from cy_serial_bridge.usb_constants import DEFAULT_VIDS_PIDS, CyType, USBClass
from cy_serial_bridge.utils import CySerialBridgeError, DiscoveredDevice, log


class OpenMode(Enum):
    """
    Enumeration of the modes a serial bridge chip can be opened in.

    Value is a tuple of:
    - CY_TYPE that the chip must be in to use this mode, or None for any
    - Driver class that will be instantiated and returned
    """

    I2C_CONTROLLER = (CyType.I2C, driver.CyI2CControllerBridge)
    SPI_CONTROLLER = (CyType.SPI, driver.CySPIControllerBridge)
    MFGR_INTERFACE = (None, driver.CyMfgrIface)

    # Note: Unlike the other open modes, UART_CDC directly returns a pyserial Serial object
    # instead of a driver from this class
    UART_CDC = (CyType.UART_CDC, serial.Serial)


# Type annotation for anything that can be returned by
AnyDriverClass = Union[driver.CySPIControllerBridge, driver.CyI2CControllerBridge, driver.CyMfgrIface, serial.Serial]


class CyScbContext:
    """
    This class represents one instance of the Cypress Serial Bridge driver.

    It wraps a libusb context and allows searching for and opening devices.

    Warning: One context instance should only be opened by one thread, and may only have one driver open
        on it at a time.
    """

    def __init__(self) -> None:
        self.usb_context = usb1.USBContext()
        self.usb_context.open()
        self.has_opened_driver = False

    @staticmethod
    def _find_serial_port_name_for_serno(serial_number: str) -> str | None:
        """
        Find the serial port name for a device with the given serial number.

        Uses pyserial to do the hard work. If no device is found, returns None
        """
        serial_port_generator: Sequence[list_ports_common.ListPortInfo] = list_ports.comports()
        for serial_port in serial_port_generator:
            if serial_port.serial_number is not None:
                # Note: Testing on Windows, the serial number always gets converted to uppercase.
                # So we have to lowercase both values before comparing them.
                if serial_port.serial_number.lower() == serial_number.lower():
                    return serial_port.device

        return None

    def list_devices(
        self,
        vid_pids: Set[tuple[int, int]] | None = DEFAULT_VIDS_PIDS,
    ) -> list[DiscoveredDevice]:
        """
        Scan for USB devices which look like they could be CY6C652xx chips based on their USB descriptor layout.

        If a vid and pid set is given, only devices with the specified vid and pid will be considered.
        If the vid and pid set is left at the default, only the driver default vid and pid will be used.
        If the vid and pid set is set to None, all devices which *could* be CY6C652xx chips are returned.

        Note: For each PID value, both the even value (pid & 0xFFFE) and the odd value ((pid & 0xFFFE) + 1)
        will be considered.  This is to support UART CDC mode (see the README)
        """
        device_list: list[DiscoveredDevice] = []

        # In my testing, on Windows, this is needed in order to correctly detect re-enumerated devices
        # in some cases.  Seems to be some sort of libusb bug...
        if sys.platform == "win32":
            self.usb_context.close()
            self.usb_context.open()

        dev: usb1.USBDevice
        for dev in self.usb_context.getDeviceIterator(skip_on_error=True):
            even_vid_pid = (dev.getVendorID(), dev.getProductID() & 0xFFFE)
            odd_vid_pid = (dev.getVendorID(), (dev.getProductID() & 0xFFFE) + 1)

            if vid_pids is not None and even_vid_pid not in vid_pids and odd_vid_pid not in vid_pids:
                # Not a VID-PID we're looking for
                continue

            # CY7C652xx devices always have one configuration
            if len(dev) != 1:
                continue
            cfg: usb1.USBConfiguration = dev[0]

            # CY7C652xx devices always have either two or three interfaces: potentially one for the USB CDC COM port,
            # one for the actual USB-serial bridge, and one for the configuration interface.
            if cfg.getNumInterfaces() != 2 and cfg.getNumInterfaces() != 3:
                continue

            usb_cdc_interface_settings: usb1.USBInterfaceSetting | None = None
            cdc_data_interface_settings: usb1.USBInterfaceSetting | None = None
            scb_interface_settings: usb1.USBInterfaceSetting | None = None
            mfg_interface_settings: usb1.USBInterfaceSetting

            if cfg.getNumInterfaces() == 3 and cfg[0][0].getClass() == USBClass.CDC:
                # USB CDC mode
                usb_cdc_interface_settings = cfg[0][0]
                cdc_data_interface_settings = cfg[1][0]
                mfg_interface_settings = cfg[2][0]

                # Check USB CDC interface
                if usb_cdc_interface_settings.getSubClass() != 0x2:
                    continue

                # Check CDC Data interface
                if cdc_data_interface_settings.getClass() != 0x0A or cdc_data_interface_settings.getSubClass() != 0x0:
                    continue

                curr_cytype = CyType.UART_CDC

            else:
                # USB vendor mode
                scb_interface_settings = cfg[0][0]
                mfg_interface_settings = cfg[1][0]

                # Check SCB interface -- the Class should be 0xFF (vendor defined/no rules)
                # and the SubClass value gives the CyType
                if scb_interface_settings.getClass() != USBClass.VENDOR:
                    continue
                if scb_interface_settings.getSubClass() not in {
                    CyType.UART_VENDOR.value,
                    CyType.SPI.value,
                    CyType.I2C.value,
                }:
                    continue

                # Check SCB endpoints
                if scb_interface_settings.getNumEndpoints() != 3:
                    continue
                # Bulk host-to-dev endpoint
                if (
                    scb_interface_settings[0].getAddress() != 0x01
                    or (scb_interface_settings[0].getAttributes() & 0x3) != 2
                ):
                    continue
                # Bulk dev-to-host endpoint
                if (
                    scb_interface_settings[1].getAddress() != 0x82
                    or (scb_interface_settings[1].getAttributes() & 0x3) != 2
                ):
                    continue
                # Interrupt dev-to-host endpoint
                if (
                    scb_interface_settings[2].getAddress() != 0x83
                    or (scb_interface_settings[2].getAttributes() & 0x3) != 3
                ):
                    continue

                curr_cytype = CyType(scb_interface_settings.getSubClass())

            # Check manufacturer interface.
            # It has a defined class/subclass and has no endpoints
            if mfg_interface_settings.getClass() != 0xFF:
                continue
            if mfg_interface_settings.getSubClass() != CyType.MFG:
                continue
            if mfg_interface_settings.getNumEndpoints() != 0:
                continue

            # If we got all the way here, it looks like a CY6C652xx device!
            # Record attributes and add it to the list
            list_entry = DiscoveredDevice(
                usb_device=dev,
                usb_configuration=cfg,
                mfg_interface_settings=mfg_interface_settings,
                scb_interface_settings=scb_interface_settings,
                usb_cdc_interface_settings=usb_cdc_interface_settings,
                cdc_data_interface_settings=cdc_data_interface_settings,
                vid=dev.getVendorID(),
                pid=dev.getProductID(),
                curr_cytype=curr_cytype,
                open_failed=False,
            )
            try:
                opened_device = dev.open()
                list_entry.manufacturer_str = opened_device.getManufacturer()
                list_entry.product_str = opened_device.getProduct()
                list_entry.serial_number = opened_device.getSerialNumber()
            except usb1.USBError:
                list_entry.open_failed = True

            # Iff this is a CDC serial device, find its associated COM port.
            # Luckily, pyserial does the hard work of talking to the OS for us here.
            if curr_cytype == CyType.UART_CDC and not list_entry.open_failed:
                if list_entry.serial_number is None:
                    log.warning(
                        "Discovered CY7C652xx device in UART mode with no serial number configured.  Will "
                        "not be able to open a terminal to this device until it is configured with a "
                        "serial number."
                    )
                else:
                    list_entry.serial_port_name = self._find_serial_port_name_for_serno(list_entry.serial_number)

            device_list.append(list_entry)

        return device_list

    # Time we allow for the device to change its type and be enumerated on the USB bus:
    # It can take quite some time for the OS to re-enumerate the serial port
    CHANGE_TYPE_TIMEOUT = 10.0  # s

    def scan_for_device(
        self, vid: int, pids: Union[int, set[int]], open_mode: OpenMode, serial_number: str | None = None
    ) -> DiscoveredDevice:
        """
        Lists all devices on the system, and then tries to find a match for the given vid, pid, and serial number.

        If no or multiple matches are found, throws an exception containing the reason.

        :param open_mode: Mode to open the SCB device in
        :param vid: Vendor ID of the device you want to open
        :param pids: Product IDs of the device you want to open.  Accepts either a single integer or a set of ints
        :param serial_number: Serial number of the device you want to open.  May be left as None if there is only one device attached.
        """
        if type(pids) is int:
            pids = {pids}

        # pids will always be a set[int] at this point but mypy can't seem to figure that out
        pids = cast(set[int], pids)

        devices = self.list_devices({(vid, pid) for pid in pids})

        # print("Scan results:" + str(devices))

        if len(devices) == 0:
            message = "No devices found"
            raise CySerialBridgeError(message)
        elif len(devices) == 1:
            # Exactly 1 device found
            device_to_open = devices[0]

            if device_to_open.open_failed:
                message = f"Found device with VID:PID {vid:04x}:{device_to_open.pid:04x} but cannot open it!"
                if sys.platform == "win32":
                    message += "  This is likely because it does not have the WinUSB driver attached."
                raise CySerialBridgeError(message)

            if serial_number is not None and device_to_open.serial_number != serial_number:
                message = "The only detected device does not have a matching serial number!"
                raise CySerialBridgeError(message)

        else:  # Multiple devices
            # Search by serial number
            if serial_number is None:
                message = "Multiple devices found but no serial number provided!"
                raise CySerialBridgeError(message)

            any_unopenable_devices = False
            device_to_open = None
            for device in devices:
                if device.open_failed:
                    any_unopenable_devices = True
                elif device.serial_number == serial_number:
                    device_to_open = device
                    break

            if device_to_open is None:
                if any_unopenable_devices:
                    message = "Did not find an exact match for serial number.  However, at least one candidate device with was found that could not be opened!"
                    if sys.platform == "win32":
                        message += "  This is likely because it does not have the WinUSB driver attached."
                    raise CySerialBridgeError(message)
                else:
                    message = "Multiple devices found but none matched the specified serial number!"
                    raise CySerialBridgeError(message)

        # mypy isn't smart enough to understand that device_to_open cannot be None at this point
        # so we have to help it out.
        device_to_open = cast(DiscoveredDevice, device_to_open)

        # If opening in UART CDC mode, we have to be able to detect the serial port in order to open the device
        if (
            open_mode == OpenMode.UART_CDC
            and device_to_open.curr_cytype == CyType.UART_CDC
            and device_to_open.serial_port_name is None
        ):
            message = "Unable to detect the correct serial port to open for this device!"
            raise CySerialBridgeError(message)

        return device_to_open

    def open_device(
        self, vid: int, pids: Union[int, set[int]], open_mode: OpenMode, serial_number: str | None = None
    ) -> AnyDriverClass:
        """
        Convenience function for opening a CY7C652xx SCB device in a desired mode.

        Unlike creating an instance of the driver class directly, this function attempts to abstract away
        management of the device's CyType and will automatically change its type to the needed one.

        Note: For each PID value, both the even value (pid & 0xFFFE) and the odd value ((pid & 0xFFFE) + 1)
        will be considered.  This is to support UART CDC mode (see the README)

        :param vid: Vendor ID of the device you want to open
        :param pids: Product IDs of the device you want to open.  Accepts either a single integer or a set of ints
        :param serial_number: Serial number of the device you want to open.  May be left as None if there is only one device attached.
        :param open_mode: Mode to open the SCB device in
        """
        # Step 1: Search for matching devices on the system
        if type(pids) is int:
            pids = {pids}

        # pids will always be a set[int] at this point but mypy can't seem to figure that out
        pids = cast(set[int], pids)

        device_to_open = self.scan_for_device(vid, pids, open_mode, serial_number)

        # Step 2: Change type of the device, if needed
        needed_cytype: CyType | None = open_mode.value[0]
        driver_class: type[AnyDriverClass] = open_mode.value[1]
        if needed_cytype is not None and device_to_open.curr_cytype != needed_cytype:
            log.info(
                f"The CyType of this device must be changed to {needed_cytype.name} in order to open it as {open_mode.name}"
            )
            change_type_start_time = time.time()

            # Open the device in manufacturer mode and change its type
            with driver.CyMfgrIface(self, device_to_open) as mfgr_driver:
                mfgr_driver.change_type(needed_cytype)
                mfgr_driver.reset_device()

            # Wait for the device to re-enumerate with the new type
            while True:
                try:
                    device_to_open = self.scan_for_device(vid, pids, open_mode, serial_number)

                    # log.debug(f"Scan found a device with CyType {device_to_open.curr_cytype}")

                    if device_to_open.curr_cytype == needed_cytype:
                        break
                except Exception as ex:
                    if time.time() < change_type_start_time + self.CHANGE_TYPE_TIMEOUT:
                        # Not found but still within the timeout, wait a bit and try again
                        time.sleep(0.01)
                    else:
                        message = "Timeout waiting for device to re-enumerate after changing its type."
                        raise CySerialBridgeError(message) from ex

                if (
                    device_to_open is not None
                    and time.time() >= change_type_start_time + self.CHANGE_TYPE_TIMEOUT
                    and device_to_open.curr_cytype != needed_cytype
                ):
                    message = "The CyType of the device did not change to the correct value within the timeout!"
                    raise CySerialBridgeError(message)

            log.info(f"Changed type of device in {time.time() - change_type_start_time:.04f} sec")

        # Step 3: Instantiate the driver!
        if open_mode == OpenMode.UART_CDC:
            if device_to_open.serial_port_name is None:
                message = (
                    "Cannot open this device as cy_serial_bridge could not determine the COM port/"
                    "TTY that it's connected to."
                )
                raise CySerialBridgeError(message)
            return serial.Serial(port=device_to_open.serial_port_name)
        else:
            # driver_class cannot be Serial at this time
            driver_class = cast(
                type[Union[driver.CySPIControllerBridge, driver.CyI2CControllerBridge, driver.CyMfgrIface]],
                driver_class,
            )
            return driver_class(self, device_to_open)

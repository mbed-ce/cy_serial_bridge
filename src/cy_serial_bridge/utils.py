from __future__ import annotations

import collections.abc
import dataclasses
import logging
import sys
from typing import TYPE_CHECKING, Union

if TYPE_CHECKING:
    import usb1

    from cy_serial_bridge.usb_constants import CyType

"""
Module with basic definitions used by multiple cy_serial_bridge modules.
"""

# Get type annotation for "any type of byte sequence".  This changed in Python 3.12
if sys.version_info < (3, 12):
    ByteSequence = collections.abc.ByteString  # noqa: PYI057
else:
    ByteSequence = Union[bytes, bytearray, memoryview]

# Logger for the package
log = logging.getLogger("cy_serial_bridge")


# Base exception for the package
class CySerialBridgeError(Exception):
    pass


@dataclasses.dataclass
class DiscoveredDevice:
    """
    Represents one detected device on the system
    """

    # USBDevice object for the discovered device
    usb_device: usb1.USBDevice

    # USBConfiguration object for the discovered device
    usb_configuration: usb1.USBConfiguration

    # USB interface settings for the manufacturer interface (always present)
    mfg_interface_settings: usb1.USBInterfaceSetting

    # USB interface settings for the Serial Control Block we want to use.
    # Set except when in CDC mode.
    scb_interface_settings: usb1.USBInterfaceSetting | None

    # when in UART CDC CDC mode only: USB CDC interface
    usb_cdc_interface_settings: usb1.USBInterfaceSetting | None
    cdc_data_interface_settings: usb1.USBInterfaceSetting | None

    # Vendor ID
    vid: int

    # Product ID
    pid: int

    # Current CyType setting (SPI, I2C, or UART)
    curr_cytype: CyType

    # If this is true, opening the device failed and the manufacturer, product, and serial_number fields
    # will not be populated.
    # On Windows, opening will fail for any USB devices which do not have the WinUSB driver attached
    # and are not HID devices.
    open_failed: bool

    # Manufacturer string
    manufacturer_str: str | None = None

    # Product name string
    product_str: str | None = None

    # Serial number string
    serial_number: str | None = None

    # Name of this device's serial port, e.g. COM3 or /dev/ttyACM0
    # Only populated for UART CDC devices that we were able to open.
    serial_port_name: str | None = None

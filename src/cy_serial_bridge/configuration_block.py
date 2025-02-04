from __future__ import annotations

import pathlib
import re
import struct
from typing import TYPE_CHECKING, cast

from cy_serial_bridge.usb_constants import CY_CONFIG_STRING_MAX_LEN_BYTES, CY_DEVICE_CONFIG_SIZE, CyType

if TYPE_CHECKING:
    from cy_serial_bridge.utils import ByteSequence

CONFIG_BLOCK_EXPECTED_MAGIC = b"CYUS"

# Version 1.0.3 observed on CY7C65211.
# Version 2.0.3 observed on CY7C65211A.
# So far no differences have been determined between the two versions.
CONFIG_BLOCK_EXPECTED_MAJOR_VERSIONS = {1, 2}


class ConfigurationBlock:
    """
    Module which implements basic reading/writing of CY7C652xx memory configuration blocks.

    These blocks are written to the chip using the reverse engineered API in order to configure the interface
    mode (UART/I2C/SPI) and set other settings such as GPIOs.  Nominally, only the Cypress USB-Serial Configuration
    Utility know how to create this data, and this program is only available as a Windows GUI application.

    This class is based on the reverse-engineered description of the format located here: https://github.com/tai/cyusb-hack/blob/master/config.txt
    """

    def __init__(self, block_file: pathlib.Path | str | None = None, block_bytes: ByteSequence | None = None):
        """
        Create a configuration_block from a file or byte array.  Must pass either a file path OR a bytes object.

        :param block_file:
        :param block_bytes:
        """
        if (block_bytes is None and block_file is None) or (block_bytes is not None and block_file is not None):
            message = "Invalid usage!"
            raise ValueError(message)

        if block_file is not None:
            # Load bytes from file.
            self._cfg_bytes = bytearray(pathlib.Path(block_file).read_bytes())
        elif block_bytes is not None:
            self._cfg_bytes = bytearray(block_bytes)

        if len(self._cfg_bytes) < CY_DEVICE_CONFIG_SIZE:
            message = f"Configuration block data is not long enough (should be {CY_DEVICE_CONFIG_SIZE} bytes)"
            raise ValueError(message)

        # Some dumps contain extra bytes so trim to 512 bytes.
        self._cfg_bytes = self._cfg_bytes[: CY_DEVICE_CONFIG_SIZE + 1]

        # Check magic, format, and checksum
        if self._cfg_bytes[0:4] != CONFIG_BLOCK_EXPECTED_MAGIC:
            message = "Incorrect magic at start of configuration block"
            raise ValueError(message)
        if self.config_format_version[0] not in CONFIG_BLOCK_EXPECTED_MAJOR_VERSIONS:
            message = f"Only know how to work with config block major versions {', '.join(str(ver) for ver in CONFIG_BLOCK_EXPECTED_MAJOR_VERSIONS)} this is 0x{self.config_format_version[0]}"
            raise ValueError(message)
        if self._get_checksum() != self._calculate_checksum():
            message = f"Checksum failed for configuration block.  Expected 0x{self._calculate_checksum():x} but read 0x{self._get_checksum():x} from header"
            raise ValueError(message)

    def _decode_string_field(self, flag_addr: int, data_start_addr: int) -> str | None:
        """
        Decode a variable-length string from the config block.

        :param flag_addr: Address of the flag indicating whether this data is set or not
        :param data_start_addr: Address that the data starts at (this is the address of the length field, 2 bytes before the first character)
        :return: String data, or None if unset
        """
        # The string fields have three parts (turns out to be slightly different than what's described in the
        # original reverse engineering document):
        # - 4 byte flag, set to 0xffffffff if present, 0x00000000 otherwise
        # - length field -- gives 2 longer than the number of bytes in the string (no idea why it's 2 longer)
        # - 64 bytes of data (encoded as UTF-16)
        #
        # Note: There is always a 0x3 byte after the length byte before the data.  No idea what this is for.

        if self._cfg_bytes[flag_addr : flag_addr + 4] == b"\xff\xff\xff\xff":
            byte_count = self._cfg_bytes[data_start_addr] - 2
            chars_start_addr = data_start_addr + 2
            chars_end_addr = chars_start_addr + byte_count

            return self._cfg_bytes[chars_start_addr:chars_end_addr].decode("utf-16-le")
        elif self._cfg_bytes[flag_addr : flag_addr + 4] == b"\x00\x00\x00\x00":
            return None
        else:
            message = "Unparseable data in descriptor"
            raise ValueError(message)

    def _encode_string_field(self, flag_addr: int, data_start_addr: int, value: str | None) -> None:
        """
        Encode a variable-length string from the config block.

        :param flag_addr: Address of the flag indicating whether this data is set or not
        :param data_start_addr: Address that the data starts at (this is the address of the length field, 2 bytes before the first character)
        :param value: String data, or None if unset
        """
        if value is None:
            self._cfg_bytes[flag_addr : flag_addr + 4] = b"\x00\x00\x00\x00"  # Set present flag to false
            self._cfg_bytes[data_start_addr] = 2  # Set length to 0 chars (can't forget the 2 offset)
            self._cfg_bytes[data_start_addr + 2 : data_start_addr + CY_CONFIG_STRING_MAX_LEN_BYTES + 2] = (
                CY_CONFIG_STRING_MAX_LEN_BYTES * b"\x00"
            )  # Zero out data
        else:
            # Write data (padded with 0s)
            encoded_string = value.encode("utf-16-le")
            if len(encoded_string) > CY_CONFIG_STRING_MAX_LEN_BYTES:
                message = "String value to long to fit in binary configuration block!"
                raise ValueError(message)
            self._cfg_bytes[data_start_addr + 2 : data_start_addr + CY_CONFIG_STRING_MAX_LEN_BYTES + 2] = (
                encoded_string + (CY_CONFIG_STRING_MAX_LEN_BYTES - len(encoded_string)) * b"\x00"
            )

            self._cfg_bytes[data_start_addr] = len(encoded_string) + 2  # Set length
            self._cfg_bytes[flag_addr : flag_addr + 4] = b"\xff\xff\xff\xff"  # Set present flag to true

    def _calculate_checksum(self) -> int:
        """Return checksum of 512-byte config bytes"""
        checksum: int = sum(struct.unpack("<125I", self._cfg_bytes[12:]))
        return 0xFFFFFFFF & checksum

    def _get_checksum(self) -> int:
        """Extract checksum value in 512-byte config bytes"""
        checksum: int = struct.unpack("<I", self._cfg_bytes[8:12])[0]
        return checksum

    @property
    def device_type(self) -> CyType:
        """
        Type of device that this configuration describes (SPI/I2C/UART/etc).

        Note: I would not recommend setting this to JTAG, MFGR, or DISABLED; I do not know what the
        hardware will do with those values as they are not officially supported modes.
        """
        if self._cfg_bytes[0x1D] == 0x01 and self._cfg_bytes[0x1C] == CyType.UART_VENDOR.value:
            return CyType.UART_CDC
        elif self._cfg_bytes[0x1D] == 0x02 and self._cfg_bytes[0x1C] == 0x01:
            return CyType.UART_PHDC
        elif self._cfg_bytes[0x1D] == 0x03:
            return CyType(self._cfg_bytes[0x1C])
        else:
            message = "Don't know how to parse DeviceType from descriptor"
            raise ValueError(message)

    @device_type.setter
    def device_type(self, value: CyType) -> None:
        if value == CyType.UART_CDC:
            self._cfg_bytes[0x1C] = CyType.UART_VENDOR.value
            self._cfg_bytes[0x1D] = 0x01
        elif value == CyType.UART_PHDC:
            self._cfg_bytes[0x1C] = CyType.UART_VENDOR.value
            self._cfg_bytes[0x1D] = 0x02
        else:
            self._cfg_bytes[0x1C] = value.value
            self._cfg_bytes[0x1D] = 0x03

    @property
    def config_format_version(self) -> tuple[int, int, int]:
        """
        Version of the configuration block format (major-minor-patch)
        """
        # I observed that in an older dump file that @tai posted, the version bytes were "01 00 00 00", and in
        # dumps from my device (with firmware version 1.0.3 build 78) they are "01 00 03 00".  This causes me
        # to think that the bytes are either major-minor-patch version of the configuration block, or are
        # the major-minor-patch version of the firmware that wrote the config block.  Either way, they seem to
        # be organized in major-minor-patch format.
        return self._cfg_bytes[4], self._cfg_bytes[5], self._cfg_bytes[6]

    @property
    def capsense_on(self) -> bool:
        """
        Whether CapSense touch sensing is enabled in this configuration

        [note that this utility currently doesn't support writing all the fields needed to make capsense work]
        """
        return self._cfg_bytes[0x4C] == 1

    @property
    def vid(self) -> int:
        """
        USB Vendor ID of the device
        """
        vid: int = struct.unpack("<H", self._cfg_bytes[0x94:0x96])[0]
        return vid

    @vid.setter
    def vid(self, value: int) -> None:
        self._cfg_bytes[0x94:0x96] = struct.pack("<H", (value))

    @property
    def pid(self) -> int:
        """
        USB Product ID of the device
        """
        pid: int = struct.unpack("<H", self._cfg_bytes[0x96:0x98])[0]
        return pid

    @pid.setter
    def pid(self, value: int) -> None:
        self._cfg_bytes[0x96:0x98] = struct.pack("<H", (value))

    @property
    def mfgr_string(self) -> str | None:
        """
        Manufacturer String of the device.  Up to 32 characters (seems to be UTF-16 type encoded in descriptor).

        May be set to None, indicating that the field is unset.
        """
        return self._decode_string_field(0xA0, 0xEE)

    @mfgr_string.setter
    def mfgr_string(self, value: str | None) -> None:
        self._encode_string_field(0xA0, 0xEE, value)

    @property
    def product_string(self) -> str | None:
        """
        Product String of the device.  Up to 32 characters (seems to be UTF-16 type encoded in descriptor).

        May be set to None, indicating that the field is unset.
        """
        return self._decode_string_field(0xA4, 0x130)

    @product_string.setter
    def product_string(self, value: str | None) -> None:
        self._encode_string_field(0xA4, 0x130, value)

    @property
    def serial_number(self) -> str | None:
        """
        Serial Number of the device.  Up to 32 characters (seems to be UTF-16 type encoded in descriptor).

        May be set to None, indicating that the field is unset.
        The serial number, according the config utility, may only be set to alphabetic and numeric characters.

        Warning: Some contexts (looking at you, Win32 API) do not preserve the case of the USB serial number.
        So it is strongly recommended to not have two devices whose serial numbers differ only by case, or
        cy_serial_bridge may not be able to distinguish between them in some situations.
        """
        return self._decode_string_field(0xA8, 0x172)

    @serial_number.setter
    def serial_number(self, value: str | None) -> None:
        if value is not None and re.fullmatch(r"[0-9a-zA-Z]+", value) is None:
            message = "Serial number may only be set to alphanumeric characters"
            raise ValueError(message)

        self._encode_string_field(0xA8, 0x172, value)

    @property
    def default_frequency(self) -> int:
        """
        Default UART baudrate or SPI/I2C clock frequency when the serial bridge initializes
        """
        # Baudrate is a three byte integer so we have to append a 0 MSByte
        return cast(int, struct.unpack("<I", self._cfg_bytes[0x24:0x27] + b"\x00")[0])

    @default_frequency.setter
    def default_frequency(self, value: int) -> None:
        # Frequency may never be higher than 3MHz in any mode
        if value > 3e6:
            message = "Frequency may not be higher than 3MHz"
            raise ValueError(message)

        self._cfg_bytes[0x24:0x27] = struct.pack("<I", value)[0:3]

    @property
    def config_bytes(self) -> bytearray:
        """
        Get the raw configuration bytes for this buffer.

        Calling this function also updates the checksum to account for any changes made to the bytes since
        the config block was updated.
        """
        self._cfg_bytes[8:12] = struct.pack("<I", self._calculate_checksum())
        return self._cfg_bytes

    def __str__(self) -> str:
        """
        Dump the decodable information from this config block.
        """
        return f"""ConfigurationBlock(
    config_format_version={".".join(str(part) for part in self.config_format_version)}
    device_type=CY_TYPE.{self.device_type.name},
    vid=0x{self.vid:04x},
    pid=0x{self.pid:04x},
    mfgr_string=\"{self.mfgr_string}\",
    product_string=\"{self.product_string}\",
    serial_number=\"{self.serial_number}\",
    capsense_on={self.capsense_on},
    default_frequency={self.default_frequency}
)"""

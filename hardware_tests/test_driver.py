import logging
import pathlib
import random
import time
from typing import cast

import pytest
import serial

import cy_serial_bridge
from cy_serial_bridge import OpenMode
from cy_serial_bridge.usb_constants import DEFAULT_PID, DEFAULT_VID

"""
Test suite for the CY7C652xx driver.
This test suite _requires access to hardware_ and MUST BE run on a machine
with a matching device plugged into it.  Additionally, at certain points in the test, jumper
changes are required, so you will be prompted to make changes.
"""

PROJECT_ROOT_DIR = pathlib.Path(__file__).parent.parent

# Eval kit has a 24LC128 EEPROM with A[2..0] = 001
EEPROM_I2C_ADDRESS = 0x51
EEPROM_PAGE_SIZE = 64


context = cy_serial_bridge.CyScbContext()


def test_user_flash():
    """
    Test ability to use the user flash programming functionality of the device
    """
    # Enable more detailed logs during the tests
    logging.basicConfig(level=logging.INFO)
    cy_serial_bridge.log.setLevel(logging.INFO)

    # Note: the mode that we open the device in doesn't really matter, it can be anything
    # for this test
    with cast(
        cy_serial_bridge.driver.CyMfgrIface, context.open_device(DEFAULT_VID, DEFAULT_PID, OpenMode.MFGR_INTERFACE)
    ) as dev:
        # Create a random 8-digit number which will be used in the test.
        # This ensures the flash is actually getting programmed and we aren't just reusing old data.
        random_number = random.randint(0, 10**8 - 1)

        # Page 1 wil be programmed in the first operation
        page_1_message = f"Hello from page 1! Number is {random_number:08}"
        page_1_bytes = page_1_message.encode("utf-8") + b"a" * (
            cy_serial_bridge.usb_constants.USER_FLASH_PAGE_SIZE - len(page_1_message)
        )

        # Pages 2-4 will be programmed in the second operation
        page_3_message = f"Hello from page 3! Number is {random_number:08}"
        page_3_bytes = page_3_message.encode("utf-8") + b"c" * (
            cy_serial_bridge.usb_constants.USER_FLASH_PAGE_SIZE - len(page_3_message)
        )
        remaining_pages_bytes = (
            b"b" * cy_serial_bridge.usb_constants.USER_FLASH_PAGE_SIZE
            + page_3_bytes
            + b"d" * cy_serial_bridge.usb_constants.USER_FLASH_PAGE_SIZE
        )

        print("Programming page 1: " + repr(page_1_bytes))
        dev.program_user_flash(0, page_1_bytes)

        print("Programming pages 2-4: " + repr(remaining_pages_bytes))
        dev.program_user_flash(cy_serial_bridge.usb_constants.USER_FLASH_PAGE_SIZE, remaining_pages_bytes)

        # First read the entire memory contents and check that it's as expected
        entire_mem = dev.read_user_flash(0, cy_serial_bridge.usb_constants.USER_FLASH_SIZE)
        print("Read entire memory space: " + repr(entire_mem))
        assert entire_mem == (page_1_bytes + remaining_pages_bytes)

        # Also test a 1 page read
        page_3_mem = dev.read_user_flash(
            2 * cy_serial_bridge.usb_constants.USER_FLASH_PAGE_SIZE, cy_serial_bridge.usb_constants.USER_FLASH_PAGE_SIZE
        )
        print("Read page 3 only: " + repr(page_3_mem))
        assert page_3_mem == page_3_bytes


def test_open_by_serial_number():
    """
    Test that open_device() uses the serial number filter correctly
    """
    # For this test to work there should be exactly 1 device connected
    available_devices = context.list_devices()
    assert len(available_devices) == 1

    # If passing a junk serial number we should get no device
    with pytest.raises(cy_serial_bridge.CySerialBridgeError, match="does not have a matching serial number"):
        context.open_device(DEFAULT_VID, {DEFAULT_PID}, OpenMode.MFGR_INTERFACE, serial_number="1234")

    # Opening the real detected serial number should work
    with context.open_device(
        DEFAULT_VID, {DEFAULT_PID}, OpenMode.MFGR_INTERFACE, serial_number=available_devices[0].serial_number
    ):
        pass


def test_double_open():
    """
    Test that attempting to open two devices with one context generates an error.
    """
    with (
        context.open_device(DEFAULT_VID, {DEFAULT_PID}, OpenMode.MFGR_INTERFACE),
        pytest.raises(cy_serial_bridge.CySerialBridgeError, match="already has a driver open"),
        context.open_device(DEFAULT_VID, {DEFAULT_PID}, OpenMode.MFGR_INTERFACE),
    ):
        pass


def test_auto_change_type():
    """
    Test that open_device() can automatically change the device's type
    """
    # Opening as SPI -> change type to SPI
    with context.open_device(DEFAULT_VID, DEFAULT_PID, OpenMode.SPI_CONTROLLER):
        pass

    # Opening as I2C -> change type to I2C
    with context.open_device(DEFAULT_VID, DEFAULT_PID, OpenMode.I2C_CONTROLLER):
        pass

    # Opening as UART -> change type to UART
    with context.open_device(DEFAULT_VID, DEFAULT_PID, OpenMode.UART_CDC):
        pass


def test_i2c_config_set_get():
    """
    Test that we can get and set the I2C controller mode config for the USB device
    """
    print("Please connect jumpers on the eval kit:")
    print("J17 = 2-3")
    print("J20 = 2-3")
    input("Press [ENTER] when done...")

    with cast(
        cy_serial_bridge.driver.CyI2CControllerBridge,
        context.open_device(DEFAULT_VID, {DEFAULT_PID}, OpenMode.I2C_CONTROLLER),
    ) as dev:
        print("Setting speed to 400kHz...")
        max_speed_config = cy_serial_bridge.driver.CyI2CConfig(400000)
        dev.set_i2c_configuration(max_speed_config)

        curr_config = dev.read_i2c_configuration()
        print("Read back: " + str(curr_config))
        assert curr_config == max_speed_config

        print("Setting speed to 50kHz...")
        low_speed_config = cy_serial_bridge.driver.CyI2CConfig(50000)
        dev.set_i2c_configuration(low_speed_config)

        curr_config = dev.read_i2c_configuration()
        print("Read back: " + str(curr_config))
        assert curr_config == low_speed_config


def test_i2c_read_write():
    """
    Test sending I2C read and write transactions
    """
    with cast(
        cy_serial_bridge.driver.CyI2CControllerBridge,
        context.open_device(DEFAULT_VID, {DEFAULT_PID}, OpenMode.I2C_CONTROLLER),
    ) as dev:
        dev.set_i2c_configuration(cy_serial_bridge.driver.CyI2CConfig(400000))

        # Basic read/write operations
        # ---------------------------------------------------------------------------

        # Try a 1 byte read from the EEPROM address to make sure it ACKs
        dev.i2c_read(EEPROM_I2C_ADDRESS, 1)

        # Try a 1 byte read from an incorrect address to make sure it does not ACK
        with pytest.raises(cy_serial_bridge.I2CNACKError) as raises:
            dev.i2c_read(EEPROM_I2C_ADDRESS + 0x10, 1)
        assert raises.value.bytes_written == 0

        # Try a short write to the EEPROM address to make sure it ACKs
        dev.i2c_write(EEPROM_I2C_ADDRESS, b"\x00\x00")

        # Try an addr-only write to an incorrect address to make sure it does not ACK
        with pytest.raises(cy_serial_bridge.I2CNACKError) as raises:
            dev.i2c_write(EEPROM_I2C_ADDRESS + 0x10, b"\x00\x00")

        # TODO this seems to be not working
        # assert raises.value.bytes_written == 0

        # Write something to the EEPROM and then read it back
        # ---------------------------------------------------------------------------

        # Create a random 8-digit number which will be used in the test.
        # This ensures the flash is actually getting programmed and we aren't just reusing old data.
        random_number = random.randint(0, 10**8 - 1)
        eeprom_message = f"Hello from EEPROM! Number is {random_number:08}".encode()
        assert len(eeprom_message) <= EEPROM_PAGE_SIZE

        eeprom_address = 0x0100  # Must be 64 byte aligned

        write_command = bytes([(eeprom_address >> 8) & 0xFF, eeprom_address & 0xFF, *eeprom_message])
        print("Writing: " + repr(write_command))
        dev.i2c_write(EEPROM_I2C_ADDRESS, write_command)

        time.sleep(0.01)  # EEPROM needs at least 5ms page program time before it can respond again

        # Reset address pointer
        dev.i2c_write(
            EEPROM_I2C_ADDRESS, bytes([(eeprom_address >> 8) & 0xFF, eeprom_address & 0xFF]), relinquish_bus=False
        )

        # Read data back
        read_data = dev.i2c_read(EEPROM_I2C_ADDRESS, len(eeprom_message))

        print("Got back: " + repr(read_data))

        assert read_data == eeprom_message


def test_spi_config_read_write():
    """
    Test that we can read and write SPI configs from the device
    """
    print("Please connect jumpers on the eval kit:")
    print("J17 = 2-5 [MISO]")
    print("J19 = 2-3 [CS]")
    print("J21 = 2-3 [SCLK]")
    print("J20 = 2-5 [MOSI]")
    input("Press [ENTER] when done...")

    with cast(
        cy_serial_bridge.driver.CySPIControllerBridge,
        context.open_device(DEFAULT_VID, {DEFAULT_PID}, OpenMode.SPI_CONTROLLER),
    ) as dev:
        config_1 = cy_serial_bridge.CySPIConfig(
            frequency=20000,
            word_size=16,
            mode=cy_serial_bridge.CySPIMode.NATIONAL_MICROWIRE,
            msbit_first=False,
            continuous_ssel=False,
            ti_select_precede=True,
        )
        print("Setting SPI configuration: " + repr(config_1))
        dev.set_spi_configuration(config_1)

        read_config_1 = dev.read_spi_configuration()
        print("Got back SPI configuration: " + repr(read_config_1))
        assert read_config_1 == config_1

        config_2 = cy_serial_bridge.CySPIConfig(
            frequency=1000000,
            word_size=8,
            mode=cy_serial_bridge.CySPIMode.MOTOROLA_MODE_1,
            msbit_first=True,
            continuous_ssel=True,
            ti_select_precede=False,
        )
        print("Setting SPI configuration: " + repr(config_2))
        dev.set_spi_configuration(config_2)

        read_config_2 = dev.read_spi_configuration()
        print("Got back SPI configuration: " + repr(read_config_2))
        assert read_config_2 == config_2


class M95M02Driver:
    """
    Rudimentary driver for the M95M02 SPI EEPRPOM on the EVK board
    """

    def __init__(self, dev: cy_serial_bridge.CySPIControllerBridge):
        self._dev = dev

        # Set correct SPI configuration
        eeprom_spi_config = cy_serial_bridge.CySPIConfig(
            frequency=2000000,  # EEPROM max frequency 5MHz, so we play it a bit safe with 2MHz
            word_size=8,
            mode=cy_serial_bridge.CySPIMode.MOTOROLA_MODE_0,  # EEPROM can use either SPI mode 0 or SPI mode 3
            msbit_first=True,
            continuous_ssel=True,
        )
        self._dev.set_spi_configuration(eeprom_spi_config)

    def read_status_register(self) -> int:
        """
        Read the status register byte from the EEPROM.

        See datasheet Figure 11 for values.
        """
        result = self._dev.spi_transfer(bytes([0x5, 0, 0]))
        return result[1]

    def write(self, address: int, data: bytes):
        """
        Write the given bytes to the given address.

        Address may be any value between 0 and 262143.
        Up to one page (256 bytes) of data may be written in one write operation.
        However, the write may not cross page boundaries.
        For example, if writing to address 512, you may write up to 256 bytes.
        However, if writing to address 760 (768 - 8), you may only write up to 8 bytes.
        """
        # First we need to enable writing
        self._dev.spi_write(bytes([0x6]))  # WREN

        # Status register should now indicate that writes are enabled
        assert self.read_status_register() == 0x2

        # Send write command
        self._dev.spi_write(bytes([0x2, (address >> 16) & 0xFF, (address >> 8) & 0xFF, address & 0xFF]) + data)

        # Status register should read as 1 for up to 10ms until the write completes
        wait_start_time = time.time()
        while time.time() - wait_start_time <= 0.01:
            if (self.read_status_register() & 0x1) == 0:
                break

        # Status register should now be 0, indicating write complete
        assert self.read_status_register() == 0

    def read(self, address: int, read_len: int):
        """
        Read data from the EEPROM.

        There are no page limits for reading -- you may read any number of bytes from any page in the device.
        """
        tx_bytes = bytes([0x3, (address >> 16) & 0xFF, (address >> 8) & 0xFF, address & 0xFF]) + b"\x00" * read_len
        return self._dev.spi_transfer(tx_bytes)[4:]


def test_spi_read_write():
    """
    Test using the CY7C652xx to read and write the EEPROM on the dev board
    """
    with cast(
        cy_serial_bridge.driver.CySPIControllerBridge,
        context.open_device(DEFAULT_VID, {DEFAULT_PID}, OpenMode.SPI_CONTROLLER),
    ) as dev:
        eeprom_driver = M95M02Driver(dev)

        random_number = random.randint(0, 10**8 - 1)
        eeprom_message = f"Hello from M95M02 EEPROM! Number is {random_number:08}".encode()

        # Program the message into the 3rd page
        eeprom_driver.write(514, eeprom_message)

        # Read the data back and make sure it's correct
        readback = eeprom_driver.read(514, len(eeprom_message))
        assert readback == eeprom_message


# TODO do an SPI test showing how to use word sizes > 8


def test_uart_loopback():
    """
    Test that we can send bytes over UART and they'll come back to us
    """
    print("Please connect a female-female jumper wire from J18 middle pin [Rx] to J21 middle pin [Tx].")
    input("Press [ENTER] when done...")

    serial_port: serial.Serial = cast(
        serial.Serial, context.open_device(DEFAULT_VID, {DEFAULT_PID}, cy_serial_bridge.OpenMode.UART_CDC)
    )
    serial_port.baudrate = 3000000  # Theoretically fastest supported by CY7C652xx
    serial_port.timeout = 0.1  # Shouldn't take too long to see the loopback

    test_string = b"Hello world!\n"
    serial_port.write(test_string)

    readback = serial_port.read(len(test_string))

    assert readback == test_string

# Test of device scanning

from __future__ import annotations

import copy
from collections.abc import Sequence

import usb1

import cy_serial_bridge
from cy_serial_bridge import CyType


class MockUSBEndpoint:
    """Mock of usb1.USBEndpoint"""

    def __init__(self, address: int, attributes: int):
        self.address = address
        self.attributes = attributes

    def getAddress(self):
        return self.address

    def getAttributes(self):
        return self.attributes


class MockUSBInterfaceSetting(Sequence):
    """Mock of usb1.USBInterfaceSetting"""

    def __init__(self, class_subclass: tuple[int, int], endpoints: Sequence[MockUSBEndpoint]):
        self.class_subclass = class_subclass
        self.endpoints = endpoints

    def getClass(self):
        return self.class_subclass[0]

    def getSubClass(self):
        return self.class_subclass[1]

    def getNumEndpoints(self):
        return len(self.endpoints)

    # Make this class indexable
    def __getitem__(self, index):
        return self.endpoints[index]

    def __len__(self):
        return len(self.endpoints)


class MockUSBInterface(Sequence):
    """Mock of usb1.USBInterface"""

    def __init__(self, settings: Sequence[MockUSBInterfaceSetting]):
        self.settings = settings

    # Make this class indexable
    def __getitem__(self, index):
        return self.settings[index]

    def __len__(self):
        return len(self.settings)


class MockUSBConfiguration(Sequence):
    """Mock of usb1.USBConfiguration"""

    def __init__(self, interfaces: Sequence[MockUSBInterface]):
        self.interfaces = interfaces

    def getNumInterfaces(self):
        return len(self)

    # Make this class indexable
    def __getitem__(self, index):
        return self.interfaces[index]

    def __len__(self):
        return len(self.interfaces)


class MockUSBDeviceHandle:
    """Mock of usb1.USBDeviceHandle"""

    def __init__(self, manufacturer: str | None, product: str | None, serno: str | None):
        self.manufacturer = manufacturer
        self.product = product
        self.serno = serno

    def getManufacturer(self):
        return self.manufacturer

    def getProduct(self):
        return self.product

    def getSerialNumber(self):
        return self.serno


class MockUSBDevice(Sequence):
    """Top-level mock of usb1.USBDevice"""

    def __init__(self, vid: int, pid: int, handle: MockUSBDeviceHandle | None, configs: Sequence[MockUSBConfiguration]):
        self.vid = vid
        self.pid = pid
        self.handle = handle
        self.configs = configs

    def getVendorID(self):
        return self.vid

    def getProductID(self):
        return self.pid

    def open(self):
        if self.handle is None:
            raise usb1.USBErrorAccess
        return self.handle

    # Make this class indexable
    def __getitem__(self, index):
        return self.configs[index]

    def __len__(self):
        return len(self.configs)


class MockListPortInfo:
    def __init__(self, serial_number: str, device: str):
        self.serial_number = serial_number
        self.device = device


# This is what a CY7C65211 looks like in I2C mode
CY7C65211_I2C_MODE_DESCRIPTOR = MockUSBDevice(
    vid=0x04B4,
    pid=0xE010,
    handle=MockUSBDeviceHandle(manufacturer="SomeMfg", product="SomeProduct", serno="SomeSerno"),
    configs=[
        MockUSBConfiguration(
            [
                MockUSBInterface(
                    settings=[
                        MockUSBInterfaceSetting(
                            class_subclass=(0xFF, 0x3),
                            endpoints=[
                                MockUSBEndpoint(address=0x01, attributes=2),
                                MockUSBEndpoint(address=0x82, attributes=2),
                                MockUSBEndpoint(address=0x83, attributes=3),
                            ],
                        )
                    ]
                ),
                MockUSBInterface(settings=[MockUSBInterfaceSetting(class_subclass=(0xFF, 0x05), endpoints=[])]),
            ]
        )
    ],
)

# This is what a CY7C65211 looks like in SPI mode
CY7C65211_SPI_MODE_DESCRIPTOR = MockUSBDevice(
    vid=0x04B4,
    pid=0xE010,
    handle=MockUSBDeviceHandle(manufacturer="SomeMfg", product="SomeProduct", serno="SomeSerno"),
    configs=[
        MockUSBConfiguration(
            [
                MockUSBInterface(
                    settings=[
                        MockUSBInterfaceSetting(
                            class_subclass=(0xFF, 0x2),
                            endpoints=[
                                MockUSBEndpoint(address=0x01, attributes=2),
                                MockUSBEndpoint(address=0x82, attributes=2),
                                MockUSBEndpoint(address=0x83, attributes=3),
                            ],
                        )
                    ]
                ),
                MockUSBInterface(settings=[MockUSBInterfaceSetting(class_subclass=(0xFF, 0x05), endpoints=[])]),
            ]
        )
    ],
)

# This is what a CY7C65211 looks like in UART CDC mode
CY7C65211_UART_CDC_MODE_DESCRIPTOR = MockUSBDevice(
    vid=0x04B4,
    pid=0xE011,
    handle=MockUSBDeviceHandle(manufacturer="SomeMfg", product="SomeProduct", serno="SomeSerno"),
    configs=[
        MockUSBConfiguration(
            [
                MockUSBInterface(
                    settings=[
                        MockUSBInterfaceSetting(
                            class_subclass=(0x2, 0x2),
                            endpoints=[
                                MockUSBEndpoint(address=0x83, attributes=3),
                            ],
                        )
                    ]
                ),
                MockUSBInterface(
                    settings=[
                        MockUSBInterfaceSetting(
                            class_subclass=(0x0A, 0x00),
                            endpoints=[
                                MockUSBEndpoint(address=0x01, attributes=2),
                                MockUSBEndpoint(address=0x82, attributes=2),
                            ],
                        )
                    ]
                ),
                MockUSBInterface(settings=[MockUSBInterfaceSetting(class_subclass=(0xFF, 0x05), endpoints=[])]),
            ]
        )
    ],
)


def test_scan_cy7c65211_i2c(mocker):
    """
    Test that we can find a CY7C65211 in I2C mode
    """
    mocker.patch("usb1.USBContext.getDeviceIterator").return_value = [CY7C65211_I2C_MODE_DESCRIPTOR]

    context = cy_serial_bridge.CyScbContext()
    assert context.list_devices() == [
        cy_serial_bridge.DiscoveredDevice(
            usb_device=CY7C65211_I2C_MODE_DESCRIPTOR,  # type: ignore[reportArgumentType]
            usb_configuration=CY7C65211_I2C_MODE_DESCRIPTOR[0],  # type: ignore[reportArgumentType]
            mfg_interface_settings=CY7C65211_I2C_MODE_DESCRIPTOR[0][1][0],  # type: ignore[reportArgumentType]
            scb_interface_settings=CY7C65211_I2C_MODE_DESCRIPTOR[0][0][0],  # type: ignore[reportArgumentType]
            usb_cdc_interface_settings=None,
            cdc_data_interface_settings=None,
            vid=0x04B4,
            pid=0xE010,
            curr_cytype=CyType.I2C,
            open_failed=False,
            manufacturer_str="SomeMfg",
            product_str="SomeProduct",
            serial_number="SomeSerno",
            serial_port_name=None,
        )
    ]


def test_scan_cy7c65211_spi(mocker):
    """
    Test that we can find a CY7C65211 in SPI mode
    """
    mocker.patch("usb1.USBContext.getDeviceIterator").return_value = [CY7C65211_SPI_MODE_DESCRIPTOR]

    context = cy_serial_bridge.CyScbContext()
    assert context.list_devices() == [
        cy_serial_bridge.DiscoveredDevice(
            usb_device=CY7C65211_SPI_MODE_DESCRIPTOR,  # type: ignore[reportArgumentType]
            usb_configuration=CY7C65211_SPI_MODE_DESCRIPTOR[0],  # type: ignore[reportArgumentType]
            mfg_interface_settings=CY7C65211_SPI_MODE_DESCRIPTOR[0][1][0],  # type: ignore[reportArgumentType]
            scb_interface_settings=CY7C65211_SPI_MODE_DESCRIPTOR[0][0][0],  # type: ignore[reportArgumentType]
            usb_cdc_interface_settings=None,
            cdc_data_interface_settings=None,
            vid=0x04B4,
            pid=0xE010,
            curr_cytype=CyType.SPI,
            open_failed=False,
            manufacturer_str="SomeMfg",
            product_str="SomeProduct",
            serial_number="SomeSerno",
            serial_port_name=None,
        )
    ]


def test_scan_cy7c65211_uart(mocker):
    """
    Test that we can find a CY7C65211 in UART mode and locate the associated serial port
    """
    mocker.patch("usb1.USBContext.getDeviceIterator").return_value = [CY7C65211_UART_CDC_MODE_DESCRIPTOR]
    mocker.patch("cy_serial_bridge.cy_scb_context.list_ports.comports").return_value = [
        MockListPortInfo("SomeSerno", "/dev/ttyACM1"),
        MockListPortInfo("SomeOtherSerno", "/dev/ttyACM2"),
    ]

    context = cy_serial_bridge.CyScbContext()
    assert context.list_devices() == [
        cy_serial_bridge.DiscoveredDevice(
            usb_device=CY7C65211_UART_CDC_MODE_DESCRIPTOR,  # type: ignore[reportArgumentType]
            usb_configuration=CY7C65211_UART_CDC_MODE_DESCRIPTOR[0],  # type: ignore[reportArgumentType]
            mfg_interface_settings=CY7C65211_UART_CDC_MODE_DESCRIPTOR[0][2][0],  # type: ignore[reportArgumentType]
            scb_interface_settings=None,
            usb_cdc_interface_settings=CY7C65211_UART_CDC_MODE_DESCRIPTOR[0][0][0],  # type: ignore[reportArgumentType]
            cdc_data_interface_settings=CY7C65211_UART_CDC_MODE_DESCRIPTOR[0][1][0],  # type: ignore[reportArgumentType]
            vid=0x04B4,
            pid=0xE011,
            curr_cytype=CyType.UART_CDC,
            open_failed=False,
            manufacturer_str="SomeMfg",
            product_str="SomeProduct",
            serial_number="SomeSerno",
            serial_port_name="/dev/ttyACM1",
        )
    ]


def test_scan_for_device(mocker):
    """
    Test that we can find a device by serial number
    """
    other_device_i2c_desc = copy.deepcopy(CY7C65211_I2C_MODE_DESCRIPTOR)
    other_device_i2c_desc.handle.serno = "SomeOtherDevice"  # type: ignore[reportOptionalMemberAccess]
    mocker.patch("usb1.USBContext.getDeviceIterator").return_value = [
        CY7C65211_I2C_MODE_DESCRIPTOR,
        other_device_i2c_desc,
    ]

    context = cy_serial_bridge.CyScbContext()
    result = context.scan_for_device(
        vid=0x04B4, pids=0xE010, open_mode=cy_serial_bridge.OpenMode.I2C_CONTROLLER, serial_number="SomeOtherDevice"
    )
    assert result.usb_device == other_device_i2c_desc

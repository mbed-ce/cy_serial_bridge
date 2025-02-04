import pathlib

import cy_serial_bridge

PROJECT_ROOT_DIR = pathlib.Path(__file__).parent.parent


def test_cfg_block_generation():
    """
    Test that we can get and set each property of a configuration block
    """
    config_block = cy_serial_bridge.ConfigurationBlock(
        PROJECT_ROOT_DIR / "example_config_blocks" / "mbed_ce_cy7c65211_spi.bin"
    )

    # Regression test: make sure that all the attributes of a known config block decode as expected
    assert config_block.config_format_version == (1, 0, 3)
    assert config_block.device_type == cy_serial_bridge.CyType.SPI
    assert config_block.vid == 0x04B4
    assert config_block.pid == 0x0004
    assert config_block.mfgr_string == "Cypress Semiconductor"
    assert config_block.product_string == "Mbed CE CY7C65211"
    assert config_block.serial_number == "14224672048496620243684302669570"
    assert not config_block.capsense_on
    assert config_block.default_frequency == 100000

    # Make sure that we can modify the attributes which support being changed
    config_block.device_type = cy_serial_bridge.CyType.UART_CDC
    assert config_block.device_type == cy_serial_bridge.CyType.UART_CDC

    config_block.vid = 0x1234
    config_block.pid = 0x5678
    assert config_block.vid == 0x1234
    assert config_block.pid == 0x5678

    config_block.mfgr_string = "Rockwell Automation"
    config_block.product_string = "Turbo Encabulator"
    config_block.serial_number = "1337"

    assert config_block.mfgr_string == "Rockwell Automation"
    assert config_block.product_string == "Turbo Encabulator"
    assert config_block.serial_number == "1337"

    config_block.default_frequency = 2056000
    assert config_block.default_frequency == 2056000

    # Also verify that strings can be changed to None and this works
    config_block.mfgr_string = None
    config_block.product_string = None
    config_block.serial_number = None

    assert config_block.mfgr_string is None
    assert config_block.product_string is None
    assert config_block.serial_number is None

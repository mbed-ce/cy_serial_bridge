import binascii
import contextlib
import dataclasses
import enum
import logging
import pathlib
import random
import sys
from typing import Annotated, Optional, cast

import click
import rich
import serial
import typer
import usb1
from serial.tools import miniterm

import cy_serial_bridge
from cy_serial_bridge.usb_constants import DEFAULT_PID, DEFAULT_VID
from cy_serial_bridge.utils import log

app = typer.Typer(
    help="Cypress Serial Bridge CLI -- reconfigure CY7C652xx serial bridge chips and use them to communicate over UART/I2C/SPI"
)


# Global options (passed before the subcommand)
# ---------------------------------------------------------------------------------------------


def parse_vid_pid(value: str | int | None) -> int | None:
    if value is None:
        return None

    if type(value) is int:
        val_int = value
    else:
        # value can only be str at this point but mypy can't figure that out
        value = cast(str, value)

        try:
            val_int = int(value, 0)
        except ValueError:
            message = "VIDs and PIDs must be integers"
            raise typer.BadParameter(message) from None

    if val_int < 0 or val_int > 0xFFFF:
        message = "VIDs and PIDs must be between 0 and 0xFFFF"
        raise typer.BadParameter(message)

    return val_int


VIDOption = typer.Option(
    "-V",
    "--vid",
    metavar="VID",
    parser=parse_vid_pid,
    help=f"VID of device to connect [default: 0x{DEFAULT_VID:04x}]",
    show_default=False,
)
PIDOption = typer.Option(
    "-P",
    "--pid",
    metavar="PID",
    parser=parse_vid_pid,
    help=f"PID of device to connect [default: 0x{DEFAULT_PID:04x}]",
    show_default=False,
)
SerialNumOption = typer.Option("-S", "--serno", help="Serial number string of of device to connect.")
SCBOption = typer.Option("-s", "--scb", min=0, max=1, help="SCB channel to use.  For dual channel devices only.")
VerboseOption = typer.Option("-v", "--verbose", help="Enable verbose logging")


@dataclasses.dataclass
class GlobalOptions:
    vid: int
    pid: int
    serial_number: str | None
    scb: int


# Note: we know that this value will always be set via the global callback before any of the CLI commands run.
# However, mypy doesn't and will generate errors that it might be None.  So, we annotate it as always having
# a value even though it's None initially.
global_opt: GlobalOptions = cast(GlobalOptions, None)


# Global context instance.
# Fine to use a global one since the CLI can only talk to one device at a time.
context = cy_serial_bridge.CyScbContext()


@app.callback()
def handle_global_options(
    vid: Annotated[int, VIDOption] = DEFAULT_VID,
    pid: Annotated[int, PIDOption] = DEFAULT_PID,
    serial_number: Annotated[Optional[str], SerialNumOption] = None,
    scb: Annotated[int, SCBOption] = 0,
    verbose: Annotated[bool, VerboseOption] = False,
) -> None:
    # Set global log level based on 'verbose'
    log_level = logging.INFO if verbose else logging.WARN
    logging.basicConfig(level=log_level)
    log.setLevel(log_level)

    # Also set libusb log level based on 'verbose'
    context.usb_context.setDebug(usb1.LOG_LEVEL_INFO if verbose else usb1.LOG_LEVEL_ERROR)

    # Save other options
    global global_opt  # noqa: PLW0603
    global_opt = GlobalOptions(vid, pid, serial_number, scb)


# Save command
# ---------------------------------------------------------------------------------------------
OutputConfigurationArgument = typer.Argument(
    exists=False, dir_okay=False, file_okay=True, writable=True, help="Path to save the configuration block to"
)


@app.command(help="Save configuration block from connected device to bin file")
def save(file: Annotated[pathlib.Path, OutputConfigurationArgument]) -> None:
    with cast(
        cy_serial_bridge.driver.CyMfgrIface,
        context.open_device(
            global_opt.vid, global_opt.pid, cy_serial_bridge.OpenMode.MFGR_INTERFACE, global_opt.serial_number
        ),
    ) as dev:
        dev.connect()
        buf = dev.read_config()
        dev.disconnect()

        # As a sanity check, parse the bytes to make sure the checksum is valid
        config_block = cy_serial_bridge.configuration_block.ConfigurationBlock(block_bytes=buf)
        log.info("Read the following configuration from the device: %s", str(config_block))

        # Save to file
        pathlib.Path(file).write_bytes(bytes(buf))


# Load command
# ---------------------------------------------------------------------------------------------
InputConfigurationArgument = typer.Argument(
    exists=True, dir_okay=False, file_okay=True, readable=True, help="Path to load the configuration block from"
)


@app.command(help="Load configuration block to connected device from bin file")
def load(file: Annotated[pathlib.Path, InputConfigurationArgument]) -> None:
    with cast(
        cy_serial_bridge.driver.CyMfgrIface,
        context.open_device(
            global_opt.vid, global_opt.pid, cy_serial_bridge.OpenMode.MFGR_INTERFACE, global_opt.serial_number
        ),
    ) as dev:
        # Load bytes and check checksum
        config_block = cy_serial_bridge.configuration_block.ConfigurationBlock(file)

        log.info("Writing the following configuration to the device: %s", str(config_block))

        dev.connect()

        log.info("Writing configuration...")
        dev.write_config(config_block)
        dev.disconnect()
        dev.reset_device()

        log.info("Done!")


# Decode command
# ---------------------------------------------------------------------------------------------
@app.command(help="Decode and display basic information from configuration block bin file")
def decode(file: Annotated[pathlib.Path, InputConfigurationArgument]) -> None:
    # Just decode the configuration block, then exit.
    cfg_block = cy_serial_bridge.configuration_block.ConfigurationBlock(file)
    print(str(cfg_block))


# Reconfigure command
# ---------------------------------------------------------------------------------------------

RandomizeSernoOption = typer.Option("--randomize-serno", help="Set the serial number of the device to a random value.")
SetSernoOption = typer.Option("--set-serno", help="Set the serial number of the device.")
SetVIDOption = typer.Option(
    "--set-vid",
    help="Set the USB Vendor ID to a given value.  Needs a 0x prefix for hex values!",
    metavar="VID",
    parser=parse_vid_pid,
)
SetPIDOption = typer.Option(
    "--set-pid",
    help="Set the USB Product ID to a given value.  Needs a 0x prefix for hex values!",
    metavar="PID",
    parser=parse_vid_pid,
)


@app.command(help="Change configuration of the connected device via the CLI")
def reconfigure(
    randomize_serno: Annotated[bool, RandomizeSernoOption] = False,
    set_serno: Annotated[Optional[str], SetSernoOption] = None,
    set_vid: Annotated[Optional[int], SetVIDOption] = None,
    set_pid: Annotated[Optional[int], SetPIDOption] = None,
) -> None:
    if randomize_serno and set_serno is not None:
        message = "You cannot pass both --randomize-serno and --set-serno at the same time!"
        raise typer.BadParameter(message)

    with cast(
        cy_serial_bridge.driver.CyMfgrIface,
        context.open_device(
            global_opt.vid, global_opt.pid, cy_serial_bridge.OpenMode.MFGR_INTERFACE, global_opt.serial_number
        ),
    ) as dev:
        dev.connect()

        try:
            buffer = dev.read_config()

            config_block = cy_serial_bridge.configuration_block.ConfigurationBlock(block_bytes=buffer)
            log.info("Read the following configuration from the device: %s", str(config_block))

            if randomize_serno:
                # Generate a random integer with 32 digits
                serial_number_max = 10**32 - 1
                random_serial_number = random.randint(0, serial_number_max)
                random_serial_number_str = f"{random_serial_number:032}"
                print(f"Assigned random serial number: {random_serial_number_str}")

                config_block.serial_number = random_serial_number_str
            elif set_serno is not None:
                config_block.serial_number = set_serno

            if set_vid is not None:
                config_block.vid = set_vid

            if set_pid is not None:
                config_block.pid = set_pid

            log.info("Writing the following configuration to the device: %s", str(config_block))

            log.info("Writing configuration...")
            dev.write_config(config_block)
            dev.disconnect()

            log.info("Done!  Resetting device now...")

            # Reset the device so that the new configuration loads
            dev.reset_device()

        except:
            dev.disconnect()
            raise


# Change type command
# ---------------------------------------------------------------------------------------------

# Incredibly annoyingly, Typer has no way to use an Enum to map between names and integer values.
# https://github.com/tiangolo/typer/issues/151
# So we have to use a workaround by dropping down to the underlying Click
# https://github.com/tiangolo/typer/issues/182#issuecomment-1708245110

TypeArgument = typer.Argument(
    help="Communication type to change the bridge into",
    click_type=click.Choice(cy_serial_bridge.CyType._member_names_, case_sensitive=False),  # noqa: SLF001
    show_default=False,
)


@app.command(
    help="Set the type of device that the serial bridge acts as (I2C/SPI/UART).  For configurable bridge devices (65211/65215) only."
)
def change_type(type: Annotated[str, TypeArgument]) -> None:  # noqa: A002
    cy_type = cy_serial_bridge.CyType[type]

    # MFG is not a type that can actually be set because the vendor interface is always active along with
    # whatever other interface is needed
    if cy_type == cy_serial_bridge.CyType.MFG:
        message = "Invalid CyType value, cannot set MFG as the device type"
        raise typer.BadParameter(message)

    if cy_type == cy_serial_bridge.CyType.UART_VENDOR:
        print(
            "UART_VENDOR devices cannot be accessed by the cy_serial_bridge library.  If you want to use the device"
            " in UART mode with this library, use UART_CDC instead."
        )
        typer.confirm("Are you sure you want to continue with this mode?", abort=True)
    elif cy_type == cy_serial_bridge.CyType.UART_PHDC:
        print(
            "UART_PHDC devices cannot be accessed by the cy_serial_bridge library and additionally UART_PHDC mode "
            "has not been tested by the cy_serial_bridge authors.  If you want to use the device"
            " in UART mode with this library, use UART_CDC instead."
        )
        typer.confirm("Are you sure you want to continue with this mode?", abort=True)
    elif cy_type == cy_serial_bridge.CyType.JTAG:
        print(
            "CyType.JTAG is only usable for SCB1 on the CY7C65215, and JTAG is currently not supported "
            "by this driver."
        )
        typer.confirm("Are you sure you want to continue with this mode?", abort=True)

    dev: cy_serial_bridge.driver.CyMfgrIface
    with cast(
        cy_serial_bridge.driver.CyMfgrIface,
        context.open_device(
            global_opt.vid, global_opt.pid, cy_serial_bridge.OpenMode.MFGR_INTERFACE, global_opt.serial_number
        ),
    ) as dev:
        dev.change_type(cy_type)

        # Reset the device so that the new configuration loads
        dev.reset_device()


# Scan command
# ---------------------------------------------------------------------------------------------
ScanAllOption = typer.Option(
    "--all", "-a", help="Scan all USB devices on the system instead of just ones with the specified vid and pid"
)


@app.command(help="Scan for USB devices which look like CY7C652xx serial bridges")
def scan(scan_all: Annotated[bool, ScanAllOption] = False) -> None:
    """
    Scan for candidate USB devices on the system
    """
    scan_filter = None if scan_all else {(global_opt.vid, global_opt.pid)}
    devices = context.list_devices(scan_filter)

    if len(devices) == 0:
        if scan_all:
            print("No devices found on the system that look like a CY7C652xx!")
        else:
            print(f"No devices found on the system with VID:PID {global_opt.vid:04x}:{global_opt.pid:04x}.")
            print("Maybe try again with --all to search all VIDs and PIDs?")
    else:
        print("Detected Devices:")
        for device in devices:
            rich.print(
                f"- [bold yellow]{device.vid:04x}[/bold yellow]:[bold yellow]{device.pid:04x}[/bold yellow] ([bold]Type:[/bold] {device.curr_cytype.name})",
                end="",
            )

            if device.open_failed:
                if sys.platform == "win32":
                    rich.print(
                        "[red]<Open failed, cannot get name, com port, or serno.  Attach WinUSB driver with Zadig!>[/red]"
                    )
                else:
                    rich.print(
                        "[red]<Open failed, cannot get name, tty, or serial number.  Check udev rules and permissions.>[/red]"
                    )
            else:
                rich.print(
                    f" ([bold]SerNo:[/bold] {device.serial_number}) ([bold]Name:[/bold] {device.manufacturer_str} {device.product_str})",
                    end="",
                )
                if device.serial_port_name is not None:
                    rich.print(f" ([bold]Serial Port:[/bold] '{device.serial_port_name}')", end="")
                rich.print("")


# I2C write & read commands
# ---------------------------------------------------------------------------------------------


def parse_i2c_addr(value: str) -> int:
    try:
        val_int = int(value, 0)
    except ValueError:
        message = "I2C address must be an integer"
        raise typer.BadParameter(message) from None

    if val_int < 0 or val_int > 0x7F:
        message = "I2C address must be between 0 and 0x7F"
        raise typer.BadParameter(message)

    return val_int


PeriphAddrArgument = typer.Argument(
    help="7-bit address of the I2C peripheral.  Don't forget a 0x prefix!", parser=parse_i2c_addr, show_default=False
)
I2CWriteDataArgument = typer.Argument(
    help="Data to write to the peripheral.  Must be a string in hex format, e.g. '0abc'."
)
I2CFreqOption = typer.Option(
    "--frequency",
    "-f",
    min=cy_serial_bridge.CyI2c.MIN_FREQUENCY,
    max=cy_serial_bridge.CyI2c.MAX_FREQUENCY,
    help="I2C frequency to use, in Hz.",
)


@app.command(help="Perform a write to an I2C peripheral")
def i2c_write(
    periph_addr: Annotated[int, PeriphAddrArgument],
    data_to_write: Annotated[str, I2CWriteDataArgument] = "",
    freq: Annotated[int, I2CFreqOption] = cy_serial_bridge.CyI2c.MAX_FREQUENCY.value,
) -> None:
    with cast(
        cy_serial_bridge.driver.CyI2CControllerBridge,
        context.open_device(
            global_opt.vid, global_opt.pid, cy_serial_bridge.OpenMode.I2C_CONTROLLER, global_opt.serial_number
        ),
    ) as bridge:
        bridge.set_i2c_configuration(cy_serial_bridge.driver.CyI2CConfig(frequency=freq))

        # Convert data_to_write into bytes
        data_bytes = binascii.a2b_hex(data_to_write)
        print(f"Writing {data_bytes!r} to address 0x{periph_addr:02x}")

        # Do the write
        try:
            bridge.i2c_write(periph_addr=periph_addr, data=data_bytes)
        except cy_serial_bridge.I2CNACKError:
            print("NACK received")
            sys.exit(1)

    print("Done.")


BytesToReadOption = typer.Argument(min=1, help="Number of bytes to read from the peripheral")

# spi-transaction command
# ---------------------------------------------------------------------------------------------


SPISendDataArgument = typer.Argument(
    help="Data to send on the MOSI line during the SPI transaction.  Must be a string in hex format, e.g. '0abc'."
)


SPIFreqOption = typer.Option(
    "--frequency",
    "-f",
    min=cy_serial_bridge.CySpi.MIN_FREQUENCY,
    max=cy_serial_bridge.CySpi.MAX_MASTER_FREQUENCY,
    help="SPI frequency to use, in Hz.",
)


SPIModeArgument = typer.Option(
    "--mode",
    "-m",
    help="SPI mode to use for the transfer",
    click_type=click.Choice(cy_serial_bridge.CySPIMode._member_names_, case_sensitive=False),  # noqa: SLF001
    show_default=False,
)


@app.command(help="Perform a transaction over the SPI bus")
def spi_transaction(
    bytes_to_send: Annotated[str, SPISendDataArgument],
    freq: Annotated[int, SPIFreqOption] = cy_serial_bridge.CySpi.MAX_MASTER_FREQUENCY.value,
    mode: Annotated[str, SPIModeArgument] = cy_serial_bridge.CySPIMode.MOTOROLA_MODE_0.name,
) -> None:
    with cast(
        cy_serial_bridge.driver.CySPIControllerBridge,
        context.open_device(
            global_opt.vid, global_opt.pid, cy_serial_bridge.OpenMode.SPI_CONTROLLER, global_opt.serial_number
        ),
    ) as bridge:
        mode_enum = cy_serial_bridge.CySPIMode[mode]

        bridge.set_spi_configuration(cy_serial_bridge.driver.CySPIConfig(frequency=freq, mode=mode_enum))

        # Convert data_to_write into bytes
        data_to_send = binascii.a2b_hex(bytes_to_send)
        print(f"Writing {data_to_send!r} to peripheral")

        # Do the transfer
        response = bridge.spi_transfer(data_to_send)

        # Display result as an ASCII string
        response_text = binascii.b2a_hex(response).decode("ASCII")
        print(f"Read from peripheral: {response_text}")


# serial-term command
# ---------------------------------------------------------------------------------------------


class EndOfLineType(str, enum.Enum):
    """
    Enum of line ending options supported by Miniterm
    """

    LF = "lf"
    CRLF = "crlf"
    CR = "cr"


# We try to ape some of miniterm's more common command line options, though it's not a complete list.
BaudrateOption = typer.Option(
    "-b", "--baudrate", help="Serial baudrate.", max=cy_serial_bridge.CyUart.MAX_BAUDRATE.value
)
EOLOption = typer.Option("--eol", help="End-of-line type to use", case_sensitive=False)


@app.command(help="Access a serial terminal for a serial bridge in UART CDC mode")
def serial_term(
    baudrate: Annotated[int, BaudrateOption] = 115200, eol: Annotated[EndOfLineType, EOLOption] = EndOfLineType.CRLF
) -> None:
    # Briefly open the serial bridge in UART CDC mode just to get it converted to the right mode
    with cast(
        serial.Serial,
        context.open_device(
            global_opt.vid, global_opt.pid, cy_serial_bridge.OpenMode.UART_CDC, global_opt.serial_number
        ),
    ) as serial_instance:
        serial_instance.baudrate = baudrate

        # Below is based on the logic in serial.tools.miniterm.main().
        # For now I have converted most of the arguments to hardcoded values
        # but they could be re-added to the argument parsing later...
        term = miniterm.Miniterm(serial_instance, echo=False, eol=eol.value, filters=[])
        term.exit_character = chr(0x1D)  # GS/CTRL+]
        term.menu_character = chr(0x14)  # Menu: CTRL+T
        term.set_rx_encoding("UTF-8")
        term.set_tx_encoding("UTF-8")

        sys.stderr.write(
            "--- Miniterm on {p.name}  {p.baudrate},{p.bytesize},{p.parity},{p.stopbits} ---\n".format(p=term.serial)
        )
        sys.stderr.write(
            "--- Quit: {} | Menu: {} | Help: {} followed by {} ---\n".format(
                miniterm.key_description(term.exit_character),
                miniterm.key_description(term.menu_character),
                miniterm.key_description(term.menu_character),
                miniterm.key_description("\x08"),
            )
        )

        term.start()
        with contextlib.suppress(KeyboardInterrupt):
            term.join(True)
        sys.stderr.write("\n--- exit ---\n")
        term.join()
        term.close()


#
# GPIO command
# ---------------------------------------------------------------------------------------------

GpioArgument = typer.Argument(
    help="GPIOs to get/set. A string in the format 'io1 io4 io2=0 io3=1'."
)

class GpioOutputStyle(str, enum.Enum):
    """
    Enum of output styles for the GPIO command
    """
    ASCII = "ascii" 
    PLAIN = "plain"
    JSON = "json"

GpioOutputStyleOption = typer.Option("--output-style", help="Output style to use", case_sensitive=False)


@app.command(help="Set/Get GPIO pins on the CY7C652xx")
def gpio(
    gpio_opt: Annotated[str, GpioArgument] = "", outstyle: Annotated[GpioOutputStyle, GpioOutputStyleOption] = GpioOutputStyle.ASCII
) -> None:
    with cast(
        cy_serial_bridge.driver.CyMfgrIface,
        context.open_device(
            global_opt.vid, global_opt.pid, cy_serial_bridge.OpenMode.MFGR_INTERFACE, global_opt.serial_number
        ),
    ) as dev:
        if outstyle == GpioOutputStyle.JSON:
            print("[")
        dev.connect()
        gpio_opts = gpio_opt.split()
        first = True
        for opt in gpio_opts:
            if "=" in opt:
                pin, value = opt.split("=")
                pin = int(pin.strip("io"))
                value = int(value)
                dev.set_gpio(pin, value)
            else:
                pin = int(opt.strip("io"))
                value = dev.get_gpio(pin)
                if outstyle == GpioOutputStyle.ASCII:
                    if not first:
                        print(" ", end="")
                    print(f"io{pin}={value}", end="")
                elif outstyle == GpioOutputStyle.PLAIN:
                    print(f"{value}")
                elif outstyle == GpioOutputStyle.JSON:
                    if not first:
                        print(",")
                    print(f'{{"pin": {pin}, "value": {value}}}', end="")        
                first = False
        dev.disconnect()
        if outstyle == GpioOutputStyle.JSON:
            print("")
            print("]")
        elif outstyle == GpioOutputStyle.ASCII:
            print("")


# Main entry point
# ---------------------------------------------------------------------------------------------

def main() -> None:
    app()


if __name__ == "__main__":
    main()

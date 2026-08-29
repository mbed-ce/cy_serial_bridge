# cy_serial_bridge
![CY7C65211 Picture](https://ce8dc832c.cloudimg.io/v7/_cdn_/42/19/C0/00/0/823588_1.jpg?width=640&height=480&wat=1&wat_url=_tme-wrk_%2Ftme_new.png&wat_scale=100p&ci_sign=f9de9ea97a6a5472cf6133a7f2b36109c2910153)

This package is a pure Python driver which controls a CY7C652xx USB to SPI/I2C/UART bridge IC.  It is based on @tai's reverse engineering work [here](https://github.com/tai/cyusb-hack).

## Background
The [CY7C652xx family](https://www.infineon.com/cms/en/product/universal-serial-bus/usb-2.0-peripheral-controllers/ez-usb-serial-bridge-controller/) of chips from Cypress (now Infineon) are "serial bridge controllers" which convert USB into embedded busses such as UART (serial port), I2C, and SPI.  They can be thought of as competitors to the more popular FT2232H and FT232H chips which are often found at the heart of commercial USB to I2C/SPI adapters.  However, in my experience so far, they are easier to work with and simpler to design your circuit board around.  In particular, they do not need an external EEPROM, and they are much better documented on the electrical side.  Why the industry still prefers the FTDI chips is... something of a mystery to me at this point.

One disadvantage of the CY7C652xx chips is that their software options are somewhat less fleshed out than the corresponding FTDI libraries.  Cypress provides two driver options: a Windows-only library which works through a proprietary Cypress driver (cyusb3.sys), and a cross-platform, open-source libusb1 driver.  The libusb driver can be downloaded [here](https://www.infineon.com/cms/en/product/universal-serial-bus/usb-2.0-peripheral-controllers/ez-usb-serial-bridge-controller/cy7c65211-24ltxi/#!designsupport), and is the basis for much of this driver (the C code is able to be adapted to Python via the `libusb1` package).

However, the available drivers provide absolutely no provision for reprogramming the "configuration block", the binary structure stored in the chip's flash memory which defines its USB attributes.  This includes manufacturer-set stuff such as the VID, PID, and serial number, but also the flag that tells it whether it should be a UART, I2C, or SPI bridge, and the default settings which are used for each bus type.  The configuration block can only be programmed using the closed-source Cypress USB Serial Configuration Utility (USCU) -- and to add to the pain, this is only available as a Windows GUI application!

This driver is being worked on with the goal of, in addition to providing a translation of the libusb C driver into Python, also reverse-engineering the format of the config block and providing utilities to modify and rewrite it.  Basic config rewriting functionality is working, and I have been able to dynamically edit the parameters of a CY7C65211 and change it between I2C, SPI, and UART mode!  Additionally, since the code quality of the libusb1 driver is... not great (it uses multiple threads internally rather than the libusb asynchronous API), I've been trying to clean up and simplify the way that data is transferred to and from the device.

### You *should* use this driver if you want to
- Have a nice Python API for interacting with CY7C652xx bridge chips in UART, I2C, and SPI mode
- Switch a CY7C652xx in between UART, I2C, and SPI mode at runtime (the official driver does NOT support this at all)
- Use an OS-agnostic CLI and/or Python API to provision CY7C652xx chips with the correct VID, PID, description, and serial number values

### You should *not* use this driver if you want to
- Ship an easy solution that works with no additional user setup on Windows machines (see Windows section below)
- Just use the serial bridge chip as a USB-UART adapter only (in this case, just use USCU to configure it once and then use a regular serial port library to operate it)
- Rapidly switch between I2C/SPI/UART modes (it takes up to a few seconds to reprogram the config block and re-enumerate the device)

## Warnings

This driver is not very well tested yet, and I would advise against relying on it for anything important without additional testing.  I am testing it only with a CY7C65211 dev kit, and have not tried it with the single-purpose devices or with a dual-channel device like the CY7C65215.  It should work for those devices, but I cannot guarantee anything.

Additionally, I assume that it would be possible to brick your CY7C652xx by loading an incorrect configuration block onto it.  Rather than downloading any of the configuration bin files in this repo, I would highly recommend doing a load operation first to download the configuration block from your specific model of chip, and then modifying it and writing it back (this is already how the reconfigure functionality works).  Writing back configurations obtained from anywhere else could be a dangerous operation!  I take no responsibility for any chips bricked through usage of this tool.

## Functionality
### Currently Supported
- Basic reprogramming (changing type, VID/PID, and serial number)
- I2C controller/master mode operation
- SPI controller/master mode operation
- User flash reading & writing
- UART CDC operation (this library changes the type of the device and finds the right tty / COM port, then pyserial handles the actual UART communication)
### Not supported yet
- vendor mode UART operation
- I2C peripheral/slave mode operation
- SPI peripheral/slave mode operation
- CapSense
- GPIO
- JTAG (only supported on the larger dual channel devices)
- Configuring dual channel CY7C652xx devices (e.g. CY7C65215)

## Using the Command-Line Interface

This driver installs a command-line interface script, `cy_serial_cli`.  It supports a number of functions:
```
 Usage: cy_serial_cli [OPTIONS] COMMAND [ARGS]...

 Cypress Serial Bridge CLI -- reconfigure CY7C652xx serial bridge chips and use them to communicate over UART/I2C/SPI

╭─ Options ───────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────╮
│ --vid                 -V      VID                              VID of device to connect [default: 0x04b4]                                                                                       │
│ --pid                 -P      PID                              PID of device to connect [default: 0xe010]                                                                                       │
│ --serno               -S      TEXT                             Serial number string of of device to connect. [default: None]                                                                    │
│ --scb                 -s      INTEGER RANGE [0<=x<=1]          SCB channel to use.  For dual channel devices only. [default: 0]                                                                 │
│ --verbose             -v                                       Enable verbose logging                                                                                                           │
│ --install-completion          [bash|zsh|fish|powershell|pwsh]  Install completion for the specified shell. [default: None]                                                                      │
│ --show-completion             [bash|zsh|fish|powershell|pwsh]  Show completion for the specified shell, to copy it or customize the installation. [default: None]                               │
│ --help                                                         Show this message and exit.                                                                                                      │
╰─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────╯
╭─ Commands ──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────╮
│ change-type            Set the type of device that the serial bridge acts as (I2C/SPI/UART).  For configurable bridge devices (65211/65215) only.                                               │
│ decode                 Decode and display basic information from configuration block bin file                                                                                                   │
│ i2c-write              Perform a write to an I2C peripheral                                                                                                                                     │
│ load                   Load configuration block to connected device from bin file                                                                                                               │
│ reconfigure            Change configuration of the connected device via the CLI                                                                                                                 │
│ save                   Save configuration block from connected device to bin file                                                                                                               │
│ scan                   Scan for USB devices which look like CY7C652xx serial bridges                                                                                                            │
│ serial-term            Access a serial terminal for a serial bridge in UART CDC mode                                                                                                            │
│ spi-transaction        Perform a transaction over the SPI bus                                                                                                                                   │
╰─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────╯
```

### Changing Settings
The `reconfigure` subcommand can be used to edit settings of a connected device, then write those settings back.  The options you pass tell it what to reconfigure:
```
 Usage: cy_serial_cli reconfigure [OPTIONS]

 Change configuration of the connected device via the CLI

┌─ Options ──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┐
│ --randomize-serno                 Set the serial number of the device to a random value.                                                                           │
│ --set-serno              TEXT     Set the serial number of the device. [default: None]                                                                             │
│ --set-vid                INTEGER  Set the USB Vendor ID to a given value.  Needs a 0x prefix for hex values! [default: None]                                       │
│ --set-pid                INTEGER  Set the USB Product ID to a given value.  Needs a 0x prefix for hex values! [default: None]                                      │
│ --help                            Show this message and exit.                                                                                                      │
└────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┘
```

Additionally, the `type` subcommand can be used to change the device's type.  However, this is mostly for debugging and for using the serial bridge with other programs, because the type is changed automatically when you open the device in SPI, I2C, or UART mode.

### Scanning for Devices
The `scan` command can be used to find CY7C652xx devices attached to your system.  Since these devices have configurable VIDs and PIDs, a heuristic search is used based on each connected device's USB descriptor.  By default, only devices with the default VID and PIDs (see "Setting Up New Devices" below) are considered, but if you use `scan --all`, that will search all USB devices on your system.

For each detected device, the serial number, type, and (if in UART CDC mode) the corresponding serial port is output.

```
> cy_serial_cli scan --all
Detected Devices:
- 04b4:e010 (Type: SPI) (SerNo: Testing1) (Name: Cypress Semiconductor Mbed CE CY7C65211)
```


### Doing I2C Transactions

For simple testing, cy_serial_cli implements commands to do I2C reads and writes.  These should be very convenient for board bring-up type tasks, though for more complex tasks it is highly recommended to use the Python API instead (see below).

For example, we can write some data to the EEPROM on the eval kit:
```
$ cy_serial_cli i2c-write 0x51 "001001020304"
Connected to I2C interface of CY7C652xx device, firmware version 1.0.3 build 78
Writing b'\x00\x10\x01\x02\x03\x04' to address 0x51
Done.
```

And then read it back (we have to reset the write pointer back to address 0x10 first):
```
$ cy_serial_cli i2c-write 0x51 "0010"        
Connected to I2C interface of CY7C652xx device, firmware version 1.0.3 build 78
Writing b'\x00\x10' to address 0x51
Done.
$ cy_serial_cli i2c-read 0x51 4      
Connected to I2C interface of CY7C652xx device, firmware version 1.0.3 build 78
Read from address 0x51: 01020304
```

As you can see, we were able to read the same byte pattern (01 02 03 04) that we had just written!

### Doing SPI Transactions
This package also implements a rudimentary CLI to do SPI transactions.  As before, this is only recommended for initial testing of hardware as it will be much easier to use the Python API for anything complex.  Also note that SPI is inherently a full-duplex bus so it always sends the same amount of bytes that it receives.

For example, we can read the status register in the EEPROM included on the eval kit by sending it the RDSR command (0x05):

```
$ cy_serial_cli spi-transaction --frequency 2000000 --mode MOTOROLA_MODE_0 050000
Connected to SPI interface of CY7C652xx device, firmware version 1.0.3 build 78
Writing b'\x05\x00\x00' to peripheral
Read from peripheral: 000000
```
(the --frequency and --mode arguments can actually be omitted in this case but I'm adding them for clarity)

Then we can set the write enable bit using WREN (0x6) and verify that it sets in the status register:
```
$ cy_serial_cli spi-transaction --frequency 2000000 --mode MOTOROLA_MODE_0 06    
Connected to SPI interface of CY7C652xx device, firmware version 1.0.3 build 78
Writing b'\x06' to peripheral
Read from peripheral: 00
$ cy_serial_cli spi-transaction --frequency 2000000 --mode MOTOROLA_MODE_0 050000
Connected to SPI interface of CY7C652xx device, firmware version 1.0.3 build 78
Writing b'\x05\x00\x00' to peripheral
Read from peripheral: 000202
```

### Accessing the Serial Port

To use the bridge in UART CDC mode, cy_serial_bridge provides the `serial-term` command.  This is a wrapper around the miniterm terminal from the `serial` package.  Running this command will switch the selected device to UART_CDC mode and then open an interactive terminal for it: 
```shell
$ python3 -m cy_serial_bridge.cli serial-term                                                                                       
--- Miniterm on COM7  115200,8,N,1 ---
--- Quit: Ctrl+] | Menu: Ctrl+T | Help: Ctrl+T followed by Ctrl+H ---
```

## Using the Python API

cy_serial_bridge provides a rich Python API that can be used to communicate with the serial bridge in each mode. 

To use the API, you must first import the package and create a context.  The context object wraps the libusb instance and must be used to scan for and open devices.
```python
import cy_serial_bridge

context = cy_serial_bridge.CyScbContext()
```

### I2C controller mode

First, you must open the device and set the configuration:
```python
with context.open_device(cy_serial_bridge.DEFAULT_VID, 
                         cy_serial_bridge.DEFAULT_PID, 
                         cy_serial_bridge.OpenMode.I2C_CONTROLLER) as bridge:
    bridge.set_i2c_configuration(cy_serial_bridge.driver.CyI2CConfig(frequency=400000))
```

(note that per the datasheet, the CY7C65211 supports I2C frequencies between 1kHz and 400kHz)

From here, you can use the i2c_write() and i2c_read() functions to send and receive data via the bridge.  For example, to read data from the EEPROM on the CY7C65211 eval board, one might do something like:
```python
addr_to_read = 0x0010
num_bytes_to_read = 10
bridge.i2c_write(0x51, bytes([(addr_to_read >> 8) & 0xFF, addr_to_read & 0xFF]), relinquish_bus=False)
read_data = bridge.i2c_read(0x51, num_bytes_to_read)
```

(note: if running this example on the eval board, be careful that the jumpers are set in the I2C position.  Otherwise, the USB operation seems to hang forever -- perhaps the serial bridge is waiting for the I2C lines to go high before initiating the transaction?)

If a NACK occurs during the operation, an exception of type cy_serial_bridge.I2CNACKError will be thrown.

Note: There appears to be a bug with the chip where I2C writes (and reads?) of only 1 byte always indicate success even if the hardware NACKed.  Also, 0 length reads and writes (used sometimes for I2C scanning) are not supported by the hardware.

### SPI controller mode

Similarly, it's possible to open the serial bridge in SPI mode:

```python
with context.open_device(cy_serial_bridge.DEFAULT_VID, 
                         cy_serial_bridge.DEFAULT_PID, 
                         cy_serial_bridge.OpenMode.SPI_CONTROLLER) as bridge:
    bridge.set_spi_configuration(cy_serial_bridge.driver.CySPIConfig(frequency=1000000))
```

Then, you can do an SPI transaction using the serial bridge object:

```python
tx_bytes = bytes([0x01, 0x02, 0x03, 0x04])
response_bytes = bridge.spi_transfer(tx_bytes)
```

This will send the data from `tx_bytes` out the MOSI line and save the data from the MISO line into `response_bytes`.

### UART CDC mode

In UART CDC mode, the serial bridge acts as a standard USB-serial converter.  Luckily, Python already has the pyserial library to interact with such devices.  So, when you open a device in UART_CDC mode, you get back a `serial.Serial` instance that you can use as you would any serial port.

```python
with context.open_device(cy_serial_bridge.DEFAULT_VID, 
                         cy_serial_bridge.DEFAULT_PID, 
                         cy_serial_bridge.OpenMode.UART_CDC) as serial_port:
    serial_port.baudrate = 115200
    serial_port.timeout = 0.1
    
    serial_port.write(b"Hello world!")
    response = serial_port.read(10)
```

See the [pyserial docs](https://pythonhosted.org/pyserial/pyserial_api.html#serial.Serial) for more information about how to use the Serial class.

Note that CY7C652xx chips can hit non-standard UART baudrates (e.g. 100000 instead of 115200), but there are some restrictions on accuracy. See Cypress [KBA92442](https://community.infineon.com/t5/Knowledge-Base-Articles/Non-Standard-Baud-Rates-in-USB-Serial-Bridge-Controllers/ta-p/249181) for details.

## OS-Specific Info

### Windows
On Windows, cy_serial_bridge (and other libusb based programs) cannot connect to USB devices unless they have the "WinUSB" driver attached to them.

To set this up, you will need to use [Zadig](https://zadig.akeo.ie/).  Simply run this program, click "Options > List All Devices" in the menu, find whichever USB devices represent the CY7C652xx (you might have to look at the VID & PID values), and install the WinUSB driver for them.  Note that there will be at least two USB devices in the list for each bridge chip -- one for the communication interface and one for the configuration interface.  You need to install the driver for *both* for cy_serial_bridge to work.

This process might have to be redone the first time that the bridge is used in each mode -- for example, if I connect a CY7C65211 to a fresh machine in SPI mode and install the driver using Zadig, then change the chip to operate in I2C mode in code, I may have to use Zadig again before Python code can open it in I2C mode.  Zadig installation will also have to be redone if the VID or PID is changed, though it only has to be done once per machine for a given VID-PID-operation mode combination.

Note: If the current driver for an entry in Zadig shows as "usbser", do NOT install the WinUSB driver over it.  The usbser driver is the correct driver for a device in UART CDC mode and will not attach to it otherwise.

I believe that it would be possible to script this a bit more gracefully, by writing a script to change the serial bridge into each mode and then invoke Zadig for each interface.  This should be looked into more.

Also note that [NirSoft USBLogView](https://www.nirsoft.net/utils/usb_log_view.html) is extremely useful for answering the question of "what are the VID & PID of the USB device I just plugged in".

### Linux
To grant access to the serial bridge USB device without root, you will need to install a udev rules file.  We've provided one for you [here](https://github.com/mbed-ce/cy_serial_bridge/tree/master/rules).  First, copy it to the `/etc/udev/rules.d` folder.  Then, run:
```shell
$ sudo udevadm control --reload-rules
$ sudo udevadm trigger
```

Finally, make sure your user is in the `plugdev` group:
```shell
$ sudo usermod -a -G plugdev <your username>
```
(you may have to log out and back in for this to take effect)

This should allow you to access the serial bridge chip without any special privileges.  

Note, however, that the rules file is written for the driver default VID and PID values (see "Setting Up New Devices").  If your device is using different VID & PID values, you will need to update the rules file or temporarily run as root.

### Mac
Using this library on a mac is simpler in many ways, because MacOS does not require any kind of permission or driver setup before you can use the serial bridge.  You basically just plug it in and it works.  Almost.

Nearly everything will work out of the box, but there is one operation that will cause trouble: changing the configuration or type of a serial bridge which is currently configured as UART_CDC.  To do this, cy_serial_bridge needs to connect to the manufacturer interface of the USB device.  However, when in UART CDC mode, there will be a kernel driver attached to this interface which is providing the CDC USB serial port.  This kernel driver needs to be detached in order for cy_serial_bridge to claim the CY7C652xx device and change its configuration.

Ordinarily, libusb automatically handles detaching the kernel driver on most OSs, including MacOS.  However, Apple decided to [restrict](https://github.com/libusb/libusb/wiki/FAQ#how-can-i-run-libusb-applications-under-mac-os-x-if-there-is-already-a-kernel-extension-installed-for-the-device-and-claim-exclusive-access) this API to processes with root permissions unless your code has a special type of certificate, one which they [don't even give out](https://github.com/libusb/libusb/issues/1014) to ordinary registered developers!

So, it is currently impossible for cy_serial_bridge to reconfigure a device set to UART_CDC mode unless the Python process is run as root.  Note that this only applies to reconfiguring the serial bridge when in UART CDC mode; you do not have to be root to send/receive data with the serial bridge in UART or any other mode.

## Setting Up New Devices
When new CY76C65211 devices arrive, and you use USCU to configure them, they will get the Cypress VID (0x4b4), and can end up with one of several PID values (e.g. 0x0003, 0x0004, etc) depending on what model of chip they are and what mode they are configured as (I2C, SPI, etc).  

However, when using the chips with this driver, we generally want them to have a consistent VID & PID so that the driver can reliably find them.  Additionally, using the default VID & PID causes major problems on Windows because Windows "knows" that Cypress's CYUSB3 driver is the best driver for this chip, so it will replace the WinUSB driver installed by Zadig with CYUSB3 each time the chip is re-plugged in.  

To get around these issues, I'm adopting the convention that we'll assign CY7C65xx devices the regular Cypress VID, but use an arbitrary new VID of 0xE010.  You can set this configuration on a new device with a command like:
```shell
cy_serial_cli --vid 0x04b4 --pid <pid of your device> reconfigure --set-pid 0xE010
```
(note that this has to be run from a `poetry shell` if developing locally)

To determine what the VID and PID of your device currently are, you can use:
```shell
cy_serial_cli scan --all
```

Also note that adding `--randomize-serno` or `--set-serno` can be added to the reconfigure command to change the serial number of the chip, which is helpful for provisioning new boards.

Another issue: On Windows, if you have a given VID and PID assigned to use the WinUSB driver via Zadig, Windows will not try and use the USB CDC driver to enumerate COM ports from the device.  This means that a device in SPI/I2C mode cannot use the same VID and PID as a device in UART CDC mode.  To solve this, this driver automatically uses two PIDs for each device.  The even PID is used in SPI/I2C mode, and the odd PID is used in UART CDC mode.

For dual-channel devices, this is extended to four VIDs/PIDs: one for both in non-UART mode, one for SCB 0 in UART mode, one for SCB 1 in UART mode, and one for both SCBs in UART mode.

WARNING: This setup is for dev testing only!  If you plan to use this driver in a real product, this strategy will not be usable, this VID space is owned by Cypress.  You will have to purchase a VID and several consecutive PID values for yourself.

## To Do List / Known Issues
- If the I2C lines are not pulled up to 3.3V, I2C read and write bulk transfers hang forever and we don't have error handling for this
- Need to understand the formatting of the data argument for SPI when frame size is not 8 bits
- I2CNACKError.bytes_written is not correct
- Need to characterize the largest transfer size that can be done of each type -- e.g. can we do transfers larger than the 256 byte (SPI/I2C) / 190 byte (UART) buffer on the chip?
- Need to understand the deal with the "notification LED" settings on this chip.  It seems like the notification LED is constantly on whenever I enable it.  The docs say it "drives a GPIO on both USB transmit and receive", but what does that mean exactly?

### Note about Vendor UART Mode
Currently, this driver does not support the "vendor UART" mode of the serial bridge chip.  In vendor UART mode, the serial bridge chip still displays the custom "vendor" interface to the PC, and the Python driver has to manually send and receive bytes via the correct endpoints.  The problem, however, is that the A in UART stands for Asynchronous: other devices can send bytes to the serial bridge asynchronously whenever they want to, and it's up to the host software to get the bytes off the chip before its 190 byte buffer fills up.

Unfortunately, the way that a naive python driver would work, bytes would only be read when a thread calls a read function on the UART driver.  They aren't read at any other time.  So, if somebody is sending you data over UART and your application code does not poll the UART fast enough, you would lose data.

One way to fix this would be to have the driver continually submit USB transfers to the CY7C652xx in the background, so that data is transferred to the host machine as soon as the bridge chip makes it available.  This would help with the buffering situation a lot, and python-libusb1 does support the async API needed to make it work.  However, the threading situation would get complicated: either we would need a new background thread to be in charge of submitting all the transfers and monitoring when they finish (and also this might mean converting every single transfer done anywhere in the driver into an asynchronous transfer so that that thread is the only one processing events), OR we need support for the `handleEventsCompleted()` function, which is [not currently included](https://github.com/vpelletier/python-libusb1/issues/94) in the python-libusb1 wrapper.

However, even that method wouldn't be the best possible solution, because data could still be lost if the Python thread doing the USB operations doesn't run fast enough.  Instead, using UART CDC mode lets the OS driver handle all of the nitty-gritty USB stuff.  On the application side, the well-established Pyserial library can be used to read from the standard serial port provided by the CY7C652xx.

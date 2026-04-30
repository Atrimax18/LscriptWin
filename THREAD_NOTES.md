# LscriptWin Thread Notes

## Goal

Maintain a Windows-driven automation flow for LSBB DUT installation and configuration.

The Windows machine is connected directly to the DUT using:

- UART serial adapters for console control
- Ethernet cable for file transfer and network checks
- Local Windows folders for images, utilities, and the MAC database

No separate Linux image server is required for the current hardware setup.

## Current Hardware Setup

- Windows PC is the automation controller and file source.
- Main script: `lscriptwin.py`
- Runtime configuration: `script_setup.yaml`
- NXP console: `COM20`, `115200`
- Marvell switch console: `COM21`, `115200`
- Extra configured serial ports:
  - `COM22`, `115200`
  - `COM24`, `115200`
- Windows host Ethernet IP: `10.10.10.1`
- DUT emergency/final Ethernet IP: `10.10.10.2`
- Switch management IP after SONiC config: `192.168.2.2`
- NXP-to-switch management side: `192.168.2.1/24`

## Local Windows Folders

- Image folder: `C:\Images`
- Utilities folder: `C:\LSBB_Utils`
- MAC database: `C:\Users\alexeyt\source\repos\DB_MAC\sn_mac_test.db`

The script detects the Windows-style `server.image_path` value and makes the DUT pull files from the Windows PC using `scp`.
This requires the Windows PC to expose an SSH server during the transfer stages.
Before provisioning starts, the script prints the local `sshd` service status and stops early unless it is running.

## File Transfer Model

- The DUT first configures `eth0` as `10.10.10.2`.
- The script verifies reachability by pinging `10.10.10.1` from the DUT.
- After the DUT configures `eth0`, it pulls files from the Windows host with `scp`.
- Deploy and switch image files are pulled from the Windows `C:\Images` folder to the DUT.
- `LSBB_Utils` is pulled directly from Windows to `/root` on the DUT with `scp -r`.
- Linux-style server paths still use the same DUT-side SCP pull pattern.

## YAML Data Kept

- Serial ports and baud rates remain in `serial`.
- Windows host IP and local image folder remain in `server`.
- DUT login, password, final IP, temporary path, deploy image filename, emergency prompt, and utilities path remain in `dut`.
- Switch prompt and ITB filename remain in `switch`.
- SONiC login, prompt, and image filename remain in `sonic`.
- Timers remain in `timeouts`.
- MAC database settings remain in `db`.

## Supported Script Modes

- `detect`: stop NXP autoboot and confirm the switch `Telesat>>` prompt.
- `mac-only`: stop both sides in U-Boot, program NXP and switch MACs, then stop.
- `provision`: run the full install and configuration flow using Windows-hosted files.
- `gen_mac`: allocate or fetch MAC blocks from the SQLite database using a DIG serial number.

## Current Provision Flow

1. Open and log `COM20` and `COM21`.
2. Stop NXP autoboot and wait for switch U-Boot prompt.
3. Program NXP MACs and switch U-Boot MAC.
4. Boot NXP into emergency Linux.
5. Configure DUT IP as `10.10.10.2` and ping Windows host `10.10.10.1`.
6. Pull the deploy script from `C:\Images` to `/tmp` with `scp`.
7. Run the deploy script on the DUT.
8. Reboot and configure persistent DUT networking.
9. Pull switch image files from `C:\Images` to `/tmp` with `scp`.
10. Start TFTP and HTTP services on the DUT for switch installation.
11. Install and boot SONiC on the switch.
12. Configure SONiC management networking.
13. Pull `LSBB_Utils` to `/root` with `scp -r`.
14. Verify switch and NXP management pings.

## Operator Notes

- Run commands with `python .\lscriptwin.py ...`.
- The Ethernet adapter on the Windows PC must own `10.10.10.1`.
- The DUT must be able to ping `10.10.10.1` before image transfer begins.
- Windows OpenSSH Server must be available so the DUT can pull files over `scp`.
- SCP login uses the Windows host credentials from `server.login` and `server.password`.
- Required image filenames still come from YAML unless overridden by command-line arguments.

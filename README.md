# Lscript

Windows-driven serial automation for the LSBB lab flow.

The script now supports four modes:

- `detect`: stop NXP autoboot on `COM20` and confirm the switch `Telesat>>` prompt on `COM21`
- `mac-only`: stop both sides in U-Boot, program NXP and switch MACs, and stop there
- `provision`: run the documented flow from MAC programming through Linux install, switch image install, SONiC setup, and `LSBB_Utils` copy
- `gen_mac`: read and update the MAC database from the YAML `db` section using a DIG board serial number

Serial logs are saved under `C:\Logs\Deployment\<DIG_SN>\<timestamp>\`, and both `COM20` and `COM21` are opened and logged from the start of the run. If no `--dig_sn` is provided, logs are saved under `C:\Logs\Deployment\NO_DIG_SN\<timestamp>\`.

If `tqdm` is installed, long operator-visible waits such as the SONiC first-boot timer are shown with a progress bar. Without `tqdm`, the script falls back to the built-in text countdown.

## YAML Configuration

The script reads its runtime settings from [script_setup.yaml](C:/Users/alexeyt/source/repos/LscriptWin/script_setup.yaml).

- `serial`
  Defines the serial ports and baud rates. `nxp` is the NXP terminal, `switch` is the switch terminal, and `sx1` / `sx2` are extra ports kept in the config.
- `server`
  Defines the Windows host IP and local image folder. When `server.image_path` is a Windows path such as `C:\Images`, the DUT pulls those files from the Windows machine over SCP after Ethernet is configured. Linux-style paths keep the same DUT-side SCP pull flow.
- `dut`
  Defines NXP-side Linux settings such as final management IP, login, password, temporary folder, deploy script filename, emergency shell prompt, and the local Windows `LSBB_Utils` folder.
- `switch`
  Defines switch-side U-Boot settings such as switch management IP, U-Boot prompt text, and default ITB filename.
- `sonic`
  Defines SONiC login settings: login user, password, login prompt, shell prompt, and default SONiC image filename.
- `prompts`
  Defines generic prompt text used for U-Boot and switch prompt detection.
- `timeouts`
  Controls all wait timers:
  `serial_open_seconds`, `prompt_wait_seconds`, `boot_interrupt_seconds`, `uboot_boot_seconds`, `emergency_boot_seconds`, `first_boot_seconds`, and `sonic_boot_seconds`.
- `db`
  Defines the SQLite MAC database used by `--dig_sn`, including DB path, table, serial column, MAC column naming pattern, and MAC count.

## Detect Mode

```powershell
python .\lscriptwin.py --mode detect
```

If the target expects a different autoboot interrupt key:

```powershell
python .\lscriptwin.py --mode detect --boot-stop-key space
python .\lscriptwin.py --mode detect --boot-stop-key ctrl-c
```

To validate only the NXP side:

```powershell
python .\lscriptwin.py --mode detect --skip-switch
```

## DB MAC Generation Mode

Use the DB-backed allocator when you want the script to manage a row of 16 MAC addresses for a DIG board serial number:

```powershell
python .\lscriptwin.py --mode gen_mac --dig_sn CLSDM-09-0926-260528-002
```

Supported DIG SN formats currently include:

- `CLSDM-09-0926-260528-002`
- `MLSDM-08-0726-B1-00010`

What `gen_mac` does:

- validates the DIG SN format
- connects to the database defined under `db:` in [script_setup.yaml](C:/Users/alexeyt/source/repos/LscriptWin/script_setup.yaml)
- checks whether the serial number already exists
- if the serial exists and all 16 MAC fields are already filled, prints the existing block and does not modify the DB
- if the serial exists but the MAC block is incomplete, prints a message and rewrites all 16 MAC addresses as a sequential `+1` range
- if the serial does not exist, finds the latest saved MAC address in the DB, creates the next sequential block of 16 MAC addresses, inserts the DIG SN, and prints the assigned range

Current DB config keys:

- `db.type`
  Right now only `sqlite` is supported
- `db.path`
  Relative paths are resolved next to the YAML file
- `db.table`
- `db.serial_column`
- `db.mac_column_format`
  Example: `mac{index}` gives `mac1` through `mac16`
- `db.mac_count`
- `db.seed_mac`
  Used as the first MAC only when the database has no saved MACs yet
- `db.auto_create`
  When `true`, the SQLite table is created automatically if it does not exist

## Provision Mode

The concrete command sequence was mapped from the manual starting at `BURN MAC ADDRESSES in NXP`.

Required runtime MAC argument:

- `--base-mac`
  Written only to NXP `mac 0`

NXP `mac 1`, `mac 2`, and `mac 3` are always written with these fixed values:

- `mac 1 00:04:9F:08:44:A2`
- `mac 2 00:04:9F:08:44:A3`
- `mac 3 00:04:9F:08:44:A4`

Optional derived MAC arguments:

- `--switch-uboot-mac`
  Default is `base-mac + 1`
- `--switch-onie-mac`
  Default is `base-mac + 1`

Example:

```powershell
python .\lscriptwin.py --mode provision `
  --base-mac 70:B3:D5:97:07:C0
```

Optional image overrides:

```powershell
python .\lscriptwin.py --mode provision `
  --base-mac 70:B3:D5:97:07:C0 `
  --deploy-script deploy-lsbb-1.1.1-20260324.sh `
  --switch-image sonic-marvell-arm64.bin `
  --switch-itb telesat_lsbb-r0.itb
```

If these fields are present in [script_setup.yaml](C:/Users/alexeyt/source/repos/LscriptWin/script_setup.yaml), the filenames are read from YAML by default:

- `dut.image_file` for the deploy script
- `switch.image_file` for the switch ITB
- `sonic.image_file` for the SONiC image

Timeouts are now separated in YAML:

- `timeouts.uboot_boot_seconds` for the U-Boot capture phase
- `timeouts.emergency_boot_seconds` for the reboot into emergency Linux

`timeouts.first_boot_seconds` is kept as a legacy timeout value.

`timeouts.sonic_boot_seconds` controls the operator-visible SONiC first-boot wait after the NXP reset pulse.

To skip the final `LSBB_Utils` copy:

```powershell
python .\lscriptwin.py --mode provision `
  --base-mac 70:B3:D5:97:07:C0 `
  --skip-utils
```

## What Provision Mode Does

- Stops autoboot on the NXP console and enters U-Boot
- Monitors NXP and switch boot in parallel from the start of the run
- Verifies on NXP boot that both CLUs are locked and that `Switch ready` and `FPGA ready` appear before continuing
- Waits for the switch `Telesat>>` U-Boot prompt
- Writes NXP `mac 0` from the runtime argument
- Writes fixed NXP values to `mac 1`, `mac 2`, and `mac 3`
- Burns the switch U-Boot MAC with `setenv ethaddr` using `base-mac + 1`
- After MAC programming, waits 1 second and sends `boot` on the NXP side to continue automatically into emergency Linux
- Shows/logs that the DUT is resetting instead of asking the operator to press the reset button
- Waits for the maintenance message, and then waits for the `sh-5.2#` prompt on the NXP terminal
- Configures DUT IP and verifies Windows host reachability with one combined emergency command:
  `ifconfig eth0 10.10.10.2 netmask 255.255.255.0 up ; ping 10.10.10.1 -c1`
- Continues only after detecting:
  `1 packets transmitted, 1 packets received, 0% packet loss`
- Sends Enter twice after successful emergency ping, then starts file transfer
- When `server.image_path` is a Windows folder, the DUT pulls the deploy script from the Windows SSH server, for example:
  `scp deploy@10.10.10.1:"C:/Images/deploy-lsbb-1.1.1-20260324.sh" /tmp/`
- This Windows-only transfer path requires an SSH server running on the Windows PC
- Before provisioning starts, the script prints the local Windows `sshd` service status and stops early unless it is `RUNNING`
- With Linux-style server paths, keeps the older SCP behavior and uses `server.login` / `server.password`
- Verifies the deploy script appears in `/tmp`, and only then runs it
- After the deploy script completes, logs in on NXP if needed with `root` / `toor` and sends `reboot`
- Configures persistent DUT networking with `nmcli`
- After `nmcli con mod fm1-mac5-static connection.autoconnect yes`, waits 2 seconds, sends Enter twice, and only then shows the next reset-button action
- After the persistent NXP IP configuration is saved, waits 1 second and sends `reboot` automatically instead of asking the operator to press reset
- Pulls switch image files from the Windows image folder to `/tmp` with `scp`
- Configures `fm1-mac10` as `192.168.2.1/24`
- After `nmcli con show` on the NXP terminal, switches to COM21 and sends `ping $serverip` from the switch U-Boot prompt
- Starts the local TFTP and HTTP services on the DUT
- Waits 2 seconds after `httpserv -p 80 &`
- Programs the switch install URL and boots the switch image
- After `bootm $onie_loadaddr`, waits on COM21 for the reboot-request message
- Waits 5 seconds, then sends the NXP reset pulse:
  `cd /root`
  `cpld w 0x45 0`
  `cpld w 0x45 3`
- Runs the operator-visible SONiC first-boot timer from `timeouts.sonic_boot_seconds`
- After the timer completes, sends Enter twice on COM21, waits for `sonic login:`, and logs in with `sonic.login` and `sonic.password` from YAML
- Logs into SONiC and runs the documented config commands:
  `sudo sonic-cfggen -w -j /usr/share/sonic/device/arm64-telesat_lsbb-r0/telesat-lsbb/default_config.json`
  `sudo config qos reload`
  `sudo config interface ip add eth0 192.168.2.2/24`
- Waits 8 seconds after each SONiC config command
- Pulls the Windows `LSBB_Utils` folder directly to `/root/` with `scp -r` unless `--skip-utils` is used
- After the copy, waits for switch `System is ready`, then runs the switch management ping:
  `sudo ip vrf exec mgmt ping 192.168.2.1 -c1`
- After the switch ping, runs the NXP ping:
  `ping 192.168.2.2 -c1`
- Stops immediately if the switch log shows bootm failures such as:
  `Wrong Image Format for bootm command`
  `ERROR: can't get kernel image!`

## Manual Intervention Points

The NXP reset-button steps are now automated by the script:

- after MAC programming, the script sends `boot`
- after the deploy script completes, the script sends `reboot`
- after the persistent NXP IP configuration is saved, the script sends `reboot`

The remaining operator-visible waits are mainly informational, such as the SONiC first-boot timer and progress bars.

## Known Manual Gaps

The Word manual includes some unclear or placeholder text that was not converted into executable commands:

- `First command`, `Second command`, `Third command`, `Fourth command`
- The garbled lines around the second transfer block
- The upgrade section with `swupdate-client`, which appears to be a separate flow

Those steps will need the exact intended commands before they can be automated safely.

## MAC-Only Mode

To stop both chips in U-Boot, write the NXP MAC set, write the switch `ethaddr`, and stop there:

```powershell
python .\lscriptwin.py --mode mac-only --base-mac 70:B3:D5:97:07:D8
```

This mode does only:

- stop NXP in U-Boot
- stop switch in U-Boot
- write NXP `mac 0` from `--base-mac`
- write fixed NXP values to `mac 1`, `mac 2`, `mac 3`
- write switch `ethaddr` as `base-mac + 1`
- save both sides and exit

## Final Status

On successful completion, the script ends with:

```text
[OK] Full installation and board configuration completed successfully
```

If something fails, the script ends with an `[ERROR] ...` message that describes the failure point.

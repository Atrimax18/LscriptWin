# LscriptWin

Windows tooling for LSBB board deployment, switch provisioning, SX4000 setup, and production test.

The current entry point is the Tkinter launcher:

```powershell
.\run_cmd.bat
```

`run_cmd.bat` starts:

```powershell
python .\lsbb_deploy3.py
```

The launcher asks for a DIG serial number, validates the supported serial format, checks SQL Server deployment progress, and enables only the deployment actions that are valid for that board.

Supported serial examples:

- `CLSDM-09-0926-260528-002`
- `MLSDM-08-0726-B1-00010`

## Current Flow

The GUI exposes these actions:

- `FULL DEPLOYMENT`
  Runs NXP deployment, switch deployment, and SX deployment in order.
- `NXP DEPLOYMENT`
  Runs only the NXP stage.
- `SWITCH DEPLOYMENT`
  Runs only the switch and SONiC stage. This is enabled after NXP passes.
- `SX DEPLOYMENT`
  Runs only the SX4000 stage. This is enabled after switch deployment passes.
- `Test PCBA`
  Runs the current system test script.
- `Test SYSTEM`
  Runs the current system test script. If `Save SFP` is selected, it also validates and saves ETH10-ETH13 SFP data to SQL Server.

The GUI records stage state in SQL Server:

- `nxp`
- `switch`
- `sx`

Each stage is marked `pending`, `running`, `success`, or `failed`. After a successful NXP stage, the 16 MAC addresses allocated in the SQLite MAC database are copied into SQL Server for the same serial number.

## Main Scripts

| Script | Purpose |
| --- | --- |
| `lsbb_deploy3.py` | GUI launcher and SQL progress coordinator |
| `lscriptnxp2.py` | NXP staged provisioning flow |
| `eth_deploy.py` | Switch U-Boot, ONIE, SONiC, and management setup |
| `sx_deploy3.py` | SX4000 file validation, boot/configure flow, transfer, and shutdown |
| `test_sys3.py` | LSBB system/PCBA test runner and result parser |
| `script_setup.yaml` | Serial, server, DUT, switch, SONiC, SX4000, timeout, and SQLite MAC settings |
| `test_setup.yaml` | Expected values and limits used by the test runner |
| `db_config.ini` | SQL Server connection and table settings |

## Configuration

Runtime deployment settings are in [`script_setup.yaml`](script_setup.yaml).

Important sections:

- `serial`
  Defines NXP, switch, SX1, and SX2 COM ports and baud rates.
- `server`
  Defines the Windows host IP, login, image folder, and public key path used during transfers.
- `dut`
  Defines NXP Linux login, final IP, deploy script filename, prompt text, and `LSBB_Utils` path.
- `switch`
  Defines switch management IP, U-Boot prompt, and ITB image filename.
- `sonic`
  Defines SONiC login, prompt, and image filename.
- `timeouts`
  Defines serial open, prompt wait, U-Boot, emergency boot, first boot, and SONiC boot timers.
- `sx4000`
  Defines SX1/SX2 source folders, startup file, remote flash paths, and modem prompt.
- `sx4000_ip`
  Defines SX modem IPs and login credentials.
- `db`
  Defines the SQLite MAC database used by the NXP stage.

SQL Server settings are in [`db_config.ini`](db_config.ini). The GUI uses this file to create/read/update the deployment progress table and MAC table.

Test expectations are in [`test_setup.yaml`](test_setup.yaml). The test runner compares measured output against this file and saves text/CSV summaries.

## Logs And Artifacts

Deployment UART logs are saved under:

```text
C:\Logs\Deployment\<DIG_SN>\
```

NXP deployment creates a timestamped run folder for each serial:

```text
C:\Logs\Deployment\<DIG_SN>\<timestamp>\
```

Switch and SX deployment scripts also write timestamped logs under the same deployment log root.

Test artifacts are saved under the log root configured by `test_setup.yaml` when saving is enabled. Typical artifacts include:

- `output.txt`
- `report_pass.txt` or `report_fail.txt`
- `report_pass.csv` or `report_fail.csv`
- `summary.txt`
- `error.txt` on failures

## Stage Details

### NXP Deployment

Run directly when debugging:

```powershell
python .\lscriptnxp2.py --dig_sn CLSDM-09-0926-260528-002
```

The NXP flow:

- opens the NXP UART
- stops autoboot
- reads or allocates MAC addresses from the SQLite DB
- burns NXP MAC values in U-Boot
- resets into emergency mode
- configures temporary network access
- starts dropbear
- pushes the configured deploy script from Windows to the DUT
- runs the deploy script
- reboots into Linux
- saves the final DUT IP
- copies `LSBB_Utils` to the DUT

Useful options:

```powershell
python .\lscriptnxp2.py --dig_sn CLSDM-09-0926-260528-002 --boot-stop-key space
python .\lscriptnxp2.py --dig_sn CLSDM-09-0926-260528-002 --show-uart
python .\lscriptnxp2.py --dig_sn CLSDM-09-0926-260528-002 --deploy-script deploy-lsbb-1.1.1-20260324.sh
```

### Switch Deployment

Run directly when debugging:

```powershell
python .\eth_deploy.py --dig_sn CLSDM-09-0926-260528-002
```

The switch flow:

- reads MAC values for the DIG serial
- verifies the switch MAC is base MAC + 1
- opens switch and NXP UART sessions
- stops switch U-Boot
- writes `ethaddr` and saves the environment
- configures NXP `fm1-mac10`
- copies switch image files
- starts local TFTP/HTTP services on the DUT
- configures ONIE install variables
- boots the switch image
- resets the switch through NXP
- waits for SONiC boot
- logs into SONiC
- configures management networking
- verifies management ping

### SX Deployment

Run directly when debugging:

```powershell
python .\sx_deploy3.py --dig_sn CLSDM-09-0926-260528-002
```

Check only:

```powershell
python .\sx_deploy3.py --dig_sn CLSDM-09-0926-260528-002 --mode check
```

Skip transfers during debug:

```powershell
python .\sx_deploy3.py --dig_sn CLSDM-09-0926-260528-002 --skip-transfer
```

The SX flow validates configured folders/files, logs into the NXP shell, boots/configures SX1 and SX2 paths, copies configured files to modem flash, and shuts the modems down at the end of the stage.

### Tests

Run directly:

```powershell
python .\test_sys3.py --dig_sn CLSDM-09-0926-260528-002
```

Run only `run.sh` checks:

```powershell
python .\test_sys3.py --dig_sn CLSDM-09-0926-260528-002 --mode run-sh
```

Parse an existing log instead of running SSH:

```powershell
python .\test_sys3.py --dig_sn CLSDM-09-0926-260528-002 --input-log logs\output.txt
```

Validate and save SFP data:

```powershell
python .\test_sys3.py --dig_sn CLSDM-09-0926-260528-002 --save-sfp
```

The test runner uses SSH connection defaults from `script_setup.yaml`, expected values from `test_setup.yaml`, and optional switch UART login checks before comparing results.

## Requirements

This project is intended to run on Windows with:

- Python 3
- access to the configured COM ports
- Windows SSH/SCP support where required by the deployment flow
- SQL Server ODBC driver for GUI progress tracking
- Python packages used by the scripts, including `pyserial` and `pyodbc`
- `paramiko` is optional for tests; the test runner can fall back to system `ssh`

The configured image and utility folders must exist before deployment:

- `server.image_path`
- `dut.utils_path`
- `sx4000.SX1_path`
- `sx4000.SX2_path`
- `sx4000.startup_file`

## Recommended Operator Workflow

1. Confirm `script_setup.yaml`, `test_setup.yaml`, and `db_config.ini` match the station.
2. Confirm Windows can access the configured serial ports.
3. Confirm the image folders contain the configured deploy, ITB, SONiC, SX1, and SX2 files.
4. Start the launcher with `.\run_cmd.bat`.
5. Enter the DIG serial number.
6. Run `FULL DEPLOYMENT` for a new board, or the enabled next stage for a resumed board.
7. Run `Test PCBA` or `Test SYSTEM` after deployment.

If a stage fails, the GUI records the failure in SQL Server and leaves the valid recovery action enabled after the issue is fixed.

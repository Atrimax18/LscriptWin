# lscriptwin_sx.py

Combined LSBB deployment and optional SX4000 configuration runner.

`lscriptwin_sx.py` runs the normal deployment flow first. If deployment passes and `--sx_config true` is provided, it continues into the SX4000 configuration stage.

The SX4000 stage uses a mixed connection model:

- NXP control is done over UART.
- PC file transfer to SX1/SX2 is done over SSH/SFTP.
- Switch SONiC commands are done over UART.
- SX1/SX2 modem commands are done over UART.

Logs are saved under:

```text
C:\Logs\Deployment\<DIG_SN>\<timestamp>\
```

For example:

```text
C:\Logs\Deployment\MLSDM-08-0726-B1-00010\20260504-143022\
```

If `--dig_sn` is not provided, logs are saved under `C:\Logs\Deployment\NO_DIG_SN\<timestamp>\`.

## What It Does Step by Step

1. Reads `script_setup.yaml`.
2. Starts the deployment stage by calling the existing `lscriptwin.py` logic.
3. Uses `--mode provision` by default, unless another `--mode` is provided.
4. Stops immediately if deployment fails.
5. Prints `[OK] Deployment stage PASSED.` only when deployment returns exit code `0`.
6. If `--sx_config true` is not provided, exits after deployment with a final `PASSED` message.
7. If `--sx_config true` is provided, starts SX4000 configuration.
8. Validates the SX4000 JSON, TXT, and startup script files from YAML.
9. Opens the Switch UART and runs the SONiC switch commands.
10. Opens the NXP UART and logs in if needed.
11. Starts `/root/LSBB_Utils/run.sh` and runs the SX4000 reset/bootstrap commands over NXP UART.
12. Opens the SX1 and SX2 UART terminals and runs the modem network/ping commands.
13. Uploads SX4000 files from the PC to SX1/SX2 with SFTP unless `--sx-skip-transfer` is used.
14. Verifies each remote uploaded file exists and has the same byte size as the local file.
15. Prints final `PASSED` if both deployment and SX4000 stages complete.
16. Prints final `FAILED` with the failing stage or error message if anything fails.

## Basic Deployment Only

```powershell
python .\lscriptwin_sx.py --base-mac 70:B3:D5:97:07:C0
```

This runs deployment only. The SX4000 stage is skipped.

## Deployment Then SX4000 Configuration

```powershell
python .\lscriptwin_sx.py --base-mac 70:B3:D5:97:07:C0 --sx_config true
```

The SX4000 stage starts only after deployment passes.

## DB-Backed MAC Allocation

```powershell
python .\lscriptwin_sx.py --dig_sn CLSDM-09-0926-260528-002 --sx_config true
```

`--dig_sn` is passed through to the deployment logic.

## Optional Deployment Arguments

The wrapper passes these arguments to `lscriptwin.py`:

- `--config`
- `--mode`
- `--boot-stop-key`
- `--skip-switch`
- `--base-mac`
- `--switch-uboot-mac`
- `--switch-onie-mac`
- `--deploy-script`
- `--switch-image`
- `--switch-itb`
- `--skip-utils`
- `--dig_sn`

Default deployment mode is:

```text
--mode provision
```

## SX4000 Arguments

- `--sx_config true`
  Runs the SX4000 configuration stage after successful deployment.

- `--sx_config false`
  Default behavior. Runs deployment only.

- `--sx-skip-transfer`
  Runs SX4000 commands but skips SFTP upload of `startup.sh`, JSON files, and `ip_params.txt`.

## Upload Verification

After each SFTP upload, the script checks the remote file with `stat` and compares the remote byte size with the local byte size. This check is done for both SX1 and SX2, including:

- `startup.sh`
- all configured SX4000 JSON files
- each modem's `ip_params.txt`

If any file is missing or has a different size, the SX4000 stage fails and prints the affected modem and file name.

## Configuration Values

The SX4000 stage uses these YAML values:

- `dut.login`
- `dut.password`
- `serial.nxp`
- `sonic.login`
- `sonic.password`
- `serial.switch`
- `serial.sx1`
- `serial.sx2`
- `sx4000_ip.sx1`
- `sx4000_ip.sx2`
- `sx4000_ip.login`
- `sx4000_ip.password`
- `sx4000.file1`
- `sx4000.file2`
- `sx4000.file3`
- `sx4000.sx1_params`
- `sx4000.sx2_params`
- `sx4000.startup_file`
- `sx4000.modem_flash1_path`
- `sx4000.modem_flash2_path`

## Python Requirement

The PC-to-SX1/SX2 SFTP upload and verification parts use Paramiko:

```powershell
python -m pip install paramiko
```

Deployment-only runs do not need Paramiko unless `--sx_config true` is used.

## Success and Failure Messages

Deployment-only success:

```text
[OK] PASSED: deployment completed successfully. SX4000 configuration was not requested.
```

Deployment plus SX4000 success:

```text
[OK] PASSED: deployment and SX4000 configuration completed successfully.
```

Failure:

```text
[FAILED] deployment: deployment returned exit code 1
```

or:

```text
[FAILED] <error details>
```

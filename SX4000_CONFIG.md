# SX4000 Configuration Script

`sx4000_config.py` is the second-stage SX4000 modem configuration helper. It reads the existing `script_setup.yaml` SX4000 sections and uses a built-in command sequence by default.

## YAML Entries Used

- `serial.nxp`
- `serial.switch`
- `serial.sx1`
- `serial.sx2`
- `dut.login` / `dut.password`
- `sonic.login` / `sonic.password` / `sonic.prompt`
- `timeouts.serial_open_seconds`
- `timeouts.prompt_wait_seconds`
- `timeouts.emergency_boot_seconds`
- `sx4000.file1`, `sx4000.file2`, `sx4000.file3`
- `sx4000.sx1_params`
- `sx4000.sx2_params`
- `sx4000.startup_file`
- `sx4000.modem_flash1_path`
- `sx4000.modem_flash2_path`
- `sx4000.prompt`
- `sx4000_ip.sx1`
- `sx4000_ip.sx2`
- `sx4000_ip.login`
- `sx4000_ip.password`

## Preflight Check

Run this first. It validates only the SX4000 files configured under `C:\Images`, but does not open serial ports:

```powershell
python .\sx4000_config.py --mode check --skip-terminal-check
```

Expected current result:

```text
[OK] SX4000 JSON, TXT, and SH files are present.
[OK] Parsed commands: 6 switch, 5 SX1, 5 SX2.
```

To also check that the NXP and Switch serial terminals are logged in, run:

```powershell
python .\sx4000_config.py --mode check
```

The script opens `serial.nxp` and `serial.switch`. If a login prompt is shown, it uses `dut.login` / `dut.password` for NXP and `sonic.login` / `sonic.password` for the Switch.

## Full Configuration

```powershell
python .\sx4000_config.py --mode configure
```

This flow:

- validates the JSON, TXT, and SH files listed in YAML
- uses the built-in SX4000 command sequence
- logs into the Switch SONiC terminal if needed
- enters the SONiC commands
- logs into SX1 and SX2 serial terminals if needed
- enters the SX1 and SX2 commands
- uploads `startup.sh` to `sx4000.modem_flash1_path` on both modems
- uploads the JSON files and each modem's `ip_params.txt` to `sx4000.modem_flash2_path` with PuTTY `pscp`
- runs `sh ./run.sh` from `LSBB_Utils` on the NXP terminal, waits for the interactive prompt, and calls the SX4000 reset/bootstrap commands directly

## File Upload

The normal `configure` flow uploads all required modem files:

- `startup.sh` is saved under `sx4000.modem_flash1_path`
- `sx4000.file1`, `sx4000.file2`, `sx4000.file3`, and each modem's `ip_params.txt` are saved under `sx4000.modem_flash2_path`

Current YAML paths:

```text
sx4000.modem_flash1_path: /mnt/flash1
sx4000.modem_flash2_path: /mnt/flash2
```

Use `--skip-transfer` when you want to run terminal commands without copying files.

## Ping Verification

SX4000 modem ping commands are normalized from:

```text
ping 10.10.10.15
```

to:

```text
ping -c1 10.10.10.15
```

The script waits for the modem prompt to return and verifies both a reply line and `0% packet loss`. If ping fails, the script stops and prints the modem ping output in the error message.

## Useful Skips

Run only Switch commands:

```powershell
python .\sx4000_config.py --mode configure --skip-sx-config --skip-transfer
```

Run only SX serial commands:

```powershell
python .\sx4000_config.py --mode configure --skip-switch-config --skip-transfer
```

Run terminal commands but skip file upload:

```powershell
python .\sx4000_config.py --mode configure --skip-transfer
```

Skip the SX1/SX2 reset, serial configuration, and modem file upload stage:

```powershell
python .\sx4000_config.py --mode configure --skip-sx-config
```

## SX4000 Reset Stage

`run.sh` launches in its default `TARGET` mode when no argument is given:

```sh
ptpython -i LSBB_Top.py "$MODE"
```

The automation does not send the `1`, `2`, or `3` menu choices. It lets `LSBB_Top.py` finish startup, waits for the interactive Python prompt (`>>`, `>>>`, or `In [n]:`), and then sends direct commands.

Normal flow:

```powershell
python .\sx4000_config.py --mode configure
```

The order is:

- Switch SONiC commands
- NXP runs `cd /root/LSBB_Utils && sh ./run.sh` using the default `TARGET` mode
- automation waits for the Python prompt
- automation sends `sx4000_ctrl.sx4000_reset_and_bootstrap_ov("SX1")`
- SX1 serial commands
- SX1 file upload
- automation sends `sx4000_ctrl.sx4000_reset_and_bootstrap_ov("SX2")` in the same NXP session
- SX2 serial commands
- SX2 file upload
- automation sends `quit()` on the NXP terminal

## File Transfer Note

The script uses PuTTY `pscp.exe` because Windows OpenSSH `scp` cannot take the saved YAML password non-interactively. `pscp.exe` is available on this machine at:

```text
C:\Program Files\PuTTY\pscp.exe
```

If PuTTY has not accepted a modem host key before, `pscp` may prompt once on the first connection.

## Reset Implementation Note

The reset/bootstrap implementation calls `sx4000_ctrl.sx4000_reset_and_bootstrap_ov()` directly inside the interactive `LSBB_Top.py` session. If it reports `Reset Failure` or `Bootstrap Override Failed`, the script stops and prints the captured output.

from __future__ import annotations

import argparse
import contextlib
import os
import pathlib
import re
import subprocess
import sys
import tempfile
import time
from typing import TextIO

import sx4000_config


class TeeStream:
    def __init__(self, *streams: TextIO) -> None:
        self.streams = streams

    def write(self, text: str) -> int:
        for stream in self.streams:
            stream.write(text)
        return len(text)

    def flush(self) -> None:
        for stream in self.streams:
            stream.flush()


def shell_quote(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"


def windows_scp_options() -> list[str]:
    return [
        "-o",
        "StrictHostKeyChecking=no",
        "-o",
        "UserKnownHostsFile=NUL",
    ]


def run_windows_command_with_password(command: list[str], password: str, timeout: int, label: str) -> str:
    env = os.environ.copy()
    askpass_cmd: pathlib.Path | None = None
    askpass_ps1: pathlib.Path | None = None
    try:
        temp_dir = pathlib.Path(tempfile.mkdtemp(prefix="sx_scp_askpass_"))
        askpass_ps1 = temp_dir / "askpass.ps1"
        askpass_cmd = temp_dir / "askpass.cmd"
        escaped_password = password.replace("'", "''")
        askpass_ps1.write_text(f"Write-Output '{escaped_password}'\n", encoding="utf-8")
        askpass_cmd.write_text(
            '@powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0askpass.ps1"\n',
            encoding="utf-8",
        )
        env["SSH_ASKPASS"] = str(askpass_cmd)
        env["SSH_ASKPASS_REQUIRE"] = "force"
        env.setdefault("DISPLAY", "sx_deploy")

        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=0,
            env=env,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
        assert process.stdout is not None
        assert process.stdin is not None

        output_parts: list[str] = []
        recent = ""
        password_sent = False
        deadline = time.monotonic() + timeout

        while True:
            if time.monotonic() > deadline:
                process.kill()
                raise TimeoutError(f"{label} timed out after {timeout} seconds.")

            char = process.stdout.read(1)
            if char:
                print(char, end="", flush=True)
                output_parts.append(char)
                recent = (recent + char)[-200:].lower()
                if "password:" in recent and not password_sent:
                    process.stdin.write(password + "\n")
                    process.stdin.flush()
                    password_sent = True
                    recent = ""
                continue

            return_code = process.poll()
            if return_code is not None:
                output = "".join(output_parts)
                if return_code != 0:
                    raise RuntimeError(f"{label} failed with exit status {return_code}.\n{output.strip()}")
                return output

            time.sleep(0.05)
    finally:
        for path in (askpass_cmd, askpass_ps1):
            if path is not None:
                try:
                    path.unlink()
                except OSError:
                    pass
        if askpass_cmd is not None:
            try:
                askpass_cmd.parent.rmdir()
            except OSError:
                pass


def copy_file_with_windows_scp(
    config: sx4000_config.Sx4000RuntimeConfig,
    sx_name: str,
    host: str,
    local_file: pathlib.Path,
    remote_dir: str,
) -> None:
    remote_file = remote_dir.rstrip("/") + "/" + local_file.name
    remote_spec = f"{config.sx4000_ip.login}@{host}:{remote_file}"
    command = [
        "scp",
        *windows_scp_options(),
        str(local_file),
        remote_spec,
    ]
    sx4000_config.info(f"{sx_name}: Windows SCP {' '.join(command)}")
    run_windows_command_with_password(
        command,
        config.sx4000_ip.password,
        timeout=300,
        label=f"{sx_name} Windows SCP {local_file.name}",
    )


def modem_file_targets(
    config: sx4000_config.Sx4000RuntimeConfig,
    params_file: pathlib.Path,
) -> tuple[tuple[pathlib.Path, str], ...]:
    flash1_path = sx4000_config.modem_remote_path(config.sx4000.modem_flash1_path)
    flash2_path = sx4000_config.modem_remote_path(config.sx4000.modem_flash2_path)
    return (
        (config.sx4000.startup_file, flash1_path),
        *[(json_file, flash2_path) for json_file in config.sx4000.json_files],
        (params_file, flash2_path),
    )


def verify_file_with_sx_uart(
    session: sx4000_config.SerialSession,
    sx_name: str,
    local_file: pathlib.Path,
    remote_dir: str,
    prompt: object,
    timeout: int,
) -> None:
    remote_file = remote_dir.rstrip("/") + "/" + local_file.name
    command = f"ls -ln {shell_quote(remote_file)}"
    sx4000_config.info(f"{sx_name}: UART verify {command}")
    start_pos = len(session.buffer)
    session.send_line(command)
    listing_pattern = re.compile(
        r"(?:^|[\r\n])[-dlpscb][-rwxXsStT-]{9}\s+\d+\s+\d+\s+\d+\s+\d+\s+.*"
        + re.escape(local_file.name),
        re.MULTILINE,
    )
    result = session.wait_for_any_pattern(
        {
            "listing": listing_pattern,
            "missing": re.compile(r"No such file|cannot access", re.IGNORECASE),
            "prompt": prompt,
        },
        timeout=timeout,
        label=f"{sx_name} ls output for {local_file.name}",
        start_pos=start_pos,
    )
    output = session.buffer[start_pos:]
    if result == "missing":
        raise RuntimeError(f"{sx_name}: remote file missing after copy: {remote_file}. Output:\n{output.strip()}")

    remote_listing = ""
    for line in reversed(output.strip().splitlines()):
        if local_file.name in line:
            remote_listing = line
            break
    remote_listing_parts = remote_listing.split()
    try:
        remote_size = int(remote_listing_parts[4])
    except (IndexError, ValueError) as exc:
        raise RuntimeError(f"{sx_name}: could not verify remote size for {remote_file}. Output:\n{output.strip()}") from exc

    local_size = local_file.stat().st_size
    if remote_size != local_size:
        raise RuntimeError(
            f"{sx_name}: copied file size mismatch for {local_file.name}: "
            f"local={local_size} bytes, remote={remote_size} bytes"
        )
    sx4000_config.ok(f"{sx_name}: verified {local_file.name} over UART ({remote_size} bytes)")


def copy_modem_files_with_windows_scp(
    config: sx4000_config.Sx4000RuntimeConfig,
    sx_name: str,
    host: str,
    params_file: pathlib.Path,
) -> None:
    sx4000_config.info(f"{sx_name}: preparing Windows SCP upload to {config.sx4000_ip.login}@{host}")
    for local_file, remote_dir in modem_file_targets(config, params_file):
        copy_file_with_windows_scp(config, sx_name, host, local_file, remote_dir)


def configure_single_sx_terminal_and_transfer(
    config: sx4000_config.Sx4000RuntimeConfig,
    repo_root: pathlib.Path,
    sx_name: str,
    serial_config: sx4000_config.SerialPortConfig,
    sx_commands: tuple[str, ...],
    host: str,
    params_file: pathlib.Path,
    skip_transfer: bool,
) -> None:
    sx_prompt = sx4000_config.sx_prompt_pattern(config.sx4000.prompt)
    sx_log = sx4000_config.timestamped_log_path(repo_root, sx_name.lower())
    with sx4000_config.SerialSession(sx_name, serial_config, sx_log, config.serial_open_seconds) as sx:
        sx4000_config.info(f"{sx_name} log: {sx_log}")
        sx4000_config.ensure_sx_shell(
            sx,
            config.sx4000_ip.login,
            config.sx4000_ip.password,
            sx_prompt,
            config.shell_wait_seconds,
        )
        sx4000_config.ok(f"{sx_name} serial terminal is logged in.")
        for command in sx_commands:
            sx4000_config.run_sx_command(sx, command, sx_prompt, config.prompt_wait_seconds)
        if skip_transfer:
            sx4000_config.warn(f"Skipping {sx_name} file upload.")
            return
        copy_modem_files_with_windows_scp(config, sx_name, host, params_file)
        for local_file, remote_dir in modem_file_targets(config, params_file):
            verify_file_with_sx_uart(sx, sx_name, local_file, remote_dir, sx_prompt, config.prompt_wait_seconds)


def configure_sx4000_sequence_with_windows_scp(
    config: sx4000_config.Sx4000RuntimeConfig,
    commands: sx4000_config.CommandPlan,
    repo_root: pathlib.Path,
    skip_transfer: bool,
) -> None:
    sx4000_config.info(
        "SX4000 sequence: start run.sh over NXP UART, reset SX1, configure SX1, reset SX2, configure SX2, then quit()."
    )
    nxp_log = sx4000_config.timestamped_log_path(repo_root, "sx4000-nxp")
    with sx4000_config.SerialSession("NXP", config.nxp, nxp_log, config.serial_open_seconds) as nxp:
        sx4000_config.info(f"NXP SX4000 log: {nxp_log}")
        lsbb_prompt_started = False
        sx1_completed = False
        sx2_completed = False
        try:
            sx4000_config.start_lsbb_utils_prompt(nxp, config)
            lsbb_prompt_started = True

            sx4000_config.run_sx4000_reset_command(nxp, config, "SX1")
            sx4000_config.info("Waiting 20 seconds after SX1 reset/bootstrap.")
            time.sleep(20)
            configure_single_sx_terminal_and_transfer(
                config=config,
                repo_root=repo_root,
                sx_name="SX1",
                serial_config=config.sx1,
                sx_commands=commands.sx1,
                host=config.sx4000_ip.sx1,
                params_file=config.sx4000.sx1_params,
                skip_transfer=skip_transfer,
            )
            sx1_completed = True

            sx4000_config.run_sx4000_reset_command(nxp, config, "SX2")
            sx4000_config.info("Waiting 20 seconds after SX2 reset/bootstrap.")
            time.sleep(20)
            configure_single_sx_terminal_and_transfer(
                config=config,
                repo_root=repo_root,
                sx_name="SX2",
                serial_config=config.sx2,
                sx_commands=commands.sx2,
                host=config.sx4000_ip.sx2,
                params_file=config.sx4000.sx2_params,
                skip_transfer=skip_transfer,
            )
            sx2_completed = True
        finally:
            if lsbb_prompt_started:
                active_error = sys.exc_info()[0] is not None
                try:
                    sx4000_config.quit_lsbb_utils_prompt(nxp, config)
                    if sx1_completed and sx2_completed:
                        sx4000_config.info("NXP UART: cpld w 0x25 0")
                        sx4000_config.run_command(
                            nxp,
                            "cpld w 0x25 0",
                            sx4000_config.ROOT_SHELL_PATTERN,
                            config.prompt_wait_seconds,
                        )
                        time.sleep(1)
                        sx4000_config.info("NXP UART: cpld w 0x35 0")
                        sx4000_config.run_command(
                            nxp,
                            "cpld w 0x35 0",
                            sx4000_config.ROOT_SHELL_PATTERN,
                            config.prompt_wait_seconds,
                        )
                        sx4000_config.ok("SX4000 deployment completed.")
                except (RuntimeError, TimeoutError) as exc:
                    if not active_error:
                        raise
                    sx4000_config.warn(f"NXP UART: failed to exit LSBB interactive session after error: {exc}")


def run_sx4000_stage(
    config: sx4000_config.Sx4000RuntimeConfig,
    commands: sx4000_config.CommandPlan,
    repo_root: pathlib.Path,
    *,
    skip_switch_config: bool,
    skip_sx_config: bool,
    skip_transfer: bool,
) -> None:
    if not skip_switch_config:
        sx4000_config.configure_switch(config, commands, repo_root)
    else:
        sx4000_config.warn("Skipping switch SONiC configuration.")

    if not skip_sx_config:
        configure_sx4000_sequence_with_windows_scp(config, commands, repo_root, skip_transfer=skip_transfer)
    else:
        sx4000_config.warn("Skipping SX4000 reset, serial configuration, and modem file upload.")

    sx4000_config.ok("SX4000 configuration flow completed.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Independent SX4000 modem deployment/configuration runner.",
    )
    parser.add_argument(
        "--config",
        default="script_setup.yaml",
        help="Path to the YAML configuration file.",
    )
    parser.add_argument(
        "--mode",
        choices=["check", "configure"],
        default="configure",
        help="check validates files and terminal logins; configure also runs commands and uploads files.",
    )
    parser.add_argument(
        "--skip_terminal_check",
        "--skip-terminal-check",
        action="store_true",
        help="Only validate YAML and configured SX4000 files.",
    )
    parser.add_argument(
        "--skip_switch_config",
        "--skip-switch-config",
        action="store_true",
        help="Do not enter SX4000-related SONiC switch commands.",
    )
    parser.add_argument(
        "--skip_sx_config",
        "--skip-sx-config",
        action="store_true",
        help="Do not run the SX1/SX2 reset, serial command, and upload sequence.",
    )
    parser.add_argument(
        "--skip_transfer",
        "--skip-transfer",
        action="store_true",
        help="Run SX4000 commands but skip Windows SCP uploads to the modems.",
    )
    parser.add_argument(
        "--dig_sn",
        "--dig-sn",
        required=True,
        help="DIG board serial number used for the C:\\Logs\\Deployment folder name.",
    )
    return parser


def run(args: argparse.Namespace, repo_root: pathlib.Path, config_path: pathlib.Path) -> int:
    try:
        sx4000_config.info("Starting SX4000 configuration stage.")
        sx_runtime_config = sx4000_config.load_runtime_config(config_path)
        sx4000_config.validate_files(sx_runtime_config)
        commands = sx4000_config.default_sx4000_commands(sx_runtime_config)
        sx4000_config.ok(
            f"Parsed commands: {len(commands.sonic)} switch, {len(commands.sx1)} SX1, {len(commands.sx2)} SX2."
        )

        if args.skip_terminal_check:
            return 0

        if args.mode == "check":
            sx4000_config.check_nxp_and_switch_login(sx_runtime_config, repo_root)
            return 0

        run_sx4000_stage(
            sx_runtime_config,
            commands,
            repo_root,
            skip_switch_config=args.skip_switch_config,
            skip_sx_config=args.skip_sx_config,
            skip_transfer=args.skip_transfer,
        )
        sx4000_config.ok("PASSED: SX4000 configuration completed successfully.")
        return 0
    except (OSError, sx4000_config.ConfigError, ValueError, RuntimeError, TimeoutError) as exc:
        print(f"[FAILED] {exc}", file=sys.stderr)
        return 1


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    repo_root = pathlib.Path(__file__).resolve().parent
    config_path = (repo_root / args.config).resolve()

    sx4000_config.configure_log_context(args.dig_sn)
    log_path = sx4000_config.timestamped_log_path(repo_root, "sx-deploy")
    with log_path.open("a", encoding="utf-8", errors="replace") as log_file:
        tee_stdout = TeeStream(sys.stdout, log_file)
        tee_stderr = TeeStream(sys.stderr, log_file)
        with contextlib.redirect_stdout(tee_stdout), contextlib.redirect_stderr(tee_stderr):
            sx4000_config.info(f"SX deploy log: {log_path}")
            sx4000_config.info(f"DIG SN: {args.dig_sn}")
            return run(args, repo_root, config_path)


if __name__ == "__main__":
    raise SystemExit(main())

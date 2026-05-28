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
from dataclasses import dataclass
from typing import TextIO

import lscriptwin
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


@dataclass(frozen=True)
class DutIpConfig:
    ip: str
    login: str
    password: str
    prompt: str
    tmp_path: str


@dataclass(frozen=True)
class SxPathsConfig:
    sx1_path: pathlib.Path
    sx2_path: pathlib.Path
    startup_file: pathlib.Path
    modem_flash1_path: str
    modem_flash2_path: str
    prompt: str


@dataclass(frozen=True)
class SxVer2Config:
    nxp: sx4000_config.SerialPortConfig
    sx1: sx4000_config.SerialPortConfig
    sx2: sx4000_config.SerialPortConfig
    dut: DutIpConfig
    sx4000: SxPathsConfig
    sx4000_ip: sx4000_config.Sx4000IpConfig
    prompt_wait_seconds: int
    serial_open_seconds: int
    shell_wait_seconds: int
    sx_reset_wait_seconds: int

    @property
    def dut_login(self) -> str:
        return self.dut.login

    @property
    def dut_password(self) -> str:
        return self.dut.password

    @property
    def dut_prompt(self) -> str:
        return self.dut.prompt


def resolve_path(raw_path: str, base_dir: pathlib.Path) -> pathlib.Path:
    path = pathlib.Path(raw_path)
    if path.is_absolute():
        return path
    return base_dir / path


def load_sx_ver2_config(path: pathlib.Path) -> SxVer2Config:
    payload = sx4000_config.load_simple_yaml(path)
    base_dir = path.parent

    try:
        serial_cfg = payload["serial"]
        dut_cfg = payload["dut"]
        server_cfg = payload["server"]
        sx_cfg = payload["sx4000"]
        sx_ip_cfg = payload["sx4000_ip"]
        timeouts_cfg = payload["timeouts"]
    except KeyError as exc:
        raise sx4000_config.ConfigError(f"Missing SX Deployment configuration key: {exc}") from exc

    image_root = pathlib.Path(str(server_cfg["image_path"]))
    startup_raw = str(sx_cfg.get("startup_file", "startup.sh"))
    startup_file = resolve_path(startup_raw, image_root)
    if not startup_file.exists():
        startup_file = resolve_path(startup_raw, base_dir)

    return SxVer2Config(
        nxp=sx4000_config.SerialPortConfig(str(serial_cfg["nxp"]["port"]), int(serial_cfg["nxp"]["baudrate"])),
        sx1=sx4000_config.SerialPortConfig(str(serial_cfg["sx1"]["port"]), int(serial_cfg["sx1"]["baudrate"])),
        sx2=sx4000_config.SerialPortConfig(str(serial_cfg["sx2"]["port"]), int(serial_cfg["sx2"]["baudrate"])),
        dut=DutIpConfig(
            ip=str(dut_cfg["final_ip"]),
            login=str(dut_cfg["login"]),
            password=str(dut_cfg["password"]),
            prompt=str(dut_cfg.get("prompt", "root@ls1046afrwy:")),
            tmp_path=str(dut_cfg.get("tmp_path", "tmp")),
        ),
        sx4000=SxPathsConfig(
            sx1_path=resolve_path(str(sx_cfg["SX1_path"]), base_dir),
            sx2_path=resolve_path(str(sx_cfg["SX2_path"]), base_dir),
            startup_file=startup_file,
            modem_flash1_path=str(sx_cfg.get("modem_flash1_path", "mnt/flash1")),
            modem_flash2_path=str(sx_cfg.get("modem_flash2_path", "mnt/flash2")),
            prompt=str(sx_cfg.get("prompt", "/# ")),
        ),
        sx4000_ip=sx4000_config.Sx4000IpConfig(
            sx1=str(sx_ip_cfg["sx1"]),
            sx2=str(sx_ip_cfg["sx2"]),
            login=str(sx_ip_cfg["login"]),
            password=str(sx_ip_cfg["password"]),
        ),
        prompt_wait_seconds=int(timeouts_cfg.get("prompt_wait_seconds", 15)),
        serial_open_seconds=int(timeouts_cfg.get("serial_open_seconds", 10)),
        shell_wait_seconds=int(timeouts_cfg.get("emergency_boot_seconds", 600)),
        sx_reset_wait_seconds=int(timeouts_cfg.get("sx4000_reset_seconds", 300)),
    )


def validate_sx_ver2_files(config: SxVer2Config) -> None:
    missing: list[str] = []
    for path in (config.sx4000.sx1_path, config.sx4000.sx2_path):
        if not path.is_dir():
            missing.append(str(path))
    if not config.sx4000.startup_file.is_file():
        missing.append(str(config.sx4000.startup_file))
    if missing:
        raise sx4000_config.ConfigError("Missing SX Deployment path(s): " + ", ".join(missing))

    for path in (config.sx4000.sx1_path, config.sx4000.sx2_path):
        if not any(child.is_file() for child in path.iterdir()):
            raise sx4000_config.ConfigError(f"No files found in SX image folder: {path}")
    if config.sx4000.startup_file.suffix.lower() != ".sh":
        raise sx4000_config.ConfigError(f"Expected SH startup file: {config.sx4000.startup_file}")

    sx4000_config.ok("SX Deployment folders and startup file are present.")


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
        temp_dir = pathlib.Path(tempfile.mkdtemp(prefix="sx_ver2_askpass_"))
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
        env.setdefault("DISPLAY", "sx_ver2")

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


def nxp_tmp_root(config: SxVer2Config) -> str:
    return "/" + config.dut.tmp_path.strip().strip("/")


def shell_quote(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"


def staged_file_names(image_dir: pathlib.Path, startup_file: pathlib.Path) -> tuple[list[pathlib.Path], pathlib.Path]:
    files = sorted(path for path in image_dir.iterdir() if path.is_file())
    startup = startup_file
    flash2_files = [path for path in files if path.name != startup.name]
    return flash2_files, startup


def scp_file_to_nxp(config: SxVer2Config, local_file: pathlib.Path, remote_dir: str) -> None:
    remote_spec = f"{config.dut.login}@{config.dut.ip}:{remote_dir.rstrip('/')}/{local_file.name}"
    command = [
        "scp",
        *windows_scp_options(),
        str(local_file),
        remote_spec,
    ]
    sx4000_config.info(f"Windows->NXP SCP {' '.join(command)}")
    run_windows_command_with_password(
        command,
        config.dut.password,
        timeout=300,
        label=f"Windows->NXP SCP {local_file.name}",
    )


def stage_folder_to_nxp(
    config: SxVer2Config,
    nxp: sx4000_config.SerialSession,
    sx_name: str,
    image_dir: pathlib.Path,
    remote_dir: str,
) -> None:
    flash2_files, startup = staged_file_names(image_dir, config.sx4000.startup_file)
    sx4000_config.run_command(
        nxp,
        f"mkdir -p {shell_quote(remote_dir)}",
        sx4000_config.ROOT_SHELL_PATTERN,
        config.prompt_wait_seconds,
        f"Create {sx_name} staging folder on NXP",
    )
    for local_file in [*flash2_files, startup]:
        scp_file_to_nxp(config, local_file, remote_dir)
    sx4000_config.ok(f"{sx_name}: staged {len(flash2_files) + 1} file(s) to NXP {remote_dir}")


def wait_with_tqdm(seconds: int, label: str) -> None:
    try:
        from tqdm import tqdm
    except ImportError as exc:
        raise sx4000_config.ConfigError("tqdm is required for sx_ver2.py. Install it with: python -m pip install tqdm") from exc

    for _ in tqdm(range(seconds), desc=label, unit="s"):
        time.sleep(1)


def run_sx_network_commands(
    config: SxVer2Config,
    repo_root: pathlib.Path,
    sx_name: str,
    serial_config: sx4000_config.SerialPortConfig,
    modem_ip: str,
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
        for command in (
            f"ifconfig nss0 {modem_ip} netmask 255.255.255.0",
            "ping -c1 10.2.4.2",
        ):
            sx4000_config.run_sx_command(sx, command, sx_prompt, config.prompt_wait_seconds)


def run_nxp_scp_to_modem(
    config: SxVer2Config,
    nxp: sx4000_config.SerialSession,
    sx_name: str,
    source_dir: str,
    modem_ip: str,
    image_dir: pathlib.Path,
) -> None:
    flash2_files, startup = staged_file_names(image_dir, config.sx4000.startup_file)
    flash1_path = sx4000_config.modem_remote_path(config.sx4000.modem_flash1_path)
    flash2_path = sx4000_config.modem_remote_path(config.sx4000.modem_flash2_path)

    sx4000_config.run_command_with_optional_password(
        session=nxp,
        command=(
            "scp -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null "
            f"{shell_quote(source_dir + '/' + startup.name)} "
            f"{config.sx4000_ip.login}@{modem_ip}:{shell_quote(flash1_path + '/')}"
        ),
        shell_prompt=sx4000_config.ROOT_SHELL_PATTERN,
        timeout=300,
        password=config.sx4000_ip.password,
    )
    sx4000_config.ok(f"{sx_name}: copied {startup.name} from NXP to {flash1_path}")

    if flash2_files:
        source_files = " ".join(shell_quote(source_dir + "/" + path.name) for path in flash2_files)
        sx4000_config.run_command_with_optional_password(
            session=nxp,
            command=(
                "scp -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null "
                f"{source_files} {config.sx4000_ip.login}@{modem_ip}:{shell_quote(flash2_path + '/')}"
            ),
            shell_prompt=sx4000_config.ROOT_SHELL_PATTERN,
            timeout=600,
            password=config.sx4000_ip.password,
        )
        sx4000_config.ok(f"{sx_name}: copied {len(flash2_files)} file(s) from NXP to {flash2_path}")
    else:
        sx4000_config.warn(f"{sx_name}: no flash2 files found in {image_dir}")


def shutdown_modems(nxp: sx4000_config.SerialSession, config: SxVer2Config) -> None:
    for command in ("cpld w 0x25 0", "cpld w 0x35 0", "cd /root"):
        sx4000_config.info(f"NXP UART: {command}")
        sx4000_config.run_command(
            nxp,
            command,
            sx4000_config.ROOT_SHELL_PATTERN,
            config.prompt_wait_seconds,
        )


def configure_sx_ver2_sequence(config: SxVer2Config, repo_root: pathlib.Path, skip_transfer: bool) -> None:
    nxp_log = sx4000_config.timestamped_log_path(repo_root, "sx-ver2-nxp")
    sx1_stage = f"{nxp_tmp_root(config)}/sx1"
    sx2_stage = f"{nxp_tmp_root(config)}/sx2"
    with sx4000_config.SerialSession("NXP", config.nxp, nxp_log, config.serial_open_seconds) as nxp:
        sx4000_config.info(f"NXP SX Deployment log: {nxp_log}")
        sx4000_config.ensure_nxp_shell(nxp, config)  # type: ignore[arg-type]

        if not skip_transfer:
            stage_folder_to_nxp(config, nxp, "SX1", config.sx4000.sx1_path, sx1_stage)
            stage_folder_to_nxp(config, nxp, "SX2", config.sx4000.sx2_path, sx2_stage)
        else:
            sx4000_config.warn("Skipping Windows->NXP staging.")

        lsbb_prompt_started = False
        shutdown_done = False
        try:
            sx4000_config.start_lsbb_utils_prompt(nxp, config)  # type: ignore[arg-type]
            lsbb_prompt_started = True

            sx4000_config.run_sx4000_reset_command(nxp, config, "SX1")  # type: ignore[arg-type]
            sx4000_config.run_sx4000_reset_command(nxp, config, "SX2")  # type: ignore[arg-type]
            sx4000_config.quit_lsbb_utils_prompt(nxp, config)  # type: ignore[arg-type]
            lsbb_prompt_started = False

            wait_with_tqdm(20, "SX boot wait")

            run_sx_network_commands(config, repo_root, "SX1", config.sx1, config.sx4000_ip.sx1)
            if not skip_transfer:
                run_nxp_scp_to_modem(config, nxp, "SX1", sx1_stage, config.sx4000_ip.sx1, config.sx4000.sx1_path)

            run_sx_network_commands(config, repo_root, "SX2", config.sx2, config.sx4000_ip.sx2)
            if not skip_transfer:
                run_nxp_scp_to_modem(config, nxp, "SX2", sx2_stage, config.sx4000_ip.sx2, config.sx4000.sx2_path)

            shutdown_modems(nxp, config)
            shutdown_done = True
        finally:
            active_error = sys.exc_info()[0] is not None
            try:
                if lsbb_prompt_started:
                    sx4000_config.quit_lsbb_utils_prompt(nxp, config)  # type: ignore[arg-type]
                    lsbb_prompt_started = False
                if not shutdown_done:
                    shutdown_modems(nxp, config)
            except (RuntimeError, TimeoutError) as exc:
                if not active_error:
                    raise
                sx4000_config.warn(f"NXP UART: failed during SX ver2 cleanup after error: {exc}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="SX4000 deploy2 runner using script_setup.yaml.")
    parser.add_argument("--config", default="script_setup.yaml", help="Path to the YAML configuration file.")
    parser.add_argument(
        "--mode",
        choices=["check", "configure"],
        default="configure",
        help="check validates files and NXP login; configure stages, boots, copies, and shuts down modems.",
    )
    parser.add_argument(
        "--skip_terminal_check",
        "--skip-terminal-check",
        action="store_true",
        help="Only validate YAML and configured SX folders.",
    )
    parser.add_argument(
        "--skip_transfer",
        "--skip-transfer",
        action="store_true",
        help="Run boot and ping flow but skip all SCP transfers.",
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
        sx4000_config.info("Starting SX Deployment ver2 stage.")
        config = load_sx_ver2_config(config_path)
        validate_sx_ver2_files(config)

        if args.skip_terminal_check:
            return 0

        if args.mode == "check":
            nxp_log = sx4000_config.timestamped_log_path(repo_root, "sx-ver2-nxp-check")
            with sx4000_config.SerialSession("NXP", config.nxp, nxp_log, config.serial_open_seconds) as nxp:
                sx4000_config.info(f"NXP check log: {nxp_log}")
                sx4000_config.ensure_nxp_shell(nxp, config)  # type: ignore[arg-type]
                sx4000_config.ok("NXP terminal is logged in.")
            return 0

        configure_sx_ver2_sequence(config, repo_root, skip_transfer=args.skip_transfer)
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
    lscriptwin.LOG_RUN_TIMESTAMP = f"{lscriptwin.LOG_RUN_TIMESTAMP}_sx_deploy"
    log_path = sx4000_config.timestamped_log_path(repo_root, "sx-ver2")
    with log_path.open("a", encoding="utf-8", errors="replace") as log_file:
        tee_stdout = TeeStream(sys.stdout, log_file)
        tee_stderr = TeeStream(sys.stderr, log_file)
        with contextlib.redirect_stdout(tee_stdout), contextlib.redirect_stderr(tee_stderr):
            sx4000_config.info(f"SX ver2 log: {log_path}")
            sx4000_config.info(f"DIG SN: {args.dig_sn}")
            return run(args, repo_root, config_path)


if __name__ == "__main__":
    raise SystemExit(main())

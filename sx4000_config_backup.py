from __future__ import annotations

import argparse
import pathlib
import re
import sys
from dataclasses import dataclass

from lscriptwin import (
    ConfigError,
    LOGIN_PATTERN,
    NXP_DISTRO_BANNER_PATTERN,
    PASSWORD_PATTERN,
    ROOT_SHELL_PATTERN,
    SerialPortConfig,
    SerialSession,
    configure_log_context,
    ensure_logs_dir,
    load_simple_yaml,
    ok,
    run_command,
    run_command_with_optional_password,
    timestamped_log_path,
    warn,
    info,
)

SX_PING_REPLY_PATTERN = re.compile(r"64 bytes from\s+\d+\.\d+\.\d+\.\d+:", re.IGNORECASE)
SX_PING_ZERO_LOSS_PATTERN = re.compile(r"\b0%\s+packet loss\b", re.IGNORECASE)
SX4000_RESET_ERROR_PATTERN = re.compile(r"SX[12]\s+-\s+(?:Reset Failure|Bootstrap Override Failed)", re.IGNORECASE)
SX4000_TARGET_READY_PATTERN = re.compile(r"(?:In \[\d+\]:|>>>|>>)", re.MULTILINE)


@dataclass(frozen=True)
class SonicConfig:
    login: str
    password: str
    prompt: str


@dataclass(frozen=True)
class Sx4000Config:
    json_files: tuple[pathlib.Path, ...]
    sx1_params: pathlib.Path
    sx2_params: pathlib.Path
    startup_file: pathlib.Path
    modem_flash1_path: str
    modem_flash2_path: str
    prompt: str


@dataclass(frozen=True)
class Sx4000IpConfig:
    sx1: str
    sx2: str
    login: str
    password: str


@dataclass(frozen=True)
class Sx4000RuntimeConfig:
    nxp: SerialPortConfig
    switch: SerialPortConfig
    sx1: SerialPortConfig
    sx2: SerialPortConfig
    dut_login: str
    dut_password: str
    dut_prompt: str
    sonic: SonicConfig
    sx4000: Sx4000Config
    sx4000_ip: Sx4000IpConfig
    prompt_wait_seconds: int
    serial_open_seconds: int
    shell_wait_seconds: int
    sx_reset_wait_seconds: int


@dataclass(frozen=True)
class CommandPlan:
    sonic: tuple[str, ...]
    sx1: tuple[str, ...]
    sx2: tuple[str, ...]


def resolve_path(raw_path: str, base_dir: pathlib.Path) -> pathlib.Path:
    path = pathlib.Path(raw_path)
    if path.is_absolute():
        return path
    return base_dir / path


def load_runtime_config(path: pathlib.Path) -> Sx4000RuntimeConfig:
    payload = load_simple_yaml(path)
    base_dir = path.parent

    try:
        serial_cfg = payload["serial"]
        dut_cfg = payload["dut"]
        sonic_cfg = payload["sonic"]
        sx_cfg = payload["sx4000"]
        sx_ip_cfg = payload["sx4000_ip"]
        timeouts_cfg = payload["timeouts"]
    except KeyError as exc:
        raise ConfigError(f"Missing SX4000 configuration key: {exc}") from exc

    json_files = tuple(
        resolve_path(str(sx_cfg[key]), base_dir)
        for key in sorted(k for k in sx_cfg if k.startswith("file"))
    )
    if not json_files:
        raise ConfigError("sx4000.file* entries are required for JSON staging.")

    startup_raw = str(sx_cfg.get("startup_file", "startup.sh"))
    startup_path = resolve_path(startup_raw, pathlib.Path(str(payload["server"]["image_path"])))
    if not startup_path.exists():
        startup_path = resolve_path(startup_raw, base_dir)

    return Sx4000RuntimeConfig(
        nxp=SerialPortConfig(str(serial_cfg["nxp"]["port"]), int(serial_cfg["nxp"]["baudrate"])),
        switch=SerialPortConfig(str(serial_cfg["switch"]["port"]), int(serial_cfg["switch"]["baudrate"])),
        sx1=SerialPortConfig(str(serial_cfg["sx1"]["port"]), int(serial_cfg["sx1"]["baudrate"])),
        sx2=SerialPortConfig(str(serial_cfg["sx2"]["port"]), int(serial_cfg["sx2"]["baudrate"])),
        dut_login=str(dut_cfg["login"]),
        dut_password=str(dut_cfg["password"]),
        dut_prompt=str(dut_cfg.get("prompt", "ls1046afrwy login:")),
        sonic=SonicConfig(
            login=str(sonic_cfg["login"]),
            password=str(sonic_cfg["password"]),
            prompt=str(sonic_cfg.get("prompt", "admin@sonic:~$")),
        ),
        sx4000=Sx4000Config(
            json_files=json_files,
            sx1_params=resolve_path(str(sx_cfg["sx1_params"]), base_dir),
            sx2_params=resolve_path(str(sx_cfg["sx2_params"]), base_dir),
            startup_file=startup_path,
            modem_flash1_path=str(sx_cfg.get("modem_flash1_path", sx_cfg.get("modem_path", "mnt/flash1"))),
            modem_flash2_path=str(sx_cfg.get("modem_flash2_path", "mnt/flash2")),
            prompt=str(sx_cfg.get("prompt", "/# ")),
        ),
        sx4000_ip=Sx4000IpConfig(
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


def sx_prompt_pattern(prompt_text: str) -> re.Pattern[str]:
    # SX consoles can emit the shell prompt and then immediately append driver logs
    # on the same line, and some serial paths use '\r' without '\n', so accept
    # the prompt token after either line break without requiring end-of-line.
    return re.compile(r"(?:^|[\r\n])\s*" + re.escape(prompt_text.strip()) + r"(?=\s|$)", re.MULTILINE)


def shell_prompt_pattern(prompt_text: str) -> re.Pattern[str]:
    return re.compile(r"(?:^|\n).{0,120}" + re.escape(prompt_text.strip()) + r"\s*$", re.MULTILINE)


def login_prompt_pattern(prompt_text: str) -> re.Pattern[str]:
    prompt = prompt_text.strip()
    if not prompt:
        return LOGIN_PATTERN
    return re.compile(r"(?:^|[\r\n])\s*" + re.escape(prompt) + r"\s*$", re.IGNORECASE | re.MULTILINE)


def default_sx4000_commands(config: Sx4000RuntimeConfig) -> CommandPlan:
    switch_ip = "10.10.10.15"
    sonic = (
        "sudo config acl rem table CP_IN",
        "sudo config acl rem table CP_OUT",
        "sudo config interface speed Ethernet10 10000",
        "sudo config interface speed Ethernet11 10000",
        f"sudo config interface ip add Vlan100 {switch_ip}/24",
    )
    sx_common = (
        "ifconfig lan2 down",
        "ifconfig lan0 up",
        "ifconfig lan1 up",
    )
    sx1 = (
        *sx_common,
        f"ifconfig nss0 {config.sx4000_ip.sx1} netmask 255.255.255.0",
        f"ping -c1 {switch_ip}",
    )
    sx2 = (
        *sx_common,
        f"ifconfig nss0 {config.sx4000_ip.sx2} netmask 255.255.255.0",
        f"ping -c1 {switch_ip}",
    )
    return CommandPlan(sonic=sonic, sx1=sx1, sx2=sx2)


def validate_files(config: Sx4000RuntimeConfig) -> None:
    required = [
        *config.sx4000.json_files,
        config.sx4000.sx1_params,
        config.sx4000.sx2_params,
        config.sx4000.startup_file,
    ]
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        raise ConfigError("Missing SX4000 file(s): " + ", ".join(missing))

    for path in config.sx4000.json_files:
        if path.suffix.lower() != ".json":
            raise ConfigError(f"Expected JSON file from sx4000.file*: {path}")
    for path in (config.sx4000.sx1_params, config.sx4000.sx2_params):
        if path.suffix.lower() != ".txt":
            raise ConfigError(f"Expected TXT modem parameter file: {path}")
    if config.sx4000.startup_file.suffix.lower() != ".sh":
        raise ConfigError(f"Expected SH startup file: {config.sx4000.startup_file}")

    ok("SX4000 JSON, TXT, and SH files are present.")


def ensure_sx_shell(session: SerialSession, username: str, password: str, prompt: re.Pattern[str], timeout: int) -> None:
    start_pos = len(session.buffer)
    session.send_line("")
    result = session.wait_for_any_pattern(
        {
            "shell": prompt,
            "login": LOGIN_PATTERN,
        },
        timeout=timeout,
        label="SX4000 shell or login",
        start_pos=start_pos,
    )
    if result == "login":
        start_pos = len(session.buffer)
        session.send_line(username)
        session.wait_for_pattern(PASSWORD_PATTERN, timeout=30, label="SX4000 password prompt", start_pos=start_pos)
        start_pos = len(session.buffer)
        session.send_line(password)
        session.wait_for_pattern(prompt, timeout=timeout, label="SX4000 shell", start_pos=start_pos)


def run_sx_command(session: SerialSession, command: str, prompt: re.Pattern[str], timeout: int) -> None:
    if command.startswith("ping "):
        command = normalize_ping_command(command)
    info(f"{session.name}: {command}")
    if command.startswith("ping "):
        start_pos = len(session.buffer)
        session.send_line(command)
        session.wait_for_pattern(prompt, timeout=timeout, label=f"SX4000 prompt after {command}", start_pos=start_pos)
        output = session.buffer[start_pos:]
        if not SX_PING_REPLY_PATTERN.search(output) or not SX_PING_ZERO_LOSS_PATTERN.search(output):
            raise RuntimeError(f"{session.name}: ping verification failed for {command}. Output:\n{output.strip()}")
        return
    run_command(session, command, prompt, timeout)


def normalize_ping_command(command: str) -> str:
    parts = command.split()
    if "-c" in parts or any(part.startswith("-c") for part in parts):
        return command
    if len(parts) == 2:
        return f"ping -c1 {parts[1]}"
    return command


def ensure_switch_sonic_shell(session: SerialSession, config: Sx4000RuntimeConfig, shell_prompt: re.Pattern[str]) -> None:
    start_pos = len(session.buffer)
    session.send_line("")
    session.send_line("")
    result = session.wait_for_any_pattern(
        {
            "shell": shell_prompt,
            "login": LOGIN_PATTERN,
        },
        timeout=config.prompt_wait_seconds,
        label="Switch SONiC shell or login",
        start_pos=start_pos,
    )
    if result == "shell":
        return

    start_pos = len(session.buffer)
    session.send_line(config.sonic.login)
    session.wait_for_pattern(PASSWORD_PATTERN, timeout=30, label="Switch SONiC password prompt", start_pos=start_pos)
    start_pos = len(session.buffer)
    session.send_line(config.sonic.password)
    session.wait_for_pattern(shell_prompt, timeout=config.shell_wait_seconds, label="Switch SONiC shell", start_pos=start_pos)


def ensure_nxp_shell(session: SerialSession, config: Sx4000RuntimeConfig) -> None:
    login_prompt = login_prompt_pattern(config.dut_prompt)
    prompt_candidates = {
        "login": login_prompt,
        "login_generic": LOGIN_PATTERN,
    }
    start_pos = len(session.buffer)
    session.send_line("")
    result = session.wait_for_any_pattern(
        {
            "shell": ROOT_SHELL_PATTERN,
            **prompt_candidates,
            "distro_banner": NXP_DISTRO_BANNER_PATTERN,
        },
        timeout=config.prompt_wait_seconds,
        label="NXP shell, login, or distro banner",
        start_pos=start_pos,
    )
    if result == "shell":
        return

    login_ready = False
    if result == "distro_banner":
        session.log_event("INFO", "NXP distro banner detected; sending Enter twice before login prompt check.")
        start_pos = len(session.buffer)
        session.send_line("")
        session.send_line("")
        session.wait_for_any_pattern(
            prompt_candidates,
            timeout=30,
            label="NXP login prompt after distro banner",
            start_pos=start_pos,
        )
        login_ready = True

    session.log_event("INFO", "NXP login prompt detected; sending Enter twice before credentials.")
    if not login_ready:
        start_pos = len(session.buffer)
        session.send_line("")
        session.send_line("")
        session.wait_for_any_pattern(
            prompt_candidates,
            timeout=30,
            label="NXP login prompt after Enter twice",
            start_pos=start_pos,
        )
    start_pos = len(session.buffer)
    session.send_line(config.dut_login)
    session.wait_for_pattern(PASSWORD_PATTERN, timeout=30, label="NXP password prompt", start_pos=start_pos)
    start_pos = len(session.buffer)
    session.send_line(config.dut_password)
    session.wait_for_pattern(ROOT_SHELL_PATTERN, timeout=config.shell_wait_seconds, label="NXP root shell", start_pos=start_pos)


def run_sonic_commands(session: SerialSession, config: Sx4000RuntimeConfig, commands: tuple[str, ...]) -> None:
    prompt = shell_prompt_pattern(config.sonic.prompt)
    ensure_switch_sonic_shell(session, config, prompt)
    for command in commands:
        info(f"Switch: {command}")
        run_command_with_optional_password(
            session=session,
            command=command,
            shell_prompt=prompt,
            timeout=config.prompt_wait_seconds,
            password=config.sonic.password,
        )


def require_paramiko():
    try:
        import paramiko
    except ImportError as exc:
        raise ConfigError("Paramiko is required for SX4000 uploads. Install it with: python -m pip install paramiko") from exc
    return paramiko


def open_ssh_client(host: str, username: str, password: str, timeout: int = 30):
    paramiko = require_paramiko()
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        hostname=host,
        username=username,
        password=password,
        timeout=timeout,
        banner_timeout=timeout,
        auth_timeout=timeout,
        look_for_keys=False,
        allow_agent=False,
    )
    return client


def modem_remote_path(raw_path: str) -> str:
    return "/" + raw_path.strip().strip("/")


def ensure_remote_dir(sftp, remote_dir: str) -> None:
    parts = [part for part in remote_dir.strip("/").split("/") if part]
    current = ""
    for part in parts:
        current += "/" + part
        try:
            sftp.stat(current)
        except OSError:
            sftp.mkdir(current)


def sftp_upload_files(client, sx_name: str, files: list[pathlib.Path], remote_dir: str) -> None:
    info(f"{sx_name}: uploading {len(files)} file(s) to {remote_dir} with Paramiko SFTP")
    with client.open_sftp() as sftp:
        ensure_remote_dir(sftp, remote_dir)
        for local_file in files:
            remote_file = remote_dir.rstrip("/") + "/" + local_file.name
            sftp.put(str(local_file), remote_file)
            remote_size = sftp.stat(remote_file).st_size
            local_size = local_file.stat().st_size
            if remote_size != local_size:
                raise RuntimeError(
                    f"{sx_name}: copied file size mismatch for {local_file.name}: "
                    f"local={local_size} bytes, remote={remote_size} bytes"
                )
            ok(f"{sx_name}: uploaded and verified {local_file.name} ({remote_size} bytes)")


def upload_single_modem_files(config: Sx4000RuntimeConfig, sx_name: str, ip: str, params_file: pathlib.Path) -> None:
    flash1_path = modem_remote_path(config.sx4000.modem_flash1_path)
    flash2_path = modem_remote_path(config.sx4000.modem_flash2_path)
    json_files = list(config.sx4000.json_files)

    client = open_ssh_client(
        host=ip,
        username=config.sx4000_ip.login,
        password=config.sx4000_ip.password,
        timeout=30,
    )
    try:
        sftp_upload_files(client, sx_name, [config.sx4000.startup_file], flash1_path)
        sftp_upload_files(client, sx_name, [*json_files, params_file], flash2_path)
    finally:
        client.close()


def start_lsbb_utils_prompt(nxp: SerialSession, config: Sx4000RuntimeConfig) -> None:
    ensure_nxp_shell(nxp, config)
    command = "cd /root/LSBB_Utils && sh ./run.sh"
    info(f"NXP UART: starting LSBB interactive session in default TARGET mode: {command}")
    start_pos = len(nxp.buffer)
    nxp.send_line(command)
    nxp.wait_for_pattern(
        SX4000_TARGET_READY_PATTERN,
        timeout=config.sx_reset_wait_seconds,
        label="LSBB interactive prompt",
        start_pos=start_pos,
    )
    info("NXP UART: sending Enter twice immediately after prompt detection.")
    nxp.send_line("")
    nxp.send_line("")
    ok("NXP UART LSBB interactive session is ready.")


def run_sx4000_reset_command(nxp: SerialSession, config: Sx4000RuntimeConfig, sx_id: str) -> None:
    command = f'sx4000_ctrl.sx4000_reset_and_bootstrap_ov("{sx_id}")'
    info(f"NXP UART: {command}")
    start_pos = len(nxp.buffer)
    nxp.send_line(command)
    success_pattern = re.compile(
        rf"{re.escape(sx_id)}\s+-\s+Reset Done,\s+Reset State=(?:RST_DONE|RST_CE)|"
        rf"{re.escape(sx_id)}\s+-\s+Bootstrap Override completed Successfully,\s+Reset State=RST_CE",
        re.IGNORECASE,
    )
    nxp.wait_for_pattern(
        success_pattern,
        config.sx_reset_wait_seconds,
        label=f"{sx_id} reset/bootstrap success message",
        start_pos=start_pos,
    )
    nxp.wait_for_pattern(
        SX4000_TARGET_READY_PATTERN,
        config.sx_reset_wait_seconds,
        label=f"LSBB prompt after {sx_id} reset/bootstrap",
        start_pos=start_pos,
    )
    output = nxp.buffer[start_pos:]
    error = SX4000_RESET_ERROR_PATTERN.search(output)
    if error:
        raise RuntimeError(f"{sx_id} reset/bootstrap failed. Output:\n{output.strip()}")
    if not success_pattern.search(output):
        raise RuntimeError(f"{sx_id} reset/bootstrap did not report success. Output:\n{output.strip()}")
    ok(f"{sx_id} reset/bootstrap command completed.")


def quit_lsbb_utils_prompt(nxp: SerialSession, config: Sx4000RuntimeConfig) -> None:
    info("NXP UART: quitting LSBB interactive session.")
    start_pos = len(nxp.buffer)
    nxp.send_line("quit()")
    nxp.wait_for_pattern(
        ROOT_SHELL_PATTERN,
        timeout=config.prompt_wait_seconds,
        label="NXP shell after quit()",
        start_pos=start_pos,
    )
    ok("Returned to NXP UART shell.")


def check_nxp_and_switch_login(config: Sx4000RuntimeConfig, repo_root: pathlib.Path) -> None:
    nxp_log = timestamped_log_path(repo_root, "sx4000-nxp")
    switch_log = timestamped_log_path(repo_root, "sx4000-switch")
    with SerialSession("NXP", config.nxp, nxp_log, config.serial_open_seconds) as nxp, SerialSession(
        "Switch",
        config.switch,
        switch_log,
        config.serial_open_seconds,
    ) as switch:
        info(f"NXP log: {nxp_log}")
        info(f"Switch log: {switch_log}")
        ensure_nxp_shell(nxp, config)
        ok("NXP terminal is logged in.")
        run_sonic_commands(switch, config, ())
        ok("Switch terminal is logged in.")


def configure_single_sx_terminal(
    config: Sx4000RuntimeConfig,
    repo_root: pathlib.Path,
    sx_name: str,
    serial_config: SerialPortConfig,
    sx_commands: tuple[str, ...],
) -> None:
    sx_prompt = sx_prompt_pattern(config.sx4000.prompt)
    sx_log = timestamped_log_path(repo_root, sx_name.lower())
    with SerialSession(sx_name, serial_config, sx_log, config.serial_open_seconds) as sx:
        info(f"{sx_name} log: {sx_log}")
        ensure_sx_shell(sx, config.sx4000_ip.login, config.sx4000_ip.password, sx_prompt, config.shell_wait_seconds)
        ok(f"{sx_name} serial terminal is logged in.")
        for command in sx_commands:
            run_sx_command(sx, command, sx_prompt, config.prompt_wait_seconds)


def configure_sx4000_sequence(config: Sx4000RuntimeConfig, commands: CommandPlan, repo_root: pathlib.Path, skip_transfer: bool) -> None:
    info("SX4000 sequence: start run.sh over NXP UART, reset SX1, configure SX1, reset SX2, configure SX2, then quit().")
    nxp_log = timestamped_log_path(repo_root, "sx4000-nxp")
    with SerialSession("NXP", config.nxp, nxp_log, config.serial_open_seconds) as nxp:
        info(f"NXP SX4000 log: {nxp_log}")
        start_lsbb_utils_prompt(nxp, config)

        run_sx4000_reset_command(nxp, config, "SX1")
        configure_single_sx_terminal(
            config=config,
            repo_root=repo_root,
            sx_name="SX1",
            serial_config=config.sx1,
            sx_commands=commands.sx1,
        )
        if not skip_transfer:
            upload_single_modem_files(config, "SX1", config.sx4000_ip.sx1, config.sx4000.sx1_params)
        else:
            warn("Skipping SX1 file upload.")

        run_sx4000_reset_command(nxp, config, "SX2")
        configure_single_sx_terminal(
            config=config,
            repo_root=repo_root,
            sx_name="SX2",
            serial_config=config.sx2,
            sx_commands=commands.sx2,
        )
        if not skip_transfer:
            upload_single_modem_files(config, "SX2", config.sx4000_ip.sx2, config.sx4000.sx2_params)
        else:
            warn("Skipping SX2 file upload.")

        quit_lsbb_utils_prompt(nxp, config)


def configure_switch(config: Sx4000RuntimeConfig, commands: CommandPlan, repo_root: pathlib.Path) -> None:
    switch_log = timestamped_log_path(repo_root, "sx4000-switch-config")
    with SerialSession("Switch", config.switch, switch_log, config.serial_open_seconds) as switch:
        info(f"Switch config log: {switch_log}")
        run_sonic_commands(switch, config, commands.sonic)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Configure SX4000 modems from YAML.")
    parser.add_argument("--config", default="script_setup.yaml", help="Path to YAML configuration.")
    parser.add_argument(
        "--mode",
        choices=["check", "configure"],
        default="check",
        help="check validates files and terminal logins; configure also runs commands and uploads files.",
    )
    parser.add_argument("--skip_terminal_check", "--skip-terminal-check", action="store_true", help="Only validate YAML and C:\\Images files.")
    parser.add_argument("--skip_switch_config", "--skip-switch-config", action="store_true", help="Do not enter SONiC commands.")
    parser.add_argument("--skip_sx_config", "--skip-sx-config", action="store_true", help="Do not run the SX1-then-SX2 reset and serial command sequence.")
    parser.add_argument("--skip_transfer", "--skip-transfer", action="store_true", help="Do not upload files to the SX4000 modems.")
    parser.add_argument("--dig_sn", "--dig-sn", help="DIG board serial number used for the C:\\Logs\\Deployment folder name.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    configure_log_context(args.dig_sn)
    repo_root = pathlib.Path(__file__).resolve().parent
    config_path = (repo_root / args.config).resolve()
    ensure_logs_dir(repo_root)

    try:
        config = load_runtime_config(config_path)
        validate_files(config)
        commands = default_sx4000_commands(config)
        ok(f"Parsed commands: {len(commands.sonic)} switch, {len(commands.sx1)} SX1, {len(commands.sx2)} SX2.")

        if args.skip_terminal_check:
            return 0

        if args.mode == "check":
            check_nxp_and_switch_login(config, repo_root)
            return 0

        if not args.skip_switch_config:
            configure_switch(config, commands, repo_root)
        else:
            warn("Skipping switch SONiC configuration.")

        if not args.skip_sx_config:
            configure_sx4000_sequence(config, commands, repo_root, skip_transfer=args.skip_transfer)
        else:
            warn("Skipping SX4000 reset, serial configuration, and modem file upload.")

        ok("SX4000 configuration flow completed.")
        return 0
    except (OSError, ConfigError, RuntimeError) as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

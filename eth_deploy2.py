from __future__ import annotations

import argparse
import datetime as dt
import getpass
import os
import pathlib
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from typing import Any

import serial

try:
    from tqdm import tqdm
except ImportError:
    tqdm = None


ACTIVE_SWITCH_LOG_FILE: Any | None = None


AUTOBOOT_PATTERN = re.compile(r"Hit any key to stop autoboot:", re.IGNORECASE)
ANSI_ESCAPE_PATTERN = re.compile(r"\x1B\[[0-?]*[ -/]*[@-~]")
PASSWORD_PATTERN = re.compile(r"(?:^|[\r\n]).{0,80}password(?: for [^:]+)?:\s*$", re.IGNORECASE | re.MULTILINE)
LOGIN_PATTERN = re.compile(r"(?:^|[\r\n]).{0,80}login:\s*$", re.IGNORECASE | re.MULTILINE)
ROOT_SHELL_PATTERN = re.compile(r"(?:^|[\r\n]).{0,120}#\s*$", re.MULTILINE)
DIG_SN_PATTERN = re.compile(r"^(?:CLS|MLS)DM-\d{2}-\d{4}-(?:\d{6}|[A-Z]\d)-\d{3,5}$")
SQL_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
SWITCH_ENV_RESET_PATTERN = re.compile(r"## Resetting to default environment", re.IGNORECASE)
SWITCH_SAVEENV_OK_PATTERN = re.compile(r"(^|\n)OK\s*$", re.IGNORECASE | re.MULTILINE)
SWITCH_PING_FAILED_PATTERN = re.compile(
    r"(?:ARP Retry count exceeded|ping failed; host .* is not alive)",
    re.IGNORECASE,
)
SWITCH_PING_ALIVE_PATTERN = re.compile(r"host\s+\S+\s+is\s+alive", re.IGNORECASE)
SWITCH_TFTP_ERROR_PATTERN = re.compile(r"TFTP error:|Not retrying", re.IGNORECASE)
SWITCH_BOOTM_ERROR_PATTERN = re.compile(r"(?:Wrong Image Format for bootm command|ERROR:\s*can't get kernel image!)", re.IGNORECASE)
SWITCH_REBOOT_REQUEST_PATTERN = re.compile(
    r"(?:The system is going down NOW!|Sent SIGTERM to all processes|Sent SIGKILL to all processes|Requesting system reboot|reboot:\s*Restarting system)",
    re.IGNORECASE,
)
LOG_ROOT = pathlib.Path(r"C:\Logs\Deployment")
COMMAND_DELAY_SECONDS = 2
SONIC_COMMAND_DELAY_SECONDS = 10
SWITCH_SONIC_BOOT_WAIT_SECONDS = 330
SONIC_PING_REPLY_PATTERN = re.compile(r"64 bytes from\s+192\.168\.2\.1:", re.IGNORECASE)
SONIC_PING_SUMMARY_PATTERN = re.compile(r"\d+\s+packets transmitted,\s+\d+\s+received,\s+0% packet loss", re.IGNORECASE)
SW_INITIALIZE_READY_PATTERN = re.compile(r"System\s+is\s+ready\.", re.IGNORECASE)

SUPPORTED_DIG_SN_EXAMPLES = (
    "CLSDM-09-0926-260528-002",
    "MLSDM-08-0726-B1-00010",
)

NXP_UART_COMMANDS = (
    "nmcli con add type ethernet ifname fm1-mac10 con-name fm1-mac10-static "
    "ipv4.addresses 192.168.2.1/24 ipv4.method manual",
    "nmcli con up fm1-mac10-static",
    "nmcli con show",
)

NXP_IMAGE_SERVER_COMMANDS = (
    "cd /tmp",
    "ps -ef | grep udpsvd",
    "ps -ef | grep httpserv",
    "busybox udpsvd -vE 0.0.0.0 69 tftpd . &",
    "ps -ef | grep udpsvd",
    "httpserv -p 80 &",
    "cd /root",
)

SONIC_CONFIG_COMMANDS = (
    "sudo sonic-cfggen -w -j /usr/share/sonic/device/arm64-telesat_lsbb-r0/telesat-lsbb/default_config.json",
    "sudo config qos reload",
    "sudo config interface ip add eth0 192.168.2.2/24",
    "sudo config save -y",
)


class ConfigError(RuntimeError):
    pass


def info(message: str) -> None:
    print(f"[INFO] {message}", flush=True)
    write_active_switch_log(f"\n[INFO] {message}\n")


def ok(message: str) -> None:
    print(f"[OK] {message}", flush=True)
    write_active_switch_log(f"\n[OK] {message}\n")


def write_active_switch_log(text: str) -> None:
    if ACTIVE_SWITCH_LOG_FILE is None:
        return
    ACTIVE_SWITCH_LOG_FILE.write(text.encode("utf-8", errors="replace"))
    ACTIVE_SWITCH_LOG_FILE.flush()


def parse_scalar(raw: str) -> Any:
    value = raw.strip()
    if not value:
        return ""
    if value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1]
    lowered = value.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        return value


def load_simple_yaml(path: pathlib.Path) -> dict[str, Any]:
    root: dict[str, Any] = {}
    stack: list[tuple[int, dict[str, Any]]] = [(-1, root)]

    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue

        indent = len(raw_line) - len(raw_line.lstrip(" "))
        if indent % 2 != 0:
            raise ConfigError(f"Unsupported indentation at line {line_number}: {raw_line!r}")

        while len(stack) > 1 and indent <= stack[-1][0]:
            stack.pop()

        current = stack[-1][1]
        if ":" not in stripped:
            raise ConfigError(f"Expected key/value mapping at line {line_number}: {raw_line!r}")

        key, _, remainder = stripped.partition(":")
        key = key.strip()
        remainder = remainder.strip()
        if not key:
            raise ConfigError(f"Missing key at line {line_number}")

        if remainder == "":
            nested: dict[str, Any] = {}
            current[key] = nested
            stack.append((indent, nested))
            continue

        current[key] = parse_scalar(remainder)

    return root


@dataclass(frozen=True)
class SerialPortConfig:
    port: str
    baudrate: int


@dataclass(frozen=True)
class DutConfig:
    final_ip: str
    login: str
    password: str
    tmp_path: str
    login_prompt: str
    prompt: str


@dataclass(frozen=True)
class ServerConfig:
    image_path: str


@dataclass(frozen=True)
class SwitchConfig:
    image_file: str


@dataclass(frozen=True)
class SonicConfig:
    image_file: str
    login: str
    password: str
    login_prompt: str
    prompt: str


@dataclass(frozen=True)
class PromptConfig:
    switch: str


@dataclass(frozen=True)
class TimeoutConfig:
    serial_open_seconds: int
    prompt_wait_seconds: int
    boot_interrupt_seconds: int
    uboot_boot_seconds: int


@dataclass(frozen=True)
class DbConfig:
    db_type: str
    path: str
    table: str
    serial_column: str
    mac_column_format: str
    mac_count: int
    seed_mac: str
    auto_create: bool


@dataclass(frozen=True)
class AppConfig:
    nxp_serial: SerialPortConfig
    switch_serial: SerialPortConfig
    dut: DutConfig
    server: ServerConfig
    switch: SwitchConfig
    sonic: SonicConfig
    prompts: PromptConfig
    timeouts: TimeoutConfig
    db: DbConfig

    @classmethod
    def from_mapping(cls, payload: dict[str, Any]) -> AppConfig:
        serial_cfg = payload["serial"]
        dut_cfg = payload["dut"]
        server_cfg = payload["server"]
        switch_cfg = payload["switch"]
        sonic_cfg = payload["sonic"]
        prompts_cfg = payload["prompts"]
        timeouts_cfg = payload["timeouts"]
        db_cfg = payload["db"]
        return cls(
            nxp_serial=SerialPortConfig(
                port=str(serial_cfg["nxp"]["port"]),
                baudrate=int(serial_cfg["nxp"]["baudrate"]),
            ),
            switch_serial=SerialPortConfig(
                port=str(serial_cfg["switch"]["port"]),
                baudrate=int(serial_cfg["switch"]["baudrate"]),
            ),
            dut=DutConfig(
                final_ip=str(dut_cfg["final_ip"]),
                login=str(dut_cfg["login"]),
                password=str(dut_cfg["password"]),
                tmp_path=str(dut_cfg.get("tmp_path", "tmp")),
                login_prompt=str(dut_cfg.get("login_prompt", "login: ")),
                prompt=str(dut_cfg.get("prompt", "#")),
            ),
            server=ServerConfig(
                image_path=str(server_cfg["image_path"]),
            ),
            switch=SwitchConfig(
                image_file=str(switch_cfg["image_file"]),
            ),
            sonic=SonicConfig(
                image_file=str(sonic_cfg["image_file"]),
                login=str(sonic_cfg["login"]),
                password=str(sonic_cfg["password"]),
                login_prompt=str(sonic_cfg.get("login_prompt", "sonic login: ")),
                prompt=str(sonic_cfg.get("prompt", "admin@sonic:~$")),
            ),
            prompts=PromptConfig(
                switch=str(prompts_cfg["switch"]),
            ),
            timeouts=TimeoutConfig(
                serial_open_seconds=int(timeouts_cfg["serial_open_seconds"]),
                prompt_wait_seconds=int(timeouts_cfg["prompt_wait_seconds"]),
                boot_interrupt_seconds=int(timeouts_cfg["boot_interrupt_seconds"]),
                uboot_boot_seconds=int(timeouts_cfg.get("uboot_boot_seconds", timeouts_cfg.get("first_boot_seconds", 300))),
            ),
            db=DbConfig(
                db_type=str(db_cfg.get("type", "sqlite")),
                path=str(db_cfg["path"]),
                table=str(db_cfg.get("table", "dig_board_macs")),
                serial_column=str(db_cfg.get("serial_column", "dig_sn")),
                mac_column_format=str(db_cfg.get("mac_column_format", "mac{index}")),
                mac_count=int(db_cfg.get("mac_count", 16)),
                seed_mac=normalize_mac(str(db_cfg.get("seed_mac", "00:00:00:00:00:00"))),
                auto_create=bool(db_cfg.get("auto_create", True)),
            ),
        )


@dataclass(frozen=True)
class ProvisionArgs:
    dig_sn: str
    base_mac: str
    switch_uboot_mac: str
    allocated_macs: tuple[str, ...]
    mac_notice: str


def load_config(path: pathlib.Path) -> AppConfig:
    try:
        return AppConfig.from_mapping(load_simple_yaml(path))
    except KeyError as exc:
        raise ConfigError(f"Missing configuration key: {exc}") from exc


def compact_mac(mac: str) -> str:
    compact = mac.strip().replace(":", "").replace("-", "")
    if len(compact) != 12:
        raise ValueError(f"Invalid MAC address: {mac}")
    int(compact, 16)
    return compact.upper()


def normalize_mac(mac: str) -> str:
    compact = compact_mac(mac)
    return ":".join(compact[index:index + 2] for index in range(0, 12, 2)).upper()


def normalize_dig_sn(dig_sn: str) -> str:
    normalized = dig_sn.strip().upper()
    if not DIG_SN_PATTERN.fullmatch(normalized):
        raise ValueError(
            "Unsupported DIG board serial number format. Expected formats like "
            "CLSDM-09-0926-260528-002 or MLSDM-08-0726-B1-00010."
        )
    return normalized


def sanitize_log_folder_name(value: str | None) -> str:
    if not value:
        return "NO_DIG_SN"
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", value.strip().upper())
    cleaned = cleaned.strip(" ._")
    return cleaned or "NO_DIG_SN"


def timestamped_log_path(dig_sn: str, name: str) -> pathlib.Path:
    run_stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    run_folder = f"{run_stamp}_eth_deploy"
    logs_dir = LOG_ROOT / sanitize_log_folder_name(dig_sn) / run_folder
    logs_dir.mkdir(parents=True, exist_ok=True)
    return logs_dir / f"{run_stamp}-{name}.log"


def sanitize_uart_text(text: str) -> str:
    cleaned = ANSI_ESCAPE_PATTERN.sub("", text)
    cleaned = cleaned.replace("\x00", "")
    cleaned = cleaned.replace("\x08", "")
    return cleaned


def compile_switch_prompt_pattern(prompt_text: str) -> re.Pattern[str]:
    prompt = prompt_text.strip()
    if prompt.endswith(">"):
        stem = re.escape(prompt.rstrip(">"))
        return re.compile(r"(?:^|[\r\n])\s*" + stem + r">+\s*$", re.IGNORECASE | re.MULTILINE)
    return re.compile(r"(?:^|[\r\n])\s*" + re.escape(prompt) + r"\s*$", re.IGNORECASE | re.MULTILINE)


def compile_prompt_pattern(prompt_text: str) -> re.Pattern[str]:
    return re.compile(r"(?:^|[\r\n])\s*" + re.escape(prompt_text.strip()) + r"\s*$", re.IGNORECASE | re.MULTILINE)


def boot_stop_bytes(mode: str) -> bytes:
    normalized = mode.lower()
    if normalized == "enter":
        return b"\r"
    if normalized == "space":
        return b" "
    if normalized == "ctrl-c":
        return b"\x03"
    raise ValueError(f"Unsupported boot stop key mode: {mode}")


class SerialSession:
    def __init__(self, name: str, config: SerialPortConfig, log_path: pathlib.Path, open_timeout: int) -> None:
        self.name = name
        self.config = config
        self.log_path = log_path
        self.buffer = ""
        self.log_file = self.log_path.open("ab")
        self.serial: serial.Serial | None = None
        deadline = time.monotonic() + open_timeout
        last_error: Exception | None = None

        while time.monotonic() < deadline:
            try:
                self.serial = serial.Serial(
                    port=config.port,
                    baudrate=config.baudrate,
                    timeout=0.1,
                    write_timeout=1,
                )
                break
            except serial.SerialException as exc:
                last_error = exc
                time.sleep(0.5)

        if self.serial is None:
            self.log_file.close()
            raise TimeoutError(f"Timed out opening {self.name} on {self.config.port}: {last_error}")

    def __enter__(self) -> SerialSession:
        global ACTIVE_SWITCH_LOG_FILE
        if self.name.lower() == "switch":
            ACTIVE_SWITCH_LOG_FILE = self.log_file
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        global ACTIVE_SWITCH_LOG_FILE
        try:
            if ACTIVE_SWITCH_LOG_FILE is self.log_file:
                ACTIVE_SWITCH_LOG_FILE = None
            self.log_file.close()
        finally:
            if self.serial is not None and self.serial.is_open:
                self.serial.close()

    def write(self, data: bytes) -> None:
        assert self.serial is not None
        self.serial.write(data)
        self.serial.flush()
        self.log_file.write(b"\n[TX] " + data + b"\n")
        self.log_file.flush()

    def send_line(self, text: str) -> None:
        self.write(text.encode("utf-8") + b"\r")

    def poll(self) -> str:
        assert self.serial is not None
        data = self.serial.read(self.serial.in_waiting or 1)
        if not data:
            return ""
        text = data.decode("utf-8", errors="replace")
        sanitized = sanitize_uart_text(text)
        if sanitized:
            self.log_file.write(sanitized.encode("utf-8", errors="replace"))
            self.log_file.flush()
            if self.name.lower() != "switch":
                write_active_switch_log(sanitized)
            if self.name.lower() == "switch":
                print(sanitized, end="", flush=True)
            self.buffer += sanitized
        return sanitized

    def log_event(self, level: str, message: str) -> None:
        self.log_file.write(f"\n[{level}] {message}\n".encode("utf-8", errors="replace"))
        self.log_file.flush()

    def wait_for_pattern(
        self,
        pattern: re.Pattern[str],
        timeout: int,
        label: str,
        start_pos: int | None = None,
    ) -> re.Match[str]:
        deadline = time.monotonic() + timeout
        scan_from = start_pos if start_pos is not None else 0
        while time.monotonic() < deadline:
            self.poll()
            match = pattern.search(self.buffer[scan_from:])
            if match:
                return match
            time.sleep(0.05)
        raise TimeoutError(f"Timed out waiting for {label} on {self.name} ({self.config.port})")

    def wait_for_any_pattern(
        self,
        patterns: dict[str, re.Pattern[str]],
        timeout: int,
        label: str,
        start_pos: int | None = None,
    ) -> str:
        deadline = time.monotonic() + timeout
        scan_from = start_pos if start_pos is not None else 0
        while time.monotonic() < deadline:
            self.poll()
            segment = self.buffer[scan_from:]
            for key, pattern in patterns.items():
                if pattern.search(segment):
                    return key
            time.sleep(0.05)
        raise TimeoutError(f"Timed out waiting for {label} on {self.name} ({self.config.port})")


def run_switch_ping_server(session: SerialSession, prompt_pattern: re.Pattern[str], timeout: int) -> None:
    info("Switch: Switch U-Boot: ping $serverip")
    start_pos = len(session.buffer)
    session.send_line("ping $serverip")
    session.wait_for_pattern(
        prompt_pattern,
        timeout=timeout,
        label="switch prompt after ping $serverip",
        start_pos=start_pos,
    )
    output = session.buffer[start_pos:]
    if SWITCH_PING_FAILED_PATTERN.search(output):
        raise RuntimeError("Switch ping failed: server is not alive.")
    if not SWITCH_PING_ALIVE_PATTERN.search(output):
        raise RuntimeError("Switch ping did not report that the server is alive.")
    ok("Switch ping $serverip reported alive")
    time.sleep(COMMAND_DELAY_SECONDS)


def detect_switch_uboot(
    session: SerialSession,
    prompt_text: str,
    stop_key: bytes,
    timeout: int,
) -> re.Pattern[str]:
    prompt_pattern = compile_switch_prompt_pattern(prompt_text)
    deadline = time.monotonic() + timeout
    interrupting = False
    interrupt_deadline = 0.0
    next_prompt_nudge = 0.0

    while time.monotonic() < deadline:
        now = time.monotonic()
        session.poll()
        if prompt_pattern.search(session.buffer):
            return prompt_pattern

        if now >= next_prompt_nudge:
            session.send_line("")
            next_prompt_nudge = now + 2

        if AUTOBOOT_PATTERN.search(session.buffer) and not interrupting:
            interrupting = True
            interrupt_deadline = now + 6

        if interrupting:
            session.write(stop_key)
            if prompt_pattern.search(session.buffer):
                return prompt_pattern
            if now > interrupt_deadline:
                interrupting = False

        time.sleep(0.1)

    raise TimeoutError(f"Timed out waiting for switch U-Boot prompt {prompt_text!r} on {session.config.port}")


def nxp_shell_prompt_pattern(config: AppConfig) -> re.Pattern[str]:
    configured = config.dut.prompt.strip()
    if configured:
        return re.compile(r"(?:^|[\r\n]).{0,120}" + re.escape(configured) + r".{0,80}[#\$]\s*$", re.MULTILINE)
    return ROOT_SHELL_PATTERN


def ensure_nxp_shell(session: SerialSession, config: AppConfig, timeout: int) -> re.Pattern[str]:
    shell_prompt = nxp_shell_prompt_pattern(config)
    login_prompt = compile_prompt_pattern(config.dut.login_prompt)
    info("NXP UART: detect Linux shell")
    start_pos = len(session.buffer)
    session.send_line("")
    result = session.wait_for_any_pattern(
        {
            "shell": shell_prompt,
            "login": login_prompt,
            "generic_login": LOGIN_PATTERN,
        },
        timeout=timeout,
        label="NXP Linux shell or login prompt",
        start_pos=start_pos,
    )
    if result == "shell":
        ok("NXP Linux shell is ready")
        return shell_prompt

    info(f"NXP UART: send username {config.dut.login}")
    start_pos = len(session.buffer)
    session.send_line(config.dut.login)
    session.wait_for_pattern(PASSWORD_PATTERN, timeout=30, label="NXP password prompt", start_pos=start_pos)
    start_pos = len(session.buffer)
    session.send_line(config.dut.password)
    session.wait_for_pattern(shell_prompt, timeout=timeout, label="NXP Linux shell prompt", start_pos=start_pos)
    ok("Logged into NXP over UART")
    return shell_prompt


def run_nxp_uart_command(
    session: SerialSession,
    command: str,
    prompt_pattern: re.Pattern[str],
    timeout: int,
    description: str | None = None,
    delay: int = COMMAND_DELAY_SECONDS,
) -> str:
    if description:
        info(f"NXP UART: {description}")
    start_pos = len(session.buffer)
    session.send_line(command)
    session.wait_for_pattern(prompt_pattern, timeout=timeout, label=f"NXP command completion for: {command}", start_pos=start_pos)
    output = session.buffer[start_pos:]
    time.sleep(delay)
    return output


def run_nxp_uart_command_live(
    session: SerialSession,
    command: str,
    prompt_pattern: re.Pattern[str],
    timeout: int,
    description: str | None = None,
    delay: int = COMMAND_DELAY_SECONDS,
) -> str:
    if description:
        info(f"NXP UART: {description}")
    start_pos = len(session.buffer)
    session.send_line(command)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        data = session.poll()
        if data:
            print(data, end="", flush=True)
        if prompt_pattern.search(session.buffer[start_pos:]):
            output = session.buffer[start_pos:]
            time.sleep(delay)
            return output
        time.sleep(0.05)
    raise TimeoutError(f"Timed out waiting for NXP command completion for: {command} on {session.config.port}")

def verify_switch_server_ping(config: AppConfig, args: argparse.Namespace) -> None:
    switch_log = timestamped_log_path(args.dig_sn, "eth2-switch")
    info(f"Switch log: {switch_log}")
    with SerialSession(
        "Switch",
        config.switch_serial,
        switch_log,
        config.timeouts.serial_open_seconds,
    ) as switch:
        switch_prompt = detect_switch_uboot(
            session=switch,
            prompt_text=config.prompts.switch,
            stop_key=boot_stop_bytes(args.boot_stop_key),
            timeout=max(config.timeouts.uboot_boot_seconds, config.timeouts.boot_interrupt_seconds),
        )
        ok("Switch U-Boot is ready")
        run_switch_ping_server(switch, switch_prompt, max(config.timeouts.prompt_wait_seconds, 30))

def install_switch_offline_rpm_from_nxp(session: SerialSession, prompt_pattern: re.Pattern[str], config: AppConfig) -> bool:
    verify_output = run_nxp_uart_command(
        session,
        "test -d /root/SWITCH && test -f /root/SWITCH/offline_rpm.tar.gz && echo OK",
        prompt_pattern,
        max(config.timeouts.prompt_wait_seconds, 60),
        "Verify /root/SWITCH/offline_rpm.tar.gz exists",
    )
    if "OK" not in verify_output:
        raise RuntimeError("Expected /root/SWITCH/offline_rpm.tar.gz on NXP; offline RPM install sequence was not started.")

    commands = (
        ("cd /root/SWITCH", max(config.timeouts.prompt_wait_seconds, 60), "Enter SWITCH folder", False),
        ("tar -xzf offline_rpm.tar.gz", max(config.timeouts.prompt_wait_seconds, 300), "Extract offline_rpm.tar.gz", False),
        ("cd offline_rpm", max(config.timeouts.prompt_wait_seconds, 60), "Enter offline_rpm folder", False),
        ("./install.sh", max(config.timeouts.prompt_wait_seconds, 600), "Run offline RPM install script", True),
        ("cd /root/SWITCH", max(config.timeouts.prompt_wait_seconds, 60), "Return to SWITCH folder", False),
        (
            "python3 sw_initialize.py --bundle switch_0.9.0.3.swu",
            max(config.timeouts.prompt_wait_seconds, 1800),
            "Run switch software initialization",
            True,
        ),
        ("cd /root", max(config.timeouts.prompt_wait_seconds, 60), "Return to main folder", False),
    )
    sw_initialize_output = ""
    for command, timeout, description, show_process in commands:
        if show_process:
            output = run_nxp_uart_command_live(session, command, prompt_pattern, timeout, description)
        else:
            output = run_nxp_uart_command(session, command, prompt_pattern, timeout, description)
        if command.startswith("python3 sw_initialize.py"):
            sw_initialize_output = output
    return bool(SW_INITIALIZE_READY_PATTERN.search(sw_initialize_output))

def run_eth_deploy(config: AppConfig, args: argparse.Namespace) -> int:
    nxp_log = timestamped_log_path(args.dig_sn, "eth2-nxp")

    info("NXP UART login and fm1-mac10 configuration flow")
    info(f"DIG SN: {args.dig_sn}")
    info(f"NXP UART log: {nxp_log}")

    try:
        with SerialSession(
            "NXP",
            config.nxp_serial,
            nxp_log,
            config.timeouts.serial_open_seconds,
        ) as nxp:
            nxp.log_event("INFO", f"DIG SN: {args.dig_sn}")
            nxp.log_event("INFO", "NXP fm1-mac10 commands are sent over UART in eth_deploy2.")

            nxp_prompt = ensure_nxp_shell(
                nxp,
                config,
                timeout=max(config.timeouts.prompt_wait_seconds, 60),
            )

            info("Step 1/5: add fm1-mac10 static connection")
            run_nxp_uart_command(
                nxp,
                "nmcli con add type ethernet ifname fm1-mac10 con-name fm1-mac10-static ipv4.addresses 192.168.2.1/24 ipv4.method manual",
                nxp_prompt,
                max(config.timeouts.prompt_wait_seconds, 60),
                "nmcli con add fm1-mac10-static",
            )

            info("Step 2/5: bring fm1-mac10 static connection up")
            run_nxp_uart_command(
                nxp,
                "nmcli con up fm1-mac10-static",
                nxp_prompt,
                max(config.timeouts.prompt_wait_seconds, 60),
                "nmcli con up fm1-mac10-static",
            )

            info("Step 3/5: show NetworkManager connections")
            output = run_nxp_uart_command(
                nxp,
                "nmcli con show",
                nxp_prompt,
                max(config.timeouts.prompt_wait_seconds, 60),
                "nmcli con show",
            )
            print(output, end="" if output.endswith("\n") else "\n")
            info("Step 4/5: detect switch U-Boot and verify ping $serverip")
            verify_switch_server_ping(config, args)
            info("Step 5/5: install offline RPMs and initialize switch software")
            system_ready = install_switch_offline_rpm_from_nxp(nxp, nxp_prompt, config)
            if not system_ready:
                raise RuntimeError("switch initialization finished, but System is ready. was not detected in the output")
            ok("NXP fm1-mac10, switch ping, offline RPM install, and switch initialization completed successfully.")
            print("[PASS] switch deployment completed successfuly", flush=True)
    except (serial.SerialException, TimeoutError, ValueError, RuntimeError) as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1

    return 0

def build_parser() -> argparse.ArgumentParser:
    examples = ", ".join(SUPPORTED_DIG_SN_EXAMPLES)
    parser = argparse.ArgumentParser(
        description="NXP UART login and fm1-mac10 NetworkManager configuration flow.",
    )
    parser.add_argument(
        "--dig_sn",
        "--dig-sn",
        required=True,
        help=(
            "DIG board serial number used to group logs for this run. "
            f"Supported formats include: {examples}."
        ),
    )
    parser.add_argument(
        "--config",
        default="script_setup.yaml",
        help="Path to the YAML configuration file.",
    )
    parser.add_argument(
        "--boot_stop_key",
        "--boot-stop-key",
        choices=["enter", "space", "ctrl-c"],
        default="enter",
        help="Key sent to stop autoboot on the switch console.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    repo_root = pathlib.Path(__file__).resolve().parent
    config_path = (repo_root / args.config).resolve()

    try:
        args.dig_sn = normalize_dig_sn(args.dig_sn)
        config = load_config(config_path)
        return run_eth_deploy(config, args)
    except (OSError, ConfigError, ValueError) as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
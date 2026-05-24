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


def warn(message: str) -> None:
    print(f"[WARN] {message}", flush=True)
    write_active_switch_log(f"\n[WARN] {message}\n")


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


def mac_plus(mac: str, increment: int) -> str:
    value = int(compact_mac(mac), 16)
    value = (value + increment) & ((1 << 48) - 1)
    return normalize_mac(f"{value:012X}")


def normalize_dig_sn(dig_sn: str) -> str:
    normalized = dig_sn.strip().upper()
    if not DIG_SN_PATTERN.fullmatch(normalized):
        raise ValueError(
            "Unsupported DIG board serial number format. Expected formats like "
            "CLSDM-09-0926-260528-002 or MLSDM-08-0726-B1-00010."
        )
    return normalized


def validate_sql_identifier(name: str, label: str) -> str:
    if not SQL_IDENTIFIER_PATTERN.fullmatch(name):
        raise ConfigError(f"Invalid SQL identifier for {label}: {name}")
    return name


def sanitize_log_folder_name(value: str | None) -> str:
    if not value:
        return "NO_DIG_SN"
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", value.strip().upper())
    cleaned = cleaned.strip(" ._")
    return cleaned or "NO_DIG_SN"


def timestamped_log_path(dig_sn: str, name: str) -> pathlib.Path:
    run_stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    logs_dir = LOG_ROOT / sanitize_log_folder_name(dig_sn) / run_stamp
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


def run_command(
    session: SerialSession,
    command: str,
    prompt_pattern: re.Pattern[str],
    timeout: int,
    description: str,
) -> str:
    info(f"{session.name}: {description}")
    start_pos = len(session.buffer)
    session.send_line(command)
    session.wait_for_pattern(prompt_pattern, timeout=timeout, label=f"command completion for: {command}", start_pos=start_pos)
    output = session.buffer[start_pos:]
    time.sleep(COMMAND_DELAY_SECONDS)
    return output


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


def configure_switch_onie_install_env(
    session: SerialSession,
    config: AppConfig,
    prompt_pattern: re.Pattern[str],
    switch_mac: str,
    timeout: int,
) -> None:
    commands = [
        f"setenv ethaddr {switch_mac}",
        f"setenv onie_install_url install_url=http://192.168.2.1/{config.sonic.image_file}",
    ]
    for command in commands:
        run_command(session, command, prompt_pattern, timeout, f"Switch U-Boot: {command}")

    info("Switch: Switch U-Boot: saveenv")
    start_pos = len(session.buffer)
    session.send_line("saveenv")
    session.wait_for_pattern(
        SWITCH_SAVEENV_OK_PATTERN,
        timeout=max(timeout, 30),
        label="switch saveenv OK for ONIE install env",
        start_pos=start_pos,
    )
    session.wait_for_pattern(
        prompt_pattern,
        timeout=max(timeout, 30),
        label="switch prompt after ONIE install saveenv",
        start_pos=start_pos,
    )
    time.sleep(COMMAND_DELAY_SECONDS)


def boot_switch_onie_image(session: SerialSession, prompt_pattern: re.Pattern[str], timeout: int) -> None:
    command = "tftpboot $onie_loadaddr $onie_image_name"
    output = run_command(session, command, prompt_pattern, max(timeout, 30), f"Switch U-Boot: {command}")
    if SWITCH_TFTP_ERROR_PATTERN.search(output):
        raise RuntimeError(f"Switch TFTP failed while loading $onie_image_name:\n{output.strip()}")
    time.sleep(6)

    command = "run onie_bootargs"
    run_command(session, command, prompt_pattern, max(timeout, 30), f"Switch U-Boot: {command}")

    command = "bootm $onie_loadaddr"
    info(f"Switch: Switch U-Boot: {command}")
    start_pos = len(session.buffer)
    session.send_line(command)
    result = session.wait_for_any_pattern(
        {
            "reboot": SWITCH_REBOOT_REQUEST_PATTERN,
            "bootm_error": SWITCH_BOOTM_ERROR_PATTERN,
        },
        timeout=max(timeout, 30),
        label="switch reboot request or bootm error after bootm $onie_loadaddr",
        start_pos=start_pos,
    )
    if result == "bootm_error":
        output = session.buffer[start_pos:]
        raise RuntimeError(f"Switch bootm failed:\n{output.strip()}")
    session.wait_for_pattern(
        SWITCH_REBOOT_REQUEST_PATTERN,
        timeout=1,
        label="switch reboot request after bootm $onie_loadaddr",
        start_pos=start_pos,
    )
    time.sleep(COMMAND_DELAY_SECONDS)


def wait_with_progress(total_seconds: int, label: str) -> None:
    info(f"{label}: waiting {total_seconds // 60}:{total_seconds % 60:02d}")
    if tqdm is not None:
        for _ in tqdm(range(total_seconds), desc=label, unit="s", leave=True, dynamic_ncols=True):
            time.sleep(1)
        return
    remaining = total_seconds
    while remaining > 0:
        chunk = min(30, remaining)
        time.sleep(chunk)
        remaining -= chunk
        if remaining:
            info(f"{label}: {remaining // 60}:{remaining % 60:02d} remaining")


def login_sonic_over_uart(session: SerialSession, config: AppConfig) -> re.Pattern[str]:
    login_pattern = compile_prompt_pattern(config.sonic.login_prompt)
    sonic_prompt = compile_prompt_pattern(config.sonic.prompt)
    info("Switch UART: detect SONiC login prompt")
    start_pos = len(session.buffer)
    session.send_line("")
    result = session.wait_for_any_pattern(
        {
            "shell": sonic_prompt,
            "login": login_pattern,
        },
        timeout=max(config.timeouts.uboot_boot_seconds, 120),
        label="SONiC login or shell prompt",
        start_pos=start_pos,
    )
    if result == "shell":
        ok("SONiC shell is already active")
        return sonic_prompt

    info(f"Switch UART: send SONiC username {config.sonic.login}")
    start_pos = len(session.buffer)
    session.send_line(config.sonic.login)
    session.wait_for_pattern(PASSWORD_PATTERN, timeout=30, label="SONiC password prompt", start_pos=start_pos)
    start_pos = len(session.buffer)
    session.send_line(config.sonic.password)
    session.wait_for_pattern(sonic_prompt, timeout=120, label="SONiC shell prompt", start_pos=start_pos)
    ok("Logged into SONiC over switch UART")
    return sonic_prompt


def run_sonic_sudo_command(
    session: SerialSession,
    command: str,
    prompt_pattern: re.Pattern[str],
    password: str,
    timeout: int = 120,
) -> None:
    info(f"SONiC: {command}")
    start_pos = len(session.buffer)
    session.send_line(command)
    result = session.wait_for_any_pattern(
        {
            "password": PASSWORD_PATTERN,
            "prompt": prompt_pattern,
        },
        timeout=timeout,
        label=f"SONiC command completion for: {command}",
        start_pos=start_pos,
    )
    if result == "password":
        start_pos = len(session.buffer)
        session.send_line(password)
        session.wait_for_pattern(prompt_pattern, timeout=timeout, label=f"SONiC prompt after sudo password for: {command}", start_pos=start_pos)
    time.sleep(SONIC_COMMAND_DELAY_SECONDS)


def configure_sonic_over_uart(session: SerialSession, config: AppConfig, prompt_pattern: re.Pattern[str]) -> None:
    for command in SONIC_CONFIG_COMMANDS:
        run_sonic_sudo_command(session, command, prompt_pattern, config.sonic.password)


def verify_sonic_management_ping(session: SerialSession, config: AppConfig, prompt_pattern: re.Pattern[str]) -> None:
    command = "sudo ip vrf exec mgmt ping 192.168.2.1 -c1"
    info(f"SONiC: {command}")
    start_pos = len(session.buffer)
    session.send_line(command)
    result = session.wait_for_any_pattern(
        {
            "password": PASSWORD_PATTERN,
            "ping": SONIC_PING_REPLY_PATTERN,
        },
        timeout=90,
        label="SONiC management ping response",
        start_pos=start_pos,
    )
    if result == "password":
        start_pos = len(session.buffer)
        session.send_line(config.sonic.password)
        session.wait_for_pattern(
            SONIC_PING_REPLY_PATTERN,
            timeout=90,
            label="SONiC management ping response after sudo password",
            start_pos=start_pos,
        )
    session.wait_for_pattern(SONIC_PING_SUMMARY_PATTERN, timeout=30, label="SONiC ping success summary", start_pos=start_pos)
    session.wait_for_pattern(prompt_pattern, timeout=30, label="SONiC prompt after ping", start_pos=start_pos)
    ok("SONiC management ping to 192.168.2.1 succeeded")


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


def burn_switch_uboot_mac(
    session: SerialSession,
    prompt_pattern: re.Pattern[str],
    switch_mac: str,
    timeout: int,
) -> None:
    info("Switch: Switch U-Boot: env default -a")
    start_pos = len(session.buffer)
    session.send_line("env default -a")
    session.wait_for_pattern(
        SWITCH_ENV_RESET_PATTERN,
        timeout=timeout,
        label="switch environment reset message",
        start_pos=start_pos,
    )
    session.wait_for_pattern(
        prompt_pattern,
        timeout=timeout,
        label="switch prompt after env default -a",
        start_pos=start_pos,
    )
    time.sleep(COMMAND_DELAY_SECONDS)

    setenv_command = f"setenv ethaddr {switch_mac}"
    run_command(session, setenv_command, prompt_pattern, timeout, f"Switch U-Boot: {setenv_command}")

    info("Switch: Switch U-Boot: saveenv")
    start_pos = len(session.buffer)
    session.send_line("saveenv")
    session.wait_for_pattern(
        SWITCH_SAVEENV_OK_PATTERN,
        timeout=max(timeout, 30),
        label="switch saveenv OK",
        start_pos=start_pos,
    )
    session.wait_for_pattern(
        prompt_pattern,
        timeout=max(timeout, 30),
        label="switch prompt after saveenv",
        start_pos=start_pos,
    )
    time.sleep(COMMAND_DELAY_SECONDS)


def build_db_path(config_path: pathlib.Path, db_config: DbConfig) -> pathlib.Path:
    db_path = pathlib.Path(db_config.path)
    if db_path.is_absolute():
        return db_path
    return (config_path.parent / db_path).resolve()


def mac_column_names(db_config: DbConfig) -> list[str]:
    columns = [db_config.mac_column_format.format(index=index) for index in range(1, db_config.mac_count + 1)]
    return [validate_sql_identifier(column, f"db.mac_column_format[{position}]") for position, column in enumerate(columns, start=1)]


def normalize_optional_mac(raw: Any) -> str | None:
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    return normalize_mac(text)


def ensure_db_table(connection: sqlite3.Connection, db_config: DbConfig) -> None:
    if not db_config.auto_create:
        return
    table = validate_sql_identifier(db_config.table, "db.table")
    serial_column = validate_sql_identifier(db_config.serial_column, "db.serial_column")
    mac_fields = ", ".join(f"{column} TEXT" for column in mac_column_names(db_config))
    connection.execute(
        f"CREATE TABLE IF NOT EXISTS {table} ("
        f"{serial_column} TEXT PRIMARY KEY, {mac_fields}, type TEXT, user TEXT, time TEXT)"
    )
    existing_columns = {
        row[1]
        for row in connection.execute(f"PRAGMA table_info({table})")
    }
    for column in [*mac_column_names(db_config), "type", "user", "time"]:
        if column not in existing_columns:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} TEXT")
    connection.commit()


def open_db_connection(config_path: pathlib.Path, db_config: DbConfig) -> sqlite3.Connection:
    if db_config.db_type.lower() != "sqlite":
        raise ConfigError(f"Unsupported db.type: {db_config.db_type}. Only 'sqlite' is supported.")
    db_path = build_db_path(config_path, db_config)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    ensure_db_table(connection, db_config)
    return connection


def fetch_serial_row(connection: sqlite3.Connection, db_config: DbConfig, dig_sn: str) -> sqlite3.Row | None:
    table = validate_sql_identifier(db_config.table, "db.table")
    serial_column = validate_sql_identifier(db_config.serial_column, "db.serial_column")
    columns = [serial_column, *mac_column_names(db_config)]
    return connection.execute(
        f"SELECT {', '.join(columns)} FROM {table} WHERE {serial_column} = ?",
        (dig_sn,),
    ).fetchone()


def find_latest_saved_mac(connection: sqlite3.Connection, db_config: DbConfig) -> str | None:
    table = validate_sql_identifier(db_config.table, "db.table")
    max_mac_value: int | None = None
    for column in mac_column_names(db_config):
        for row in connection.execute(f"SELECT {column} FROM {table} WHERE {column} IS NOT NULL AND TRIM({column}) <> ''"):
            mac = normalize_optional_mac(row[0])
            if mac is None:
                continue
            value = int(compact_mac(mac), 16)
            if max_mac_value is None or value > max_mac_value:
                max_mac_value = value
    if max_mac_value is None:
        return None
    return normalize_mac(f"{max_mac_value:012X}")


def build_mac_block(base_mac: str, count: int) -> list[str]:
    return [mac_plus(base_mac, offset) for offset in range(count)]


def infer_base_mac_from_existing(row_macs: list[str | None]) -> str | None:
    for index, mac in enumerate(row_macs):
        if mac is not None:
            return mac_plus(mac, -index)
    return None


def upsert_serial_row(connection: sqlite3.Connection, db_config: DbConfig, dig_sn: str, macs: list[str]) -> None:
    table = validate_sql_identifier(db_config.table, "db.table")
    serial_column = validate_sql_identifier(db_config.serial_column, "db.serial_column")
    mac_columns = mac_column_names(db_config)
    metadata_columns = ["type", "user", "time"]
    all_columns = [serial_column, *mac_columns, *metadata_columns]
    placeholders = ", ".join("?" for _ in all_columns)
    updates = ", ".join(f"{column} = excluded.{column}" for column in [*mac_columns, *metadata_columns])
    values = [
        dig_sn,
        *[compact_mac(mac) for mac in macs],
        "LSBB_DIG_BOARD",
        getpass.getuser(),
        time.ctime(),
    ]
    connection.execute(
        f"INSERT INTO {table} ({', '.join(all_columns)}) VALUES ({placeholders}) "
        f"ON CONFLICT({serial_column}) DO UPDATE SET {updates}",
        values,
    )
    connection.commit()


def resolve_macs_from_db(config: AppConfig, config_path: pathlib.Path, dig_sn: str) -> ProvisionArgs:
    db_config = config.db
    with open_db_connection(config_path, db_config) as connection:
        row = fetch_serial_row(connection, db_config, dig_sn)
        if row is not None:
            row_macs = [normalize_optional_mac(row[column]) for column in mac_column_names(db_config)]
            if all(mac is not None for mac in row_macs):
                allocated_macs = tuple(mac for mac in row_macs if mac is not None)
                return ProvisionArgs(
                    dig_sn=dig_sn,
                    base_mac=allocated_macs[0],
                    switch_uboot_mac=allocated_macs[1],
                    allocated_macs=allocated_macs,
                    mac_notice="Using existing MAC allocation from database.",
                )

            base_mac = infer_base_mac_from_existing(row_macs)
            if base_mac is None:
                latest_saved_mac = find_latest_saved_mac(connection, db_config)
                base_mac = mac_plus(latest_saved_mac or db_config.seed_mac, 1 if latest_saved_mac else 0)
            new_macs = tuple(build_mac_block(base_mac, db_config.mac_count))
            upsert_serial_row(connection, db_config, dig_sn, list(new_macs))
            return ProvisionArgs(
                dig_sn=dig_sn,
                base_mac=new_macs[0],
                switch_uboot_mac=new_macs[1],
                allocated_macs=new_macs,
                mac_notice="Serial number existed with incomplete MAC data. Rebuilt and saved a new MAC block.",
            )

        latest_saved_mac = find_latest_saved_mac(connection, db_config)
        base_mac = mac_plus(latest_saved_mac or db_config.seed_mac, 1 if latest_saved_mac else 0)
        new_macs = tuple(build_mac_block(base_mac, db_config.mac_count))
        upsert_serial_row(connection, db_config, dig_sn, list(new_macs))
        return ProvisionArgs(
            dig_sn=dig_sn,
            base_mac=new_macs[0],
            switch_uboot_mac=new_macs[1],
            allocated_macs=new_macs,
            mac_notice="New serial number detected. Created and saved a new MAC block.",
        )


def verify_switch_mac_is_base_plus_one(provision: ProvisionArgs) -> None:
    expected_switch_mac = mac_plus(provision.base_mac, 1)
    actual_switch_mac = normalize_mac(provision.switch_uboot_mac)
    if actual_switch_mac != expected_switch_mac:
        raise ValueError(
            f"Switch MAC must be base MAC + 1. base={provision.base_mac}, "
            f"expected={expected_switch_mac}, actual={actual_switch_mac}"
        )


def local_image_file_path(config: AppConfig, filename: str) -> pathlib.Path:
    image_dir = pathlib.Path(config.server.image_path)
    return image_dir / filename


def remote_tmp_dir(config: AppConfig) -> str:
    return "/tmp"


def relevant_image_files(config: AppConfig) -> tuple[pathlib.Path, ...]:
    return (
        local_image_file_path(config, config.sonic.image_file),
        local_image_file_path(config, config.switch.image_file),
    )


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


def shell_quote(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"


def output_has_exact_line(output: str, expected: str) -> bool:
    return any(line.strip() == expected for line in output.splitlines())


def output_has_file_listing(output: str, filename: str) -> bool:
    for line in output.splitlines():
        stripped = line.strip()
        if filename in stripped and re.match(r"^[bcdlps-][rwx-]{9}\s+", stripped):
            return True
    return False


def verify_remote_image_files(
    config: AppConfig,
    nxp: SerialSession,
    prompt_pattern: re.Pattern[str],
    copied_files: tuple[pathlib.Path, ...],
) -> None:
    remote_dir = remote_tmp_dir(config)
    for local_path in copied_files:
        remote_path = f"{remote_dir}/{local_path.name}"
        command = f"test -f {shell_quote(remote_path)} && ls -l {shell_quote(remote_path)}"
        output = run_nxp_uart_command(
            nxp,
            command,
            prompt_pattern,
            timeout=max(config.timeouts.prompt_wait_seconds, 30),
            description=f"Verify copied file exists: {remote_path}",
        )
        if not output_has_file_listing(output, local_path.name):
            raise RuntimeError(f"Copied file verification failed on DUT: {remote_path}")
        ok(f"Verified {local_path.name} exists in DUT tmp")


def start_nxp_image_servers(config: AppConfig, nxp: SerialSession, prompt_pattern: re.Pattern[str]) -> None:
    for command in NXP_IMAGE_SERVER_COMMANDS:
        if command == "cd /tmp":
            run_nxp_uart_command(nxp, "cd /tmp && pwd", prompt_pattern, config.timeouts.prompt_wait_seconds, command)
            continue
        if command == "cd /root":
            run_nxp_uart_command(nxp, "cd /root && pwd", prompt_pattern, config.timeouts.prompt_wait_seconds, command)
            continue
        run_nxp_uart_command(nxp, f"cd /tmp && {command}", prompt_pattern, config.timeouts.prompt_wait_seconds, command)


def run_windows_scp_with_password(command: list[str], password: str, timeout: int) -> None:
    env = os.environ.copy()
    askpass_cmd: pathlib.Path | None = None
    askpass_ps1: pathlib.Path | None = None
    try:
        temp_dir = pathlib.Path(tempfile.mkdtemp(prefix="eth_scp_askpass_"))
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
        env.setdefault("DISPLAY", "eth_deploy")

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
                raise TimeoutError(f"Windows->DUT SCP timed out after {timeout} seconds.")

            char = process.stdout.read(1)
            if char:
                print(char, end="", flush=True)
                write_active_switch_log(char)
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
                if return_code != 0:
                    raise RuntimeError(
                        f"Windows->DUT SCP failed with exit status {return_code}.\n"
                        f"{''.join(output_parts).strip()}"
                    )
                return

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


def copy_relevant_images_to_dut(config: AppConfig, nxp: SerialSession, prompt_pattern: re.Pattern[str]) -> None:
    files = relevant_image_files(config)
    if not files:
        warn("No image files were configured for Windows-to-DUT copy.")
        return

    remote_dir = remote_tmp_dir(config)
    info(f"Windows->DUT SCP target: {config.dut.login}@{config.dut.final_ip}:{remote_dir}")
    run_nxp_uart_command(nxp, f"mkdir -p {shell_quote(remote_dir)}", prompt_pattern, config.timeouts.prompt_wait_seconds, f"Create {remote_dir}")

    for local_path in files:
        if not local_path.is_file():
            raise RuntimeError(f"Configured image file was not found: {local_path}")
        remote_spec = f"{config.dut.login}@{config.dut.final_ip}:{remote_dir}"
        command = [
            "scp",
            "-o",
            "StrictHostKeyChecking=no",
            "-o",
            "UserKnownHostsFile=NUL",
            str(local_path),
            remote_spec,
        ]
        info(f"Windows->DUT SCP: {' '.join(command)}")
        run_windows_scp_with_password(command, config.dut.password, timeout=1800)
        ok(f"Copied {local_path.name} to DUT")
    verify_remote_image_files(config, nxp, prompt_pattern, files)
    start_nxp_image_servers(config, nxp, prompt_pattern)


def run_nxp_uart_commands_and_copy_images(
    config: AppConfig,
    nxp: SerialSession,
    prompt_pattern: re.Pattern[str],
    after_nmcli: Any | None = None,
) -> None:
    for command in NXP_UART_COMMANDS:
        run_nxp_uart_command(nxp, command, prompt_pattern, config.timeouts.prompt_wait_seconds, command)
    if after_nmcli is not None:
        after_nmcli()
    copy_relevant_images_to_dut(config, nxp, prompt_pattern)


def reset_switch_from_nxp(nxp: SerialSession, prompt_pattern: re.Pattern[str], config: AppConfig) -> None:
    run_nxp_uart_command(nxp, "cd /root", prompt_pattern, config.timeouts.prompt_wait_seconds, "Return to /root before switch reset")
    run_nxp_uart_command(nxp, "cpld w 0x45 0", prompt_pattern, config.timeouts.prompt_wait_seconds, "NXP reset switch: cpld w 0x45 0")
    run_nxp_uart_command(nxp, "cpld w 0x45 3", prompt_pattern, config.timeouts.prompt_wait_seconds, "NXP reset switch: cpld w 0x45 3")


def run_eth_deploy(config: AppConfig, provision: ProvisionArgs, args: argparse.Namespace) -> int:
    provision = ProvisionArgs(
        dig_sn=provision.dig_sn,
        base_mac=normalize_mac(provision.base_mac),
        switch_uboot_mac=normalize_mac(provision.switch_uboot_mac),
        allocated_macs=tuple(normalize_mac(mac) for mac in provision.allocated_macs),
        mac_notice=provision.mac_notice,
    )
    verify_switch_mac_is_base_plus_one(provision)
    switch_log = timestamped_log_path(provision.dig_sn, "eth-switch")
    nxp_log = timestamped_log_path(provision.dig_sn, "eth-nxp")

    info(f"DIG SN: {provision.dig_sn}")
    info(f"Base MAC: {provision.base_mac}")
    info(f"MAC2 / switch ethaddr: {provision.switch_uboot_mac} (base MAC + 1)")
    info(f"Switch U-Boot prompt from YAML: {config.prompts.switch}")
    info(f"Switch log: {switch_log}")
    info(f"NXP UART log: {nxp_log}")
    info(provision.mac_notice)

    try:
        with SerialSession(
            "Switch",
            config.switch_serial,
            switch_log,
            config.timeouts.serial_open_seconds,
        ) as switch, SerialSession(
            "NXP",
            config.nxp_serial,
            nxp_log,
            config.timeouts.serial_open_seconds,
        ) as nxp:
            switch.log_event("INFO", f"DIG SN: {provision.dig_sn}")
            switch.log_event("INFO", provision.mac_notice)
            switch.log_event("INFO", f"Base MAC: {provision.base_mac}")
            switch.log_event("INFO", f"Switch ethaddr: {provision.switch_uboot_mac}")
            nxp.log_event("INFO", f"DIG SN: {provision.dig_sn}")
            nxp.log_event("INFO", "NXP commands are sent over UART in eth_deploy.")

            nxp_prompt = ensure_nxp_shell(
                nxp,
                config,
                timeout=max(config.timeouts.prompt_wait_seconds, 60),
            )

            info("Step 1/7: detect switch U-Boot using the YAML prompt")
            switch_prompt = detect_switch_uboot(
                session=switch,
                prompt_text=config.prompts.switch,
                stop_key=boot_stop_bytes(args.boot_stop_key),
                timeout=max(config.timeouts.uboot_boot_seconds, config.timeouts.boot_interrupt_seconds),
            )
            ok("Switch U-Boot is ready")

            info("Step 2/7: write MAC2 as switch ethaddr and saveenv")
            burn_switch_uboot_mac(
                switch,
                switch_prompt,
                provision.switch_uboot_mac,
                config.timeouts.prompt_wait_seconds,
            )

            info("Step 3/7: use NXP UART, configure fm1-mac10, ping serverip, copy files, and start image servers")
            run_nxp_uart_commands_and_copy_images(
                config,
                nxp,
                nxp_prompt,
                after_nmcli=lambda: run_switch_ping_server(
                    switch,
                    switch_prompt,
                    max(config.timeouts.prompt_wait_seconds, 30),
                ),
            )

            info("Step 4/7: set switch ONIE install environment")
            configure_switch_onie_install_env(
                switch,
                config,
                switch_prompt,
                provision.switch_uboot_mac,
                config.timeouts.prompt_wait_seconds,
            )

            info("Step 5/7: boot switch ONIE image")
            boot_switch_onie_image(switch, switch_prompt, config.timeouts.uboot_boot_seconds)

            info("Step 6/7: reset switch from NXP and wait for SONiC boot")
            reset_switch_from_nxp(nxp, nxp_prompt, config)
            wait_with_progress(SWITCH_SONIC_BOOT_WAIT_SECONDS, "Switch SONiC boot")

            info("Step 7/7: log into SONiC, configure management, and verify ping")
            sonic_prompt = login_sonic_over_uart(switch, config)
            configure_sonic_over_uart(switch, config, sonic_prompt)
            verify_sonic_management_ping(switch, config, sonic_prompt)
            ok("Deployment completed successfully.")
    except (serial.SerialException, TimeoutError, ValueError, RuntimeError) as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        write_active_switch_log(f"\n[ERROR] {exc}\n")
        return 1

    return 0


def build_parser() -> argparse.ArgumentParser:
    examples = ", ".join(SUPPORTED_DIG_SN_EXAMPLES)
    parser = argparse.ArgumentParser(
        description="Standalone switch ethaddr and NXP fm1-mac10 configuration flow.",
    )
    parser.add_argument(
        "--dig_sn",
        "--dig-sn",
        required=True,
        help=(
            "DIG board serial number used to allocate/read MAC addresses from the DB. "
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
        dig_sn = normalize_dig_sn(args.dig_sn)
        config = load_config(config_path)
        provision = resolve_macs_from_db(config, config_path, dig_sn)
        return run_eth_deploy(config, provision, args)
    except (OSError, ConfigError, ValueError, sqlite3.Error) as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

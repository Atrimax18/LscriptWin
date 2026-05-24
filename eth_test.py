from __future__ import annotations

import argparse
import datetime as dt
import pathlib
import re
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Any

import serial

try:
    from tqdm import tqdm
except ImportError:
    tqdm = None


AUTOBOOT_PATTERN = re.compile(r"Hit any key to stop autoboot:", re.IGNORECASE)
LOGIN_PATTERN = re.compile(r"(?:^|\n).{0,40}login:\s*$", re.IGNORECASE | re.MULTILINE)
SONIC_LOGIN_PATTERN = re.compile(r"(?:^|\n).{0,40}sonic login:\s*$", re.IGNORECASE | re.MULTILINE)
PASSWORD_PATTERN = re.compile(r"(?:^|\n).{0,80}password(?: for [^:]+)?:\s*$", re.IGNORECASE | re.MULTILINE)
HOST_KEY_CONFIRM_YES_PATTERN = re.compile(r"are you sure you want to continue connecting", re.IGNORECASE)
HOST_KEY_CONFIRM_Y_PATTERN = re.compile(r"do you want to continue connecting\?\s*\(y/n\)", re.IGNORECASE)
ROOT_SHELL_PATTERN = re.compile(r"(?:^|\n).{0,120}#\s*$", re.MULTILINE)
USER_SHELL_PATTERN = re.compile(r"(?:^|\n).{0,120}(?:\$|>)\s*$", re.MULTILINE)
UBOOT_PROMPT_PATTERN = re.compile(r"(?:^|\n)\s*=>\s*$", re.MULTILINE)
ANSI_ESCAPE_PATTERN = re.compile(r"\x1B\[[0-?]*[ -/]*[@-~]")
MAC_SAVE_SUCCESS_PATTERN = re.compile(r"Programming passed\.", re.IGNORECASE)
SONIC_SYSTEM_READY_PATTERN = re.compile(
    r"(?:^|\n)(?:[A-Z][a-z]{2}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2}(?:\.\d+)?\s+)?System is ready\s*$",
    re.IGNORECASE | re.MULTILINE,
)
NXP_REBOOT_TRANSITION_PATTERN = re.compile(r"(?:reboot: Restarting system|NOTICE:|ls1046afrwy login:)", re.IGNORECASE)
SWITCH_GOING_DOWN_PATTERN = re.compile(r"The system is going down NOW!", re.IGNORECASE)
SWITCH_SIGTERM_PATTERN = re.compile(r"Sent SIGTERM to all processes", re.IGNORECASE)
SWITCH_SIGKILL_PATTERN = re.compile(r"Sent SIGKILL to all processes", re.IGNORECASE)
SWITCH_REBOOT_REQUEST_PATTERN = re.compile(r"(?:requesting\s+)?system.*reboot|requesting.*reboot", re.IGNORECASE)
SWITCH_BOOTM_WRONG_FORMAT_PATTERN = re.compile(r"Wrong Image Format for bootm command", re.IGNORECASE)
SWITCH_BOOTM_KERNEL_ERROR_PATTERN = re.compile(r"ERROR:\s*can't get kernel image!", re.IGNORECASE)
SWITCH_ENV_RESET_PATTERN = re.compile(r"## Resetting to default environment", re.IGNORECASE)
SWITCH_SAVEENV_OK_PATTERN = re.compile(r"(^|\n)OK\s*$", re.IGNORECASE | re.MULTILINE)
PING_SUCCESS_PATTERN = re.compile(
    r"(?:64 bytes from\s+10\.10\.10\.1:.*?1 packets transmitted,\s*1 packets received,\s*0% packet loss|"
    r"1 packets transmitted,\s*1 packets received,\s*0% packet loss)",
    re.IGNORECASE | re.DOTALL,
)
PING_SUCCESS_TEXT = "1 packets transmitted, 1 packets received, 0% packet loss"
EMERGENCY_SHELL_PATTERN = re.compile(r"(?:^|\n)\s*sh-5\.2#\s*$", re.MULTILINE)
EMERGENCY_MAINTENANCE_PATTERN = re.compile(
    r"You are in emergency mode\..*?Press Enter for maintenance\s*\(or press Control-D to continue\):",
    re.IGNORECASE | re.DOTALL,
)
NXP_CLU1_LOCKED_PATTERN = re.compile(r"CLU: DEV1: design: \[DV1_V3\.3\] \| PLL Status - Locked", re.IGNORECASE)
NXP_CLU2_LOCKED_PATTERN = re.compile(r"CLU: DEV2: design: \[DV2_V3\.3\] \| PLL Status - Locked", re.IGNORECASE)
NXP_SWITCH_READY_PATTERN = re.compile(r"(?:^|\n)Switch ready\s*$", re.IGNORECASE | re.MULTILINE)
NXP_FPGA_READY_PATTERN = re.compile(r"(?:^|\n)FPGA ready\s*$", re.IGNORECASE | re.MULTILINE)
DIG_SN_PATTERN = re.compile(r"^[A-Z]{5}-\d{2}-\d{4}-(?:\d{6}|[A-Z]\d)-\d{3,5}$")
SQL_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class ConfigError(RuntimeError):
    pass


class BootValidationError(RuntimeError):
    pass


def info(message: str) -> None:
    print(f"[INFO] {message}", flush=True)


def ok(message: str) -> None:
    print(f"[OK] {message}", flush=True)


def warn(message: str) -> None:
    print(f"[WARN] {message}", flush=True)


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
    return root


def compact_mac(mac: str) -> str:
    compact = mac.strip().replace(":", "").replace("-", "")
    if len(compact) != 12:
        raise ValueError(f"Invalid MAC address: {mac}")
    int(compact, 16)
    return compact.upper()


def normalize_mac(mac: str) -> str:
    compact = compact_mac(mac)
    return ":".join(compact[index:index + 2] for index in range(0, 12, 2))


def mac_plus(mac: str, increment: int) -> str:
    value = int(normalize_mac(mac).replace(":", ""), 16)
    value = (value + increment) & ((1 << 48) - 1)
    packed = f"{value:012X}"
    return ":".join(packed[index:index + 2] for index in range(0, 12, 2))


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


def manual_pause(message: str) -> None:
    print(f"[ACTION] {message}", flush=True)
    try:
        input("Press Enter when ready to continue...")
    except EOFError as exc:
        raise RuntimeError("Manual intervention was required but stdin is not interactive.") from exc


def sanitize_uart_text(text: str) -> str:
    cleaned = ANSI_ESCAPE_PATTERN.sub("", text)
    cleaned = cleaned.replace("\x00", "")
    cleaned = cleaned.replace("\x08", "")
    return cleaned


def sanitize_uart_text_stream(text: str, carry: str) -> tuple[str, str]:
    combined = carry + text
    trailing_escape = ""

    last_escape = combined.rfind("\x1b")
    if last_escape != -1:
        candidate = combined[last_escape:]
        if not ANSI_ESCAPE_PATTERN.fullmatch(candidate):
            trailing_escape = candidate
            combined = combined[:last_escape]

    return sanitize_uart_text(combined), trailing_escape


@dataclass(frozen=True)
class SerialPortConfig:
    port: str
    baudrate: int


@dataclass(frozen=True)
class ServerConfig:
    ip: str
    login: str
    password: str
    image_path: str


@dataclass(frozen=True)
class DutConfig:
    final_ip: str
    login: str
    password: str
    utils_path: str
    tmp_path: str
    image_file: str
    em_prompt: str


@dataclass(frozen=True)
class SwitchConfig:
    ip: str
    prompt: str
    image_file: str


@dataclass(frozen=True)
class SonicConfig:
    login: str
    password: str
    prompt: str
    image_file: str


@dataclass(frozen=True)
class PromptConfig:
    u_boot: str
    switch: str


@dataclass(frozen=True)
class TimeoutConfig:
    serial_open_seconds: int
    prompt_wait_seconds: int
    boot_interrupt_seconds: int
    uboot_boot_seconds: int
    emergency_boot_seconds: int
    first_boot_seconds: int
    sonic_boot_seconds: int


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
    nxp: SerialPortConfig
    switch_serial: SerialPortConfig
    server: ServerConfig
    dut: DutConfig
    switch: SwitchConfig
    sonic: SonicConfig
    prompts: PromptConfig
    timeouts: TimeoutConfig
    db: DbConfig | None

    @classmethod
    def from_mapping(cls, payload: dict[str, Any]) -> "AppConfig":
        serial_cfg = payload["serial"]
        server_cfg = payload["server"]
        dut_cfg = payload["dut"]
        switch_cfg = payload["switch"]
        sonic_cfg = payload["sonic"]
        prompts_cfg = payload["prompts"]
        timeouts_cfg = payload["timeouts"]
        db_cfg = payload.get("db")
        return cls(
            nxp=SerialPortConfig(
                port=str(serial_cfg["nxp"]["port"]),
                baudrate=int(serial_cfg["nxp"]["baudrate"]),
            ),
            switch_serial=SerialPortConfig(
                port=str(serial_cfg["switch"]["port"]),
                baudrate=int(serial_cfg["switch"]["baudrate"]),
            ),
            server=ServerConfig(
                ip=str(server_cfg["ip"]),
                login=str(server_cfg["login"]),
                password=str(server_cfg["password"]),
                image_path=str(server_cfg["image_path"]),
            ),
            dut=DutConfig(
                final_ip=str(dut_cfg["final_ip"]),
                login=str(dut_cfg["login"]),
                password=str(dut_cfg["password"]),
                utils_path=str(dut_cfg["utils_path"]),
                tmp_path=str(dut_cfg["tmp_path"]),
                image_file=str(dut_cfg.get("image_file", "deploy-lsbb-1.1.1-20260324.sh")),
                em_prompt=str(dut_cfg.get("em_prompt", "sh-5.2#")),
            ),
            switch=SwitchConfig(
                ip=str(switch_cfg["ip"]),
                prompt=str(switch_cfg["prompt"]),
                image_file=str(switch_cfg.get("image_file", "telesat_lsbb-r0.itb")),
            ),
            sonic=SonicConfig(
                login=str(sonic_cfg["login"]),
                password=str(sonic_cfg["password"]),
                prompt=str(sonic_cfg.get("prompt", "admin@sonic:~$")),
                image_file=str(sonic_cfg.get("image_file", "sonic-marvell-arm64.bin")),
            ),
            prompts=PromptConfig(
                u_boot=str(prompts_cfg["u_boot"]),
                switch=str(prompts_cfg["switch"]),
            ),
            timeouts=TimeoutConfig(
                serial_open_seconds=int(timeouts_cfg["serial_open_seconds"]),
                prompt_wait_seconds=int(timeouts_cfg["prompt_wait_seconds"]),
                boot_interrupt_seconds=int(timeouts_cfg["boot_interrupt_seconds"]),
                uboot_boot_seconds=int(timeouts_cfg.get("uboot_boot_seconds", timeouts_cfg.get("first_boot_seconds", 300))),
                emergency_boot_seconds=int(timeouts_cfg.get("emergency_boot_seconds", max(int(timeouts_cfg.get("sonic_boot_seconds", timeouts_cfg.get("first_boot_seconds", 300))) * 2, 600))),
                first_boot_seconds=int(timeouts_cfg.get("first_boot_seconds", timeouts_cfg.get("uboot_boot_seconds", 300))),
                sonic_boot_seconds=int(timeouts_cfg.get("sonic_boot_seconds", timeouts_cfg.get("first_boot_seconds", timeouts_cfg.get("uboot_boot_seconds", 300)))),
            ),
            db=(
                DbConfig(
                    db_type=str(db_cfg.get("type", "sqlite")),
                    path=str(db_cfg["path"]),
                    table=str(db_cfg.get("table", "dig_board_macs")),
                    serial_column=str(db_cfg.get("serial_column", "dig_sn")),
                    mac_column_format=str(db_cfg.get("mac_column_format", "mac{index}")),
                    mac_count=int(db_cfg.get("mac_count", 16)),
                    seed_mac=normalize_mac(str(db_cfg.get("seed_mac", "00:00:00:00:00:00"))),
                    auto_create=bool(db_cfg.get("auto_create", True)),
                )
                if db_cfg is not None
                else None
            ),
        )


def load_config(path: pathlib.Path) -> AppConfig:
    try:
        payload = load_simple_yaml(path)
        return AppConfig.from_mapping(payload)
    except KeyError as exc:
        raise ConfigError(f"Missing configuration key: {exc}") from exc


def ensure_logs_dir(base_dir: pathlib.Path) -> pathlib.Path:
    logs_dir = base_dir / "logs"
    logs_dir.mkdir(exist_ok=True)
    return logs_dir


def timestamped_log_path(base_dir: pathlib.Path, name: str) -> pathlib.Path:
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    return ensure_logs_dir(base_dir) / f"{stamp}-{name}.log"


def boot_stop_bytes(mode: str) -> bytes:
    normalized = mode.lower()
    if normalized == "enter":
        return b"\r"
    if normalized == "space":
        return b" "
    if normalized == "ctrl-c":
        return b"\x03"
    raise ValueError(f"Unsupported boot stop key mode: {mode}")


def compile_switch_prompt_pattern(prompt_text: str) -> re.Pattern[str]:
    prompt = prompt_text.strip()
    if prompt.endswith(">"):
        stem = re.escape(prompt.rstrip(">"))
        return re.compile(r"(?:^|\n)\s*" + stem + r">+\s*$", re.IGNORECASE | re.MULTILINE)
    return re.compile(r"(?:^|\n)\s*" + re.escape(prompt) + r"\s*$", re.IGNORECASE | re.MULTILINE)


class SerialSession:
    def __init__(self, name: str, config: SerialPortConfig, log_path: pathlib.Path, open_timeout: int) -> None:
        self.name = name
        self.config = config
        self.log_path = log_path
        self.buffer = ""
        self._ansi_carry = ""
        self.log_file = self.log_path.open("ab")
        deadline = time.monotonic() + open_timeout
        last_error: Exception | None = None
        self.serial: serial.Serial | None = None

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

    def __enter__(self) -> "SerialSession":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
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

    def log_event(self, level: str, message: str) -> None:
        self.log_file.write(f"\n[{level}] {message}\n".encode("utf-8", errors="replace"))
        self.log_file.flush()

    def send_line(self, text: str) -> None:
        self.write(text.encode("utf-8") + b"\r")

    def poll(self) -> str:
        assert self.serial is not None
        data = self.serial.read(self.serial.in_waiting or 1)
        if not data:
            return ""
        text = data.decode("utf-8", errors="replace")
        sanitized, self._ansi_carry = sanitize_uart_text_stream(text, self._ansi_carry)
        if sanitized:
            self.log_file.write(sanitized.encode("utf-8", errors="replace"))
            self.log_file.flush()
            self.buffer += sanitized
        return sanitized

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
            segment = self.buffer[scan_from:]
            match = pattern.search(segment)
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
    description: str | None = None,
) -> None:
    if description:
        info(f"{session.name}: {description}")
    start_pos = len(session.buffer)
    session.send_line(command)
    session.wait_for_pattern(prompt_pattern, timeout=timeout, label=f"command completion for: {command}", start_pos=start_pos)


def run_command_capture(
    session: SerialSession,
    command: str,
    prompt_pattern: re.Pattern[str],
    timeout: int,
    description: str | None = None,
) -> str:
    if description:
        info(f"{session.name}: {description}")
    start_pos = len(session.buffer)
    session.send_line(command)
    session.wait_for_pattern(prompt_pattern, timeout=timeout, label=f"command completion for: {command}", start_pos=start_pos)
    return session.buffer[start_pos:]


def run_command_wait_text(
    session: SerialSession,
    command: str,
    expected_text: str,
    timeout: int,
    description: str | None = None,
    progress_label: str | None = None,
) -> str:
    if description:
        info(f"{session.name}: {description}")
    start_pos = len(session.buffer)
    session.send_line(command)
    deadline = time.monotonic() + timeout
    progress = None
    last_progress_second = 0
    if progress_label and tqdm is not None:
        progress = tqdm(total=timeout, desc=progress_label, unit="s", leave=True, dynamic_ncols=True)
    try:
        while time.monotonic() < deadline:
            session.poll()
            segment = session.buffer[start_pos:]
            if expected_text in segment:
                if progress is not None and last_progress_second < timeout:
                    progress.update(timeout - last_progress_second)
                return segment
            if progress is not None:
                elapsed_seconds = min(timeout, int(time.monotonic() - (deadline - timeout)))
                if elapsed_seconds > last_progress_second:
                    progress.update(elapsed_seconds - last_progress_second)
                    last_progress_second = elapsed_seconds
            time.sleep(0.05)
    finally:
        if progress is not None:
            progress.close()
    raise TimeoutError(f"Timed out waiting for command completion for: {command} on {session.name} ({session.config.port})")
    return session.buffer[start_pos:]


def run_command_wait_pattern(
    session: SerialSession,
    command: str,
    expected_pattern: re.Pattern[str],
    timeout: int,
    description: str | None = None,
) -> str:
    if description:
        info(f"{session.name}: {description}")
    start_pos = len(session.buffer)
    session.send_line(command)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        session.poll()
        segment = session.buffer[start_pos:]
        if expected_pattern.search(segment):
            return segment
        time.sleep(0.05)
    raise TimeoutError(f"Timed out waiting for command completion for: {command} on {session.name} ({session.config.port})")


def run_ping_check(session: SerialSession, command: str, success_text: str, timeout: int, description: str | None = None) -> str:
    if description:
        info(f"{session.name}: {description}")
    start_pos = len(session.buffer)
    session.send_line(command)
    session.wait_for_pattern(
        re.compile(re.escape(success_text)),
        timeout=timeout,
        label=f"ping success for: {command}",
        start_pos=start_pos,
    )
    return session.buffer[start_pos:]


def run_command_with_optional_password(
    session: SerialSession,
    command: str,
    shell_prompt: re.Pattern[str],
    timeout: int,
    password: str,
    password_prompt: re.Pattern[str] = PASSWORD_PATTERN,
) -> None:
    start_pos = len(session.buffer)
    session.send_line(command)
    result = session.wait_for_any_pattern(
        {
            "hostkey_yes": HOST_KEY_CONFIRM_YES_PATTERN,
            "hostkey_y": HOST_KEY_CONFIRM_Y_PATTERN,
            "password": password_prompt,
            "shell": shell_prompt,
        },
        timeout=timeout,
        label=f"command completion for: {command}",
        start_pos=start_pos,
    )
    if result in {"hostkey_yes", "hostkey_y"}:
        start_pos = len(session.buffer)
        hostkey_answer = "yes" if result == "hostkey_yes" else "y"
        session.log_event("INFO", f"Host key confirmation detected; sending {hostkey_answer!r}.")
        session.send_line(hostkey_answer)
        result = session.wait_for_any_pattern(
            {
                "password": password_prompt,
                "shell": shell_prompt,
            },
            timeout=timeout,
            label=f"host-key confirmation completion for: {command}",
            start_pos=start_pos,
        )
    if result == "password":
        start_pos = len(session.buffer)
        session.log_event("INFO", "Password prompt detected; sending configured password.")
        session.send_line(password)
        session.wait_for_pattern(shell_prompt, timeout=timeout, label=f"shell after password for: {command}", start_pos=start_pos)


def run_scp_download(
    session: SerialSession,
    server_login: str,
    server_ip: str,
    server_password: str,
    remote_path: str,
    destination: str,
    shell_prompt: re.Pattern[str],
    timeout: int,
    recursive: bool = False,
) -> None:
    recursive_flag = "-r " if recursive else ""
    if is_windows_style_path(remote_path):
        remote_spec = f'{server_login}@{server_ip}:"{remote_path}"'
    else:
        remote_spec = f"{server_login}@{server_ip}:{remote_path}"
    command = f"scp {recursive_flag}{remote_spec} {destination}"
    session.log_event("INFO", f"Starting SCP download: {remote_path} -> {destination}")
    run_command_with_optional_password(
        session=session,
        command=command,
        shell_prompt=shell_prompt,
        timeout=timeout,
        password=server_password,
    )
    session.log_event("INFO", f"Completed SCP download: {remote_path} -> {destination}")


def is_windows_style_path(raw_path: str) -> bool:
    return bool(pathlib.PureWindowsPath(raw_path).drive or "\\" in raw_path)


def ensure_linux_shell(
    session: SerialSession,
    username: str,
    password: str,
    shell_prompt: re.Pattern[str],
    boot_timeout: int,
    fresh: bool = False,
) -> None:
    start_pos = len(session.buffer) if fresh else None
    result = session.wait_for_any_pattern(
        {
            "shell": shell_prompt,
            "login": LOGIN_PATTERN,
        },
        timeout=boot_timeout,
        label="linux shell or login",
        start_pos=start_pos,
    )
    if result == "login":
        start_pos = len(session.buffer)
        session.send_line(username)
        session.wait_for_pattern(PASSWORD_PATTERN, timeout=30, label="password prompt", start_pos=start_pos)
        start_pos = len(session.buffer)
        session.send_line(password)
        session.wait_for_pattern(shell_prompt, timeout=boot_timeout, label="linux shell", start_pos=start_pos)


def ensure_sonic_shell(session: SerialSession, config: AppConfig, shell_prompt: re.Pattern[str]) -> None:
    boot_timeout = max(config.timeouts.emergency_boot_seconds, config.timeouts.sonic_boot_seconds)
    session.log_event("INFO", "SONiC boot timer completed; sending Enter twice and waiting for sonic login prompt.")

    start_pos = len(session.buffer)
    session.send_line("")
    session.send_line("")
    result = session.wait_for_any_pattern(
        {
            "login": SONIC_LOGIN_PATTERN,
            "shell": shell_prompt,
        },
        timeout=config.timeouts.prompt_wait_seconds,
        label="SONiC login prompt after Enter twice",
        start_pos=start_pos,
    )
    if result == "shell":
        return

    start_pos = len(session.buffer)
    session.log_event("INFO", f"SONiC login prompt detected; sending username from YAML: {config.sonic.login}.")
    session.send_line(config.sonic.login)
    session.wait_for_pattern(PASSWORD_PATTERN, timeout=30, label="SONiC password prompt", start_pos=start_pos)

    start_pos = len(session.buffer)
    session.log_event("INFO", "SONiC password prompt detected; sending password from YAML.")
    session.send_line(config.sonic.password)
    session.wait_for_pattern(shell_prompt, timeout=boot_timeout, label="SONiC shell prompt", start_pos=start_pos)


def wait_for_switch_reboot_request(session: SerialSession, timeout: int) -> None:
    start_pos = len(session.buffer)
    deadline = time.monotonic() + timeout
    progress = None
    last_progress_second = 0
    if tqdm is not None:
        progress = tqdm(total=timeout, desc="Switch reboot request", unit="s", leave=True, dynamic_ncols=True)
    try:
        while time.monotonic() < deadline:
            session.poll()
            segment = session.buffer[start_pos:]
            if SWITCH_REBOOT_REQUEST_PATTERN.search(segment):
                if progress is not None and last_progress_second < timeout:
                    progress.update(timeout - last_progress_second)
                return
            if SWITCH_BOOTM_WRONG_FORMAT_PATTERN.search(segment):
                message = "Switch bootm failed: Wrong Image Format for bootm command"
                session.log_event("ERROR", message)
                raise RuntimeError(message)
            if SWITCH_BOOTM_KERNEL_ERROR_PATTERN.search(segment):
                message = "Switch bootm failed: ERROR: can't get kernel image!"
                session.log_event("ERROR", message)
                raise RuntimeError(message)
            if progress is not None:
                elapsed_seconds = min(timeout, int(time.monotonic() - (deadline - timeout)))
                if elapsed_seconds > last_progress_second:
                    progress.update(elapsed_seconds - last_progress_second)
                    last_progress_second = elapsed_seconds
            time.sleep(0.05)
    finally:
        if progress is not None:
            progress.close()
    raise TimeoutError(f"Timed out waiting for switch reboot request on {session.name} ({session.config.port})")


def wait_with_operator_timer(total_seconds: int, label: str) -> None:
    info(f"{label}: waiting about {total_seconds // 60}:{total_seconds % 60:02d}")
    if tqdm is not None:
        for _ in tqdm(
            range(total_seconds),
            desc=label,
            unit="s",
            leave=True,
            dynamic_ncols=True,
        ):
            time.sleep(1)
        info(f"{label}: timer completed")
        return
    remaining = total_seconds
    while remaining > 0:
        chunk = 60 if remaining > 60 else remaining
        time.sleep(chunk)
        remaining -= chunk
        if remaining > 0:
            if remaining >= 60:
                info(f"{label}: {remaining // 60} minute(s) remaining ({remaining // 60}:{remaining % 60:02d})")
            else:
                info(f"{label}: {remaining} second(s) remaining (0:{remaining:02d})")
    info(f"{label}: timer completed")


def ensure_emergency_shell(session: SerialSession, boot_timeout: int, fresh: bool = False) -> None:
    start_pos = len(session.buffer) if fresh else None
    session.wait_for_pattern(
        EMERGENCY_SHELL_PATTERN,
        timeout=boot_timeout,
        label="emergency shell prompt",
        start_pos=start_pos,
    )


def ensure_emergency_maintenance_prompt(session: SerialSession, boot_timeout: int, fresh: bool = False) -> None:
    start_pos = len(session.buffer) if fresh else None
    session.wait_for_pattern(
        EMERGENCY_MAINTENANCE_PATTERN,
        timeout=boot_timeout,
        label="emergency maintenance prompt",
        start_pos=start_pos,
    )


def validate_nxp_boot_markers(session: SerialSession) -> None:
    checks = [
        ("CLU DEV1 locked", NXP_CLU1_LOCKED_PATTERN),
        ("CLU DEV2 locked", NXP_CLU2_LOCKED_PATTERN),
        ("Switch ready", NXP_SWITCH_READY_PATTERN),
        ("FPGA ready", NXP_FPGA_READY_PATTERN),
    ]
    missing = [label for label, pattern in checks if not pattern.search(session.buffer)]
    if missing:
        message = "NXP boot validation failed; missing: " + ", ".join(missing)
        session.log_event("ERROR", message)
        raise BootValidationError(message)


def detect_nxp_uboot(
    session: SerialSession,
    autoboot_wait_timeout: int,
    prompt_timeout: int,
    stop_key: bytes,
) -> None:
    session.wait_for_pattern(AUTOBOOT_PATTERN, timeout=autoboot_wait_timeout, label="autoboot countdown")
    deadline = time.monotonic() + max(prompt_timeout, 6)
    while time.monotonic() < deadline:
        session.write(stop_key)
        try:
            session.wait_for_any_pattern(
                {
                    "prompt": UBOOT_PROMPT_PATTERN,
                    "banner": re.compile(r"U-Boot", re.IGNORECASE),
                },
                timeout=0.25,
                label="U-Boot prompt",
            )
            validate_nxp_boot_markers(session)
            return
        except TimeoutError:
            time.sleep(0.15)
    session.wait_for_any_pattern(
        {
            "prompt": UBOOT_PROMPT_PATTERN,
            "banner": re.compile(r"U-Boot", re.IGNORECASE),
        },
        timeout=prompt_timeout,
        label="U-Boot prompt",
    )
    validate_nxp_boot_markers(session)


def detect_switch_prompt(
    session: SerialSession,
    prompt_text: str,
    timeout: int,
    fresh: bool = False,
) -> re.Pattern[str]:
    pattern = compile_switch_prompt_pattern(prompt_text)
    start_pos = len(session.buffer) if fresh else None
    session.wait_for_pattern(pattern, timeout=timeout, label="switch prompt", start_pos=start_pos)
    return pattern


def monitor_boot_parallel(
    nxp: SerialSession,
    switch: SerialSession,
    switch_prompt_text: str,
    stop_key: bytes,
    timeout: int,
) -> tuple[bool, bool, re.Pattern[str]]:
    switch_pattern = compile_switch_prompt_pattern(switch_prompt_text)
    deadline = time.monotonic() + timeout
    nxp_captured = False
    switch_ready = False
    nxp_interrupting = False
    switch_interrupting = False
    interrupt_deadline = 0.0
    switch_interrupt_deadline = 0.0

    while time.monotonic() < deadline:
        nxp.poll()
        switch.poll()

        if not nxp_interrupting and AUTOBOOT_PATTERN.search(nxp.buffer):
            nxp_interrupting = True
            interrupt_deadline = time.monotonic() + 6

        if nxp_interrupting and not nxp_captured:
            nxp.write(stop_key)
            if UBOOT_PROMPT_PATTERN.search(nxp.buffer):
                validate_nxp_boot_markers(nxp)
                nxp_captured = True
                nxp_interrupting = False
            elif time.monotonic() > interrupt_deadline:
                nxp_interrupting = False

        if not switch_interrupting and AUTOBOOT_PATTERN.search(switch.buffer):
            switch_interrupting = True
            switch_interrupt_deadline = time.monotonic() + 6

        if switch_interrupting and not switch_ready:
            switch.write(stop_key)
            if switch_pattern.search(switch.buffer):
                switch_ready = True
                switch_interrupting = False
            elif time.monotonic() > switch_interrupt_deadline:
                switch_interrupting = False

        if switch_pattern.search(switch.buffer):
            switch_ready = True

        if nxp_captured and switch_ready:
            return True, True, switch_pattern

        time.sleep(0.1)

    return nxp_captured, switch_ready, switch_pattern


def remote_server_path(server: ServerConfig, filename: str) -> str:
    if is_windows_style_path(server.image_path):
        image_dir = pathlib.PureWindowsPath(server.image_path)
        if not image_dir.drive:
            raise ConfigError(f"Windows image_path must include a drive letter: {server.image_path}")
        return pathlib.PureWindowsPath(image_dir, filename).as_posix()
    image_path = server.image_path.strip().strip("/")
    if "/" in image_path:
        return f"/{image_path}/{filename}"
    return f"/home/{server.login}/{image_path}/{filename}"


def remote_utils_path(server: ServerConfig, dut: DutConfig) -> str:
    if is_windows_style_path(dut.utils_path):
        utils_dir = pathlib.PureWindowsPath(dut.utils_path)
        if not utils_dir.drive:
            raise ConfigError(f"Windows utils_path must include a drive letter: {dut.utils_path}")
        return utils_dir.as_posix()
    return f"/home/{server.login}/{dut.utils_path}"


def get_windows_ssh_server_status() -> str:
    try:
        completed = subprocess.run(
            ["sc", "query", "sshd"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except OSError as exc:
        raise ConfigError("Could not query Windows sshd service status.") from exc
    except subprocess.TimeoutExpired as exc:
        raise ConfigError("Timed out while querying Windows sshd service status.") from exc

    output = f"{completed.stdout}\n{completed.stderr}"
    if "STATE" in output:
        match = re.search(r"STATE\s*:\s*\d+\s+([A-Z_]+)", output)
        if match:
            return match.group(1)
        return "UNKNOWN"
    if completed.returncode == 1060 or "does not exist as an installed service" in output.lower():
        return "NOT_INSTALLED"
    return "UNKNOWN"


def ensure_windows_ssh_server_ready(config: AppConfig) -> None:
    if not is_windows_style_path(config.server.image_path):
        return
    status = get_windows_ssh_server_status()
    info(f"Windows SSH server status: {status}")
    if status != "RUNNING":
        raise RuntimeError(
            "Windows SSH server is required for DUT-side SCP pulls, but sshd status is "
            f"{status}. Start or install the OpenSSH Server service before provisioning."
        )


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
    return compact_mac(text)


def create_db_table_if_needed(connection: sqlite3.Connection, db_config: DbConfig) -> None:
    if not db_config.auto_create:
        return

    table = validate_sql_identifier(db_config.table, "db.table")
    serial_column = validate_sql_identifier(db_config.serial_column, "db.serial_column")
    mac_columns = mac_column_names(db_config)
    mac_fields = ", ".join(f"{column} TEXT" for column in mac_columns)
    connection.execute(
        f"CREATE TABLE IF NOT EXISTS {table} ({serial_column} TEXT PRIMARY KEY, {mac_fields})"
    )
    connection.commit()


def open_db_connection(config_path: pathlib.Path, db_config: DbConfig) -> sqlite3.Connection:
    return connection


def fetch_serial_row(connection: sqlite3.Connection, db_config: DbConfig, dig_sn: str) -> sqlite3.Row | None:
    return row


def find_latest_saved_mac(connection: sqlite3.Connection, db_config: DbConfig) -> str | None:
    return ":".join(packed[index:index + 2] for index in range(0, 12, 2))


def existing_row_macs(row: sqlite3.Row, db_config: DbConfig) -> list[str | None]:
    return [normalize_optional_mac(row[column]) for column in mac_column_names(db_config)]


def build_mac_block(base_mac: str, count: int) -> list[str]:
    return [normalize_mac(mac_plus(base_mac, offset)) for offset in range(count)]


def infer_base_mac_from_existing(row_macs: list[str | None]) -> str | None:
    return None


def upsert_serial_row(connection: sqlite3.Connection, db_config: DbConfig, dig_sn: str, macs: list[str]) -> None:
    connection.commit()


def print_mac_block(dig_sn: str, macs: list[str]) -> None:
    info(f"DIG SN: {dig_sn}")
    for index, mac in enumerate(macs, start=1):
        print(f"mac{index:02d}: {mac}", flush=True)


def run_gen_mac_mode(config: AppConfig, args: argparse.Namespace, config_path: pathlib.Path) -> int:
    return 0


@dataclass(frozen=True)
class ProvisionArgs:
    mac_notice: str | None


def log_selected_macs(sessions: list[SerialSession], provision: ProvisionArgs) -> None:
                session.log_event("INFO", f"mac{index:02d}: {mac}")


def build_gen_mac_command(dig_sn: str) -> str:
    return f"python .\\lscriptwin.py --mode gen_mac --dig_sn {dig_sn}"


def resolve_provision_macs_from_db(config: AppConfig, args: argparse.Namespace) -> tuple[str, str, str, tuple[str, ...], str]:
        return normalize_mac(new_macs[0]), normalize_mac(new_macs[1]), dig_sn, new_macs, "New serial number detected. Created and saved a new 16-MAC block."


def build_provision_args(config: AppConfig, args: argparse.Namespace) -> ProvisionArgs:
    )


def burn_nxp_macs(session: SerialSession, provision: ProvisionArgs, timeout: int) -> None:
    )


def burn_switch_uboot_mac(session: SerialSession, prompt_pattern: re.Pattern[str], provision: ProvisionArgs, timeout: int) -> None:
    )


def configure_emergency_linux(
    session: SerialSession,
    config: AppConfig,
    provision: ProvisionArgs,
) -> None:
    emergency_prompt_text = config.dut.em_prompt.rstrip() + " "
    emergency_wait_timeout = config.timeouts.emergency_boot_seconds
    ensure_emergency_maintenance_prompt(session, emergency_wait_timeout, fresh=True)
    info("NXP: emergency maintenance prompt detected, sending Enter")
    session.send_line("")
    ensure_emergency_shell(session, emergency_wait_timeout, fresh=False)
    network_check_command = (
        f"ifconfig eth0 {config.dut.final_ip} netmask 255.255.255.0 up ; "
        f"ping {config.server.ip} -c1"
    )
    info(f"NXP: Configure DUT IP and verify host reachability: {network_check_command}")
    session.log_event("INFO", f"Starting network check: {network_check_command}")
    ping_output = run_command_wait_text(
        session,
        network_check_command,
        PING_SUCCESS_TEXT,
        max(config.timeouts.prompt_wait_seconds, 90),
        "Configure DUT IP and verify host reachability",
    )
    if not PING_SUCCESS_PATTERN.search(ping_output):
        session.log_event("ERROR", f"Ping to {config.server.ip} failed; stopping before file transfer.")
        raise RuntimeError(f"Ping to {config.server.ip} failed; stopping before file transfer.")
    ok("Ping success detected, starting SCP pull from Windows host")
    session.log_event("INFO", f"Ping success detected, starting SCP pull to /{config.dut.tmp_path}")
    start_pos = len(session.buffer)
    session.send_line("")
    session.wait_for_pattern(
        re.compile(re.escape(emergency_prompt_text)),
        timeout=config.timeouts.prompt_wait_seconds,
        label="emergency shell prompt after first Enter",
        start_pos=start_pos,
    )
    start_pos = len(session.buffer)
    session.send_line("")
    session.wait_for_pattern(
        re.compile(re.escape(emergency_prompt_text)),
        timeout=config.timeouts.prompt_wait_seconds,
        label="emergency shell prompt after second Enter",
        start_pos=start_pos,
    )
    time.sleep(1)
    run_scp_download(
        session=session,
        server_login=config.server.login,
        server_ip=config.server.ip,
        server_password=config.server.password,
        remote_path=remote_server_path(config.server, provision.deploy_script),
        destination=f"/{config.dut.tmp_path}",
        shell_prompt=re.compile(re.escape(emergency_prompt_text)),
        timeout=max(config.timeouts.emergency_boot_seconds, 120),
    )
    run_command_wait_text(session, f"cd /{config.dut.tmp_path}", emergency_prompt_text, config.timeouts.prompt_wait_seconds, "Change to the temporary directory")
    ls_output = run_command_wait_text(
        session,
        "ls -all",
        emergency_prompt_text,
        config.timeouts.prompt_wait_seconds,
        "List the temporary directory",
    )
    if provision.deploy_script not in ls_output:
        session.log_event("ERROR", f"Expected {provision.deploy_script} in /{config.dut.tmp_path}, but it was not listed after transfer.")
        raise RuntimeError(f"Expected {provision.deploy_script} in /{config.dut.tmp_path}, but it was not listed after transfer.")
    session.log_event("INFO", f"Verified uploaded image in /{config.dut.tmp_path}: {provision.deploy_script}")
    run_command_wait_text(
        session,
        f"sh ./{provision.deploy_script}",
        emergency_prompt_text,
        max(config.timeouts.emergency_boot_seconds * 4, 600),
        "Run the LSBB deploy script",
        progress_label="NXP deploy script",
    )


def boot_nxp_from_uboot(session: SerialSession) -> None:
    info("DUT resetting: sending U-Boot boot command on NXP")
    session.log_event("INFO", "DUT resetting from NXP U-Boot via boot command.")
    time.sleep(1)
    session.send_line("boot")


def reboot_nxp_from_linux(session: SerialSession, config: AppConfig, reason: str, delay_seconds: int = 0) -> None:
    ensure_linux_shell(session, config.dut.login, config.dut.password, ROOT_SHELL_PATTERN, config.timeouts.emergency_boot_seconds)
    if delay_seconds > 0:
        time.sleep(delay_seconds)
    info(f"DUT resetting: {reason}")
    session.log_event("INFO", f"DUT resetting: {reason}")
    start_pos = len(session.buffer)
    session.send_line("reboot")
    session.wait_for_pattern(
        NXP_REBOOT_TRANSITION_PATTERN,
        timeout=max(config.timeouts.prompt_wait_seconds, 60),
        label="NXP reboot transition",
        start_pos=start_pos,
    )


def configure_installed_linux(session: SerialSession, config: AppConfig) -> None:
    shell = ROOT_SHELL_PATTERN
    ensure_linux_shell(session, config.dut.login, config.dut.password, shell, config.timeouts.emergency_boot_seconds, fresh=True)
    run_command(
        session,
        "nmcli con add type ethernet ifname fm1-mac5 con-name fm1-mac5-static ipv4.addresses "
        f"{config.dut.final_ip}/24 ipv4.method manual",
        shell,
        config.timeouts.prompt_wait_seconds,
        "Create the persistent DUT management connection",
    )
    run_command(session, "nmcli con up fm1-mac5-static", shell, config.timeouts.prompt_wait_seconds, "Bring up the DUT management connection")
    run_command(
        session,
        "nmcli con mod fm1-mac5-static connection.autoconnect yes",
        shell,
        config.timeouts.prompt_wait_seconds,
        "Enable autoconnect for the DUT management connection",
    )
    time.sleep(2)
    run_command(session, "", shell, config.timeouts.prompt_wait_seconds, "Confirm NXP shell prompt after autoconnect change")
    run_command(session, "", shell, config.timeouts.prompt_wait_seconds, "Confirm NXP shell prompt after second Enter")


def stage_switch_images(
    session: SerialSession,
    switch_session: SerialSession,
    config: AppConfig,
    provision: ProvisionArgs,
) -> re.Pattern[str]:
    shell = ROOT_SHELL_PATTERN
    ensure_linux_shell(session, config.dut.login, config.dut.password, shell, config.timeouts.emergency_boot_seconds, fresh=True)
    run_scp_download(
        session=session,
        server_login=config.server.login,
        server_ip=config.server.ip,
        server_password=config.server.password,
        remote_path=remote_server_path(config.server, provision.switch_image),
        destination=f"/{config.dut.tmp_path}",
        shell_prompt=shell,
        timeout=max(config.timeouts.emergency_boot_seconds, 120),
    )
    run_scp_download(
        session=session,
        server_login=config.server.login,
        server_ip=config.server.ip,
        server_password=config.server.password,
        remote_path=remote_server_path(config.server, provision.switch_itb),
        destination=f"/{config.dut.tmp_path}",
        shell_prompt=shell,
        timeout=max(config.timeouts.emergency_boot_seconds, 120),
    )
    run_command(session, f"cd /{config.dut.tmp_path}", shell, config.timeouts.prompt_wait_seconds, "Change to the temporary directory")
    ls_output = run_command_capture(
        session,
        "ls -all",
        shell,
        config.timeouts.prompt_wait_seconds,
        "List the temporary directory after switch image transfer",
    )
    missing_files = [name for name in (provision.switch_image, provision.switch_itb) if name not in ls_output]
    if missing_files:
        missing_text = ", ".join(missing_files)
        session.log_event(
            "ERROR",
            f"Expected switch transfer files in /{config.dut.tmp_path}, but missing: {missing_text}",
        )
        raise RuntimeError(
            f"Expected switch transfer files in /{config.dut.tmp_path}, but missing: {missing_text}"
        )
    session.log_event(
        "INFO",
        f"Verified switch transfer files in /{config.dut.tmp_path}: {provision.switch_image}, {provision.switch_itb}",
    )
    run_command(
        session,
        "nmcli con add type ethernet ifname fm1-mac10 con-name fm1-mac10-static ipv4.addresses "
        "192.168.2.1/24 ipv4.method manual",
        shell,
        config.timeouts.prompt_wait_seconds,
        "Create the NXP-to-switch management connection",
    )
    run_command(session, "nmcli con up fm1-mac10-static", shell, config.timeouts.prompt_wait_seconds, "Bring up the NXP-to-switch connection")
    run_command(session, "nmcli con show", shell, config.timeouts.prompt_wait_seconds, "Show active NetworkManager connections")
    switch_prompt = detect_switch_prompt(switch_session, config.prompts.switch, config.timeouts.uboot_boot_seconds)
    run_command(
        switch_session,
        "ping $serverip",
        switch_prompt,
        config.timeouts.prompt_wait_seconds,
        "Switch U-Boot: probe serverip from COM21",
    )
    run_command(session, f"cd /{config.dut.tmp_path}", shell, config.timeouts.prompt_wait_seconds, "Change to the temporary directory")
    run_command(session, "ps -ef | grep udpsvd", shell, config.timeouts.prompt_wait_seconds, "Check whether TFTP is already running")
    run_command(session, "ps -ef | grep httpserv", shell, config.timeouts.prompt_wait_seconds, "Check whether HTTP is already running")
    run_command(session, "busybox udpsvd -vE 0.0.0.0 69 tftpd . &", shell, config.timeouts.prompt_wait_seconds, "Start the TFTP server")
    run_command(session, "ps -ef | grep udpsvd", shell, config.timeouts.prompt_wait_seconds, "Confirm the TFTP server is active")
    run_command(session, "httpserv -p 80 &", shell, config.timeouts.prompt_wait_seconds, "Start the HTTP server")
    time.sleep(2)
    return switch_prompt


def install_switch_image(session: SerialSession, prompt_pattern: re.Pattern[str], config: AppConfig, provision: ProvisionArgs) -> None:
    commands = [
        f"setenv ethaddr {provision.switch_onie_mac}",
        f"setenv onie_install_url install_url=http://192.168.2.1/{provision.switch_image}",
        "saveenv",
        "tftpboot $onie_loadaddr $onie_image_name",
        "run onie_bootargs",
    ]
    for command in commands:
        run_command(session, command, prompt_pattern, max(config.timeouts.uboot_boot_seconds, 30), description=f"Switch install: {command}")
    info("Switch install: bootm $onie_loadaddr")
    session.log_event("INFO", "Switch install: bootm $onie_loadaddr")
    session.send_line("bootm $onie_loadaddr")


def reset_switch_from_nxp(session: SerialSession, timeout: int) -> None:
    run_command(session, "cd /root", ROOT_SHELL_PATTERN, timeout, "Return to /root before pulsing the switch reset line")
    run_command(session, "cpld w 0x45 0", ROOT_SHELL_PATTERN, timeout, "Assert the switch reset line")
    run_command(session, "cpld w 0x45 3", ROOT_SHELL_PATTERN, timeout, "Release the switch reset line")


def configure_sonic(session: SerialSession, config: AppConfig) -> None:
    shell = re.compile(re.escape(config.sonic.prompt))
    ensure_sonic_shell(session, config, shell)
    commands = [
        "sudo sonic-cfggen -w -j /usr/share/sonic/device/arm64-telesat_lsbb-r0/telesat-lsbb/default_config.json",
        "sudo config qos reload",
        f"sudo config interface ip add eth0 {config.switch.ip}/24",
        "sudo config save -y",
    ]
    for command in commands:
        run_command_with_optional_password(
            session=session,
            command=command,
            shell_prompt=shell,
            timeout=max(config.timeouts.emergency_boot_seconds, 60),
            password=config.sonic.password,
        )
        time.sleep(8)
    session.log_event("INFO", "Waiting for SONiC final 'System is ready' before management ping.")
    session.wait_for_pattern(
        SONIC_SYSTEM_READY_PATTERN,
        timeout=max(config.timeouts.sonic_boot_seconds, 120),
        label="SONiC final system ready after configuration",
    )


def verify_switch_management_ping(session: SerialSession, config: AppConfig) -> None:
    shell = re.compile(re.escape(config.sonic.prompt))
    session.log_event("INFO", "Waiting for switch 'System is ready' before management ping.")
    session.wait_for_pattern(
        SONIC_SYSTEM_READY_PATTERN,
        timeout=max(config.timeouts.sonic_boot_seconds, 120),
        label="switch system ready before management ping",
    )
    run_command_with_optional_password(
        session=session,
        command="sudo ip vrf exec mgmt ping 192.168.2.1 -c1",
        shell_prompt=shell,
        timeout=max(config.timeouts.emergency_boot_seconds, 60),
        password=config.sonic.password,
    )


def verify_nxp_ping_switch(session: SerialSession, config: AppConfig) -> None:
    ensure_linux_shell(session, config.dut.login, config.dut.password, ROOT_SHELL_PATTERN, config.timeouts.emergency_boot_seconds)
    run_command(
        session,
        f"ping {config.switch.ip} -c1",
        ROOT_SHELL_PATTERN,
        max(config.timeouts.prompt_wait_seconds, 30),
        "Verify NXP can reach the switch management IP",
    )


def copy_utils_and_run(session: SerialSession, config: AppConfig) -> None:
    if not config.dut.utils_path:
        return

    shell = ROOT_SHELL_PATTERN
    ensure_linux_shell(session, config.dut.login, config.dut.password, shell, config.timeouts.emergency_boot_seconds)
    run_scp_download(
        session=session,
        server_login=config.server.login,
        server_ip=config.server.ip,
        server_password=config.server.password,
        remote_path=remote_utils_path(config.server, config.dut),
        destination="/root/",
        shell_prompt=shell,
        timeout=max(config.timeouts.emergency_boot_seconds, 120),
        recursive=True,
    )


def run_detect_mode(config: AppConfig, args: argparse.Namespace, repo_root: pathlib.Path) -> int:
    nxp_log = timestamped_log_path(repo_root, "nxp")
    switch_log = timestamped_log_path(repo_root, "switch")
    info(f"Watching NXP console on {config.nxp.port}; log: {nxp_log}")
    if not args.skip_switch:
        info(f"Watching switch console on {config.switch_serial.port}; log: {switch_log}")

    try:
        with SerialSession("NXP", config.nxp, nxp_log, config.timeouts.serial_open_seconds) as nxp, SerialSession(
            "Switch",
            config.switch_serial,
            switch_log,
            config.timeouts.serial_open_seconds,
        ) as switch:
            if args.skip_switch:
                detect_nxp_uboot(
                    session=nxp,
                    autoboot_wait_timeout=max(config.timeouts.uboot_boot_seconds, config.timeouts.boot_interrupt_seconds),
                    prompt_timeout=config.timeouts.prompt_wait_seconds,
                    stop_key=boot_stop_bytes(args.boot_stop_key),
                )
                ok(f"Reached U-Boot on {config.nxp.port}")
                return 0

            nxp_ready, switch_ready, _ = monitor_boot_parallel(
                nxp=nxp,
                switch=switch,
                switch_prompt_text=config.prompts.switch,
                stop_key=boot_stop_bytes(args.boot_stop_key),
                timeout=max(config.timeouts.uboot_boot_seconds, config.timeouts.boot_interrupt_seconds),
            )
            if not nxp_ready:
                raise TimeoutError(f"Timed out waiting for NXP U-Boot on {config.nxp.port}")
            if not switch_ready:
                raise TimeoutError(f"Timed out waiting for switch prompt on {config.switch_serial.port}")
            ok(f"Reached U-Boot on {config.nxp.port}")
            ok(f"Reached switch prompt on {config.switch_serial.port}")
    except (serial.SerialException, TimeoutError, ValueError, RuntimeError) as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1

    return 0


def run_mac_only_mode(config: AppConfig, args: argparse.Namespace, repo_root: pathlib.Path) -> int:
    provision = build_provision_args(config, args)
    nxp_log = timestamped_log_path(repo_root, "nxp")
    switch_log = timestamped_log_path(repo_root, "switch")

    info(f"NXP base MAC: {provision.base_mac}")
    info(f"Switch U-Boot MAC: {provision.switch_uboot_mac}")
    info(f"NXP log: {nxp_log}")
    info(f"Switch log: {switch_log}")

    try:
        with SerialSession("NXP", config.nxp, nxp_log, config.timeouts.serial_open_seconds) as nxp, SerialSession(
            "Switch",
            config.switch_serial,
            switch_log,
            config.timeouts.serial_open_seconds,
        ) as switch:
            log_selected_macs([nxp], provision)
            nxp_ready, switch_ready, switch_prompt = monitor_boot_parallel(
                nxp=nxp,
                switch=switch,
                switch_prompt_text=config.prompts.switch,
                stop_key=boot_stop_bytes(args.boot_stop_key),
                timeout=max(config.timeouts.uboot_boot_seconds, config.timeouts.boot_interrupt_seconds),
            )
            if not nxp_ready:
                raise TimeoutError(f"Timed out waiting for NXP U-Boot on {config.nxp.port}")
            if not switch_ready:
                raise TimeoutError(f"Timed out waiting for switch prompt on {config.switch_serial.port}")

            ok("NXP U-Boot is ready")
            ok("Switch U-Boot is ready")

            burn_nxp_macs(nxp, provision, config.timeouts.prompt_wait_seconds)
            burn_switch_uboot_mac(switch, switch_prompt, provision, config.timeouts.prompt_wait_seconds)
    except (serial.SerialException, TimeoutError, ValueError, RuntimeError) as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1

    ok("MAC programming completed")
    return 0


def run_provision_mode(config: AppConfig, args: argparse.Namespace, repo_root: pathlib.Path) -> int:
    provision = build_provision_args(config, args)
    nxp_log = timestamped_log_path(repo_root, "nxp")
    switch_log = timestamped_log_path(repo_root, "switch")

    ensure_windows_ssh_server_ready(config)
    info(f"NXP base MAC: {provision.base_mac}")
    info(f"Switch U-Boot MAC: {provision.switch_uboot_mac}")
    info(f"Switch ONIE MAC: {provision.switch_onie_mac}")
    info(f"NXP log: {nxp_log}")
    info(f"Switch log: {switch_log}")

    try:
        with SerialSession("NXP", config.nxp, nxp_log, config.timeouts.serial_open_seconds) as nxp, SerialSession(
            "Switch",
            config.switch_serial,
            switch_log,
            config.timeouts.serial_open_seconds,
        ) as switch:
            log_selected_macs([nxp], provision)
            info("Stopping NXP autoboot and entering U-Boot")
            nxp_ready, switch_ready, switch_prompt = monitor_boot_parallel(
                nxp=nxp,
                switch=switch,
                switch_prompt_text=config.prompts.switch,
                stop_key=boot_stop_bytes(args.boot_stop_key),
                timeout=max(config.timeouts.uboot_boot_seconds, config.timeouts.boot_interrupt_seconds),
            )
            if not nxp_ready:
                raise TimeoutError(f"Timed out waiting for NXP U-Boot on {config.nxp.port}")
            if not switch_ready:
                raise TimeoutError(f"Timed out waiting for switch prompt on {config.switch_serial.port}")
            ok("NXP U-Boot is ready")
            ok("Switch U-Boot is ready")

            burn_nxp_macs(nxp, provision, config.timeouts.prompt_wait_seconds)
            burn_switch_uboot_mac(switch, switch_prompt, provision, config.timeouts.prompt_wait_seconds)
            boot_nxp_from_uboot(nxp)
            configure_emergency_linux(nxp, config, provision)

            reboot_nxp_from_linux(nxp, config, "rebooting after deploy script completed")
            configure_installed_linux(nxp, config)

            reboot_nxp_from_linux(nxp, config, "rebooting after persistent DUT IP configuration", delay_seconds=1)
            switch_prompt = stage_switch_images(nxp, switch, config, provision)

            info("Switch U-Boot serverip probe completed; starting ONIE image install")
            install_switch_image(switch, switch_prompt, config, provision)

            info(f"Waiting for switch reboot request on {config.switch_serial.port} after bootm")
            wait_for_switch_reboot_request(switch, 240)
            time.sleep(5)
            info("Switch reboot request detected; pulsing reset from NXP")
            reset_switch_from_nxp(nxp, config.timeouts.prompt_wait_seconds)
            wait_with_operator_timer(config.timeouts.sonic_boot_seconds, "Switch SONiC first boot")
            configure_sonic(switch, config)

            if not provision.skip_utils:
                copy_utils_and_run(nxp, config)
            else:
                warn("Skipping LSBB_Utils copy and run because --skip-utils was provided")
            verify_switch_management_ping(switch, config)
            verify_nxp_ping_switch(nxp, config)
    except (serial.SerialException, TimeoutError, ValueError, RuntimeError) as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1

    ok("Full installation and board configuration completed successfully")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Windows-driven serial automation for Lscript.")
    parser.add_argument(
        "--config",
        default="script_setup.yaml",
        help="Path to the YAML configuration file.",
    )
    parser.add_argument(
        "--mode",
        choices=["detect", "mac-only", "provision", "gen_mac"],
        default="detect",
        help="Run the quick prompt detector, MAC-only flow, full provisioning flow, or DB-backed MAC generation.",
    )
    parser.add_argument(
        "--boot-stop-key",
        choices=["enter", "space", "ctrl-c"],
        default="enter",
        help="Key sent to stop autoboot on the NXP console.",
    )
    parser.add_argument(
        "--skip-switch",
        action="store_true",
        help="Only detect and stop autoboot on the NXP console.",
    )
    parser.add_argument("--base-mac", help="Runtime MAC written to 'mac 0' in NXP U-Boot.")
    parser.add_argument(
        "--switch-uboot-mac",
        help="Switch U-Boot ethaddr. Defaults to base-mac plus 1.",
    )
    parser.add_argument(
        "--switch-onie-mac",
        help="Switch ONIE ethaddr. Defaults to base-mac plus 1.",
    )
    parser.add_argument(
        "--deploy-script",
        default=None,
        help="Deploy script filename under the configured image folder.",
    )
    parser.add_argument(
        "--switch-image",
        default=None,
        help="Switch image filename under the configured image folder.",
    )
    parser.add_argument(
        "--switch-itb",
        default=None,
        help="Extra switch ITB filename copied to the DUT.",
    )
    parser.add_argument(
        "--skip-utils",
        action="store_true",
        help="Do not copy LSBB_Utils or run its test suite.",
    )
    parser.add_argument("--dig_sn", help="DIG board serial number used by gen_mac or DB-backed provision mode.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    repo_root = pathlib.Path(__file__).resolve().parent
    config_path = (repo_root / args.config).resolve()

    try:
        config = load_config(config_path)
        if args.mode == "gen_mac":
            return run_gen_mac_mode(config, args, config_path)
        if args.mode == "mac-only":
            return run_mac_only_mode(config, args, repo_root)
        if args.mode == "provision":
            return run_provision_mode(config, args, repo_root)
        return run_detect_mode(config, args, repo_root)
    except (OSError, ConfigError, ValueError, sqlite3.Error) as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
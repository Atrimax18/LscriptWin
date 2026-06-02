from __future__ import annotations

import argparse
import datetime as dt
import pathlib
import re
import socket
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Any, Optional

import serial

try:
    import paramiko
except ImportError:
    paramiko = None

try:
    from tqdm import tqdm
except ImportError:
    tqdm = None



AUTOBOOT_PATTERN = re.compile(r"Hit any key to stop autoboot:", re.IGNORECASE)
LOGIN_PATTERN = re.compile(r"(?:^|\n).{0,40}login:\s*$", re.IGNORECASE | re.MULTILINE)
NXP_LOGIN_WITH_TRAILING_OUTPUT_PATTERN = re.compile(r"(?:^|[\r\n]).{0,80}login:\s*(?:$|[\r\n]|\[)", re.IGNORECASE | re.MULTILINE)
PASSWORD_PATTERN = re.compile(r"(?:^|\n).{0,80}password(?: for [^:]+)?:\s*$", re.IGNORECASE | re.MULTILINE)
USERNAME_PROMPT_PATTERN = re.compile(r"(?:^|[\r\n])[^\r\n]*(?:login|username)\s*:\s*$", re.IGNORECASE | re.MULTILINE)
NXP_CLU1_LOCKED_PATTERN = re.compile(r"CLU: DEV1: design: \[DV1_V3\.3\] \| PLL Status - Locked", re.IGNORECASE)
NXP_CLU2_LOCKED_PATTERN = re.compile(r"CLU: DEV2: design: \[DV2_V3\.3\] \| PLL Status - Locked", re.IGNORECASE)
NXP_SWITCH_READY_PATTERN = re.compile(r"(?:^|\n)Switch ready\s*$", re.IGNORECASE | re.MULTILINE)
NXP_FPGA_READY_PATTERN = re.compile(r"(?:^|\n)FPGA ready\s*$", re.IGNORECASE | re.MULTILINE)
UBOOT_PROMPT_PATTERN = re.compile(r"(?:^|\n)\s*=>\s*$", re.MULTILINE)
MAC_SAVE_SUCCESS_PATTERN = re.compile(r"Programming passed\.", re.IGNORECASE)
ROOT_SHELL_PATTERN = re.compile(r"(?:^|\n).{0,120}#\s*$", re.MULTILINE)
GENERIC_SHELL_PATTERN = re.compile(r"[#>$]\s*$", re.MULTILINE)
ANSI_ESCAPE_PATTERN = re.compile(r"\x1B\[[0-?]*[ -/]*[@-~]")
PING_SUCCESS_PATTERN = re.compile(r"1 packets transmitted,\s*1 packets received,\s*0% packet loss", re.IGNORECASE)
DEPLOYMENT_COMPLETE_PATTERN = re.compile(r"\[4/4\]\s+Deployment complete!", re.IGNORECASE)
DEPLOYMENT_RUN_REBOOT_PATTERN = re.compile(r"Run:\s*reboot", re.IGNORECASE)
NXP_REDIS_STARTED_PATTERN = re.compile(r"Started\s+Redis\b.*Data\s+Store\.?", re.IGNORECASE)
NXP_LOGIN_BANNER_PATTERN = re.compile(r"Satixfy[\s\S]{0,200}Landing\s+Station[\s\S]{0,200}Distro", re.IGNORECASE)
NXP_OPENSSH_KEYGEN_DONE_PATTERN = re.compile(r"Finished\s+OpenSSH\s+Key\s+Generation\.?", re.IGNORECASE)
HOST_KEY_CONFIRM_YES_PATTERN = re.compile(r"are you sure you want to continue connecting", re.IGNORECASE)
HOST_KEY_CONFIRM_Y_PATTERN = re.compile(r"do you want to continue connecting\?\s*\(y/n\)", re.IGNORECASE)
EMERGENCY_MAINTENANCE_PATTERN = re.compile(r"You\s+are\s+in\s+emergency\s+mode", re.IGNORECASE | re.DOTALL,
)
SWITCH_ENV_RESET_PATTERN = re.compile(r"## Resetting to default environment", re.IGNORECASE)
SWITCH_SAVEENV_OK_PATTERN = re.compile(r"(^|\n)OK\s*$", re.IGNORECASE | re.MULTILINE)
NXP_REBOOT_TRANSITION_PATTERN = re.compile(r"(?:reboot: Restarting system|NOTICE:|(?:^|\n).{0,40}login:\s*$)", re.IGNORECASE | re.MULTILINE)
DIG_SN_PATTERN = re.compile(r"^(?:CLS|MLS)DM-\d{2}-\d{4}-(?:\d{6}|[A-Z]\d)-\d{3,5}$")   #re.compile(r"^[A-Z]{5}-\d{2}-\d{4}-(?:\d{6}|[A-Z]\d)-\d{3,5}$")
SQL_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
LOG_ROOT = pathlib.Path(r"C:\Logs\Deployment")
UART_BUFFER_MAX_CHARS = 100_000
UART_BUFFER_TRIM_TO_CHARS = 50_000


class BootValidationError(RuntimeError):
    pass

class ConfigError(RuntimeError):
    pass


def info(message: str) -> None:
    print(f"[INFO] {message}", flush=True)


def ok(message: str) -> None:
    print(f"[OK] {message}", flush=True)


def warn(message: str) -> None:
    print(f"[WARN] {message}", flush=True)


def stage(number: int, message: str) -> None:
    print(f"\n[STAGE {number}] {message}", flush=True)


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


def sanitize_log_folder_name(value: str | None) -> str:
    if not value:
        return "NO_DIG_SN"
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", value.strip().upper())
    cleaned = cleaned.strip(" ._")
    return cleaned or "NO_DIG_SN"


def run_dir_for_dig_sn(dig_sn: str) -> pathlib.Path:
    folder = sanitize_log_folder_name(dig_sn)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir = LOG_ROOT / folder / stamp
    run_dir.mkdir(parents=True, exist_ok=True)
    return run_dir


def timestamped_log_path(run_dir: pathlib.Path, name: str) -> pathlib.Path:
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    return run_dir / f"{stamp}-{name}.log"


def boot_stop_bytes(mode: str) -> bytes:
    normalized = mode.lower()
    if normalized == "enter":
        return b"\r"
    if normalized == "space":
        return b" "
    if normalized == "ctrl-c":
        return b"\x03"
    raise ValueError(f"Unsupported boot stop key mode: {mode}")


def compile_shell_prompt_pattern(prompt_text: str) -> re.Pattern[str]:
    return re.compile(r"(?:^|[\r\n])\s*" + re.escape(prompt_text.strip()) + r"(?=\s|$)", re.MULTILINE)


def compile_login_prompt_pattern(prompt_text: str) -> re.Pattern[str]:
    prompt = prompt_text.strip()
    if not prompt:
        return LOGIN_PATTERN
    return re.compile(r"(?:^|[\r\n])\s*" + re.escape(prompt) + r"\s*$", re.IGNORECASE | re.MULTILINE)


def compile_switch_prompt_pattern(prompt_text: str) -> re.Pattern[str]:
    prompt = prompt_text.strip()
    if prompt.endswith(">"):
        stem = re.escape(prompt.rstrip(">"))
        return re.compile(r"(?:^|\n)\s*" + stem + r">+\s*$", re.IGNORECASE | re.MULTILINE)
    return re.compile(r"(?:^|\n)\s*" + re.escape(prompt) + r"\s*$", re.IGNORECASE | re.MULTILINE)


def is_windows_style_path(raw_path: str) -> bool:
    return bool(pathlib.PureWindowsPath(raw_path).drive or "\\" in raw_path)


def remote_server_path(server_image_root: str, server_login: str, filename: str) -> str:
    if is_windows_style_path(server_image_root):
        return str(pathlib.PureWindowsPath(server_image_root, filename))
    image_path = server_image_root.strip().strip("/")
    if "/" in image_path:
        return f"/{image_path}/{filename}"
    return f"/home/{server_login}/{image_path}/{filename}"


def local_server_file_path(server_image_root: str, filename: str) -> pathlib.Path:
    if is_windows_style_path(server_image_root):
        return pathlib.Path(str(pathlib.PureWindowsPath(server_image_root, filename)))
    return pathlib.Path(server_image_root) / filename


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
    login_prompt: str
    prompt: str


@dataclass(frozen=True)
class SwitchConfig:
    prompt: str


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
    prompts: PromptConfig
    timeouts: TimeoutConfig
    db: DbConfig

    @classmethod
    def from_mapping(cls, payload: dict[str, Any]) -> "AppConfig":
        serial_cfg = payload["serial"]
        server_cfg = payload["server"]
        dut_cfg = payload["dut"]
        switch_cfg = payload["switch"]
        prompts_cfg = payload["prompts"]
        timeouts_cfg = payload["timeouts"]
        db_cfg = payload["db"]
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
                utils_path=str(dut_cfg.get("utils_path", r"C:\LSBB_Utils")),
                tmp_path=str(dut_cfg.get("tmp_path", "tmp")),
                image_file=str(dut_cfg.get("image_file", "deploy-lsbb-1.1.1-20260324.sh")),
                em_prompt=str(dut_cfg.get("em_prompt", "sh-5.2# ")),
                login_prompt=str(dut_cfg.get("login_prompt", "ls1046afrwy login: ")),
                prompt=str(dut_cfg.get("prompt", "ls1046afrwy login: ")),
            ),
            switch=SwitchConfig(prompt=str(switch_cfg["prompt"])),
            prompts=PromptConfig(
                u_boot=str(prompts_cfg["u_boot"]),
                switch=str(prompts_cfg["switch"]),
            ),
            timeouts=TimeoutConfig(
                serial_open_seconds=int(timeouts_cfg["serial_open_seconds"]),
                prompt_wait_seconds=int(timeouts_cfg["prompt_wait_seconds"]),
                boot_interrupt_seconds=int(timeouts_cfg["boot_interrupt_seconds"]),
                uboot_boot_seconds=int(timeouts_cfg.get("uboot_boot_seconds", 300)),
                emergency_boot_seconds=int(timeouts_cfg.get("emergency_boot_seconds", 600)),
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


def load_config(path: pathlib.Path) -> AppConfig:
    try:
        return AppConfig.from_mapping(load_simple_yaml(path))
    except KeyError as exc:
        raise ConfigError(f"Missing configuration key: {exc}") from exc


class SerialSession:
    def __init__(
        self,
        name: str,
        config: SerialPortConfig,
        log_path: pathlib.Path,
        open_timeout: int,
        live_uart_output: bool = False,
    ) -> None:
        self.name = name
        self.config = config
        self.log_path = log_path
        self.open_timeout = open_timeout
        self.live_uart_output = live_uart_output
        self.buffer = ""
        self._ansi_carry = ""
        self._console_line_fragment = ""
        self.serial: serial.Serial | None = None
        self.log_file = None

    def _open_serial(self, open_timeout: int) -> None:
        deadline = time.monotonic() + open_timeout
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            try:
                self.serial = serial.Serial(
                    port=self.config.port,
                    baudrate=self.config.baudrate,
                    timeout=0.1,
                    write_timeout=1,
                )
                return
            except serial.SerialException as exc:
                last_error = exc
                time.sleep(0.5)
        raise TimeoutError(f"Timed out opening {self.name} on {self.config.port}: {last_error}")

    def reopen_serial(self, reason: Exception) -> None:
        self.log_event("WARN", f"Serial read failed on {self.config.port}: {reason!r}. Reopening port.")
        if self.serial is not None:
            try:
                if self.serial.is_open:
                    self.serial.close()
            except Exception:
                pass
        self.serial = None
        time.sleep(1)
        self._open_serial(self.open_timeout)
        self.log_event("INFO", f"Reopened serial port {self.config.port} after read failure.")

    def __enter__(self) -> "SerialSession":
        self.log_file = self.log_path.open("ab")
        try:
            self._open_serial(self.open_timeout)
        except Exception:
            if self.log_file is not None and not self.log_file.closed:
                self.log_file.close()
            raise
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            if self.log_file is not None and not self.log_file.closed:
                self.log_file.close()
        finally:
            if self.serial is not None and self.serial.is_open:
                self.serial.close()

    def write(self, data: bytes) -> None:
        assert self.serial is not None
        assert self.log_file is not None
        self.serial.flush()
        self.serial.write(data)
        self.log_file.flush()
        self.log_file.write(b"\n[TX] " + data + b"\n")
        
        if self.live_uart_output:
            tx_text = data.decode("utf-8", errors="replace").replace("\r", "\\r").replace("\n", "\\n")
            print(f"[UART {self.name} TX] {tx_text}", flush=True)

    def send_line(self, text: str) -> None:
        self.write(text.encode("utf-8") + b"\r")

    def log_event(self, level: str, message: str) -> None:
        assert self.log_file is not None
        self.log_file.flush()
        self.log_file.write(f"\n[{level}] {message}\n".encode("utf-8", errors="replace"))
        

    def clear_buffer(self) -> None:
        self.buffer = ""
        self._ansi_carry = ""
        self._console_line_fragment = ""

    def emit_live_uart(self, text: str) -> None:
        if not self.live_uart_output or not text:
            return
        combined = self._console_line_fragment + text.replace("\r\n", "\n").replace("\r", "\n")
        lines = combined.split("\n")
        self._console_line_fragment = lines.pop() if lines else ""
        for line in lines:
            print(f"[UART {self.name}] {line}", flush=True)

    def poll(self) -> str:
        assert self.serial is not None
        assert self.log_file is not None
        try:
            data = self.serial.read(self.serial.in_waiting or 1)
        except (serial.SerialException, PermissionError, OSError) as exc:
            self.reopen_serial(exc)
            return ""
        if not data:
            return ""
        text = data.decode("utf-8", errors="replace")
        sanitized, self._ansi_carry = sanitize_uart_text_stream(text, self._ansi_carry)
        if sanitized:
            self.log_file.write(sanitized.encode("utf-8", errors="replace"))
            self.log_file.flush()
            self.buffer += sanitized
            if len(self.buffer) > UART_BUFFER_MAX_CHARS:
                self.buffer = self.buffer[-UART_BUFFER_TRIM_TO_CHARS:]
            self.emit_live_uart(sanitized)
        return sanitized

    def wait_for_pattern(
        self,
        pattern: re.Pattern[str],
        timeout: float,
        label: str,
        start_pos: Optional[int] = None,
    ) -> re.Match[str]:

        deadline = time.monotonic() + timeout
        scan_from = 0 if start_pos is None else start_pos
        last_buffer_len = 0

        while True:
            now = time.monotonic()
            if now >= deadline:
                raise TimeoutError(
                    f"Timed out waiting for {label} "
                    f"on {self.name} ({self.config.port})"
                )

            # Read new UART data
            self.poll()

            # Search only if buffer changed
            current_len = len(self.buffer)

            if current_len != last_buffer_len:

                match = pattern.search(self.buffer, scan_from)

                if match:
                    return match

                # Next scan starts near end of previous buffer
                # Keeps regex working across chunk boundaries
                scan_from = max(0, current_len - 8198)

                last_buffer_len = current_len

            # Small sleep reduces CPU usage
            time.sleep(0.01)

    def wait_for_any_pattern(
        self,
        patterns: dict[str, re.Pattern[str]],
        timeout: int,
        label: str,
        start_pos: int | None = None,
    ) -> str:
        deadline = time.monotonic() + timeout
        scan_from = 0 if start_pos is None else start_pos

        last_buffer_len = 0
        while True:
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"Timed out waiting for {label} "
                    f"on {self.name} ({self.config.port})"
                )
            
            self.poll()
            current_len = len(self.buffer)
            
            if current_len != last_buffer_len:
                for key, pattern in patterns.items():
                    match = pattern.search(self.buffer, scan_from)
                    if match:
                        return key
                scan_from = max(0, current_len - 8198)
                last_buffer_len = current_len
            time.sleep(0.01)           


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
    echo_pattern = re.compile(re.escape(command))
    prompt_start_pos = start_pos
    try:
        echo_match = session.wait_for_pattern(
            echo_pattern,
            timeout=min(timeout, 5),
            label=f"command echo for: {command}",
            start_pos=start_pos,
        )
        prompt_start_pos = start_pos + echo_match.end()
    except TimeoutError:
        prompt_start_pos = start_pos
    session.wait_for_pattern(
        prompt_pattern,
        timeout=timeout,
        label=f"command completion for: {command}",
        start_pos=prompt_start_pos,
    )
    return session.buffer[start_pos:]


def run_command_with_optional_password(
    session: SerialSession,
    command: str,
    shell_prompt: re.Pattern[str],
    timeout: int,
    password: str,
) -> None:
    start_pos = len(session.buffer)
    session.send_line(command)
    result = session.wait_for_any_pattern(
        {
            "hostkey_yes": HOST_KEY_CONFIRM_YES_PATTERN,
            "hostkey_y": HOST_KEY_CONFIRM_Y_PATTERN,
            "password": PASSWORD_PATTERN,
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
                "password": PASSWORD_PATTERN,
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
) -> None:
    if is_windows_style_path(remote_path):
        remote_spec = f'{server_login}@{server_ip}:"{remote_path}"'
    else:
        remote_spec = f"{server_login}@{server_ip}:{remote_path}"
    command = f"scp {remote_spec} {destination}"
    session.log_event("INFO", f"Starting SCP download: {remote_path} -> {destination}")
    run_command_with_optional_password(
        session=session,
        command=command,
        shell_prompt=shell_prompt,
        timeout=timeout,
        password=server_password,
    )


def wait_for_linux_shell(
    session: SerialSession,
    username: str,
    password: str,
    shell_prompt: re.Pattern[str],
    boot_timeout: int,
    login_prompt: re.Pattern[str],
    scan_start_pos: int | None = None,
    enter_kick_interval: float = 10.0,
) -> None:
    window = session.buffer[scan_start_pos:] if scan_start_pos is not None else ""
    sent_user = False
    sent_password = False
    deadline = time.monotonic() + boot_timeout
    next_enter_kick = time.monotonic() + enter_kick_interval

    while time.monotonic() < deadline:
        data = session.poll()
        if data:
            window = (window + data)[-12000:]
            next_enter_kick = time.monotonic() + enter_kick_interval
        lower_window = window.lower()

        if (shell_prompt.search(window) or GENERIC_SHELL_PATTERN.search(window)) and not sent_password:
            session.log_event("INFO", "Linux shell detected before credentials were needed.")
            return

        if sent_password and (shell_prompt.search(window) or GENERIC_SHELL_PATTERN.search(window)):
            session.log_event("INFO", "Linux shell detected after sending password.")
            return

        if not sent_user and (
            login_prompt.search(window)
            or USERNAME_PROMPT_PATTERN.search(lower_window)
            or LOGIN_PATTERN.search(window)
            or NXP_LOGIN_WITH_TRAILING_OUTPUT_PATTERN.search(window)
        ):
            session.log_event("INFO", f"Detected login prompt; sending username {username!r}.")
            session.send_line(username)
            sent_user = True
            window = ""
            continue

        if sent_user and not sent_password and PASSWORD_PATTERN.search(lower_window):
            session.log_event("INFO", "Detected password prompt; sending configured password.")
            session.send_line(password)
            sent_password = True
            window = ""
            continue

        if not sent_user and time.monotonic() >= next_enter_kick:
            session.log_event("INFO", "No login prompt detected yet; sending Enter to refresh UART login prompt.")
            session.send_line("")
            next_enter_kick = time.monotonic() + enter_kick_interval

        time.sleep(0.05)

    if sent_user and not sent_password:
        session.log_event("WARN", "Password prompt was not detected before timeout; sending password anyway.")
        session.send_line(password)
        sent_password = True
        final_deadline = time.monotonic() + 30
        window = ""
        while time.monotonic() < final_deadline:
            data = session.poll()
            if data:
                window = (window + data)[-12000:]
            if shell_prompt.search(window) or GENERIC_SHELL_PATTERN.search(window):
                return
            time.sleep(0.05)
    raise TimeoutError(f"Timed out waiting for linux shell on {session.name} ({self.config.port if False else session.config.port})")


def ensure_emergency_access(session: SerialSession, prompt_pattern: re.Pattern[str], boot_timeout: int, fresh: bool = False) -> None:
    start_pos = len(session.buffer) if fresh else None
    session.wait_for_pattern(
        prompt_pattern,
        timeout=boot_timeout,
        label="emergency shell prompt",
        start_pos=start_pos,
    )


def detect_nxp_uboot(session: SerialSession, autoboot_wait_timeout: int, prompt_timeout: int, stop_key: bytes) -> None:
    session.wait_for_pattern(AUTOBOOT_PATTERN, timeout=autoboot_wait_timeout, label="autoboot countdown")
    deadline = time.monotonic() + max(prompt_timeout, 6)
    while time.monotonic() < deadline:
        session.write(stop_key)
        try:
            session.wait_for_pattern(UBOOT_PROMPT_PATTERN, timeout=0.25, label="U-Boot prompt")
            validate_nxp_boot_markers(session)
            return
        except TimeoutError:
            time.sleep(0.15)
    session.wait_for_pattern(UBOOT_PROMPT_PATTERN, timeout=prompt_timeout, label="U-Boot prompt")
    validate_nxp_boot_markers(session)


def detect_switch_prompt(session: SerialSession, prompt_text: str, timeout: int, fresh: bool = False) -> re.Pattern[str]:
    pattern = compile_switch_prompt_pattern(prompt_text)
    session.wait_for_pattern(pattern, timeout=timeout, label="switch prompt", start_pos=len(session.buffer) if fresh else None)
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
    nxp_ready = False
    switch_ready = False
    while time.monotonic() < deadline:
        nxp.poll()
        switch.poll()
        if not nxp_ready and AUTOBOOT_PATTERN.search(nxp.buffer):
            nxp.write(stop_key)
            if UBOOT_PROMPT_PATTERN.search(nxp.buffer):
                validate_nxp_boot_markers(nxp)
                nxp_ready = True
        if not switch_ready and AUTOBOOT_PATTERN.search(switch.buffer):
            switch.write(stop_key)
        if not switch_ready and switch_pattern.search(switch.buffer):
            switch_ready = True
        if nxp_ready and switch_ready:
            return True, True, switch_pattern
        time.sleep(0.1)
    return nxp_ready, switch_ready, switch_pattern


def get_windows_ssh_server_status() -> str:
    completed = subprocess.run(
        ["sc", "query", "sshd"],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    output = f"{completed.stdout}\n{completed.stderr}"
    if "STATE" in output:
        match = re.search(r"STATE\s*:\s*\d+\s+([A-Z_]+)", output)
        if match:
            return match.group(1)
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
            "Windows SSH server is required for DUT-side SCP pulls, but sshd is not RUNNING."
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
    connection.execute(f"CREATE TABLE IF NOT EXISTS {table} ({serial_column} TEXT PRIMARY KEY, {mac_fields})")
    connection.commit()


def open_db_connection(config_path: pathlib.Path, db_config: DbConfig) -> sqlite3.Connection:
    if db_config.db_type.lower() != "sqlite":
        raise ConfigError(f"Unsupported db.type: {db_config.db_type}. Only 'sqlite' is supported right now.")
    db_path = build_db_path(config_path, db_config)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    create_db_table_if_needed(connection, db_config)
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
            normalized = normalize_optional_mac(row[0])
            if normalized is None:
                continue
            value = int(normalized.replace(":", ""), 16)
            if max_mac_value is None or value > max_mac_value:
                max_mac_value = value
    if max_mac_value is None:
        return None
    packed = f"{max_mac_value:012X}"
    return ":".join(packed[index:index + 2] for index in range(0, 12, 2))


def existing_row_macs(row: sqlite3.Row, db_config: DbConfig) -> list[str | None]:
    return [normalize_optional_mac(row[column]) for column in mac_column_names(db_config)]


def build_mac_block(base_mac: str, count: int) -> list[str]:
    return [normalize_mac(mac_plus(base_mac, offset)) for offset in range(count)]


def infer_base_mac_from_existing(row_macs: list[str | None]) -> str | None:
    for index, mac in enumerate(row_macs):
        if mac is None:
            continue
        return compact_mac(mac_plus(mac, -index))
    return None


def upsert_serial_row(connection: sqlite3.Connection, db_config: DbConfig, dig_sn: str, macs: list[str]) -> None:
    table = validate_sql_identifier(db_config.table, "db.table")
    serial_column = validate_sql_identifier(db_config.serial_column, "db.serial_column")
    mac_columns = mac_column_names(db_config)
    stored_macs = [compact_mac(mac) for mac in macs]
    placeholders = ", ".join("?" for _ in range(len(mac_columns) + 1))
    updates = ", ".join(f"{column} = excluded.{column}" for column in mac_columns)
    connection.execute(
        f"INSERT INTO {table} ({serial_column}, {', '.join(mac_columns)}) VALUES ({placeholders}) "
        f"ON CONFLICT({serial_column}) DO UPDATE SET {updates}",
        [dig_sn, *stored_macs],
    )
    connection.commit()


@dataclass(frozen=True)
class ProvisionArgs:
    dig_sn: str
    base_mac: str
    switch_mac: str
    nxp_mac1: str
    nxp_mac2: str
    nxp_mac3: str
    deploy_script: str


def resolve_provision_args(config: AppConfig, args: argparse.Namespace, config_path: pathlib.Path) -> ProvisionArgs:
    dig_sn = normalize_dig_sn(args.dig_sn)
    with open_db_connection(config_path, config.db) as connection:
        row = fetch_serial_row(connection, config.db, dig_sn)
        if row is not None:
            row_macs = existing_row_macs(row, config.db)
            if all(mac is not None for mac in row_macs):
                allocated = [normalize_mac(mac) for mac in row_macs if mac is not None]
                base_mac = allocated[0]
                switch_mac = allocated[1]
            else:
                base_seed = infer_base_mac_from_existing(row_macs)
                if base_seed is None:
                    latest_saved_mac = find_latest_saved_mac(connection, config.db)
                    base_seed = normalize_mac(mac_plus(latest_saved_mac or config.db.seed_mac, 1 if latest_saved_mac else 0))
                new_macs = build_mac_block(base_seed, config.db.mac_count)
                upsert_serial_row(connection, config.db, dig_sn, new_macs)
                base_mac = normalize_mac(new_macs[0])
                switch_mac = normalize_mac(new_macs[1])
        else:
            latest_saved_mac = find_latest_saved_mac(connection, config.db)
            base_seed = normalize_mac(mac_plus(latest_saved_mac or config.db.seed_mac, 1 if latest_saved_mac else 0))
            new_macs = build_mac_block(base_seed, config.db.mac_count)
            upsert_serial_row(connection, config.db, dig_sn, new_macs)
            base_mac = normalize_mac(new_macs[0])
            switch_mac = normalize_mac(new_macs[1])
    return ProvisionArgs(
        dig_sn=dig_sn,
        base_mac=base_mac,
        switch_mac=switch_mac,
        nxp_mac1="00:04:9F:08:44:A2",
        nxp_mac2="00:04:9F:08:44:A3",
        nxp_mac3="00:04:9F:08:44:A4",
        deploy_script=args.deploy_script or config.dut.image_file,
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

def burn_nxp_macs(session: SerialSession, provision: ProvisionArgs, timeout: int) -> None:
    commands = [
        "gpio clear 27",
        "mac read",
        "mac id",
        "mac ports 4",
        f"mac 0 {provision.base_mac}",
        f"mac 1 {provision.nxp_mac1}",
        f"mac 2 {provision.nxp_mac2}",
        f"mac 3 {provision.nxp_mac3}",
    ]
    for command in commands:
        run_command(session, command, UBOOT_PROMPT_PATTERN, timeout, description=f"NXP U-Boot: {command}")
    info("NXP: NXP U-Boot: mac save")
    start_pos = len(session.buffer)
    session.send_line("mac save")
    session.wait_for_pattern(MAC_SAVE_SUCCESS_PATTERN, timeout=timeout, label="mac save success", start_pos=start_pos)
    session.wait_for_pattern(UBOOT_PROMPT_PATTERN, timeout=timeout, label="U-Boot prompt after mac save", start_pos=start_pos)


def burn_switch_mac(session: SerialSession, prompt_pattern: re.Pattern[str], provision: ProvisionArgs, timeout: int) -> None:
    start_pos = len(session.buffer)
    session.send_line("env default -a")
    session.wait_for_pattern(SWITCH_ENV_RESET_PATTERN, timeout=timeout, label="switch environment reset", start_pos=start_pos)
    session.wait_for_pattern(prompt_pattern, timeout=timeout, label="switch prompt after env default -a", start_pos=start_pos)
    run_command(session, f"setenv ethaddr {provision.switch_mac}", prompt_pattern, timeout, "Switch U-Boot: setenv ethaddr")
    info("Switch: Switch U-Boot: saveenv")
    start_pos = len(session.buffer)
    session.send_line("saveenv")
    session.wait_for_pattern(SWITCH_SAVEENV_OK_PATTERN, timeout=max(timeout, 30), label="switch saveenv OK", start_pos=start_pos)
    session.wait_for_pattern(prompt_pattern, timeout=max(timeout, 30), label="switch prompt after saveenv", start_pos=start_pos)

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

def reset_nxp_from_uboot_to_emergency(session: SerialSession, config: AppConfig) -> None:
    emergency_prompt = compile_shell_prompt_pattern(config.dut.em_prompt)
    info("NXP: sending reset from U-Boot, waiting 65 seconds without UART reads, then checking YAML emergency prompt")
    session.clear_buffer()
    session.send_line("reset")
    wait_with_operator_timer(65, "NXP boot delay")
    session.clear_buffer()
    session.send_line("")
    session.send_line("")
    ensure_emergency_access(session, emergency_prompt, config.timeouts.emergency_boot_seconds, fresh=True)


def monitor_uart_only(session: SerialSession, seconds: int) -> None:
    info(f"NXP: monitoring raw UART output for {seconds} seconds without prompt detection")
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        session.poll()
        time.sleep(0.05)


def stage_network_in_emergency(session: SerialSession, config: AppConfig) -> None:
    emergency_prompt = compile_shell_prompt_pattern(config.dut.em_prompt)
    run_command(
        session,
        f"ifconfig eth0 {config.dut.final_ip} netmask 255.255.255.0 up",
        emergency_prompt,
        max(config.timeouts.prompt_wait_seconds, 30),
        "Configure DUT IP in emergency mode",
    )
    time.sleep(0.1)
    info(f"{session.name}: Ping the PC from emergency mode")
    start_pos = len(session.buffer)
    session.send_line(f"ping {config.server.ip} -c1")
    session.wait_for_pattern(
        PING_SUCCESS_PATTERN,
        timeout=max(config.timeouts.prompt_wait_seconds, 90),
        label=f"ping success for {config.server.ip}",
        start_pos=start_pos,
    )
    session.wait_for_pattern(
        emergency_prompt,
        timeout=max(config.timeouts.prompt_wait_seconds, 90),
        label="emergency shell prompt after ping",
        start_pos=start_pos,
    )
    output = session.buffer[start_pos:]
    if not PING_SUCCESS_PATTERN.search(output):
        raise RuntimeError(f"Ping to {config.server.ip} failed during emergency-stage network configuration.")


def start_dropbear_in_emergency(session: SerialSession, prompt_pattern: re.Pattern[str], timeout: int) -> None:
    run_command(session, "mkdir -p /var/run/dropbear", prompt_pattern, timeout, "Create dropbear runtime directory")
    run_command(
        session,
        "dropbear -R -B",
        prompt_pattern,
        timeout,
        "Start dropbear service in emergency mode",
    )


def upload_deploy_script_from_windows_to_dut(config: AppConfig, provision: ProvisionArgs) -> None:
    local_path = local_server_file_path(config.server.image_path, provision.deploy_script)
    if not local_path.is_file():
        raise FileNotFoundError(f"Deploy script not found on the Windows host: {local_path}")
    remote_dir = f"/{config.dut.tmp_path}".rstrip("/")
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
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=max(config.timeouts.emergency_boot_seconds, 180),
        check=False,
    )
    if completed.returncode != 0:
        output = (completed.stdout or "") + (("\n" + completed.stderr) if completed.stderr else "")
        raise RuntimeError(f"Windows->DUT SCP failed for {local_path} -> {remote_spec}\n{output.strip()}")


def mkdir_remote_path(sftp: paramiko.SFTPClient, remote_path: str) -> None:
    current = ""
    for part in remote_path.strip("/").split("/"):
        current = f"{current}/{part}" if current else f"/{part}"
        try:
            sftp.mkdir(current)
        except OSError:
            pass


def upload_lsbb_utils_from_windows_to_dut(config: AppConfig) -> None:
    if paramiko is None:
        raise RuntimeError("paramiko is required to copy LSBB_Utils with password authentication.")
    local_path = pathlib.Path(config.dut.utils_path)
    if not local_path.is_dir():
        raise FileNotFoundError(f"LSBB_Utils directory not found on the Windows host: {local_path}")
    remote_root = f"/root/{local_path.name}"
    info(
        "Windows->DUT SFTP: "
        f"{local_path} -> {config.dut.login}@{config.dut.final_ip}:{remote_root}"
    )
    client = connect_over_ssh_password(config)
    uploaded_files = 0
    try:
        with client.open_sftp() as sftp:
            mkdir_remote_path(sftp, remote_root)
            for directory in sorted(path for path in local_path.rglob("*") if path.is_dir()):
                relative = directory.relative_to(local_path).as_posix()
                mkdir_remote_path(sftp, f"{remote_root}/{relative}")
            for file_path in sorted(path for path in local_path.rglob("*") if path.is_file()):
                relative = file_path.relative_to(local_path).as_posix()
                remote_path = f"{remote_root}/{relative}"
                mkdir_remote_path(sftp, remote_path.rsplit("/", 1)[0])
                sftp.put(str(file_path), remote_path)
                uploaded_files += 1
    finally:
        client.close()
    ok(f"Copied LSBB_Utils to {config.dut.login}@{config.dut.final_ip}:{remote_root} ({uploaded_files} files)")


def transfer_deploy_script_in_emergency(session: SerialSession, config: AppConfig, provision: ProvisionArgs) -> None:
    emergency_prompt = compile_shell_prompt_pattern(config.dut.em_prompt)
    start_dropbear_in_emergency(session, emergency_prompt, 30)
    upload_deploy_script_from_windows_to_dut(config, provision)
    verify_output = run_command_capture(
        session,
        f"test -f /{config.dut.tmp_path}/{provision.deploy_script} && echo OK",
        emergency_prompt,
        config.timeouts.prompt_wait_seconds,
        "Verify deployment script exists in DUT temporary directory",
    )
    if "OK" not in verify_output:
        raise RuntimeError(
            f"Expected {provision.deploy_script} in /{config.dut.tmp_path} after SCP transfer, but file verification did not return OK."
        )


def wait_for_deploy_completion(session: SerialSession, config: AppConfig, deploy_start_pos: int) -> None:
    timeout = max(config.timeouts.emergency_boot_seconds * 4, 900)
    deadline = time.monotonic() + timeout
    progress = None
    last_progress_second = 0
    if tqdm is not None:
        progress = tqdm(total=timeout, desc="NXP deploy script", unit="s", leave=True, dynamic_ncols=True)
    try:
        while time.monotonic() < deadline:
            session.poll()
            segment = session.buffer[deploy_start_pos:]
            if DEPLOYMENT_COMPLETE_PATTERN.search(segment) and DEPLOYMENT_RUN_REBOOT_PATTERN.search(segment):
                if progress is not None and last_progress_second < timeout:
                    progress.update(timeout - last_progress_second)
                return
            if progress is not None:
                elapsed_seconds = min(timeout, int(time.monotonic() - (deadline - timeout)))
                if elapsed_seconds > last_progress_second:
                    progress.update(elapsed_seconds - last_progress_second)
                    last_progress_second = elapsed_seconds
            time.sleep(0.05)
    finally:
        if progress is not None:
            progress.close()
    raise TimeoutError(f"Timed out waiting for deployment completion on {session.name} ({session.config.port})")


def reboot_after_deploy(session: SerialSession, config: AppConfig) -> None:
    info("NXP: deploy script completed, sending reboot to leave emergency mode")
    reboot_start = len(session.buffer)
    session.send_line("reboot")
    session.wait_for_pattern(
        NXP_REBOOT_TRANSITION_PATTERN,
        timeout=max(config.timeouts.prompt_wait_seconds, 60),
        label="reboot transition after deploy",
        start_pos=reboot_start,
    )


def wait_for_post_deploy_uart_login(session: SerialSession, config: AppConfig) -> None:
    info("NXP: showing live UART after deployment reboot, then waiting for login before sending credentials")
    previous_live_uart_output = session.live_uart_output
    session.live_uart_output = True
    session.clear_buffer()
    shell_prompt = compile_shell_prompt_pattern(config.dut.prompt)
    try:
        session.wait_for_pattern(
            NXP_REDIS_STARTED_PATTERN,
            timeout=max(config.timeouts.emergency_boot_seconds, 180),
            label="Redis service startup after deploy reboot",
        )
        info("NXP: Redis detected, waiting up to 20 seconds for OpenSSH key generation")
        try:
            session.wait_for_pattern(
                NXP_OPENSSH_KEYGEN_DONE_PATTERN,
                timeout=20,
                label="OpenSSH key generation after deploy reboot",
            )
            info("NXP: OpenSSH key generation finished; sending UART credentials")
        except TimeoutError:
            info("NXP: OpenSSH key generation was not detected in 20 seconds; sending UART credentials anyway")
        info("NXP: waiting 6 seconds after OpenSSH readiness before sending UART credentials")
        time.sleep(6)
        last_error: TimeoutError | None = None
        for attempt in range(1, 3):
            info(f"NXP: sending UART credentials attempt {attempt}/2 and waiting for DUT shell")
            credential_start = len(session.buffer)
            session.send_line("")
            session.send_line(config.dut.login)
            time.sleep(1)
            session.send_line(config.dut.password)
            try:
                session.wait_for_any_pattern(
                    {
                        "configured shell": shell_prompt,
                        "generic shell": GENERIC_SHELL_PATTERN,
                    },
                    timeout=10,
                    label="DUT shell after blind UART credentials",
                    start_pos=credential_start,
                )
                ok("NXP: entered DUT shell after reboot")
                return
            except TimeoutError as exc:
                last_error = exc
                session.log_event("WARN", f"UART credential attempt {attempt}/2 failed: {exc}")
        if session.serial is not None and session.serial.is_open:
            session.serial.close()
        raise RuntimeError("NXP UART login failed after retry; serial port was closed.") from last_error
    finally:
        session.live_uart_output = previous_live_uart_output


def run_deploy_script(session: SerialSession, config: AppConfig, provision: ProvisionArgs) -> None:
    emergency_prompt = compile_shell_prompt_pattern(config.dut.em_prompt)
    run_command(session, f"cd /{config.dut.tmp_path}", emergency_prompt, 30, "Change to temporary deployment directory")
    info("NXP: starting deployment script")
    deploy_start_pos = len(session.buffer)
    session.send_line(f"sh /{config.dut.tmp_path}/{provision.deploy_script}")
    wait_for_deploy_completion(session, config, deploy_start_pos)
    session.clear_buffer()
    reboot_after_deploy(session, config)
    wait_for_post_deploy_uart_login(session, config)


def save_ip_over_uart(session: SerialSession, config: AppConfig) -> None:
    shell_prompt = ROOT_SHELL_PATTERN
    fm1_mac5_add = (
        "nmcli con add type ethernet ifname fm1-mac5 con-name fm1-mac5-static "
        f"ipv4.addresses {config.dut.final_ip}/24 ipv4.method manual"
    )
    fm1_mac9_add = (
        "nmcli con add type ethernet ifname fm1-mac9 con-name fm1-mac9-static "
        "ipv4.addresses 10.2.4.2/24 ipv4.method manual"
    )
    run_command(
        session,
        fm1_mac5_add,
        shell_prompt,
        max(config.timeouts.prompt_wait_seconds, 60),
        "Configure persistent DUT IP with nmcli",
    )
    run_command(
        session,
        "nmcli con up fm1-mac5-static",
        shell_prompt,
        max(config.timeouts.prompt_wait_seconds, 60),
        "Bring up persistent DUT IP with nmcli",
    )
    run_command(
        session,
        "nmcli con mod fm1-mac5-static connection.autoconnect yes",
        shell_prompt,
        max(config.timeouts.prompt_wait_seconds, 60),
        "Enable DUT IP autoconnect with nmcli",
    )
    session.send_line("")
    session.wait_for_pattern(
        shell_prompt,
        timeout=max(config.timeouts.prompt_wait_seconds, 60),
        label="shell prompt after fm1-mac5 blank enter",
        start_pos=len(session.buffer),
    )
    run_command(
        session,
        fm1_mac9_add,
        shell_prompt,
        max(config.timeouts.prompt_wait_seconds, 60),
        "Configure fm1-mac9 static IP with nmcli",
    )
    run_command(
        session,
        "nmcli con up fm1-mac9-static",
        shell_prompt,
        max(config.timeouts.prompt_wait_seconds, 60),
        "Bring up fm1-mac9 static IP with nmcli",
    )
    run_command(
        session,
        "nmcli con mod fm1-mac9-static connection.autoconnect yes",
        shell_prompt,
        max(config.timeouts.prompt_wait_seconds, 60),
        "Enable fm1-mac9 autoconnect with nmcli",
    )
    session.send_line("")
    session.wait_for_pattern(
        shell_prompt,
        timeout=max(config.timeouts.prompt_wait_seconds, 60),
        label="shell prompt after fm1-mac9 blank enter",
        start_pos=len(session.buffer),
    )
    session.clear_buffer()


def transfer_lsbb_utils_after_login(session: SerialSession, config: AppConfig) -> None:
    shell_prompt = ROOT_SHELL_PATTERN
    upload_lsbb_utils_from_windows_to_dut(config)
    verify_output = run_command_capture(
        session,
        "test -d /root/LSBB_Utils && echo OK",
        shell_prompt,
        max(config.timeouts.prompt_wait_seconds, 60),
        "Verify LSBB_Utils exists on the DUT",
    )
    if "OK" not in verify_output:
        raise RuntimeError("Expected /root/LSBB_Utils on the DUT after SCP transfer, but verification did not return OK.")
    run_command(
        session,
        "chmod +x LSBB_Utils/run.sh",
        shell_prompt,
        max(config.timeouts.prompt_wait_seconds, 60),
        "Make LSBB_Utils run.sh executable",
    )


def ssh_exec_checked(client: paramiko.SSHClient, command: str, timeout: int = 60) -> str:
    stdin, stdout, stderr = client.exec_command(command, timeout=timeout)
    exit_code = stdout.channel.recv_exit_status()
    output = stdout.read().decode("utf-8", errors="replace")
    error = stderr.read().decode("utf-8", errors="replace")
    if exit_code != 0:
        raise RuntimeError(f"SSH command failed ({exit_code}): {command}\n{error or output}")
    return output


def connect_over_ssh_password(config: AppConfig) -> paramiko.SSHClient:
    if paramiko is None:
        raise RuntimeError("paramiko is required for the SSH stages.")
    wait_for_ssh(config.dut.final_ip, 22, max(config.timeouts.emergency_boot_seconds, 180))
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        hostname=config.dut.final_ip,
        username=config.dut.login,
        password=config.dut.password,
        look_for_keys=False,
        allow_agent=False,
        timeout=30,
        auth_timeout=30,
        banner_timeout=30,
    )
    return client


def save_ip_over_ssh(config: AppConfig, ssh_log_path: pathlib.Path) -> None:
    if paramiko is None:
        raise RuntimeError("paramiko is required for the SSH stages.")
    client = connect_over_ssh_password(config)
    try:
        ssh_exec_checked(
            client,
            "nmcli con add type ethernet ifname fm1-mac5 con-name fm1-mac5-static "
            f"ipv4.addresses {config.dut.final_ip}/24 ipv4.method manual",
        )
        ssh_exec_checked(client, "nmcli con up fm1-mac5-static")
        ssh_exec_checked(client, "nmcli con mod fm1-mac5-static connection.autoconnect yes")
        ssh_exec_checked(
            client,
            "nmcli con add type ethernet ifname fm1-mac9 con-name fm1-mac9-static "
            "ipv4.addresses 10.2.4.2/24 ipv4.method manual",
        )
        ssh_exec_checked(client, "nmcli con up fm1-mac9-static")
        ssh_exec_checked(client, "nmcli con mod fm1-mac9-static connection.autoconnect yes")
        ssh_log_path.write_text(
            ssh_exec_checked(client, "hostname ; ip addr show dev eth0"),
            encoding="utf-8",
        )
    finally:
        client.close()


def wait_for_ssh(host: str, port: int, timeout: int) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=5):
                return
        except OSError:
            time.sleep(2)
    raise TimeoutError(f"Timed out waiting for SSH on {host}:{port}")


def run_nxp_flow(config: AppConfig, provision: ProvisionArgs, args: argparse.Namespace, run_dir: pathlib.Path) -> int:
    nxp_log = timestamped_log_path(run_dir, "nxp")
    info(f"NXP log: {nxp_log}")
    try:
        with SerialSession(
            "NXP",
            config.nxp,
            nxp_log,
            config.timeouts.serial_open_seconds,
            live_uart_output=args.show_uart,
        ) as nxp:
            stage(1, "Stop autoboot and configure NXP at U-Boot")
            detect_nxp_uboot(
                nxp,
                config.timeouts.uboot_boot_seconds,
                config.timeouts.uboot_boot_seconds,
                boot_stop_bytes(args.boot_stop_key),
            )
            burn_nxp_macs(nxp, provision, config.timeouts.prompt_wait_seconds)

            if args.monitor_uart_seconds > 0:
                stage(2, "Reset NXP after MAC save and monitor raw UART only")
                nxp.clear_buffer()
                info("NXP: sending reset from U-Boot without emergency detection")
                nxp.send_line("reset")
                monitor_uart_only(nxp, args.monitor_uart_seconds)
                ok("UART monitor-only run completed")
                return 0

            stage(2, "Reset NXP after MAC save and enter emergency mode using YAML prompt")
            reset_nxp_from_uboot_to_emergency(nxp, config)

            stage(3, "Send IP and ping commands from NXP to the PC")
            stage_network_in_emergency(nxp, config)

            stage(4, "Start dropbear and push the deployment script from Windows to the DUT")
            transfer_deploy_script_in_emergency(nxp, config, provision)

            stage(5, "Start the deployment script, then reboot out of emergency mode")
            run_deploy_script(nxp, config, provision)

            stage(6, "Log in on UART after reboot and save the DUT IP")
            save_ip_over_uart(nxp, config)

            stage(7, "Copy LSBB_Utils from Windows to the DUT")
            transfer_lsbb_utils_after_login(nxp, config)
    except (serial.SerialException, TimeoutError, RuntimeError, OSError, ValueError, sqlite3.Error) as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1
    ok("NXP staged flow completed successfully.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Stage-separated NXP provisioning flow with UART-to-SSH handoff.")
    parser.add_argument("--dig_sn", required=True, help="DIG board serial number used to allocate/read MAC addresses from the DB.")
    parser.add_argument("--config", default="script_setup.yaml", help="Path to the YAML configuration file.")
    parser.add_argument(
        "--boot-stop-key",
        choices=["enter", "space", "ctrl-c"],
        default="enter",
        help="Key sent to stop autoboot on both UART consoles.",
    )
    parser.add_argument(
        "--deploy-script",
        default=None,
        help="Deployment script filename under the configured server.image_path. Defaults to dut.image_file from YAML.",
    )
    parser.add_argument(
        "--show-uart",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Mirror live UART RX/TX to the console in addition to saving it in the log files.",
    )
    parser.add_argument(
        "--monitor-uart-seconds",
        type=int,
        default=0,
        help="Debug mode: after NXP reset, skip emergency detection and only stream UART for this many seconds, then exit.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    repo_root = pathlib.Path(__file__).resolve().parent
    config_path = (repo_root / args.config).resolve()
    run_dir = run_dir_for_dig_sn(args.dig_sn)
    try:
        config = load_config(config_path)
        provision = resolve_provision_args(config, args, config_path)
        return run_nxp_flow(config, provision, args, run_dir)
    except (ConfigError, FileNotFoundError, OSError, ValueError, sqlite3.Error) as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

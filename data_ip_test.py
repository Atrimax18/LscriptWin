from __future__ import annotations

import argparse
import pathlib
import re
import subprocess
import sys

from lscriptwin import (
    ConfigError,
    SerialSession,
    ensure_logs_dir,
    info,
    ok,
    timestamped_log_path,
)
from sx4000_config import Sx4000RuntimeConfig, load_runtime_config, run_sonic_commands


DATA_IP_COMMANDS = (
    "sudo config interface speed Ethernet12 10000",
    "sudo config interface speed Ethernet13 10000",
    "sudo config interface ip add Ethernet12 10.10.10.20/24",
    "sudo config interface ip add Ethernet13 10.10.10.30/24",
)

DATA_IP_TARGETS = ("10.10.10.20", "10.10.10.30")

WINDOWS_PING_OK_PATTERN = re.compile(r"\(\s*0%\s*loss\s*\)", re.IGNORECASE)
POSIX_PING_OK_PATTERN = re.compile(r"\b0%\s+packet loss\b", re.IGNORECASE)


def configure_switch_data_ips(config: Sx4000RuntimeConfig, repo_root: pathlib.Path) -> None:
    switch_log = timestamped_log_path(repo_root, "data-ip-switch")
    with SerialSession("Switch", config.switch, switch_log, config.serial_open_seconds) as switch:
        info(f"Switch log: {switch_log}")
        run_sonic_commands(switch, config, DATA_IP_COMMANDS)
    ok("Switch login verified and data IP commands completed.")


def ping_command(target: str, count: int, timeout_seconds: int) -> list[str]:
    if sys.platform.startswith("win"):
        return ["ping", "-n", str(count), "-w", str(timeout_seconds * 1000), target]
    return ["ping", "-c", str(count), "-W", str(timeout_seconds), target]


def ping_success(output: str) -> bool:
    if WINDOWS_PING_OK_PATTERN.search(output) or POSIX_PING_OK_PATTERN.search(output):
        return True
    return False


def run_pc_ping(target: str, count: int, timeout_seconds: int) -> None:
    command = ping_command(target, count, timeout_seconds)
    info(f"PC: {' '.join(command)}")
    completed = subprocess.run(command, text=True, capture_output=True, timeout=(count * timeout_seconds) + 10)
    output = (completed.stdout or "") + (completed.stderr or "")
    if completed.returncode != 0 or not ping_success(output):
        raise RuntimeError(f"PC ping failed for {target}.\n{output.strip()}")
    ok(f"PC ping passed for {target}.")


def run_pc_pings(count: int, timeout_seconds: int) -> None:
    for target in DATA_IP_TARGETS:
        run_pc_ping(target, count, timeout_seconds)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Configure switch data IPs and verify PC ping reachability.")
    parser.add_argument("--config", default="script_setup.yaml", help="Path to YAML configuration.")
    parser.add_argument("--skip-switch-config", action="store_true", help="Do not run switch login/configuration commands.")
    parser.add_argument("--skip-ping", action="store_true", help="Do not run PC ping checks.")
    parser.add_argument("--ping-count", type=int, default=4, help="Number of ICMP echo requests per target.")
    parser.add_argument("--ping-timeout", type=int, default=3, help="Per-packet ping timeout in seconds.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    repo_root = pathlib.Path(__file__).resolve().parent
    config_path = (repo_root / args.config).resolve()
    ensure_logs_dir(repo_root)

    try:
        config = load_runtime_config(config_path)
        if not args.skip_switch_config:
            configure_switch_data_ips(config, repo_root)
        if not args.skip_ping:
            run_pc_pings(args.ping_count, args.ping_timeout)
        ok("Data IP test completed.")
        return 0
    except (OSError, ConfigError, RuntimeError, subprocess.SubprocessError, TimeoutError) as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

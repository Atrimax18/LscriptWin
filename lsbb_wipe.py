from __future__ import annotations

import argparse
import pathlib
import re
import sys
import time

import serial

from lscriptnxp3 import (
    AUTOBOOT_PATTERN,
    ConfigError,
    SerialSession,
    UBOOT_PROMPT_PATTERN,
    boot_stop_bytes,
    compile_switch_prompt_pattern,
    info,
    load_config,
    ok,
    run_dir_for_dig_sn,
    run_command,
    timestamped_log_path,
    validate_nxp_boot_markers,
)


NXP_WIPE_COMMANDS = (
    "mmc dev 0",
    "mmc erase 0 0x0ED1F800",
)
SWITCH_WIPE_COMMANDS = (
    "mmc dev 0",
    "mmc erase 0 0xFFFFFFFF",
)
SWITCH_UBOOT_BANNER_PATTERN = re.compile(r"\bU-Boot\b", re.IGNORECASE)


def wait_seconds(seconds: int, label: str) -> None:
    info(f"{label}: waiting {seconds} seconds")
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        time.sleep(min(1.0, deadline - time.monotonic()))


def detect_uboot_pair(
    nxp: SerialSession,
    switch: SerialSession,
    switch_prompt_text: str,
    stop_key: bytes,
    timeout: int,
) -> re.Pattern[str]:
    switch_prompt = compile_switch_prompt_pattern(switch_prompt_text)
    deadline = time.monotonic() + timeout
    nxp_ready = False
    switch_ready = False
    switch_uboot_seen = False
    nxp_interrupt_until = 0.0
    switch_interrupt_until = 0.0
    next_nxp_prompt_nudge = time.monotonic() + 2
    next_switch_prompt_nudge = time.monotonic() + 2

    info("Sending Enter to switch U-Boot console")
    switch.write(b"\r")
    info("Sending Enter to NXP U-Boot console")
    nxp.write(b"\r")
    info("Waiting until both U-Boot prompts are detected before wipe commands")

    while time.monotonic() < deadline:
        nxp.poll()
        switch.poll()

        if not nxp_ready:
            if UBOOT_PROMPT_PATTERN.search(nxp.buffer):
                validate_nxp_boot_markers(nxp)
                nxp_ready = True
                ok(f"NXP U-Boot is ready on {nxp.config.port}")
            elif AUTOBOOT_PATTERN.search(nxp.buffer):
                nxp_interrupt_until = max(nxp_interrupt_until, time.monotonic() + 6)

        if not switch_ready:
            switch_has_uboot_output = bool(
                SWITCH_UBOOT_BANNER_PATTERN.search(switch.buffer) or AUTOBOOT_PATTERN.search(switch.buffer)
            )
            if switch_has_uboot_output and not switch_uboot_seen:
                switch_uboot_seen = True
                info(f"Switch U-Boot output detected on {switch.config.port}")
            if switch_prompt.search(switch.buffer):
                switch_ready = True
                ok(f"Switch U-Boot prompt is ready on {switch.config.port}")
            elif AUTOBOOT_PATTERN.search(switch.buffer):
                switch_interrupt_until = max(switch_interrupt_until, time.monotonic() + 6)

        now = time.monotonic()
        if not switch_ready and now >= next_switch_prompt_nudge:
            switch.send_line("")
            next_switch_prompt_nudge = now + 2
        if not nxp_ready and now >= next_nxp_prompt_nudge:
            nxp.send_line("")
            next_nxp_prompt_nudge = now + 2
        if not nxp_ready and now < nxp_interrupt_until:
            nxp.write(stop_key)
        if not switch_ready and now < switch_interrupt_until:
            switch.write(stop_key)

        if nxp_ready and switch_ready:
            return switch_prompt

        time.sleep(0.1)

    missing = []
    if not nxp_ready:
        missing.append(f"NXP U-Boot on {nxp.config.port}")
    if not switch_ready:
        missing.append(f"switch U-Boot on {switch.config.port}")
    raise TimeoutError("Timed out waiting for " + " and ".join(missing))


def send_wipe_commands(
    nxp: SerialSession,
    switch: SerialSession,
    switch_prompt: re.Pattern[str],
    erase_timeout: int,
) -> None:
    info("NXP: sending wipe commands")
    for command in NXP_WIPE_COMMANDS:
        run_command(nxp, command, UBOOT_PROMPT_PATTERN, erase_timeout, f"NXP U-Boot: {command}")

    info("Switch: sending wipe commands")
    for command in SWITCH_WIPE_COMMANDS:
        run_command(switch, command, switch_prompt, erase_timeout, f"Switch U-Boot: {command}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Wipe LSBB NXP and switch eMMC from U-Boot.")
    parser.add_argument("--config", default="script_setup.yaml", help="Path to the YAML configuration file.")
    parser.add_argument(
        "--boot-stop-key",
        "--boot_stop_key",
        choices=["enter", "space", "ctrl-c"],
        default="enter",
        help="Key sent to stop autoboot on both UART consoles.",
    )
    parser.add_argument(
        "--detect-timeout",
        type=int,
        default=None,
        help="Seconds to wait for both U-Boot prompts. Defaults to the configured U-Boot timeout.",
    )
    parser.add_argument(
        "--erase-timeout",
        type=int,
        default=900,
        help="Seconds to wait for each erase command to return to the U-Boot prompt.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    repo_root = pathlib.Path(__file__).resolve().parent
    config_path = (repo_root / args.config).resolve()

    try:
        config = load_config(config_path)
        detect_timeout = args.detect_timeout or max(config.timeouts.uboot_boot_seconds, config.timeouts.boot_interrupt_seconds)
        run_dir = run_dir_for_dig_sn("LSBB_WIPE")
        nxp_log = timestamped_log_path(run_dir, "wipe-nxp-uart")
        switch_log = timestamped_log_path(run_dir, "wipe-switch-uart")

        info(f"Opening NXP UART {config.nxp.port}; log: {nxp_log}")
        info(f"Opening switch UART {config.switch_serial.port}; log: {switch_log}")
        with SerialSession("NXP", config.nxp, nxp_log, config.timeouts.serial_open_seconds) as nxp, SerialSession(
            "Switch",
            config.switch_serial,
            switch_log,
            config.timeouts.serial_open_seconds,
        ) as switch:
            switch_prompt = detect_uboot_pair(
                nxp,
                switch,
                config.prompts.switch,
                boot_stop_bytes(args.boot_stop_key),
                detect_timeout,
            )
            ok("Both U-Boot prompts detected; starting wipe commands")
            send_wipe_commands(nxp, switch, switch_prompt, args.erase_timeout)
            wait_seconds(15, "Post-wipe completion delay")
            ok("NXP wipe passed")
            ok("Switch wipe passed")

        ok("Turn off the unit, wipe completed.")
        return 0
    except (serial.SerialException, TimeoutError, RuntimeError, OSError, ValueError, ConfigError) as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())









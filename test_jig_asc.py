from __future__ import annotations

import argparse
import csv
import datetime as dt
import io
import pathlib
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Any

from test_sys4 import (
    EXIT_MARKER_RE,
    NXP_PYTHON_PROMPT_RE,
    RUN_SH_DONE_RE,
    apply_connection_defaults,
    check_nxp_login,
    clean_folder_name,
    clean_terminal_text,
    get_nested,
    load_yaml,
    log_nxp,
    recv_ssh_channel_until_prompt,
    report_value,
    ssh_connect,
    strip_ansi,
    strip_terminal_cpr,
)


ASC_READ_COMMAND = "asc_ctrl.asc_read_volt_all()"
ASC_CFG_COMMAND = "asc_ctrl.asc_vmon_cfg()"
VOLTAGE_LINE_RE = re.compile(
    r"^\s*(?P<name>\S+)\s+\([^)]*\)\s*=>\s*"
    r"(?P<value>-?\d+(?:\.\d+)?)\s*"
    r"\(UnderVolt=(?P<under>-?\d+(?:\.\d+)?),\s*"
    r"OverVolt=(?P<over>-?\d+(?:\.\d+)?),",
    re.MULTILINE,
)


@dataclass(frozen=True)
class VoltageMeasurement:
    name: str
    value: float
    under_volt: float
    over_volt: float


@dataclass(frozen=True)
class VoltageResult:
    name: str
    value: float | None
    minimum: float | None
    maximum: float | None
    passed: bool
    note: str = ""


def output_dir(log_root: pathlib.Path, sn: str, when: dt.datetime) -> pathlib.Path:
    base_folder = when.strftime("%Y%m%d_%H%M%S_acs_test")
    serial_dir = log_root / clean_folder_name(sn.upper())
    candidate = serial_dir / base_folder
    index = 1
    while candidate.exists():
        candidate = serial_dir / f"{base_folder}_{index:02d}"
        index += 1
    return candidate


def parse_asc_read_volt_all(output: str) -> dict[str, VoltageMeasurement]:
    text = strip_ansi(output)
    command_index = text.rfind(ASC_READ_COMMAND)
    if command_index >= 0:
        text = text[command_index:]

    measurements: dict[str, VoltageMeasurement] = {}
    for match in VOLTAGE_LINE_RE.finditer(text):
        name = match.group("name")
        measurements[name] = VoltageMeasurement(
            name=name,
            value=float(match.group("value")),
            under_volt=float(match.group("under")),
            over_volt=float(match.group("over")),
        )
    return measurements


def voltage_limits(expected: dict[str, Any]) -> dict[str, dict[str, Any]]:
    limits = expected.get("asc_voltage_test")
    if isinstance(limits, dict):
        return limits

    limits = get_nested(expected, "tests.asc_voltage")
    if isinstance(limits, dict):
        return limits

    raise RuntimeError("Missing ASC voltage limits in test_setup.yaml: expected asc_voltage_test or tests.asc_voltage.")


def compare_voltages(
    limits: dict[str, dict[str, Any]],
    measurements: dict[str, VoltageMeasurement],
) -> list[VoltageResult]:
    results: list[VoltageResult] = []
    for name, limit in limits.items():
        if not isinstance(limit, dict):
            continue
        minimum = float(limit["min"]) if "min" in limit and limit["min"] is not None else None
        maximum = float(limit["max"]) if "max" in limit and limit["max"] is not None else None
        measurement = measurements.get(name)
        if measurement is None:
            results.append(VoltageResult(name, None, minimum, maximum, False, "not found"))
            continue
        passed = True
        if minimum is not None and measurement.value < minimum:
            passed = False
        if maximum is not None and measurement.value > maximum:
            passed = False
        results.append(VoltageResult(name, measurement.value, minimum, maximum, passed))

    for name, measurement in measurements.items():
        if name not in limits:
            results.append(VoltageResult(name, measurement.value, measurement.under_volt, measurement.over_volt, True, "no YAML limit"))
    return results


def result_line(result: VoltageResult) -> str:
    status = "PASS" if result.passed else "FAIL"
    if result.value is None:
        value = "NOT_FOUND"
    else:
        value = f"{result.value:.3f}V"
    minimum = "" if result.minimum is None else f"{result.minimum:.3f}"
    maximum = "" if result.maximum is None else f"{result.maximum:.3f}"
    limits = f"{minimum}..{maximum}"
    suffix = f" ({result.note})" if result.note else ""
    return f"[{status}] {result.name:<18} {value:>10}  limits={limits}{suffix}"


def report_text(results: list[VoltageResult], sn: str) -> tuple[str, int]:
    failed = [result for result in results if not result.passed]
    passed = len(results) - len(failed)
    status = "PASSED" if results and not failed else "FAILED"
    lines = [
        f"Report ACS SN: {sn} - {status}",
        "=========================================================================",
        "",
        "--- Testing ASC Voltage ---",
        f"[INFO] Section summary: {passed} passed, {len(failed)} failed, {len(results)} checked.",
    ]
    lines.extend(result_line(result) for result in results)
    lines.extend(["", f"Summary: {passed} passed, {len(failed)} failed, {len(results)} checked."])
    return "\n".join(lines) + "\n", 0 if results and not failed else 1


def csv_report_text(results: list[VoltageResult]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(["test_name", "value", "min", "max", "units", "status"])
    for result in results:
        writer.writerow(
            [
                result.name,
                "" if result.value is None else report_value(result.value),
                "" if result.minimum is None else report_value(result.minimum),
                "" if result.maximum is None else report_value(result.maximum),
                "V",
                "PASS" if result.passed else "FAIL",
            ]
        )
    return buffer.getvalue()


def save_run_artifacts(
    run_dir: pathlib.Path,
    output: str,
    report: str,
    csv_report: str,
    args: argparse.Namespace,
    exit_code: int,
) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    status = "PASSED" if exit_code == 0 else "FAILED"
    (run_dir / "output.txt").write_text(output, encoding="utf-8")
    (run_dir / f"acs_report_{status}.txt").write_text(report, encoding="utf-8")
    (run_dir / f"acs_report_{status}.csv").write_text(csv_report, encoding="utf-8", newline="")
    summary = (
        f"dig_sn: {args.dig_sn}\n"
        f"host: {args.host}\n"
        f"user: {args.user}\n"
        f"remote_dir: {args.remote_dir}\n"
        f"script: {args.script}\n"
        f"skip_prbs: {args.skip_prbs}\n"
        f"status: {status}\n"
        f"exit_code: {exit_code}\n"
    )
    (run_dir / "summary.txt").write_text(summary, encoding="utf-8")


def save_error_artifacts(run_dir: pathlib.Path, output: str, args: argparse.Namespace, exc: BaseException) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    if output:
        (run_dir / "output.txt").write_text(output, encoding="utf-8")
    (run_dir / "error.txt").write_text(f"{type(exc).__name__}: {exc}\n", encoding="utf-8")
    summary = (
        f"dig_sn: {args.dig_sn}\n"
        f"host: {getattr(args, 'host', '')}\n"
        f"user: {getattr(args, 'user', '')}\n"
        f"remote_dir: {getattr(args, 'remote_dir', '')}\n"
        f"script: {getattr(args, 'script', '')}\n"
        "exit_code: 1\n"
        f"error: {type(exc).__name__}\n"
    )
    (run_dir / "summary.txt").write_text(summary, encoding="utf-8")


def send_python_command(
    channel: Any,
    args: argparse.Namespace,
    output_parts: list[str],
    command: str,
    timeout: float,
) -> str:
    print(f"[TX] nxp python {command}")
    log_nxp(args, f"\n[TX] nxp python {command}\n")
    channel.send(command + "\r")
    return recv_ssh_channel_until_prompt(channel, args, output_parts, timeout)


def run_acs_interactive(args: argparse.Namespace) -> str:
    client = ssh_connect(args)
    output_parts: list[str] = []
    started = time.monotonic()
    last_progress = started
    sent_cfg = False
    sent_read = False
    marker = "__TEST_EXIT__"
    command = args.remote_command or f"cd {args.remote_dir} && sh ./{args.script}"
    args.output_streamed = True

    try:
        channel = client.invoke_shell()
        channel.settimeout(0.0)
        log_nxp(args, f"\n[TX] nxp shell ({command}); printf '\\n{marker}%s\\n' $?\n")
        channel.send(f"({command}); printf '\\n{marker}%s\\n' $?\n")

        while True:
            if time.monotonic() - started > args.timeout:
                raise TimeoutError(f"Timed out after {args.timeout} seconds waiting for ACS voltage test.")

            if channel.recv_ready():
                chunk = channel.recv(4096).decode(errors="replace")
                output_parts.append(chunk)
                log_nxp(args, chunk)
                print(strip_terminal_cpr(chunk), end="", flush=True)
                last_progress = time.monotonic()

                clean_output = clean_terminal_text("".join(output_parts))
                if not sent_cfg and RUN_SH_DONE_RE.search(clean_output) and NXP_PYTHON_PROMPT_RE.search(clean_output):
                    print("\n[INFO] run.sh completed; starting ASC voltage monitor config.")
                    send_python_command(channel, args, output_parts, ASC_CFG_COMMAND, args.command_timeout)
                    sent_cfg = True
                    print("[INFO] Waiting 1 second before ASC voltage read.")
                    time.sleep(1)
                    read_output = send_python_command(channel, args, output_parts, ASC_READ_COMMAND, args.command_timeout)
                    sent_read = True
                    if not parse_asc_read_volt_all(read_output):
                        raise RuntimeError("ASC voltage read command finished but no voltage measurements were parsed.")
                    break

                if EXIT_MARKER_RE.search(clean_output) and not sent_read:
                    raise RuntimeError("run.sh exited before the NXP Python prompt was ready for ASC voltage commands.")
            else:
                if channel.closed:
                    break
                if time.monotonic() - last_progress > 30:
                    print("[INFO] Waiting for run.sh to finish and show the Python prompt...", flush=True)
                    last_progress = time.monotonic()
                time.sleep(0.1)
    finally:
        client.close()

    if not sent_cfg or not sent_read:
        combined = strip_terminal_cpr("".join(output_parts))
        raise RuntimeError(f"ACS voltage commands were not completed.\n{combined.strip()}")
    return EXIT_MARKER_RE.sub("", strip_terminal_cpr("".join(output_parts)))


def run_acs(args: argparse.Namespace) -> str:
    if args.password:
        print(f"[TX] ssh {args.user}@{args.host} cd {args.remote_dir} && sh ./{args.script}")
        return run_acs_interactive(args)
    raise RuntimeError("ACS interactive mode requires SSH password login through paramiko.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run ASC voltage test and compare asc_read_volt_all() with test_setup.yaml.")
    parser.add_argument("--dig_sn", required=True, help="DUT serial number used for the ASC log folder.")
    parser.add_argument("--skip-prbs", action="store_true", help="Accepted for jig compatibility; this ACS test does not run PRBS.")
    parser.add_argument("--config", default="test_setup.yaml", help="YAML file with ASC voltage limits.")
    parser.add_argument("--setup-config", default="script_setup.yaml", help="YAML file with SSH connection information.")
    parser.add_argument("--input-log", help="Parse an existing output file instead of running SSH.")
    parser.add_argument("--host", help="SSH host/IP address override. Defaults to script_setup.yaml dut.final_ip.")
    parser.add_argument("--user", help="SSH username override. Defaults to script_setup.yaml dut.login.")
    parser.add_argument("--password", help="SSH password override. Defaults to script_setup.yaml dut.password.")
    parser.add_argument("--port", type=int, help="SSH port.")
    parser.add_argument("--identity-file", help="SSH private key path.")
    parser.add_argument("--ssh-option", action="append", help="Extra ssh -o option for the login precheck. Can be repeated.")
    parser.add_argument("--force-openssh", action="store_true", help="Use system ssh for the login precheck.")
    parser.add_argument("--connect-timeout", type=int, default=10, help="SSH connection timeout in seconds.")
    parser.add_argument("--timeout", type=int, default=900, help="Overall remote command timeout in seconds.")
    parser.add_argument("--command-timeout", type=int, default=60, help="Timeout for each ASC Python command.")
    parser.add_argument("--remote-dir", default=argparse.SUPPRESS, help="Remote directory containing run.sh.")
    parser.add_argument("--script", default="run.sh", help="Remote .sh script name.")
    parser.add_argument("--remote-command", help="Full remote command override.")
    parser.add_argument("--skip-login-check", action="store_true", help="Skip the NXP SSH login precheck.")
    parser.add_argument("--no-save", action="store_true", help="Do not save ASC test artifacts.")
    parser.set_defaults(interactive=False)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    repo_root = pathlib.Path(__file__).resolve().parent
    config_path = (repo_root / args.config).resolve()
    setup_path = (repo_root / args.setup_config).resolve()
    run_dir: pathlib.Path | None = None
    output = ""
    args.run_dir = None

    try:
        expected = load_yaml(config_path)
        setup = load_yaml(setup_path)
        apply_connection_defaults(args, setup)

        if not args.no_save:
            log_path = get_nested(expected, "logs.path")
            log_root = pathlib.Path(str(log_path)) if log_path else repo_root / "logs"
            run_dir = output_dir(log_root, args.dig_sn, dt.datetime.now())
            run_dir.mkdir(parents=True, exist_ok=False)
            args.run_dir = run_dir
            print(f"[INFO] ACS log folder: {run_dir}")

        if args.input_log:
            output_path = (repo_root / args.input_log).resolve()
            output = output_path.read_text(encoding="utf-8", errors="replace")
            print(f"[INFO] Loaded output from {output_path}")
        else:
            if not args.host:
                raise RuntimeError("Missing dut.final_ip in script_setup.yaml. Use --host to override.")
            if not args.user:
                raise RuntimeError("Missing dut.login in script_setup.yaml. Use --user to override.")
            if not args.skip_login_check:
                check_nxp_login(args)
            output = run_acs(args)
            if not getattr(args, "output_streamed", False):
                print(output, end="" if output.endswith("\n") else "\n")

        measurements = parse_asc_read_volt_all(output)
        if not measurements:
            raise RuntimeError("No ASC voltage measurements were found after asc_read_volt_all().")
        results = compare_voltages(voltage_limits(expected), measurements)
        report, exit_code = report_text(results, args.dig_sn)
        csv_report = csv_report_text(results)
        print("\n" + report, end="")
        print(f"[{'PASS' if exit_code == 0 else 'FAIL'}] Unit {args.dig_sn} ASC voltage test {'passed' if exit_code == 0 else 'failed'}.")

        if run_dir is not None:
            save_run_artifacts(run_dir, output, report, csv_report, args, exit_code)
            print(f"[INFO] Saved ACS test artifacts to {run_dir}")

        return exit_code
    except (OSError, RuntimeError, subprocess.SubprocessError, TimeoutError) as exc:
        if run_dir is not None:
            save_error_artifacts(run_dir, output, args, exc)
            print(f"[INFO] Saved error artifacts to {run_dir}")
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

import argparse
import csv
import datetime as dt
import io
import pathlib
import re
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Any

try:
    import yaml
except ImportError:  # pragma: no cover - fallback parser is used at runtime.
    yaml = None

try:
    import paramiko
except ImportError:  # pragma: no cover - ssh subprocess fallback is used at runtime.
    paramiko = None

try:
    import serial
except ImportError:  # pragma: no cover - handled at runtime.
    serial = None


ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
CPR_RE = re.compile(r"\x1b\[(?:6n|\d+;\d+R)|(?<!\S)\[\d+;\d+R")
BAD_PATH_CHARS_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]+')
EXIT_MARKER_RE = re.compile(r"__TEST_EXIT__(\d+)")
RUN_SH_DONE_RE = re.compile(r"Modem\s+Link\s+-\s+All\s+Disabled.*?Data\s+Path\s+-\s+Enabled", re.IGNORECASE | re.DOTALL)
FULL_TEST_DONE_RE = re.compile(r"INA_MAIN\s*:\s*[-0-9.]+\s+[-0-9.]+\s+[-0-9.]+.*?>>>", re.IGNORECASE | re.DOTALL)
WINDOWS_PING_OK_RE = re.compile(r"\(\s*0%\s*loss\s*\)", re.IGNORECASE)
POSIX_PING_OK_RE = re.compile(r"\b0%\s+packet loss\b", re.IGNORECASE)

DATA_IP_TARGETS = {
    "sfp1": {
        "target": "10.10.10.15",
        "interface": "Ethernet10",
        "isolation_interface": "Ethernet11",
        "mode": "toggle",
    },
    "sfp2": {
        "target": "10.10.10.15",
        "interface": "Ethernet11",
        "isolation_interface": "Ethernet10",
        "mode": "toggle",
    },
    "sfp3": {
        "target": "10.10.10.20",
        "interface": "Ethernet12",
        "mode": "data_ip",
    },
    "sfp4": {
        "target": "10.10.10.30",
        "interface": "Ethernet13",
        "mode": "data_ip",
    },
}

@dataclass
class CheckResult:
    name: str
    expected: Any
    actual: Any
    passed: bool


SECTION_TITLES = (
    ("version.cpld.", "CPLD Version"),
    ("power_seq.", "Power Sequence"),
    ("sky_loln.", "Skyworks PLL"),
    ("version.fpga.", "FPGA Version"),
    ("txfem_init.", "TXFEM Init"),
    ("rxfem_init.", "RXFEM Init"),
    ("tests.sx4000.", "SX4000 Reset"),
    ("tests.domain.", "Domain Lock Status"),
    ("tests.fpga.", "FPGA Tests"),
    ("tests.eth.login", "Switch Login"),
    ("tests.eth.ping.", "Data IP Ping"),
    ("tests.temp.", "Temperature"),
    ("tests.power.", "Power"),
)

REPORT_LABELS = {
    "version.cpld.build": "CPLD_BUILD",
    "version.cpld.date": "CPLD_DATE",
    "version.cpld.ver": "CPLD_VER",
    "version.fpga.build": "FPGA_BUILD",
    "version.fpga.dig_ver": "FPGA_DIG_VER",
    "version.fpga.gp_ver": "FPGA_GP_VER",
    "version.fpga.ver": "FPGA_VER",
    "sky_loln.sky1.loln": "SKY1.LOLn",
    "sky_loln.sky2.loln": "SKY2.LOLn",
    "txfem_init.dac.chipg": "DAC_CHIP_GRADE",
    "txfem_init.dac.prod_id": "DAC_PRODUCT_ID",
    "txfem_init.dac.type": "DAC_CHIP_TYPE",
    "txfem_init.pll.freq": "PLL_FREQ",
    "txfem_init.pll.lock": "PLL_LOCK",
    "txfem_init.pll.prod_id": "PLL_PRODUCT_ID",
    "txfem_init.pll.type": "PLL_CHIP_TYPE",
    "txfem_init.pll.ven_id": "PLL_VENDOR_ID",
    "rxfem_init.adc.chip_id": "ADC_CHIP_ID",
    "rxfem_init.adc.stat_rx_debug": "ADC_STAT_RX_DEBUG",
    "rxfem_init.adc.stat_status": "ADC_STAT_STATUS",
    "rxfem_init.adc.type": "ADC_TYPE",
    "rxfem_init.adc.ven_id": "ADC_VEN_ID",
    "rxfem_init.pll.freq": "PLL_FREQ",
    "rxfem_init.pll.lock": "PLL_LOCK",
    "rxfem_init.pll.prod_id": "PLL_PRODUCT_ID",
    "rxfem_init.pll.type": "PLL_CHIP_TYPE",
    "rxfem_init.pll.ven_id": "PLL_VENDOR_ID",
}

REPORT_ORDER = {
    "txfem_init.pll.type": 0,
    "txfem_init.pll.prod_id": 1,
    "txfem_init.pll.ven_id": 2,
    "txfem_init.pll.freq": 3,
    "txfem_init.pll.lock": 4,
    "rxfem_init.pll.type": 0,
    "rxfem_init.pll.prod_id": 1,
    "rxfem_init.pll.ven_id": 2,
    "rxfem_init.pll.freq": 3,
    "rxfem_init.pll.lock": 4,
    "tests.fpga.dac_jesd_link": 0,
    "tests.fpga.mdm0_dig_loopback": 1,
    "tests.fpga.mdm1_dig_loopback": 2,
    "tests.fpga.mdm2_dig_loopback": 3,
    "tests.fpga.mdm3_dig_loopback": 4,
    "tests.fpga.mdm0_full_loopback": 5,
    "tests.fpga.mdm1_full_loopback": 6,
    "tests.fpga.mdm2_full_loopback": 7,
    "tests.fpga.mdm3_full_loopback": 8,
    "tests.eth.ping.sfp1": 0,
    "tests.eth.ping.sfp2": 1,
    "tests.eth.ping.sfp3": 2,
    "tests.eth.ping.sfp4": 3,
}

HEX_REPORT_FIELDS = {
    "version.fpga.gp_ver",
    "version.fpga.dig_ver",
}


def strip_ansi(text: str) -> str:
    return ANSI_RE.sub("", text)


def strip_terminal_cpr(text: str) -> str:
    return CPR_RE.sub("", text)


def clean_folder_name(value: str) -> str:
    cleaned = BAD_PATH_CHARS_RE.sub("_", value.strip())
    cleaned = cleaned.strip(" ._")
    if not cleaned:
        raise RuntimeError("Folder name cannot be empty.")
    return cleaned


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
            raise RuntimeError(f"Unsupported indentation at line {line_number}: {raw_line!r}")

        while len(stack) > 1 and indent <= stack[-1][0]:
            stack.pop()

        current = stack[-1][1]
        if ":" not in stripped:
            raise RuntimeError(f"Expected key/value mapping at line {line_number}: {raw_line!r}")

        key, _, remainder = stripped.partition(":")
        key = key.strip()
        remainder = remainder.strip()
        if not key:
            raise RuntimeError(f"Missing key at line {line_number}")

        if remainder == "":
            nested: dict[str, Any] = {}
            current[key] = nested
            stack.append((indent, nested))
        else:
            current[key] = parse_scalar(remainder)

    return root


def load_yaml(path: pathlib.Path) -> dict[str, Any]:
    if yaml is None:
        return load_simple_yaml(path)

    with path.open("r", encoding="utf-8") as fh:
        loaded = yaml.safe_load(fh) or {}
    if not isinstance(loaded, dict):
        raise RuntimeError(f"{path} must contain a YAML mapping.")
    return loaded


def first_match(pattern: str, text: str, flags: int = re.IGNORECASE | re.MULTILINE) -> re.Match[str] | None:
    return re.search(pattern, text, flags)


def canon(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, float):
        return f"{value:g}".lower()
    if isinstance(value, int):
        return str(value).lower()

    text = str(value).strip()
    if not text:
        return ""

    try:
        if text.lower().startswith("0x"):
            return hex(int(text, 16)).lower()
        if re.fullmatch(r"[0-9a-fA-F]+", text) and re.search(r"[a-fA-F]", text):
            return text.upper()
        if re.fullmatch(r"\d+\.0+", text):
            return str(int(float(text)))
    except ValueError:
        pass

    return text.lower()


def equalish(expected: Any, actual: Any) -> bool:
    exp = canon(expected)
    act = canon(actual)
    if exp == act:
        return True

    expected_int = comparable_int(expected)
    actual_int = comparable_int(actual)
    if expected_int is not None and actual_int is not None:
        return expected_int == actual_int

    try:
        if exp.lower().startswith("0x") or act.lower().startswith("0x"):
            return int(exp, 0) == int(act, 0)
    except ValueError:
        return False

    return False


def comparable_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)

    text = str(value).strip()
    if not text:
        return None
    try:
        if text.lower().startswith("0x"):
            return int(text, 16)
        if re.fullmatch(r"[0-9A-Fa-f]+", text) and re.search(r"[A-Fa-f]", text):
            return int(text, 16)
        if re.fullmatch(r"\d+", text):
            return int(text, 10)
    except ValueError:
        return None
    return None


def range_check(expected: dict[str, Any], actual: Any) -> bool:
    if actual is None:
        return False
    try:
        value = float(actual)
        min_value = expected.get("min")
        max_value = expected.get("max")
        if min_value is not None and value < float(min_value):
            return False
        if max_value is not None and value > float(max_value):
            return False
        return True
    except (TypeError, ValueError):
        return False


def report_label(name: str) -> str:
    if name in REPORT_LABELS:
        return REPORT_LABELS[name]
    if name.startswith("power_seq."):
        return name.rsplit(".", 1)[-1].upper()
    if name.startswith("tests.temp."):
        return "TEMP_" + name.rsplit(".", 1)[-1].upper()
    if name.startswith("tests.power."):
        return "POWER_" + name.rsplit(".", 1)[-1].upper()
    if name.startswith("tests.domain."):
        return name.removeprefix("tests.domain.").upper()
    if name.startswith("tests.sx4000."):
        return name.removeprefix("tests.sx4000.").upper()
    if name == "tests.fpga.uplink.snr_min":
        return "UPLINK_SNR"
    if name == "tests.fpga.dac_jesd_link":
        return "DAC_JESD_LINK"
    if name.startswith("tests.fpga.mdm") and name.endswith("_dig_loopback"):
        return name.removeprefix("tests.fpga.").removesuffix("_dig_loopback").upper() + "_DIG_LOOPBACK"
    if name.startswith("tests.fpga.mdm") and name.endswith("_full_loopback"):
        return name.removeprefix("tests.fpga.").removesuffix("_full_loopback").upper() + "_FULL_LOOPBACK"
    if name == "tests.eth.login":
        return "SWITCH_LOGIN"
    if name.startswith("tests.eth.ping."):
        return "DATA_IP_" + name.rsplit(".", 1)[-1].upper()
    if ".jesd." in name:
        return name.removeprefix("txfem_init.jesd.").upper()
    return name


def report_value(value: Any, name: str | None = None) -> str:
    if value is None:
        return "NOT_FOUND"
    if name in HEX_REPORT_FIELDS and isinstance(value, int):
        return f"0x{value:X}"
    if name and name.startswith("tests.temp."):
        try:
            return f"{float(value):.1f}"
        except (TypeError, ValueError):
            return str(value)
    if isinstance(value, float):
        return f"{value:.2f}".rstrip("0").rstrip(".")
    return str(value)


def report_result_line(result: CheckResult) -> str:
    if result.passed:
        status = "PASS"
    elif result.actual is None:
        status = "NOT_FOUND"
    else:
        status = "FAIL"

    label = report_label(result.name)
    actual = report_value(result.actual, result.name)
    if result.passed:
        return f"[{status}] {label}: '{actual}'"
    if result.actual is None:
        return f"[{status}] {label}: expected='{report_value(result.expected, result.name)}'"
    return f"[{status}] {label}: '{actual}' (expected: '{report_value(result.expected, result.name)}')"


def report_sort_key(result: CheckResult) -> tuple[int, str]:
    return (REPORT_ORDER.get(result.name, 100), report_label(result.name))


def ordered_report_results(results: list[CheckResult]) -> list[CheckResult]:
    ordered: list[CheckResult] = []
    used: set[int] = set()
    for prefix, _title in SECTION_TITLES:
        section_results = [
            (index, result)
            for index, result in enumerate(results)
            if result.name.startswith(prefix)
        ]
        for index, result in sorted(section_results, key=lambda item: report_sort_key(item[1])):
            ordered.append(result)
            used.add(index)

    for index, result in enumerate(results):
        if index not in used:
            ordered.append(result)
    return ordered


def add_check(results: list[CheckResult], name: str, expected: Any, actual: Any) -> None:
    if expected is None:
        return
    results.append(CheckResult(name=name, expected=expected, actual=actual, passed=equalish(expected, actual)))


def add_range_check(results: list[CheckResult], name: str, expected: dict[str, Any], actual: Any) -> None:
    results.append(CheckResult(name=name, expected=f"{expected.get('min')}..{expected.get('max')}", actual=actual, passed=range_check(expected, actual)))


def parse_output(output: str) -> dict[str, Any]:
    text = strip_ansi(output)
    data: dict[str, Any] = {}

    sonic_sv = first_match(
        r"SONiC\s+Software\s+Version:\s*SONiC\.SONiC-LSBB-Ver\.([^\s]+)",
        text,
        re.IGNORECASE,
    )
    if sonic_sv:
        data["sonic.sv"] = sonic_sv.group(1)

    sonic_os = first_match(r"SONiC\s+OS\s+Version:\s*([^\s]+)", text, re.IGNORECASE)
    if sonic_os:
        data["sonic.os"] = sonic_os.group(1)

    for component, version in re.findall(
        r"^\s*(?:\S+\s+\S+\s+)?(U-Boot|ONIE-VERSION)\s+(\S+)",
        text,
        re.IGNORECASE | re.MULTILINE,
    ):
        data[f"sonic.firmware.{component}"] = version

    cpld = first_match(
        r"CPLD\s+Ver:\s*([0-9.]+),\s*Build:\s*(0x[0-9a-fA-F]+|\d+),\s*Compiled\s+Date:\s*([0-9-]+)",
        text,
    )
    if cpld:
        data["version.cpld.ver"] = cpld.group(1)
        data["version.cpld.build"] = cpld.group(2)
        data["version.cpld.date"] = cpld.group(3)

    fpga_info = first_match(
        r"FPGA\s+GP\s+Container\s+Info:\s*"
        r".*?\bGP\s+Cont\s+Ver:\s*(0x[0-9a-fA-F]+)"
        r".*?^\s*Version\s*:\s*([0-9.]+)"
        r".*?^\s*Build\s*:\s*(0x[0-9a-fA-F]+|\d+)",
        text,
        re.IGNORECASE | re.MULTILINE | re.DOTALL,
    )
    dig_ver = first_match(
        r"FPGA\s+Dig\s+Container\s+Info:\s*.*?\bDig\s+Cont\s+Ver:\s*(0x[0-9a-fA-F]+)",
        text,
        re.IGNORECASE | re.MULTILINE | re.DOTALL,
    )
    if fpga_info:
        data["version.fpga.gp_ver"] = fpga_info.group(1)
        data["version.fpga.ver"] = fpga_info.group(2)
        data["version.fpga.build"] = fpga_info.group(3)
    if dig_ver:
        data["version.fpga.dig_ver"] = dig_ver.group(1)

    for domain, result in re.findall(
        r"Power\s+Seq\s+Domain:\s*([A-Za-z0-9_]+)\s*,\s*Result\s*=\s*([0-9a-fA-FxX]+)",
        text,
        re.IGNORECASE,
    ):
        data[f"power_seq.{domain.lower()}"] = result

    loln_values = re.findall(r"\bLOLn\s*=\s*(\d+)", text, re.IGNORECASE)
    for index, value in enumerate(loln_values[:2], start=1):
        data[f"sky_loln.sky{index}.loln"] = value

    for clu_id, pll_state in re.findall(r"CLU\[(\d+)\].*?\bpll\s*=\s*([A-Za-z0-9_]+)", text, re.IGNORECASE):
        data[f"sky_loln.sky{clu_id}.id"] = clu_id
        data[f"sky_loln.sky{clu_id}.pll"] = pll_state

    tx_pll = first_match(
        r"TXFEM\s+ADF4368\s*\(TARGET\):\s*freq\s*=\s*(\d+)\s*MHz\s+lock\s*=\s*(\d+)",
        text,
    )
    if not tx_pll:
        tx_pll = first_match(
            r"TXFEM\s+ADF4368\s+Initilization\s*\(Fout\s*=\s*(\d+)\s*\[MHz\]\)\s+finished\s+successfully\s+\(Lock\s*=\s*(\d+)\)",
            text,
        )
    if tx_pll:
        data["txfem_init.pll.freq"] = tx_pll.group(1)
        data["txfem_init.pll.lock"] = tx_pll.group(2)

    tx_pll_id = first_match(
        r"===\s+TXFEM\s+Init\s+=+.*?"
        r"ADF4368:\s*ChipType\s*=\s*(0x[0-9a-fA-F]+),\s*"
        r"ProductID\s*=\s*(0x[0-9a-fA-F]+),\s*"
        r"VendorID\s*=\s*(0x[0-9a-fA-F]+)",
        text,
        re.IGNORECASE | re.MULTILINE | re.DOTALL,
    )
    if tx_pll_id:
        data["txfem_init.pll.type"] = tx_pll_id.group(1)
        data["txfem_init.pll.prod_id"] = tx_pll_id.group(2)
        data["txfem_init.pll.ven_id"] = tx_pll_id.group(3)

    dac = first_match(
        r"AD9175:\s*ChipType\s*=\s*(0x[0-9a-fA-F]+),\s*ChipGrade\s*=\s*(0x[0-9a-fA-F]+),\s*ProductID\s*=\s*(0x[0-9a-fA-F]+)",
        text,
    )
    if dac:
        data["txfem_init.dac.type"] = dac.group(1)
        data["txfem_init.dac.chipg"] = dac.group(2)
        data["txfem_init.dac.prod_id"] = dac.group(3)
    if first_match(r"DAC\s+AD9175\s+-\s+Configuration\s+Done\s*!", text):
        data["txfem_init.dac.config_done"] = True

    for dac_id, field, value in re.findall(r"DAC([01]),\s*([A-Z_]+)\s*=\s*(0x[0-9a-fA-F]+)", text):
        key = field.lower()
        data[f"txfem_init.jesd.dac{dac_id}.{key}"] = value

    rx_pll = first_match(
        r"RXFEM\s+ADF4368\s*\(TARGET\):\s*freq\s*=\s*(\d+)\s*MHz\s+lock\s*=\s*(\d+)",
        text,
    )
    if not rx_pll:
        rx_pll = first_match(
            r"RXFEM\s+ADF4368\s+Initilization\s*\(Fout\s*=\s*(\d+)\s*\[MHz\]\)\s+finished\s+successfully\s+\(Lock\s*=\s*(\d+)\)",
            text,
        )
    if rx_pll:
        data["rxfem_init.pll.freq"] = rx_pll.group(1)
        data["rxfem_init.pll.lock"] = rx_pll.group(2)

    rx_pll_id = first_match(
        r"===\s+RXFEM\s+Init\s+=+.*?"
        r"ADF4368:\s*ChipType\s*=\s*(0x[0-9a-fA-F]+),\s*"
        r"ProductID\s*=\s*(0x[0-9a-fA-F]+),\s*"
        r"VendorID\s*=\s*(0x[0-9a-fA-F]+)",
        text,
        re.IGNORECASE | re.MULTILINE | re.DOTALL,
    )
    if rx_pll_id:
        data["rxfem_init.pll.type"] = rx_pll_id.group(1)
        data["rxfem_init.pll.prod_id"] = rx_pll_id.group(2)
        data["rxfem_init.pll.ven_id"] = rx_pll_id.group(3)

    adc = first_match(
        r"AD9213:\s*ChipType\s*=\s*(0x[0-9a-fA-F]+),\s*Chip\s+ID\s*=\s*(0x[0-9a-fA-F]+),\s*Vendor\s+ID\s*=\s*(0x[0-9a-fA-F]+)",
        text,
    )
    if adc:
        data["rxfem_init.adc.type"] = adc.group(1)
        data["rxfem_init.adc.chip_id"] = adc.group(2)
        data["rxfem_init.adc.ven_id"] = adc.group(3)
    if first_match(r"ADC\s+AD9213\s+-\s+Configuration\s+Done\s*!", text):
        data["rxfem_init.adc.config_done"] = True

    for field, value in re.findall(r"FPGA\s+JESD\s+ADC\s+-\s*(STAT_[A-Z_]+)\s*=\s*(0x[0-9a-fA-F]+)", text):
        data[f"rxfem_init.adc.{field.lower()}"] = value

    for sx_id, state in re.findall(r"\b(SX[12])\s+-\s+Reset\s+Done,\s+Reset\s+State\s*=\s*([A-Z_]+)", text, re.IGNORECASE):
        data[f"tests.sx4000.{sx_id.lower()}.reset"] = "done"
        data[f"tests.sx4000.{sx_id.lower()}.reset_state"] = state.lower()

    for sx_id, state in re.findall(
        r"\b(SX[12])\s+-\s+Bootstrap\s+Override\s+completed\s+Successfully,\s+Reset\s+State\s*=\s*([A-Z_]+)",
        text,
        re.IGNORECASE,
    ):
        data[f"tests.sx4000.{sx_id.lower()}.state"] = state.lower()

    domain_starts = list(re.finditer(r"^Domain:\s*([A-Z0-9_]+)\s*$", text, re.IGNORECASE | re.MULTILINE))
    for index, match in enumerate(domain_starts):
        domain = match.group(1).lower()
        end = domain_starts[index + 1].start() if index + 1 < len(domain_starts) else len(text)
        block = text[match.end():end]
        status = first_match(
            r"FPGA_MB_LOCK_status\s*=\s*(0x[0-9a-fA-F]+),\s*"
            r"FPGA_SH_LOCK_status\s*=\s*(0x[0-9a-fA-F]+),\s*"
            r"SX_EMB_LOCK_status\s*=\s*(0x[0-9a-fA-F]+),\s*"
            r"SX_SH_LOCK_status\s*=\s*(0x[0-9a-fA-F]+)",
            block,
        )
        if status:
            data[f"tests.domain.{domain}.fpga_mb_lock_status"] = status.group(1)
            data[f"tests.domain.{domain}.fpga_mb_sh_lock_status"] = status.group(2)
            data[f"tests.domain.{domain}.sx_emb_lock_status"] = status.group(3)
            data[f"tests.domain.{domain}.sx_sh_lock_status"] = status.group(4)

    report_domain_re = re.compile(
        r"^\[(?:PASS|FAIL)\]\s+"
        r"(SX[12]_ANTD[01])\."
        r"(FPGA_MB_LOCK_STATUS|FPGA_MB_SH_LOCK_STATUS|SX_EMB_LOCK_STATUS|SX_SH_LOCK_STATUS)"
        r":\s*'([^']+)'",
        re.IGNORECASE | re.MULTILINE,
    )
    for domain, field, value in report_domain_re.findall(text):
        data[f"tests.domain.{domain.lower()}.{field.lower()}"] = value

    uplink_sections = re.findall(
        r"===\s*FPGA\s+UpLink\s+Check\s+\(and\s+Fix\).*?(?=Entering\s+SONIC|Domain\s+:\s+Temperature|\Z)",
        text,
        re.IGNORECASE | re.DOTALL,
    )
    snr_source = "\n".join(uplink_sections) if uplink_sections else ""
    snr_values = [float(value) for value in re.findall(r"\bSNR\s*:\s*(-?\d+(?:\.\d+)?)", snr_source, re.IGNORECASE)]
    if snr_values:
        data["tests.fpga.uplink.snr_min"] = sum(snr_values) / len(snr_values)
        data["tests.fpga.uplink.snr_count"] = len(snr_values)
        data["tests.fpga.uplink.status"] = "fail" if re.search(r"FPGA\s+UpLink\s+Check\s+-\s+Fail", snr_source, re.IGNORECASE) else "pass"

    dac_jesd = first_match(r"FPGA\s*->\s*DAC\s+JESD\s+Link\s*-\s*(Pass|Fail)", text, re.IGNORECASE)
    if dac_jesd:
        data["tests.fpga.dac_jesd_link"] = dac_jesd.group(1).lower()

    for modem, snr, status in re.findall(
        r"MDM(\d+)\s*<->\s*FPGA\s+Dig\s+Loopback\s+Test,\s*SNR\s*=\s*(-?\d+(?:\.\d+)?)\s*-\s*(Pass|Fail)",
        text,
        re.IGNORECASE,
    ):
        data[f"tests.fpga.mdm{modem}_dig_loopback"] = float(snr)
        data[f"tests.fpga.mdm{modem}_dig_loopback_status"] = status.lower()

    for modem, snr, status in re.findall(
        r"Full\s+Loopback\s+Test,\s*MDM(\d+),\s*SNR\s*=\s*(-?\d+(?:\.\d+)?)\s*-\s*(Pass|Fail)",
        text,
        re.IGNORECASE,
    ):
        data[f"tests.fpga.mdm{modem}_full_loopback"] = float(snr)
        data[f"tests.fpga.mdm{modem}_full_loopback_status"] = status.lower()

    eth_login = re.findall(r"Entering\s+SONIC\s+-\s+(Pass|Failed)", text, re.IGNORECASE)
    if eth_login:
        data["tests.eth.login"] = eth_login[-1].lower()

    temp_sections = re.findall(
        r"Domain\s*:\s*Temperature\s*\[Deg\].*?(?=Domain\s*:\s*Volt|\Z)",
        text,
        re.IGNORECASE | re.DOTALL,
    )
    for temp_section in temp_sections:
        for name, raw_value in re.findall(r"^\s*([A-Z0-9_]+)\s*:\s*(N/A|-?\d+(?:\.\d+)?)\s*$", temp_section, re.MULTILINE):
            key = name.lower()
            if raw_value.upper() == "N/A":
                data[f"tests.temp.{key}"] = None
            else:
                data[f"tests.temp.{key}"] = float(raw_value)

    for name, _volt, _current, power in re.findall(
        r"^\s*(INA_[A-Z0-9_]+)\s*:\s*(-?\d+(?:\.\d+)?)\s+(-?\d+(?:\.\d+)?)\s+(-?\d+(?:\.\d+)?)\s*$",
        text,
        re.MULTILINE,
    ):
        data[f"tests.power.{name.lower()}"] = float(power)

    return data


def flatten(mapping: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in mapping.items():
        name = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(value, dict):
            out.update(flatten(value, name))
        else:
            out[name] = value
    return out


def compare(
    expected: dict[str, Any],
    actual: dict[str, Any],
    include_full_tests: bool = True,
    include_ping_tests: bool = True,
    include_loopback_tests: bool = True,
) -> list[CheckResult]:
    flat_expected = flatten(expected)
    results: list[CheckResult] = []
    comparable_roots = ("version.", "power_seq.", "sky_loln.", "txfem_init.", "rxfem_init.")

    for name, expected_value in sorted(flat_expected.items()):
        if not name.startswith(comparable_roots):
            continue
        if re.fullmatch(r"sky_loln\.sky\d+\.id", name):
            continue
        if re.fullmatch(r"[rt]xfem_init\.pll\.(type|prod_id|ven_id)", name) and name not in actual:
            continue
        add_check(results, name, expected_value, actual.get(name))

    if not include_full_tests:
        return results

    tests_cfg = expected.get("tests", {})
    if isinstance(tests_cfg, dict):
        sx_cfg = tests_cfg.get("sx4000", {})
        if isinstance(sx_cfg, dict):
            for sx_name, sx_expected in sx_cfg.items():
                if isinstance(sx_expected, dict):
                    for field in ("reset", "state"):
                        if field in sx_expected:
                            add_check(results, f"tests.sx4000.{sx_name}.{field}", sx_expected[field], actual.get(f"tests.sx4000.{sx_name}.{field}"))

        domain_cfg = tests_cfg.get("domain", {})
        if isinstance(domain_cfg, dict):
            for domain_name, domain_expected in domain_cfg.items():
                if isinstance(domain_expected, dict):
                    for field, expected_value in domain_expected.items():
                        add_check(results, f"tests.domain.{domain_name}.{field}", expected_value, actual.get(f"tests.domain.{domain_name}.{field}"))

        fpga_cfg = tests_cfg.get("fpga", {})
        uplink_cfg = fpga_cfg.get("uplink", {}) if isinstance(fpga_cfg, dict) else {}
        if isinstance(uplink_cfg, dict) and "snr_min" in uplink_cfg:
            actual_snr = actual.get("tests.fpga.uplink.snr_min")
            passed = actual_snr is not None and float(actual_snr) >= float(uplink_cfg["snr_min"])
            results.append(CheckResult("tests.fpga.uplink.snr_min", f">= {uplink_cfg['snr_min']}", actual_snr, passed))
        if isinstance(fpga_cfg, dict):
            if "dac_jesd_link" in fpga_cfg:
                add_check(results, "tests.fpga.dac_jesd_link", fpga_cfg["dac_jesd_link"], actual.get("tests.fpga.dac_jesd_link"))
            if include_loopback_tests:
                for modem in range(4):
                    dig_key = f"mdm{modem}_dig_loopback"
                    dig_limits = fpga_cfg.get(dig_key)
                    if isinstance(dig_limits, dict) and ("min" in dig_limits or "max" in dig_limits):
                        dig_actual = actual.get(f"tests.fpga.{dig_key}")
                        dig_status = actual.get(f"tests.fpga.{dig_key}_status")
                        dig_passed = dig_actual is not None and dig_status == "pass" and range_check(dig_limits, dig_actual)
                        dig_display = dig_actual if dig_status in (None, "pass") else f"{report_value(dig_actual)} ({dig_status})"
                        results.append(CheckResult(f"tests.fpga.{dig_key}", f"{dig_limits.get('min')}..{dig_limits.get('max')}", dig_display, dig_passed))
                    full_key = f"mdm{modem}_full_loopback"
                    full_limits = fpga_cfg.get(full_key)
                    if isinstance(full_limits, dict) and ("min" in full_limits or "max" in full_limits):
                        full_actual = actual.get(f"tests.fpga.{full_key}")
                        full_status = actual.get(f"tests.fpga.{full_key}_status")
                        full_passed = full_actual is not None and full_status == "pass" and range_check(full_limits, full_actual)
                        full_display = full_actual if full_status in (None, "pass") else f"{report_value(full_actual)} ({full_status})"
                        results.append(CheckResult(f"tests.fpga.{full_key}", f"{full_limits.get('min')}..{full_limits.get('max')}", full_display, full_passed))

        eth_cfg = tests_cfg.get("eth", {})
        if isinstance(eth_cfg, dict) and "login" in eth_cfg:
            add_check(results, "tests.eth.login", eth_cfg["login"], actual.get("tests.eth.login"))
        ping_cfg = eth_cfg.get("ping", {}) if isinstance(eth_cfg, dict) else {}
        if include_ping_tests and isinstance(ping_cfg, dict):
            for sfp_name in DATA_IP_TARGETS:
                enabled = ping_cfg.get(f"{sfp_name}_en")
                if enabled is not None and int(enabled) == 1:
                    add_check(results, f"tests.eth.ping.{sfp_name}", "pass", actual.get(f"tests.eth.ping.{sfp_name}"))

        temp_cfg = tests_cfg.get("temp", {})
        if isinstance(temp_cfg, dict):
            limits = temp_cfg.get("limits", {})
            enabled = get_nested(temp_cfg, "devices.enabled", {})
            if isinstance(limits, dict) and isinstance(enabled, dict):
                for device_name, enabled_value in enabled.items():
                    if int(enabled_value) == 1:
                        add_range_check(results, f"tests.temp.{device_name}", limits, actual.get(f"tests.temp.{device_name}"))

        power_cfg = tests_cfg.get("power", {})
        if isinstance(power_cfg, dict):
            for rail_name, rail_limits in power_cfg.items():
                if isinstance(rail_limits, dict) and ("min" in rail_limits or "max" in rail_limits):
                    add_range_check(results, f"tests.power.{rail_name}", rail_limits, actual.get(f"tests.power.{rail_name}"))

    return results


def get_nested(mapping: dict[str, Any], path: str, default: Any = None) -> Any:
    current: Any = mapping
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            return default
        current = current[part]
    return current


def default_remote_dir(setup: dict[str, Any]) -> str:
    raw = str(get_nested(setup, "dut.utils_path") or "").strip()
    if not raw:
        return "/root/LSBB_Utils"
    if "\\" in raw or re.match(r"^[A-Za-z]:", raw):
        return f"/root/{pathlib.PureWindowsPath(raw).name}"
    return raw


def apply_connection_defaults(args: argparse.Namespace, setup: dict[str, Any]) -> None:
    args.host = args.host or get_nested(setup, "dut.final_ip")
    args.user = args.user or get_nested(setup, "dut.login")
    args.password = args.password or get_nested(setup, "dut.password")

    if not hasattr(args, "remote_dir"):
        args.remote_dir = default_remote_dir(setup)


def build_ssh_command(args: argparse.Namespace) -> list[str]:
    target = args.host
    if args.user:
        target = f"{args.user}@{target}"

    command = args.remote_command or f"cd {args.remote_dir} && sh ./{args.script}"
    ssh_cmd = ["ssh", "-o", f"ConnectTimeout={args.connect_timeout}"]
    if args.port:
        ssh_cmd.extend(["-p", str(args.port)])
    if args.identity_file:
        ssh_cmd.extend(["-i", args.identity_file])
    if args.ssh_option:
        for option in args.ssh_option:
            ssh_cmd.extend(["-o", option])
    ssh_cmd.extend([target, command])
    return ssh_cmd


def ssh_connect(args: argparse.Namespace) -> Any:
    if paramiko is None:
        raise RuntimeError("paramiko is not installed.")

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    connect_kwargs: dict[str, Any] = {
        "hostname": args.host,
        "username": args.user,
        "password": args.password,
        "timeout": args.connect_timeout,
        "look_for_keys": True,
    }
    if args.port:
        connect_kwargs["port"] = args.port
    if args.identity_file:
        connect_kwargs["key_filename"] = args.identity_file

    client.connect(**connect_kwargs)
    return client


def run_paramiko_exec(args: argparse.Namespace, command: str) -> str:
    client = ssh_connect(args)
    try:
        stdin, stdout, stderr = client.exec_command(command, timeout=args.timeout)
        stdin.close()
        output = stdout.read().decode(errors="replace")
        error_output = stderr.read().decode(errors="replace")
        exit_status = stdout.channel.recv_exit_status()
    finally:
        client.close()

    combined = output + error_output
    if exit_status != 0:
        raise RuntimeError(f"SSH command failed with exit code {exit_status}.\n{combined.strip()}")
    return combined


def run_paramiko_interactive(args: argparse.Namespace, command: str) -> str:
    client = ssh_connect(args)
    output_parts: list[str] = []
    sent_system_init = False
    saw_sx4000_prompt = False
    sent_full_test_command = False
    started = time.monotonic()
    last_progress = started
    exit_status: int | None = None
    marker = "__TEST_EXIT__"
    args.output_streamed = True
    full_like_mode = args.mode in {"full", "skip_eth"}

    try:
        channel = client.invoke_shell()
        channel.settimeout(0.0)
        channel.send(f"({command}); printf '\\n{marker}%s\\n' $?\n")

        while True:
            if time.monotonic() - started > args.timeout:
                raise TimeoutError(f"Timed out after {args.timeout} seconds waiting for SSH command to finish.")

            if channel.recv_ready():
                chunk = channel.recv(4096).decode(errors="replace")
                output_parts.append(chunk)
                print(strip_terminal_cpr(chunk), end="", flush=True)
                last_progress = time.monotonic()

                clean_output = strip_ansi("".join(output_parts))
                if args.mode == "run-sh" and RUN_SH_DONE_RE.search(clean_output):
                    print("\n[INFO] run.sh completion marker detected.")
                    exit_status = 0
                    break

                if full_like_mode and not sent_full_test_command and RUN_SH_DONE_RE.search(clean_output) and ">>>" in clean_output:
                    channel.send("lsbb_cil_test()\r")
                    sent_full_test_command = True
                    print("\n[INFO] Started full test: lsbb_cil_test()")

                if full_like_mode and FULL_TEST_DONE_RE.search(clean_output):
                    if args.mode == "full":
                        print("\n[INFO] Ping test starting.")
                    exit_status = 0
                    break

                match = EXIT_MARKER_RE.search(clean_output)
                if match:
                    exit_status = int(match.group(1))
                    break

                if (
                    not sent_system_init
                    and "Press any key to skip System Init" in clean_output
                ):
                    sent_system_init = True
                    if args.system_init_response is not None:
                        response = args.system_init_response
                        channel.send(response + "\r")
                        print(f"\n[INFO] Sent system-init prompt response: {response!r}")
                    else:
                        print("\n[INFO] System Init prompt detected; waiting 3 seconds.")
                        time.sleep(3)

                if (
                    not saw_sx4000_prompt
                    and "=== SX4000 Init" in clean_output
                    and "Press 3 - to Load both SX4000 #1 & #2" in clean_output
                ):
                    saw_sx4000_prompt = True
                    print("\n[INFO] SX4000 prompt detected; waiting for next output.")
            else:
                if time.monotonic() - last_progress > 30:
                    if full_like_mode and not sent_full_test_command:
                        print("[INFO] Waiting for Python prompt to start full test...", flush=True)
                    elif full_like_mode:
                        print("[INFO] Waiting for full test output...", flush=True)
                    else:
                        print("[INFO] Waiting for next output...", flush=True)
                    last_progress = time.monotonic()
                if channel.closed:
                    break
                time.sleep(0.1)
    finally:
        client.close()

    combined = strip_terminal_cpr("".join(output_parts))
    combined = EXIT_MARKER_RE.sub("", combined)
    if exit_status is None:
        raise RuntimeError(f"SSH command ended before exit marker was found.\n{combined.strip()}")
    if exit_status != 0:
        raise RuntimeError(f"SSH command failed with exit code {exit_status}.\n{combined.strip()}")
    return combined


def run_ssh(args: argparse.Namespace) -> str:
    command = args.remote_command or f"cd {args.remote_dir} && sh ./{args.script}"
    if args.password and paramiko is not None and not args.force_openssh:
        print(f"[TX] ssh {args.user}@{args.host} {command}")
        if args.interactive:
            return run_paramiko_interactive(args, command)
        return run_paramiko_exec(args, command)

    if args.password and paramiko is None and not args.force_openssh:
        print("[WARN] paramiko is not installed, so password SSH cannot be automated. Falling back to OpenSSH.")

    ssh_cmd = build_ssh_command(args)
    print(f"[TX] {' '.join(ssh_cmd)}")
    completed = subprocess.run(ssh_cmd, text=True, capture_output=True, timeout=args.timeout)
    output = (completed.stdout or "") + (completed.stderr or "")
    if completed.returncode != 0:
        raise RuntimeError(f"SSH command failed with exit code {completed.returncode}.\n{output.strip()}")
    return output


def check_nxp_login(args: argparse.Namespace) -> str:
    remote_dir = shlex.quote(str(args.remote_dir))
    command = (
        "printf 'NXP_LOGIN_CHECK user='; whoami; "
        "printf 'NXP_LOGIN_CHECK host='; hostname; "
        f"test -d {remote_dir}"
    )
    original_command = args.remote_command
    args.remote_command = command
    try:
        output = run_ssh(args)
    finally:
        args.remote_command = original_command

    expected_user = str(args.user or "").strip()
    if expected_user and f"NXP_LOGIN_CHECK user={expected_user}" not in strip_ansi(output):
        raise RuntimeError(f"NXP login check failed. Expected SSH user {expected_user!r}.\n{output.strip()}")

    print("[INFO] NXP SSH login check passed.")
    return output


def serial_wait(
    uart: Any,
    patterns: dict[str, str],
    timeout: float,
    buffer: str = "",
    echo: bool = True,
    match_start: int = 0,
) -> tuple[str | None, str]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        waiting = getattr(uart, "in_waiting", 0) or 0
        chunk = uart.read(waiting or 1)
        if chunk:
            text = chunk.decode(errors="replace")
            buffer += text
            if echo:
                print(text, end="", flush=True)
            clean_buffer = strip_ansi(buffer[match_start:])
            for name, pattern in patterns.items():
                if re.search(pattern, clean_buffer, re.IGNORECASE | re.MULTILINE):
                    return name, buffer
        else:
            time.sleep(0.05)
    return None, buffer


def check_switch_uart_login(setup: dict[str, Any]) -> str:
    if serial is None:
        raise RuntimeError("pyserial is not installed. Install it or use --skip-switch-login-check.")

    settings = switch_uart_login_settings(setup)
    port = settings["port"]
    baudrate = settings["baudrate"]

    if not port:
        raise RuntimeError("Missing serial.switch.port in script_setup.yaml.")

    print(f"[TX] uart switch {port} @ {baudrate}")
    output = ""
    try:
        with serial.Serial(port=port, baudrate=baudrate, timeout=0.2, write_timeout=1) as uart:
            time.sleep(min(settings["open_timeout"], 1.0))
            uart.write(b"\r\n")
            uart.flush()
            matched, output = serial_wait(
                uart,
                {
                    "shell": shell_prompt_line_pattern(settings["shell_prompt"]),
                    "login": re.escape(settings["login_prompt"]),
                    "password": r"password\s*:",
                },
                settings["prompt_timeout"],
                output,
            )

            output = finish_switch_uart_login(uart, settings, matched, output, echo=True)
            output, version_output = run_switch_uart_command_capture(uart, setup, "show version", output)
            print(version_output, end="" if version_output.endswith("\n") else "\n")
            output, firmware_output = run_switch_uart_command_capture(uart, setup, "show platform firmware status", output)
            print(firmware_output, end="" if firmware_output.endswith("\n") else "\n")
            print("\n[INFO] Switch login Passed.")
            return output
    except OSError as exc:
        raise RuntimeError(f"Switch UART login failed on {port}: {exc}") from exc


def switch_uart_login_settings(setup: dict[str, Any]) -> dict[str, Any]:
    port = get_nested(setup, "serial.switch.port")
    if not port:
        raise RuntimeError("Missing serial.switch.port in script_setup.yaml.")
    return {
        "port": str(port),
        "baudrate": int(get_nested(setup, "serial.switch.baudrate", 115200)),
        "username": str(get_nested(setup, "sonic.login", "admin")),
        "password": str(get_nested(setup, "sonic.password", "admin")),
        "login_prompt": str(get_nested(setup, "sonic.login_prompt", "sonic login:")),
        "shell_prompt": str(get_nested(setup, "sonic.prompt", "admin@sonic:~$")),
        "open_timeout": float(get_nested(setup, "timeouts.serial_open_seconds", 10)),
        "prompt_timeout": float(get_nested(setup, "timeouts.prompt_wait_seconds", 15)),
    }


def shell_prompt_line_pattern(prompt: str) -> str:
    return re.escape(prompt) + r"[ \t]*(?:\r?\n|$)"


def finish_switch_uart_login(
    uart: Any,
    settings: dict[str, Any],
    matched: str | None,
    buffer: str,
    echo: bool,
) -> str:
    prompt_line = shell_prompt_line_pattern(settings["shell_prompt"])
    retries = 0
    while retries < 2:
        if matched == "shell":
            return buffer

        if matched == "login":
            match_start = len(buffer)
            uart.write((settings["username"] + "\r\n").encode())
            uart.flush()
            buffer += settings["username"] + "\n"
            matched, buffer = serial_wait(
                uart,
                {"password": r"password\s*:", "shell": prompt_line},
                settings["prompt_timeout"],
                buffer,
                echo=echo,
                match_start=match_start,
            )
            continue

        if matched == "password":
            match_start = len(buffer)
            uart.write((settings["password"] + "\r\n").encode())
            uart.flush()
            buffer += "<password>\n"
            matched, buffer = serial_wait(
                uart,
                {
                    "shell": prompt_line,
                    "login": re.escape(settings["login_prompt"]),
                    "login_failed": r"login\s+incorrect|authentication\s+failed",
                },
                max(settings["prompt_timeout"], 30),
                buffer,
                echo=echo,
                match_start=match_start,
            )
            continue

        if matched == "login_failed":
            retries += 1
            match_start = len(buffer)
            uart.write(b"\r\n")
            uart.flush()
            matched, buffer = serial_wait(
                uart,
                {"login": re.escape(settings["login_prompt"]), "shell": prompt_line},
                settings["prompt_timeout"],
                buffer,
                echo=echo,
                match_start=match_start,
            )
            continue

        break

    raise RuntimeError(f"Switch UART login failed: did not see prompt {settings['shell_prompt']!r}.")


def ensure_switch_uart_shell(uart: Any, setup: dict[str, Any], buffer: str = "") -> str:
    settings = switch_uart_login_settings(setup)
    prompt_line = shell_prompt_line_pattern(settings["shell_prompt"])
    match_start = len(buffer)
    uart.write(b"\r\n")
    uart.flush()
    matched, buffer = serial_wait(
        uart,
        {
            "shell": prompt_line,
            "login": re.escape(settings["login_prompt"]),
            "password": r"password\s*:",
        },
        settings["prompt_timeout"],
        buffer,
        echo=False,
        match_start=match_start,
    )
    return finish_switch_uart_login(uart, settings, matched, buffer, echo=False)


def run_switch_uart_command(uart: Any, setup: dict[str, Any], command: str, buffer: str) -> str:
    settings = switch_uart_login_settings(setup)
    prompt_line = shell_prompt_line_pattern(settings["shell_prompt"])
    waiting = getattr(uart, "in_waiting", 0) or 0
    if waiting:
        buffer += uart.read(waiting).decode(errors="replace")
    match_start = len(buffer)
    uart.write((command + "\r\n").encode())
    uart.flush()
    matched, buffer = serial_wait(
        uart,
        {
            "shell": prompt_line,
            "sudo_password": r"\[sudo\]\s+password|password\s+for\s+\S+\s*:",
        },
        max(settings["prompt_timeout"], 30),
        buffer,
        echo=False,
        match_start=match_start,
    )
    if matched == "sudo_password":
        match_start = len(buffer)
        uart.write((settings["password"] + "\r\n").encode())
        uart.flush()
        buffer += "<password>\n"
        matched, buffer = serial_wait(
            uart,
            {"shell": prompt_line},
            max(settings["prompt_timeout"], 30),
            buffer,
            echo=False,
            match_start=match_start,
        )
    if matched != "shell":
        raise RuntimeError(f"Switch UART command timed out: {command}")
    if command.strip().startswith("sudo "):
        time.sleep(8)
    return buffer


def run_switch_uart_command_capture(uart: Any, setup: dict[str, Any], command: str, buffer: str) -> tuple[str, str]:
    start = len(buffer)
    buffer = run_switch_uart_command(uart, setup, command, buffer)
    return buffer, buffer[start:]


def append_switch_debug_log(debug_log: pathlib.Path | None, text: str) -> None:
    if debug_log is None:
        return
    debug_log.parent.mkdir(parents=True, exist_ok=True)
    with debug_log.open("a", encoding="utf-8", errors="replace") as fh:
        fh.write(text)
        if text and not text.endswith("\n"):
            fh.write("\n")


def run_switch_uart_command_logged(
    uart: Any,
    setup: dict[str, Any],
    command: str,
    buffer: str,
    debug_log: pathlib.Path | None,
) -> str:
    start = len(buffer)
    append_switch_debug_log(debug_log, f"\n[TX] {command}\n")
    buffer = run_switch_uart_command(uart, setup, command, buffer)
    append_switch_debug_log(debug_log, buffer[start:])
    return buffer


def run_switch_uart_command_capture_logged(
    uart: Any,
    setup: dict[str, Any],
    command: str,
    buffer: str,
    debug_log: pathlib.Path | None,
) -> tuple[str, str]:
    start = len(buffer)
    buffer = run_switch_uart_command_logged(uart, setup, command, buffer, debug_log)
    return buffer, buffer[start:]


def parse_interface_status(output: str, interface: str) -> tuple[str | None, str | None]:
    status_re = re.compile(
        rf"^\s*{re.escape(interface)}\s+\S+\s+\S+\s+\S+\s+\S+\s+\S+\s+\S+\s+"
        r"(?P<oper>up|down)\s+(?P<admin>up|down)\b",
        re.IGNORECASE | re.MULTILINE,
    )
    match = status_re.search(strip_ansi(output))
    if not match:
        return None, None
    return match.group("oper").lower(), match.group("admin").lower()


def read_switch_interface_status(
    uart: Any | None,
    setup: dict[str, Any],
    sfp_name: str,
    interface: str,
    buffer: str,
    debug_log: pathlib.Path | None = None,
) -> tuple[str | None, str | None, str]:
    if uart is None:
        print(f"[INFO] Skipping interface status for {sfp_name}; switch UART config is disabled.")
        return None, None, buffer
    print(f"[INFO] Checking {sfp_name} {interface} status.")
    buffer, command_output = run_switch_uart_command_capture_logged(
        uart,
        setup,
        f"show int sta | grep {interface}",
        buffer,
        debug_log,
    )
    oper, admin = parse_interface_status(command_output, interface)
    if oper is None or admin is None:
        print(f"[INFO] {sfp_name} {interface} status not found.")
    else:
        print(f"[INFO] {sfp_name} {interface} Oper={oper} Admin={admin}.")
    return oper, admin, buffer


def require_switch_interface_status(
    uart: Any | None,
    setup: dict[str, Any],
    sfp_name: str,
    interface: str,
    buffer: str,
    allowed_states: set[tuple[str, str]],
    description: str,
    debug_log: pathlib.Path | None = None,
    retries: int = 3,
    retry_delay: float = 2.0,
) -> str:
    expected_text = " or ".join(f"Oper={exp_oper} Admin={exp_admin}" for exp_oper, exp_admin in sorted(allowed_states))
    last_oper: str | None = None
    last_admin: str | None = None
    for attempt in range(retries + 1):
        oper, admin, buffer = read_switch_interface_status(uart, setup, sfp_name, interface, buffer, debug_log)
        last_oper = oper
        last_admin = admin
        if (oper, admin) in allowed_states:
            return buffer
        if attempt < retries:
            print(
                f"[INFO] {sfp_name} {interface} expected {expected_text} {description}; "
                f"got Oper={oper} Admin={admin}. Retrying."
            )
            time.sleep(retry_delay)
    raise RuntimeError(
        f"{sfp_name} {interface} must be {expected_text} {description}. "
        f"Got Oper={last_oper} Admin={last_admin}."
    )


def enabled_data_ip_targets(ping_cfg: dict[str, Any]) -> dict[str, dict[str, str]]:
    enabled: dict[str, dict[str, str]] = {}
    for sfp_name, target_cfg in DATA_IP_TARGETS.items():
        if int(ping_cfg.get(f"{sfp_name}_en", 0)) == 1:
            enabled[sfp_name] = target_cfg
    return enabled


def data_ip_commands(targets: dict[str, dict[str, str]]) -> tuple[str, ...]:
    commands: list[str] = []
    for target_cfg in targets.values():
        if target_cfg["mode"] != "data_ip":
            continue
        interface = target_cfg["interface"]
        target = target_cfg["target"]
        commands.append(f"sudo config interface speed {interface} 10000")
        commands.append(f"sudo config interface ip add {interface} {target}/24")
    return tuple(commands)


def open_switch_uart(setup: dict[str, Any]) -> Any:
    if serial is None:
        raise RuntimeError("pyserial is not installed. Install it or use --skip-data-ip-switch-config.")
    settings = switch_uart_login_settings(setup)
    print(f"[TX] uart switch data-ip {settings['port']} @ {settings['baudrate']}")
    return serial.Serial(
        port=settings["port"],
        baudrate=settings["baudrate"],
        timeout=0.2,
        write_timeout=1,
    )


def configure_data_ip_switch(
    uart: Any,
    setup: dict[str, Any],
    targets: dict[str, dict[str, str]],
    buffer: str,
    debug_log: pathlib.Path | None = None,
) -> str:
    commands = data_ip_commands(targets)
    if not commands:
        return buffer
    print("[INFO] Configuring SFP3/SFP4 switch data IPs before PC ping.")
    for target_cfg in targets.values():
        if target_cfg["mode"] != "data_ip":
            continue
        interface = target_cfg["interface"]
        target = target_cfg["target"]
        print(f"[INFO] Configuring {interface} speed for data IP test.")
        buffer = run_switch_uart_command_logged(
            uart,
            setup,
            f"sudo config interface speed {interface} 10000",
            buffer,
            debug_log,
        )
        print(f"[INFO] Configuring {interface} IP {target}/24.")
        buffer = run_switch_uart_command_logged(
            uart,
            setup,
            f"sudo config interface ip add {interface} {target}/24",
            buffer,
            debug_log,
        )
    print("[INFO] Switch data IP commands completed.")
    return buffer


def ping_command(target: str, count: int, timeout_seconds: int) -> list[str]:
    if sys.platform.startswith("win"):
        return ["ping", "-n", str(count), "-w", str(timeout_seconds * 1000), target]
    return ["ping", "-c", str(count), "-W", str(timeout_seconds), target]


def ping_success(output: str) -> bool:
    return bool(WINDOWS_PING_OK_RE.search(output) or POSIX_PING_OK_RE.search(output))


def run_pc_ping_for_target(sfp_name: str, target: str, ping_count: int, ping_timeout: int) -> tuple[str, str]:
    command = ping_command(target, ping_count, ping_timeout)
    print(f"[TX] {' '.join(command)}")
    completed = subprocess.run(
        command,
        text=True,
        capture_output=True,
        timeout=(ping_count * ping_timeout) + 10,
    )
    ping_output = (completed.stdout or "") + (completed.stderr or "")
    passed = completed.returncode == 0 and ping_success(ping_output)
    status = "pass" if passed else "failed"
    if passed:
        print(f"[INFO] Data IP ping {sfp_name} {target} Passed.")
    else:
        print(f"[INFO] Data IP ping {sfp_name} {target} Failed.")
    return status, f"\n[TX] {' '.join(command)}\n{ping_output}\n"


def run_toggle_ping_target(
    uart: Any | None,
    setup: dict[str, Any],
    sfp_name: str,
    target_cfg: dict[str, str],
    buffer: str,
    ping_count: int,
    ping_timeout: int,
    debug_log: pathlib.Path | None = None,
) -> tuple[str, str, str]:
    interface = target_cfg["interface"]
    isolation_interface = target_cfg["isolation_interface"]
    if uart is None:
        print(f"[INFO] Skipping switch toggle for {sfp_name}; switch UART config is disabled.")
        status, ping_log = run_pc_ping_for_target(sfp_name, target_cfg["target"], ping_count, ping_timeout)
        return status, ping_log, buffer

    buffer = require_switch_interface_status(
        uart,
        setup,
        sfp_name,
        interface,
        buffer,
        allowed_states={("up", "up")},
        description="for active SFP before isolation ping",
        debug_log=debug_log,
    )
    print(f"[INFO] Shutting down {isolation_interface} for {interface} isolation test.")
    buffer = run_switch_uart_command_logged(
        uart,
        setup,
        f"sudo config interface shutdown {isolation_interface}",
        buffer,
        debug_log,
    )
    buffer = require_switch_interface_status(
        uart,
        setup,
        sfp_name,
        isolation_interface,
        buffer,
        allowed_states={("down", "up"), ("down", "down")},
        description="for isolated port before ping",
        debug_log=debug_log,
    )
    print("[INFO] Waiting 8 seconds after isolation status check.")
    time.sleep(8)
    try:
        status, ping_log = run_pc_ping_for_target(sfp_name, target_cfg["target"], ping_count, ping_timeout)
    finally:
        print(f"[INFO] Starting up {isolation_interface} after {interface} isolation test.")
        buffer = run_switch_uart_command_logged(
            uart,
            setup,
            f"sudo config interface startup {isolation_interface}",
            buffer,
            debug_log,
        )
    return status, ping_log, buffer


def run_data_ip_tests(
    setup: dict[str, Any],
    ping_cfg: dict[str, Any],
    skip_switch_config: bool,
    skip_ping: bool,
    ping_count: int,
    ping_timeout: int,
    run_dir: pathlib.Path | None = None,
) -> tuple[dict[str, Any], str]:
    actual: dict[str, Any] = {}
    logs: list[str] = ["\n=== Data IP Test =======================================================\n"]
    debug_log = run_dir / "switch_uart_data_ip.log" if run_dir is not None else None
    if debug_log is not None:
        append_switch_debug_log(debug_log, "=== Switch UART Data IP Debug Log ===\n")
    targets = enabled_data_ip_targets(ping_cfg)
    if not targets:
        print("[INFO] Data IP test skipped; no supported SFP ping tests enabled in test_setup.yaml.")
        return actual, "".join(logs)

    uart = None
    buffer = ""
    try:
        if not skip_switch_config:
            try:
                uart = open_switch_uart(setup)
                settings = switch_uart_login_settings(setup)
                time.sleep(min(settings["open_timeout"], 1.0))
                buffer = ensure_switch_uart_shell(uart, setup, buffer)
                append_switch_debug_log(debug_log, buffer)
                print("\n[INFO] Switch login Passed.")
                buffer = configure_data_ip_switch(uart, setup, targets, buffer, debug_log)
            except OSError as exc:
                settings = switch_uart_login_settings(setup)
                raise RuntimeError(f"Switch UART data IP config failed on {settings['port']}: {exc}") from exc

        if not skip_ping:
            ordered_targets = [
                (sfp_name, target_cfg)
                for mode in ("data_ip", "toggle")
                for sfp_name, target_cfg in targets.items()
                if target_cfg["mode"] == mode
            ]
            for sfp_name, target_cfg in ordered_targets:
                if target_cfg["mode"] == "data_ip":
                    status, ping_log = run_pc_ping_for_target(sfp_name, target_cfg["target"], ping_count, ping_timeout)
                else:
                    status, ping_log, buffer = run_toggle_ping_target(
                        uart,
                        setup,
                        sfp_name,
                        target_cfg,
                        buffer,
                        ping_count,
                        ping_timeout,
                        debug_log,
                    )
                logs.append(ping_log)
                actual[f"tests.eth.ping.{sfp_name}"] = status
    finally:
        if uart is not None:
            uart.close()

    return actual, "".join(logs)


def output_dir(log_root: pathlib.Path, dig_sn: str, when: dt.datetime) -> pathlib.Path:
    base_folder = when.strftime("%Y%m%d_%H%M%S")
    serial_dir = log_root / clean_folder_name(dig_sn.upper())
    candidate = serial_dir / base_folder
    index = 1
    while candidate.exists():
        candidate = serial_dir / f"{base_folder}_{index:02d}"
        index += 1
    return candidate


def report_text(results: list[CheckResult], actual: dict[str, Any], dig_sn: str = "XXXXXXX") -> tuple[str, int]:
    passed = sum(1 for result in results if result.passed)
    not_found = [result for result in results if result.actual is None]
    mismatched = [result for result in results if result.actual is not None and not result.passed]
    failed = len(not_found) + len(mismatched)
    unit_status = "PASSED" if failed == 0 and results else "FAILED"
    lines = [
        f"Report DIG_BOARD SN: {dig_sn} - {unit_status}",
        "=========================================================================",
    ]
    if actual.get("sonic.sv") is not None:
        lines.append(f"Sonic_SV: {actual['sonic.sv']}")
    if actual.get("sonic.os") is not None:
        lines.append(f"Sonic_OS: {actual['sonic.os']}")
    firmware_items = sorted(
        (name.removeprefix("sonic.firmware."), value)
        for name, value in actual.items()
        if name.startswith("sonic.firmware.")
    )
    for component, version in firmware_items:
        lines.append(f"{component}: {version}")
    ordered_results = ordered_report_results(results)
    for prefix, title in SECTION_TITLES:
        section_results = [result for result in ordered_results if result.name.startswith(prefix)]
        if not section_results:
            continue
        section_passed = sum(1 for result in section_results if result.passed)
        section_failed = sum(1 for result in section_results if result.actual is not None and not result.passed)
        section_not_found = sum(1 for result in section_results if result.actual is None)
        lines.append("")
        lines.append(f"--- Testing {title} ---")
        lines.append(
            f"[INFO] Section summary: {section_passed} passed, "
            f"{section_failed} failed, {section_not_found} not found."
        )
        for result in section_results:
            lines.append(report_result_line(result))
    lines.append("")
    lines.append(
        f"Summary: {passed} passed, {len(mismatched)} failed, "
        f"{len(not_found)} not found, {len(results)} checked."
    )

    if not_found:
        lines.append("")
        lines.append("Data not found in output according to test_setup.yaml:")
        for result in not_found:
            lines.append(f"  - {result.name}: expected={result.expected!r}")

    if mismatched:
        lines.append("")
        lines.append("Data found but not matching test_setup.yaml:")
        for result in mismatched:
            lines.append(f"  - {result.name}: expected={result.expected!r} actual={result.actual!r}")

    if not actual:
        lines.append("")
        lines.append("No parsable values were found in the command output.")

    return "\n".join(lines) + "\n", 0 if failed == 0 and results else 1


def csv_units(name: str) -> str:
    if name.startswith("tests.temp."):
        return "C"
    if name.startswith("tests.power."):
        return "W"
    if name == "tests.fpga.uplink.snr_min":
        return "dBm"
    if name.startswith("tests.fpga.mdm") and (name.endswith("_dig_loopback") or name.endswith("_full_loopback")):
        return "dBm"
    return ""


def csv_expected_bounds(expected: Any) -> tuple[str, str]:
    if isinstance(expected, str):
        match = re.fullmatch(r"\s*([^.\s]+(?:\.\d+)?)\.\.([^.\s]+(?:\.\d+)?)\s*", expected)
        if match:
            return match.group(1), match.group(2)
    return "", ""


def csv_report_text(report: str, results: list[CheckResult]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(["test_name", "value", "min", "max", "units", "status"])
    preamble, _, _rest = report.partition("\n\n")
    header_line_re = re.compile(
        r"^(?!Report DIG_BOARD SN:)(?!={5,})([A-Za-z0-9_.-]+):\s*(.+)$",
        re.MULTILINE,
    )
    for test_name, value in header_line_re.findall(preamble):
        writer.writerow([test_name, value.strip(), "", "", "", "PASS"])
    for result in ordered_report_results(results):
        min_value, max_value = csv_expected_bounds(result.expected)
        status = "PASS" if result.passed else "FAIL"
        writer.writerow(
            [
                report_label(result.name),
                report_value(result.actual, result.name) if result.actual is not None else "",
                min_value,
                max_value,
                csv_units(result.name),
                status,
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
    (run_dir / f"report_{status}.txt").write_text(report, encoding="utf-8")
    (run_dir / f"report_{status}.csv").write_text(csv_report, encoding="utf-8", newline="")
    summary = (
        f"dig_sn: {args.dig_sn}\n"
        f"host: {args.host}\n"
        f"user: {args.user}\n"
        f"remote_dir: {args.remote_dir}\n"
        f"script: {args.script}\n"
        f"status: {status}\n"
        f"exit_code: {exit_code}\n"
    )
    (run_dir / "summary.txt").write_text(summary, encoding="utf-8")


def save_error_artifacts(
    run_dir: pathlib.Path,
    output: str,
    args: argparse.Namespace,
    exc: BaseException,
) -> None:
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


def legacy_save_output(repo_root: pathlib.Path, output: str) -> pathlib.Path:
    logs_dir = repo_root / "logs"
    logs_dir.mkdir(exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    path = logs_dir / f"test_output_{stamp}.txt"
    path.write_text(output, encoding="utf-8")
    return path


def print_report(results: list[CheckResult], actual: dict[str, Any], dig_sn: str = "XXXXXXX") -> int:
    report, exit_code = report_text(results, actual, dig_sn)
    print("\n" + report, end="")
    return exit_code


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run LSBB system tests over SSH and compare the output with test_setup.yaml.")
    parser.add_argument("--dig_sn", required=True, help="DUT digital serial number used for the test log folder.")
    parser.add_argument("--config", default="test_setup.yaml", help="YAML file with expected values.")
    parser.add_argument("--setup-config", default="script_setup.yaml", help="YAML file with SSH connection information.")
    parser.add_argument("--mode", choices=("full", "run-sh", "skip_eth"), default="full", help="full runs/checks lsbb_cil_test too; run-sh checks only run.sh output; skip_eth runs full test but skips ping.")
    parser.add_argument("--input-log", help="Parse an existing output file instead of running SSH.")
    parser.add_argument("--host", help="SSH host/IP address override. Defaults to script_setup.yaml dut.final_ip.")
    parser.add_argument("--user", help="SSH username override. Defaults to script_setup.yaml dut.login.")
    parser.add_argument("--password", help="SSH password override. Defaults to script_setup.yaml dut.password.")
    parser.add_argument("--port", type=int, help="SSH port.")
    parser.add_argument("--identity-file", help="SSH private key path.")
    parser.add_argument("--ssh-option", action="append", help="Extra ssh -o option. Can be repeated.")
    parser.add_argument("--force-openssh", action="store_true", help="Use system ssh even when paramiko is available.")
    parser.add_argument("--connect-timeout", type=int, default=10, help="SSH connection timeout in seconds.")
    parser.add_argument("--timeout", type=int, default=900, help="Remote command timeout in seconds.")
    parser.add_argument("--remote-dir", default=argparse.SUPPRESS, help="Remote directory containing the shell script.")
    parser.add_argument("--script", default="run.sh", help="Remote .sh script name.")
    parser.add_argument("--remote-command", help="Full remote command override.")
    parser.add_argument("--interactive", action=argparse.BooleanOptionalAction, default=True, help="Use an interactive SSH channel for run.sh prompts.")
    parser.add_argument("--system-init-response", default=None, help="Optional response sent when run.sh waits at the System Init prompt.")
    parser.add_argument("--skip-login-check", action="store_true", help="Skip the NXP SSH login precheck.")
    parser.add_argument("--skip-switch-login-check", action="store_true", help="Skip the switch UART login precheck.")
    parser.add_argument("--skip-data-ip", action="store_true", help="Skip the data IP switch configuration and PC ping tests.")
    parser.add_argument("--skip-data-ip-switch-config", action="store_true", help="Skip only the data IP switch UART configuration commands.")
    parser.add_argument("--skip-data-ip-ping", action="store_true", help="Skip only the data IP PC ping checks.")
    parser.add_argument("--ping-count", type=int, default=1, help="Number of ICMP echo requests per data IP target.")
    parser.add_argument("--ping-timeout", type=int, default=3, help="Per-packet data IP ping timeout in seconds.")
    parser.add_argument("--no-save", action="store_true", help="Do not save test artifacts.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    repo_root = pathlib.Path(__file__).resolve().parent
    config_path = (repo_root / args.config).resolve()
    setup_path = (repo_root / args.setup_config).resolve()
    run_dir: pathlib.Path | None = None
    output = ""

    try:
        expected = load_yaml(config_path)
        setup = load_yaml(setup_path)
        apply_connection_defaults(args, setup)

        if not args.no_save:
            log_path = get_nested(expected, "logs.path")
            log_root = pathlib.Path(str(log_path)) if log_path else repo_root / "logs"
            run_dir = output_dir(log_root, args.dig_sn, dt.datetime.now())
            run_dir.mkdir(parents=True, exist_ok=False)
            print(f"[INFO] Test log folder: {run_dir}")

        if args.input_log:
            output_path = (repo_root / args.input_log).resolve()
            output = output_path.read_text(encoding="utf-8", errors="replace")
            print(f"[INFO] Loaded output from {output_path}")
        else:
            switch_uart_output = ""
            if not args.host:
                raise RuntimeError("Missing dut.final_ip in script_setup.yaml. Use --host to override.")
            if not args.user:
                raise RuntimeError("Missing dut.login in script_setup.yaml. Use --user to override.")
            if not args.skip_login_check:
                check_nxp_login(args)
            if not args.skip_switch_login_check:
                switch_uart_output = check_switch_uart_login(setup)
            output = run_ssh(args)
            if switch_uart_output:
                output += "\n" + switch_uart_output
            if not getattr(args, "output_streamed", False):
                print(output, end="" if output.endswith("\n") else "\n")

        actual = parse_output(output)
        if not args.input_log and args.mode == "full" and not args.skip_data_ip:
            ping_cfg = get_nested(expected, "tests.eth.ping", {})
            data_ip_actual, data_ip_output = run_data_ip_tests(
                setup=setup,
                ping_cfg=ping_cfg if isinstance(ping_cfg, dict) else {},
                skip_switch_config=args.skip_data_ip_switch_config,
                skip_ping=args.skip_data_ip_ping,
                ping_count=args.ping_count,
                ping_timeout=args.ping_timeout,
                run_dir=run_dir,
            )
            actual.update(data_ip_actual)
            output += data_ip_output
        results = compare(
            expected,
            actual,
            include_full_tests=args.mode in {"full", "skip_eth"},
            include_ping_tests=args.mode == "full" and not args.skip_data_ip,
            include_loopback_tests=args.mode in {"full", "skip_eth"},
        )
        report, exit_code = report_text(results, actual, args.dig_sn)
        csv_report = csv_report_text(report, results)
        print("\n" + report, end="")
        if exit_code == 0:
            print(f"[PASS] Unit {args.dig_sn} passed all tests.")
        else:
            print(f"[FAIL] Unit {args.dig_sn} failed tests.")

        if run_dir is not None:
            save_run_artifacts(run_dir, output, report, csv_report, args, exit_code)
            print(f"[INFO] Saved test artifacts to {run_dir}")

        return exit_code
    except (OSError, RuntimeError, subprocess.SubprocessError, TimeoutError) as exc:
        if run_dir is not None:
            save_error_artifacts(run_dir, output, args, exc)
            print(f"[INFO] Saved error artifacts to {run_dir}")
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

import argparse
import configparser
import csv
import datetime as dt
import io
import os
import pathlib
import re
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Any

from rf_spectrum import KeysightN9010B, ScpiError, load_spectrum_config

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


ANSI_RE = re.compile(r"\x1b\[[?0-9;]*[ -/]*[@-~]")
CPR_RE = re.compile(r"\x1b\[(?:6n|\d+;\d+R)|(?<!\S)\[\d+;\d+R")
BAD_PATH_CHARS_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]+')
EXIT_MARKER_RE = re.compile(r"__TEST_EXIT__(\d+)")
RUN_SH_DONE_RE = re.compile(r"Modem\s+Link\s+-\s+All\s+(?:Disabled|Enabled).*?Data\s+Path\s+-\s+Enabled", re.IGNORECASE | re.DOTALL)
FULL_TEST_DONE_RE = re.compile(r"INA_MAIN\s*:\s*[-0-9.]+\s+[-0-9.]+\s+[-0-9.]+.*?>>>", re.IGNORECASE | re.DOTALL)
FULL_LOOPBACK_PASS_RE = re.compile(
    r"Full\s+Loopback\s+Test\s*"
    r"\(\s*SNRs\s*=\s*([^)]+?)\s*\)\s*-\s*Pass",
    re.IGNORECASE,
)
SWITCH_CONSOLE_PROMPT_RE = re.compile(r"Console(?:\([^)]+\))?#\s*$", re.MULTILINE)
NXP_PYTHON_PROMPT_RE = re.compile(r">>>\s*$")
SQL_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

INTERNAL_PRBS_SWITCH_INTERFACES = ("0/1", "0/2", "0/3", "0/4")
EXTERNAL_PRBS_SWITCH_INTERFACES = ("0/10", "0/11", "0/12", "0/13")
INTERNAL_PRBS_NXP_COMMANDS = (
    'sx4000_ctrl.sds_prbs_en(sx_id="SX1", sds_type="ETH", near_end_lb=False, far_end_lb=False, prbs_type=31)',
    'sx4000_ctrl.sds_prbs_en(sx_id="SX2", sds_type="ETH", near_end_lb=False, far_end_lb=False, prbs_type=31)',
)
SFP_PORTS = ("eth10", "eth11", "eth12", "eth13")
SFP_FIELD_MAP = {
    "eth10": "mng_sfp1",
    "eth11": "mng_sfp2",
    "eth12": "data_sfp1",
    "eth13": "data_sfp2",
}
SFP_PAIRING_TABLE = "LSBB_pairing"
DEFAULT_RF_STATE_PATH = r"C:\pr_1u\State_channel_power.state"
RF_CHANNEL_POWER_QUERY = ":FETCh:CHPower?"
RF_PSD_QUERY = ":TRACe:DATA? TRACE1"
RF_SCREENSHOT_FORMAT_COMMAND = ""
RF_SCREENSHOT_QUERY = ":HCOPy:SDUMp:DATA?"
RF_SCREENSHOT_FILENAME = "rf_screenshot.png"

@dataclass
class CheckResult:
    name: str
    expected: Any
    actual: Any
    passed: bool


@dataclass(frozen=True)
class SfpRecord:
    pn: str
    sn: str


@dataclass(frozen=True)
class SqlConfig:
    server: str
    database: str
    username: str
    password: str
    schema: str
    driver_candidates: tuple[str, ...]
    connection_string: str
    timeout_seconds: int

    @property
    def pairing_qualified_name(self) -> str:
        return f"[{self.schema}].[{SFP_PAIRING_TABLE}]"


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
    ("tests.eth.eth", "ETH Transceivers"),
    ("tests.eth.prbs.", "Internal PRBS"),
    ("tests.eth.ext_prbs.", "External PRBS"),
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
    "tests.fpga.mdm0_dig_loopback": 1,
    "tests.fpga.mdm1_dig_loopback": 2,
    "tests.fpga.mdm2_dig_loopback": 3,
    "tests.fpga.mdm3_dig_loopback": 4,
    "tests.fpga.mdm0_full_loopback": 5,
    "tests.fpga.mdm1_full_loopback": 6,
    "tests.fpga.mdm2_full_loopback": 7,
    "tests.fpga.mdm3_full_loopback": 8,
    "tests.eth.prbs.switch_0_1_lock": 0,
    "tests.eth.prbs.switch_0_2_lock": 1,
    "tests.eth.prbs.switch_0_3_lock": 2,
    "tests.eth.prbs.switch_0_4_lock": 3,
    "tests.eth.prbs.sx1_line0_ber": 4,
    "tests.eth.prbs.sx1_line0_errcount": 5,
    "tests.eth.prbs.sx1_line1_ber": 6,
    "tests.eth.prbs.sx1_line1_errcount": 7,
    "tests.eth.prbs.sx2_line0_ber": 8,
    "tests.eth.prbs.sx2_line0_errcount": 9,
    "tests.eth.prbs.sx2_line1_ber": 10,
    "tests.eth.prbs.sx2_line1_errcount": 11,
    "tests.eth.ext_prbs.eth10.status": 0,
    "tests.eth.ext_prbs.eth10.err": 1,
    "tests.eth.ext_prbs.eth10.ber_err": 2,
    "tests.eth.ext_prbs.eth11.status": 3,
    "tests.eth.ext_prbs.eth11.err": 4,
    "tests.eth.ext_prbs.eth11.ber_err": 5,
    "tests.eth.ext_prbs.eth12.status": 6,
    "tests.eth.ext_prbs.eth12.err": 7,
    "tests.eth.ext_prbs.eth12.ber_err": 8,
    "tests.eth.ext_prbs.eth13.status": 9,
    "tests.eth.ext_prbs.eth13.err": 10,
    "tests.eth.ext_prbs.eth13.ber_err": 11,
    "tests.eth.eth10_pn": 0,
    "tests.eth.eth11_pn": 1,
    "tests.eth.eth12_pn": 2,
    "tests.eth.eth13_pn": 3,
}

HEX_REPORT_FIELDS = {
    "version.fpga.gp_ver",
    "version.fpga.dig_ver",
}


def strip_ansi(text: str) -> str:
    return ANSI_RE.sub("", text)


def strip_terminal_cpr(text: str) -> str:
    return CPR_RE.sub("", text)


def clean_terminal_text(text: str) -> str:
    text = strip_ansi(strip_terminal_cpr(text))
    return text.replace("\x08", "")


def clean_folder_name(value: str) -> str:
    cleaned = BAD_PATH_CHARS_RE.sub("_", value.strip())
    cleaned = cleaned.strip(" ._")
    if not cleaned:
        raise RuntimeError("Folder name cannot be empty.")
    return cleaned


def realtime_log_path(args: argparse.Namespace, filename: str) -> pathlib.Path | None:
    run_dir = getattr(args, "run_dir", None)
    if run_dir is None:
        return None
    return pathlib.Path(run_dir) / filename


def append_realtime_log(path: pathlib.Path | None, text: str) -> None:
    if path is None or not text:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", errors="replace") as fh:
        fh.write(text)


def log_nxp(args: argparse.Namespace, text: str) -> None:
    append_realtime_log(realtime_log_path(args, "nxp_realtime.log"), text)


def log_switch(args: argparse.Namespace, text: str) -> None:
    append_realtime_log(realtime_log_path(args, "switch_realtime.log"), text)


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
    if name.startswith("tests.fpga.mdm") and name.endswith("_dig_loopback"):
        return name.removeprefix("tests.fpga.").removesuffix("_dig_loopback").upper() + "_DIG_LOOPBACK"
    if name.startswith("tests.fpga.mdm") and name.endswith("_full_loopback"):
        return name.removeprefix("tests.fpga.").removesuffix("_full_loopback").upper() + "_FULL_LOOPBACK"
    if name == "tests.eth.login":
        return "SWITCH_LOGIN"
    eth_pn = re.fullmatch(r"tests\.eth\.(eth1[0-3])_pn", name)
    if eth_pn:
        return f"{eth_pn.group(1).upper()}_PN"
    eth_sn = re.fullmatch(r"tests\.eth\.(eth1[0-3])_sn", name)
    if eth_sn:
        return f"{eth_sn.group(1).upper()}_SN"
    if name.startswith("tests.eth.prbs.switch_") and name.endswith("_lock"):
        return "PRBS_" + name.removeprefix("tests.eth.prbs.switch_").removesuffix("_lock").upper() + "_LOCK"
    sx_prbs = re.fullmatch(r"tests\.eth\.prbs\.(sx[12])_line([01])_(ber|errcount)", name)
    if sx_prbs:
        return f"PRBS_{sx_prbs.group(1).upper()}_LINE{sx_prbs.group(2)}_{sx_prbs.group(3).upper()}"
    ext_prbs = re.fullmatch(r"tests\.eth\.ext_prbs\.(eth\d+)\.(status|err|ber_err)", name)
    if ext_prbs:
        return f"EXT_PRBS_{ext_prbs.group(1).upper()}_{ext_prbs.group(2).upper()}"
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
    if name and (name.endswith("_ber") or name.endswith(".ber_err")):
        try:
            return f"{float(value):.6e}"
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


def parse_loopback_snr_group(
    data: dict[str, Any],
    text: str,
    pattern: str,
    key_suffix: str,
) -> re.Match[str] | None:
    return parse_loopback_snr_group_after(data, text, pattern, key_suffix, 0)


def parse_loopback_snr_group_after(
    data: dict[str, Any],
    text: str,
    pattern: str,
    key_suffix: str,
    start_at: int,
) -> re.Match[str] | None:
    matches = [match for match in re.finditer(pattern, text, re.IGNORECASE) if match.start() >= start_at]
    if not matches:
        return None

    selected = next(
        (match for match in reversed(matches) if match.group(2).lower() == "pass"),
        matches[-1],
    )
    status = "fail" if selected.group(2).lower().startswith("fail") else "pass"
    snr_values = [
        float(value)
        for value in re.findall(r"-?\d+(?:\.\d+)?", selected.group(1))
    ]
    if len(snr_values) < 4:
        return None

    for modem, snr in enumerate(snr_values[:4]):
        data[f"tests.fpga.mdm{modem}_{key_suffix}"] = snr
        data[f"tests.fpga.mdm{modem}_{key_suffix}_status"] = status
    return selected


def parse_internal_prbs_output(text: str) -> dict[str, Any]:
    data: dict[str, Any] = {}
    clean_text = strip_ansi(text)
    for interface, status, lock_state in re.findall(
        r"^\s*(0/[1-4])\s*\|\s*\d+\s*\|\s*PRBS_31\s*\|\s*(Passed|Failed)\s*\|\s*(Locked|UnLocked)\s*\|",
        clean_text,
        re.IGNORECASE | re.MULTILINE,
    ):
        key = interface.replace("/", "_")
        data[f"tests.eth.prbs.switch_{key}_status"] = status.lower()
        data[f"tests.eth.prbs.switch_{key}_lock"] = lock_state.lower()

    sx_read_re = re.compile(
        r'sds_read_prbs_err\(sx_id="(SX[12])",\s*sds_type="ETH"\).*?'
        r"(?=(?:\r?\n)>>>|\Z)",
        re.IGNORECASE | re.DOTALL,
    )
    for match in sx_read_re.finditer(clean_text):
        sx_id = match.group(1)
        block = match.group(0)
        for lane, errcount, ber in re.findall(
            r"Lane\s+([01]),\s*ErrCnt\s*=\s*(\d+)\s*,\s*BER:\s*([0-9.eE+-]+)",
            block,
            re.IGNORECASE,
        ):
            sx_key = sx_id.lower()
            data[f"tests.eth.prbs.{sx_key}_line{lane}_errcount"] = int(errcount)
            data[f"tests.eth.prbs.{sx_key}_line{lane}_ber"] = float(ber)
    return data


def parse_external_prbs_output(text: str) -> dict[str, Any]:
    data: dict[str, Any] = {}
    clean_text = strip_ansi(text)
    show_matches = list(re.finditer(r"dbg link prbs show interface ethernet 0/10-13", clean_text, re.IGNORECASE))
    if show_matches:
        clean_text = clean_text[show_matches[-1].start():]
    for interface, _polynomial, _status, lock_state, errors, ber in re.findall(
        r"^\s*(0/1[0-3])\s*\|\s*\d+\s*\|\s*(PRBS_7|PRBS_31)\s*\|\s*(Passed|Failed)\s*\|\s*(Locked|UnLocked)\s*\|\s*(0x[0-9a-fA-F]+|\d+)\s*\|\s*([0-9.eE+-]+)\s*\|?",
        clean_text,
        re.IGNORECASE | re.MULTILINE,
    ):
        eth_name = "eth" + interface.split("/", 1)[1]
        data[f"tests.eth.ext_prbs.{eth_name}.status"] = lock_state
        data[f"tests.eth.ext_prbs.{eth_name}.err"] = int(errors, 16) if errors.lower().startswith("0x") else int(errors)
        data[f"tests.eth.ext_prbs.{eth_name}.ber_err"] = float(ber)
    return data


def parse_transceiver_eeprom_output(text: str) -> dict[str, Any]:
    data: dict[str, Any] = {}
    clean_text = strip_ansi(text)
    block_re = re.compile(
        r"^Ethernet(1[0-3]):\s*SFP EEPROM detected(?P<body>.*?)(?=^Ethernet\d+:|\Z)",
        re.IGNORECASE | re.MULTILINE | re.DOTALL,
    )
    for match in block_re.finditer(clean_text):
        eth_name = f"eth{match.group(1)}"
        body = match.group("body")
        pn_match = re.search(r"^\s*Vendor PN:\s*(\S+)\s*$", body, re.IGNORECASE | re.MULTILINE)
        sn_match = re.search(r"^\s*Vendor SN:\s*(\S+)\s*$", body, re.IGNORECASE | re.MULTILINE)
        if pn_match:
            data[f"tests.eth.{eth_name}_pn"] = pn_match.group(1)
        if sn_match:
            data[f"tests.eth.{eth_name}_sn"] = sn_match.group(1)

    info_re = re.compile(
        r"^\s*\[INFO\]\s+ETH(1[0-3])\s+transceiver\s+PN=([^,\s]+),\s+SN=([^. \r\n]+)\.",
        re.IGNORECASE | re.MULTILINE,
    )
    for eth_number, pn, sn in info_re.findall(clean_text):
        eth_name = f"eth{eth_number.lower()}"
        data[f"tests.eth.{eth_name}_pn"] = pn.strip()
        data[f"tests.eth.{eth_name}_sn"] = sn.strip()
    return data


def parse_latest_sfp_records(text: str) -> dict[str, SfpRecord]:
    clean_text = strip_ansi(text)
    info_re = re.compile(
        r"^\s*\[INFO\]\s+ETH(1[0-3])\s+transceiver\s+PN=([^,\s]+),\s+SN=([^. \r\n]+)\.",
        re.IGNORECASE | re.MULTILINE,
    )
    latest: dict[str, SfpRecord] = {}
    current: dict[str, SfpRecord] = {}
    for match in info_re.finditer(clean_text):
        eth_name = f"eth{match.group(1).lower()}"
        if eth_name == "eth10":
            current = {}
        current[eth_name] = SfpRecord(pn=match.group(2).strip(), sn=match.group(3).strip())
        if all(port in current for port in SFP_PORTS):
            latest = dict(current)
    if latest:
        return latest

    parsed = parse_transceiver_eeprom_output(clean_text)
    for port in SFP_PORTS:
        pn = parsed.get(f"tests.eth.{port}_pn")
        sn = parsed.get(f"tests.eth.{port}_sn")
        if pn is not None and sn is not None:
            latest[port] = SfpRecord(pn=str(pn).strip(), sn=str(sn).strip())
    return latest


def validate_sfp_records(records: dict[str, SfpRecord], expected: dict[str, Any]) -> None:
    missing = [port.upper() for port in SFP_PORTS if port not in records]
    if missing:
        raise RuntimeError("Missing SFP data for: " + ", ".join(missing))

    errors: list[str] = []
    for port in SFP_PORTS:
        record = records[port]
        expected_pn = get_nested(expected, f"tests.eth.{port}_pn")
        if expected_pn in (None, ""):
            errors.append(f"{port.upper()}: missing expected PN in test_setup.yaml")
            continue
        if str(record.pn).strip().upper() != str(expected_pn).strip().upper():
            errors.append(f"{port.upper()}: expected PN {expected_pn}, read PN {record.pn}")
        if not record.sn:
            errors.append(f"{port.upper()}: missing SN")
    if errors:
        raise RuntimeError("SFP validation failed; not saving to SQL DB:\n" + "\n".join(errors))


def require_internal_prbs_switch_locks(show_output: str) -> None:
    data = parse_internal_prbs_output(show_output)
    missing_or_unlocked = []
    for interface in INTERNAL_PRBS_SWITCH_INTERFACES:
        key = interface.replace("/", "_")
        lock_state = data.get(f"tests.eth.prbs.switch_{key}_lock")
        if lock_state != "locked":
            missing_or_unlocked.append(f"{interface}: {lock_state or 'not found'}")
    if missing_or_unlocked:
        raise RuntimeError("Switch PRBS lock check failed: " + ", ".join(missing_or_unlocked))


def parse_output(output: str) -> dict[str, Any]:
    text = strip_ansi(output)
    data: dict[str, Any] = {}
    data.update(parse_internal_prbs_output(text))
    data.update(parse_external_prbs_output(text))
    data.update(parse_transceiver_eeprom_output(text))

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

    dig_loopback_match = parse_loopback_snr_group(
        data,
        text,
        r"MDMs\s*<->\s*FPGA\s+Dig\s+Loopback\s+Check\s*"
        r"\(\s*SNRs\s*=\s*([^)]+?)\s*\)\s*-\s*(Pass|Fail(?:ed)?)",
        "dig_loopback",
    )
    parse_loopback_snr_group_after(
        data,
        text,
        r"Full\s+Loopback\s+Test\s*"
        r"\(\s*SNRs\s*=\s*([^)]+?)\s*\)\s*-\s*(Pass|Fail(?:ed)?)",
        "full_loopback",
        dig_loopback_match.end() if dig_loopback_match else 0,
    )

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
    include_prbs_tests: bool = True,
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

        fpga_cfg = tests_cfg.get("fpga", {})
        uplink_cfg = fpga_cfg.get("uplink", {}) if isinstance(fpga_cfg, dict) else {}
        if isinstance(uplink_cfg, dict) and "snr_min" in uplink_cfg:
            actual_snr = actual.get("tests.fpga.uplink.snr_min")
            passed = actual_snr is not None and float(actual_snr) >= float(uplink_cfg["snr_min"])
            results.append(CheckResult("tests.fpga.uplink.snr_min", f">= {uplink_cfg['snr_min']}", actual_snr, passed))
        if isinstance(fpga_cfg, dict):
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
        if isinstance(eth_cfg, dict):
            for interface in ("eth10", "eth11", "eth12", "eth13"):
                pn_key = f"{interface}_pn"
                if pn_key in eth_cfg:
                    add_check(results, f"tests.eth.{pn_key}", eth_cfg[pn_key], actual.get(f"tests.eth.{pn_key}"))
        prbs_cfg = eth_cfg.get("prbs", {}) if isinstance(eth_cfg, dict) else {}
        if include_prbs_tests and isinstance(prbs_cfg, dict):
            for interface in INTERNAL_PRBS_SWITCH_INTERFACES:
                key = interface.replace("/", "_")
                add_check(results, f"tests.eth.prbs.switch_{key}_lock", "locked", actual.get(f"tests.eth.prbs.switch_{key}_lock"))
            for sx_name in ("sx1", "sx2"):
                sx_limits = prbs_cfg.get(sx_name)
                if isinstance(sx_limits, dict):
                    for lane in (0, 1):
                        for metric in ("ber", "errcount"):
                            min_key = f"line{lane}_{metric}_min"
                            max_key = f"line{lane}_{metric}_max"
                            if min_key in sx_limits or max_key in sx_limits:
                                limits = {
                                    "min": sx_limits.get(min_key),
                                    "max": sx_limits.get(max_key),
                                }
                                name = f"tests.eth.prbs.{sx_name}_line{lane}_{metric}"
                                add_range_check(results, name, limits, actual.get(name))

        ext_prbs_cfg = eth_cfg.get("ext_prbs", {}) if isinstance(eth_cfg, dict) else {}
        if include_prbs_tests and isinstance(ext_prbs_cfg, dict):
            for eth_name, eth_expected in ext_prbs_cfg.items():
                if not isinstance(eth_expected, dict):
                    continue
                if "status" in eth_expected:
                    add_check(
                        results,
                        f"tests.eth.ext_prbs.{eth_name}.status",
                        eth_expected["status"],
                        actual.get(f"tests.eth.ext_prbs.{eth_name}.status"),
                    )
                if "err" in eth_expected:
                    add_check(
                        results,
                        f"tests.eth.ext_prbs.{eth_name}.err",
                        eth_expected["err"],
                        actual.get(f"tests.eth.ext_prbs.{eth_name}.err"),
                    )
                if "ber_err_min" in eth_expected or "ber_err_max" in eth_expected:
                    limits = {
                        "min": eth_expected.get("ber_err_min"),
                        "max": eth_expected.get("ber_err_max"),
                    }
                    add_range_check(results, f"tests.eth.ext_prbs.{eth_name}.ber_err", limits, actual.get(f"tests.eth.ext_prbs.{eth_name}.ber_err"))

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


def nxp_login_settings(setup: dict[str, Any]) -> dict[str, Any]:
    host = get_nested(setup, "dut.final_ip")
    username = get_nested(setup, "dut.login")
    password = get_nested(setup, "dut.password")
    missing = [
        name
        for name, value in (
            ("dut.final_ip", host),
            ("dut.login", username),
            ("dut.password", password),
        )
        if value in (None, "")
    ]
    if missing:
        raise RuntimeError(f"Missing NXP login settings in script_setup.yaml: {', '.join(missing)}")
    return {
        "host": str(host),
        "username": str(username),
        "password": str(password),
        "remote_dir": default_remote_dir(setup),
    }


def apply_connection_defaults(args: argparse.Namespace, setup: dict[str, Any]) -> None:
    nxp_settings = nxp_login_settings(setup)
    args.host = args.host or nxp_settings["host"]
    args.user = args.user or nxp_settings["username"]
    args.password = args.password or nxp_settings["password"]

    if not hasattr(args, "remote_dir"):
        args.remote_dir = nxp_settings["remote_dir"]


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
        if rf_analyzer is not None:
            rf_analyzer.close()
        client.close()

    combined = output + error_output
    log_nxp(args, combined)
    if exit_status != 0:
        raise RuntimeError(f"SSH command failed with exit code {exit_status}.\n{combined.strip()}")
    return combined


def recv_ssh_channel_until_prompt(
    channel: Any,
    args: argparse.Namespace,
    output_parts: list[str],
    timeout: float,
) -> str:
    started = time.monotonic()
    segment_parts: list[str] = []
    while True:
        if time.monotonic() - started > timeout:
            raise TimeoutError(f"Timed out after {timeout:g} seconds waiting for NXP Python prompt.")
        if channel.recv_ready():
            chunk = channel.recv(4096).decode(errors="replace")
            output_parts.append(chunk)
            segment_parts.append(chunk)
            log_nxp(args, chunk)
            print(strip_terminal_cpr(chunk), end="", flush=True)
            if NXP_PYTHON_PROMPT_RE.search(clean_terminal_text("".join(segment_parts))):
                return strip_terminal_cpr("".join(segment_parts))
        else:
            if channel.closed:
                raise RuntimeError("SSH channel closed while waiting for NXP Python prompt.")
            time.sleep(0.1)


def drain_ssh_channel(channel: Any, args: argparse.Namespace, output_parts: list[str]) -> None:
    while channel.recv_ready():
        chunk = channel.recv(4096).decode(errors="replace")
        output_parts.append(chunk)
        log_nxp(args, chunk)
        print(strip_terminal_cpr(chunk), end="", flush=True)
        time.sleep(0.05)


def send_nxp_python_command(
    channel: Any,
    args: argparse.Namespace,
    output_parts: list[str],
    command: str,
    timeout: float = 60,
) -> str:
    print(f"[TX] nxp python {command}")
    log_nxp(args, f"\n[TX] nxp python {command}\n")
    drain_ssh_channel(channel, args, output_parts)
    channel.send(command + "\r")
    output = recv_ssh_channel_until_prompt(channel, args, output_parts, timeout)
    time.sleep(1)
    return output


def send_sx_prbs_read_command(
    channel: Any,
    args: argparse.Namespace,
    output_parts: list[str],
    sx_id: str,
) -> str:
    command = f'sx4000_ctrl.sds_read_prbs_err(sx_id="{sx_id}", sds_type="ETH")'
    output = send_nxp_python_command(channel, args, output_parts, command)
    parsed = parse_internal_prbs_output(output)
    sx_key = sx_id.lower()
    missing = [
        f"line{lane}_{metric}"
        for lane in (0, 1)
        for metric in ("errcount", "ber")
        if f"tests.eth.prbs.{sx_key}_line{lane}_{metric}" not in parsed
    ]
    if missing:
        raise RuntimeError(f"{sx_id} PRBS read output missing: {', '.join(missing)}")
    return output


def nxp_shell_prompt_pattern(setup: dict[str, Any]) -> str:
    prompt = str(get_nested(setup, "dut.prompt", "")).strip()
    if prompt:
        return re.escape(prompt) + r".*[#>$]\s*$"
    return r"(?m)^[^\r\n]*[#>$]\s*$"


def recv_ssh_channel_until_pattern(
    args: argparse.Namespace,
    channel: Any,
    output_parts: list[str],
    pattern: str,
    timeout: float,
    description: str,
) -> str:
    started = time.monotonic()
    segment_parts: list[str] = []
    while True:
        if time.monotonic() - started > timeout:
            raise TimeoutError(f"Timed out after {timeout:g} seconds waiting for {description}.")
        if channel.recv_ready():
            chunk = channel.recv(4096).decode(errors="replace")
            output_parts.append(chunk)
            segment_parts.append(chunk)
            log_nxp(args, chunk)
            print(strip_terminal_cpr(chunk), end="", flush=True)
            if re.search(pattern, clean_terminal_text("".join(segment_parts)), re.IGNORECASE | re.MULTILINE):
                return strip_terminal_cpr("".join(segment_parts))
        else:
            if channel.closed:
                raise RuntimeError(f"SSH channel closed while waiting for {description}.")
            time.sleep(0.1)


def send_nxp_shell_command(
    args: argparse.Namespace,
    channel: Any,
    setup: dict[str, Any],
    output_parts: list[str],
    command: str,
    timeout: float = 30,
) -> str:
    print(f"[TX] nxp shell {command}")
    log_nxp(args, f"\n[TX] nxp shell {command}\n")
    drain_ssh_channel(channel, args, output_parts)
    channel.send(command + "\r")
    output = recv_ssh_channel_until_pattern(
        args,
        channel,
        output_parts,
        nxp_shell_prompt_pattern(setup),
        timeout,
        "NXP shell prompt",
    )
    time.sleep(1)
    return output


def exit_python_and_shutdown_sx(
    args: argparse.Namespace,
    channel: Any,
    setup: dict[str, Any],
    output_parts: list[str],
) -> None:
    print("[TX] nxp python quit()")
    log_nxp(args, "\n[TX] nxp python quit()\n")
    drain_ssh_channel(channel, args, output_parts)
    channel.send("quit()\r")
    recv_ssh_channel_until_pattern(
        args,
        channel,
        output_parts,
        nxp_shell_prompt_pattern(setup),
        30,
        "NXP shell prompt after quit()",
    )
    time.sleep(1)
    run_external_prbs_test(args, setup, output_parts)
    run_transceiver_eeprom_check(args, setup, output_parts)
    for command in ("cpld w 0x25 0", "cpld w 0x35 0", "cd /root"):
        send_nxp_shell_command(args, channel, setup, output_parts, command)


def run_switch_console_command_capture(
    args: argparse.Namespace,
    uart: Any,
    command: str,
    buffer: str,
    timeout: float = 30,
) -> tuple[str, str]:
    waiting = getattr(uart, "in_waiting", 0) or 0
    if waiting:
        buffer += uart.read(waiting).decode(errors="replace")
    match_start = len(buffer)
    print(f"[TX] switch console {command}")
    log_switch(args, f"\n[TX] switch console {command}\n")
    uart.write((command + "\r\n").encode())
    uart.flush()
    matched, buffer = serial_wait(
        uart,
        {"console": SWITCH_CONSOLE_PROMPT_RE.pattern},
        timeout,
        buffer,
        echo=True,
        match_start=match_start,
    )
    if matched != "console":
        raise RuntimeError(f"Switch console command timed out: {command}")
    log_switch(args, buffer[match_start:])
    return buffer, buffer[match_start:]


def send_switch_enter(
    args: argparse.Namespace,
    uart: Any,
    buffer: str,
    count: int = 1,
    pause: float = 0.2,
) -> str:
    for _ in range(count):
        print("[TX] switch uart <ENTER>")
        log_switch(args, "\n[TX] switch uart <ENTER>\n")
        uart.write(b"\r\n")
        uart.flush()
        time.sleep(pause)
        waiting = getattr(uart, "in_waiting", 0) or 0
        if waiting:
            text = uart.read(waiting).decode(errors="replace")
            buffer += text
            log_switch(args, text)
            print(text, end="", flush=True)
    return buffer


def exit_switch_console(args: argparse.Namespace, uart: Any, setup: dict[str, Any], buffer: str) -> str:
    settings = switch_uart_login_settings(setup)
    prompt_line = shell_prompt_line_pattern(settings["shell_prompt"])
    waiting = getattr(uart, "in_waiting", 0) or 0
    if waiting:
        buffer += uart.read(waiting).decode(errors="replace")
    match_start = len(buffer)
    print("[TX] switch console CLIexit")
    log_switch(args, "\n[TX] switch console CLIexit\n")
    uart.write(b"CLIexit\r\n")
    uart.flush()
    matched, buffer = serial_wait(
        uart,
        {"shell": prompt_line},
        max(settings["prompt_timeout"], 30),
        buffer,
        echo=True,
        match_start=match_start,
    )
    if matched != "shell":
        raise RuntimeError("Switch console exit failed: Sonic prompt not detected after CLIexit.")
    log_switch(args, buffer[match_start:])
    return buffer


def run_external_prbs_test(
    args: argparse.Namespace,
    setup: dict[str, Any],
    output_parts: list[str],
) -> None:
    if serial is None:
        raise RuntimeError("pyserial is not installed. Install it or disable the external PRBS test.")

    settings = switch_uart_login_settings(setup)
    switch_output = "\n=== External PRBS Test =================================================\n"
    second_show_output = ""
    log_switch(args, switch_output)
    print("[INFO] External PRBS test starting.")
    print(f"[TX] uart switch {settings['port']} @ {settings['baudrate']}")
    log_switch(args, f"[TX] uart switch {settings['port']} @ {settings['baudrate']}\n")

    with serial.Serial(port=settings["port"], baudrate=settings["baudrate"], timeout=0.2, write_timeout=1) as uart:
        time.sleep(min(settings["open_timeout"], 1.0))
        switch_output = ensure_switch_uart_shell(uart, setup, switch_output)
        log_switch(args, switch_output)

        for command in (
            "docker exec -it syncd telnet 127.0.0.1 12345",
            "configure",
            "interface range ethernet 0/10,11,12,13",
            "debug",
            "end",
            "dbg link prbs interface ethernet 0/10,11 polynomial 7",
            "dbg link prbs interface ethernet 0/12,13 polynomial 31",
        ):
            switch_output, _ = run_switch_console_command_capture(
                args,
                uart,
                command,
                switch_output,
                timeout=max(settings["prompt_timeout"], 30),
            )
            time.sleep(1)

        switch_output, _ = run_switch_console_command_capture(
            args,
            uart,
            "dbg link prbs show interface ethernet 0/10-13",
            switch_output,
            timeout=max(settings["prompt_timeout"], 30),
        )
        time.sleep(1)
        switch_output, second_show_output = run_switch_console_command_capture(
            args,
            uart,
            "dbg link prbs show interface ethernet 0/10-13",
            switch_output,
            timeout=max(settings["prompt_timeout"], 30),
        )
        time.sleep(1)

        parsed = parse_external_prbs_output(second_show_output)
        for interface in EXTERNAL_PRBS_SWITCH_INTERFACES:
            eth_name = "eth" + interface.split("/", 1)[1]
            lock_state = parsed.get(f"tests.eth.ext_prbs.{eth_name}.status")
            err_count = parsed.get(f"tests.eth.ext_prbs.{eth_name}.err")
            ber = parsed.get(f"tests.eth.ext_prbs.{eth_name}.ber_err")
            if lock_state is None:
                print(f"[INFO] External PRBS {eth_name.upper()} result not found.")
            else:
                print(f"[INFO] External PRBS {eth_name.upper()} {lock_state}, errors={err_count}, BER={ber}.")

        switch_output, _ = run_switch_console_command_capture(
            args,
            uart,
            "end",
            switch_output,
            timeout=max(settings["prompt_timeout"], 30),
        )
        time.sleep(1)
        switch_output = exit_switch_console(args, uart, setup, switch_output)

    output_parts.append(switch_output)
    print("[INFO] External PRBS test completed.")


def run_transceiver_eeprom_check(
    args: argparse.Namespace,
    setup: dict[str, Any],
    output_parts: list[str],
) -> None:
    if serial is None:
        raise RuntimeError("pyserial is not installed. Install it or disable the transceiver EEPROM check.")

    settings = switch_uart_login_settings(setup)
    switch_output = "\n=== Transceiver EEPROM Check ==========================================\n"
    log_switch(args, switch_output)
    print("[INFO] Transceiver EEPROM check starting.")
    print(f"[TX] uart switch {settings['port']} @ {settings['baudrate']}")
    log_switch(args, f"[TX] uart switch {settings['port']} @ {settings['baudrate']}\n")

    with serial.Serial(port=settings["port"], baudrate=settings["baudrate"], timeout=0.2, write_timeout=1) as uart:
        time.sleep(min(settings["open_timeout"], 1.0))
        switch_output = ensure_switch_uart_shell(uart, setup, switch_output)
        log_switch(args, switch_output)
        print("[TX] switch uart show interfaces transceiver eeprom")
        log_switch(args, "\n[TX] switch uart show interfaces transceiver eeprom\n")
        switch_output, eeprom_output = run_switch_uart_command_capture(
            uart,
            setup,
            "show interfaces transceiver eeprom",
            switch_output,
        )
        log_switch(args, eeprom_output)
        print(eeprom_output, end="" if eeprom_output.endswith("\n") else "\n")

    parsed = parse_transceiver_eeprom_output(eeprom_output)
    for interface in ("eth10", "eth11", "eth12", "eth13"):
        pn = parsed.get(f"tests.eth.{interface}_pn")
        sn = parsed.get(f"tests.eth.{interface}_sn")
        if pn is None:
            print(f"[INFO] {interface.upper()} transceiver PN not found.")
        else:
            print(f"[INFO] {interface.upper()} transceiver PN={pn}, SN={sn or 'not found'}.")

    output_parts.append(switch_output)
    print("[INFO] Transceiver EEPROM check completed.")


def run_internal_prbs_test(
    args: argparse.Namespace,
    setup: dict[str, Any],
    channel: Any,
    output_parts: list[str],
) -> None:
    if serial is None:
        raise RuntimeError("pyserial is not installed. Install it or disable the internal PRBS test.")

    settings = switch_uart_login_settings(setup)
    switch_output = "\n=== Internal PRBS Test =================================================\n"
    log_switch(args, switch_output)
    print("[INFO] Internal PRBS test starting.")
    print(f"[TX] uart switch {settings['port']} @ {settings['baudrate']}")
    log_switch(args, f"[TX] uart switch {settings['port']} @ {settings['baudrate']}\n")

    with serial.Serial(port=settings["port"], baudrate=settings["baudrate"], timeout=0.2, write_timeout=1) as uart:
        time.sleep(min(settings["open_timeout"], 1.0))
        switch_output = ensure_switch_uart_shell(uart, setup, switch_output)
        log_switch(args, switch_output)

        switch_output = send_switch_enter(args, uart, switch_output, count=2)

        switch_output, _ = run_switch_console_command_capture(
            args,
            uart,
            "docker exec -it syncd telnet 127.0.0.1 12345",
            switch_output,
            timeout=max(settings["prompt_timeout"], 30),
        )
        for command in (
            "configure",
            "interface range ethernet 0/1-4",
            "debug",
            "end",
            "dbg link prbs interface ethernet 0/1-4 polynomial 31",
        ):
            switch_output, _ = run_switch_console_command_capture(args, uart, command, switch_output)

        for command in INTERNAL_PRBS_NXP_COMMANDS:
            send_nxp_python_command(channel, args, output_parts, command)

        switch_output, _ = run_switch_console_command_capture(
            args,
            uart,
            "dbg link prbs show interface ethernet 0/1-4",
            switch_output,
            timeout=max(settings["prompt_timeout"], 30),
        )
        time.sleep(1)
        switch_output, second_show_output = run_switch_console_command_capture(
            args,
            uart,
            "dbg link prbs show interface ethernet 0/1-4",
            switch_output,
            timeout=max(settings["prompt_timeout"], 30),
        )
        require_internal_prbs_switch_locks(second_show_output)
        print("[INFO] Switch PRBS locks detected on 0/1-4.")

        for sx_id in ("SX1", "SX2"):
            send_sx_prbs_read_command(channel, args, output_parts, sx_id)

        exit_switch_console(args, uart, setup, switch_output)

    output_parts.append(switch_output)
    print("[INFO] Internal PRBS test completed.")


def recv_exact(sock: Any, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining > 0:
        chunk = sock.recv(remaining)
        if not chunk:
            raise ScpiError("Socket closed while reading binary SCPI block.")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def query_binary_block(spectrum: KeysightN9010B, command: str) -> bytes:
    sock = spectrum._require_socket()
    spectrum.write(command)
    header = recv_exact(sock, 2)
    if header[:1] != b"#":
        chunks = [header]
        while True:
            try:
                chunk = sock.recv(spectrum.config.recv_size)
            except TimeoutError:
                break
            except OSError:
                break
            if not chunk:
                break
            chunks.append(chunk)
            if chunk.endswith(b"\n"):
                break
        return b"".join(chunks).rstrip(b"\r\n")

    digit_count = int(header[1:2].decode("ascii"))
    if digit_count == 0:
        chunks: list[bytes] = []
        while True:
            chunk = sock.recv(spectrum.config.recv_size)
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks)

    length = int(recv_exact(sock, digit_count).decode("ascii"))
    payload = recv_exact(sock, length)
    try:
        sock.settimeout(0.1)
        sock.recv(1)
    except OSError:
        pass
    finally:
        sock.settimeout(spectrum.config.timeout_seconds)
    return payload


def csv_number_values(raw: str) -> list[float]:
    values: list[float] = []
    for item in raw.replace("\n", ",").split(","):
        item = item.strip()
        if not item:
            continue
        try:
            values.append(float(item))
        except ValueError:
            continue
    return values


def scpi_state_path(path: str) -> str:
    if "'" in path:
        raise RuntimeError("RF state path cannot contain a single quote for SCPI MMEM:LOAD:STAT.")
    return f"'{path}'"


def safe_rf_query(spectrum: KeysightN9010B, command: str) -> str:
    try:
        return spectrum.query(command)
    except ScpiError as exc:
        return f"ERROR: {exc}"


def open_and_load_rf_spectrum(args: argparse.Namespace) -> KeysightN9010B | None:
    if not getattr(args, "rf_spectrum", True):
        return None

    config_path = pathlib.Path(getattr(args, "rf_config_path", args.config))
    config = load_spectrum_config(config_path)
    ip_address = args.rf_ip or config.ip_address
    port = args.rf_port if args.rf_port is not None else config.port
    timeout = args.rf_timeout if args.rf_timeout is not None else config.timeout_seconds

    spectrum = KeysightN9010B(ip_address=ip_address, port=port, timeout_seconds=timeout)
    try:
        spectrum.connect()
        idn = spectrum.idn()
        args.rf_idn = idn
        print(f"[INFO] Connected RF spectrum analyzer: {idn}")
        spectrum.write("*CLS")
        spectrum.write(f":MMEM:LOAD:STAT {scpi_state_path(args.rf_state_path)}")
        if spectrum._socket is not None:
            spectrum._socket.settimeout(args.rf_trigger_timeout)
        load_opc = spectrum.query("*OPC?")
        if load_opc.strip() != "1":
            raise RuntimeError(f"EXA state load did not complete; *OPC? returned {load_opc!r}.")
        if spectrum._socket is not None:
            spectrum._socket.settimeout(timeout)
        print(f"[INFO] Loaded RF state; *OPC? returned {load_opc.strip()}. EXA will be armed after Full Loopback Test pass: {args.rf_state_path}")
        return spectrum
    except Exception:
        spectrum.close()
        raise


def arm_rf_spectrum(args: argparse.Namespace, spectrum: KeysightN9010B | None) -> None:
    if spectrum is None:
        return
    spectrum.write(":INITiate:CONTinuous OFF")
    spectrum.write(":INITiate:IMMediate")
    print("[INFO] Full Loopback Test passed; armed EXA trigger for RF measurement.")


def collect_rf_spectrum(args: argparse.Namespace, spectrum: KeysightN9010B | None) -> None:
    if spectrum is None or getattr(args, "rf_measurement", None) is not None:
        return

    previous_timeout = spectrum.config.timeout_seconds
    if spectrum._socket is not None:
        spectrum._socket.settimeout(args.rf_trigger_timeout)
    try:
        opc = spectrum.query("*OPC?")
        channel_power_raw = spectrum.query(args.rf_channel_power_query)
        start_freq = safe_rf_query(spectrum, ":SENSe:FREQuency:STARt?")
        stop_freq = safe_rf_query(spectrum, ":SENSe:FREQuency:STOP?")
        sweep_points = safe_rf_query(spectrum, ":SENSe:SWEep:POINts?")
        psd_raw = spectrum.query(args.rf_psd_query)
        screenshot_bytes = b""
        screenshot_error = ""
        if args.rf_screenshot:
            try:
                if args.rf_screenshot_format_command:
                    spectrum.write(args.rf_screenshot_format_command)
                screenshot_bytes = query_binary_block(spectrum, args.rf_screenshot_query)
            except Exception as exc:
                screenshot_error = f"{type(exc).__name__}: {exc}"
                print(f"[WARN] RF screenshot capture failed: {screenshot_error}")
        args.rf_measurement = {
            "idn": getattr(args, "rf_idn", ""),
            "state_path": args.rf_state_path,
            "trigger_point": "Full Loopback Test pass",
            "trigger_opc": opc,
            "channel_power_query": args.rf_channel_power_query,
            "channel_power_raw": channel_power_raw,
            "channel_power_values": csv_number_values(channel_power_raw),
            "psd_query": args.rf_psd_query,
            "psd_raw": psd_raw,
            "psd_values": csv_number_values(psd_raw),
            "start_freq_hz": start_freq,
            "stop_freq_hz": stop_freq,
            "sweep_points": sweep_points,
            "screenshot_filename": RF_SCREENSHOT_FILENAME if screenshot_bytes else "",
            "screenshot_bytes": screenshot_bytes,
            "screenshot_error": screenshot_error,
        }
        print("[INFO] RF channel power and PSD data captured from triggered EXA measurement.")
    finally:
        if spectrum._socket is not None:
            spectrum._socket.settimeout(previous_timeout)


def save_rf_artifacts(run_dir: pathlib.Path, args: argparse.Namespace) -> None:
    measurement = getattr(args, "rf_measurement", None)
    if not measurement:
        return

    summary_lines = [
        f"dig_sn: {getattr(args, 'dig_sn', '')}",
        f"idn: {measurement['idn']}",
        f"state_path: {measurement['state_path']}",
        f"trigger_point: {measurement['trigger_point']}",
        f"trigger_opc: {measurement['trigger_opc']}",
        f"channel_power_query: {measurement['channel_power_query']}",
        f"channel_power_raw: {measurement['channel_power_raw']}",
        f"psd_query: {measurement['psd_query']}",
        f"start_freq_hz: {measurement['start_freq_hz']}",
        f"stop_freq_hz: {measurement['stop_freq_hz']}",
        f"sweep_points: {measurement['sweep_points']}",
        f"screenshot_file: {measurement['screenshot_filename']}",
        f"screenshot_error: {measurement['screenshot_error']}",
    ]
    (run_dir / "rf_summary.txt").write_text("\n".join(summary_lines) + "\n", encoding="utf-8")

    with (run_dir / "rf_channel_power.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["index", "value"])
        for index, value in enumerate(measurement["channel_power_values"], start=1):
            writer.writerow([index, value])

    with (run_dir / "rf_psd.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["index", "value"])
        for index, value in enumerate(measurement["psd_values"], start=1):
            writer.writerow([index, value])

    (run_dir / "rf_psd_raw.txt").write_text(measurement["psd_raw"], encoding="utf-8")
    if measurement["screenshot_bytes"]:
        (run_dir / measurement["screenshot_filename"]).write_bytes(measurement["screenshot_bytes"])


def run_paramiko_interactive(args: argparse.Namespace, command: str, setup: dict[str, Any] | None = None) -> str:
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
    rf_analyzer: KeysightN9010B | None = None

    try:
        channel = client.invoke_shell()
        channel.settimeout(0.0)
        log_nxp(args, f"\n[TX] nxp shell ({command}); printf '\\n{marker}%s\\n' $?\n")
        channel.send(f"({command}); printf '\\n{marker}%s\\n' $?\n")

        while True:
            if time.monotonic() - started > args.timeout:
                raise TimeoutError(f"Timed out after {args.timeout} seconds waiting for SSH command to finish.")

            if channel.recv_ready():
                chunk = channel.recv(4096).decode(errors="replace")
                output_parts.append(chunk)
                log_nxp(args, chunk)
                print(strip_terminal_cpr(chunk), end="", flush=True)
                last_progress = time.monotonic()

                clean_output = strip_ansi("".join(output_parts))
                if args.mode == "run-sh" and RUN_SH_DONE_RE.search(clean_output):
                    print("\n[INFO] run.sh completion marker detected.")
                    exit_status = 0
                    break

                if full_like_mode and not sent_full_test_command and RUN_SH_DONE_RE.search(clean_output) and ">>>" in clean_output:
                    rf_analyzer = open_and_load_rf_spectrum(args)
                    log_nxp(args, "\n[TX] nxp python lsbb_cil_test()\n")
                    channel.send("lsbb_cil_test()\r")
                    sent_full_test_command = True
                    print("\n[INFO] Started full test: lsbb_cil_test()")

                if (
                    full_like_mode
                    and rf_analyzer is not None
                    and getattr(args, "rf_measurement", None) is None
                    and FULL_LOOPBACK_PASS_RE.search(clean_output)
                ):
                    arm_rf_spectrum(args, rf_analyzer)
                    collect_rf_spectrum(args, rf_analyzer)

                if full_like_mode and FULL_TEST_DONE_RE.search(clean_output):
                    if args.mode == "full" and setup is not None:
                        run_internal_prbs_test(args, setup, channel, output_parts)
                        exit_python_and_shutdown_sx(args, channel, setup, output_parts)
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
                        log_nxp(args, f"\n[TX] nxp prompt response {response!r}\n")
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
        if rf_analyzer is not None:
            rf_analyzer.close()
        client.close()

    combined = strip_terminal_cpr("".join(output_parts))
    combined = EXIT_MARKER_RE.sub("", combined)
    if exit_status is None:
        raise RuntimeError(f"SSH command ended before exit marker was found.\n{combined.strip()}")
    if exit_status != 0:
        raise RuntimeError(f"SSH command failed with exit code {exit_status}.\n{combined.strip()}")
    return combined


def run_ssh(args: argparse.Namespace, setup: dict[str, Any] | None = None) -> str:
    command = args.remote_command or f"cd {args.remote_dir} && sh ./{args.script}"
    if args.password and paramiko is not None and not args.force_openssh:
        print(f"[TX] ssh {args.user}@{args.host} {command}")
        if args.interactive:
            return run_paramiko_interactive(args, command, setup)
        return run_paramiko_exec(args, command)

    if args.password and paramiko is None and not args.force_openssh:
        print("[WARN] paramiko is not installed, so password SSH cannot be automated. Falling back to OpenSSH.")

    ssh_cmd = build_ssh_command(args)
    print(f"[TX] {' '.join(ssh_cmd)}")
    log_nxp(args, f"\n[TX] {' '.join(ssh_cmd)}\n")
    completed = subprocess.run(ssh_cmd, text=True, capture_output=True, timeout=args.timeout)
    output = (completed.stdout or "") + (completed.stderr or "")
    log_nxp(args, output)
    if completed.returncode != 0:
        raise RuntimeError(f"SSH command failed with exit code {completed.returncode}.\n{output.strip()}")
    return output


def check_nxp_login(args: argparse.Namespace) -> str:
    print(f"[INFO] Checking NXP login from script_setup.yaml: {args.user}@{args.host}")
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

    print(f"[INFO] Checking switch login from script_setup.yaml: {settings['username']} on {port}")
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
    username = get_nested(setup, "sonic.login")
    password = get_nested(setup, "sonic.password")
    login_prompt = get_nested(setup, "sonic.login_prompt")
    shell_prompt = get_nested(setup, "sonic.prompt")
    missing = [
        name
        for name, value in (
            ("sonic.login", username),
            ("sonic.password", password),
            ("sonic.login_prompt", login_prompt),
            ("sonic.prompt", shell_prompt),
        )
        if value in (None, "")
    ]
    if missing:
        raise RuntimeError(f"Missing switch login settings in script_setup.yaml: {', '.join(missing)}")
    return {
        "port": str(port),
        "baudrate": int(get_nested(setup, "serial.switch.baudrate", 115200)),
        "username": str(username),
        "password": str(password),
        "login_prompt": str(login_prompt),
        "shell_prompt": str(shell_prompt),
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


def output_dir(log_root: pathlib.Path, dig_sn: str, when: dt.datetime) -> pathlib.Path:
    base_folder = when.strftime("%Y%m%d_%H%M%S_test_rf")
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
    for interface in ("eth10", "eth11", "eth12", "eth13"):
        pn = actual.get(f"tests.eth.{interface}_pn")
        sn = actual.get(f"tests.eth.{interface}_sn")
        if pn is not None:
            lines.append(f"{interface.upper()}_PN: {pn}")
        if sn is not None:
            lines.append(f"{interface.upper()}_SN: {sn}")
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
        return "dB"
    if name.startswith("tests.eth.prbs.") and name.endswith("_ber"):
        return "BER"
    if name.startswith("tests.eth.prbs.") and name.endswith("_errcount"):
        return "errors"
    if name.startswith("tests.eth.ext_prbs.") and name.endswith(".ber_err"):
        return "BER"
    if name.startswith("tests.eth.ext_prbs.") and name.endswith(".err"):
        return "errors"
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
    save_rf_artifacts(run_dir, args)


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
    save_rf_artifacts(run_dir, args)


def legacy_save_output(repo_root: pathlib.Path, output: str) -> pathlib.Path:
    logs_dir = repo_root / "logs"
    logs_dir.mkdir(exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    path = logs_dir / f"test_output_{stamp}.txt"
    path.write_text(output, encoding="utf-8")
    return path


def validate_sql_identifier(name: str, label: str) -> str:
    if not SQL_IDENTIFIER_PATTERN.fullmatch(name):
        raise RuntimeError(f"Invalid SQL identifier for {label}: {name}")
    return name


def load_sql_config(repo_root: pathlib.Path) -> SqlConfig:
    path = repo_root / "db_config.ini"
    if not path.is_file():
        raise RuntimeError(f"Missing SQL config file: {path}")
    parser = configparser.ConfigParser()
    parser.read(path, encoding="utf-8")
    if "sql_server" not in parser:
        raise RuntimeError(f"Missing [sql_server] section in {path}")

    section = parser["sql_server"]
    server = os.environ.get("SERVER_NAME", section.get("server", "")).strip()
    database = os.environ.get("DB_NAME", section.get("database", "")).strip()
    username = os.environ.get("DB_LOGIN", section.get("username", "")).strip()
    password = os.environ.get("DB_PASSWORD", section.get("password", "")).strip()
    schema = validate_sql_identifier(section.get("schema", "dbo").strip(), "schema")
    driver_candidates = tuple(
        driver.strip()
        for driver in section.get("driver_candidates", "").split(",")
        if driver.strip()
    ) or ("ODBC Driver 18 for SQL Server", "ODBC Driver 17 for SQL Server", "SQL Server Native Client 11.0", "SQL Server")
    connection_string = section.get(
        "connection_string",
        "DRIVER={{{driver}}};SERVER={server};DATABASE={database};UID={username};PWD={password};Encrypt=no;TrustServerCertificate=yes;",
    ).strip()
    timeout_seconds = section.getint("timeout_seconds", fallback=5)
    missing = [
        name
        for name, value in {
            "server": server,
            "database": database,
            "username": username,
            "password": password,
        }.items()
        if not value
    ]
    if missing:
        raise RuntimeError(f"Missing SQL config values in {path}: {', '.join(missing)}")
    return SqlConfig(
        server=server,
        database=database,
        username=username,
        password=password,
        schema=schema,
        driver_candidates=driver_candidates,
        connection_string=connection_string,
        timeout_seconds=timeout_seconds,
    )


def open_sql_connection(config: SqlConfig):
    try:
        import pyodbc  # type: ignore[import-not-found]
    except ImportError as exc:
        raise RuntimeError("pyodbc is required for --save-sfp SQL Server access.") from exc

    installed = set(pyodbc.drivers())
    drivers = [driver for driver in config.driver_candidates if driver in installed]
    if not drivers:
        drivers = list(config.driver_candidates)

    last_error: Exception | None = None
    for driver in drivers:
        connection_string = config.connection_string.format(
            driver=driver,
            server=config.server,
            database=config.database,
            username=config.username,
            password=config.password,
        )
        try:
            return pyodbc.connect(connection_string, timeout=config.timeout_seconds)
        except pyodbc.Error as exc:
            last_error = exc
    raise RuntimeError(f"Could not connect to SQL Server {config.server}/{config.database}: {last_error}")


def save_sfp_records_to_sql(repo_root: pathlib.Path, dig_sn: str, records: dict[str, SfpRecord]) -> str:
    config = load_sql_config(repo_root)
    values: dict[str, str] = {"dig_board_sn": dig_sn}
    for port, column_prefix in SFP_FIELD_MAP.items():
        values[f"{column_prefix}_sn"] = records[port].sn
        values[f"{column_prefix}_pn"] = records[port].pn

    sfp_columns = [column for column in values if column != "dig_board_sn"]
    with open_sql_connection(config) as connection:
        cursor = connection.cursor()
        try:
            cursor.execute(
                f"SELECT 1 FROM {config.pairing_qualified_name} WHERE dig_board_sn = ?",
                dig_sn,
            )
            exists = cursor.fetchone() is not None
            if exists:
                assignments = ", ".join(f"{column} = ?" for column in sfp_columns)
                params = [values[column] for column in sfp_columns]
                params.append(dig_sn)
                cursor.execute(
                    f"UPDATE {config.pairing_qualified_name} SET {assignments} WHERE dig_board_sn = ?",
                    *params,
                )
                action = "updated"
            else:
                columns = ["dig_board_sn", *sfp_columns]
                placeholders = ", ".join("?" for _column in columns)
                params = [values[column] for column in columns]
                cursor.execute(
                    f"INSERT INTO {config.pairing_qualified_name} ({', '.join(columns)}) VALUES ({placeholders})",
                    *params,
                )
                action = "inserted"
            connection.commit()
            return action
        except Exception:
            connection.rollback()
            raise


def save_sfp_if_requested(args: argparse.Namespace, repo_root: pathlib.Path, output: str, expected: dict[str, Any]) -> None:
    if not args.save_sfp:
        return

    records = parse_latest_sfp_records(output)
    validate_sfp_records(records, expected)
    try:
        action = save_sfp_records_to_sql(repo_root, args.dig_sn, records)
    except Exception as exc:
        raise RuntimeError(f"SFP SQL save failed; not saved: {exc}") from exc
    print(f"[INFO] SFP data {action} in SQL DB for dig_board_sn {args.dig_sn}.")


def print_report(results: list[CheckResult], actual: dict[str, Any], dig_sn: str = "XXXXXXX") -> int:
    report, exit_code = report_text(results, actual, dig_sn)
    print("\n" + report, end="")
    return exit_code


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run LSBB system tests over SSH and compare the output with test_setup.yaml.")
    parser.add_argument("--dig_sn", required=True, help="DUT digital serial number used for the test log folder.")
    parser.add_argument("--config", default="test_setup.yaml", help="YAML file with expected values.")
    parser.add_argument("--setup-config", default="script_setup.yaml", help="YAML file with SSH connection information.")
    parser.add_argument("--mode", choices=("full", "run-sh", "skip_eth"), default="full", help="full runs/checks lsbb_cil_test too; run-sh checks only run.sh output.")
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
    parser.add_argument("--no-save", action="store_true", help="Do not save test artifacts.")
    parser.add_argument("--save-sfp", action="store_true", help="Validate and save ETH10-ETH13 SFP PN/SN data to SQL Server.")
    parser.add_argument("--rf-spectrum", action=argparse.BooleanOptionalAction, default=True, help="Load the EXA state before lsbb_cil_test(), then arm and capture after Full Loopback Test passes.")
    parser.add_argument("--rf-state-path", default=DEFAULT_RF_STATE_PATH, help="EXA state file loaded before lsbb_cil_test().")
    parser.add_argument("--rf-ip", help="Override spectrum.ip from the setup YAML file.")
    parser.add_argument("--rf-port", type=int, help="Override spectrum.socket from the setup YAML file.")
    parser.add_argument("--rf-timeout", type=float, help="Socket timeout for normal EXA SCPI commands.")
    parser.add_argument("--rf-trigger-timeout", type=float, default=120.0, help="Seconds to wait for EXA state load and the triggered acquisition to complete.")
    parser.add_argument("--rf-channel-power-query", default=RF_CHANNEL_POWER_QUERY, help="SCPI query used to fetch channel power results.")
    parser.add_argument("--rf-psd-query", default=RF_PSD_QUERY, help="SCPI query used to fetch PSD/trace data.")
    parser.add_argument("--rf-screenshot", action=argparse.BooleanOptionalAction, default=True, help="Save an EXA display screenshot in the test log folder.")
    parser.add_argument("--rf-screenshot-format-command", default=RF_SCREENSHOT_FORMAT_COMMAND, help="Optional SCPI command used to select screenshot format; default sends no format command.")
    parser.add_argument("--rf-screenshot-query", default=RF_SCREENSHOT_QUERY, help="SCPI query used to fetch screenshot image bytes.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    repo_root = pathlib.Path(__file__).resolve().parent
    config_path = (repo_root / args.config).resolve()
    setup_path = (repo_root / args.setup_config).resolve()
    run_dir: pathlib.Path | None = None
    output = ""
    report = ""
    csv_report = ""
    exit_code = 1
    args.run_dir = None
    args.rf_config_path = None
    args.rf_measurement = None

    try:
        expected = load_yaml(config_path)
        setup = load_yaml(setup_path)
        args.rf_config_path = config_path
        apply_connection_defaults(args, setup)

        if not args.no_save:
            log_path = get_nested(expected, "logs.path")
            log_root = pathlib.Path(str(log_path)) if log_path else repo_root / "logs"
            run_dir = output_dir(log_root, args.dig_sn, dt.datetime.now())
            run_dir.mkdir(parents=True, exist_ok=False)
            args.run_dir = run_dir
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
            output = run_ssh(args, setup)
            if switch_uart_output:
                output += "\n" + switch_uart_output
            if not getattr(args, "output_streamed", False):
                print(output, end="" if output.endswith("\n") else "\n")

        actual = parse_output(output)
        results = compare(
            expected,
            actual,
            include_full_tests=args.mode in {"full", "skip_eth"},
            include_prbs_tests=args.mode == "full",
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

        save_sfp_if_requested(args, repo_root, output, expected)

        return exit_code
    except (OSError, RuntimeError, subprocess.SubprocessError, TimeoutError) as exc:
        if run_dir is not None:
            save_error_artifacts(run_dir, output, args, exc)
            print(f"[INFO] Saved error artifacts to {run_dir}")
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
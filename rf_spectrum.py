from __future__ import annotations

import argparse
import pathlib
import socket
from dataclasses import dataclass
from typing import Any


DEFAULT_SCPI_PORT = 5025
DEFAULT_TIMEOUT_SECONDS = 5.0
DEFAULT_TERMINATOR = "\n"
DEFAULT_CONFIG_PATH = pathlib.Path("test_setup.yaml")


class ScpiError(RuntimeError):
    """Raised when the SCPI socket connection or command fails."""


class ConfigError(RuntimeError):
    """Raised when the spectrum YAML configuration is missing or invalid."""


@dataclass(frozen=True)
class KeysightN9010BConfig:
    ip_address: str
    port: int = DEFAULT_SCPI_PORT
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    terminator: str = DEFAULT_TERMINATOR
    recv_size: int = 4096


class KeysightN9010B:
    """Small LAN SCPI driver for a Keysight N9010B spectrum analyzer."""

    def __init__(
        self,
        ip_address: str,
        port: int = DEFAULT_SCPI_PORT,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        terminator: str = DEFAULT_TERMINATOR,
    ) -> None:
        self.config = KeysightN9010BConfig(
            ip_address=ip_address,
            port=port,
            timeout_seconds=timeout_seconds,
            terminator=terminator,
        )
        self._socket: socket.socket | None = None

    def __enter__(self) -> "KeysightN9010B":
        self.connect()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    @property
    def connected(self) -> bool:
        return self._socket is not None

    def connect(self) -> None:
        if self._socket is not None:
            return

        try:
            sock = socket.create_connection(
                (self.config.ip_address, self.config.port),
                timeout=self.config.timeout_seconds,
            )
            sock.settimeout(self.config.timeout_seconds)
        except OSError as exc:
            raise ScpiError(
                f"Failed to connect to N9010B at "
                f"{self.config.ip_address}:{self.config.port}: {exc}"
            ) from exc

        self._socket = sock

    def close(self) -> None:
        if self._socket is None:
            return

        try:
            self._socket.close()
        finally:
            self._socket = None

    def write(self, command: str) -> None:

        #print(f"Sending: {command!r}")
        sock = self._require_socket()
        payload = self._encode_command(command)

        try:

            #print(repr(command))
            #print(repr(payload))
            sock.sendall(payload)
        except OSError as exc:
            raise ScpiError(f"Failed to send SCPI command {command!r}: {exc}") from exc

    def read(self) -> str:
        sock = self._require_socket()
        chunks: list[bytes] = []
        terminator = self.config.terminator.encode("ascii")

        while True:
            try:
                chunk = sock.recv(self.config.recv_size)
            except socket.timeout as exc:
                if chunks:
                    break
                raise ScpiError("Timed out waiting for SCPI response.") from exc
            except OSError as exc:
                raise ScpiError(f"Failed to read SCPI response: {exc}") from exc

            if not chunk:
                break

            chunks.append(chunk)
            if chunk.endswith(terminator) or terminator in chunk:
                break

        return b"".join(chunks).decode("ascii", errors="replace").strip()

    def query(self, command: str) -> str:
        self.write(command)
        return self.read()

    def idn(self) -> str:
        return self.query("*IDN?")

    def _require_socket(self) -> socket.socket:
        if self._socket is None:
            self.connect()
        if self._socket is None:
            raise ScpiError("SCPI socket is not connected.")
        return self._socket

    def _encode_command(self, command: str) -> bytes:
        normalized = command.rstrip("\r\n") + self.config.terminator
        return normalized.encode("ascii")


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

    for line_number, raw_line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
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


def load_spectrum_config(path: pathlib.Path) -> KeysightN9010BConfig:
    if not path.exists():
        raise ConfigError(f"Configuration file not found: {path}")

    payload = load_simple_yaml(path)
    spectrum = payload.get("spectrum")
    if not isinstance(spectrum, dict):
        raise ConfigError(f"Missing 'spectrum' section in {path}")

    ip_address = spectrum.get("ip") or spectrum.get("ip_address")
    if not ip_address:
        raise ConfigError("Missing spectrum.ip in test setup configuration.")

    port = spectrum.get("socket", spectrum.get("port", DEFAULT_SCPI_PORT))
    timeout = spectrum.get("timeout", spectrum.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS))

    return KeysightN9010BConfig(
        ip_address=str(ip_address),
        port=int(port),
        timeout_seconds=float(timeout),
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Keysight N9010B LAN SCPI driver utility."
    )
    parser.add_argument(
        "--config",
        type=pathlib.Path,
        default=DEFAULT_CONFIG_PATH,
        help=f"Spectrum setup YAML file. Default: {DEFAULT_CONFIG_PATH}",
    )
    parser.add_argument(
        "--dig_sn",
        help="Optional DUT digital serial number for caller/artifact metadata.",
    )
    parser.add_argument(
        "--ip",
        dest="ip_address",
        help="Override spectrum.ip from the setup YAML file.",
    )
    parser.add_argument(
        "--port",
        type=int,
        help="Override spectrum.socket from the setup YAML file.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        help="Override spectrum.timeout from the setup YAML file.",
    )
    parser.add_argument(
        "--command",
        default="*IDN?",
        help='SCPI query command to send. Default: "*IDN?"',
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    try:
        config = load_spectrum_config(args.config)
        ip_address = args.ip_address or config.ip_address
        port = args.port if args.port is not None else config.port
        timeout = args.timeout if args.timeout is not None else config.timeout_seconds

        with KeysightN9010B(
            ip_address=ip_address,
            port=port,
            timeout_seconds=timeout,
        ) as spectrum:
            if args.command.rstrip().endswith("?"):
                print(spectrum.query(args.command))
            else:
                spectrum.write(args.command)
    except (ConfigError, ScpiError) as exc:
        print(f"ERROR: {exc}")
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
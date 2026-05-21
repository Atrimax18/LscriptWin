from __future__ import annotations

import argparse
import pathlib
import socket
import sys

import lscriptwin
import sx4000_config
from lscriptwin import ConfigError, info, ok


class StageError(RuntimeError):
    def __init__(self, stage: str, message: str) -> None:
        super().__init__(message)
        self.stage = stage


def parse_bool(raw: str) -> bool:
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "y", "on"}:
        return True
    if value in {"0", "false", "no", "n", "off"}:
        return False
    raise argparse.ArgumentTypeError("expected true or false")


def run_sx4000_stage(
    config: sx4000_config.Sx4000RuntimeConfig,
    repo_root: pathlib.Path,
    *,
    skip_transfer: bool,
) -> None:
    sx4000_config.validate_files(config)
    commands = sx4000_config.default_sx4000_commands(config)
    ok(f"Parsed commands: {len(commands.sonic)} switch, {len(commands.sx1)} SX1, {len(commands.sx2)} SX2.")

    sx4000_config.configure_switch(config, commands, repo_root)
    sx4000_config.configure_sx4000_sequence(config, commands, repo_root, skip_transfer=skip_transfer)
    ok("SX4000 configuration flow completed.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run LSBB deployment first, then optionally configure SX4000.")
    parser.add_argument("--config", default="script_setup.yaml", help="Path to YAML configuration.")
    parser.add_argument(
        "--mode",
        choices=["detect", "mac-only", "provision", "gen_mac"],
        default="provision",
        help="Deployment mode passed to lscriptwin.py. Defaults to provision.",
    )
    parser.add_argument("--boot_stop_key", "--boot-stop-key", choices=["enter", "space", "ctrl-c"], default="enter")
    parser.add_argument("--skip_switch", "--skip-switch", action="store_true")
    parser.add_argument("--base_mac", "--base-mac")
    parser.add_argument("--switch_uboot_mac", "--switch-uboot-mac")
    parser.add_argument("--switch_onie_mac", "--switch-onie-mac")
    parser.add_argument("--deploy_script", "--deploy-script")
    parser.add_argument("--switch_image", "--switch-image")
    parser.add_argument("--switch_itb", "--switch-itb")
    parser.add_argument("--skip_utils", "--skip-utils", action="store_true")
    parser.add_argument("--dig_sn", "--dig-sn")
    parser.add_argument(
        "--sx_config",
        "--sx-config",
        type=parse_bool,
        default=False,
        metavar="true|false",
        help="Run the SX4000 configuration stage after successful deployment.",
    )
    parser.add_argument("--sx_skip_transfer", "--sx-skip-transfer", action="store_true", help="Run SX4000 commands but skip SFTP uploads.")
    return parser


def build_deployment_argv(args: argparse.Namespace) -> list[str]:
    deployment_argv = ["--config", args.config, "--mode", args.mode, "--boot-stop-key", args.boot_stop_key]
    for flag in ("skip_switch", "skip_utils"):
        if getattr(args, flag):
            deployment_argv.append("--" + flag.replace("_", "-"))
    for name in (
        "base_mac",
        "switch_uboot_mac",
        "switch_onie_mac",
        "deploy_script",
        "switch_image",
        "switch_itb",
        "dig_sn",
    ):
        value = getattr(args, name)
        if value is not None:
            deployment_argv.extend(["--" + name.replace("_", "-"), value])
    return deployment_argv


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    repo_root = pathlib.Path(__file__).resolve().parent
    config_path = (repo_root / args.config).resolve()

    try:
        info("Starting deployment stage.")
        deploy_status = lscriptwin.main(build_deployment_argv(args))
        if deploy_status != 0:
            raise StageError("deployment", f"deployment returned exit code {deploy_status}")
        ok("Deployment stage PASSED.")

        if not args.sx_config:
            ok("PASSED: deployment completed successfully. SX4000 configuration was not requested.")
            return 0

        info("Starting SX4000 configuration stage.")
        sx_runtime_config = sx4000_config.load_runtime_config(config_path)
        run_sx4000_stage(
            sx_runtime_config,
            repo_root,
            skip_transfer=args.sx_skip_transfer,
        )
        ok("PASSED: deployment and SX4000 configuration completed successfully.")
        return 0
    except StageError as exc:
        print(f"[FAILED] {exc.stage}: {exc}", file=sys.stderr)
        return 1
    except (OSError, ConfigError, RuntimeError, TimeoutError, socket.error) as exc:
        print(f"[FAILED] {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

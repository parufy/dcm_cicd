#!/usr/bin/env python3
"""Run Vaunix ATT control on an ATT control PC over SSH."""

from __future__ import annotations

import argparse
import base64
import json
import logging
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from ssh_client import make_client

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] vatt_control - %(message)s",
)
logger = logging.getLogger("vatt_control")


def _as_remote_path(path: str) -> str:
    return path.replace("\\", "/")


def _remote_join(base: str, name: str) -> str:
    return _as_remote_path(base).rstrip("/") + "/" + name


def _append_optional(cmd: list[str], flag: str, value) -> None:
    if value is not None and value != "":
        cmd += [flag, str(value)]


def _build_remote_cmd(args: argparse.Namespace) -> list[str]:
    script_path = _remote_join(args.remote_dir, "Vaunix_lda802q_control.py")
    cmd = [
        args.python_path,
        script_path,
        "--mode",
        args.mode,
        "--dll-dir",
        args.dll_dir,
    ]
    _append_optional(cmd, "--serial", args.serial)
    _append_optional(cmd, "--channel", args.channel)
    _append_optional(cmd, "--attenuation-db", args.attenuation_db)
    _append_optional(cmd, "--start-db", args.start_db)
    _append_optional(cmd, "--stop-db", args.stop_db)
    _append_optional(cmd, "--step-db", args.step_db)
    _append_optional(cmd, "--dwell-ms", args.dwell_ms)
    _append_optional(cmd, "--step-db2", args.step_db2)
    _append_optional(cmd, "--dwell-ms2", args.dwell_ms2)
    _append_optional(cmd, "--idle-ms", args.idle_ms)
    _append_optional(cmd, "--hold-ms", args.hold_ms)
    _append_optional(cmd, "--ramps-b64", args.ramps_b64)
    _append_optional(cmd, "--settings-b64", args.settings_b64)
    if args.channels:
        cmd += ["--channels", *[str(ch) for ch in args.channels]]
    if args.test_mode:
        cmd.append("--test-mode")
    if args.no_go:
        cmd.append("--no-go")
    if args.bidirectional:
        cmd.append("--bidirectional")
    if args.repeat:
        cmd.append("--repeat")
    return cmd


def _extract_json_object(text: str) -> dict | None:
    decoder = json.JSONDecoder()
    for idx, char in enumerate(text):
        if char != "{":
            continue
        try:
            value, _ = decoder.raw_decode(text[idx:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    return None


def _append_jsonl_utf8_sig(path: Path, item: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(item, ensure_ascii=False) + "\n"
    if path.exists() and path.stat().st_size > 0:
        with path.open("a", encoding="utf-8") as f:
            f.write(line)
    else:
        path.write_text(line, encoding="utf-8-sig")


def _requested_settings(args: argparse.Namespace) -> dict | None:
    """Record requested set values without reading them back from the device."""
    if args.mode == "set_multi" and args.settings_b64:
        try:
            decoded = base64.urlsafe_b64decode(args.settings_b64.encode("ascii"))
            specs = json.loads(decoded.decode("utf-8"))
            return {
                str(spec["channel"]): spec["attenuation_db"]
                for spec in specs
                if isinstance(spec, dict)
                and "channel" in spec
                and "attenuation_db" in spec
            }
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError, TypeError):
            return None
    if args.attenuation_db is None:
        return None
    if args.mode == "set" and args.channel is not None:
        return {str(args.channel): args.attenuation_db}
    if args.mode == "set_all":
        if args.channels:
            return {str(channel): args.attenuation_db for channel in args.channels}
        return {"all": args.attenuation_db}
    return None


def _requested_ramps(args: argparse.Namespace) -> list[dict] | None:
    if not args.ramps_b64:
        return None
    try:
        decoded = base64.urlsafe_b64decode(args.ramps_b64.encode("ascii"))
        value = json.loads(decoded.decode("utf-8"))
        return value if isinstance(value, list) else None
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return None


def _requested_stop_channels(args: argparse.Namespace) -> list[int] | None:
    if args.mode == "stop_ramp_multi" and args.channels:
        return list(args.channels)
    return None


def run_vatt_control(args: argparse.Namespace) -> bool:
    output_file = Path(args.output)
    history_file = output_file.with_name("vatt_history.jsonl")
    report = {
        "timestamp": datetime.now().isoformat(),
        "exec_mode": "ssh" if args.ssh_host else "local",
        "exec_on": args.ssh_host or "local",
        "mode": args.mode,
        "remote_dir": args.remote_dir,
        "history_file": str(history_file),
        "deploy_requested": args.deploy,
        "deployed": False,
        "returncode": None,
        "stdout": [],
        "stderr": [],
        "requested_settings": _requested_settings(args),
        "requested_ramps": _requested_ramps(args),
        "requested_stop_channels": _requested_stop_channels(args),
        "vatt_result": None,
        "success": False,
    }

    try:
        if args.ssh_host:
            with make_client(
                args.ssh_host,
                args.ssh_user,
                args.ssh_password,
                args.ssh_port,
            ) as ssh:
                if args.deploy:
                    logger.info("Deploying vatt_cnt to %s", args.remote_dir)
                    ssh.put_directory(Path(args.local_vatt_dir), _as_remote_path(args.remote_dir))
                    report["deployed"] = True

                remote_cmd = subprocess.list2cmdline(_build_remote_cmd(args))
                logger.info("Running VATT command on %s", args.ssh_host)
                rc, stdout, stderr = ssh.run(remote_cmd, timeout=args.timeout)
        else:
            local_script = Path(args.local_vatt_dir) / "Vaunix_lda802q_control.py"
            cmd = _build_remote_cmd(args)
            cmd[1] = str(local_script)
            logger.info("Running VATT command locally")
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=args.timeout,
            )
            rc, stdout, stderr = proc.returncode, proc.stdout, proc.stderr

        report["returncode"] = rc
        report["stdout"] = stdout.splitlines()
        report["stderr"] = stderr.splitlines()
        report["vatt_result"] = _extract_json_object(stdout)
        report["success"] = rc == 0
        return rc == 0
    except Exception as exc:
        logger.error("VATT control error: %s", exc)
        report["stderr"] = [str(exc)]
        return False
    finally:
        output_file.parent.mkdir(parents=True, exist_ok=True)
        output_file.write_text(
            json.dumps(report, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        _append_jsonl_utf8_sig(history_file, report)
        logger.info("VATT result written to %s", output_file)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="VATT control over SSH")
    parser.add_argument("--mode", required=True,
                        choices=[
                            "status", "set", "set_all", "set_multi", "ramp", "ramp_multi",
                            "stop_ramp", "stop_ramp_multi",
                        ])
    parser.add_argument("--local-vatt-dir", required=True)
    parser.add_argument("--remote-dir", default=r"C:\cicd\vatt_cnt")
    parser.add_argument("--python-path", default="python")
    parser.add_argument("--dll-dir", default=r"C:\cicd\vatt_cnt")
    parser.add_argument("--serial", type=int, default=None)
    parser.add_argument("--channel", type=int, default=None)
    parser.add_argument("--channels", type=int, nargs="*", default=None)
    parser.add_argument("--attenuation-db", type=float, default=None)
    parser.add_argument("--start-db", type=float, default=None)
    parser.add_argument("--stop-db", type=float, default=None)
    parser.add_argument("--step-db", type=float, default=None)
    parser.add_argument("--dwell-ms", type=int, default=None)
    parser.add_argument("--step-db2", type=float, default=None)
    parser.add_argument("--dwell-ms2", type=int, default=None)
    parser.add_argument("--idle-ms", type=int, default=None)
    parser.add_argument("--hold-ms", type=int, default=None)
    parser.add_argument("--ramps-b64", default=None)
    parser.add_argument("--settings-b64", default=None)
    parser.add_argument("--test-mode", action="store_true")
    parser.add_argument("--no-go", action="store_true")
    parser.add_argument("--bidirectional", action="store_true")
    parser.add_argument("--repeat", action="store_true")
    parser.add_argument("--deploy", action="store_true")
    parser.add_argument("--output", required=True)
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument("--ssh-host", default=None)
    parser.add_argument("--ssh-user", default="root")
    parser.add_argument("--ssh-password", default="")
    parser.add_argument("--ssh-port", type=int, default=22)
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)
        logging.getLogger("ssh_client").setLevel(logging.DEBUG)
    return args


def main() -> None:
    success = run_vatt_control(parse_args())
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()

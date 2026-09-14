#!/usr/bin/env python3
"""
ping_test.py
pingを実行して結果をJSONに保存する。

【実行モード】
  --ssh-host なし / --adb-serial なし → コンテナ上でローカル実行
  --ssh-host あり / --adb-serial なし → SSH でリモートホストに接続して ping 実行
  --ssh-host なし / --adb-serial あり → ローカルの adb 経由で端末から ping 実行
  --ssh-host あり / --adb-serial あり → SSH で端末機制御PC に接続し、
                                         そこから adb 経由で端末から ping 実行
"""

import argparse
import json
import logging
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from ssh_client import make_client

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] ping_test - %(message)s",
)
logger = logging.getLogger("ping_test")


# ─── ping 出力パーサ（Linux / Android 共通フォーマット） ─────────
def parse_ping_output(output: str) -> dict:
    result = {
        "packets_transmitted": None,
        "packets_received":    None,
        "packet_loss_percent": None,
        "rtt_min":  None,
        "rtt_avg":  None,
        "rtt_max":  None,
        "rtt_mdev": None,
    }
    stat = re.search(
        r"(\d+) packets transmitted, (\d+) (?:packets )?received"
        r".*?(\d+(?:\.\d+)?)% packet loss",
        output,
    )
    if stat:
        result["packets_transmitted"] = int(stat.group(1))
        result["packets_received"]    = int(stat.group(2))
        result["packet_loss_percent"] = float(stat.group(3))

    # Linux:   rtt min/avg/max/mdev = ...
    # Android: round-trip min/avg/max = ... (mdev なし)
    rtt = re.search(
        r"(?:rtt|round-trip) min/avg/max(?:/mdev)? = "
        r"([\d.]+)/([\d.]+)/([\d.]+)(?:/([\d.]+))? ms",
        output,
    )
    if rtt:
        result["rtt_min"]  = float(rtt.group(1))
        result["rtt_avg"]  = float(rtt.group(2))
        result["rtt_max"]  = float(rtt.group(3))
        result["rtt_mdev"] = float(rtt.group(4)) if rtt.group(4) else None
    return result


# ─── ping コマンド文字列の組み立て ───────────────────────────────
def _build_ping_cmd(target: str, count: int, interval: float, timeout: int) -> str:
    """Linux / Android 共通の ping コマンドを返す"""
    return f"ping -c {count} -i {interval} -W {timeout} {target}"


# ─── ADB 経由の ping 実行 ────────────────────────────────────────
def _run_via_adb(
    ping_cmd: str,
    serial: str,
    adb_path: str,
    run_fn,           # callable(cmd, timeout) → (rc, stdout, stderr)
    run_timeout: int,
) -> tuple[int, str]:
    """
    adb shell ping を実行して (returncode, raw_output) を返す。
    run_fn にはローカル subprocess またはSSH越しの実行関数を渡す。
    """
    adb_parts = [adb_path, "-s", serial, "shell"] if serial else [adb_path, "shell"]
    adb_cmd   = " ".join(adb_parts) + " " + ping_cmd

    # 端末接続確認
    devices_cmd = f"{adb_path} -s {serial} devices" if serial else f"{adb_path} devices"
    rc_dev, out_dev, _ = run_fn(devices_cmd, 10)
    if rc_dev != 0 or "device" not in out_dev:
        raise RuntimeError(
            f"ADB端末が見つかりません (serial={serial or '自動'}): {out_dev.strip()}"
        )

    rc, stdout, stderr = run_fn(adb_cmd, run_timeout)
    return rc, stdout + stderr


# ─── メイン実行関数 ──────────────────────────────────────────────
def run_ping(
    target: str,
    count: int,
    interval: float,
    timeout: int,
    output_file: Path,
    # SSH 引数
    ssh_host: str | None,
    ssh_user: str,
    ssh_password: str,
    ssh_port: int,
    # ADB 引数
    adb_serial: str | None,
    adb_path: str,
) -> bool:
    ping_cmd    = _build_ping_cmd(target, count, interval, timeout)
    run_timeout = int(count * interval + timeout + 30)

    # 実行モードを決定
    use_ssh = bool(ssh_host)
    use_adb = adb_serial is not None   # serial="" も ADB モードとして扱う

    if use_ssh and use_adb:
        exec_mode = "ssh+adb"
        exec_on   = f"{ssh_host} → adb({adb_serial or '自動'})"
    elif use_ssh:
        exec_mode = "ssh"
        exec_on   = ssh_host
    elif use_adb:
        exec_mode = "adb"
        exec_on   = f"local → adb({adb_serial or '自動'})"
    else:
        exec_mode = "local"
        exec_on   = "local"

    logger.info(f"ping実行: [{exec_on}] → {target} (count={count}, mode={exec_mode})")

    try:
        raw = ""
        rc  = 1

        if use_ssh:
            # ── SSH 接続（ADB有無で分岐） ─────────────────────────
            with make_client(ssh_host, ssh_user, ssh_password, ssh_port) as ssh:
                def _ssh_run(cmd, t):
                    return ssh.run(cmd, timeout=t)

                if use_adb:
                    # SSH 経由で ADB → 端末 ping
                    rc, raw = _run_via_adb(
                        ping_cmd, adb_serial, adb_path, _ssh_run, run_timeout
                    )
                else:
                    # SSH 経由で直接 ping
                    rc, stdout, stderr = ssh.run(ping_cmd, timeout=run_timeout)
                    raw = stdout + stderr

        else:
            # ── ローカル実行（ADB有無で分岐） ────────────────────
            def _local_run(cmd, t):
                proc = subprocess.run(
                    cmd, shell=True, capture_output=True, text=True, timeout=t
                )
                return proc.returncode, proc.stdout, proc.stderr

            if use_adb:
                # ローカル ADB → 端末 ping
                rc, raw = _run_via_adb(
                    ping_cmd, adb_serial, adb_path, _local_run, run_timeout
                )
            else:
                # ローカル直接 ping
                rc, stdout, stderr = _local_run(ping_cmd, run_timeout)
                raw = stdout + stderr

        metrics = parse_ping_output(raw)
        report  = {
            "timestamp":  datetime.now().isoformat(),
            "exec_mode":  exec_mode,
            "exec_on":    exec_on,
            "target":     target,
            "returncode": rc,
            "metrics":    metrics,
            "raw_output": raw.splitlines(),
        }
        output_file.parent.mkdir(parents=True, exist_ok=True)
        output_file.write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        logger.info(
            f"  結果: loss={metrics.get('packet_loss_percent')}%, "
            f"rtt_avg={metrics.get('rtt_avg')}ms → {output_file}"
        )
        return rc == 0

    except Exception as e:
        logger.error(f"ping実行エラー: {e}")
        return False


# ─── エントリポイント ────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="ping試験")
    parser.add_argument("--target",       required=True,  help="ping 送信先アドレス")
    parser.add_argument("--count",        type=int,   default=5)
    parser.add_argument("--interval",     type=float, default=1.0)
    parser.add_argument("--timeout",      type=int,   default=5)
    parser.add_argument("--output",       required=True)
    # SSH 引数（省略時はローカル実行）
    parser.add_argument("--ssh-host",     default=None,
                        help="リモート実行ホスト (省略=ローカル)")
    parser.add_argument("--ssh-user",     default="root")
    parser.add_argument("--ssh-password", default="")
    parser.add_argument("--ssh-port",     type=int, default=22)
    # ADB 引数（指定時は端末から ping を実行）
    parser.add_argument("--adb-serial",   default=None,
                        help="ADB デバイスシリアル番号 (指定時=端末から ping 実行)")
    parser.add_argument("--adb-path",     default="adb",
                        help="adb コマンドのパス (デフォルト: adb)")
    parser.add_argument("--debug",        action="store_true",
                        help="デバッグモード: SSH/ADB 応答をリアルタイム表示")
    args = parser.parse_args()

    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)
        logging.getLogger("ssh_client").setLevel(logging.DEBUG)

    success = run_ping(
        target=args.target, count=args.count,
        interval=args.interval, timeout=args.timeout,
        output_file=Path(args.output),
        ssh_host=args.ssh_host, ssh_user=args.ssh_user,
        ssh_password=args.ssh_password, ssh_port=args.ssh_port,
        adb_serial=args.adb_serial,
        adb_path=args.adb_path,
    )
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()

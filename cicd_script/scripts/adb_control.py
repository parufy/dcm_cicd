#!/usr/bin/env python3
"""
adb_control.py
ADB を使って Android 端末の機内モードを制御するスクリプト。

  --ssh-host 指定あり → SSH で端末機制御PCに接続し、そこで adb コマンドを実行
  --ssh-host 指定なし → コンテナ(ローカル)上で adb コマンドを直接実行

【機内モード制御の仕組み】
  Android の機内モードは settings コマンドで DB 値を書き換えた後、
  ブロードキャストで変更を通知することで有効/無効を切り替える。
  - on  : settings put global airplane_mode_on 1
          am broadcast -a android.intent.action.AIRPLANE_MODE --ez state true
  - off : settings put global airplane_mode_on 0
          am broadcast -a android.intent.action.AIRPLANE_MODE --ez state false
  - status のみの場合は settings get global airplane_mode_on の値を返す
"""

import argparse
import json
import logging
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from ssh_client import make_client

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] adb_control - %(message)s",
)
logger = logging.getLogger("adb_control")




# ─── ADB コマンドランナー ────────────────────────────────────────
class AdbRunner:
    """
    adb コマンドをローカルまたはSSH経由で実行するクラス。
    serial が指定された場合は -s <serial> オプションを付加する。
    """

    def __init__(
        self,
        serial: str | None,
        ssh_host: str | None,
        ssh_user: str,
        ssh_password: str,
        ssh_port: int,
        adb_path: str,
    ):
        self.serial      = serial
        self.ssh_host    = ssh_host
        self.ssh_user    = ssh_user
        self.ssh_password = ssh_password
        self.ssh_port    = ssh_port
        self.adb_path    = adb_path
        self._ssh        = None   # SSHClient（接続後にセット）

    def _build_cmd(self, *adb_args: str) -> str:
        """adb コマンド文字列を組み立てる"""
        parts = [self.adb_path]
        if self.serial:
            parts += ["-s", self.serial]
        parts += list(adb_args)
        return " ".join(parts)

    def run(self, *adb_args: str, timeout: int = 30) -> tuple[int, str, str]:
        """
        adb コマンドを実行して (returncode, stdout, stderr) を返す。
        SSH接続済みの場合はリモート実行、そうでなければローカル実行。
        """
        cmd = self._build_cmd(*adb_args)
        logger.debug(f"adb実行: {cmd}")

        if self._ssh is not None:
            return self._ssh.run(cmd, timeout=timeout)
        else:
            proc = subprocess.run(
                cmd, shell=True, capture_output=True, text=True, timeout=timeout
            )
            return proc.returncode, proc.stdout, proc.stderr

    def __enter__(self):
        if self.ssh_host:
            self._ssh = make_client(
                self.ssh_host, self.ssh_user, self.ssh_password, self.ssh_port
            )
            self._ssh.connect()
        return self

    def __exit__(self, *_):
        if self._ssh:
            self._ssh.close()
            self._ssh = None


# ─── 端末接続確認 ────────────────────────────────────────────────
def _check_device(runner: AdbRunner, serial: str | None) -> tuple[bool, str]:
    """
    adb devices で端末が接続・認証済みか確認する。
    Returns: (ok, device_serial_or_error)
    """
    rc, stdout, stderr = runner.run("devices", "-l")
    if rc != 0:
        return False, f"adb devices 失敗: {stderr.strip()}"

    lines = [l for l in stdout.splitlines() if l.strip() and "List of devices" not in l]
    if not lines:
        return False, "接続中の端末が見つかりません"

    # unauthorized / offline チェック
    for line in lines:
        parts = line.split()
        dev_serial = parts[0]
        state      = parts[1] if len(parts) > 1 else ""
        if serial and dev_serial != serial:
            continue
        if state == "unauthorized":
            return False, f"端末 {dev_serial} が未認証です (USBデバッグの許可が必要)"
        if state == "offline":
            return False, f"端末 {dev_serial} がオフラインです"
        if state == "device":
            return True, dev_serial

    if serial:
        return False, f"指定したシリアル '{serial}' の端末が見つかりません"
    return False, "利用可能な端末が見つかりません"


# ─── 機内モード状態取得 ──────────────────────────────────────────
def _get_airplane_mode(runner: AdbRunner) -> str | None:
    """
    現在の機内モード状態を返す。"enabled"=ON, "disabled"=OFF, None=取得失敗
    コマンド: adb shell cmd connectivity airplane-mode
    出力例  : Airplane mode: enabled / Airplane mode: disabled
    """
    rc, stdout, _ = runner.run("shell", "cmd", "connectivity", "airplane-mode")
    if rc != 0:
        return None
    line = stdout.strip().lower()
    if "enabled" in line:
        return "enabled"
    if "disabled" in line:
        return "disabled"
    return None


# ─── 機内モード制御 ──────────────────────────────────────────────
def run_adb_control(
    mode: str,
    serial: str | None,
    wait_after: int,
    output_file: Path,
    adb_path: str,
    ssh_host: str | None,
    ssh_user: str,
    ssh_password: str,
    ssh_port: int,
) -> bool:
    exec_on = ssh_host if ssh_host else "local"
    logger.info(
        f"ADB機内モード制御: [{exec_on}] "
        f"serial={serial or '(自動)'} mode={mode}"
    )

    report = {
        "timestamp":    datetime.now().isoformat(),
        "exec_on":      exec_on,
        "serial":       serial,
        "mode":         mode,
        "before":       None,
        "after":        None,
        "success":      False,
        "message":      "",
    }

    try:
        with AdbRunner(serial, ssh_host, ssh_user, ssh_password, ssh_port, adb_path) as runner:

            # ── 1. 端末接続確認 ───────────────────────────────────
            ok, dev = _check_device(runner, serial)
            if not ok:
                report["message"] = dev
                logger.error(f"  端末確認失敗: {dev}")
                _write_report(report, output_file)
                return False
            logger.info(f"  端末確認OK: {dev}")
            report["serial"] = dev

            # ── 2. 現在の状態取得 ─────────────────────────────────
            current = _get_airplane_mode(runner)
            state_str = {"enabled": "ON", "disabled": "OFF"}.get(current, f"不明({current})")
            logger.info(f"  現在の機内モード: {state_str}")
            report["before"] = state_str

            if mode == "status":
                report["after"]   = state_str
                report["success"] = True
                report["message"] = f"機内モード: {state_str}"
                logger.info(f"  {report['message']}")
                _write_report(report, output_file)
                return True

            # ── 3. 機内モード切り替え ─────────────────────────────
            target_val = "enabled" if mode == "on" else "disabled"
            target_str = "ON" if mode == "on" else "OFF"
            subcmd     = "enable" if mode == "on" else "disable"

            # adb shell cmd connectivity airplane-mode enable/disable
            rc, stdout, stderr = runner.run(
                "shell", "cmd", "connectivity", "airplane-mode", subcmd
            )
            if rc != 0:
                msg = f"airplane-mode {subcmd} 失敗: {stderr.strip()}"
                report["message"] = msg
                logger.error(f"  {msg}")
                _write_report(report, output_file)
                return False

            logger.info(f"  機内モード {target_str} を指示しました")

            # ── 4. 待機（設定反映を待つ） ─────────────────────────
            if wait_after > 0:
                logger.info(f"  反映待機: {wait_after}秒")
                import time
                time.sleep(wait_after)

            # ── 5. 反映確認 ───────────────────────────────────────
            after = _get_airplane_mode(runner)
            after_str = {"enabled": "ON", "disabled": "OFF"}.get(after, f"不明({after})")
            logger.info(f"  変更後の機内モード: {after_str}")
            report["after"] = after_str

            if after == target_val:
                report["success"] = True
                report["message"] = f"機内モードを {target_str} に変更しました"
                logger.info(f"  ✓ {report['message']}")
            else:
                report["message"] = (
                    f"設定変更後も期待値と不一致 "
                    f"(期待: {target_str}, 実際: {after_str})"
                )
                logger.warning(f"  ✗ {report['message']}")

            _write_report(report, output_file)
            return report["success"]

    except Exception as e:
        report["message"] = str(e)
        logger.error(f"ADB制御エラー: {e}")
        _write_report(report, output_file)
        return False


def _write_report(report: dict, output_file: Path) -> None:
    output_file.parent.mkdir(parents=True, exist_ok=True)
    output_file.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )


# ─── エントリポイント ────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="ADB 機内モード制御")
    parser.add_argument("--mode",         required=True, choices=["on", "off", "status"],
                        help="機内モード操作: on / off / status")
    parser.add_argument("--serial",       default=None,
                        help="ADBデバイスシリアル番号 (省略時は接続中の1台を使用)")
    parser.add_argument("--wait-after",   type=int, default=3,
                        help="操作後の待機秒数 (デフォルト: 3)")
    parser.add_argument("--adb-path",     default="adb",
                        help="adb コマンドのパス (デフォルト: adb)")
    parser.add_argument("--output",       required=True,
                        help="結果JSONの出力先ファイルパス")
    # SSH引数（省略時はローカル実行）
    parser.add_argument("--ssh-host",     default=None,
                        help="端末機制御PCのアドレス (省略=ローカル実行)")
    parser.add_argument("--ssh-user",     default="root")
    parser.add_argument("--ssh-password", default="")
    parser.add_argument("--ssh-port",     type=int, default=22)
    parser.add_argument("--debug",        action="store_true",
                        help="デバッグモード: SSH応答をリアルタイム表示")
    args = parser.parse_args()

    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)
        logging.getLogger("ssh_client").setLevel(logging.DEBUG)

    success = run_adb_control(
        mode=args.mode,
        serial=args.serial,
        wait_after=args.wait_after,
        output_file=Path(args.output),
        adb_path=args.adb_path,
        ssh_host=args.ssh_host,
        ssh_user=args.ssh_user,
        ssh_password=args.ssh_password,
        ssh_port=args.ssh_port,
    )
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()

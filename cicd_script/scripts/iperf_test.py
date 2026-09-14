#!/usr/bin/env python3
"""
iperf_test.py
iperf3を実行して結果をJSONに保存する。

【実行モード】
  --ssh-host なし / --adb-serial なし → コンテナ上でローカル実行
  --ssh-host あり / --adb-serial なし → SSH でリモートホストに接続して iperf3 実行
  --ssh-host なし / --adb-serial あり → ローカルの adb 経由で端末から iperf3 実行
  --ssh-host あり / --adb-serial あり → SSH で端末機制御PC に接続し、
                                         そこから adb 経由で端末から iperf3 実行（クライアント側）

【端末間 iperf（端末 → 端末）】
  --server-adb-serial を指定すると、サーバ側端末でも iperf3 -s を起動してから
  クライアント側端末から接続する。
  サーバ側端末の制御PCは --server-ssh-host で指定（省略時は --ssh-host と共用）。

  構成例:
    [端末A（クライアント）] → [端末B（サーバ）]
    制御PC-A  →  adb -s <A> shell iperf3 -c <端末BのIP>
    制御PC-B  →  adb -s <B> shell iperf3 -s -D（バックグラウンド起動）

【ADB モードの前提】
  端末の /data/local/tmp/iperf3 にバイナリが配置されていること。
  バイナリパスは --adb-iperf-path / --server-adb-iperf-path で変更可能。
"""

import argparse
import json
import logging
import subprocess
import sys
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from ssh_client import make_client

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] iperf_test - %(message)s",
)
logger = logging.getLogger("iperf_test")

# 端末上のiperf3バイナリのデフォルトパス
ADB_IPERF3_DEFAULT = "/data/local/tmp/iperf3"


# ─── iperf3 JSON パーサ ───────────────────────────────────────────
def _extract_intervals(data: dict) -> list[dict]:
    """iperf3 JSON の intervals から1秒ごとのスループットを抽出する"""
    result = []
    for interval in data.get("intervals", []):
        s = interval.get("sum", {})
        result.append({
            "start":           round(s.get("start", 0.0), 3),
            "end":             round(s.get("end",   0.0), 3),
            "bits_per_second": s.get("bits_per_second"),
            "bytes":           s.get("bytes"),
            "retransmits":     s.get("retransmits"),   # TCPのみ。UDPはNone
        })
    return result


def parse_iperf_json(raw_json: str) -> tuple[dict, dict]:
    """
    iperf3 JSON出力を解析してクライアント/サーバ両側のメトリクスを返す。
    Returns: (client_metrics, server_metrics)
    """
    empty: dict = {}
    try:
        data = json.loads(raw_json)
        end  = data.get("end", {})

        summary = end.get("sum_received") or end.get("sum", {})
        streams = end.get("streams", [])
        cpu     = end.get("cpu_utilization_percent", {})
        client_metrics = {
            "summary": {
                "bits_per_second":  summary.get("bits_per_second"),
                "bytes":            summary.get("bytes"),
                "seconds":          summary.get("seconds"),
                "retransmits":      summary.get("retransmits"),
                "stream_count":     len(streams),
                "cpu_host_total":   cpu.get("host_total"),
                "cpu_remote_total": cpu.get("remote_total"),
            },
            "intervals": _extract_intervals(data),
        }

        srv_json = data.get("server_output_json")
        if srv_json:
            srv_end     = srv_json.get("end", {})
            srv_summary = srv_end.get("sum_received") or srv_end.get("sum", {})
            srv_streams = srv_end.get("streams", [])
            server_metrics: dict = {
                "summary": {
                    "bits_per_second": srv_summary.get("bits_per_second"),
                    "bytes":           srv_summary.get("bytes"),
                    "seconds":         srv_summary.get("seconds"),
                    "stream_count":    len(srv_streams),
                },
                "intervals": _extract_intervals(srv_json),
            }
        else:
            srv_text = data.get("server_output_text", "")
            server_metrics = {"raw_text": srv_text.splitlines()} if srv_text else {}

        return client_metrics, server_metrics

    except (json.JSONDecodeError, KeyError) as e:
        logger.warning(f"JSON解析失敗: {e}")
        return empty, empty


# ─── iperf3 コマンド組み立て ──────────────────────────────────────
def _build_client_cmd(
    iperf_bin: str,
    server: str, port: int,
    protocol: str, duration: int,
    parallel: int, bandwidth: str | None,
    get_server_output: bool,
    direction: str = "ul",
) -> str:
    parts = [
        iperf_bin, "-c", server, "-p", str(port),
        "-t", str(duration), "-P", str(parallel),
        "-J",
    ]
    if get_server_output:
        parts.append("--get-server-output")
    if direction.lower() == "dl":
        parts.append("-R")
    if protocol.lower() == "udp":
        parts.append("-u")
        if bandwidth:
            parts += ["-b", bandwidth]
    return " ".join(parts)


def _build_server_cmd(iperf_bin: str, port: int) -> str:
    """iperf3 サーバをバックグラウンドで起動するコマンド"""
    # -D: デーモン化（バックグラウンド実行）
    return f"{iperf_bin} -s -p {port} -D"


def _build_server_kill_cmd(port: int) -> str:
    """指定ポートで待ち受けている iperf3 プロセスを停止するコマンド"""
    return f"kill $(lsof -ti tcp:{port} 2>/dev/null) 2>/dev/null || true"


# ─── ADB 関連ヘルパー ────────────────────────────────────────────
def _check_adb_device(serial: str, adb_path: str, run_fn) -> None:
    """接続・認証済みでなければ RuntimeError を送出する"""
    cmd = f"{adb_path} -s {serial} devices" if serial else f"{adb_path} devices"
    rc, out, _ = run_fn(cmd, 10)
    if rc != 0 or "device" not in out:
        raise RuntimeError(
            f"ADB端末が見つかりません (serial={serial or '自動'}): {out.strip()}"
        )
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        dev, state = parts[0], parts[1]
        if serial and dev != serial:
            continue
        if state == "unauthorized":
            raise RuntimeError(f"端末 {dev} が未認証です (USBデバッグの許可が必要)")
        if state == "offline":
            raise RuntimeError(f"端末 {dev} がオフラインです")


def _adb_shell_cmd(adb_path: str, serial: str | None, shell_cmd: str) -> str:
    """adb shell コマンド文字列を組み立てる"""
    if serial:
        return f"{adb_path} -s {serial} shell {shell_cmd}"
    return f"{adb_path} shell {shell_cmd}"


def _make_run_fn(ssh_client=None):
    """SSH クライアントまたはローカル subprocess の run 関数を返す"""
    if ssh_client is not None:
        def _ssh_run(cmd, t):
            return ssh_client.run(cmd, timeout=t)
        return _ssh_run
    else:
        def _local_run(cmd, t):
            proc = subprocess.run(
                cmd, shell=True, capture_output=True, text=True, timeout=t
            )
            return proc.returncode, proc.stdout, proc.stderr
        return _local_run


# ─── サーバ側端末の iperf3 起動・停止 コンテキストマネージャ ─────
@contextmanager
def _adb_iperf_server(
    run_fn,
    adb_path: str,
    server_serial: str | None,
    iperf_bin: str,
    port: int,
    startup_wait: int = 2,
):
    """
    iperf3 サーバをバックグラウンドで起動し、終了時に停止する。
    with ブロックを抜けると自動的に kill する。
    """
    start_cmd = _adb_shell_cmd(
        adb_path, server_serial, _build_server_cmd(iperf_bin, port)
    )
    kill_cmd = _adb_shell_cmd(
        adb_path, server_serial, _build_server_kill_cmd(port)
    )

    logger.info(f"  [サーバ端末] iperf3 サーバ起動: serial={server_serial or '自動'}, port={port}")
    rc, out, err = run_fn(start_cmd, 10)
    if rc != 0:
        raise RuntimeError(
            f"iperf3 サーバ起動失敗 (serial={server_serial or '自動'}): {err.strip()}"
        )

    logger.info(f"  [サーバ端末] 起動待機: {startup_wait}秒")
    time.sleep(startup_wait)

    try:
        yield
    finally:
        logger.info(f"  [サーバ端末] iperf3 サーバ停止")
        run_fn(kill_cmd, 10)


# ─── メイン実行関数 ──────────────────────────────────────────────
def run_iperf(
    server: str, port: int, protocol: str,
    duration: int, parallel: int, bandwidth: str | None,
    output_file: Path,
    direction: str,
    # クライアント側 SSH 引数
    ssh_host: str | None, ssh_user: str, ssh_password: str, ssh_port: int,
    # クライアント側 ADB 引数
    adb_serial: str | None,
    adb_path: str,
    adb_iperf_path: str,
    # サーバ側 ADB 引数（端末間iperf用）
    server_adb_serial: str | None,     # 指定時はこの端末で iperf3 -s を起動
    server_adb_iperf_path: str,        # サーバ端末上のiperf3バイナリパス
    server_ssh_host: str | None,       # サーバ端末の制御PC（省略時はssh_hostと共用）
    server_ssh_user: str,
    server_ssh_password: str,
    server_ssh_port: int,
    server_startup_wait: int,          # サーバ起動後の待機秒数
) -> bool:
    run_timeout      = duration + 60
    use_ssh          = bool(ssh_host)
    use_adb          = adb_serial is not None
    use_server_adb   = server_adb_serial is not None  # 端末間iperf

    # 実行モード文字列
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

    if use_server_adb:
        srv_ctrl = server_ssh_host or ssh_host or "local"
        exec_on += f" → adb-server({server_adb_serial or '自動'} @ {srv_ctrl})"
        exec_mode += "+adb-server"

    dir_label = "DL" if direction.lower() == "dl" else "UL"

    # クライアント側バイナリ・サーバ出力取得フラグ
    if use_adb:
        iperf_bin         = adb_iperf_path
        get_server_output = False
    else:
        iperf_bin         = "iperf3"
        # 端末間iperf ではサーバ出力を --get-server-output で取れないため無効
        get_server_output = not use_server_adb

    logger.info(
        f"iperf3実行: [{exec_on}] → {server}:{port} "
        f"[{protocol.upper()}, {dir_label}, {duration}s, {parallel}stream]"
        + (" [端末間]" if use_server_adb else "")
    )

    client_cmd = _build_client_cmd(
        iperf_bin, server, port, protocol, duration,
        parallel, bandwidth, get_server_output, direction,
    )

    try:
        rc     = 1
        stdout = ""
        stderr = ""

        # ── SSH クライアント接続（クライアント側・サーバ側を確立） ──
        # サーバ側制御PCが別ホストの場合は別接続、共用の場合は同一接続を再利用
        srv_ssh_host = server_ssh_host or ssh_host
        use_srv_ssh  = bool(srv_ssh_host) and use_server_adb
        shared_ssh   = use_srv_ssh and (srv_ssh_host == ssh_host) and use_ssh

        def _execute(cli_run_fn, srv_run_fn=None):
            """
            cli_run_fn: クライアント側コマンド実行関数
            srv_run_fn: サーバ側コマンド実行関数（None の場合は cli_run_fn を流用）
            """
            nonlocal rc, stdout, stderr
            _srv_run = srv_run_fn or cli_run_fn

            # ADB 接続確認
            if use_adb:
                _check_adb_device(adb_serial, adb_path, cli_run_fn)
            if use_server_adb:
                _check_adb_device(server_adb_serial, adb_path, _srv_run)

            # クライアント実行コマンド組み立て
            if use_adb:
                run_cmd = _adb_shell_cmd(adb_path, adb_serial, client_cmd)
            else:
                run_cmd = client_cmd

            if use_server_adb:
                # サーバ端末で iperf3 -s を起動してからクライアント実行
                with _adb_iperf_server(
                    _srv_run, adb_path, server_adb_serial,
                    server_adb_iperf_path, port, server_startup_wait,
                ):
                    rc, stdout, stderr = cli_run_fn(run_cmd, run_timeout)
            else:
                rc, stdout, stderr = cli_run_fn(run_cmd, run_timeout)

        if shared_ssh:
            # クライアント・サーバ制御PCが同一 → SSH接続を1本共用
            with make_client(ssh_host, ssh_user, ssh_password, ssh_port) as ssh:
                _execute(_make_run_fn(ssh), _make_run_fn(ssh))

        elif use_ssh and use_srv_ssh:
            # クライアント・サーバ制御PCが別々 → SSH接続を2本確立
            with make_client(ssh_host, ssh_user, ssh_password, ssh_port) as cli_ssh, \
                 make_client(srv_ssh_host, server_ssh_user, server_ssh_password, server_ssh_port) as srv_ssh:
                _execute(_make_run_fn(cli_ssh), _make_run_fn(srv_ssh))

        elif use_ssh:
            # クライアント側のみSSH
            with make_client(ssh_host, ssh_user, ssh_password, ssh_port) as ssh:
                _execute(_make_run_fn(ssh))

        elif use_srv_ssh:
            # サーバ側のみSSH（クライアントはローカル）
            with make_client(srv_ssh_host, server_ssh_user, server_ssh_password, server_ssh_port) as srv_ssh:
                _execute(_make_run_fn(), _make_run_fn(srv_ssh))

        else:
            # 両側ともローカル
            _execute(_make_run_fn())

        client_metrics, server_metrics = parse_iperf_json(stdout) if rc == 0 else ({}, {})
        bps = (client_metrics.get("summary") or {}).get("bits_per_second")
        logger.info(
            f"  クライアント結果: {f'{bps/1e6:.2f} Mbps' if bps else 'N/A'}"
        )
        if server_metrics and "summary" in server_metrics:
            srv_bps = server_metrics["summary"].get("bits_per_second")
            logger.info(
                f"  サーバ結果: {f'{srv_bps/1e6:.2f} Mbps' if srv_bps else '(テキスト形式)'}"
            )
        elif use_adb or use_server_adb:
            logger.info("  サーバ結果: (ADBモードのため未取得)")
        else:
            logger.warning("  サーバ側ログ未取得 (iperf3サーバが --json 未対応の可能性)")

        report = {
            "timestamp":           datetime.now().isoformat(),
            "exec_mode":           exec_mode,
            "exec_on":             exec_on,
            "server":              server, "port": port,
            "protocol":            protocol, "direction": direction,
            "duration":            duration,
            "parallel":            parallel, "bandwidth_target": bandwidth,
            "server_adb_serial":   server_adb_serial,
            "returncode":          rc,
            "client_metrics":      client_metrics,
            "server_metrics":      server_metrics,
            "raw_output":          stdout.splitlines(),
            "stderr":              stderr.splitlines(),
        }
        output_file.parent.mkdir(parents=True, exist_ok=True)
        output_file.write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return rc == 0

    except Exception as e:
        logger.error(f"iperf3実行エラー: {e}")
        return False


# ─── エントリポイント ────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="iperf3帯域試験")
    parser.add_argument("--server",                required=True,
                        help="iperf3 サーバのアドレス（端末間の場合はサーバ端末のIPアドレス）")
    parser.add_argument("--port",                  type=int, default=5201)
    parser.add_argument("--protocol",              choices=["tcp", "udp"], default="tcp")
    parser.add_argument("--direction",             choices=["ul", "dl"], default="ul",
                        help="通信方向: ul=アップロード(デフォルト) / dl=ダウンロード(-R)")
    parser.add_argument("--duration",              type=int, default=10)
    parser.add_argument("--parallel",              type=int, default=1)
    parser.add_argument("--bandwidth",             default=None)
    parser.add_argument("--output",                required=True)

    # クライアント側 SSH 引数
    parser.add_argument("--ssh-host",              default=None,
                        help="クライアント側の端末機制御PC (省略=ローカル)")
    parser.add_argument("--ssh-user",              default="root")
    parser.add_argument("--ssh-password",          default="")
    parser.add_argument("--ssh-port",              type=int, default=22)

    # クライアント側 ADB 引数
    parser.add_argument("--adb-serial",            default=None,
                        help="クライアント端末のADBシリアル番号 (指定時=端末から iperf3 実行)")
    parser.add_argument("--adb-path",              default="adb",
                        help="adb コマンドのパス (デフォルト: adb)")
    parser.add_argument("--adb-iperf-path",        default=ADB_IPERF3_DEFAULT,
                        help=f"クライアント端末上の iperf3 バイナリパス (デフォルト: {ADB_IPERF3_DEFAULT})")

    # サーバ側 ADB 引数（端末間iperf用）
    parser.add_argument("--server-adb-serial",     default=None,
                        help="サーバ端末のADBシリアル番号 (指定時=この端末でiperf3 -s を起動)")
    parser.add_argument("--server-adb-iperf-path", default=ADB_IPERF3_DEFAULT,
                        help=f"サーバ端末上の iperf3 バイナリパス (デフォルト: {ADB_IPERF3_DEFAULT})")
    parser.add_argument("--server-ssh-host",       default=None,
                        help="サーバ端末の制御PC (省略時は --ssh-host と共用)")
    parser.add_argument("--server-ssh-user",       default="root")
    parser.add_argument("--server-ssh-password",   default="")
    parser.add_argument("--server-ssh-port",       type=int, default=22)
    parser.add_argument("--server-startup-wait",   type=int, default=2,
                        help="サーバ起動後の待機秒数 (デフォルト: 2)")

    parser.add_argument("--debug",                 action="store_true",
                        help="デバッグモード: SSH/ADB 応答をリアルタイム表示")
    args = parser.parse_args()

    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)
        logging.getLogger("ssh_client").setLevel(logging.DEBUG)

    success = run_iperf(
        server=args.server, port=args.port, protocol=args.protocol,
        duration=args.duration, parallel=args.parallel, bandwidth=args.bandwidth,
        output_file=Path(args.output),
        direction=args.direction,
        ssh_host=args.ssh_host, ssh_user=args.ssh_user,
        ssh_password=args.ssh_password, ssh_port=args.ssh_port,
        adb_serial=args.adb_serial,
        adb_path=args.adb_path,
        adb_iperf_path=args.adb_iperf_path,
        server_adb_serial=args.server_adb_serial,
        server_adb_iperf_path=args.server_adb_iperf_path,
        server_ssh_host=args.server_ssh_host,
        server_ssh_user=args.server_ssh_user,
        server_ssh_password=args.server_ssh_password,
        server_ssh_port=args.server_ssh_port,
        server_startup_wait=args.server_startup_wait,
    )
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()

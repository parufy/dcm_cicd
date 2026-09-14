#!/usr/bin/env python3
"""
scenario_executor.py
GitLab CI/CD パイプライン 自動試験実行エンジン

【実行場所の指定】
各ステップの params に host を指定することで実行場所を制御する。

  host 指定なし     → gitlab-runner コンテナ上でローカル実行
  host: host1       → hosts セクションで定義した host1 にSSH接続して実行
  host:             → 複数ホストを列挙した場合、execution に従い並列/逐次実行
    - host1
    - host2

【execution パラメータ】
  execution: sequential  （デフォルト）前のステップ完了後に順番に実行
  execution: parallel    連続する parallel ステップをまとめて同時実行し、
                         全て完了してから次のステップへ進む
"""

import argparse
import base64
import json

from report_generator import generate_html_report
import logging
import os
import re
import subprocess
import sys
import time
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
from itertools import groupby
from pathlib import Path
from typing import Iterator
from uuid import uuid4

import yaml

# ─── ロガー設定 ─────────────────────────────────────────────────
LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s - %(message)s"
logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
logger = logging.getLogger("scenario_executor")


def _enable_debug_logging() -> None:
    """全ロガーのレベルを DEBUG に切り替える"""
    logging.getLogger().setLevel(logging.DEBUG)
    for name in ("scenario_executor", "executor", "ssh_client",
                 "ping_test", "iperf_test", "logcollect", "report_generator"):
        logging.getLogger(name).setLevel(logging.DEBUG)
    logger.debug("デバッグモード有効")

# ローカル実行を示す特殊なホスト名
LOCAL_HOST = "__local__"


# ─── 環境変数展開ヘルパー ────────────────────────────────────────
def _resolve_env(value: str) -> str:
    """
    "${VAR_NAME}" 形式の文字列を環境変数の値に展開する。
    例: "${HOST1_PASSWORD}" → os.environ["HOST1_PASSWORD"]
    """
    match = re.fullmatch(r"\$\{(\w+)\}", value.strip())
    if match:
        env_key = match.group(1)
        resolved = os.environ.get(env_key, "")
        if not resolved:
            logger.warning(f"環境変数 '{env_key}' が未設定です")
        return resolved
    return value


# ─── データクラス ────────────────────────────────────────────────
def _safe_filename(value: str) -> str:
    """Convert a step name to a stable file name fragment."""
    name = re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("._")
    return name or "step"


@dataclass
class HostConfig:
    name: str
    address: str
    user: str = "root"
    password: str = ""
    port: int = 22


@dataclass
class ScenarioStep:
    name: str
    action: str
    params: dict      = field(default_factory=dict)
    execution: str    = "sequential"   # "sequential" | "parallel"
    # params から解決したホストリスト（パーサが設定）
    target_hosts: list[str] = field(default_factory=list)


@dataclass
class PipelineConfig:
    hosts: dict[str, HostConfig]      # name → HostConfig
    scenarios: list[ScenarioStep]
    iperf_server: dict = field(default_factory=dict)
    log_targets: list[str] = field(default_factory=list)


@dataclass
class StepResult:
    host: str          # 実行ホスト名（ローカルの場合は LOCAL_HOST）
    step_name: str
    action: str
    success: bool
    output: str        = ""
    error: str         = ""
    duration: float    = 0.0
    output_file: "Path | None" = None   # ping/iperf の結果JSONパス（サマリ表示用）


# ─── YAMLパーサ ─────────────────────────────────────────────────
class ScenarioParser:
    """YAMLシナリオファイルを解析してPipelineConfigに変換する"""

    VALID_ACTIONS    = {"wait", "ping", "iperf", "logcollect", "adb_control", "vatt_control"}
    VALID_EXECUTIONS = {"sequential", "parallel"}

    def parse(self, yaml_path: str) -> PipelineConfig:
        path = Path(yaml_path)
        if not path.exists():
            raise FileNotFoundError(f"シナリオファイルが見つかりません: {yaml_path}")

        with open(path, encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}

        logger.info(f"シナリオファイル読み込み: {yaml_path}")

        config     = raw.get("config", {}) or {}
        hosts_raw  = self._load_config_section(raw, config, "hosts", path.parent, [])
        defaults   = self._load_config_section(raw, config, "defaults", path.parent, {})
        operations = self._load_config_section(raw, config, "operations", path.parent, {})

        hosts     = self._parse_hosts(hosts_raw)
        scenarios = self._parse_scenarios(
            self._expand_repeats(raw.get("scenarios", [])),
            hosts,
            defaults,
            operations,
        )

        logger.info(f"  定義ホスト数: {len(hosts)}, ステップ数: {len(scenarios)}")
        return PipelineConfig(
            hosts=hosts,
            scenarios=scenarios,
            iperf_server=raw.get("iperf_server", {}),
            log_targets=raw.get("log_targets", []),
        )

    def _load_config_section(
        self,
        raw: dict,
        config: dict,
        section: str,
        base_dir: Path,
        default,
    ):
        if section in raw:
            return raw[section] or default

        config_value = config.get(section)
        if not config_value:
            return default

        config_path = Path(config_value)
        if not config_path.is_absolute():
            config_path = base_dir / config_path
        if not config_path.exists():
            raise FileNotFoundError(f"{section} 設定ファイルが見つかりません: {config_path}")

        with open(config_path, encoding="utf-8") as f:
            loaded = yaml.safe_load(f) or {}

        if section in loaded:
            return loaded[section] or default
        return loaded or default

    def _expand_repeats(self, raw_scenarios: list) -> list[dict]:
        expanded = []
        for i, item in enumerate(raw_scenarios):
            if "repeat" not in item:
                expanded.append(item)
                continue

            count = int(item.get("repeat", 0))
            if count < 1:
                raise ValueError(f"repeat ブロック{i+1}: repeat は 1 以上を指定してください")

            child_steps = item.get("steps")
            if not isinstance(child_steps, list) or not child_steps:
                raise ValueError(f"repeat ブロック{i+1}: steps に1件以上のステップを指定してください")

            child_steps = self._expand_repeats(child_steps)
            for repeat_index in range(1, count + 1):
                for child_index, child in enumerate(child_steps, 1):
                    step = deepcopy(child)
                    self._apply_repeat_context(step, repeat_index, count, child_index)
                    expanded.append(step)
        return expanded

    def _apply_repeat_context(
        self,
        value,
        repeat_index: int,
        repeat_count: int,
        child_index: int,
    ):
        if isinstance(value, dict):
            for key, child_value in value.items():
                value[key] = self._apply_repeat_context(
                    child_value,
                    repeat_index,
                    repeat_count,
                    child_index,
                )
            if "name" in value:
                value["name"] = f"{value['name']} ({repeat_index}/{repeat_count})"
            return value
        if isinstance(value, list):
            return [
                self._apply_repeat_context(v, repeat_index, repeat_count, child_index)
                for v in value
            ]
        if isinstance(value, str):
            replacements = {
                "{repeat}": str(repeat_index),
                "{repeat_index}": str(repeat_index),
                "{repeat_count}": str(repeat_count),
                "{step}": str(child_index),
            }
            for placeholder, replacement in replacements.items():
                value = value.replace(placeholder, replacement)
            return value
        return value

    def _parse_hosts(self, raw_hosts: list) -> dict[str, HostConfig]:
        hosts = {}
        for h in raw_hosts:
            raw_pw   = str(h.get("password", ""))
            password = _resolve_env(raw_pw)
            cfg = HostConfig(
                name=h["name"],
                address=h["address"],
                user=h.get("user", "root"),
                password=password,
                port=int(h.get("port", 22)),
            )
            hosts[cfg.name] = cfg
        return hosts

    def _parse_scenarios(
        self,
        raw_scenarios: list,
        hosts: dict[str, HostConfig],
        defaults: dict | None = None,
        operations: dict | None = None,
    ) -> list[ScenarioStep]:
        defaults = defaults or {}
        operations = operations or {}
        steps = []
        for i, s in enumerate(raw_scenarios):
            step = self._resolve_step(i, s, defaults, operations)
            action = step.get("action", "").lower()
            if action not in self.VALID_ACTIONS:
                raise ValueError(
                    f"ステップ{i+1} '{step.get('name')}': "
                    f"不正なaction '{action}' (有効値: {self.VALID_ACTIONS})"
                )

            execution = step.get("execution", "sequential").lower()
            if execution not in self.VALID_EXECUTIONS:
                raise ValueError(
                    f"ステップ{i+1} '{step.get('name')}': "
                    f"不正なexecution '{execution}' "
                    f"(有効値: {self.VALID_EXECUTIONS})"
                )

            params = step.get("params", {})

            # ── params.host を解決してターゲットホストリストを構築 ──
            raw_host = params.get("host")
            if raw_host is None:
                # host 未指定 → コンテナ(ローカル)実行
                target_hosts = [LOCAL_HOST]
            elif isinstance(raw_host, list):
                # host がリスト → 複数ホスト指定
                target_hosts = raw_host
            else:
                # host が文字列 → 単一ホスト指定
                target_hosts = [str(raw_host)]

            # 定義済みホスト名かチェック（LOCAL_HOST は除外）
            for h in target_hosts:
                if h != LOCAL_HOST and h not in hosts:
                    raise ValueError(
                        f"ステップ{i+1} '{step.get('name')}': "
                        f"hosts に未定義のホスト名 '{h}' が指定されました"
                    )

            steps.append(
                ScenarioStep(
                    name=step.get("name", f"step_{i+1}"),
                    action=action,
                    params=params,
                    execution=execution,
                    target_hosts=target_hosts,
                )
            )
        return steps

    def _resolve_step(
        self,
        index: int,
        raw_step: dict,
        defaults: dict,
        operations: dict,
    ) -> dict:
        use_name = raw_step.get("use")
        operation = {}
        if use_name:
            if use_name not in operations:
                raise ValueError(
                    f"ステップ{index+1} '{raw_step.get('name')}': "
                    f"未定義のoperation '{use_name}' が指定されました"
                )
            operation = operations[use_name] or {}

        action = raw_step.get("action", operation.get("action", ""))
        action = str(action).lower()
        common_defaults = defaults.get("common", {}) or {}
        action_defaults = defaults.get(action, {}) or {}

        params = {}
        params.update(action_defaults)
        params.update(operation.get("params", {}) or {})
        params.update(raw_step.get("params", {}) or {})

        return {
            "name": raw_step.get("name", operation.get("name", f"step_{index+1}")),
            "action": action,
            "execution": raw_step.get(
                "execution",
                operation.get("execution", common_defaults.get("execution", "sequential")),
            ),
            "params": params,
        }


# ─── アクション実行クラス ─────────────────────────────────────────
class ActionExecutor:
    """
    1ステップを特定の実行場所（ホストまたはローカル）で実行するクラス。
    host=None の場合はローカル実行（コンテナ上）。
    """

    SCRIPTS_DIR = Path(__file__).parent / "scripts"

    def __init__(
        self,
        host: HostConfig | None,
        output_dir: Path,
    ):
        self.host       = host
        # ローカル実行の場合は出力ディレクトリを "local" サブディレクトリに
        label           = host.name if host else LOCAL_HOST
        self.output_dir = output_dir / label
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.log        = logging.getLogger(f"executor.{label}")

    @property
    def _label(self) -> str:
        return self.host.name if self.host else "local"

    def dispatch(self, step: ScenarioStep) -> StepResult:
        """actionに対応するハンドラへディスパッチ"""
        where = self._label
        self.log.info(
            f"[{step.action.upper()}] {step.name}  "
            f"(host={where}, execution={step.execution})"
        )
        start = time.time()

        handlers = {
            "wait":        self._action_wait,
            "ping":        self._action_ping,
            "iperf":       self._action_iperf,
            "logcollect":  self._action_logcollect,
            "adb_control": self._action_adb_control,
            "vatt_control": self._action_vatt_control,
        }

        handler = handlers.get(step.action)
        if handler is None:
            return StepResult(
                host=where, step_name=step.name, action=step.action,
                success=False, error=f"未実装のaction: {step.action}",
                duration=time.time() - start,
            )

        result          = handler(step)
        result.duration = time.time() - start
        return result

    # ── SSH引数ヘルパー（ローカル時は空リスト） ──────────────────
    def _ssh_args(self) -> list[str]:
        """
        ホストが指定されている場合はSSH接続引数を返す。
        ローカル実行（host=None）の場合は空リストを返し、
        各スクリプトはSSH引数なしでローカル動作する。
        """
        if self.host is None:
            return []
        args = [
            "--ssh-host",     self.host.address,
            "--ssh-user",     self.host.user,
            "--ssh-password", self.host.password,
            "--ssh-port",     str(self.host.port),
        ]
        # デバッグモード時は子スクリプトにも --debug を伝播
        if logging.getLogger().level <= logging.DEBUG:
            args.append("--debug")
        return args

    # ── wait ────────────────────────────────────────────────────
    def _action_wait(self, step: ScenarioStep) -> StepResult:
        duration = step.params.get("duration", 1)
        self.log.info(f"  待機中: {duration}秒")
        time.sleep(duration)
        return StepResult(
            host=self._label, step_name=step.name, action="wait",
            success=True, output=f"waited {duration}s",
        )

    # ── ping ────────────────────────────────────────────────────
    def _action_ping(self, step: ScenarioStep) -> StepResult:
        p           = step.params
        output_file = self.output_dir / p.get("output_file", "ping_result.txt")
        cmd = [
            sys.executable,
            str(self.SCRIPTS_DIR / "ping_test.py"),
            "--target",   str(p.get("target", "8.8.8.8")),
            "--count",    str(p.get("count", 5)),
            "--interval", str(p.get("interval", 1.0)),
            "--timeout",  str(p.get("timeout", 5)),
            "--output",   str(output_file),
            *self._ssh_args(),
        ]
        # ADB モード: adb_serial が指定されている場合は端末から ping を実行
        if p.get("adb_serial") is not None:
            cmd += ["--adb-serial", str(p["adb_serial"])]
        if p.get("adb_path"):
            cmd += ["--adb-path", str(p["adb_path"])]
        return self._run_script(step, cmd, result_file=output_file)

    # ── iperf ───────────────────────────────────────────────────
    def _action_iperf(self, step: ScenarioStep) -> StepResult:
        p           = step.params
        output_file = self.output_dir / p.get("output_file", "iperf_result.txt")
        cmd = [
            sys.executable,
            str(self.SCRIPTS_DIR / "iperf_test.py"),
            "--server",   str(p.get("server", "127.0.0.1")),
            "--port",     str(p.get("port", 5201)),
            "--protocol", str(p.get("protocol", "tcp")),
            "--duration", str(p.get("duration", 10)),
            "--parallel", str(p.get("parallel", 1)),
            "--output",   str(output_file),
            *self._ssh_args(),
        ]
        if p.get("bandwidth"):
            cmd += ["--bandwidth", str(p["bandwidth"])]
        if p.get("direction"):
            cmd += ["--direction", str(p["direction"])]
        # ADB モード: adb_serial が指定されている場合は端末から iperf3 を実行
        if p.get("adb_serial") is not None:
            cmd += ["--adb-serial", str(p["adb_serial"])]
        if p.get("adb_path"):
            cmd += ["--adb-path", str(p["adb_path"])]
        if p.get("adb_iperf_path"):
            cmd += ["--adb-iperf-path", str(p["adb_iperf_path"])]
        # 端末間 iperf: server_adb_serial が指定されている場合はサーバ端末を起動
        if p.get("server_adb_serial") is not None:
            cmd += ["--server-adb-serial", str(p["server_adb_serial"])]
        if p.get("server_adb_iperf_path"):
            cmd += ["--server-adb-iperf-path", str(p["server_adb_iperf_path"])]
        if p.get("server_startup_wait") is not None:
            cmd += ["--server-startup-wait", str(p["server_startup_wait"])]
        # サーバ端末の制御PCが別ホストの場合
        if p.get("server_ssh_host"):
            srv_host_cfg = self.host  # デフォルトはクライアント側と同じ
            # hosts から解決する場合は scenario_executor 側で対応が必要
            # ここでは YAML に直接アドレスを書く方式をサポート
            cmd += ["--server-ssh-host",     str(p["server_ssh_host"])]
            cmd += ["--server-ssh-user",     str(p.get("server_ssh_user", "root"))]
            cmd += ["--server-ssh-password", str(p.get("server_ssh_password", ""))]
            cmd += ["--server-ssh-port",     str(p.get("server_ssh_port", 22))]
        return self._run_script(step, cmd, result_file=output_file)

    # ── logcollect ──────────────────────────────────────────────
    def _action_logcollect(self, step: ScenarioStep) -> StepResult:
        p          = step.params
        output_dir = self.output_dir / p.get("output_dir", "collected_logs")
        files      = p.get("files", [])
        cmd = [
            sys.executable,
            str(self.SCRIPTS_DIR / "logcollect.py"),
            "--log-dir",    str(p.get("log_dir", "/var/log")),
            "--output-dir", str(output_dir),
            *self._ssh_args(),
        ]
        if files:
            cmd += ["--files"] + files
        if p.get("compress", False):
            cmd += ["--compress"]
        return self._run_script(step, cmd)

    # ── adb_control ─────────────────────────────────────────────
    def _action_adb_control(self, step: ScenarioStep) -> StepResult:
        p           = step.params
        mode        = str(p.get("mode", "status")).lower()
        if mode not in ("on", "off", "status"):
            return StepResult(
                host=self._label, step_name=step.name, action="adb_control",
                success=False,
                error=f"不正なmode '{mode}' (有効値: on / off / status)",
            )
        output_file = self.output_dir / p.get("output_file", f"adb_{mode}_result.txt")
        cmd = [
            sys.executable,
            str(self.SCRIPTS_DIR / "adb_control.py"),
            "--mode",       mode,
            "--wait-after", str(p.get("wait_after", 3)),
            "--adb-path",   str(p.get("adb_path", "adb")),
            "--output",     str(output_file),
            *self._ssh_args(),
        ]
        if p.get("serial"):
            cmd += ["--serial", str(p["serial"])]
        return self._run_script(step, cmd, result_file=output_file)

    # ── vatt_control ───────────────────────────────────────────
    def _action_vatt_control(self, step: ScenarioStep) -> StepResult:
        p = step.params
        mode = str(p.get("mode", "status")).lower()
        if mode not in (
            "status", "set", "set_all", "set_multi", "ramp", "ramp_multi",
            "stop_ramp", "stop_ramp_multi",
        ):
            return StepResult(
                host=self._label, step_name=step.name, action="vatt_control",
                success=False,
                error=(
                    f"Invalid mode '{mode}' "
                    "(valid: status / set / set_all / set_multi / ramp / ramp_multi / "
                    "stop_ramp / stop_ramp_multi)"
                ),
            )

        if mode == "stop_ramp_multi" and not p.get("channels"):
            return StepResult(
                host=self._label, step_name=step.name, action="vatt_control",
                success=False,
                error="stop_ramp_multi requires a non-empty params.channels list",
            )

        if mode == "set_multi":
            settings = p.get("settings")
            if not isinstance(settings, list) or not settings:
                return StepResult(
                    host=self._label, step_name=step.name, action="vatt_control",
                    success=False,
                    error="set_multi requires a non-empty params.settings list",
                )

        default_output = f"vatt_{mode}_{_safe_filename(step.name)}_{uuid4().hex[:8]}.json"
        output_file = self.output_dir / p.get("output_file", default_output)
        local_vatt_dir = Path(__file__).parent / "vatt_cnt"
        remote_dir = p.get("remote_dir", r"C:\cicd\vatt_cnt")
        cmd = [
            sys.executable,
            str(self.SCRIPTS_DIR / "vatt_control.py"),
            "--mode", mode,
            "--local-vatt-dir", str(p.get("local_vatt_dir", local_vatt_dir)),
            "--remote-dir", str(remote_dir),
            "--python-path", str(p.get("python_path", "python")),
            "--dll-dir", str(p.get("dll_dir", remote_dir)),
            "--output", str(output_file),
            "--timeout", str(p.get("timeout", 600)),
            *self._ssh_args(),
        ]
        if p.get("deploy", True):
            cmd.append("--deploy")
        if p.get("test_mode", False):
            cmd.append("--test-mode")
        if p.get("no_go", False):
            cmd.append("--no-go")
        if p.get("bidirectional", False):
            cmd.append("--bidirectional")
        if p.get("repeat", False):
            cmd.append("--repeat")
        if mode == "ramp_multi":
            ramps = p.get("ramps")
            if not isinstance(ramps, list) or not ramps:
                return StepResult(
                    host=self._label, step_name=step.name, action="vatt_control",
                    success=False,
                    error="ramp_multi requires a non-empty params.ramps list",
                )
            ramps_json = json.dumps(ramps, ensure_ascii=False, separators=(",", ":"))
            ramps_b64 = base64.urlsafe_b64encode(ramps_json.encode("utf-8")).decode("ascii")
            cmd += ["--ramps-b64", ramps_b64]
        if mode == "set_multi":
            settings_json = json.dumps(
                p["settings"], ensure_ascii=False, separators=(",", ":")
            )
            settings_b64 = base64.urlsafe_b64encode(
                settings_json.encode("utf-8")
            ).decode("ascii")
            cmd += ["--settings-b64", settings_b64]

        optional_args = [
            ("serial", "--serial"),
            ("channel", "--channel"),
            ("attenuation_db", "--attenuation-db"),
            ("start_db", "--start-db"),
            ("stop_db", "--stop-db"),
            ("step_db", "--step-db"),
            ("dwell_ms", "--dwell-ms"),
            ("step_db2", "--step-db2"),
            ("dwell_ms2", "--dwell-ms2"),
            ("idle_ms", "--idle-ms"),
            ("hold_ms", "--hold-ms"),
        ]
        for key, flag in optional_args:
            if p.get(key) is not None:
                cmd += [flag, str(p[key])]
        if p.get("channels"):
            cmd += ["--channels"] + [str(ch) for ch in p["channels"]]
        result = self._run_script(step, cmd, result_file=output_file)
        self._log_vatt_console_summary(output_file)
        return result

    def _log_vatt_console_summary(self, result_file: Path) -> None:
        summary = _load_vatt_summary(result_file)
        if summary:
            self.log.info(f"  VATT: {summary}")

    # ── 共通スクリプト実行ヘルパー ──────────────────────────────
    def _run_script(self, step: ScenarioStep, cmd: list[str], result_file: "Path | None" = None) -> StepResult:
        masked = [
            "***" if cmd[i - 1] == "--ssh-password" else v
            for i, v in enumerate(cmd)
        ]
        self.log.debug(f"  コマンド: {' '.join(masked)}")
        try:
            proc    = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
            success = proc.returncode == 0
            if not success:
                self.log.warning(f"  終了コード: {proc.returncode}")
            if proc.stdout.strip():
                for line in proc.stdout.splitlines():
                    self.log.debug(f"  [stdout] {line}")
            if proc.stderr.strip():
                for line in proc.stderr.splitlines():
                    self.log.debug(f"  [stderr] {line}")
            return StepResult(
                host=self._label, step_name=step.name, action=step.action,
                success=success, output=proc.stdout, error=proc.stderr,
                output_file=result_file,
            )
        except subprocess.TimeoutExpired:
            return StepResult(
                host=self._label, step_name=step.name, action=step.action,
                success=False, error="タイムアウト (600s超過)",
            )
        except Exception as e:
            return StepResult(
                host=self._label, step_name=step.name, action=step.action,
                success=False, error=str(e),
            )


# ─── 1ステップを対象ホスト群に対して実行 ────────────────────────
def run_step_on_hosts(
    step: ScenarioStep,
    hosts: dict[str, HostConfig],
    output_dir: Path,
) -> list[StepResult]:
    """
    ステップの target_hosts と execution に従いアクションを実行する。

    target_hosts が1件 → そのまま実行
    target_hosts が複数 かつ execution=parallel  → スレッドで同時実行
    target_hosts が複数 かつ execution=sequential → 順番に実行
    """
    def _run_on(host_name: str) -> StepResult:
        host_cfg = None if host_name == LOCAL_HOST else hosts[host_name]
        executor = ActionExecutor(host_cfg, output_dir)
        return executor.dispatch(step)

    if len(step.target_hosts) == 1:
        return [_run_on(step.target_hosts[0])]

    if step.execution == "parallel":
        results: list[StepResult] = [None] * len(step.target_hosts)  # type: ignore
        futures: dict = {}
        with ThreadPoolExecutor(max_workers=len(step.target_hosts)) as pool:
            for idx, h in enumerate(step.target_hosts):
                futures[pool.submit(_run_on, h)] = idx
            for fut in as_completed(futures):
                results[futures[fut]] = fut.result()
        return results
    else:
        return [_run_on(h) for h in step.target_hosts]


# ─── シナリオ全体の実行 ─────────────────────────────────────────
def run_scenarios(
    scenarios: list[ScenarioStep],
    hosts: dict[str, HostConfig],
    output_dir: Path,
) -> list[StepResult]:
    """
    シナリオを先頭から順番に実行する。
    各ステップ内での並列/逐次は run_step_on_hosts が制御する。

    execution=parallel のステップが連続する場合は、
    そのグループ全体を同時に投入して全完了を待つ。
    """
    all_results: list[StepResult] = []

    logger.info(f"=== シナリオ実行開始 ({len(scenarios)} ステップ) ===")

    for execution, group in _group_steps(scenarios):

        if execution == "sequential":
            # ── 逐次実行 ──────────────────────────────────────
            for step in group:
                results = run_step_on_hosts(step, hosts, output_dir)
                for r in results:
                    all_results.append(r)
                    _log_step_result(r)

        else:
            # ── parallel グループ: ステップ単位で並列投入 ────
            step_names = ", ".join(s.name for s in group)
            logger.info(
                f"  並列グループ実行開始 ({len(group)} ステップ): {step_names}"
            )
            # 各ステップを並列に投入（各ステップ内でもhostが複数なら並列実行）
            group_results: dict[int, list[StepResult]] = {}
            futures: dict = {}
            with ThreadPoolExecutor(max_workers=len(group)) as pool:
                for idx, step in enumerate(group):
                    fut = pool.submit(run_step_on_hosts, step, hosts, output_dir)
                    futures[fut] = idx
                for fut in as_completed(futures):
                    group_results[futures[fut]] = fut.result()

            for idx in sorted(group_results):
                for r in group_results[idx]:
                    all_results.append(r)
                    _log_step_result(r)

            logger.info("  並列グループ実行完了")

    logger.info("=== シナリオ実行完了 ===")
    return all_results


# ─── ステップグループのイテレータ ────────────────────────────────
def _group_steps(
    steps: list[ScenarioStep],
) -> Iterator[tuple[str, list[ScenarioStep]]]:
    """連続する同一 execution のステップをグループ化して返す"""
    for execution, group in groupby(steps, key=lambda s: s.execution):
        yield execution, list(group)


def _log_step_result(result: StepResult) -> None:
    status = "✓ OK" if result.success else "✗ FAIL"
    logger.info(
        f"  {status} [{result.host}] '{result.step_name}' ({result.duration:.1f}s)"
    )
    if not result.success:
        logger.error(f"    エラー: {result.error}")


# ─── 結果JSONからサマリを読み込む ────────────────────────────────
def _load_ping_summary(result_file: Path) -> str | None:
    """ping結果JSONからサマリ1行を生成する"""
    try:
        with open(result_file, encoding="utf-8") as f:
            data = json.load(f)
        m = data.get("metrics", {})
        loss    = m.get("packet_loss_percent")
        rtt_avg = m.get("rtt_avg")
        rtt_min = m.get("rtt_min")
        rtt_max = m.get("rtt_max")
        tx      = m.get("packets_transmitted")
        rx      = m.get("packets_received")
        return (
            f"packets: {tx}送信/{rx}受信  "
            f"loss: {loss}%  "
            f"rtt(min/avg/max): {rtt_min}/{rtt_avg}/{rtt_max} ms"
        )
    except Exception:
        return None


def _load_iperf_summary(result_file: Path) -> str | None:
    """iperf結果JSONからクライアント/サーバのサマリ1行を生成する"""
    try:
        with open(result_file, encoding="utf-8") as f:
            data = json.load(f)
        protocol = data.get("protocol", "").upper()

        def _mbps(bps):
            return f"{bps/1e6:.1f} Mbps" if bps else "N/A"

        cli  = (data.get("client_metrics") or {}).get("summary", {})
        srv  = (data.get("server_metrics") or {}).get("summary", {})
        cli_bps  = cli.get("bits_per_second")
        srv_bps  = srv.get("bits_per_second")
        retrans  = cli.get("retransmits")
        seconds  = cli.get("seconds")

        parts = [
            f"[{protocol}]",
            f"client: {_mbps(cli_bps)}",
            f"server: {_mbps(srv_bps)}" if srv_bps else "server: (未取得)",
        ]
        if retrans is not None:
            parts.append(f"retransmits: {retrans}")
        if seconds:
            parts.append(f"duration: {seconds:.1f}s")
        return "  ".join(parts)
    except Exception:
        return None


# ─── レポート出力 ─────────────────────────────────────────────
def _load_vatt_summary(result_file: Path) -> str | None:
    """Load a compact VATT summary for GitLab console output."""
    try:
        # Older VATT result files were written as UTF-8 with BOM.  utf-8-sig
        # accepts both those files and the BOM-less UTF-8 files written now.
        with open(result_file, encoding="utf-8-sig") as f:
            data = json.load(f)

        mode = data.get("mode", "unknown")
        rc = data.get("returncode")
        deploy_requested = data.get("deploy_requested")
        deployed = data.get("deployed")
        vatt_result = data.get("vatt_result") or {}
        message = vatt_result.get("message") or ""
        settings = vatt_result.get("settings") or data.get("requested_settings")
        set_calls = vatt_result.get("set_calls")
        ramps = vatt_result.get("ramps") or data.get("requested_ramps")
        start_groups = vatt_result.get("start_groups") or []
        stopped_channels = (
            vatt_result.get("stopped_channels")
            or data.get("requested_stop_channels")
        )
        stop_chmask = vatt_result.get("stop_chmask")
        values = vatt_result.get("values")

        parts = []
        if settings:
            setting_parts = []
            for ch, value in sorted(
                settings.items(),
                key=lambda item: (str(item[0]).lower() == "all", int(item[0]) if str(item[0]).isdigit() else 0),
            ):
                channel_label = "ALL CHANNELS" if str(ch).lower() == "all" else f"CH{ch}"
                setting_parts.append(f"{channel_label}={float(value):.2f} dB")
            call_text = f"  set_calls={set_calls}" if set_calls is not None else ""
            parts.append("ATT SETTING: " + " | ".join(setting_parts) + call_text)
        if ramps:
            ramp_parts = []
            for spec in sorted(ramps, key=lambda item: int(item["channel"])):
                ramp_parts.append(
                    f"CH{spec['channel']}={float(spec['start_db']):.2f}"
                    f"->{float(spec['stop_db']):.2f} dB"
                    f" step={float(spec.get('step_db', 0.5)):.2f} dB"
                    f" dwell={int(spec.get('dwell_ms', 50))} ms"
                )
            start_call_count = len(start_groups) if start_groups else "N/A"
            parts.append(
                "RAMP CONFIG: " + " | ".join(ramp_parts)
                + f"  start_calls={start_call_count}"
            )
        if stopped_channels:
            stopped = " | ".join(
                f"CH{channel}" for channel in sorted(int(ch) for ch in stopped_channels)
            )
            mask_text = f"0x{int(stop_chmask):X}" if stop_chmask is not None else "N/A"
            parts.append(f"RAMP STOP: {stopped}  chmask={mask_text}")
        if values:
            value_parts = []
            for ch, value in sorted(values.items(), key=lambda item: int(item[0])):
                value_parts.append(f"CH{ch}={float(value):.2f} dB")
            parts.append("ATT READBACK: " + " | ".join(value_parts))

        metadata = [f"mode={mode}"]
        if deploy_requested is not None:
            metadata.append(f"deploy_requested={deploy_requested}")
        if deployed is not None:
            metadata.append(f"deployed={deployed}")
        if rc is not None:
            metadata.append(f"returncode={rc}")
        if not values and not settings and not ramps and not stopped_channels and message:
            metadata.append(f"message={message}")
        parts.append("  ".join(metadata))

        if not data.get("success", False):
            stderr = data.get("stderr") or []
            if stderr:
                parts.append("error=" + " / ".join(str(line) for line in stderr[-3:]))
        return "  ".join(parts)
    except Exception:
        return None


def print_report(results: list[StepResult]) -> bool:
    print("\n" + "=" * 70)
    print("  試験結果サマリ")
    print("=" * 70)

    ok = sum(1 for r in results if r.success)
    ng = len(results) - ok

    from itertools import groupby as _gb
    for step_name, group in _gb(results, key=lambda r: r.step_name):
        group = list(group)
        print(f"\n  ステップ: {step_name}")
        for r in group:
            mark       = "✓" if r.success else "✗"
            host_label = "local" if r.host == LOCAL_HOST else r.host
            print(f"    {mark} [{host_label:<14}] {r.action:<12} ({r.duration:.1f}s)")

            # ── ping サマリ ──────────────────────────────────
            if r.action == "ping" and r.output_file and r.output_file.exists():
                summary = _load_ping_summary(r.output_file)
                if summary:
                    print(f"         {summary}")

            # ── iperf サマリ ─────────────────────────────────
            elif r.action == "iperf" and r.output_file and r.output_file.exists():
                summary = _load_iperf_summary(r.output_file)
                if summary:
                    print(f"         {summary}")
                # 1秒ごとのスループット一覧
                try:
                    with open(r.output_file, encoding="utf-8") as f:
                        data = json.load(f)
                    intervals = (data.get("client_metrics") or {}).get("intervals", [])
                    if intervals:
                        print(f"         {'秒':>6}  {'Client (Mbps)':>14}  {'Server (Mbps)':>14}  {'retransmits':>11}")
                        print(f"         {'-'*6}  {'-'*14}  {'-'*14}  {'-'*11}")
                        srv_intervals = (data.get("server_metrics") or {}).get("intervals", [])
                        srv_map = {
                            round(iv["start"], 3): iv.get("bits_per_second")
                            for iv in srv_intervals
                        }
                        for iv in intervals:
                            cli_bps  = iv.get("bits_per_second")
                            srv_bps  = srv_map.get(round(iv["start"], 3))
                            retrans  = iv.get("retransmits")
                            cli_str  = f"{cli_bps/1e6:>14.1f}" if cli_bps else f"{'N/A':>14}"
                            srv_str  = f"{srv_bps/1e6:>14.1f}" if srv_bps else f"{'N/A':>14}"
                            ret_str  = f"{retrans:>11}" if retrans is not None else f"{'N/A':>11}"
                            t_str    = f"{iv['start']:>3.0f} - {iv['end']:<3.0f}"
                            print(f"         {t_str:>6}  {cli_str}  {srv_str}  {ret_str}")
                except Exception:
                    pass

            elif r.action == "vatt_control" and r.output_file and r.output_file.exists():
                summary = _load_vatt_summary(r.output_file)
                if summary:
                    print(f"         {summary}")

            if not r.success:
                print(f"         → エラー: {r.error}")

    print("\n" + "-" * 70)
    print(f"  合計: {len(results)} 件 / 成功: {ok} / 失敗: {ng}")
    print("=" * 70)
    return ng == 0


# ─── メイン ──────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(
        description="GitLab Pipeline ネットワーク自動試験ランナー"
    )
    parser.add_argument("--scenario",    "-s", required=True,
                        help="試験シナリオYAMLファイルパス")
    parser.add_argument("--output-dir",  "-o", default="test_results",
                        help="結果出力ディレクトリ (デフォルト: test_results)")
    parser.add_argument("--max-workers", "-w", type=int, default=10,
                        help="並列実行の最大スレッド数 (デフォルト: 10)")
    parser.add_argument("--dry-run",     action="store_true",
                        help="実際には実行せずシナリオの解析のみ行う")
    parser.add_argument("--debug",       action="store_true",
                        help="デバッグモード: リモート実行のコマンドライン応答を表示する")
    args = parser.parse_args()

    if args.debug:
        _enable_debug_logging()

    ts         = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_dir = Path(args.output_dir) / ts
    output_dir.mkdir(parents=True, exist_ok=True)

    try:
        cfg = ScenarioParser().parse(args.scenario)
    except (FileNotFoundError, ValueError, yaml.YAMLError) as e:
        logger.error(f"シナリオ解析エラー: {e}")
        sys.exit(1)

    if args.dry_run:
        logger.info("[DRY-RUN] シナリオ解析成功。実行はスキップします。")
        for i, step in enumerate(cfg.scenarios, 1):
            hosts_label = (
                "local" if step.target_hosts == [LOCAL_HOST]
                else ", ".join(step.target_hosts)
            )
            logger.info(
                f"  Step {i:02d}: [{step.action}] [{step.execution}] "
                f"host=[{hosts_label}]  {step.name}"
            )
        sys.exit(0)

    results = run_scenarios(cfg.scenarios, cfg.hosts, output_dir)
    success = print_report(results)
    html_path = generate_html_report(cfg, results, output_dir, args.scenario, LOCAL_HOST)
    print(f"\nHTMLレポート: {html_path}")
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()

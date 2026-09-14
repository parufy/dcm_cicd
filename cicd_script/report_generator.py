#!/usr/bin/env python3
"""
report_generator.py
試験シナリオ内容と結果サマリを HTML ファイルに出力するモジュール。
pipeline_runner.py から呼び出される。
"""

import json
import logging
from datetime import datetime
from itertools import groupby
from pathlib import Path

logger = logging.getLogger("report_generator")

# pipeline_runner から渡される型（循環import回避のため文字列アノテーションで参照）
# 実行時は pipeline_runner.PipelineConfig / StepResult を受け取る


def _h(s: object) -> str:
    """HTML エスケープ"""
    return (
        str(s)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _mbps(bps: float | None) -> str:
    return f"{bps / 1e6:.1f}" if bps else "N/A"


# ─── シナリオテーブル ─────────────────────────────────────────
def _build_scenario_rows(cfg, local_host_sentinel: str) -> str:
    rows = []
    for i, step in enumerate(cfg.scenarios, 1):
        hosts_label = (
            "local"
            if step.target_hosts == [local_host_sentinel]
            else ", ".join(step.target_hosts)
        )
        params_str = "<br>".join(
            f"{_h(k)}: {_h(v)}"
            for k, v in step.params.items()
            if k != "host"
        )
        rows.append(f"""
        <tr>
          <td class="num">{i}</td>
          <td>{_h(step.name)}</td>
          <td><span class="badge action-{_h(step.action)}">{_h(step.action)}</span></td>
          <td><span class="badge exec-{_h(step.execution)}">{_h(step.execution)}</span></td>
          <td class="host">{_h(hosts_label)}</td>
          <td class="params">{params_str}</td>
        </tr>""")
    return "".join(rows)


# ─── adb_control 詳細 HTML ──────────────────────────────────────
def _adb_detail(result_file: Path) -> str:
    try:
        d = json.loads(result_file.read_text(encoding="utf-8"))
        before  = _h(d.get("before", ""))
        after   = _h(d.get("after", ""))
        mode    = _h(d.get("mode", ""))
        serial  = _h(d.get("serial") or "自動")
        msg     = _h(d.get("message", ""))
        ok      = d.get("success", False)
        row_cls = "pass" if ok else "fail"
        return f"""
        <div class="detail">
          <table class="inner">
            <tr><th>操作</th><th>対象端末</th><th>変更前</th><th>変更後</th></tr>
            <tr class="{row_cls}">
              <td>{mode}</td>
              <td class="host">{serial}</td>
              <td>{before}</td>
              <td>{after}</td>
            </tr>
          </table>
          {"" if ok else f'<div class="error-msg">{msg}</div>'}
        </div>"""
    except Exception:
        return ""


# ─── ping 詳細 HTML ───────────────────────────────────────────
def _ping_detail(result_file: Path) -> str:
    try:
        d         = json.loads(result_file.read_text(encoding="utf-8"))
        m         = d.get("metrics", {})
        exec_mode = d.get("exec_mode", "")
        exec_on   = d.get("exec_on", "")
        # exec_mode バッジ色 (local / ssh / adb / ssh+adb)
        mode_colors = {
            "local":   ("e2e8f0", "475569"),
            "ssh":     ("dbeafe", "1e40af"),
            "adb":     ("dcfce7", "166534"),
            "ssh+adb": ("fde68a", "92400e"),
        }
        bg, fg     = mode_colors.get(exec_mode, ("f3f4f6", "374151"))
        mode_badge = (
            f'<span style="background:#{bg};color:#{fg};padding:1px 7px;'
            f'border-radius:8px;font-size:11px;font-weight:600;">'
            f'{_h(exec_mode)}</span>'
            f'<span style="font-size:11px;color:#666;margin-left:6px;">'
            f'{_h(exec_on)}</span>'
        ) if exec_mode else ""
        return f"""
        <div class="detail">
          <table class="inner">
            {"<tr><th colspan='6'>" + mode_badge + "</th></tr>" if mode_badge else ""}
            <tr><th>送信</th><th>受信</th><th>loss</th>
                <th>RTT min</th><th>RTT avg</th><th>RTT max</th></tr>
            <tr>
              <td>{_h(m.get('packets_transmitted', ''))}</td>
              <td>{_h(m.get('packets_received', ''))}</td>
              <td>{_h(m.get('packet_loss_percent', ''))} %</td>
              <td>{_h(m.get('rtt_min', ''))} ms</td>
              <td>{_h(m.get('rtt_avg', ''))} ms</td>
              <td>{_h(m.get('rtt_max', ''))} ms</td>
            </tr>
          </table>
        </div>"""
    except Exception:
        return ""


# ─── iperf 詳細 HTML ──────────────────────────────────────────
def _iperf_detail(result_file: Path) -> str:
    try:
        d         = json.loads(result_file.read_text(encoding="utf-8"))
        proto     = _h(d.get("protocol", "").upper())
        direction = _h(d.get("direction", "ul").upper())
        exec_mode = d.get("exec_mode", "")
        exec_on   = d.get("exec_on", "")
        cli_sum   = (d.get("client_metrics") or {}).get("summary", {})
        srv_sum   = (d.get("server_metrics") or {}).get("summary", {})
        intervals = (d.get("client_metrics") or {}).get("intervals", [])
        srv_ivs   = (d.get("server_metrics") or {}).get("intervals", [])
        srv_map   = {round(iv["start"], 3): iv.get("bits_per_second") for iv in srv_ivs}

        # exec_mode バッジ（ping_detail と同じ色定義）
        mode_colors = {
            "local":   ("e2e8f0", "475569"),
            "ssh":     ("dbeafe", "1e40af"),
            "adb":     ("dcfce7", "166534"),
            "ssh+adb": ("fde68a", "92400e"),
        }
        bg, fg     = mode_colors.get(exec_mode, ("f3f4f6", "374151"))
        mode_badge = (
            f'<span style="background:#{bg};color:#{fg};padding:1px 7px;'
            f'border-radius:8px;font-size:11px;font-weight:600;">'
            f'{_h(exec_mode)}</span>'
            f'<span style="font-size:11px;color:#666;margin-left:6px;">'
            f'{_h(exec_on)}</span>'
        ) if exec_mode else ""

        # ADB モードではサーバ側出力が取得できないことを注記
        is_adb     = "adb" in exec_mode
        srv_note   = ' <span style="font-size:10px;color:#999;">(ADBモードのため未取得)</span>' if is_adb else ""
        srv_mbps   = _mbps(srv_sum.get('bits_per_second')) + " Mbps" if srv_sum.get('bits_per_second') else f"N/A{srv_note}"

        iv_rows = ""
        for iv in intervals:
            s_bps = srv_map.get(round(iv["start"], 3))
            ret   = iv.get("retransmits")
            iv_rows += f"""<tr>
              <td class="num">{iv['start']:.0f} - {iv['end']:.0f}</td>
              <td class="num">{_mbps(iv.get('bits_per_second'))}</td>
              <td class="num">{_mbps(s_bps) if s_bps else ("-" if is_adb else "N/A")}</td>
              <td class="num">{"N/A" if ret is None else ret}</td>
            </tr>"""

        return f"""
        <div class="detail">
          <table class="inner">
            {"<tr><th colspan='3'>" + mode_badge + "</th></tr>" if mode_badge else ""}
            <tr><th>項目</th><th>Client</th><th>Server</th></tr>
            <tr><td>Protocol / 方向</td><td colspan="2">{proto} / {direction}</td></tr>
            <tr><td>Throughput</td>
                <td>{_mbps(cli_sum.get('bits_per_second'))} Mbps</td>
                <td>{srv_mbps}</td></tr>
            <tr><td>Duration</td>
                <td>{cli_sum.get('seconds', 'N/A')} s</td>
                <td>{srv_sum.get('seconds', 'N/A') if not is_adb else '-'} s</td></tr>
            <tr><td>Retransmits</td><td>{cli_sum.get('retransmits', 'N/A')}</td><td>-</td></tr>
            <tr><td>Streams</td><td colspan="2">{cli_sum.get('stream_count', 'N/A')}</td></tr>
          </table>
          <div class="iv-title">1秒ごとのスループット</div>
          <table class="inner iv-table">
            <tr><th>秒</th><th>Client (Mbps)</th><th>Server (Mbps)</th><th>Retransmits</th></tr>
            {iv_rows}
          </table>
        </div>"""
    except Exception:
        return ""


# ─── 結果テーブル ─────────────────────────────────────────────
def _vatt_detail(result_file: Path) -> str:
    try:
        # utf-8-sig reads both legacy BOM-prefixed results and current UTF-8.
        d = json.loads(result_file.read_text(encoding="utf-8-sig"))
        vatt = d.get("vatt_result") or {}
        settings = vatt.get("settings") or d.get("requested_settings") or {}
        set_calls = vatt.get("set_calls")
        ramps = vatt.get("ramps") or d.get("requested_ramps") or []
        start_groups = vatt.get("start_groups") or []
        stopped_channels = vatt.get("stopped_channels") or d.get("requested_stop_channels") or []
        stop_chmask = vatt.get("stop_chmask")
        values = vatt.get("values") or {}
        mode = _h(d.get("mode", ""))
        exec_mode = _h(d.get("exec_mode", ""))
        exec_on = _h(d.get("exec_on", ""))
        deploy_requested = _h(d.get("deploy_requested", ""))
        deployed = _h(d.get("deployed", ""))
        rc = _h(d.get("returncode", ""))
        msg = _h(vatt.get("message") or "")
        ok = d.get("success", False)
        row_cls = "pass" if ok else "fail"

        if settings:
            setting_chips = "".join(
                f'<span class="att-setting-value">'
                f'{"ALL CHANNELS" if str(ch).lower() == "all" else "CH" + _h(ch)}'
                f'&nbsp; {float(value):.2f} dB</span>'
                for ch, value in sorted(
                    settings.items(),
                    key=lambda item: (str(item[0]).lower() == "all", int(item[0]) if str(item[0]).isdigit() else 0),
                )
            )
            setting_html = f"""
          <div class="att-setting">
            <div class="att-setting-title">ATT設定値</div>
            <div class="att-values">{setting_chips}</div>
            <div class="att-setting-info">設定API呼び出し回数: {set_calls if set_calls is not None else 'N/A'}</div>
          </div>"""
        else:
            setting_html = ""

        if ramps:
            ramp_rows = "".join(
                f"<tr><td>CH{_h(spec.get('channel'))}</td>"
                f"<td class=\"num\">{float(spec.get('start_db')):.2f} dB</td>"
                f"<td class=\"num\">{float(spec.get('stop_db')):.2f} dB</td>"
                f"<td class=\"num\">{float(spec.get('step_db', 0.5)):.2f} dB</td>"
                f"<td class=\"num\">{int(spec.get('dwell_ms', 50))} ms</td>"
                f"<td>{'Yes' if spec.get('repeat', False) else 'No'}</td>"
                f"<td>{'Yes' if spec.get('bidirectional', False) else 'No'}</td></tr>"
                for spec in sorted(ramps, key=lambda item: int(item["channel"]))
            )
            ramp_html = f"""
          <div class="ramp-config">
            <div class="ramp-title">チャンネル別Ramp設定</div>
            <table class="inner ramp-table">
              <tr><th>Channel</th><th>Start</th><th>Stop</th><th>Step</th>
                  <th>Dwell</th><th>Repeat</th><th>Bidirectional</th></tr>
              {ramp_rows}
            </table>
            <div class="ramp-start-info">一括開始API呼び出し回数: {len(start_groups) if start_groups else 'N/A'}</div>
          </div>"""
        else:
            ramp_html = ""

        if stopped_channels:
            stop_chips = "".join(
                f'<span class="ramp-stop-value">CH{int(channel)}</span>'
                for channel in sorted(int(ch) for ch in stopped_channels)
            )
            mask_text = f"0x{int(stop_chmask):X}" if stop_chmask is not None else "N/A"
            stop_html = f"""
          <div class="ramp-stop">
            <div class="ramp-stop-title">Ramp停止対象</div>
            <div class="att-values">{stop_chips}</div>
            <div class="ramp-stop-info">Channel mask: {mask_text}</div>
          </div>"""
        else:
            stop_html = ""

        if values:
            value_chips = "".join(
                f'<span class="att-value">CH{_h(ch)}&nbsp; {float(value):.2f} dB</span>'
                for ch, value in sorted(values.items(), key=lambda item: int(item[0]))
            )
            readback_html = f"""
          <div class="att-readback">
            <div class="att-title">ATT読み出し値</div>
            <div class="att-values">{value_chips}</div>
          </div>"""
        elif d.get("mode") == "status":
            readback_html = """
          <div class="att-readback">
            <div class="att-title">ATT読み出し値</div>
            <div class="att-values"><span class="att-value unavailable">N/A</span></div>
          </div>"""
        else:
            readback_html = ""

        stderr = d.get("stderr") or []
        err_html = ""
        if not ok and stderr:
            err_html = f'<div class="error-msg">{_h(" / ".join(str(line) for line in stderr[-3:]))}</div>'

        return f"""
        <div class="detail">
          <table class="inner">
            <tr><th>Mode</th><th>Exec</th><th>Deploy requested</th><th>Deployed</th><th>Return code</th><th>Message</th></tr>
            <tr class="{row_cls}">
              <td>{mode}</td>
              <td>{exec_mode} {_h(exec_on)}</td>
              <td>{deploy_requested}</td>
              <td>{deployed}</td>
              <td class="num">{rc}</td>
              <td>{msg}</td>
            </tr>
          </table>
          {setting_html}
          {ramp_html}
          {stop_html}
          {readback_html}
          {err_html}
        </div>"""
    except Exception:
        return ""


def _build_result_rows(results: list, local_host_sentinel: str) -> str:
    rows = []
    for step_name, group in groupby(results, key=lambda r: r.step_name):
        group = list(group)
        for idx, r in enumerate(group):
            host_label  = "local" if r.host == local_host_sentinel else r.host
            status_cls  = "pass" if r.success else "fail"
            status_mark = "✓ OK" if r.success else "✗ FAIL"

            # 詳細 HTML
            detail_html = ""
            if r.action == "ping" and r.output_file and r.output_file.exists():
                detail_html = _ping_detail(r.output_file)
            elif r.action == "iperf" and r.output_file and r.output_file.exists():
                detail_html = _iperf_detail(r.output_file)
            elif r.action == "adb_control" and r.output_file and r.output_file.exists():
                detail_html = _adb_detail(r.output_file)
            elif r.action == "vatt_control" and r.output_file and r.output_file.exists():
                detail_html = _vatt_detail(r.output_file)

            error_html = (
                f'<div class="error-msg">エラー: {_h(r.error)}</div>'
                if not r.success else ""
            )

            # ステップ名セルは先頭行のみ rowspan で表示
            name_cell = (
                f'<td rowspan="{len(group)}" class="step-name">{_h(step_name)}</td>'
                if idx == 0 else ""
            )

            rows.append(f"""
        <tr class="result-row {status_cls}">
          {name_cell}
          <td><span class="badge action-{_h(r.action)}">{_h(r.action)}</span></td>
          <td class="host">{_h(host_label)}</td>
          <td class="num">{r.duration:.1f} s</td>
          <td class="{status_cls}-badge">{status_mark}</td>
          <td>{detail_html}{error_html}</td>
        </tr>""")
    return "".join(rows)


# ─── CSS ─────────────────────────────────────────────────────
_CSS = """
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
    body { font-family: "Segoe UI", "Helvetica Neue", Arial, sans-serif;
           font-size: 13px; background: #f4f6f9; color: #333; }
    header { background: #1e2a3a; color: #fff; padding: 18px 28px; }
    header h1 { font-size: 20px; font-weight: 600; }
    header .meta { margin-top: 6px; font-size: 12px; color: #a0b0c0; }
    .overall { display: inline-block; margin-left: 16px; padding: 2px 12px;
               border-radius: 4px; font-weight: 700; font-size: 13px; }
    .overall.pass { background: #27ae60; color: #fff; }
    .overall.fail { background: #e74c3c; color: #fff; }
    .container { max-width: 1200px; margin: 20px auto; padding: 0 16px; }
    .card { background: #fff; border-radius: 8px;
            box-shadow: 0 1px 4px rgba(0,0,0,.1); margin-bottom: 24px; overflow: hidden; }
    .card-header { background: #2c3e50; color: #fff; padding: 10px 18px;
                   font-size: 14px; font-weight: 600; }
    .summary-grid { display: flex; gap: 16px; padding: 16px 18px; flex-wrap: wrap; }
    .summary-box { flex: 1; min-width: 120px; background: #f8f9fa; border-radius: 6px;
                   padding: 12px 16px; text-align: center; border: 1px solid #e0e0e0; }
    .summary-box .val { font-size: 28px; font-weight: 700; }
    .summary-box .lbl { font-size: 11px; color: #666; margin-top: 4px; }
    .val.ok    { color: #27ae60; }
    .val.fail  { color: #e74c3c; }
    .val.total { color: #2980b9; }
    table { width: 100%; border-collapse: collapse; }
    th, td { padding: 7px 12px; border: 1px solid #e0e4ea;
             text-align: left; vertical-align: top; }
    th { background: #ecf0f4; font-weight: 600; font-size: 12px; }
    tr:hover td { background: #f7f9fc; }
    .num  { text-align: right; font-variant-numeric: tabular-nums; }
    .host { font-family: monospace; font-size: 12px; }
    .params { font-size: 11px; color: #555; line-height: 1.6; }
    .step-name { font-weight: 600; white-space: nowrap; background: #fafbfc; }
    .badge { display: inline-block; padding: 2px 8px; border-radius: 10px;
             font-size: 11px; font-weight: 600; white-space: nowrap; }
    .action-ping       { background: #dbeafe; color: #1e40af; }
    .action-iperf      { background: #dcfce7; color: #166534; }
    .action-adb_control { background: #fce7f3; color: #9d174d; }
    .action-vatt_control { background: #e0f2fe; color: #075985; }
    .action-logcollect { background: #fef9c3; color: #854d0e; }
    .action-wait       { background: #f3e8ff; color: #6b21a8; }
    .exec-sequential   { background: #e2e8f0; color: #475569; }
    .exec-parallel     { background: #fde68a; color: #92400e; }
    .pass td { background: #f0fdf4; }
    .fail td { background: #fff5f5; }
    .pass-badge { color: #16a34a; font-weight: 700; white-space: nowrap; }
    .fail-badge { color: #dc2626; font-weight: 700; white-space: nowrap; }
    .detail { margin-top: 4px; }
    .inner { width: auto; min-width: 400px; font-size: 12px; border-color: #d0d7de; }
    .inner th { background: #f1f5f9; }
    .iv-title { margin: 8px 0 4px; font-size: 11px; font-weight: 600; color: #475569; }
    .iv-table { max-height: 220px; display: block; overflow-y: auto; }
    .att-setting { margin-top: 8px; padding: 8px 10px; border-left: 3px solid #2563eb;
                   background: #eff6ff; }
    .att-setting-title { margin-bottom: 6px; font-size: 11px; font-weight: 600; color: #1e3a8a; }
    .att-setting-value { display: inline-block; padding: 3px 8px; border-radius: 10px;
                         background: #dbeafe; color: #1e40af; font-weight: 600;
                         font-variant-numeric: tabular-nums; white-space: nowrap; }
    .att-setting-info { margin-top: 5px; font-size: 11px; color: #1e40af; }
    .ramp-config { margin-top: 8px; padding: 8px 10px; border-left: 3px solid #7c3aed;
                   background: #f5f3ff; }
    .ramp-title { margin-bottom: 6px; font-size: 11px; font-weight: 600; color: #5b21b6; }
    .ramp-table { min-width: 650px; }
    .ramp-start-info { margin-top: 5px; font-size: 11px; color: #5b21b6; }
    .ramp-stop { margin-top: 8px; padding: 8px 10px; border-left: 3px solid #dc2626;
                 background: #fef2f2; }
    .ramp-stop-title { margin-bottom: 6px; font-size: 11px; font-weight: 600; color: #991b1b; }
    .ramp-stop-value { display: inline-block; padding: 3px 8px; border-radius: 10px;
                       background: #fee2e2; color: #991b1b; font-weight: 600; }
    .ramp-stop-info { margin-top: 5px; font-size: 11px; color: #991b1b; }
    .att-readback { margin-top: 8px; padding: 8px 10px; border-left: 3px solid #f97316;
                    background: #fff7ed; }
    .att-title { margin-bottom: 6px; font-size: 11px; font-weight: 600; color: #7c2d12; }
    .att-values { display: flex; flex-wrap: wrap; gap: 6px; }
    .att-value { display: inline-block; padding: 3px 8px; border-radius: 10px;
                 background: #ffedd5; color: #9a3412; font-weight: 600;
                 font-variant-numeric: tabular-nums; white-space: nowrap; }
    .att-value.unavailable { background: #f1f5f9; color: #64748b; }
    .error-msg { color: #dc2626; font-size: 12px; margin-top: 4px; }
"""


# ─── メイン公開関数 ───────────────────────────────────────────
def generate_html_report(
    cfg,
    results: list,
    output_dir: Path,
    scenario_path: str,
    local_host_sentinel: str = "__local__",
) -> Path:
    """
    試験シナリオ内容と結果サマリを HTML ファイルに出力する。

    Args:
        cfg:                  PipelineConfig（pipeline_runner から渡す）
        results:              list[StepResult]
        output_dir:           出力先ディレクトリ
        scenario_path:        シナリオYAMLのパス文字列（表示用）
        local_host_sentinel:  ローカル実行を示す特殊ホスト名
    Returns:
        生成した HTML ファイルの Path
    """
    ts_str  = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    ok      = sum(1 for r in results if r.success)
    ng      = len(results) - ok
    overall = "PASS" if ng == 0 else "FAIL"
    ov_cls  = "pass" if ng == 0 else "fail"

    scenario_rows = _build_scenario_rows(cfg, local_host_sentinel)
    result_rows   = _build_result_rows(results, local_host_sentinel)

    html = f"""<!DOCTYPE html>
<html lang="ja">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>試験レポート - {_h(ts_str)}</title>
  <style>{_CSS}</style>
</head>
<body>
<header>
  <h1>ネットワーク試験レポート
    <span class="overall {ov_cls}">{overall}</span>
  </h1>
  <div class="meta">
    生成日時: {_h(ts_str)} ／ シナリオ: {_h(scenario_path)}
  </div>
</header>

<div class="container">

  <div class="card">
    <div class="card-header">試験サマリ</div>
    <div class="summary-grid">
      <div class="summary-box">
        <div class="val total">{len(results)}</div><div class="lbl">総ステップ数</div>
      </div>
      <div class="summary-box">
        <div class="val ok">{ok}</div><div class="lbl">成功</div>
      </div>
      <div class="summary-box">
        <div class="val fail">{ng}</div><div class="lbl">失敗</div>
      </div>
      <div class="summary-box">
        <div class="val total">{len(cfg.hosts)}</div><div class="lbl">定義ホスト数</div>
      </div>
    </div>
  </div>

  <div class="card">
    <div class="card-header">試験シナリオ</div>
    <table>
      <thead>
        <tr>
          <th>#</th><th>ステップ名</th><th>Action</th>
          <th>Execution</th><th>Host</th><th>パラメータ</th>
        </tr>
      </thead>
      <tbody>{scenario_rows}</tbody>
    </table>
  </div>

  <div class="card">
    <div class="card-header">結果サマリ</div>
    <table>
      <thead>
        <tr>
          <th>ステップ名</th><th>Action</th><th>Host</th>
          <th>時間</th><th>結果</th><th>詳細</th>
        </tr>
      </thead>
      <tbody>{result_rows}</tbody>
    </table>
  </div>

</div>
</body>
</html>"""

    report_path = output_dir / "report.html"
    report_path.write_text(html, encoding="utf-8")
    logger.info(f"HTMLレポート出力: {report_path}")
    return report_path

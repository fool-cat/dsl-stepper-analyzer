#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Windows-friendly browser workspace for dsl_stepper_speed.py.

Features:
- Double-click or run directly: starts a local web UI in the browser
- Drag .dsl files onto the launcher/exe: files are preloaded into the UI
- No manual smoothing input
- Multiple .dsl files can be loaded and viewed together on one comparison page
"""

from __future__ import annotations

import dataclasses
import email.policy
import html
import pathlib
import socket
import sys
import tempfile
import threading
import time
import traceback
import urllib.parse
import webbrowser
from email.parser import BytesParser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Dict, List, Optional


SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from dsl_stepper_speed import STEP_EDGE_FALLING, STEP_EDGE_RISING, analyze_and_write, read_dsl_meta  # noqa: E402


INACTIVITY_TIMEOUT_S = 15 * 60
DIR_PROBE_NONE = -1
DIR_POSITIVE_OPTIONS = [
    ("high", "高电平为正方向"),
    ("low", "低电平为正方向"),
]
STEP_EDGE_OPTIONS = [
    (STEP_EDGE_RISING, "上升沿计步"),
    (STEP_EDGE_FALLING, "下降沿计步"),
]


def _dir_positive_text(dir_positive_level: str) -> str:
    return "高电平为正方向" if dir_positive_level == "high" else "低电平为正方向"


def _dir_summary(dir_probe: int, dir_positive_level: str) -> str:
    if dir_probe < 0:
        return "未使用DIR（默认正向）"
    return f"CH{dir_probe} ({_dir_positive_text(dir_positive_level)})"


def _step_edge_text(step_edge: str) -> str:
    return "上升沿计步" if step_edge == STEP_EDGE_RISING else "下降沿计步"


def _is_dsl(p: pathlib.Path) -> bool:
    return p.suffix.lower() == ".dsl"


def _normalize_files(argv: List[str]) -> List[pathlib.Path]:
    out: List[pathlib.Path] = []
    for a in argv:
        try:
            p = pathlib.Path(a.strip('"')).expanduser()
        except Exception:
            continue
        if _is_dsl(p):
            out.append(p)
    return out


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@dataclasses.dataclass
class SignalGroup:
    group_id: str
    label: str
    dir_probe: int
    step_probe: int
    dir_positive_level: str = "high"
    step_edge: str = STEP_EDGE_RISING
    report_html: Optional[pathlib.Path] = None
    error: str = ""


@dataclasses.dataclass
class CaptureFile:
    file_id: str
    name: str
    dsl_path: pathlib.Path
    total_probes: int
    groups: List[SignalGroup]
    source_kind: str = "path"


class WorkspaceState:
    def __init__(self) -> None:
        self._tempdir = tempfile.TemporaryDirectory(prefix="dsl_stepper_speed_")
        self.temp_root = pathlib.Path(self._tempdir.name)
        self._lock = threading.Lock()
        self._counter = 0
        self.files: Dict[str, CaptureFile] = {}
        self.last_touch = time.time()
        self.last_error = ""

    def _next_id(self, prefix: str) -> str:
        self._counter += 1
        return f"{prefix}{self._counter:04d}"

    def _default_group(self, total_probes: int, label: str = "电机 1") -> SignalGroup:
        return SignalGroup(
            group_id=self._next_id("grp"),
            label=label,
            dir_probe=0 if total_probes > 1 else DIR_PROBE_NONE,
            step_probe=1 if total_probes > 1 else 0,
            dir_positive_level="high",
            step_edge=STEP_EDGE_RISING,
        )

    def cleanup(self) -> None:
        self._tempdir.cleanup()

    def touch(self) -> None:
        with self._lock:
            self.last_touch = time.time()

    def add_capture_from_path(self, src_path: pathlib.Path) -> CaptureFile:
        meta = read_dsl_meta(src_path)
        with self._lock:
            file_id = self._next_id("file")
            item = CaptureFile(
                file_id=file_id,
                name=src_path.name,
                dsl_path=src_path,
                total_probes=meta.total_probes,
                groups=[self._default_group(meta.total_probes)],
                source_kind="path",
            )
            self.files[file_id] = item
            self.last_touch = time.time()
            return item

    def add_capture_from_upload(self, filename: str, data: bytes) -> CaptureFile:
        safe_name = pathlib.Path(filename or "capture.dsl").name
        if not safe_name.lower().endswith(".dsl"):
            safe_name += ".dsl"
        with self._lock:
            file_id = self._next_id("file")
            dst_path = self.temp_root / f"{file_id}_{safe_name}"
            dst_path.write_bytes(data)
        meta = read_dsl_meta(dst_path)
        with self._lock:
            item = CaptureFile(
                file_id=file_id,
                name=safe_name,
                dsl_path=dst_path,
                total_probes=meta.total_probes,
                groups=[self._default_group(meta.total_probes)],
                source_kind="upload",
            )
            self.files[file_id] = item
            self.last_touch = time.time()
            return item

    def list_files(self) -> List[CaptureFile]:
        with self._lock:
            return list(self.files.values())

    def remove_file(self, file_id: str) -> None:
        with self._lock:
            item = self.files.pop(file_id, None)
            self.last_touch = time.time()
        if not item:
            return
        for group in item.groups:
            if group.report_html and group.report_html.exists():
                try:
                    group.report_html.unlink()
                except OSError:
                    pass
        if item.source_kind == "upload" and item.dsl_path.exists():
            try:
                item.dsl_path.unlink()
            except OSError:
                pass

    def add_group(self, file_id: str) -> None:
        with self._lock:
            item = self.files[file_id]
            label = f"电机 {len(item.groups) + 1}"
            item.groups.append(self._default_group(item.total_probes, label))
            self.last_touch = time.time()

    def remove_group(self, file_id: str, group_id: str) -> None:
        with self._lock:
            item = self.files[file_id]
            if len(item.groups) <= 1:
                return
            group = next((g for g in item.groups if g.group_id == group_id), None)
            if not group:
                return
            item.groups = [g for g in item.groups if g.group_id != group_id]
            self.last_touch = time.time()
        if group.report_html and group.report_html.exists():
            try:
                group.report_html.unlink()
            except OSError:
                pass

    def update_group_config(
        self,
        file_id: str,
        group_id: str,
        *,
        label: str,
        dir_probe: int,
        step_probe: int,
        dir_positive_level: str,
        step_edge: str,
    ) -> None:
        with self._lock:
            item = self.files[file_id]
            group = next(g for g in item.groups if g.group_id == group_id)
            group.label = label or group.label
            group.dir_probe = dir_probe
            group.step_probe = step_probe
            group.dir_positive_level = dir_positive_level or group.dir_positive_level
            group.step_edge = step_edge or group.step_edge
            group.error = ""
            self.last_touch = time.time()

    def analyze_group(self, file_id: str, group_id: str) -> SignalGroup:
        with self._lock:
            item = self.files[file_id]
            group = next(g for g in item.groups if g.group_id == group_id)
            group.error = ""

        if group.dir_probe >= 0 and group.dir_probe == group.step_probe:
            raise ValueError(f"{item.name} / {group.label}: DIR 和 STEP 不能是同一个通道。")

        if item.source_kind == "path":
            out_html = item.dsl_path.with_suffix("")
            safe_group = "".join(ch if ch.isalnum() else "_" for ch in group.label).strip("_") or group.group_id
            out_html = out_html.with_name(out_html.name + f"_{safe_group}_speed.html")
        else:
            base = self.temp_root / f"{file_id}_{group.group_id}_{pathlib.Path(item.name).stem}"
            out_html = base.with_name(base.name + "_speed.html")

        result = analyze_and_write(
            dsl_path=item.dsl_path,
            dir_probe=group.dir_probe,
            step_probe=group.step_probe,
            dir_high_positive=(group.dir_positive_level != "low"),
            step_edge=group.step_edge,
            out_html=out_html,
            out_csv=None,
        )

        with self._lock:
            group.report_html = result.out_html
            group.error = ""
            self.last_touch = time.time()
            return group


class AppHandler(BaseHTTPRequestHandler):
    server_version = "DslStepperSpeedWeb/1.0"

    @property
    def app(self) -> "AppServer":
        return self.server  # type: ignore[return-value]

    def log_message(self, format: str, *args) -> None:
        return

    def do_GET(self) -> None:
        self.app.state.touch()
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/":
            self._send_html(self.app.render_index())
            return
        if parsed.path.startswith("/report/"):
            report_id = pathlib.Path(parsed.path).stem
            for item in self.app.state.list_files():
                for group in item.groups:
                    if group.group_id == report_id and group.report_html and group.report_html.exists():
                        self._send_file(group.report_html, "text/html; charset=utf-8")
                        return
            self.send_error(HTTPStatus.NOT_FOUND, "Report not found")
            return
        self.send_error(HTTPStatus.NOT_FOUND, "Not found")

    def do_POST(self) -> None:
        self.app.state.touch()
        parsed = urllib.parse.urlparse(self.path)
        try:
            if parsed.path == "/upload":
                self._handle_upload()
                return
            if parsed.path == "/analyze":
                self._handle_analyze()
                return
            if parsed.path == "/remove-file":
                self._handle_remove_file()
                return
            if parsed.path == "/add-group":
                self._handle_add_group()
                return
            if parsed.path == "/remove-group":
                self._handle_remove_group()
                return
            if parsed.path == "/shutdown":
                self._handle_shutdown()
                return
        except Exception:
            self.app.state.last_error = traceback.format_exc()
        self._redirect("/")

    def _handle_upload(self) -> None:
        files = self._read_multipart_files()
        for filename, raw in files:
            try:
                self.app.state.add_capture_from_upload(filename, raw)
            except Exception:
                self.app.state.last_error = traceback.format_exc()
        self._redirect("/")

    def _handle_analyze(self) -> None:
        params = self._read_form_urlencoded()
        for item in self.app.state.list_files():
            for group in item.groups:
                label_value = params.get(f"label_{group.group_id}", [group.label])[0].strip()
                dir_value = params.get(f"dir_{group.group_id}", [str(group.dir_probe)])[0]
                step_value = params.get(f"step_{group.group_id}", [str(group.step_probe)])[0]
                dir_level_value = params.get(f"dir_level_{group.group_id}", [group.dir_positive_level])[0]
                step_edge_value = params.get(f"step_edge_{group.group_id}", [group.step_edge])[0]
                self.app.state.update_group_config(
                    item.file_id,
                    group.group_id,
                    label=label_value or group.label,
                    dir_probe=int(dir_value),
                    step_probe=int(step_value),
                    dir_positive_level=dir_level_value,
                    step_edge=step_edge_value,
                )

        self.app.state.last_error = ""
        for item in self.app.state.list_files():
            for group in item.groups:
                try:
                    self.app.state.analyze_group(item.file_id, group.group_id)
                except Exception:
                    group.error = traceback.format_exc()
                    self.app.state.last_error = group.error
        self._redirect("/")

    def _handle_remove_file(self) -> None:
        params = self._read_form_urlencoded()
        file_id = params.get("file_id", [""])[0]
        if file_id:
            self.app.state.remove_file(file_id)
        self._redirect("/")

    def _handle_add_group(self) -> None:
        params = self._read_form_urlencoded()
        file_id = params.get("file_id", [""])[0]
        if file_id:
            self.app.state.add_group(file_id)
        self._redirect("/")

    def _handle_remove_group(self) -> None:
        params = self._read_form_urlencoded()
        group_key = params.get("group_key", [""])[0]
        file_id, _, group_id = group_key.partition("::")
        if file_id and group_id:
            self.app.state.remove_group(file_id, group_id)
        self._redirect("/")

    def _handle_shutdown(self) -> None:
        self._send_html(
            """
            <!doctype html>
            <html lang="zh-CN"><meta charset="utf-8"><title>已关闭</title>
            <body style="font-family:sans-serif;padding:24px;background:#101722;color:#e7eefc">
            <h2>程序正在退出</h2>
            <p>这个页面现在可以关闭了。</p>
            </body></html>
            """
        )
        threading.Thread(target=self.app.shutdown_soon, daemon=True).start()

    def _read_form_urlencoded(self) -> dict:
        length = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(length).decode("utf-8", "replace")
        return urllib.parse.parse_qs(raw, keep_blank_values=True)

    def _read_multipart_files(self) -> List[tuple[str, bytes]]:
        content_type = self.headers.get("Content-Type", "")
        content_length = int(self.headers.get("Content-Length", "0") or "0")
        body = self.rfile.read(content_length)
        if "multipart/form-data" not in content_type:
            return []

        header_block = (
            f"Content-Type: {content_type}\r\n"
            "MIME-Version: 1.0\r\n"
            "\r\n"
        ).encode("utf-8")
        message = BytesParser(policy=email.policy.default).parsebytes(header_block + body)

        files: List[tuple[str, bytes]] = []
        for part in message.iter_parts():
            filename = part.get_filename()
            if not filename:
                continue
            payload = part.get_payload(decode=True)
            if payload is None:
                continue
            files.append((filename, payload))
        return files

    def _redirect(self, location: str) -> None:
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", location)
        self.end_headers()

    def _send_html(self, body: str, status: int = 200) -> None:
        encoded = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _send_file(self, path: pathlib.Path, content_type: str) -> None:
        data = path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class AppServer(ThreadingHTTPServer):
    def __init__(self, addr: tuple[str, int], state: WorkspaceState) -> None:
        super().__init__(addr, AppHandler)
        self.state = state

    def shutdown_soon(self) -> None:
        time.sleep(0.3)
        self.shutdown()

    def render_index(self) -> str:
        captures = self.state.list_files()
        error_html = ""
        if self.state.last_error:
            error_html = (
                '<div class="error"><strong>最近一次错误</strong><pre>'
                + html.escape(self.state.last_error)
                + "</pre></div>"
            )

        file_cards = []
        for item in captures:
            group_rows = []
            for group in item.groups:
                dir_options = "".join(
                    f'<option value="{DIR_PROBE_NONE}"{" selected" if group.dir_probe < 0 else ""}>未使用DIR（默认正向）</option>'
                    + "".join(
                        f'<option value="{i}"{" selected" if i == group.dir_probe else ""}>CH{i}</option>'
                        for i in range(item.total_probes)
                    )
                )
                step_options = "".join(
                    f'<option value="{i}"{" selected" if i == group.step_probe else ""}>CH{i}</option>'
                    for i in range(item.total_probes)
                )
                dir_level_options = "".join(
                    f'<option value="{value}"{" selected" if value == group.dir_positive_level else ""}>{label}</option>'
                    for value, label in DIR_POSITIVE_OPTIONS
                )
                step_edge_options = "".join(
                    f'<option value="{value}"{" selected" if value == group.step_edge else ""}>{label}</option>'
                    for value, label in STEP_EDGE_OPTIONS
                )
                report_actions = ""
                if group.report_html and group.report_html.exists():
                    report_actions = f'<a class="mini" href="/report/{group.group_id}.html" target="_blank">打开 HTML</a>'
                error_note = f'<div class="row-error"><pre>{html.escape(group.error)}</pre></div>' if group.error else ""
                report_actions = report_actions or '<span class="mini placeholder">打开 HTML</span>'
                remove_group_btn = (
                    f'<button class="mini danger" type="submit" formaction="/remove-group" formmethod="post" '
                    f'name="group_key" value="{item.file_id}::{group.group_id}">删组</button>'
                )
                if len(item.groups) <= 1:
                    remove_group_btn = '<span class="mini ghost">至少保留 1 组</span>'
                dir_level_attrs = f'data-role="dir-level" data-group="{group.group_id}"'
                group_rows.append(
                    f"""
                    <div class="group-row" data-file="{item.file_id}" data-group="{group.group_id}">
                      <div class="group-name">
                        <label>电机名</label>
                        <input type="text" name="label_{group.group_id}" value="{html.escape(group.label)}" />
                      </div>
                      <div>
                        <label>DIR</label>
                        <select data-role="dir" data-file="{item.file_id}" data-group="{group.group_id}" name="dir_{group.group_id}">{dir_options}</select>
                      </div>
                      <div>
                        <label>STEP</label>
                        <select data-role="step" data-file="{item.file_id}" data-group="{group.group_id}" name="step_{group.group_id}">{step_options}</select>
                      </div>
                      <div>
                        <label>DIR 极性</label>
                        <select {dir_level_attrs} name="dir_level_{group.group_id}">{dir_level_options}</select>
                      </div>
                      <div>
                        <label>STEP 边沿</label>
                        <select name="step_edge_{group.group_id}">{step_edge_options}</select>
                      </div>
                      <div class="group-actions">
                        {report_actions}
                        {remove_group_btn}
                      </div>
                      {error_note}
                    </div>
                    """
                )
            file_cards.append(
                f"""
                <section class="file-card">
                  <div class="file-head">
                    <div>
                      <div class="fname">{html.escape(item.name)}</div>
                      <div class="sub">{html.escape(str(item.dsl_path))}</div>
                    </div>
                    <div class="file-head-actions">
                      <span class="pill">通道数 {item.total_probes}</span>
                      <button class="mini" type="submit" formaction="/add-group" formmethod="post" name="file_id" value="{item.file_id}">新增信号组</button>
                      <button class="mini danger" type="submit" formaction="/remove-file" formmethod="post" name="file_id" value="{item.file_id}">移除文件</button>
                    </div>
                  </div>
                  <div class="group-list">
                    {''.join(group_rows)}
                  </div>
                </section>
                """
            )

        reports = []
        for item in captures:
            for group in item.groups:
                if not group.report_html or not group.report_html.exists():
                    continue
                reports.append(
                    f"""
                    <section class="report-card">
                      <div class="report-head">
                        <div>
                          <h3>{html.escape(item.name)} / {html.escape(group.label)}</h3>
                          <div class="sub">DIR={_dir_summary(group.dir_probe, group.dir_positive_level)}, STEP=CH{group.step_probe} ({_step_edge_text(group.step_edge)})</div>
                        </div>
                        <div class="report-links">
                          <a href="/report/{group.group_id}.html" target="_blank">单独打开</a>
                        </div>
                      </div>
                      <iframe src="/report/{group.group_id}.html" loading="lazy"></iframe>
                    </section>
                    """
                )

        files_html = "\n".join(file_cards) if file_cards else '<div class="empty-card">还没有载入任何 DSL 文件。</div>'
        reports_html = "\n".join(reports) if reports else '<div class="empty-card">生成后，这里会显示多个 DSL 报告，方便直接对比查看。</div>'

        return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>DSL Stepper Compare</title>
  <style>
    :root {{
      --bg0: #07111d;
      --bg1: #0f1c2e;
      --card: rgba(255,255,255,0.05);
      --line: rgba(255,255,255,0.10);
      --fg: #edf4ff;
      --muted: rgba(237,244,255,0.72);
      --accent: #72e0c2;
      --accent2: #f0be5d;
      --danger: #ff7f7f;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      color: var(--fg);
      font-family: "Segoe UI", "Microsoft YaHei", sans-serif;
      background:
        radial-gradient(1200px 700px at 10% 8%, rgba(114,224,194,0.15), transparent 55%),
        radial-gradient(900px 500px at 90% 15%, rgba(240,190,93,0.12), transparent 55%),
        linear-gradient(180deg, var(--bg0), var(--bg1));
    }}
    .wrap {{ max-width: 1400px; margin: 0 auto; padding: 22px 16px 32px; }}
    .hero {{
      display: grid;
      gap: 12px;
      margin-bottom: 16px;
    }}
    h1 {{ margin: 0; font-size: 22px; }}
    .sub {{ color: var(--muted); font-size: 13px; }}
    .card {{
      background: var(--card);
      border: 1px solid var(--line);
      border-radius: 16px;
      box-shadow: 0 12px 36px rgba(0,0,0,0.28);
      overflow: hidden;
    }}
    .card-body {{ padding: 16px; }}
    .stack {{ display: grid; gap: 16px; }}
    .toolbar {{
      display: flex;
      gap: 10px;
      flex-wrap: wrap;
      align-items: center;
      padding: 14px 16px;
      border-bottom: 1px solid var(--line);
      background: linear-gradient(180deg, rgba(255,255,255,0.04), rgba(255,255,255,0.01));
    }}
    .pill {{
      font-size: 12px;
      padding: 6px 10px;
      border: 1px solid var(--line);
      border-radius: 999px;
      color: var(--muted);
      background: rgba(255,255,255,0.03);
    }}
    form.upload {{
      display: flex;
      gap: 10px;
      flex-wrap: wrap;
      align-items: center;
      margin-top: 4px;
    }}
    input[type="file"] {{
      flex: 1 1 520px;
      color: var(--muted);
    }}
    input[type="text"] {{
      width: 100%;
      background: rgba(255,255,255,0.07);
      color: var(--fg);
      border: 1px solid rgba(255,255,255,0.12);
      border-radius: 8px;
      padding: 6px 8px;
    }}
    button, .mini {{
      appearance: none;
      border: 1px solid rgba(255,255,255,0.14);
      background: rgba(255,255,255,0.08);
      color: var(--fg);
      border-radius: 10px;
      padding: 8px 12px;
      font: inherit;
      cursor: pointer;
      text-decoration: none;
      display: inline-flex;
      align-items: center;
      justify-content: center;
    }}
    button.primary {{ background: linear-gradient(180deg, rgba(114,224,194,0.22), rgba(114,224,194,0.10)); border-color: rgba(114,224,194,0.35); }}
    button.warn {{ background: linear-gradient(180deg, rgba(240,190,93,0.24), rgba(240,190,93,0.10)); border-color: rgba(240,190,93,0.35); }}
    button.danger, .mini.danger {{ background: rgba(255,127,127,0.10); border-color: rgba(255,127,127,0.28); }}
    .mini.ghost {{ opacity: 0.5; cursor: default; }}
    .mini.placeholder {{ opacity: 0; pointer-events: none; }}
    select {{
      width: 100%;
      min-width: 92px;
      background: rgba(255,255,255,0.07);
      color: var(--fg);
      border: 1px solid rgba(255,255,255,0.12);
      border-radius: 8px;
      padding: 6px 8px;
    }}
    .fname {{ font-weight: 600; }}
    .actions {{ display: flex; gap: 8px; flex-wrap: wrap; }}
    .file-card {{
      border: 1px solid rgba(255,255,255,0.08);
      border-radius: 16px;
      overflow: hidden;
      background: rgba(255,255,255,0.03);
      margin-top: 14px;
    }}
    .file-head {{
      display: flex;
      gap: 12px;
      flex-wrap: wrap;
      align-items: center;
      justify-content: space-between;
      padding: 14px 16px;
      border-bottom: 1px solid rgba(255,255,255,0.08);
      background: rgba(255,255,255,0.02);
    }}
    .file-head-actions {{
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
      align-items: center;
    }}
    .group-list {{ padding: 12px 16px 16px; display: grid; gap: 12px; }}
    .group-row {{
      display: grid;
      grid-template-columns:
        minmax(180px, 1.6fr)
        minmax(110px, .7fr)
        minmax(110px, .7fr)
        minmax(150px, .95fr)
        minmax(150px, .95fr)
        auto;
      gap: 10px;
      align-items: end;
      padding: 12px;
      border-radius: 12px;
      border: 1px solid rgba(255,255,255,0.08);
      background: rgba(255,255,255,0.03);
    }}
    .group-row label {{ display:block; color: var(--muted); font-size: 12px; margin-bottom: 6px; }}
    .group-actions {{ display:flex; gap:8px; flex-wrap:wrap; align-items:center; min-width: 180px; justify-content:flex-start; }}
    .group-name {{ min-width: 0; }}
    .row-error pre, .error pre {{
      margin: 8px 0 0;
      white-space: pre-wrap;
      word-break: break-word;
      font-size: 12px;
      color: #ffd7d7;
      background: rgba(255,127,127,0.08);
      border: 1px solid rgba(255,127,127,0.18);
      border-radius: 10px;
      padding: 10px;
      overflow: auto;
    }}
    .error {{ margin-bottom: 16px; }}
    .report-stack {{ display: grid; gap: 16px; }}
    .report-card {{
      background: rgba(255,255,255,0.04);
      border: 1px solid rgba(255,255,255,0.08);
      border-radius: 16px;
      overflow: hidden;
    }}
    .report-head {{
      display: flex;
      gap: 12px;
      flex-wrap: wrap;
      align-items: center;
      justify-content: space-between;
      padding: 14px 16px;
      border-bottom: 1px solid rgba(255,255,255,0.08);
      background: rgba(255,255,255,0.02);
    }}
    .report-head h3 {{ margin: 0 0 4px; font-size: 16px; }}
    .report-links {{ display: flex; gap: 10px; flex-wrap: wrap; }}
    .report-links a {{ color: var(--accent); text-decoration: none; font-size: 13px; }}
    iframe {{
      width: 100%;
      min-height: 860px;
      border: 0;
      display: block;
      background: white;
    }}
    .empty, .empty-card {{
      color: var(--muted);
      text-align: center;
      padding: 24px 12px;
    }}
    @media (max-width: 1120px) {{
      .group-row {{ grid-template-columns: 1fr 1fr; }}
      .group-actions {{ grid-column: 1 / -1; }}
    }}
    @media (max-width: 720px) {{
      .group-row {{ grid-template-columns: 1fr; }}
    }}
  </style>
</head>
<body>
  <div class="wrap">
    <div class="hero">
      <h1>DSL Stepper 对比工作台</h1>
      <div class="sub">程序启动后直接打开这个网页。可以拖文件到 exe 预加载，也可以在这里继续添加多个 .dsl 文件统一解析和对比查看。</div>
      <div class="toolbar">
        <div class="pill">不再提供“滤波点数/窗口”输入，默认自动稳健滤波</div>
        <div class="pill">支持设置 DIR 极性 / STEP 边沿，也可不使用DIR</div>
        <div class="pill">支持多个 DSL 同页对比</div>
        <div class="pill">只生成 HTML，不再生成 CSV</div>
        <form action="/shutdown" method="post" style="margin-left:auto">
          <button class="danger" type="submit">退出程序</button>
        </form>
      </div>
    </div>
    {error_html}
    <div class="stack">
      <div class="card">
        <div class="card-body">
          <form class="upload" action="/upload" method="post" enctype="multipart/form-data">
            <input type="file" name="files" accept=".dsl" multiple />
            <button class="primary" type="submit">添加 DSL 文件</button>
          </form>
        </div>
        <form action="/analyze" method="post" id="analyzeForm">
          <div class="card-body" style="padding-top:0">
            {files_html}
          </div>
          <div class="toolbar">
            <div class="pill">每组都可独立设置通道、DIR 极性和 STEP 边沿，DIR 可留空按正向</div>
            <button class="primary" type="submit">生成/刷新对比视图</button>
          </div>
        </form>
      </div>
      <div class="report-stack">
        {reports_html}
      </div>
    </div>
  </div>
  <script>
    function pickDifferentValue(selectEl, avoidValue) {{
      for (const opt of selectEl.options) {{
        if (opt.value !== avoidValue) return opt.value;
      }}
      return selectEl.value;
    }}

    function syncProbeSelectors() {{
      const rows = document.querySelectorAll('select[data-group]');
      const groupIds = [...new Set([...rows].map((node) => node.dataset.group))];
      for (const groupId of groupIds) {{
        const dirSel = document.querySelector(`select[data-group="${{groupId}}"][data-role="dir"]`);
        const stepSel = document.querySelector(`select[data-group="${{groupId}}"][data-role="step"]`);
        const dirLevelSel = document.querySelector(`select[data-group="${{groupId}}"][data-role="dir-level"]`);
        if (!dirSel || !stepSel) continue;

        function applyConstraint(changedRole) {{
          const dirDisabled = dirSel.value === '{DIR_PROBE_NONE}';
          if (dirLevelSel) {{
            dirLevelSel.disabled = dirDisabled;
            dirLevelSel.title = dirDisabled ? '未使用DIR时固定按正方向' : '';
          }}
          if (dirDisabled) {{
            return;
          }}
          if (dirSel.value === stepSel.value) {{
            if (changedRole === 'dir') {{
              stepSel.value = pickDifferentValue(stepSel, dirSel.value);
            }} else if (changedRole === 'step') {{
              dirSel.value = pickDifferentValue(dirSel, stepSel.value);
            }} else {{
              stepSel.value = pickDifferentValue(stepSel, dirSel.value);
            }}
          }}
        }}

        dirSel.addEventListener('change', () => applyConstraint('dir'));
        stepSel.addEventListener('change', () => applyConstraint('step'));
        applyConstraint('');
      }}
    }}

    function validateProbeSelections(event) {{
      const rows = document.querySelectorAll('select[data-group]');
      const groupIds = [...new Set([...rows].map((node) => node.dataset.group))];
      for (const groupId of groupIds) {{
        const dirSel = document.querySelector(`select[data-group="${{groupId}}"][data-role="dir"]`);
        const stepSel = document.querySelector(`select[data-group="${{groupId}}"][data-role="step"]`);
        if (!dirSel || !stepSel) continue;
        if (dirSel.value !== '{DIR_PROBE_NONE}' && dirSel.value === stepSel.value) {{
          event.preventDefault();
          alert(`信号组 ${{groupId}} 的 DIR 和 STEP 不能相同，请重新选择。`);
          stepSel.focus();
          return false;
        }}
      }}
      return true;
    }}

    syncProbeSelectors();
    const analyzeForm = document.getElementById('analyzeForm');
    if (analyzeForm) {{
      analyzeForm.addEventListener('submit', validateProbeSelections);
    }}
  </script>
</body>
</html>"""


def _start_shutdown_watcher(server: AppServer) -> None:
    def _watch() -> None:
        while True:
            time.sleep(2.0)
            if time.time() - server.state.last_touch > INACTIVITY_TIMEOUT_S:
                try:
                    server.shutdown()
                except Exception:
                    pass
                return

    threading.Thread(target=_watch, daemon=True).start()


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    state = WorkspaceState()

    try:
        for path in _normalize_files(argv):
            if path.exists():
                state.add_capture_from_path(path)
    except Exception:
        state.last_error = traceback.format_exc()

    port = _find_free_port()
    server = AppServer(("127.0.0.1", port), state)
    _start_shutdown_watcher(server)

    url = f"http://127.0.0.1:{port}/"
    try:
        webbrowser.open(url, new=1)
    except Exception:
        state.last_error = traceback.format_exc()

    try:
        server.serve_forever()
    finally:
        state.cleanup()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

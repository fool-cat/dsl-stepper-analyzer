#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GUI wrapper for dsl_stepper_speed.py using zenity dialogs.

Usage:
1. Double-click / run directly:
   - popup file chooser
   - popup DIR/STEP channel selectors
   - popup DIR polarity / STEP edge selectors
   - generate HTML report

2. Drag .dsl file onto this program or a .desktop launcher that forwards files:
   - use argv file path(s)
   - popup DIR/STEP channel selectors
   - popup DIR polarity / STEP edge selectors
   - generate HTML report
"""

from __future__ import annotations

import pathlib
import shutil
import subprocess
import sys
from typing import Iterable, List, Optional

from dsl_stepper_speed import read_dsl_meta


SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
CLI_SCRIPT = SCRIPT_DIR / "dsl_stepper_speed.py"
DIR_PROBE_NONE = -1
DIR_POSITIVE_OPTIONS = [
    ("high", "高电平为正方向"),
    ("low", "低电平为正方向"),
]
STEP_EDGE_OPTIONS = [
    ("rising", "上升沿计步"),
    ("falling", "下降沿计步"),
]


class GuiCancelled(Exception):
    pass


def _run_zenity(args: List[str]) -> str:
    cmd = ["zenity", *args]
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if proc.returncode == 0:
        return proc.stdout.strip()
    raise GuiCancelled(proc.stderr.strip() or "cancelled")


def _show_info(title: str, text: str) -> None:
    subprocess.run(["zenity", "--info", "--title", title, "--width", "520", "--text", text], check=False)


def _show_error(title: str, text: str) -> None:
    subprocess.run(["zenity", "--error", "--title", title, "--width", "560", "--text", text], check=False)


def _ask_yes_no(title: str, text: str) -> bool:
    proc = subprocess.run(
        ["zenity", "--question", "--title", title, "--width", "480", "--text", text],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return proc.returncode == 0


def choose_file() -> pathlib.Path:
    path = _run_zenity(
        [
            "--file-selection",
            "--title",
            "选择 DSL 抓包文件",
            "--filename",
            str(SCRIPT_DIR) + "/",
            "--file-filter",
            "DSL 文件 | *.dsl",
            "--file-filter",
            "所有文件 | *",
        ]
    )
    return pathlib.Path(path)


def choose_probe(
    *,
    probe_count: int,
    title: str,
    prompt: str,
    default_probe: int,
    exclude: Optional[int] = None,
    include_none: bool = False,
    none_label: str = "未使用DIR",
    none_desc: str = "不采样DIR，固定按正方向",
) -> int:
    args = [
        "--list",
        "--radiolist",
        "--title",
        title,
        "--text",
        prompt,
        "--width",
        "520",
        "--height",
        str(max(280, 180 + probe_count * 24)),
        "--column",
        "选择",
        "--column",
        "通道",
        "--column",
        "说明",
    ]
    if include_none:
        enabled = "TRUE" if default_probe == DIR_PROBE_NONE else "FALSE"
        args.extend([enabled, str(DIR_PROBE_NONE), none_label, none_desc])
    for i in range(probe_count):
        if i == exclude:
            continue
        is_default = i == default_probe and i != exclude
        enabled = "TRUE" if is_default else "FALSE"
        desc = f"CH{i} / probe{i}"
        args.extend([enabled, str(i), desc])
    value = _run_zenity(args)
    return int(value)


def choose_option(
    *,
    title: str,
    prompt: str,
    default_value: str,
    options: List[tuple[str, str]],
) -> str:
    args = [
        "--list",
        "--radiolist",
        "--title",
        title,
        "--text",
        prompt,
        "--width",
        "520",
        "--height",
        str(max(260, 180 + len(options) * 26)),
        "--column",
        "选择",
        "--column",
        "值",
        "--column",
        "说明",
    ]
    for value, desc in options:
        enabled = "TRUE" if value == default_value else "FALSE"
        args.extend([enabled, value, desc])
    return _run_zenity(args)


def output_html_path(dsl_path: pathlib.Path) -> pathlib.Path:
    base = dsl_path.with_suffix("")
    return base.with_name(base.name + "_speed.html")


def _dir_positive_text(dir_positive_level: str) -> str:
    return "高电平为正方向" if dir_positive_level == "high" else "低电平为正方向"


def _dir_summary(dir_probe: int, dir_positive_level: str) -> str:
    if dir_probe < 0:
        return "未使用DIR（默认正向）"
    return f"CH{dir_probe} ({_dir_positive_text(dir_positive_level)})"


def _step_edge_text(step_edge: str) -> str:
    return "上升沿计步" if step_edge == "rising" else "下降沿计步"


def analyze_capture(
    dsl_path: pathlib.Path,
    dir_probe: int,
    step_probe: int,
    dir_positive_level: str,
    step_edge: str,
) -> subprocess.CompletedProcess[str]:
    cmd = [
        sys.executable,
        str(CLI_SCRIPT),
        str(dsl_path),
        "--step-probe",
        str(step_probe),
        "--step-edge",
        step_edge,
    ]
    if dir_probe < 0:
        cmd.append("--no-dir")
    else:
        cmd.extend(["--dir-probe", str(dir_probe)])
    if dir_probe >= 0 and dir_positive_level == "low":
        cmd.append("--dir-low-positive")
    return subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def _pick_settings_for_file(dsl_path: pathlib.Path) -> tuple[int, int, str, str]:
    meta = read_dsl_meta(dsl_path)
    if meta.total_probes < 1:
        raise RuntimeError(f"文件 {dsl_path.name} 没有可用通道，无法选择 STEP。")

    dir_probe = choose_probe(
        probe_count=meta.total_probes,
        title="选择 DIR 通道",
        prompt=f"文件: {dsl_path.name}\n请选择 DIR 所在通道，或直接使用默认正方向。",
        default_probe=DIR_PROBE_NONE if meta.total_probes < 2 else 0,
        include_none=True,
        none_label="未使用DIR",
        none_desc="不采样DIR，固定按正方向",
    )

    step_default = 1 if meta.total_probes > 1 and dir_probe != 1 else 0
    step_probe = choose_probe(
        probe_count=meta.total_probes,
        title="选择 STEP 通道",
        prompt=f"文件: {dsl_path.name}\n请选择 STEP 所在通道。",
        default_probe=step_default,
        exclude=dir_probe,
    )

    if dir_probe >= 0 and dir_probe == step_probe:
        raise RuntimeError("DIR 和 STEP 不能选择同一个通道。")

    dir_positive_level = "high"
    if dir_probe >= 0:
        dir_positive_level = choose_option(
            title="选择 DIR 正方向极性",
            prompt=f"文件: {dsl_path.name}\n请选择 DIR 哪个电平表示正方向。",
            default_value="high",
            options=DIR_POSITIVE_OPTIONS,
        )

    step_edge = choose_option(
        title="选择 STEP 计步边沿",
        prompt=f"文件: {dsl_path.name}\n请选择 STEP 以哪个边沿作为有效步进。",
        default_value="rising",
        options=STEP_EDGE_OPTIONS,
    )

    return dir_probe, step_probe, dir_positive_level, step_edge


def _normalize_input_files(args: Iterable[str]) -> List[pathlib.Path]:
    files: List[pathlib.Path] = []
    for arg in args:
        p = pathlib.Path(arg).expanduser()
        if p.suffix.lower() == ".dsl":
            files.append(p)
    return files


def _open_html_if_requested(html_path: pathlib.Path) -> None:
    if not shutil.which("xdg-open"):
        return
    if _ask_yes_no("输出完成", f"已生成:\n{html_path.name}\n\n是否现在打开 HTML 曲线页面?"):
        subprocess.run(["xdg-open", str(html_path)], check=False)


def process_one_file(dsl_path: pathlib.Path) -> bool:
    if not dsl_path.exists():
        _show_error("文件不存在", f"找不到文件:\n{dsl_path}")
        return False

    try:
        dir_probe, step_probe, dir_positive_level, step_edge = _pick_settings_for_file(dsl_path)
    except GuiCancelled:
        return False
    except Exception as exc:
        _show_error("选择参数失败", str(exc))
        return False

    result = analyze_capture(dsl_path, dir_probe, step_probe, dir_positive_level, step_edge)
    out_html = output_html_path(dsl_path)

    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "未知错误"
        _show_error(
            "解析失败",
            (
                f"文件: {dsl_path.name}\n"
                f"DIR = {_dir_summary(dir_probe, dir_positive_level)}\n"
                f"STEP = CH{step_probe} ({_step_edge_text(step_edge)})\n\n"
                f"{detail}"
            ),
        )
        return False

    _show_info(
        "输出完成",
        (
            f"文件: {dsl_path.name}\n"
            f"DIR = {_dir_summary(dir_probe, dir_positive_level)}\n"
            f"STEP = CH{step_probe} ({_step_edge_text(step_edge)})\n\n"
            f"HTML:\n{out_html}"
        ),
    )
    _open_html_if_requested(out_html)
    return True


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

    if not shutil.which("zenity"):
        print("This GUI wrapper requires zenity. Please install zenity or run dsl_stepper_speed.py directly.", file=sys.stderr)
        return 2

    files = _normalize_input_files(argv)
    if not files:
        try:
            files = [choose_file()]
        except GuiCancelled:
            return 1

    success = False
    for dsl_path in files:
        ok = process_one_file(dsl_path)
        success = success or ok

    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())

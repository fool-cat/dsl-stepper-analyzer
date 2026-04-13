#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Parse DSLogic/DSView .dsl captures (zip-based) and plot stepper speed curve.

Assumptions (validated on DSLogic v2 capture format):
- The .dsl file is a zip.
- Per-probe logic data is stored as bit-packed bytes in entries: L-<probe>/<block>
  where block ranges [0, total_blocks).
- Bit order inside each byte is LSB-first (bit0 is earliest sample).

This script extracts configurable edges from the STEP signal, samples DIR at those times,
computes:
- instantaneous speed (steps/s) between consecutive STEP edges
- acceleration (steps/s^2) from discrete derivative of speed
- jerk (steps/s^3) from discrete derivative of acceleration
- signed distance (steps) as cumulative step count

Outputs:
- Self-contained HTML (no external JS) that draws interactive curves.
- Optional CSV with (t_s, speed_steps_per_s, accel_steps_per_s2, jerk_steps_per_s3, distance_steps, dir_bit)
"""

from __future__ import annotations

import argparse
import configparser
import dataclasses
import html
import json
import math
import pathlib
import statistics
import textwrap
import zipfile
from typing import List, Optional, Tuple


@dataclasses.dataclass(frozen=True)
class DslMeta:
    total_samples: int
    total_probes: int
    total_blocks: int
    sample_rate_hz: float


STEP_EDGE_RISING = "rising"
STEP_EDGE_FALLING = "falling"
DIR_POSITIVE_HIGH = "high"
DIR_POSITIVE_LOW = "low"
DIR_PROBE_NONE = -1
EDGE_BIT_POS_TABLE: List[Tuple[int, ...]] = [tuple(i for i in range(8) if (b >> i) & 1) for b in range(256)]


def _parse_samplerate_text(s: str) -> float:
    s = s.strip()
    # Examples: "100 MHz", "10 MHz", "1 kHz", "100000000"
    try:
        return float(s)
    except ValueError:
        pass
    parts = s.split()
    if len(parts) != 2:
        raise ValueError(f"Unrecognized samplerate format: {s!r}")
    value = float(parts[0])
    unit = parts[1].lower()
    scale = {
        "hz": 1.0,
        "khz": 1e3,
        "mhz": 1e6,
        "ghz": 1e9,
    }.get(unit)
    if scale is None:
        raise ValueError(f"Unrecognized samplerate unit: {parts[1]!r}")
    return value * scale


def _read_header_ini(z: zipfile.ZipFile) -> configparser.ConfigParser:
    raw = z.read("header").decode("utf-8", "replace")
    cp = configparser.ConfigParser()
    cp.optionxform = str  # preserve case/spaces
    cp.read_string(raw)
    return cp


def _read_session_json(z: zipfile.ZipFile) -> Optional[dict]:
    # Some captures have a "session" JSON (DSView), some might not.
    try:
        raw = z.read("session")
    except KeyError:
        return None
    try:
        return json.loads(raw.decode("utf-8", "replace"))
    except Exception:
        return None


def read_dsl_meta(dsl_path: pathlib.Path) -> DslMeta:
    with zipfile.ZipFile(dsl_path, "r") as z:
        cp = _read_header_ini(z)
        sess = _read_session_json(z) or {}

        total_samples = int(cp["header"]["total samples"])
        total_probes = int(cp["header"]["total probes"])
        total_blocks = int(cp["header"]["total blocks"])

        # Prefer numeric session field when present.
        sr = None
        if isinstance(sess, dict):
            v = sess.get("Sample rate")
            if isinstance(v, str) and v.strip().isdigit():
                sr = float(v.strip())
            elif isinstance(v, (int, float)):
                sr = float(v)

        if sr is None:
            sr = _parse_samplerate_text(cp["header"]["samplerate"])

        return DslMeta(
            total_samples=total_samples,
            total_probes=total_probes,
            total_blocks=total_blocks,
            sample_rate_hz=sr,
        )


def _dsl_entry_name(probe: int, block: int) -> str:
    return f"L-{probe}/{block}"


def read_probe_bytes(dsl_path: pathlib.Path, meta: DslMeta, probe: int) -> bytes:
    if probe < 0 or probe >= meta.total_probes:
        raise ValueError(f"probe out of range: {probe} (total_probes={meta.total_probes})")
    expected = (meta.total_samples + 7) // 8
    parts: List[bytes] = []
    with zipfile.ZipFile(dsl_path, "r") as z:
        for blk in range(meta.total_blocks):
            name = _dsl_entry_name(probe, blk)
            try:
                parts.append(z.read(name))
            except KeyError as e:
                raise KeyError(f"Missing entry {name!r} in {dsl_path}") from e

    data = b"".join(parts)
    if len(data) < expected:
        raise ValueError(
            f"Probe {probe} data too short: got {len(data)} bytes, expected {expected} bytes"
        )
    if len(data) > expected:
        data = data[:expected]
    return data


def bit_at_lsb_first(bitpacked: bytes, sample_index: int) -> int:
    byte_i = sample_index >> 3
    bit_i = sample_index & 7
    return (bitpacked[byte_i] >> bit_i) & 1


def normalize_step_edge(step_edge: str) -> str:
    step_edge = (step_edge or STEP_EDGE_RISING).strip().lower()
    if step_edge not in (STEP_EDGE_RISING, STEP_EDGE_FALLING):
        raise ValueError(f"Unsupported STEP edge mode: {step_edge!r}")
    return step_edge


def step_edge_label(step_edge: str) -> str:
    step_edge = normalize_step_edge(step_edge)
    return "上升沿" if step_edge == STEP_EDGE_RISING else "下降沿"


def dir_positive_level_label(dir_high_positive: bool) -> str:
    return DIR_POSITIVE_HIGH if dir_high_positive else DIR_POSITIVE_LOW


def dir_probe_enabled(dir_probe: int) -> bool:
    return dir_probe >= 0


def _find_signal_edges_lsb_first(bitpacked: bytes, *, rising: bool) -> List[int]:
    """
    Return sample indices where signal has a configurable edge transition.

    The stream is interpreted LSB-first per byte.
    """
    prev = 0  # previous sample bit (0/1)
    edges: List[int] = []
    for byte_i, b in enumerate(bitpacked):
        prev_bits = ((b << 1) & 0xFE) | prev  # align previous bit for each position
        edge_bits = b & (~prev_bits & 0xFF) if rising else ((~b) & prev_bits & 0xFF)
        if edge_bits:
            base = byte_i * 8
            for bit_i in EDGE_BIT_POS_TABLE[edge_bits]:
                edges.append(base + bit_i)
        prev = (b >> 7) & 1

    # For rising-edge detection, a capture that starts while already high would
    # synthesize a fake edge at sample 0. Dropping it is usually safer.
    if edges and edges[0] == 0:
        edges = edges[1:]
    return edges


def find_rising_edges_lsb_first(bitpacked: bytes) -> List[int]:
    return _find_signal_edges_lsb_first(bitpacked, rising=True)


def find_step_edges_lsb_first(bitpacked: bytes, step_edge: str = STEP_EDGE_RISING) -> List[int]:
    return _find_signal_edges_lsb_first(bitpacked, rising=(normalize_step_edge(step_edge) == STEP_EDGE_RISING))


def compute_speed_curve(
    step_edges_samples: List[int],
    dir_bitpacked: Optional[bytes],
    sample_rate_hz: float,
    dir_sample_offset: int = -1,
    dir_high_positive: bool = True,
) -> Tuple[List[float], List[float], List[int]]:
    """
    Compute instantaneous speed between consecutive STEP edges.
    Returns (t_s, speed_steps_per_s_signed, dir_bits_at_interval).
    """
    if len(step_edges_samples) < 2:
        return [], [], []

    times: List[float] = []
    speeds: List[float] = []
    dir_bits: List[int] = []
    fixed_positive_bit = 1 if dir_high_positive else 0

    for i in range(1, len(step_edges_samples)):
        a = step_edges_samples[i - 1]
        b = step_edges_samples[i]
        if b <= a:
            continue
        dt = (b - a) / sample_rate_hz
        if dt <= 0:
            continue
        t_mid = ((a + b) / 2.0) / sample_rate_hz

        # Sample DIR a little before the step edge by default.
        if dir_bitpacked is None:
            dir_bit = fixed_positive_bit
            sign = 1.0
        else:
            dir_sample = b + dir_sample_offset
            if dir_sample < 0:
                dir_sample = 0
            dir_bit = bit_at_lsb_first(dir_bitpacked, dir_sample)
            sign = 1.0 if (dir_bit == 1) == dir_high_positive else -1.0

        times.append(t_mid)
        speeds.append(sign * (1.0 / dt))
        dir_bits.append(dir_bit)

    return times, speeds, dir_bits


def compute_accel_curve(t_s: List[float], v: List[float]) -> List[Optional[float]]:
    """
    Discrete derivative a[i] = dv/dt aligned to t_s[i]. First point is None.
    """
    return compute_optional_derivative(t_s, v)


def compute_optional_derivative(t_s: List[float], values: List[Optional[float]]) -> List[Optional[float]]:
    """
    Discrete derivative aligned to t_s[i]. First point is None.
    """
    if not t_s or len(t_s) != len(values):
        return []
    out: List[Optional[float]] = [None]
    for i in range(1, len(t_s)):
        dt = t_s[i] - t_s[i - 1]
        if dt <= 0:
            out.append(None)
            continue
        prev = values[i - 1]
        curr = values[i]
        if prev is None or curr is None or not math.isfinite(prev) or not math.isfinite(curr):
            out.append(None)
            continue
        out.append((curr - prev) / dt)
    return out


def compute_jerk_curve(t_s: List[float], a: List[Optional[float]]) -> List[Optional[float]]:
    """
    Discrete derivative of acceleration: jerk (steps/s^3).
    """
    return compute_optional_derivative(t_s, a)


def compute_distance_steps(dir_bits: List[int], dir_high_positive: bool) -> List[int]:
    pos = 0
    out: List[int] = []
    for b in dir_bits:
        sign = 1 if ((b == 1) == dir_high_positive) else -1
        pos += sign
        out.append(pos)
    return out


def moving_average(values: List[float], window: int) -> List[float]:
    if window <= 1 or len(values) == 0:
        return list(values)
    window = min(window, len(values))
    out: List[float] = []
    s = 0.0
    q: List[float] = []
    for v in values:
        q.append(v)
        s += v
        if len(q) > window:
            s -= q.pop(0)
        out.append(s / len(q))
    return out


def _clamp_odd(value: int, min_value: int, max_value: int) -> int:
    value = max(min_value, min(max_value, value))
    if value % 2 == 0:
        if value < max_value:
            value += 1
        else:
            value -= 1
    return max(min_value, min(max_value, value))


def choose_auto_filter_window(point_count: int) -> int:
    """
    Pick a small odd window automatically.

    The goal is not strong smoothing; it is just enough context to identify
    isolated glitches without forcing the user to tune a parameter.
    """
    if point_count <= 4:
        return 1
    if point_count <= 15:
        return 5
    estimate = point_count // 120
    return _clamp_odd(estimate, 5, 21)


def despike_speed_curve(values: List[float], window: Optional[int] = None) -> Tuple[List[float], dict]:
    """
    Remove isolated spikes using a Hampel-like local median test.

    This is more robust than a plain moving average when a single interval
    produces an unrealistically large speed peak.
    """
    n = len(values)
    if n <= 2:
        return list(values), {"mode": "raw", "window": 1, "replaced_points": 0}

    window = choose_auto_filter_window(n) if window is None else max(1, window)
    if window <= 1:
        return list(values), {"mode": "raw", "window": window, "replaced_points": 0}

    half = window // 2
    out = list(values)
    replaced = 0
    for i, x in enumerate(values):
        lo = max(0, i - half)
        hi = min(n, i + half + 1)
        neighborhood = values[lo:hi]
        if len(neighborhood) < 3:
            continue

        local_median = float(statistics.median(neighborhood))
        deviations = [abs(v - local_median) for v in neighborhood]
        mad = float(statistics.median(deviations))
        robust_sigma = max(1e-12, 1.4826 * mad)

        is_outlier = abs(x - local_median) > 6.0 * robust_sigma

        if 0 < i < n - 1:
            neighbor_scale = max(abs(values[i - 1]), abs(values[i + 1]), abs(local_median), 1.0)
            isolated_jump = abs(x - local_median) > 3.5 * neighbor_scale
            neighbors_same_trend = values[i - 1] * values[i + 1] >= 0
            is_outlier = is_outlier or (isolated_jump and neighbors_same_trend)

        if is_outlier:
            out[i] = local_median
            replaced += 1

    return out, {
        "mode": "despike",
        "window": window,
        "replaced_points": replaced,
    }


def auto_filter_speed_curve(values: List[float]) -> Tuple[List[float], dict]:
    """
    Robust automatic filter for speed values.

    Pipeline:
    1. Median-based despike to suppress isolated giant peaks.
    2. A tiny trailing moving average to soften residual jitter.
    """
    n = len(values)
    if n <= 2:
        return list(values), {"mode": "raw", "window": 1, "replaced_points": 0, "smooth_window": 1}

    despike_window = choose_auto_filter_window(n)
    cleaned, info = despike_speed_curve(values, despike_window)
    smooth_window = 3 if n >= 5 else 1
    filtered = moving_average(cleaned, smooth_window) if smooth_window > 1 else cleaned
    info = dict(info)
    info["mode"] = "auto_robust"
    info["smooth_window"] = smooth_window
    return filtered, info


def build_speed_variants(values: List[float]) -> Tuple[dict, str]:
    raw = list(values)
    point_count = len(raw)
    auto_window = choose_auto_filter_window(point_count)

    despike_only, despike_info = despike_speed_curve(raw, auto_window)
    moving_avg_window = 1 if point_count <= 4 else min(auto_window, 9)
    moving_avg = moving_average(raw, moving_avg_window) if moving_avg_window > 1 else list(raw)
    auto_robust, auto_info = auto_filter_speed_curve(raw)

    variants = {
        "raw": {
            "label": "原始",
            "short_label": "原始",
            "color": "#ff8a65",
            "line_width": 1.2,
            "alpha": 0.85,
            "values": raw,
            "default_visible": True,
            "stats": {"mode": "raw", "window": 1, "replaced_points": 0},
        },
        "despike": {
            "label": f"去尖峰 (中位数, 窗口={despike_info['window']})",
            "short_label": "去尖峰",
            "color": "#66e3c4",
            "line_width": 1.6,
            "alpha": 0.95,
            "values": despike_only,
            "default_visible": False,
            "stats": despike_info,
        },
        "moving_avg": {
            "label": f"移动平均 (窗口={moving_avg_window})",
            "short_label": "均值",
            "color": "#7aa7ff",
            "line_width": 1.6,
            "alpha": 0.90,
            "values": moving_avg,
            "default_visible": False,
            "stats": {"mode": "moving_average_auto", "window": moving_avg_window, "replaced_points": 0},
        },
        "auto_robust": {
            "label": (
                "自动稳健 "
                f"(去尖峰窗口={auto_info['window']}, 平滑窗口={auto_info.get('smooth_window', 1)})"
            ),
            "short_label": "自动稳健",
            "color": "#ffd36a",
            "line_width": 1.9,
            "alpha": 1.0,
            "values": auto_robust,
            "default_visible": True,
            "stats": auto_info,
        },
    }
    return variants, "auto_robust"


def build_derivative_variants(
    t_s: List[float],
    source_variants: dict,
    derivative_fn,
    *,
    unit_label: str,
) -> dict:
    variants = {}
    for key, variant in source_variants.items():
        derived = derivative_fn(t_s, variant["values"])
        variants[key] = {
            "label": variant["label"],
            "short_label": variant["short_label"],
            "color": variant["color"],
            "line_width": variant["line_width"],
            "alpha": variant["alpha"],
            "values": derived,
            "default_visible": variant["default_visible"],
            "stats": {
                "mode": variant["stats"].get("mode", "derived"),
                "source_window": variant["stats"].get("window"),
                "source_replaced_points": variant["stats"].get("replaced_points", 0),
                "derived_unit": unit_label,
            },
        }
    return variants


def _write_csv(
    path: pathlib.Path,
    t_s: List[float],
    v: List[float],
    a: List[Optional[float]],
    j: List[Optional[float]],
    dist_steps: List[int],
    dir_bits: List[int],
) -> None:
    import csv

    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["t_s", "speed_steps_per_s", "accel_steps_per_s2", "jerk_steps_per_s3", "distance_steps", "dir_bit"])
        for i in range(len(t_s)):
            ai = a[i] if i < len(a) else None
            ji = j[i] if i < len(j) else None
            w.writerow(
                [
                    f"{t_s[i]:.9f}",
                    f"{v[i]:.6f}",
                    "" if ai is None else f"{ai:.6f}",
                    "" if ji is None else f"{ji:.6f}",
                    dist_steps[i],
                    dir_bits[i],
                ]
            )


def _json_dumps_compact(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def _make_html(
    title: str,
    t_s: List[float],
    speed_variants: dict,
    accel_variants: dict,
    jerk_variants: dict,
    dist_steps: List[int],
    meta: DslMeta,
    stats: dict,
) -> str:
    # Keep arrays as JSON for easy JS parsing.
    data_json = _json_dumps_compact(
        {
            "t": t_s,
            "speed_variants": speed_variants,
            "accel_variants": accel_variants,
            "jerk_variants": jerk_variants,
            "d": dist_steps,
        }
    )
    stats_json = _json_dumps_compact(stats)

    safe_title = html.escape(title)

    return textwrap.dedent(
        f"""\
        <!doctype html>
        <html lang="zh-CN">
        <head>
          <meta charset="utf-8" />
          <meta name="viewport" content="width=device-width, initial-scale=1" />
          <title>{safe_title}</title>
          <style>
            :root {{
              --bg0: #0b1220;
              --bg1: #0f1a2d;
              --fg:  #e7eefc;
              --muted: rgba(231,238,252,0.75);
              --grid: rgba(231,238,252,0.12);
              --axis: rgba(231,238,252,0.28);
              --line: #66e3c4;
              --accent: #ffd36a;
              --bad: #ff6b6b;
              --mono: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace;
              --sans: ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, Helvetica, Arial, "Apple Color Emoji", "Segoe UI Emoji";
            }}
            body {{
              margin: 0;
              font-family: var(--sans);
              background: radial-gradient(1200px 600px at 15% 10%, rgba(102,227,196,0.18), transparent 60%),
                          radial-gradient(900px 500px at 85% 20%, rgba(255,211,106,0.15), transparent 55%),
                          linear-gradient(180deg, var(--bg0), var(--bg1));
              color: var(--fg);
            }}
            .wrap {{
              max-width: 1100px;
              margin: 0 auto;
              padding: 22px 16px 28px;
            }}
            header {{
              display: flex;
              gap: 14px;
              flex-wrap: wrap;
              align-items: baseline;
              justify-content: space-between;
            }}
            h1 {{
              margin: 0;
              font-size: 18px;
              letter-spacing: 0.2px;
              font-weight: 650;
            }}
            .sub {{
              color: var(--muted);
              font-family: var(--mono);
              font-size: 12px;
            }}
            .card {{
              margin-top: 14px;
              background: rgba(255,255,255,0.04);
              border: 1px solid rgba(255,255,255,0.08);
              border-radius: 14px;
              overflow: hidden;
              box-shadow: 0 8px 32px rgba(0,0,0,0.35);
            }}
            .toolbar {{
              padding: 10px 12px;
              display: flex;
              gap: 10px;
              flex-wrap: wrap;
              align-items: center;
              border-bottom: 1px solid rgba(255,255,255,0.07);
              background: linear-gradient(180deg, rgba(255,255,255,0.035), rgba(255,255,255,0.02));
            }}
            .pill {{
              padding: 6px 10px;
              border-radius: 999px;
              background: rgba(255,255,255,0.06);
              border: 1px solid rgba(255,255,255,0.09);
              font-size: 12px;
              color: var(--muted);
            }}
            .pill strong {{ color: var(--fg); font-weight: 650; }}
            .toggle {{
              display: inline-flex;
              align-items: center;
              gap: 6px;
              color: var(--fg);
            }}
            .mode-bar {{
              display: flex;
              gap: 8px;
              flex-wrap: wrap;
              align-items: center;
            }}
            .mode-btn {{
              appearance: none;
              border: 1px solid rgba(255,255,255,0.14);
              background: rgba(255,255,255,0.06);
              color: rgba(231,238,252,0.82);
              border-radius: 999px;
              padding: 7px 12px;
              font: inherit;
              font-size: 12px;
              cursor: pointer;
              transition: background 120ms ease, border-color 120ms ease, color 120ms ease, transform 120ms ease;
            }}
            .mode-btn.active {{
              color: #09111f;
              font-weight: 700;
              background: linear-gradient(180deg, rgba(255,211,106,0.98), rgba(255,190,74,0.94));
              border-color: rgba(255,211,106,0.98);
              box-shadow: 0 0 0 1px rgba(255,211,106,0.18), 0 6px 16px rgba(255,189,64,0.22);
              transform: translateY(-1px);
            }}
            .layer-panel {{
              display: inline-flex;
              gap: 10px;
              flex-wrap: wrap;
              align-items: center;
              padding: 6px 10px;
              border-radius: 999px;
              background: rgba(255,255,255,0.04);
              border: 1px solid rgba(255,255,255,0.08);
            }}
            .layer-panel strong {{
              color: var(--fg);
              font-size: 12px;
            }}
            .toggle input {{
              accent-color: #66e3c4;
            }}
            .hint {{
              margin-left: auto;
              color: rgba(231,238,252,0.68);
              font-size: 12px;
            }}
            canvas {{
              width: 100%;
              height: 520px;
              display: block;
              background: rgba(0,0,0,0.08);
            }}
            .footer {{
              padding: 10px 12px 12px;
              color: rgba(231,238,252,0.70);
              font-size: 12px;
              display: grid;
              grid-template-columns: 1fr;
              gap: 8px;
            }}
            .footer pre {{
              margin: 0;
              padding: 10px 12px;
              border-radius: 12px;
              background: rgba(255,255,255,0.04);
              border: 1px solid rgba(255,255,255,0.08);
              overflow-x: auto;
              font-family: var(--mono);
            }}
            .tooltip {{
              position: fixed;
              z-index: 10;
              pointer-events: none;
              transform: translate(10px, 10px);
              background: rgba(10,14,22,0.92);
              border: 1px solid rgba(255,255,255,0.16);
              border-radius: 10px;
              padding: 8px 10px;
              font-family: var(--mono);
              font-size: 12px;
              color: var(--fg);
              box-shadow: 0 8px 24px rgba(0,0,0,0.35);
              display: none;
              max-width: min(420px, 92vw);
            }}
            .tooltip .k {{ color: rgba(231,238,252,0.70); }}
            @media (max-width: 720px) {{
              canvas {{ height: 420px; }}
              .hint {{ width: 100%; margin-left: 0; }}
            }}
          </style>
        </head>
        <body>
          <div class="wrap">
            <header>
              <h1>{safe_title}</h1>
              <div class="sub">sample_rate={meta.sample_rate_hz:.0f} Hz, total_samples={meta.total_samples}</div>
            </header>
            <div class="card">
              <div class="toolbar">
                <div class="mode-bar" id="modeBtns"></div>
                <label class="pill toggle">
                  <input id="showLines" type="checkbox" checked />
                  <strong>连线</strong>
                </label>
                <div class="pill"><strong>操作</strong> 滚轮缩放, 拖拽平移, 双击复位</div>
                <div class="layer-panel" id="curveLayers"></div>
                <div class="hint">提示: 如果你想做物理速度 (mm/s, RPM), 可以在脚本参数里指定转换系数</div>
              </div>
              <canvas id="cv"></canvas>
              <div class="footer">
                <div>统计:</div>
                <pre id="stats"></pre>
              </div>
            </div>
          </div>
          <div class="tooltip" id="tip"></div>
          <script>
          const DATA = {data_json};
          const STATS = {stats_json};
          document.getElementById('stats').textContent = JSON.stringify(STATS, null, 2);

          const cv = document.getElementById('cv');
          const tip = document.getElementById('tip');
          const ctx = cv.getContext('2d');

          const SERIES = {{
            speed: {{ key: 'speed', label: '速度', unit: 'steps/s', yLabel: 'speed (steps/s)', color: '#ffb347' }},
            accel: {{ key: 'accel', label: '加速度', unit: 'steps/s^2', yLabel: 'accel (steps/s^2)', color: '#66e3c4' }},
            jerk:  {{ key: 'jerk',  label: '加加速度', unit: 'steps/s^3', yLabel: 'jerk (steps/s^3)', color: '#7aa7ff' }},
            dist:  {{ key: 'dist',  label: '距离', unit: 'steps', yLabel: 'distance (steps)', color: '#ff8fab', arr: () => DATA.d }},
          }};
          const VARIANT_STYLES = {{
            raw: {{ dash: [], alpha: 0.85, width: 1.2, short: '原始' }},
            despike: {{ dash: [7, 4], alpha: 0.90, width: 1.5, short: '去尖峰' }},
            moving_avg: {{ dash: [2, 4], alpha: 0.88, width: 1.5, short: '均值' }},
            auto_robust: {{ dash: [], alpha: 1.0, width: 2.0, short: '自动稳健' }},
            manual_moving_avg: {{ dash: [10, 4], alpha: 0.95, width: 1.8, short: '手动均值' }},
          }};
          const CATEGORY_GROUPS = {{
            speed: DATA.speed_variants || {{}},
            accel: DATA.accel_variants || {{}},
            jerk: DATA.jerk_variants || {{}},
            dist: {{
              dist: {{
                label: '距离',
                short_label: '距离',
                values: DATA.d,
                default_visible: true,
              }}
            }},
          }};

          function resize() {{
            const dpr = Math.max(1, window.devicePixelRatio || 1);
            const rect = cv.getBoundingClientRect();
            cv.width = Math.floor(rect.width * dpr);
            cv.height = Math.floor(rect.height * dpr);
            ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
            draw();
          }}

          const state = {{
            x0: DATA.t.length ? DATA.t[0] : 0,
            x1: DATA.t.length ? DATA.t[DATA.t.length - 1] : 1,
            currentCategory: 'speed',
            showLines: true,
            showPoints: true,
            dragging: false,
            dragStartX: 0,
            dragStartRange: [0,1],
            visibleLayerKeys: {{
              speed: [],
              accel: [],
              jerk: [],
              dist: [],
            }},
          }};

          for (const groupKey of ['speed', 'accel', 'jerk', 'dist']) {{
            const keys = Object.keys(CATEGORY_GROUPS[groupKey] || {{}});
            state.visibleLayerKeys[groupKey] = keys.filter((k) => CATEGORY_GROUPS[groupKey][k] && CATEGORY_GROUPS[groupKey][k].default_visible);
          }}
          if (!state.visibleLayerKeys.speed.length && CATEGORY_GROUPS.speed.auto_robust) {{
            state.visibleLayerKeys.speed = ['auto_robust'];
          }}

          function setupModeButtons() {{
            const el = document.getElementById('modeBtns');
            el.innerHTML = '';
            for (const groupKey of ['speed','accel','jerk','dist']) {{
              const cfg = SERIES[groupKey];
              const btn = document.createElement('button');
              btn.type = 'button';
              btn.className = 'mode-btn';
              btn.dataset.key = groupKey;
              btn.textContent = cfg.label;
              btn.addEventListener('click', () => {{
                state.currentCategory = groupKey;
                updateModeButtons();
                setupCurveLayerToggles();
                draw();
              }});
              el.appendChild(btn);
            }}
            updateModeButtons();
          }}

          function updateModeButtons() {{
            const btns = document.querySelectorAll('#modeBtns .mode-btn');
            btns.forEach((btn) => {{
              btn.classList.toggle('active', btn.dataset.key === state.currentCategory);
            }});
          }}

          function setupCurveLayerToggles() {{
            const el = document.getElementById('curveLayers');
            el.innerHTML = '';
            const groupKey = state.currentCategory;
            const cfg = SERIES[groupKey];
            const variants = CATEGORY_GROUPS[groupKey] || {{}};
            const keys = Object.keys(variants);
            if (!keys.length) {{
              el.style.display = 'none';
              return;
            }}
            el.style.display = 'inline-flex';
            const title = document.createElement('strong');
            title.textContent = `${{cfg.label}}图层`;
            title.style.color = cfg.color;
            el.appendChild(title);
            for (const key of keys) {{
              const item = variants[key];
              const style = getLayerStyle(groupKey, key, item);
              const label = document.createElement('label');
              label.className = 'toggle';
              label.style.color = style.color;

              const input = document.createElement('input');
              input.type = 'checkbox';
              input.checked = state.visibleLayerKeys[groupKey].includes(key);
              input.dataset.group = groupKey;
              input.dataset.key = key;
              input.addEventListener('change', (ev) => {{
                const checked = ev.target.checked;
                const g = ev.target.dataset.group;
                const k = ev.target.dataset.key;
                if (checked) {{
                  if (!state.visibleLayerKeys[g].includes(k)) state.visibleLayerKeys[g].push(k);
                }} else {{
                  state.visibleLayerKeys[g] = state.visibleLayerKeys[g].filter((v) => v !== k);
                  if (!state.visibleLayerKeys[g].length) {{
                    state.visibleLayerKeys[g] = [k];
                    ev.target.checked = true;
                  }}
                }}
                draw();
              }});

              const text = document.createElement('span');
              text.textContent = item.short_label || style.shortLabel || item.label;
              label.appendChild(input);
              label.appendChild(text);
              el.appendChild(label);
            }}
          }}

          function getLayerStyle(groupKey, key, cfg) {{
            const series = SERIES[groupKey];
            const style = VARIANT_STYLES[key] || VARIANT_STYLES.raw;
            return {{
              color: series.color,
              dash: style.dash || [],
              alpha: style.alpha ?? (cfg.alpha || 1),
              width: style.width ?? (cfg.line_width || 1.5),
              shortLabel: style.short || cfg.short_label || cfg.label,
              unit: series.unit,
              yLabel: series.yLabel,
              categoryLabel: series.label,
            }};
          }}

          function collectActiveSeries() {{
            const out = [];
            const groupKey = state.currentCategory;
            const group = CATEGORY_GROUPS[groupKey] || {{}};
            const visibleKeys = state.visibleLayerKeys[groupKey] || [];
            for (const key of visibleKeys) {{
              const cfg = group[key];
              if (!cfg) continue;
              const style = getLayerStyle(groupKey, key, cfg);
              out.push({{
                key: `${{groupKey}}:${{key}}`,
                groupKey,
                variantKey: key,
                label: cfg.short_label || style.shortLabel,
                shortLabel: cfg.short_label || style.shortLabel,
                color: style.color,
                dash: style.dash,
                lineWidth: style.width,
                alpha: style.alpha,
                unit: style.unit,
                yLabel: style.yLabel,
                values: cfg.values,
              }});
            }}
            return out;
          }}

          function niceStep(span, targetTicks) {{
            if (span <= 0) return 1;
            const raw = span / Math.max(1, targetTicks);
            const pow10 = Math.pow(10, Math.floor(Math.log10(raw)));
            const n = raw / pow10;
            let m = 1;
            if (n < 1.5) m = 1;
            else if (n < 3) m = 2;
            else if (n < 7) m = 5;
            else m = 10;
            return m * pow10;
          }}

          function fmt(x) {{
            if (!isFinite(x)) return 'NaN';
            const ax = Math.abs(x);
            if (ax >= 1e6 || ax < 1e-3) return x.toExponential(3);
            if (ax < 1) return x.toFixed(6);
            if (ax < 10) return x.toFixed(4);
            if (ax < 100) return x.toFixed(3);
            if (ax < 1000) return x.toFixed(2);
            return x.toFixed(1);
          }}

          function draw() {{
            const w = cv.getBoundingClientRect().width;
            const h = cv.getBoundingClientRect().height;
            ctx.clearRect(0,0,w,h);

            const padL = 58, padR = 18, padT = 16, padB = 40;
            const plotW = Math.max(1, w - padL - padR);
            const plotH = Math.max(1, h - padT - padB);

            const t = DATA.t;
            if (!t.length) {{
              ctx.fillStyle = 'rgba(231,238,252,0.75)';
              ctx.font = '14px sans-serif';
              ctx.fillText('没有可绘制的数据', padL, padT + 20);
              return;
            }}

            const xMin = state.x0, xMax = state.x1;
            // Compute y-range from visible points (fast enough for ~20k)
            let yMin = Infinity, yMax = -Infinity;
            const i0 = Math.max(0, bisectLeft(t, xMin) - 1);
            const i1 = Math.min(t.length - 1, bisectRight(t, xMax) + 1);
            const activeSeries = collectActiveSeries();
            if (!activeSeries.length) {{
              ctx.fillStyle = 'rgba(231,238,252,0.75)';
              ctx.font = '14px sans-serif';
              ctx.fillText('请至少勾选一条曲线图层', padL, padT + 20);
              return;
            }}

            for (const layer of activeSeries) {{
              for (let i=i0; i<=i1; i++) {{
                const y = layer.values[i];
                if (y === null || y === undefined || !isFinite(y)) continue;
                if (y < yMin) yMin = y;
                if (y > yMax) yMax = y;
              }}
            }}
            if (!isFinite(yMin) || !isFinite(yMax) || yMin === yMax) {{
              yMin -= 1; yMax += 1;
            }} else {{
              const m = (yMax - yMin) * 0.08;
              yMin -= m; yMax += m;
            }}

            function xToPx(x) {{
              return padL + (x - xMin) / (xMax - xMin) * plotW;
            }}
            function yToPx(y) {{
              return padT + (1 - (y - yMin) / (yMax - yMin)) * plotH;
            }}

            // Grid + axes
            ctx.lineWidth = 1;
            ctx.strokeStyle = 'rgba(231,238,252,0.12)';
            ctx.fillStyle = 'rgba(231,238,252,0.70)';
            ctx.font = '12px ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace';

            const xStep = niceStep(xMax - xMin, 6);
            const yStep = niceStep(yMax - yMin, 5);

            const xStart = Math.floor(xMin / xStep) * xStep;
            for (let x=xStart; x<=xMax + 1e-12; x += xStep) {{
              const px = xToPx(x);
              ctx.beginPath();
              ctx.moveTo(px, padT);
              ctx.lineTo(px, padT + plotH);
              ctx.stroke();
              ctx.fillText(fmt(x), px - 10, padT + plotH + 18);
            }}

            const yStart = Math.floor(yMin / yStep) * yStep;
            for (let y=yStart; y<=yMax + 1e-12; y += yStep) {{
              const py = yToPx(y);
              ctx.beginPath();
              ctx.moveTo(padL, py);
              ctx.lineTo(padL + plotW, py);
              ctx.stroke();
              ctx.fillText(fmt(y), 6, py + 4);
            }}

            // Axis box
            ctx.strokeStyle = 'rgba(231,238,252,0.28)';
            ctx.beginPath();
            ctx.rect(padL, padT, plotW, plotH);
            ctx.stroke();

            for (const layer of activeSeries) {{
              const visiblePoints = [];
              for (let i=i0; i<=i1; i++) {{
                const x = t[i];
                if (x < xMin || x > xMax) continue;
                const yv = layer.values[i];
                if (yv === null || yv === undefined || !isFinite(yv)) continue;
                visiblePoints.push([xToPx(x), yToPx(yv)]);
              }}

              if (state.showLines && visiblePoints.length) {{
                ctx.save();
                ctx.globalAlpha = layer.alpha;
                ctx.lineWidth = layer.lineWidth;
                ctx.strokeStyle = layer.color;
                ctx.setLineDash(layer.dash || []);
                ctx.beginPath();
                ctx.moveTo(visiblePoints[0][0], visiblePoints[0][1]);
                for (let i=1; i<visiblePoints.length; i++) {{
                  ctx.lineTo(visiblePoints[i][0], visiblePoints[i][1]);
                }}
                ctx.stroke();
                ctx.restore();
              }}

              if (state.showPoints) {{
                ctx.save();
                ctx.globalAlpha = Math.min(1, layer.alpha + 0.05);
                ctx.fillStyle = layer.color;
                for (const [px, py] of visiblePoints) {{
                  ctx.beginPath();
                  ctx.arc(px, py, layer.groupKey === 'dist' ? 2.2 : 1.8, 0, Math.PI * 2);
                  ctx.fill();
                }}
                ctx.restore();
              }}
            }}

            if (activeSeries.length) {{
              const legendX = padL + 12;
              let legendY = padT + 18;
              for (const layer of activeSeries) {{
                ctx.save();
                ctx.globalAlpha = layer.alpha;
                ctx.strokeStyle = layer.color;
                ctx.lineWidth = layer.lineWidth;
                ctx.setLineDash(layer.dash || []);
                ctx.beginPath();
                ctx.moveTo(legendX, legendY);
                ctx.lineTo(legendX + 18, legendY);
                ctx.stroke();
                ctx.restore();
                ctx.fillStyle = 'rgba(231,238,252,0.84)';
                ctx.fillText(layer.label, legendX + 24, legendY + 4);
                legendY += 18;
              }}
            }}

            // Labels
            ctx.fillStyle = 'rgba(231,238,252,0.80)';
            ctx.font = '12px ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace';
            ctx.fillText('t (s)', padL + plotW - 40, padT + plotH + 34);
            ctx.save();
            ctx.translate(16, padT + 12);
            ctx.rotate(-Math.PI / 2);
            ctx.fillText(activeSeries[0].yLabel, 0, 0);
            ctx.restore();
          }}

          function bisectLeft(arr, x) {{
            let lo = 0, hi = arr.length;
            while (lo < hi) {{
              const mid = (lo + hi) >> 1;
              if (arr[mid] < x) lo = mid + 1;
              else hi = mid;
            }}
            return lo;
          }}
          function bisectRight(arr, x) {{
            let lo = 0, hi = arr.length;
            while (lo < hi) {{
              const mid = (lo + hi) >> 1;
              if (arr[mid] <= x) lo = mid + 1;
              else hi = mid;
            }}
            return lo;
          }}

          function showTip(clientX, clientY) {{
            const rect = cv.getBoundingClientRect();
            const x = (clientX - rect.left);
            const w = rect.width;
            const padL = 58, padR = 18;
            const plotW = Math.max(1, w - padL - padR);
            const xFrac = (x - padL) / plotW;
            const tMin = state.x0, tMax = state.x1;
            const tx = tMin + xFrac * (tMax - tMin);
            const i = Math.min(DATA.t.length - 1, Math.max(0, bisectLeft(DATA.t, tx)));
            const t0 = DATA.t[i];
            tip.style.left = clientX + 'px';
            tip.style.top = clientY + 'px';
            const lines = [`<div><span class="k">t</span>=${{fmt(t0)}} s</div>`];
            for (const layer of collectActiveSeries()) {{
              const value = layer.values[i];
              if (value === null || value === undefined || !isFinite(value)) continue;
              lines.push(`<div><span class="k" style="color:${{layer.color}}">${{layer.label}}</span>=${{fmt(value)}} ${{layer.unit}}</div>`);
            }}
            tip.innerHTML = lines.join('');
            tip.style.display = 'block';
          }}

          cv.addEventListener('mousemove', (ev) => {{
            showTip(ev.clientX, ev.clientY);
          }});
          cv.addEventListener('mouseleave', () => {{ tip.style.display = 'none'; }});

          cv.addEventListener('wheel', (ev) => {{
            ev.preventDefault();
            const rect = cv.getBoundingClientRect();
            const x = (ev.clientX - rect.left);
            const w = rect.width;
            const padL = 58, padR = 18;
            const plotW = Math.max(1, w - padL - padR);
            const frac = (x - padL) / plotW;
            const anchor = state.x0 + frac * (state.x1 - state.x0);
            const zoom = Math.exp(ev.deltaY * 0.0015);
            const newSpan = (state.x1 - state.x0) * zoom;
            const fullSpan = DATA.t[DATA.t.length - 1] - DATA.t[0];
            const minSpan = fullSpan / 5000; // avoid infinity zoom
            const span = Math.max(minSpan, Math.min(fullSpan, newSpan));
            state.x0 = anchor - frac * span;
            state.x1 = state.x0 + span;
            if (state.x0 < DATA.t[0]) {{ state.x0 = DATA.t[0]; state.x1 = state.x0 + span; }}
            if (state.x1 > DATA.t[DATA.t.length - 1]) {{ state.x1 = DATA.t[DATA.t.length - 1]; state.x0 = state.x1 - span; }}
            draw();
          }}, {{ passive: false }});

          cv.addEventListener('mousedown', (ev) => {{
            state.dragging = true;
            state.dragStartX = ev.clientX;
            state.dragStartRange = [state.x0, state.x1];
          }});
          window.addEventListener('mouseup', () => {{ state.dragging = false; }});
          window.addEventListener('mousemove', (ev) => {{
            if (!state.dragging) return;
            const rect = cv.getBoundingClientRect();
            const dx = ev.clientX - state.dragStartX;
            const span = state.dragStartRange[1] - state.dragStartRange[0];
            const dt = -dx / rect.width * span;
            state.x0 = state.dragStartRange[0] + dt;
            state.x1 = state.dragStartRange[1] + dt;
            const tMin = DATA.t[0], tMax = DATA.t[DATA.t.length - 1];
            if (state.x0 < tMin) {{ state.x1 += (tMin - state.x0); state.x0 = tMin; }}
            if (state.x1 > tMax) {{ state.x0 -= (state.x1 - tMax); state.x1 = tMax; }}
            draw();
          }});

          cv.addEventListener('dblclick', () => {{
            state.x0 = DATA.t[0];
            state.x1 = DATA.t[DATA.t.length - 1];
            draw();
          }});

          window.addEventListener('resize', resize);
          document.getElementById('showLines').addEventListener('change', (ev) => {{
            state.showLines = ev.target.checked;
            draw();
          }});
          setupModeButtons();
          setupCurveLayerToggles();
          resize();
          </script>
        </body>
        </html>
        """
    )


def _compute_stats(
    t_s: List[float],
    v: List[float],
    a: List[Optional[float]],
    dist_steps: List[int],
    meta: DslMeta,
    step_edges: List[int],
    dir_probe: Optional[int],
    step_probe: int,
    step_edge: str,
    dir_high_positive: bool,
    dir_sample_offset: int,
    filter_info: Optional[dict] = None,
) -> dict:
    signal_config = {
        "dir_probe": None if dir_probe is None else int(dir_probe),
        "step_probe": int(step_probe),
        "dir_positive_level": "fixed_positive" if dir_probe is None else dir_positive_level_label(dir_high_positive),
        "step_edge": normalize_step_edge(step_edge),
        "dir_sample_offset": int(dir_sample_offset),
        "dir_source": "fixed_positive" if dir_probe is None else "captured",
    }
    if not t_s:
        return {
            "points": 0,
            "sample_rate_hz": meta.sample_rate_hz,
            "total_samples": meta.total_samples,
            "capture_duration_s": meta.total_samples / meta.sample_rate_hz,
            "signal_config": signal_config,
            "note": "Not enough STEP edges to compute speed.",
        }
    abs_v = [abs(x) for x in v]
    abs_a = [abs(x) for x in a if x is not None and math.isfinite(x)] if a else []
    stats = {
        "points": len(v),
        "duration_s": float(t_s[-1] - t_s[0]) if len(t_s) > 1 else 0.0,
        "sample_rate_hz": meta.sample_rate_hz,
        "total_samples": meta.total_samples,
        "capture_duration_s": meta.total_samples / meta.sample_rate_hz,
        "step_edges": len(step_edges),
        "signal_config": signal_config,
        "positive_points": sum(1 for x in v if x > 0),
        "negative_points": sum(1 for x in v if x < 0),
        "distance_steps_end": int(dist_steps[-1]) if dist_steps else 0,
        "speed_steps_per_s": {
            "min": float(min(v)),
            "max": float(max(v)),
            "mean": float(statistics.fmean(v)),
            "median": float(statistics.median(v)),
        },
        "speed_abs_steps_per_s": {
            "min": float(min(abs_v)),
            "max": float(max(abs_v)),
            "mean": float(statistics.fmean(abs_v)),
            "median": float(statistics.median(abs_v)),
        },
        "accel_abs_steps_per_s2": None
        if not abs_a
        else {
            "max": float(max(abs_a)),
            "p95": float(statistics.quantiles(abs_a, n=20)[18]) if len(abs_a) >= 20 else float(max(abs_a)),
        },
    }
    if filter_info:
        stats["filter"] = filter_info
    return stats


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="Parse DSLogic .dsl (dir/step) and render speed curve as HTML.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    ap.add_argument("dsl", type=pathlib.Path, help="Input .dsl capture file")
    ap.add_argument("--dir-probe", type=int, default=0, help="DIR probe index (channel 0). Use -1 to disable DIR.")
    ap.add_argument("--step-probe", type=int, default=1, help="STEP probe index (channel 1)")
    ap.add_argument("--dir-high-positive", action="store_true", default=True, help="DIR=1 means positive speed")
    ap.add_argument(
        "--dir-low-positive",
        action="store_true",
        default=False,
        help="DIR=0 means positive speed (invert sign)",
    )
    ap.add_argument(
        "--dir-sample-offset",
        type=int,
        default=-1,
        help="Sample DIR at (step_edge + offset). Use -1 to sample just before the selected STEP edge.",
    )
    ap.add_argument(
        "--no-dir",
        action="store_true",
        default=False,
        help="Do not sample DIR. Treat all motion as positive direction.",
    )
    ap.add_argument(
        "--step-edge",
        choices=[STEP_EDGE_RISING, STEP_EDGE_FALLING],
        default=STEP_EDGE_RISING,
        help="Which STEP edge is treated as the effective step event.",
    )
    ap.add_argument(
        "--smooth",
        type=int,
        default=0,
        help="Legacy manual moving-average window. <=1 means use automatic robust filtering.",
    )
    ap.add_argument(
        "--steps-per-rev",
        type=float,
        default=0.0,
        help="If set (>0), also compute RPM stats. Physical conversion only affects stats text, not the plot.",
    )
    ap.add_argument(
        "--mm-per-rev",
        type=float,
        default=0.0,
        help="If set (>0) and steps-per-rev>0, also compute mm/s stats (linear stage).",
    )
    ap.add_argument("--out-html", type=pathlib.Path, default=None, help="Output HTML path")
    ap.add_argument("--out-csv", type=pathlib.Path, default=None, help="Output CSV path")

    args = ap.parse_args(argv)

    dsl_path: pathlib.Path = args.dsl
    if not dsl_path.exists():
        raise SystemExit(f"Input not found: {dsl_path}")

    dir_probe = DIR_PROBE_NONE if args.no_dir else args.dir_probe
    dir_high_positive = True
    if args.dir_low_positive and not args.no_dir:
        dir_high_positive = False

    result = analyze_and_write(
        dsl_path=dsl_path,
        dir_probe=dir_probe,
        step_probe=args.step_probe,
        smooth=args.smooth,
        dir_sample_offset=args.dir_sample_offset,
        dir_high_positive=dir_high_positive,
        step_edge=args.step_edge,
        out_html=args.out_html,
        out_csv=args.out_csv,
        steps_per_rev=args.steps_per_rev,
        mm_per_rev=args.mm_per_rev,
    )

    print(f"Wrote: {result.out_html}")
    if result.out_csv is not None:
        print(f"Wrote: {result.out_csv}")
    print(f"DIR: {'固定正向（未使用DIR）' if not dir_probe_enabled(dir_probe) else f'CH{dir_probe}'}")
    print(f"Step {step_edge_label(args.step_edge)}: {result.step_edges}")
    if result.points:
        print(f"Curve points: {result.points}  (edges-1)")
        print(f"Speed abs max: {result.speed_abs_max:.3f} steps/s")
    return 0


@dataclasses.dataclass(frozen=True)
class AnalyzeResult:
    out_html: pathlib.Path
    out_csv: Optional[pathlib.Path]
    step_edges: int
    points: int
    speed_abs_max: float
    stats: dict


def analyze_and_write(
    *,
    dsl_path: pathlib.Path,
    dir_probe: int,
    step_probe: int,
    smooth: int = 0,
    dir_sample_offset: int = -1,
    dir_high_positive: bool = True,
    step_edge: str = STEP_EDGE_RISING,
    out_html: Optional[pathlib.Path] = None,
    out_csv: Optional[pathlib.Path] = None,
    steps_per_rev: float = 0.0,
    mm_per_rev: float = 0.0,
) -> AnalyzeResult:
    """
    Core entrypoint for GUI wrappers. Reads capture, computes curves, and writes HTML.
    CSV is written only when out_csv is explicitly provided.
    """
    step_edge = normalize_step_edge(step_edge)
    meta = read_dsl_meta(dsl_path)
    if dir_probe_enabled(dir_probe) and dir_probe == step_probe:
        raise ValueError("DIR and STEP cannot use the same probe when DIR sampling is enabled.")
    step_bytes = read_probe_bytes(dsl_path, meta, step_probe)
    dir_bytes = read_probe_bytes(dsl_path, meta, dir_probe) if dir_probe_enabled(dir_probe) else None

    step_edges = find_step_edges_lsb_first(step_bytes, step_edge)
    t_s, v_steps_s, dir_bits = compute_speed_curve(
        step_edges,
        dir_bytes,
        sample_rate_hz=meta.sample_rate_hz,
        dir_sample_offset=dir_sample_offset,
        dir_high_positive=dir_high_positive,
    )

    raw_speed_steps_s = list(v_steps_s)
    speed_variants, preferred_variant_key = build_speed_variants(raw_speed_steps_s)

    filter_info: dict
    if smooth and smooth > 1:
        v_steps_s = moving_average(v_steps_s, smooth)
        filter_info = {
            "mode": "moving_average_manual",
            "window": int(smooth),
            "replaced_points": 0,
        }
        speed_variants["manual_moving_avg"] = {
            "label": f"手动移动平均 (窗口={int(smooth)})",
            "short_label": "手动均值",
            "color": "#b388ff",
            "line_width": 1.9,
            "alpha": 1.0,
            "values": list(v_steps_s),
            "default_visible": True,
            "stats": dict(filter_info),
        }
        preferred_variant_key = "manual_moving_avg"
    else:
        v_steps_s = list(speed_variants[preferred_variant_key]["values"])
        filter_info = dict(speed_variants[preferred_variant_key]["stats"])

    accel_variants = build_derivative_variants(
        t_s,
        speed_variants,
        compute_accel_curve,
        unit_label="steps/s^2",
    )
    accel_steps_s2 = compute_accel_curve(t_s, v_steps_s)
    jerk_variants = build_derivative_variants(
        t_s,
        accel_variants,
        compute_jerk_curve,
        unit_label="steps/s^3",
    )
    jerk_steps_s3 = compute_jerk_curve(t_s, accel_steps_s2)
    distance_steps = compute_distance_steps(dir_bits, dir_high_positive)

    stats = _compute_stats(
        t_s,
        v_steps_s,
        accel_steps_s2,
        distance_steps,
        meta,
        step_edges,
        dir_probe if dir_probe_enabled(dir_probe) else None,
        step_probe,
        step_edge,
        dir_high_positive,
        dir_sample_offset,
        filter_info,
    )
    stats["speed_variants"] = {
        key: {
            "label": variant["label"],
            "default_visible": bool(variant["default_visible"]),
            "stats": variant["stats"],
        }
        for key, variant in speed_variants.items()
    }
    stats["preferred_speed_variant"] = preferred_variant_key
    stats["accel_variants"] = {
        key: {
            "label": variant["label"],
            "default_visible": bool(variant["default_visible"]),
            "stats": variant["stats"],
        }
        for key, variant in accel_variants.items()
    }
    stats["jerk_variants"] = {
        key: {
            "label": variant["label"],
            "default_visible": bool(variant["default_visible"]),
            "stats": variant["stats"],
        }
        for key, variant in jerk_variants.items()
    }
    jerk_abs = [abs(x) for x in jerk_steps_s3 if x is not None and math.isfinite(x)]
    stats["jerk_abs_steps_per_s3"] = None if not jerk_abs else {
        "max": float(max(jerk_abs)),
        "p95": float(statistics.quantiles(jerk_abs, n=20)[18]) if len(jerk_abs) >= 20 else float(max(jerk_abs)),
    }
    if t_s and steps_per_rev and steps_per_rev > 0:
        rpm = [x / steps_per_rev * 60.0 for x in v_steps_s]
        stats["rpm"] = {
            "min": float(min(rpm)),
            "max": float(max(rpm)),
            "mean": float(statistics.fmean(rpm)),
            "median": float(statistics.median(rpm)),
        }
        if mm_per_rev and mm_per_rev > 0:
            mm_s = [x / steps_per_rev * mm_per_rev for x in v_steps_s]
            stats["mm_per_s"] = {
                "min": float(min(mm_s)),
                "max": float(max(mm_s)),
                "mean": float(statistics.fmean(mm_s)),
                "median": float(statistics.median(mm_s)),
            }

    if out_html is None:
        base = dsl_path.with_suffix("")
        out_html = base.with_name(base.name + "_speed.html")
    title = dsl_path.name + " 速度/加速度/加加速度/距离曲线"
    page = _make_html(title, t_s, speed_variants, accel_variants, jerk_variants, distance_steps, meta, stats)
    out_html.write_text(page, encoding="utf-8")
    if out_csv is not None:
        _write_csv(out_csv, t_s, v_steps_s, accel_steps_s2, jerk_steps_s3, distance_steps, dir_bits)

    speed_abs_max = max((abs(x) for x in v_steps_s), default=0.0)
    return AnalyzeResult(
        out_html=out_html,
        out_csv=out_csv,
        step_edges=len(step_edges),
        points=len(t_s),
        speed_abs_max=float(speed_abs_max),
        stats=stats,
    )


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""bootc-report: Generate an HTML report from bootc-bench results.

Reads the JSON output from bootc_bench.py and produces a self-contained
HTML report with summary tables, timing charts, and CPU/memory time-series.
Supports both baseline (registry pull) and delta (oci-delta) benchmark modes.
"""

import argparse
import html
import json
import os
import re
import sys
import textwrap
from datetime import datetime
from pathlib import Path


def load_results(path: str) -> dict:
    """Load and validate the results JSON."""
    with open(path) as f:
        data = json.load(f)
    if "benchmarks" not in data:
        raise ValueError("Invalid results file: missing 'benchmarks' key")
    return data


def format_bytes(b: int | float | None) -> str:
    """Human-readable byte size."""
    if b is None:
        return "N/A"
    b = float(b)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(b) < 1024:
            return f"{b:.1f} {unit}"
        b /= 1024
    return f"{b:.1f} PB"


def format_duration(sec: float | None) -> str:
    """Human-readable duration."""
    if sec is None:
        return "N/A"
    if sec < 60:
        return f"{sec:.1f}s"
    minutes = int(sec // 60)
    secs = sec % 60
    return f"{minutes}m {secs:.1f}s"


def short_image_ref(ref: str) -> str:
    """Shorten an image reference for display."""
    parts = ref.rsplit("/", 1)
    return parts[-1] if len(parts) > 1 else ref


def get_mode(bench: dict) -> str:
    """Get the benchmark mode for a benchmark entry."""
    return bench.get("mode", "baseline")


def has_delta_benchmarks(data: dict) -> bool:
    """Check if any benchmarks use delta mode."""
    return any(get_mode(b) == "delta" for b in data["benchmarks"])


def has_baseline_benchmarks(data: dict) -> bool:
    """Check if any benchmarks use baseline mode."""
    return any(get_mode(b) == "baseline" for b in data["benchmarks"])


def build_summary_table(data: dict) -> str:
    """Build the HTML summary comparison table."""
    show_delta = has_delta_benchmarks(data)
    rows = []
    for bench in data["benchmarks"]:
        target = bench["target_image"]
        mode = get_mode(bench)
        upgrade_type = bench.get("upgrade_type", "y-stream")
        variant = bench.get("variant", "vanilla")

        upgrade_badge = (
            '<span class="badge badge-zstream">z-stream</span>'
            if upgrade_type == "z-stream"
            else '<span class="badge badge-ystream">y-stream</span>'
        )
        variant_badge = (
            '<span class="badge badge-customized">customized</span>'
            if variant == "customized"
            else '<span class="badge badge-vanilla">vanilla</span>'
        )
        summary = bench.get("summary", {})
        layer_info = bench.get("target_layer_info", {})
        delta_arts = bench.get("delta_artifacts") or {}

        stage = summary.get("stage_duration_sec", {})
        reboot = summary.get("reboot_duration_sec", {})
        total = summary.get("total_duration_sec", {})
        transfer = summary.get("delta_transfer_duration_sec", {})
        apply_ = summary.get("delta_apply_duration_sec", {})

        success = summary.get("successful_iterations", 0)
        failed = summary.get("failed_iterations", 0)

        mode_badge = (
            '<span class="badge badge-delta">delta</span>'
            if mode == "delta"
            else '<span class="badge badge-baseline">baseline</span>'
        )

        # Download size: for delta mode show delta size, for baseline show image size
        if mode == "delta" and delta_arts.get("delta_size_bytes"):
            dl_size = format_bytes(delta_arts["delta_size_bytes"])
        else:
            dl_size = format_bytes(layer_info.get("total_compressed_size_bytes"))

        # Delta-specific columns
        delta_cols = ""
        if show_delta:
            if mode == "delta":
                delta_cols = f"""
            <td>{format_duration(transfer.get('mean'))}<br>
                <small>±{transfer.get('stddev', 0):.1f}s</small></td>
            <td>{format_duration(apply_.get('mean'))}<br>
                <small>±{apply_.get('stddev', 0):.1f}s</small></td>"""
            else:
                delta_cols = """
            <td class="na">—</td>
            <td class="na">—</td>"""

        rows.append(f"""
        <tr>
            <td>{html.escape(short_image_ref(target))}</td>
            <td>{upgrade_badge}</td>
            <td>{variant_badge}</td>
            <td>{mode_badge}</td>
            {delta_cols}
            <td>{format_duration(stage.get('mean'))}<br>
                <small>±{stage.get('stddev', 0):.1f}s</small></td>
            <td>{format_duration(reboot.get('mean'))}<br>
                <small>±{reboot.get('stddev', 0):.1f}s</small></td>
            <td>{format_duration(total.get('mean'))}<br>
                <small>±{total.get('stddev', 0):.1f}s</small></td>
            <td>{layer_info.get('layer_count', 'N/A')}</td>
            <td>{dl_size}</td>
            <td>{success}/{success + failed}</td>
        </tr>""")

    delta_headers = ""
    if show_delta:
        delta_headers = """
                <th>Transfer (mean)</th>
                <th>Apply (mean)</th>"""

    return f"""
    <table>
        <thead>
            <tr>
                <th>Target</th>
                <th>Upgrade</th>
                <th>Variant</th>
                <th>Mode</th>
                {delta_headers}
                <th>Stage (mean)</th>
                <th>Reboot (mean)</th>
                <th>Total (mean)</th>
                <th>Layers</th>
                <th>Download Size</th>
                <th>Success</th>
            </tr>
        </thead>
        <tbody>
            {''.join(rows)}
        </tbody>
    </table>"""


def build_delta_artifacts_section(bench: dict) -> str:
    """Build a section showing delta preparation artifacts."""
    delta_arts = bench.get("delta_artifacts")
    if not delta_arts:
        return ""

    old_size = delta_arts.get("old_archive_size_bytes", 0)
    new_size = delta_arts.get("new_archive_size_bytes", 0)
    delta_size = delta_arts.get("delta_size_bytes", 0)
    ratio = (delta_size / new_size * 100) if new_size else 0
    savings = new_size - delta_size if new_size and delta_size else 0

    return f"""
    <div class="artifacts-box">
        <h4>Delta Artifacts</h4>
        <div class="artifacts-grid">
            <div class="artifact-item">
                <span class="artifact-label">Base Archive</span>
                <span class="artifact-value">{format_bytes(old_size)}</span>
                <small>{format_duration(delta_arts.get('old_export_duration_sec'))} to export</small>
            </div>
            <div class="artifact-item">
                <span class="artifact-label">Target Archive</span>
                <span class="artifact-value">{format_bytes(new_size)}</span>
                <small>{format_duration(delta_arts.get('new_export_duration_sec'))} to export</small>
            </div>
            <div class="artifact-item highlight">
                <span class="artifact-label">Delta File</span>
                <span class="artifact-value">{format_bytes(delta_size)}</span>
                <small>{format_duration(delta_arts.get('delta_create_duration_sec'))} to create</small>
            </div>
            <div class="artifact-item">
                <span class="artifact-label">Compression Ratio</span>
                <span class="artifact-value">{ratio:.1f}%</span>
                <small>{format_bytes(savings)} saved vs full image</small>
            </div>
        </div>
    </div>"""


def build_iteration_table(bench: dict) -> str:
    """Build a per-iteration detail table for a single target."""
    mode = get_mode(bench)
    is_delta = mode == "delta"
    rows = []
    for it in bench.get("iterations", []):
        stage = it.get("stage_phase") or {}
        reboot = it.get("reboot_phase") or {}
        parsed = it.get("bootc_parsed") or {}

        delta_cols = ""
        if is_delta:
            transfer = it.get("delta_transfer_phase") or {}
            apply_ = it.get("delta_apply_phase") or {}
            delta_cols = f"""
            <td>{format_duration(transfer.get('duration_sec'))}</td>
            <td>{format_duration(apply_.get('duration_sec'))}</td>
            <td>{format_bytes(it.get('delta_file_size_bytes'))}</td>"""

        rows.append(f"""
        <tr>
            <td>{it.get('iteration', '?')}</td>
            {delta_cols}
            <td>{format_duration(stage.get('duration_sec'))}</td>
            <td>{format_duration(reboot.get('duration_sec'))}</td>
            <td>{format_duration(it.get('total_duration_sec'))}</td>
            <td>{html.escape(str(parsed.get('download_size_human', 'N/A')))}</td>
            <td>{format_duration(parsed.get('deploy_duration_sec'))}</td>
            <td>{format_bytes(it.get('disk_delta_bytes'))}</td>
            <td>{'✅' if it.get('error') is None else '❌'}</td>
        </tr>""")

    delta_headers = ""
    if is_delta:
        delta_headers = """
                <th>Transfer</th>
                <th>Apply</th>
                <th>Delta Size</th>"""

    return f"""
    <table>
        <thead>
            <tr>
                <th>Iter</th>
                {delta_headers}
                <th>Stage</th>
                <th>Reboot</th>
                <th>Total</th>
                <th>Download</th>
                <th>Deploy</th>
                <th>Disk Δ</th>
                <th>Status</th>
            </tr>
        </thead>
        <tbody>
            {''.join(rows)}
        </tbody>
    </table>"""


def build_chart_data(data: dict) -> dict:
    """Prepare data structures for Chart.js charts."""
    charts = {
        "timing_comparison": {
            "labels": [],
            "modes": [],
            "stage_means": [],
            "stage_errs": [],
            "reboot_means": [],
            "reboot_errs": [],
            "transfer_means": [],
            "transfer_errs": [],
            "apply_means": [],
            "apply_errs": [],
        },
        "per_target": {},
    }

    for bench in data["benchmarks"]:
        mode = get_mode(bench)
        label = short_image_ref(bench["target_image"])
        upgrade_type = bench.get("upgrade_type", "y-stream")
        variant = bench.get("variant", "vanilla")
        label_suffix_parts = []
        if upgrade_type == "z-stream":
            label_suffix_parts.append("z")
        if variant == "customized":
            label_suffix_parts.append("custom")
        if mode == "delta":
            label_suffix_parts.append("delta")
        if label_suffix_parts:
            label += " (" + ", ".join(label_suffix_parts) + ")"
        summary = bench.get("summary", {})
        stage = summary.get("stage_duration_sec", {})
        reboot = summary.get("reboot_duration_sec", {})
        transfer = summary.get("delta_transfer_duration_sec", {})
        apply_ = summary.get("delta_apply_duration_sec", {})

        charts["timing_comparison"]["labels"].append(label)
        charts["timing_comparison"]["modes"].append(mode)
        charts["timing_comparison"]["stage_means"].append(stage.get("mean", 0))
        charts["timing_comparison"]["stage_errs"].append(stage.get("stddev", 0))
        charts["timing_comparison"]["reboot_means"].append(reboot.get("mean", 0))
        charts["timing_comparison"]["reboot_errs"].append(reboot.get("stddev", 0))
        charts["timing_comparison"]["transfer_means"].append(transfer.get("mean", 0))
        charts["timing_comparison"]["transfer_errs"].append(transfer.get("stddev", 0))
        charts["timing_comparison"]["apply_means"].append(apply_.get("mean", 0))
        charts["timing_comparison"]["apply_errs"].append(apply_.get("stddev", 0))

        # Per-iteration timing
        iter_data = {
            "mode": mode,
            "stages": [], "reboots": [], "totals": [], "iters": [],
            "transfers": [], "applies": [],
        }
        for it in bench.get("iterations", []):
            sp = it.get("stage_phase") or {}
            rp = it.get("reboot_phase") or {}
            tp = it.get("delta_transfer_phase") or {}
            ap = it.get("delta_apply_phase") or {}
            iter_data["iters"].append(it.get("iteration", 0))
            iter_data["stages"].append(sp.get("duration_sec", 0))
            iter_data["reboots"].append(rp.get("duration_sec", 0))
            iter_data["totals"].append(it.get("total_duration_sec", 0))
            iter_data["transfers"].append(tp.get("duration_sec", 0))
            iter_data["applies"].append(ap.get("duration_sec", 0))

        # CPU/memory time series from first successful iteration
        cpu_ts = []
        mem_ts = []
        for it in bench.get("iterations", []):
            if it.get("error") is not None:
                continue
            samples = it.get("cpu_samples", [])
            if samples:
                t0 = samples[0].get("timestamp", 0)
                cpu_ts = [
                    {"x": round(s["timestamp"] - t0, 1),
                     "y": s.get("cpu_time_ns", 0) / 1e9}
                    for s in samples
                ]
                mem_ts = [
                    {"x": round(s["timestamp"] - t0, 1),
                     "y": s.get("memory_rss_kb", 0) / 1024}
                    for s in samples
                ]
            break

        iter_data["cpu_ts"] = cpu_ts
        iter_data["mem_ts"] = mem_ts
        charts["per_target"][label] = iter_data

    # Delta size comparison data (for delta benchmarks)
    delta_size_data = {"labels": [], "full_sizes": [], "delta_sizes": []}
    for bench in data["benchmarks"]:
        if get_mode(bench) != "delta":
            continue
        delta_arts = bench.get("delta_artifacts") or {}
        if not delta_arts:
            continue
        label = short_image_ref(bench["target_image"])
        delta_size_data["labels"].append(label)
        delta_size_data["full_sizes"].append(
            round(delta_arts.get("new_archive_size_bytes", 0) / 1024**2, 1)
        )
        delta_size_data["delta_sizes"].append(
            round(delta_arts.get("delta_size_bytes", 0) / 1024**2, 1)
        )
    charts["delta_size"] = delta_size_data

    return charts


def generate_html(data: dict) -> str:
    """Generate the full HTML report."""
    config = data.get("config", {})
    chart_data = build_chart_data(data)
    show_delta = has_delta_benchmarks(data)
    mode = config.get("mode", "baseline")

    # Build per-target detail sections
    target_sections = []
    for bench in data["benchmarks"]:
        bench_mode = get_mode(bench)
        label = short_image_ref(bench["target_image"])
        chart_upgrade_type = bench.get("upgrade_type", "y-stream")
        chart_variant = bench.get("variant", "vanilla")
        label_suffix_parts = []
        if chart_upgrade_type == "z-stream":
            label_suffix_parts.append("z")
        if chart_variant == "customized":
            label_suffix_parts.append("custom")
        if bench_mode == "delta":
            label_suffix_parts.append("delta")
        chart_label = label
        if label_suffix_parts:
            chart_label += " (" + ", ".join(label_suffix_parts) + ")"
        safe_id = re.sub(r'[^a-zA-Z0-9_-]', '-', chart_label)
        iter_table = build_iteration_table(bench)

        # Delta artifacts
        artifacts_html = build_delta_artifacts_section(bench)

        # Error details
        errors = []
        for it in bench.get("iterations", []):
            if it.get("error"):
                errors.append(
                    f"<li>Iteration {it['iteration']}: "
                    f"<code>{html.escape(str(it['error']))}</code></li>"
                )
        error_html = ""
        if errors:
            error_html = f"""
            <div class="error-box">
                <strong>Errors:</strong>
                <ul>{''.join(errors)}</ul>
            </div>"""

        mode_badge = (
            '<span class="badge badge-delta">delta</span>'
            if bench_mode == "delta"
            else '<span class="badge badge-baseline">baseline</span>'
        )

        bench_upgrade_type = bench.get("upgrade_type", "y-stream")
        bench_variant = bench.get("variant", "vanilla")
        upgrade_badge = (
            '<span class="badge badge-zstream">z-stream</span>'
            if bench_upgrade_type == "z-stream"
            else '<span class="badge badge-ystream">y-stream</span>'
        )
        variant_badge = (
            '<span class="badge badge-customized">customized</span>'
            if bench_variant == "customized"
            else '<span class="badge badge-vanilla">vanilla</span>'
        )

        target_sections.append(f"""
        <div class="target-section" id="target-{safe_id}">
            <h3>→ {html.escape(label)} {mode_badge} {upgrade_badge} {variant_badge}</h3>
            {artifacts_html}
            {error_html}
            {iter_table}
            <div class="chart-row">
                <div class="chart-container">
                    <canvas id="iter-chart-{safe_id}"></canvas>
                </div>
                <div class="chart-container">
                    <canvas id="mem-chart-{safe_id}"></canvas>
                </div>
            </div>
        </div>""")

    summary_table = build_summary_table(data)

    # Delta size chart section
    delta_size_chart_html = ""
    if show_delta:
        delta_size_chart_html = """
        <div class="main-chart">
            <canvas id="delta-size-chart"></canvas>
        </div>"""

    mode_desc = {
        "baseline": "Registry Pull (baseline)",
        "delta": "OCI Delta",
    }.get(mode, mode)

    extra_config_lines = ""
    base_stream = config.get('base_stream')
    if base_stream:
        extra_config_lines += f"<br><strong>Base stream:</strong> <code>{html.escape(base_stream)}</code>"
        zt = config.get('zstream_target')
        if zt:
            extra_config_lines += f"<br><strong>Z-stream target:</strong> <code>{html.escape(zt)}</code>"
    pkgs = config.get('packages')
    if pkgs:
        extra_config_lines += f"<br><strong>Packages:</strong> <code>{html.escape(', '.join(pkgs))}</code>"

    return textwrap.dedent(f"""\
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>bootc-bench Report</title>
        <script src="https://cdn.jsdelivr.net/npm/chart.js@4"></script>
        <style>
            :root {{
                --bg: #0d1117;
                --surface: #161b22;
                --border: #30363d;
                --text: #e6edf3;
                --text-muted: #8b949e;
                --accent: #58a6ff;
                --green: #3fb950;
                --orange: #d29922;
                --red: #f85149;
                --purple: #bc8cff;
                --cyan: #39d2c0;
            }}
            * {{ box-sizing: border-box; margin: 0; padding: 0; }}
            body {{
                font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif;
                background: var(--bg);
                color: var(--text);
                line-height: 1.6;
                padding: 2rem;
                max-width: 1200px;
                margin: 0 auto;
            }}
            h1 {{ color: var(--accent); margin-bottom: 0.5rem; }}
            h2 {{ color: var(--text); margin: 2rem 0 1rem; border-bottom: 1px solid var(--border); padding-bottom: 0.5rem; }}
            h3 {{ color: var(--accent); margin: 1.5rem 0 0.75rem; }}
            h4 {{ color: var(--text-muted); margin: 0.75rem 0 0.5rem; font-size: 0.95rem; }}
            .subtitle {{ color: var(--text-muted); margin-bottom: 2rem; }}
            .config-box {{
                background: var(--surface);
                border: 1px solid var(--border);
                border-radius: 6px;
                padding: 1rem 1.5rem;
                margin-bottom: 2rem;
                font-size: 0.9rem;
            }}
            .config-box code {{ color: var(--accent); }}
            .badge {{
                display: inline-block;
                padding: 0.15em 0.55em;
                border-radius: 10px;
                font-size: 0.75rem;
                font-weight: 600;
                text-transform: uppercase;
                letter-spacing: 0.05em;
                vertical-align: middle;
            }}
            .badge-baseline {{
                background: rgba(88, 166, 255, 0.15);
                color: var(--accent);
                border: 1px solid rgba(88, 166, 255, 0.3);
            }}
            .badge-delta {{
                background: rgba(57, 210, 192, 0.15);
                color: var(--cyan);
                border: 1px solid rgba(57, 210, 192, 0.3);
            }}
            .badge-zstream {{
                background: rgba(210, 153, 34, 0.15);
                color: var(--orange);
                border: 1px solid rgba(210, 153, 34, 0.3);
            }}
            .badge-ystream {{
                background: rgba(188, 140, 255, 0.15);
                color: var(--purple);
                border: 1px solid rgba(188, 140, 255, 0.3);
            }}
            .badge-vanilla {{
                background: rgba(139, 148, 158, 0.15);
                color: var(--text-muted);
                border: 1px solid rgba(139, 148, 158, 0.3);
            }}
            .badge-customized {{
                background: rgba(63, 185, 80, 0.15);
                color: var(--green);
                border: 1px solid rgba(63, 185, 80, 0.3);
            }}
            table {{
                width: 100%;
                border-collapse: collapse;
                margin: 1rem 0;
                background: var(--surface);
                border-radius: 6px;
                overflow: hidden;
            }}
            th, td {{
                padding: 0.75rem 1rem;
                text-align: left;
                border-bottom: 1px solid var(--border);
            }}
            th {{
                background: rgba(88, 166, 255, 0.1);
                color: var(--accent);
                font-weight: 600;
                font-size: 0.85rem;
                text-transform: uppercase;
                letter-spacing: 0.05em;
            }}
            td {{ font-size: 0.95rem; }}
            td small {{ color: var(--text-muted); }}
            td.na {{ color: var(--text-muted); text-align: center; }}
            tr:last-child td {{ border-bottom: none; }}
            tr:hover td {{ background: rgba(255, 255, 255, 0.03); }}
            .chart-row {{
                display: grid;
                grid-template-columns: 1fr 1fr;
                gap: 1.5rem;
                margin: 1.5rem 0;
            }}
            .chart-container {{
                background: var(--surface);
                border: 1px solid var(--border);
                border-radius: 6px;
                padding: 1rem;
            }}
            .main-chart {{
                background: var(--surface);
                border: 1px solid var(--border);
                border-radius: 6px;
                padding: 1.5rem;
                margin: 1.5rem 0;
            }}
            .error-box {{
                background: rgba(248, 81, 73, 0.1);
                border: 1px solid var(--red);
                border-radius: 6px;
                padding: 0.75rem 1rem;
                margin: 0.5rem 0;
                font-size: 0.9rem;
            }}
            .error-box code {{ color: var(--red); font-size: 0.85rem; word-break: break-all; }}
            .target-section {{
                margin-bottom: 2rem;
                padding-bottom: 2rem;
                border-bottom: 1px solid var(--border);
            }}
            .artifacts-box {{
                background: var(--surface);
                border: 1px solid var(--border);
                border-radius: 6px;
                padding: 1rem 1.5rem;
                margin: 1rem 0;
            }}
            .artifacts-grid {{
                display: grid;
                grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
                gap: 1rem;
                margin-top: 0.75rem;
            }}
            .artifact-item {{
                display: flex;
                flex-direction: column;
                padding: 0.75rem;
                border-radius: 6px;
                background: rgba(255, 255, 255, 0.02);
                border: 1px solid var(--border);
            }}
            .artifact-item.highlight {{
                background: rgba(57, 210, 192, 0.08);
                border-color: rgba(57, 210, 192, 0.3);
            }}
            .artifact-label {{
                font-size: 0.8rem;
                color: var(--text-muted);
                text-transform: uppercase;
                letter-spacing: 0.05em;
                margin-bottom: 0.25rem;
            }}
            .artifact-value {{
                font-size: 1.25rem;
                font-weight: 600;
                color: var(--text);
            }}
            .artifact-item small {{
                color: var(--text-muted);
                font-size: 0.8rem;
                margin-top: 0.25rem;
            }}
            .footer {{
                margin-top: 3rem;
                padding-top: 1rem;
                border-top: 1px solid var(--border);
                color: var(--text-muted);
                font-size: 0.85rem;
            }}
            @media (max-width: 768px) {{
                .chart-row {{ grid-template-columns: 1fr; }}
                .artifacts-grid {{ grid-template-columns: 1fr 1fr; }}
                body {{ padding: 1rem; }}
            }}
        </style>
    </head>
    <body>
        <h1>bootc-bench Report</h1>
        <p class="subtitle">Generated {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</p>

        <div class="config-box">
            <strong>Base image:</strong> <code>{html.escape(config.get('base_image', 'N/A'))}</code>{extra_config_lines}<br>
            <strong>Mode:</strong> <span class="badge badge-{'delta' if mode == 'delta' else 'baseline'}">{mode}</span><br>
            <strong>Iterations:</strong> {config.get('iterations', 'N/A')} per target<br>
            <strong>VM:</strong> {config.get('vm_vcpus', '?')} vCPUs, {config.get('vm_memory_mb', '?')} MB RAM<br>
            <strong>Run date:</strong> {config.get('timestamp', 'N/A')[:19]}
        </div>

        <h2>Summary</h2>
        {summary_table}

        <div class="main-chart">
            <canvas id="timing-comparison-chart"></canvas>
        </div>

        {delta_size_chart_html}

        <h2>Per-Target Details</h2>
        {''.join(target_sections)}

        <div class="footer">
            Generated by <strong>bootc-bench</strong> &middot;
            Source data: results.json
        </div>

        <script>
        const chartData = {json.dumps(chart_data).replace("</", "<\\/")};
        const hasDelta = {json.dumps(show_delta)};

        Chart.defaults.color = '#8b949e';
        Chart.defaults.borderColor = '#30363d';

        // --- Main timing comparison bar chart ---
        (() => {{
            const datasets = [];

            // If any delta benchmarks exist, show transfer + apply phases
            const hasAnyTransfer = chartData.timing_comparison.transfer_means.some(v => v > 0);
            const hasAnyApply = chartData.timing_comparison.apply_means.some(v => v > 0);

            if (hasAnyTransfer) {{
                datasets.push({{
                    label: 'Transfer (delta)',
                    data: chartData.timing_comparison.transfer_means,
                    backgroundColor: 'rgba(57, 210, 192, 0.7)',
                    borderColor: 'rgba(57, 210, 192, 1)',
                    borderWidth: 1,
                }});
            }}
            if (hasAnyApply) {{
                datasets.push({{
                    label: 'Apply (delta)',
                    data: chartData.timing_comparison.apply_means,
                    backgroundColor: 'rgba(210, 153, 34, 0.7)',
                    borderColor: 'rgba(210, 153, 34, 1)',
                    borderWidth: 1,
                }});
            }}

            datasets.push(
                {{
                    label: 'Stage (pull + deploy)',
                    data: chartData.timing_comparison.stage_means,
                    backgroundColor: 'rgba(88, 166, 255, 0.7)',
                    borderColor: 'rgba(88, 166, 255, 1)',
                    borderWidth: 1,
                }},
                {{
                    label: 'Reboot',
                    data: chartData.timing_comparison.reboot_means,
                    backgroundColor: 'rgba(63, 185, 80, 0.7)',
                    borderColor: 'rgba(63, 185, 80, 1)',
                    borderWidth: 1,
                }}
            );

            new Chart(document.getElementById('timing-comparison-chart'), {{
                type: 'bar',
                data: {{
                    labels: chartData.timing_comparison.labels,
                    datasets: datasets,
                }},
                options: {{
                    responsive: true,
                    plugins: {{
                        title: {{
                            display: true,
                            text: 'Upgrade Timing Comparison (mean)',
                            color: '#e6edf3',
                            font: {{ size: 16 }},
                        }},
                        tooltip: {{
                            callbacks: {{
                                afterLabel: function(ctx) {{
                                    const errArrays = [
                                        chartData.timing_comparison.transfer_errs,
                                        chartData.timing_comparison.apply_errs,
                                        chartData.timing_comparison.stage_errs,
                                        chartData.timing_comparison.reboot_errs,
                                    ];
                                    // Map dataset index to the right error array
                                    // datasets are: [transfer?], [apply?], stage, reboot
                                    let errIdx = ctx.datasetIndex;
                                    if (!hasAnyTransfer) errIdx += 1;
                                    if (!hasAnyApply) errIdx += 1;
                                    const errs = errArrays[errIdx];
                                    if (errs) {{
                                        return '±' + errs[ctx.dataIndex].toFixed(1) + 's';
                                    }}
                                    return '';
                                }}
                            }}
                        }}
                    }},
                    scales: {{
                        x: {{ stacked: true }},
                        y: {{
                            stacked: true,
                            title: {{ display: true, text: 'Duration (seconds)' }},
                        }}
                    }}
                }}
            }});
        }})();

        // --- Delta size comparison chart ---
        if (hasDelta && chartData.delta_size && chartData.delta_size.labels.length > 0) {{
            new Chart(document.getElementById('delta-size-chart'), {{
                type: 'bar',
                data: {{
                    labels: chartData.delta_size.labels,
                    datasets: [
                        {{
                            label: 'Full Image (MB)',
                            data: chartData.delta_size.full_sizes,
                            backgroundColor: 'rgba(88, 166, 255, 0.5)',
                            borderColor: 'rgba(88, 166, 255, 1)',
                            borderWidth: 1,
                        }},
                        {{
                            label: 'Delta File (MB)',
                            data: chartData.delta_size.delta_sizes,
                            backgroundColor: 'rgba(57, 210, 192, 0.5)',
                            borderColor: 'rgba(57, 210, 192, 1)',
                            borderWidth: 1,
                        }}
                    ]
                }},
                options: {{
                    responsive: true,
                    plugins: {{
                        title: {{
                            display: true,
                            text: 'Delta vs Full Image Size',
                            color: '#e6edf3',
                            font: {{ size: 16 }},
                        }}
                    }},
                    scales: {{
                        y: {{
                            title: {{ display: true, text: 'Size (MB)' }},
                        }}
                    }}
                }}
            }});
        }}

        // --- Per-target charts ---
        for (const [label, tdata] of Object.entries(chartData.per_target)) {{
            const safeId = label.replace(/:/g, '-').replace(/\\//g, '-')
                               .replace(/ /g, '-').replace(/[()]/g, '');
            const isDelta = tdata.mode === 'delta';

            // Iteration timing chart
            const iterDatasets = [];
            const hasTransfers = tdata.transfers && tdata.transfers.some(v => v > 0);
            const hasApplies = tdata.applies && tdata.applies.some(v => v > 0);

            if (isDelta && hasTransfers) {{
                iterDatasets.push({{
                    label: 'Transfer',
                    data: tdata.transfers,
                    backgroundColor: 'rgba(57, 210, 192, 0.7)',
                    borderColor: 'rgba(57, 210, 192, 1)',
                    borderWidth: 1,
                }});
            }}
            if (isDelta && hasApplies) {{
                iterDatasets.push({{
                    label: 'Apply',
                    data: tdata.applies,
                    backgroundColor: 'rgba(210, 153, 34, 0.7)',
                    borderColor: 'rgba(210, 153, 34, 1)',
                    borderWidth: 1,
                }});
            }}
            iterDatasets.push(
                {{
                    label: 'Stage',
                    data: tdata.stages,
                    backgroundColor: 'rgba(88, 166, 255, 0.7)',
                    borderColor: 'rgba(88, 166, 255, 1)',
                    borderWidth: 1,
                }},
                {{
                    label: 'Reboot',
                    data: tdata.reboots,
                    backgroundColor: 'rgba(63, 185, 80, 0.7)',
                    borderColor: 'rgba(63, 185, 80, 1)',
                    borderWidth: 1,
                }}
            );

            const iterCanvas = document.getElementById('iter-chart-' + safeId);
            if (iterCanvas) {{
                new Chart(iterCanvas, {{
                    type: 'bar',
                    data: {{
                        labels: tdata.iters.map(i => 'Iter ' + i),
                        datasets: iterDatasets,
                    }},
                    options: {{
                        responsive: true,
                        plugins: {{
                            title: {{
                                display: true,
                                text: 'Per-Iteration Timing',
                                color: '#e6edf3',
                            }}
                        }},
                        scales: {{
                            x: {{ stacked: true }},
                            y: {{
                                stacked: true,
                                title: {{ display: true, text: 'Seconds' }},
                            }}
                        }}
                    }}
                }});
            }}

            // Memory time series
            const memCanvas = document.getElementById('mem-chart-' + safeId);
            if (memCanvas && tdata.mem_ts && tdata.mem_ts.length > 0) {{
                new Chart(memCanvas, {{
                    type: 'line',
                    data: {{
                        datasets: [{{
                            label: 'Memory RSS (MB)',
                            data: tdata.mem_ts,
                            borderColor: 'rgba(188, 140, 255, 0.8)',
                            backgroundColor: 'rgba(188, 140, 255, 0.1)',
                            fill: true,
                            tension: 0.3,
                            pointRadius: 1,
                        }}]
                    }},
                    options: {{
                        responsive: true,
                        plugins: {{
                            title: {{
                                display: true,
                                text: 'Memory Usage (iteration 1)',
                                color: '#e6edf3',
                            }}
                        }},
                        scales: {{
                            x: {{
                                type: 'linear',
                                title: {{ display: true, text: 'Time (seconds)' }},
                            }},
                            y: {{
                                title: {{ display: true, text: 'RSS (MB)' }},
                            }}
                        }}
                    }}
                }});
            }}
        }}
        </script>
    </body>
    </html>""")


def main():
    parser = argparse.ArgumentParser(
        description="Generate an HTML report from bootc-bench results",
    )
    parser.add_argument(
        "results", nargs="?", default="./bootc-bench-results/results.json",
        help="Path to results.json (default: ./bootc-bench-results/results.json)",
    )
    parser.add_argument(
        "-o", "--output", default=None,
        help="Output HTML file (default: <results-dir>/report.html)",
    )
    args = parser.parse_args()

    results_path = Path(args.results).resolve()
    if not results_path.exists():
        print(f"Error: {results_path} not found", file=sys.stderr)
        sys.exit(1)

    data = load_results(str(results_path))

    if args.output:
        output_path = Path(args.output).resolve()
    else:
        output_path = results_path.parent / "report.html"

    report_html = generate_html(data)
    output_path.write_text(report_html)
    print(f"Report written to {output_path}")


if __name__ == "__main__":
    main()

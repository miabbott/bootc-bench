#!/usr/bin/env python3
"""bootc-report: Generate an HTML report from bootc-bench results.

Reads the JSON output from bootc_bench.py and produces a self-contained
HTML report with summary tables, timing charts, and CPU/memory time-series.
"""

import argparse
import html
import json
import os
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
    # registry.redhat.io/rhel9/rhel-bootc:9.8 -> rhel-bootc:9.8
    parts = ref.rsplit("/", 1)
    return parts[-1] if len(parts) > 1 else ref


def build_summary_table(data: dict) -> str:
    """Build the HTML summary comparison table."""
    config = data.get("config", {})
    rows = []
    for bench in data["benchmarks"]:
        target = bench["target_image"]
        summary = bench.get("summary", {})
        layer_info = bench.get("target_layer_info", {})

        stage = summary.get("stage_duration_sec", {})
        reboot = summary.get("reboot_duration_sec", {})
        total = summary.get("total_duration_sec", {})

        success = summary.get("successful_iterations", 0)
        failed = summary.get("failed_iterations", 0)

        rows.append(f"""
        <tr>
            <td>{html.escape(short_image_ref(target))}</td>
            <td>{format_duration(stage.get('mean'))}<br>
                <small>±{stage.get('stddev', 0):.1f}s</small></td>
            <td>{format_duration(reboot.get('mean'))}<br>
                <small>±{reboot.get('stddev', 0):.1f}s</small></td>
            <td>{format_duration(total.get('mean'))}<br>
                <small>±{total.get('stddev', 0):.1f}s</small></td>
            <td>{layer_info.get('layer_count', 'N/A')}</td>
            <td>{format_bytes(layer_info.get('total_compressed_size_bytes'))}</td>
            <td>{success}/{success + failed}</td>
        </tr>""")

    return f"""
    <table>
        <thead>
            <tr>
                <th>Target</th>
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


def build_iteration_table(bench: dict) -> str:
    """Build a per-iteration detail table for a single target."""
    rows = []
    for it in bench.get("iterations", []):
        stage = it.get("stage_phase", {})
        reboot = it.get("reboot_phase", {})
        parsed = it.get("bootc_parsed", {})

        rows.append(f"""
        <tr>
            <td>{it.get('iteration', '?')}</td>
            <td>{format_duration(stage.get('duration_sec'))}</td>
            <td>{format_duration(reboot.get('duration_sec'))}</td>
            <td>{format_duration(it.get('total_duration_sec'))}</td>
            <td>{parsed.get('download_size_human', 'N/A')}</td>
            <td>{parsed.get('deploy_duration_sec', 'N/A')}s</td>
            <td>{format_bytes(it.get('disk_delta_bytes'))}</td>
            <td>{'✅' if it.get('error') is None else '❌'}</td>
        </tr>""")

    return f"""
    <table>
        <thead>
            <tr>
                <th>Iter</th>
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
            "stage_means": [],
            "stage_errs": [],
            "reboot_means": [],
            "reboot_errs": [],
        },
        "per_target": {},
    }

    for bench in data["benchmarks"]:
        label = short_image_ref(bench["target_image"])
        summary = bench.get("summary", {})
        stage = summary.get("stage_duration_sec", {})
        reboot = summary.get("reboot_duration_sec", {})

        charts["timing_comparison"]["labels"].append(label)
        charts["timing_comparison"]["stage_means"].append(stage.get("mean", 0))
        charts["timing_comparison"]["stage_errs"].append(stage.get("stddev", 0))
        charts["timing_comparison"]["reboot_means"].append(reboot.get("mean", 0))
        charts["timing_comparison"]["reboot_errs"].append(reboot.get("stddev", 0))

        # Per-iteration timing for scatter/line
        iter_data = {"stages": [], "reboots": [], "totals": [], "iters": []}
        for it in bench.get("iterations", []):
            sp = it.get("stage_phase", {})
            rp = it.get("reboot_phase", {})
            iter_data["iters"].append(it.get("iteration", 0))
            iter_data["stages"].append(sp.get("duration_sec", 0))
            iter_data["reboots"].append(rp.get("duration_sec", 0))
            iter_data["totals"].append(it.get("total_duration_sec", 0))

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

    return charts


def generate_html(data: dict) -> str:
    """Generate the full HTML report."""
    config = data.get("config", {})
    chart_data = build_chart_data(data)

    # Build per-target detail sections
    target_sections = []
    for bench in data["benchmarks"]:
        label = short_image_ref(bench["target_image"])
        safe_id = label.replace(":", "-").replace("/", "-")
        iter_table = build_iteration_table(bench)

        # Error details if any
        errors = []
        for it in bench.get("iterations", []):
            if it.get("error"):
                errors.append(
                    f"<li>Iteration {it['iteration']}: "
                    f"<code>{html.escape(it['error'])}</code></li>"
                )
        error_html = ""
        if errors:
            error_html = f"""
            <div class="error-box">
                <strong>Errors:</strong>
                <ul>{''.join(errors)}</ul>
            </div>"""

        target_sections.append(f"""
        <div class="target-section" id="target-{safe_id}">
            <h3>→ {html.escape(label)}</h3>
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
            .error-box code {{ color: var(--red); font-size: 0.85rem; }}
            .target-section {{
                margin-bottom: 2rem;
                padding-bottom: 2rem;
                border-bottom: 1px solid var(--border);
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
                body {{ padding: 1rem; }}
            }}
        </style>
    </head>
    <body>
        <h1>bootc-bench Report</h1>
        <p class="subtitle">Generated {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</p>

        <div class="config-box">
            <strong>Base image:</strong> <code>{html.escape(config.get('base_image', 'N/A'))}</code><br>
            <strong>Iterations:</strong> {config.get('iterations', 'N/A')} per target<br>
            <strong>VM:</strong> {config.get('vm_vcpus', '?')} vCPUs, {config.get('vm_memory_mb', '?')} MB RAM<br>
            <strong>Run date:</strong> {config.get('timestamp', 'N/A')[:19]}
        </div>

        <h2>Summary</h2>
        {summary_table}

        <div class="main-chart">
            <canvas id="timing-comparison-chart"></canvas>
        </div>

        <h2>Per-Target Details</h2>
        {''.join(target_sections)}

        <div class="footer">
            Generated by <strong>bootc-bench</strong> &middot;
            Source data: results.json
        </div>

        <script>
        const chartData = {json.dumps(chart_data)};

        Chart.defaults.color = '#8b949e';
        Chart.defaults.borderColor = '#30363d';

        // --- Main timing comparison bar chart ---
        new Chart(document.getElementById('timing-comparison-chart'), {{
            type: 'bar',
            data: {{
                labels: chartData.timing_comparison.labels,
                datasets: [
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
                ]
            }},
            options: {{
                responsive: true,
                plugins: {{
                    title: {{
                        display: true,
                        text: 'Upgrade Timing Comparison (mean)',
                        color: '#e6edf3',
                        font: {{ size: 16 }}
                    }},
                    tooltip: {{
                        callbacks: {{
                            afterLabel: function(ctx) {{
                                const errs = ctx.datasetIndex === 0
                                    ? chartData.timing_comparison.stage_errs
                                    : chartData.timing_comparison.reboot_errs;
                                return '±' + errs[ctx.dataIndex].toFixed(1) + 's';
                            }}
                        }}
                    }}
                }},
                scales: {{
                    x: {{ stacked: true }},
                    y: {{
                        stacked: true,
                        title: {{ display: true, text: 'Duration (seconds)' }}
                    }}
                }}
            }}
        }});

        // --- Per-target charts ---
        for (const [label, tdata] of Object.entries(chartData.per_target)) {{
            const safeId = label.replace(':', '-').replace('/', '-');

            // Iteration timing chart
            new Chart(document.getElementById('iter-chart-' + safeId), {{
                type: 'bar',
                data: {{
                    labels: tdata.iters.map(i => 'Iter ' + i),
                    datasets: [
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
                    ]
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
                            title: {{ display: true, text: 'Seconds' }}
                        }}
                    }}
                }}
            }});

            // Memory time series (from first successful iteration)
            if (tdata.mem_ts && tdata.mem_ts.length > 0) {{
                new Chart(document.getElementById('mem-chart-' + safeId), {{
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
                                title: {{ display: true, text: 'Time (seconds)' }}
                            }},
                            y: {{
                                title: {{ display: true, text: 'RSS (MB)' }}
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

#!/bin/bash
# Full 3-target × 3-mode comparison benchmark
# Scheduled to run at 18:00 on 2026-06-17
#
# Targets: rhel-bootc 9.8, 10.0, 10.2 (from 9.6 base)
# Modes:   baseline-gzip, baseline-zstd, delta
# Iterations: 5 per target per mode

set -euo pipefail
cd "$(dirname "$0")"

TARGETS=(
    registry.redhat.io/rhel9/rhel-bootc:9.8
    registry.redhat.io/rhel10/rhel-bootc:10.0
    registry.redhat.io/rhel10/rhel-bootc:10.2
)

ITERATIONS=5
STAMP=$(date +%Y%m%d-%H%M%S)
OUTDIR="run-full-${STAMP}"
mkdir -p "$OUTDIR"
LOGFILE="${OUTDIR}/benchmark.log"

log() {
    echo "[$(date '+%H:%M:%S')] $*" | tee -a "$LOGFILE"
}

log "=== Full comparison benchmark ==="
log "Targets: ${TARGETS[*]}"
log "Modes: baseline-gzip, baseline-zstd, delta"
log "Iterations: $ITERATIONS"
log "Output: $OUTDIR"
log ""

# Firewall port 5000 must already be open for libvirt zone.
# Set it up before scheduling: sudo firewall-cmd --zone=libvirt --add-port=5000/tcp --permanent
if ! curl -sf http://192.168.122.1:5000/v2/ &>/dev/null && \
   ! curl -sf http://localhost:5000/v2/ &>/dev/null; then
    log "WARNING: registry port 5000 may not be reachable from VMs"
    log "Ensure: sudo firewall-cmd --zone=libvirt --add-port=5000/tcp --permanent"
fi

FAILED=0

# --- Run 1: baseline-gzip ---
log "========== RUN 1/3: baseline-gzip =========="
if python3 bootc_bench.py \
    -m baseline-gzip \
    -t "${TARGETS[@]}" \
    -n "$ITERATIONS" \
    -o "${OUTDIR}/gzip" \
    2>&1 | tee -a "$LOGFILE"; then
    log "baseline-gzip: DONE"
else
    log "baseline-gzip: FAILED (exit $?)"
    FAILED=$((FAILED + 1))
fi
log ""

# --- Run 2: baseline-zstd ---
log "========== RUN 2/3: baseline-zstd =========="
if python3 bootc_bench.py \
    -m baseline-zstd \
    -t "${TARGETS[@]}" \
    -n "$ITERATIONS" \
    -o "${OUTDIR}/zstd" \
    2>&1 | tee -a "$LOGFILE"; then
    log "baseline-zstd: DONE"
else
    log "baseline-zstd: FAILED (exit $?)"
    FAILED=$((FAILED + 1))
fi
log ""

# --- Run 3: delta ---
log "========== RUN 3/3: delta =========="
if python3 bootc_bench.py \
    -m delta \
    -t "${TARGETS[@]}" \
    -n "$ITERATIONS" \
    -o "${OUTDIR}/delta" \
    2>&1 | tee -a "$LOGFILE"; then
    log "delta: DONE"
else
    log "delta: FAILED (exit $?)"
    FAILED=$((FAILED + 1))
fi
log ""

# --- Merge results ---
log "========== Merging results =========="
python3 -c "
import json, sys, os

outdir = '${OUTDIR}'
runs = {}
for mode in ('gzip', 'zstd', 'delta'):
    path = os.path.join(outdir, mode, 'results.json')
    if os.path.exists(path):
        runs[mode] = json.load(open(path))
    else:
        print(f'WARNING: {path} not found, skipping', file=sys.stderr)

if not runs:
    print('ERROR: No results to merge', file=sys.stderr)
    sys.exit(1)

first = next(iter(runs.values()))
combined = {
    'config': {
        'base_image': first['config']['base_image'],
        'targets': first['config']['targets'],
        'iterations': first['config']['iterations'],
        'vm_memory_mb': first['config']['vm_memory_mb'],
        'vm_vcpus': first['config']['vm_vcpus'],
        'mode': 'comparison',
        'modes_compared': list(runs.keys()),
        'timestamp': first['config']['timestamp'],
    },
    'benchmarks': [],
}
for mode, data in runs.items():
    combined['benchmarks'].extend(data.get('benchmarks', []))

with open(os.path.join(outdir, 'results.json'), 'w') as f:
    json.dump(combined, f, indent=2, default=str)
print(f'Merged {len(combined[\"benchmarks\"])} benchmarks into {outdir}/results.json')
" 2>&1 | tee -a "$LOGFILE"

# --- Generate report ---
log "========== Generating report =========="
python3 bootc_report.py "${OUTDIR}/results.json" -o "${OUTDIR}/report.html" \
    2>&1 | tee -a "$LOGFILE"

# --- Summary ---
log ""
log "========== COMPLETE =========="
log "Failed runs: $FAILED/3"
log "Results: ${OUTDIR}/results.json"
log "Report:  ${OUTDIR}/report.html"
log "Log:     $LOGFILE"

# Print quick comparison table
python3 -c "
import json, os

outdir = '${OUTDIR}'
path = os.path.join(outdir, 'results.json')
if not os.path.exists(path):
    exit()

data = json.load(open(path))
benchmarks = data['benchmarks']

# Group by target
from collections import defaultdict
by_target = defaultdict(dict)
for b in benchmarks:
    target = b['target_image'].split('/')[-1]
    mode = b['mode']
    total = b.get('summary', {}).get('total_duration_sec', {})
    mean = total.get('mean', 'N/A') if isinstance(total, dict) else 'N/A'
    success = b.get('summary', {}).get('successful_iterations', 0)
    total_iters = success + b.get('summary', {}).get('failed_iterations', 0)
    by_target[target][mode] = (mean, success, total_iters)

print()
print(f'{\"Target\":>20} {\"Gzip\":>14} {\"zstd:chunked\":>14} {\"Delta\":>14}')
print('-' * 66)
for target in sorted(by_target):
    row = f'{target:>20}'
    for mode in ('baseline-gzip', 'baseline-zstd', 'delta'):
        if mode in by_target[target]:
            mean, ok, tot = by_target[target][mode]
            if isinstance(mean, float):
                row += f' {mean:>8.1f}s {ok}/{tot}'
            else:
                row += f' {\"N/A\":>8} {ok}/{tot}'
        else:
            row += f' {\"—\":>14}'
    print(row)
print()
" 2>&1 | tee -a "$LOGFILE"

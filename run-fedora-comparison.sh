#!/bin/bash
# Fedora 43 -> 44 upgrade comparison benchmark
#
# Runs baseline-gzip, baseline-zstd, and delta (oci-delta) modes, each
# under multiple simulated network profiles, for both vanilla and
# customized (package-layered) Fedora bootc images.
#
# Usage:
#   ./run-fedora-comparison.sh                          # defaults: 43 -> 44
#   ./run-fedora-comparison.sh 43 44                     # explicit versions
#   ./run-fedora-comparison.sh 43 44 --no-customize      # skip customized variants
#
# Override the mode/profile matrix via env vars:
#   MODES="baseline-gzip delta" NETWORK_PROFILES="none constrained" ./run-fedora-comparison.sh
#   ITERATIONS=5 ./run-fedora-comparison.sh

set -euo pipefail
cd "$(dirname "$0")"

BASE_VERSION="${1:-43}"
TARGET_VERSION="${2:-44}"
shift 2 2>/dev/null || true
EXTRA_ARGS=("$@")

BASE_IMAGE="quay.io/fedora/fedora-bootc:${BASE_VERSION}"
TARGET_IMAGE="quay.io/fedora/fedora-bootc:${TARGET_VERSION}"

ITERATIONS="${ITERATIONS:-5}"
MODES=(${MODES:-baseline-gzip baseline-zstd delta})
PROFILES=(${NETWORK_PROFILES:-none broadband constrained})

STAMP=$(date +%Y%m%d-%H%M%S)
OUTDIR="run-fedora-${BASE_VERSION}-${TARGET_VERSION}-${STAMP}"
mkdir -p "$OUTDIR"
LOGFILE="${OUTDIR}/benchmark.log"

log() {
    echo "[$(date '+%H:%M:%S')] $*" | tee -a "$LOGFILE"
}

TOTAL_RUNS=$(( ${#MODES[@]} * ${#PROFILES[@]} ))

log "=== Fedora ${BASE_VERSION} -> ${TARGET_VERSION} comparison benchmark ==="
log "Base image:   $BASE_IMAGE"
log "Target image: $TARGET_IMAGE"
log "Modes:        ${MODES[*]}"
log "Profiles:     ${PROFILES[*]}"
log "Iterations:   $ITERATIONS per (mode, profile)"
log "Total runs:   $TOTAL_RUNS"
log "Extra args:   ${EXTRA_ARGS[*]:-none}"
log "Output:       $OUTDIR"
log ""

# Firewall port 5000 must already be open for libvirt zone (needed by
# baseline-gzip/baseline-zstd, which push through a local registry).
if ! curl -sf http://192.168.122.1:5000/v2/ &>/dev/null && \
   ! curl -sf http://localhost:5000/v2/ &>/dev/null; then
    log "NOTE: registry port 5000 not reachable yet (fine if this is the first run;"
    log "      bootc_bench.py starts it on demand). If baseline-gzip/zstd runs fail"
    log "      to pull, run: sudo firewall-cmd --zone=libvirt --add-port=5000/tcp --permanent"
fi

FAILED=0
RUN_DIRS=()

COMMON_ARGS=(
    -b "$BASE_IMAGE"
    -t "$TARGET_IMAGE"
    -n "$ITERATIONS"
    "${EXTRA_ARGS[@]}"
)

RUN_NUM=0
for mode in "${MODES[@]}"; do
    for profile in "${PROFILES[@]}"; do
        RUN_NUM=$((RUN_NUM + 1))
        RUN_DIR="${OUTDIR}/${mode}-${profile}"
        RUN_DIRS+=("$RUN_DIR")
        log "========== RUN ${RUN_NUM}/${TOTAL_RUNS}: mode=${mode} profile=${profile} =========="
        if python3 bootc_bench.py \
            -m "$mode" \
            --network-profile "$profile" \
            "${COMMON_ARGS[@]}" \
            -o "$RUN_DIR" \
            2>&1 | tee -a "$LOGFILE"; then
            log "${mode}/${profile}: DONE"
        else
            log "${mode}/${profile}: FAILED (exit $?)"
            FAILED=$((FAILED + 1))
        fi
        log ""
    done
done

# --- Merge results ---
log "========== Merging results =========="
python3 -c "
import json, sys, os

outdir = '${OUTDIR}'
run_dirs = '''${RUN_DIRS[@]}'''.split()
runs = {}
for d in run_dirs:
    path = os.path.join(d, 'results.json')
    key = os.path.basename(d)
    if os.path.exists(path):
        runs[key] = json.load(open(path))
    else:
        print(f'WARNING: {path} not found, skipping', file=sys.stderr)

if not runs:
    print('ERROR: No results to merge', file=sys.stderr)
    sys.exit(1)

first = next(iter(runs.values()))
combined = {
    'config': {
        'base_image': first['config']['base_image'],
        'ystream_targets': first['config'].get('ystream_targets', []),
        'packages': first['config'].get('packages'),
        'customize': first['config'].get('customize', False),
        'iterations': first['config']['iterations'],
        'vm_memory_mb': first['config']['vm_memory_mb'],
        'vm_vcpus': first['config']['vm_vcpus'],
        'mode': 'comparison',
        'modes_compared': sorted(set(r['config']['mode'] for r in runs.values())),
        'network_profile': 'comparison',
        'timestamp': first['config']['timestamp'],
    },
    'benchmarks': [],
}
for key, data in runs.items():
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
log "Failed runs: $FAILED/$TOTAL_RUNS"
log "Results: ${OUTDIR}/results.json"
log "Report:  ${OUTDIR}/report.html"
log "Log:     $LOGFILE"

# Print quick comparison table: rows = scenario (target/variant), columns = mode x profile
python3 -c "
import json, os
from collections import defaultdict

outdir = '${OUTDIR}'
path = os.path.join(outdir, 'results.json')
if not os.path.exists(path):
    exit()

data = json.load(open(path))
benchmarks = data['benchmarks']

by_key = defaultdict(dict)
columns = set()
for b in benchmarks:
    target = b['target_image'].split('/')[-1]
    variant = b.get('variant', 'vanilla')
    mode = b['mode']
    profile = b.get('network_profile', 'none')
    col = f'{mode}/{profile}'
    columns.add(col)
    total = b.get('summary', {}).get('total_duration_sec', {})
    mean = total.get('mean', 'N/A') if isinstance(total, dict) else 'N/A'
    net = b.get('summary', {}).get('net_rx_bytes', {})
    net_mb = round((net.get('mean', 0) or 0) / 1024**2, 1) if isinstance(net, dict) else 0
    success = b.get('summary', {}).get('successful_iterations', 0)
    total_iters = success + b.get('summary', {}).get('failed_iterations', 0)
    key = f'{target} [{variant[:4]}]'
    by_key[key][col] = (mean, net_mb, success, total_iters)

columns = sorted(columns)
print()
header = f'{\"Scenario\":>28}' + ''.join(f'{c:>22}' for c in columns)
print(header)
print('-' * len(header))
for key in sorted(by_key):
    row = f'{key:>28}'
    for col in columns:
        if col in by_key[key]:
            mean, net_mb, ok, tot = by_key[key][col]
            cell = f'{mean:.0f}s/{net_mb:.0f}MB {ok}/{tot}' if isinstance(mean, float) else f'N/A {ok}/{tot}'
            row += f'{cell:>22}'
        else:
            row += f'{\"—\":>22}'
    print(row)
print()
print('Cell format: total_duration / network_rx_bytes  success/total')
" 2>&1 | tee -a "$LOGFILE"

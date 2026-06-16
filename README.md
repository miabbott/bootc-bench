# bootc-bench

Benchmarking harness for measuring performance statistics during bootc VM upgrades.
Provisions fresh VMs via libvirt, runs `bootc switch` to an upgrade target, and
collects timing, resource usage, and image layer data across multiple iterations.

## What it measures

- **Stage timing** — wall-clock duration of `bootc switch` (image pull + ostree deploy)
- **Reboot timing** — time from reboot initiation until SSH is available again
- **Download size** — compressed image size from the registry (via skopeo + bootc output)
- **Layer stats** — count, per-layer sizes, digests, and media types
- **CPU/memory usage** — sampled from the hypervisor via `libvirt domstats` (no SSH overhead)
- **Disk delta** — change in disk usage before/after upgrade

## Default test matrix

| Base | Target |
|------|--------|
| `registry.redhat.io/rhel9/rhel-bootc:9.6` | `registry.redhat.io/rhel9/rhel-bootc:9.8` |
| `registry.redhat.io/rhel9/rhel-bootc:9.6` | `registry.redhat.io/rhel10/rhel-bootc:10.0` |
| `registry.redhat.io/rhel9/rhel-bootc:9.6` | `registry.redhat.io/rhel10/rhel-bootc:10.2` |

## Prerequisites

- Python 3.10+
- `libvirt` + `qemu-kvm` installed and running (with the `default` network active)
- `podman` and `skopeo` with registry auth for `registry.redhat.io`
- `bootc-image-builder` container image (pulled automatically)
- Python packages: `pip install -r requirements.txt`

## Usage

```bash
# Run with defaults (3 iterations, all targets)
python3 bootc_bench.py

# Single target, 5 iterations
python3 bootc_bench.py -t registry.redhat.io/rhel9/rhel-bootc:9.8 -n 5

# Use a pre-built qcow2 (skip image building)
python3 bootc_bench.py --qcow2 /path/to/base.qcow2

# Custom VM resources
python3 bootc_bench.py --vm-memory 8192 --vm-vcpus 4

# Verbose logging
python3 bootc_bench.py -v
```

## Options

| Flag | Default | Description |
|------|---------|-------------|
| `-b, --base-image` | `registry.redhat.io/rhel9/rhel-bootc:9.6` | Base bootc image |
| `-t, --targets` | 9.8, 10.0, 10.2 | Upgrade target image(s) |
| `-n, --iterations` | 3 | Iterations per target |
| `-o, --output-dir` | `./bootc-bench-results` | Output directory |
| `--qcow2` | — | Pre-built base qcow2 (skip build step) |
| `--vm-memory` | 4096 | VM memory in MB |
| `--vm-vcpus` | 2 | VM vCPU count |
| `--libvirt-uri` | `qemu:///system` | Libvirt connection URI |
| `-v, --verbose` | — | Debug logging |

## How it works

1. **Build base qcow2 (once):** Creates a derived Containerfile from the base bootc image
   with an SSH key baked in, builds it with podman, and runs `bootc-image-builder`
   to produce a qcow2 disk image.

2. **Collect target layer info (once per target):** Uses `skopeo inspect` from the host
   to gather layer counts, sizes, and digests. Resolves multi-arch manifest lists
   to the amd64 image manifest.

3. **Per iteration:**
   - Copies the base qcow2 to `/var/lib/libvirt/images/bootc-bench/`
   - Provisions a fresh VM via libvirt
   - Waits for SSH, copies registry auth into the VM
   - Starts background CPU/memory monitoring via `libvirt domstats`
   - Runs `bootc switch <target>` and times the stage phase
   - Reboots the VM and times until SSH returns
   - Collects post-upgrade stats (`bootc status`, disk usage)
   - Destroys the VM and deletes the disk

4. **Output:** Writes `results.json` with per-iteration data and summary statistics
   (mean, stddev, min, max) for each target.

## Output format

Results are written to `<output-dir>/results.json`:

```json
{
  "config": {
    "base_image": "registry.redhat.io/rhel9/rhel-bootc:9.6",
    "targets": ["registry.redhat.io/rhel9/rhel-bootc:9.8"],
    "iterations": 3
  },
  "benchmarks": [
    {
      "target_image": "registry.redhat.io/rhel9/rhel-bootc:9.8",
      "target_layer_info": {
        "layer_count": 66,
        "total_compressed_size_bytes": 1395864371
      },
      "iterations": [
        {
          "stage_phase": { "name": "stage", "duration_sec": 82.1 },
          "reboot_phase": { "name": "reboot", "duration_sec": 19.4 },
          "total_duration_sec": 97.4,
          "download_size_bytes": 1395864371,
          "layers_pulled": 66,
          "bootc_parsed": {
            "layers_needed": 66,
            "download_size_human": "1.3 GB",
            "deploy_duration_sec": 2,
            "target_version": "9.8"
          },
          "cpu_samples": [],
          "memory_samples": []
        }
      ],
      "summary": {
        "stage_duration_sec": { "mean": 73.7, "stddev": 7.2, "min": 69.6, "max": 82.1 },
        "reboot_duration_sec": { "mean": 17.9, "stddev": 2.4, "min": 15.3, "max": 19.4 },
        "total_duration_sec": { "mean": 91.7, "stddev": 4.5, "min": 88.9, "max": 97.4 }
      }
    }
  ]
}
```

## Delta mode (oci-delta)

In addition to the default **baseline** mode (full registry pull), bootc-bench
supports a **delta** mode that uses [oci-delta](https://github.com/containers/oci-delta)
to create and apply binary deltas between OCI images.

```bash
# Run delta benchmark (requires oci-delta binary)
python3 bootc_bench.py -m delta -t registry.redhat.io/rhel9/rhel-bootc:9.8

# Use a custom oci-delta binary path
python3 bootc_bench.py -m delta --oci-delta-bin /path/to/oci-delta

# With a pre-built qcow2
python3 bootc_bench.py -m delta --qcow2 /path/to/base.qcow2 -n 5
```

### How delta mode works

1. **Prepare delta artifacts (once per target):**
   - Exports the base and target images as OCI archives via `skopeo copy`
   - Runs `oci-delta create` to produce a binary delta file

2. **Per iteration:**
   - Provisions a fresh VM (same as baseline)
   - **Transfer phase:** SCP the delta file + oci-delta binary into the VM
   - **Apply phase:** Run `oci-delta apply` inside the VM to reconstruct
     the target OCI archive
   - **Stage phase:** Run `bootc switch --transport=oci-archive` against
     the local archive (no registry pull)
   - **Reboot phase:** Same as baseline

### Delta-specific output fields

| Field | Description |
|-------|-------------|
| `delta_artifacts` | Archive sizes, delta size, export/create timings |
| `delta_transfer_phase` | Time to SCP delta + binary into the VM |
| `delta_apply_phase` | Time to reconstruct the OCI archive |
| `delta_file_size_bytes` | Size of the binary delta file |

## HTML report

Generate a self-contained HTML report from the results JSON:

```bash
# Default: reads ./bootc-bench-results/results.json
python3 bootc_report.py

# Custom input and output
python3 bootc_report.py /path/to/results.json -o report.html
```

The report includes:

- **Summary table** — per-target comparison with mode badges, timing means, and success rates
- **Timing comparison chart** — stacked bar chart with all phases (transfer, apply, stage, reboot)
- **Delta size chart** — full image vs delta file size comparison (delta mode only)
- **Per-target details** — iteration-level tables, per-iteration timing charts, and memory usage time series
- **Delta artifacts** — archive sizes, compression ratio, and preparation timings (delta mode only)

## License

MIT — see [LICENSE](LICENSE).

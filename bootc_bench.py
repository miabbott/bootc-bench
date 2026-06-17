#!/usr/bin/env python3
"""bootc-bench: Benchmarking harness for bootc VM upgrades.

Measures timing, resource usage, and image statistics during bootc upgrades
of VMs provisioned via libvirt/qemu from bootc-image-builder qcow2 images.
"""

import argparse
import json
import logging
import os
import shutil
import socket
import statistics
import subprocess
import tempfile
import textwrap
import threading
import time
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Optional

import libvirt
import paramiko

log = logging.getLogger("bootc-bench")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DEFAULT_BASE_IMAGE = "registry.redhat.io/rhel9/rhel-bootc:9.6"
DEFAULT_TARGETS = [
    "registry.redhat.io/rhel9/rhel-bootc:9.8",
    "registry.redhat.io/rhel10/rhel-bootc:10.0",
    "registry.redhat.io/rhel10/rhel-bootc:10.2",
]
DEFAULT_ITERATIONS = 3
DEFAULT_VM_MEMORY_MB = 4096
DEFAULT_VM_VCPUS = 2
DEFAULT_VM_DISK_GB = 40
# Process scheduling: run heavy host-side work at reduced priority so the
# benchmark doesn't starve the desktop.  nice 10 = lower CPU priority,
# ionice -c3 = idle I/O class (only uses I/O when nothing else needs it).
NICE_PREFIX = ["nice", "-n", "10", "ionice", "-c3"]
BIB_IMAGE = "registry.redhat.io/rhel9/bootc-image-builder"
OCI_DELTA_BIN_DEFAULT = "./oci-delta"
MONITOR_INTERVAL_SEC = 2
SSH_TIMEOUT_SEC = 300
SSH_PORT = 22


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class MonitorSample:
    """A single CPU/memory sample from virsh domstats."""
    timestamp: float
    cpu_time_ns: int = 0
    cpu_user_ns: int = 0
    cpu_system_ns: int = 0
    memory_rss_kb: int = 0
    memory_available_kb: int = 0
    memory_used_kb: int = 0


@dataclass
class PhaseResult:
    """Timing and context for a single phase."""
    name: str
    duration_sec: float
    start_time: str
    end_time: str


@dataclass
class IterationResult:
    """All data collected from a single benchmark iteration."""
    iteration: int
    stage_phase: Optional[PhaseResult] = None
    reboot_phase: Optional[PhaseResult] = None
    total_duration_sec: float = 0.0
    pre_upgrade: dict = field(default_factory=dict)
    post_upgrade: dict = field(default_factory=dict)
    bootc_switch_output: str = ""
    bootc_parsed: dict = field(default_factory=dict)
    download_size_bytes: Optional[int] = None
    layers_pulled: Optional[int] = None
    layer_details: list = field(default_factory=list)
    disk_delta_bytes: Optional[int] = None
    # Delta-mode fields
    delta_transfer_phase: Optional[PhaseResult] = None
    delta_apply_phase: Optional[PhaseResult] = None
    delta_file_size_bytes: Optional[int] = None
    delta_apply_output: str = ""
    mode: str = "baseline"
    cpu_samples: list = field(default_factory=list)
    memory_samples: list = field(default_factory=list)
    error: Optional[str] = None


@dataclass
class SummaryStat:
    """Summary statistics for a metric across iterations."""
    mean: float
    stddev: float
    min: float
    max: float
    count: int


# ---------------------------------------------------------------------------
# SSH helpers
# ---------------------------------------------------------------------------

class SSH:
    """Thin wrapper around paramiko for VM SSH operations."""

    def __init__(self, host: str, key_path: str, user: str = "root",
                 port: int = SSH_PORT):
        self.host = host
        self.user = user
        self.port = port
        self.key_path = key_path
        self._client: Optional[paramiko.SSHClient] = None

    def connect(self, timeout: int = SSH_TIMEOUT_SEC):
        """Connect, retrying until timeout."""
        deadline = time.time() + timeout
        last_err = None
        while time.time() < deadline:
            try:
                client = paramiko.SSHClient()
                client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
                client.connect(
                    self.host,
                    port=self.port,
                    username=self.user,
                    key_filename=self.key_path,
                    timeout=10,
                    auth_timeout=10,
                    banner_timeout=10,
                )
                self._client = client
                log.info("SSH connected to %s", self.host)
                return
            except (
                paramiko.ssh_exception.SSHException,
                paramiko.ssh_exception.NoValidConnectionsError,
                socket.error,
                OSError,
            ) as e:
                last_err = e
                time.sleep(3)
        raise TimeoutError(
            f"SSH to {self.host} not available after {timeout}s: {last_err}"
        )

    def run(self, cmd: str, timeout: int = 600) -> tuple[int, str, str]:
        """Run a command, return (exit_code, stdout, stderr)."""
        if not self._client:
            raise RuntimeError("Not connected")
        log.debug("SSH run: %s", cmd)
        _, stdout, stderr = self._client.exec_command(cmd, timeout=timeout)
        exit_code = stdout.channel.recv_exit_status()
        out = stdout.read().decode("utf-8", errors="replace")
        err = stderr.read().decode("utf-8", errors="replace")
        return exit_code, out, err

    def upload_file(self, local_path: str, remote_path: str):
        """Upload a file via SFTP."""
        if not self._client:
            raise RuntimeError("Not connected")
        sftp = self._client.open_sftp()
        sftp.put(local_path, remote_path)
        sftp.close()

    def close(self):
        if self._client:
            self._client.close()
            self._client = None


# ---------------------------------------------------------------------------
# VM management via libvirt
# ---------------------------------------------------------------------------

class VMManager:
    """Manage benchmark VMs through libvirt."""

    DOMAIN_XML_TEMPLATE = textwrap.dedent("""\
        <domain type='kvm'>
          <name>{name}</name>
          <uuid>{uuid}</uuid>
          <memory unit='MiB'>{memory_mb}</memory>
          <vcpu>{vcpus}</vcpu>
          <os>
            <type arch='x86_64' machine='q35'>hvm</type>
            <boot dev='hd'/>
          </os>
          <features>
            <acpi/>
            <apic/>
          </features>
          <cpu mode='host-passthrough'/>
          <cputune>
            <period>100000</period>
            <quota>{cpu_quota}</quota>
          </cputune>
          <devices>
            <disk type='file' device='disk'>
              <driver name='qemu' type='qcow2'/>
              <source file='{disk_path}'/>
              <target dev='vda' bus='virtio'/>
            </disk>
            <interface type='network'>
              <source network='default'/>
              <model type='virtio'/>
            </interface>
            <serial type='pty'>
              <target port='0'/>
            </serial>
            <console type='pty'>
              <target type='serial' port='0'/>
            </console>
            <channel type='unix'>
              <target type='virtio' name='org.qemu.guest_agent.0'/>
            </channel>
          </devices>
          <seclabel type='none'/>
        </domain>
    """)

    def __init__(self, uri: str = "qemu:///system"):
        self.uri = uri
        self.conn: Optional[libvirt.virConnect] = None

    def connect(self):
        self.conn = libvirt.open(self.uri)
        if not self.conn:
            raise RuntimeError(f"Failed to connect to {self.uri}")

    def close(self):
        if self.conn:
            self.conn.close()
            self.conn = None

    def create_vm(self, name: str, disk_path: str,
                  memory_mb: int = DEFAULT_VM_MEMORY_MB,
                  vcpus: int = DEFAULT_VM_VCPUS) -> libvirt.virDomain:
        """Define and start a VM."""
        # Cap each vCPU to ~50% of a host core so the VM doesn't
        # starve the desktop.  period=100ms, quota = vcpus * 50ms.
        cpu_quota = vcpus * 50000
        xml = self.DOMAIN_XML_TEMPLATE.format(
            name=name,
            uuid=str(uuid.uuid4()),
            memory_mb=memory_mb,
            vcpus=vcpus,
            cpu_quota=cpu_quota,
            disk_path=disk_path,
        )
        dom = self.conn.defineXML(xml)
        if not dom:
            raise RuntimeError(f"Failed to define VM {name}")
        dom.create()
        log.info("VM %s started", name)
        return dom

    def get_vm_ip(self, dom: libvirt.virDomain,
                  timeout: int = 180) -> str:
        """Wait for the VM to get an IP via DHCP (from libvirt agent or ARP)."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                # Try guest agent first
                ifaces = dom.interfaceAddresses(
                    libvirt.VIR_DOMAIN_INTERFACE_ADDRESSES_SRC_AGENT
                )
            except libvirt.libvirtError:
                try:
                    # Fall back to DHCP lease
                    ifaces = dom.interfaceAddresses(
                        libvirt.VIR_DOMAIN_INTERFACE_ADDRESSES_SRC_LEASE
                    )
                except libvirt.libvirtError:
                    ifaces = {}

            for iface_name, iface_data in ifaces.items():
                if iface_name == "lo":
                    continue
                for addr in iface_data.get("addrs", []):
                    if addr["type"] == 0:  # IPv4
                        ip = addr["addr"]
                        log.info("VM IP: %s", ip)
                        return ip
            time.sleep(3)
        raise TimeoutError(f"VM did not get an IP within {timeout}s")

    def get_domstats(self, dom: libvirt.virDomain) -> MonitorSample:
        """Collect a single stats sample via virsh domstats."""
        sample = MonitorSample(timestamp=time.time())
        try:
            stats = dom.memoryStats()
            sample.memory_rss_kb = stats.get("rss", 0)
            sample.memory_available_kb = stats.get("available", 0)
            sample.memory_used_kb = stats.get("rss", 0)
        except libvirt.libvirtError:
            pass

        try:
            # CPU stats: array of per-vcpu stats + total
            info = dom.info()
            # info[4] is CPU time in nanoseconds
            sample.cpu_time_ns = info[4] if len(info) > 4 else 0
        except libvirt.libvirtError:
            pass

        return sample

    def destroy_vm(self, dom: libvirt.virDomain):
        """Force-stop and undefine a VM."""
        name = dom.name()
        try:
            if dom.isActive():
                dom.destroy()
        except libvirt.libvirtError:
            pass
        try:
            dom.undefineFlags(
                libvirt.VIR_DOMAIN_UNDEFINE_NVRAM
                | libvirt.VIR_DOMAIN_UNDEFINE_SNAPSHOTS_METADATA
            )
        except libvirt.libvirtError:
            try:
                dom.undefine()
            except libvirt.libvirtError:
                pass
        log.info("VM %s destroyed", name)

    def reboot_vm(self, dom: libvirt.virDomain):
        """Send reboot signal to VM."""
        dom.reboot(0)
        log.info("VM %s reboot initiated", dom.name())


# ---------------------------------------------------------------------------
# Resource monitor (runs in background thread)
# ---------------------------------------------------------------------------

class ResourceMonitor:
    """Collect CPU/memory samples in a background thread."""

    def __init__(self, vm_mgr: VMManager, dom: libvirt.virDomain,
                 interval: float = MONITOR_INTERVAL_SEC):
        self.vm_mgr = vm_mgr
        self.dom = dom
        self.interval = interval
        self.samples: list[MonitorSample] = []
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self):
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        log.info("Resource monitor started")

    def stop(self) -> list[MonitorSample]:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=10)
        log.info("Resource monitor stopped (%d samples)", len(self.samples))
        return self.samples

    def _run(self):
        while not self._stop.is_set():
            try:
                sample = self.vm_mgr.get_domstats(self.dom)
                self.samples.append(sample)
            except Exception as e:
                log.debug("Monitor sample error: %s", e)
            self._stop.wait(self.interval)


# ---------------------------------------------------------------------------
# Image builder
# ---------------------------------------------------------------------------

def _find_authfile() -> Optional[str]:
    """Locate the host's container registry auth file."""
    candidates = [
        Path.home() / ".docker" / "config.json",
        Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"))
        / "containers" / "auth.json",
        Path.home() / ".config" / "containers" / "auth.json",
    ]
    for path in candidates:
        if path.exists():
            return str(path)
    return None


def generate_ssh_keypair(output_dir: Path) -> tuple[Path, Path]:
    """Generate an SSH keypair for VM access, return (private, public) paths."""
    priv_key = output_dir / "bootc-bench-key"
    pub_key = output_dir / "bootc-bench-key.pub"
    if priv_key.exists():
        log.info("SSH keypair already exists at %s", priv_key)
        return priv_key, pub_key
    subprocess.run(
        ["ssh-keygen", "-t", "ed25519", "-f", str(priv_key), "-N", "", "-q"],
        check=True,
    )
    log.info("Generated SSH keypair at %s", priv_key)
    return priv_key, pub_key


def build_base_qcow2(
    base_image: str,
    output_dir: Path,
    ssh_pub_key_path: Path,
) -> Path:
    """Build a qcow2 disk image from the base bootc container image.

    Steps:
    1. Create a derived Containerfile that bakes in the SSH public key.
    2. Build the derived container with podman.
    3. Run bootc-image-builder to produce a qcow2.

    Returns the path to the qcow2 file.
    """
    qcow2_path = output_dir / "base.qcow2"
    if qcow2_path.exists():
        log.info("Base qcow2 already exists at %s", qcow2_path)
        return qcow2_path

    build_dir = output_dir / "build"
    build_dir.mkdir(parents=True, exist_ok=True)

    # Copy the SSH public key into the build context
    shutil.copy2(ssh_pub_key_path, build_dir / "authorized_keys")

    # Create Containerfile
    containerfile = build_dir / "Containerfile"
    containerfile.write_text(textwrap.dedent(f"""\
        FROM {base_image}
        RUN mkdir -p /root/.ssh && chmod 700 /root/.ssh
        COPY authorized_keys /root/.ssh/authorized_keys
        RUN chmod 600 /root/.ssh/authorized_keys
        # Ensure sshd is enabled
        RUN systemctl enable sshd
    """))

    derived_tag = "localhost/bootc-bench-base:latest"
    log.info("Building derived container image: %s", derived_tag)

    # Find the host's registry auth file so sudo podman can pull images
    authfile = _find_authfile()
    build_cmd = NICE_PREFIX + ["sudo", "podman", "build", "-t", derived_tag,
                 "-f", str(containerfile), str(build_dir)]
    if authfile:
        # Insert after NICE_PREFIX + "sudo"
        build_cmd.insert(len(NICE_PREFIX) + 2, f"--authfile={authfile}")
        log.info("Using authfile: %s", authfile)

    subprocess.run(build_cmd, check=True)

    # Run bootc-image-builder
    bib_output = output_dir / "bib-output"
    bib_output.mkdir(parents=True, exist_ok=True)

    log.info("Running bootc-image-builder (this may take several minutes)...")
    bib_cmd = NICE_PREFIX + [
        "sudo", "podman", "run",
        "--rm", "--privileged", f"--pull=newer",
        f"--authfile={authfile}" if authfile else None,
        "-v", f"{bib_output}:/output",
        "-v", "/var/lib/containers/storage:/var/lib/containers/storage",
    ]
    # Remove None entries
    bib_cmd = [x for x in bib_cmd if x is not None]
    if authfile:
        bib_cmd.extend(["-v", f"{authfile}:/run/containers/0/auth.json:ro"])
    bib_cmd.extend([BIB_IMAGE, "--type", "qcow2", "--local", derived_tag])
    subprocess.run(bib_cmd, check=True)

    # bootc-image-builder outputs to /output/qcow2/disk.qcow2
    built_qcow2 = bib_output / "qcow2" / "disk.qcow2"
    if not built_qcow2.exists():
        raise FileNotFoundError(
            f"bootc-image-builder did not produce expected output at {built_qcow2}"
        )

    # BIB output is owned by root — chown it so we can copy without sudo
    subprocess.run(["sudo", "chown", f"{os.getuid()}:{os.getgid()}",
                    str(built_qcow2)], check=True)
    shutil.copy2(str(built_qcow2), str(qcow2_path))
    log.info("Base qcow2 ready: %s", qcow2_path)
    return qcow2_path


# ---------------------------------------------------------------------------
# Upgrade stats collection
# ---------------------------------------------------------------------------

def collect_bootc_status(ssh: SSH) -> dict:
    """Collect bootc status as JSON."""
    rc, out, err = ssh.run("bootc status --json")
    if rc != 0:
        log.warning("bootc status failed: %s", err)
        return {"error": err}
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return {"raw": out}


def collect_disk_usage(ssh: SSH) -> dict:
    """Collect disk usage stats."""
    rc, out, _ = ssh.run("df -B1 / | tail -1 | awk '{print $2, $3, $4}'")
    if rc != 0:
        return {}
    parts = out.strip().split()
    if len(parts) == 3:
        return {
            "total_bytes": int(parts[0]),
            "used_bytes": int(parts[1]),
            "available_bytes": int(parts[2]),
        }
    return {"raw": out}


def parse_bootc_switch_output(output: str) -> dict:
    """Extract stats from bootc switch/upgrade output.

    Example output:
        layers already present: 0; layers needed: 66 (1.3 GB)
        Deploying...done (2 seconds)
        Queued for next boot: registry.redhat.io/rhel9/rhel-bootc:9.8
          Version: 9.8
          Digest: sha256:600858c0...
    """
    import re
    info = {"raw_output": output}
    lines = output.strip().splitlines()
    info["output_lines"] = lines

    for line in lines:
        # Parse: layers already present: N; layers needed: M (X.X GB/MB)
        m = re.match(
            r"layers already present:\s*(\d+);\s*layers needed:\s*(\d+)\s*\(([^)]+)\)",
            line.strip(),
        )
        if m:
            info["layers_already_present"] = int(m.group(1))
            info["layers_needed"] = int(m.group(2))
            info["download_size_human"] = m.group(3)
            # Convert human size to bytes
            size_str = m.group(3).strip()
            size_m = re.match(r"([\d.]+)\s*(GB|MB|KB|B)", size_str, re.I)
            if size_m:
                val = float(size_m.group(1))
                unit = size_m.group(2).upper()
                multipliers = {"B": 1, "KB": 1024, "MB": 1024**2, "GB": 1024**3}
                info["download_size_bytes"] = int(val * multipliers.get(unit, 1))

        # Parse: Deploying...done (N seconds)
        m = re.match(r"Deploying\.+done\s*\((\d+)\s*seconds?\)", line.strip())
        if m:
            info["deploy_duration_sec"] = int(m.group(1))

        # Parse: Version: X.Y
        m = re.match(r"\s*Version:\s*(.+)", line.strip())
        if m:
            info["target_version"] = m.group(1).strip()

        # Parse: Digest: sha256:...
        m = re.match(r"\s*Digest:\s*(sha256:\S+)", line.strip())
        if m:
            info["target_digest"] = m.group(1)

    return info


def get_image_layer_info(ssh: SSH, image_ref: str) -> dict:
    """Use skopeo to inspect image layers.

    Handles multi-arch manifest lists by resolving to the amd64 manifest,
    then inspects the resolved image for layer details.
    """
    # First, try --raw to see if it's a manifest list
    rc, out, err = ssh.run(
        f"skopeo inspect --raw docker://{image_ref}"
    )
    if rc != 0:
        log.warning("skopeo inspect --raw failed: %s", err)
        return {"error": f"skopeo inspect --raw failed: {err}"}

    try:
        raw = json.loads(out)
    except json.JSONDecodeError:
        return {"error": "Could not parse skopeo --raw output"}

    media_type = raw.get("mediaType", "")
    schema = raw.get("schemaVersion", 0)

    # If it's a manifest list / OCI index, resolve the amd64 digest
    if ("manifest.list" in media_type or "image.index" in media_type
            or (schema == 2 and "manifests" in raw)):
        digest = None
        for m in raw.get("manifests", []):
            platform = m.get("platform", {})
            if (platform.get("architecture") == "amd64"
                    and platform.get("os") == "linux"):
                digest = m.get("digest")
                break
        if not digest:
            return {"error": "No amd64 manifest found in manifest list",
                    "manifest_list": raw}

        # Re-inspect the architecture-specific manifest
        ref_by_digest = image_ref.rsplit(":", 1)[0] + "@" + digest
        rc, out, err = ssh.run(
            f"skopeo inspect --raw docker://{ref_by_digest}"
        )
        if rc != 0:
            return {"error": f"skopeo inspect digest failed: {err}"}
        try:
            raw = json.loads(out)
        except json.JSONDecodeError:
            return {"error": "Could not parse resolved manifest"}

    # Now raw should be an image manifest with layers
    layers = raw.get("layers", [])
    total_size = sum(
        layer.get("size", 0) for layer in layers
        if isinstance(layer, dict)
    )

    # Also get the friendly inspect output for additional metadata
    rc2, out2, _ = ssh.run(f"skopeo inspect docker://{image_ref}")
    inspect_data = {}
    if rc2 == 0:
        try:
            inspect_data = json.loads(out2)
        except json.JSONDecodeError:
            pass

    return {
        "layer_count": len(layers),
        "total_compressed_size_bytes": total_size,
        "layers": [
            {
                "digest": l.get("digest", ""),
                "size_bytes": l.get("size", 0),
                "media_type": l.get("mediaType", ""),
            }
            for l in layers if isinstance(l, dict)
        ],
        "digest": inspect_data.get("Digest", ""),
        "labels": inspect_data.get("Labels", {}),
    }


def get_image_layer_info_host(image_ref: str) -> dict:
    """Collect image layer info from the host using skopeo (no VM needed)."""
    try:
        raw_result = subprocess.run(
            ["skopeo", "inspect", "--raw", f"docker://{image_ref}"],
            capture_output=True, text=True, check=True,
        )
        raw = json.loads(raw_result.stdout)
    except (subprocess.CalledProcessError, json.JSONDecodeError) as e:
        log.warning("skopeo inspect --raw failed on host: %s", e)
        return {"error": str(e)}

    media_type = raw.get("mediaType", "")
    schema = raw.get("schemaVersion", 0)

    # Resolve manifest list to amd64
    if ("manifest.list" in media_type or "image.index" in media_type
            or (schema == 2 and "manifests" in raw)):
        digest = None
        for m in raw.get("manifests", []):
            platform = m.get("platform", {})
            if (platform.get("architecture") == "amd64"
                    and platform.get("os") == "linux"):
                digest = m.get("digest")
                break
        if not digest:
            return {"error": "No amd64 manifest found in manifest list"}

        ref_by_digest = image_ref.rsplit(":", 1)[0] + "@" + digest
        try:
            raw_result = subprocess.run(
                ["skopeo", "inspect", "--raw", f"docker://{ref_by_digest}"],
                capture_output=True, text=True, check=True,
            )
            raw = json.loads(raw_result.stdout)
        except (subprocess.CalledProcessError, json.JSONDecodeError) as e:
            return {"error": f"Failed to resolve amd64 manifest: {e}"}

    layers = raw.get("layers", [])
    total_size = sum(
        layer.get("size", 0) for layer in layers if isinstance(layer, dict)
    )

    # Friendly inspect for metadata
    inspect_data = {}
    try:
        inspect_result = subprocess.run(
            ["skopeo", "inspect", f"docker://{image_ref}"],
            capture_output=True, text=True, check=True,
        )
        inspect_data = json.loads(inspect_result.stdout)
    except (subprocess.CalledProcessError, json.JSONDecodeError):
        pass

    return {
        "layer_count": len(layers),
        "total_compressed_size_bytes": total_size,
        "layers": [
            {
                "digest": l.get("digest", ""),
                "size_bytes": l.get("size", 0),
                "media_type": l.get("mediaType", ""),
            }
            for l in layers if isinstance(l, dict)
        ],
        "digest": inspect_data.get("Digest", ""),
        "labels": inspect_data.get("Labels", {}),
    }


def copy_registry_auth(ssh: SSH):
    """Copy host registry auth into the VM.

    bootc/ostree can look for auth in several locations. We copy to all of them
    to ensure it's found regardless of how bootc resolves credentials.
    """
    auth_paths = [
        Path.home() / ".docker" / "config.json",
        Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"))
        / "containers" / "auth.json",
        Path.home() / ".config" / "containers" / "auth.json",
    ]
    src = None
    for auth_path in auth_paths:
        if auth_path.exists():
            src = auth_path
            break
    if not src:
        log.warning("No registry auth file found on host; VM may not be able "
                    "to pull images")
        return

    log.info("Copying registry auth from %s", src)
    targets = [
        ("/root/.docker", "/root/.docker/config.json"),
        ("/etc/containers", "/etc/containers/auth.json"),
        ("/etc/ostree", "/etc/ostree/auth.json"),
        ("/run/containers/0", "/run/containers/0/auth.json"),
    ]
    for dir_path, file_path in targets:
        ssh.run(f"mkdir -p {dir_path}")
        ssh.upload_file(str(src), file_path)
        ssh.run(f"chmod 600 {file_path}")
        log.debug("Uploaded auth to %s", file_path)


# ---------------------------------------------------------------------------
# Wait for SSH after reboot
# ---------------------------------------------------------------------------

def wait_for_ssh_down(host: str, timeout: int = 60):
    """Wait for SSH to become unreachable (VM is rebooting)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            sock = socket.create_connection((host, SSH_PORT), timeout=3)
            sock.close()
            time.sleep(1)
        except (socket.error, OSError):
            log.debug("SSH is down (reboot in progress)")
            return
    log.warning("SSH never went down within %ds; VM may not have rebooted", timeout)


# ---------------------------------------------------------------------------
# Single iteration
# ---------------------------------------------------------------------------

def run_iteration(
    iteration_num: int,
    base_qcow2: Path,
    target_image: str,
    ssh_key_path: Path,
    vm_mgr: VMManager,
    work_dir: Path,
    target_layer_info: Optional[dict] = None,
    vm_memory_mb: int = DEFAULT_VM_MEMORY_MB,
    vm_vcpus: int = DEFAULT_VM_VCPUS,
) -> IterationResult:
    """Run a single benchmark iteration."""
    result = IterationResult(iteration=iteration_num)
    vm_name = f"bootc-bench-{iteration_num}-{uuid.uuid4().hex[:8]}"
    disk_path = work_dir / f"{vm_name}.qcow2"
    dom = None
    ssh = None

    try:
        # 1. Copy base qcow2
        log.info("[Iter %d] Copying base qcow2...", iteration_num)
        subprocess.run(
            NICE_PREFIX + ["cp", str(base_qcow2), str(disk_path)],
            check=True,
        )

        # Resize disk to ensure enough space for upgrade
        subprocess.run(
            NICE_PREFIX + ["qemu-img", "resize", str(disk_path),
             f"{DEFAULT_VM_DISK_GB}G"],
            check=True, capture_output=True,
        )

        # 2. Create and start VM
        log.info("[Iter %d] Starting VM %s...", iteration_num, vm_name)
        dom = vm_mgr.create_vm(vm_name, str(disk_path),
                               memory_mb=vm_memory_mb, vcpus=vm_vcpus)

        # 3. Get VM IP and connect via SSH
        vm_ip = vm_mgr.get_vm_ip(dom)
        ssh = SSH(vm_ip, str(ssh_key_path))
        ssh.connect()

        # 4. Copy registry credentials
        copy_registry_auth(ssh)

        # 5. Pre-upgrade baseline
        log.info("[Iter %d] Collecting pre-upgrade baseline...", iteration_num)
        result.pre_upgrade = {
            "bootc_status": collect_bootc_status(ssh),
            "disk_usage": collect_disk_usage(ssh),
        }

        # Use pre-collected layer info (collected once per target)
        if target_layer_info:
            result.layer_details = target_layer_info.get("layers", [])
            result.layers_pulled = target_layer_info.get("layer_count")
            result.download_size_bytes = target_layer_info.get("total_compressed_size_bytes")

        # 6. Start resource monitor
        monitor = ResourceMonitor(vm_mgr, dom)
        monitor.start()

        # 7. Stage phase: run bootc switch
        log.info("[Iter %d] Running bootc switch to %s...",
                 iteration_num, target_image)
        stage_start = time.time()
        stage_start_ts = datetime.now().isoformat()

        # bootc writes progress to stderr with terminal control codes.
        # Use bash pipefail so we get bootc's exit code, not cat's.
        # Merge stderr into stdout and pipe through cat to strip tty codes.
        rc, out, err = ssh.run(
            f"bash -c 'set -o pipefail; bootc switch {target_image} 2>&1 | cat'",
            timeout=1800,  # 30 min max for large upgrades
        )

        stage_end = time.time()
        stage_end_ts = datetime.now().isoformat()
        stage_duration = stage_end - stage_start

        result.stage_phase = PhaseResult(
            name="stage",
            duration_sec=round(stage_duration, 2),
            start_time=stage_start_ts,
            end_time=stage_end_ts,
        )
        result.bootc_switch_output = out

        # Enrich results with parsed bootc output
        parsed = parse_bootc_switch_output(out)
        if parsed.get("download_size_bytes"):
            result.download_size_bytes = parsed["download_size_bytes"]
        if parsed.get("layers_needed"):
            result.layers_pulled = parsed["layers_needed"]
        result.bootc_parsed = parsed

        if rc != 0:
            result.error = f"bootc switch failed (rc={rc}): {out}"
            log.error("[Iter %d] %s", iteration_num, result.error)
            monitor.stop()
            return result

        log.info("[Iter %d] Stage phase completed in %.1fs",
                 iteration_num, stage_duration)

        # 8. Reboot phase
        log.info("[Iter %d] Rebooting VM...", iteration_num)
        reboot_start = time.time()
        reboot_start_ts = datetime.now().isoformat()

        ssh.run("systemctl reboot", timeout=10)
        ssh.close()

        # Wait for SSH to go down, then come back up
        wait_for_ssh_down(vm_ip)
        time.sleep(5)  # Grace period

        ssh = SSH(vm_ip, str(ssh_key_path))
        ssh.connect(timeout=SSH_TIMEOUT_SEC)

        reboot_end = time.time()
        reboot_end_ts = datetime.now().isoformat()
        reboot_duration = reboot_end - reboot_start

        result.reboot_phase = PhaseResult(
            name="reboot",
            duration_sec=round(reboot_duration, 2),
            start_time=reboot_start_ts,
            end_time=reboot_end_ts,
        )

        log.info("[Iter %d] Reboot phase completed in %.1fs",
                 iteration_num, reboot_duration)

        # 9. Post-upgrade stats
        log.info("[Iter %d] Collecting post-upgrade stats...", iteration_num)
        result.post_upgrade = {
            "bootc_status": collect_bootc_status(ssh),
            "disk_usage": collect_disk_usage(ssh),
        }

        # Compute disk delta
        pre_used = result.pre_upgrade.get("disk_usage", {}).get("used_bytes", 0)
        post_used = result.post_upgrade.get("disk_usage", {}).get("used_bytes", 0)
        if pre_used and post_used:
            result.disk_delta_bytes = post_used - pre_used

        # Total duration
        result.total_duration_sec = round(stage_duration + reboot_duration, 2)

        # 10. Stop monitor and collect samples
        samples = monitor.stop()
        result.cpu_samples = [asdict(s) for s in samples]
        result.memory_samples = [
            {
                "timestamp": s.timestamp,
                "rss_kb": s.memory_rss_kb,
                "available_kb": s.memory_available_kb,
            }
            for s in samples
        ]

        log.info(
            "[Iter %d] Complete — stage=%.1fs reboot=%.1fs total=%.1fs",
            iteration_num, stage_duration, reboot_duration,
            result.total_duration_sec,
        )

    except Exception as e:
        result.error = str(e)
        log.error("[Iter %d] Failed: %s", iteration_num, e, exc_info=True)

    finally:
        # Cleanup
        if ssh:
            ssh.close()
        if dom:
            vm_mgr.destroy_vm(dom)
        if disk_path.exists():
            disk_path.unlink(missing_ok=True)
            log.debug("Deleted disk %s", disk_path)

    return result


# ---------------------------------------------------------------------------
# Delta mode: preparation and iteration
# ---------------------------------------------------------------------------

def export_oci_archive(image_ref: str, output_path: Path,
                       authfile: Optional[str] = None) -> float:
    """Export a container image to an OCI archive via skopeo. Returns duration."""
    if output_path.exists():
        log.info("OCI archive already exists: %s", output_path)
        return 0.0
    log.info("Exporting %s to %s ...", image_ref, output_path)
    cmd = NICE_PREFIX + ["skopeo", "copy",
           "--override-arch", "amd64",
           "--remove-signatures",
           f"docker://{image_ref}",
           f"oci-archive:{output_path}"]
    if authfile:
        cmd.extend(["--authfile", authfile])
    start = time.time()
    subprocess.run(cmd, check=True)
    duration = time.time() - start
    size_mb = output_path.stat().st_size / 1024**2
    log.info("Exported %s (%.0f MB) in %.1fs", output_path.name, size_mb, duration)
    return duration


def create_delta(oci_delta_bin: Path, old_archive: Path, new_archive: Path,
                 delta_path: Path) -> tuple[float, int]:
    """Run oci-delta create. Returns (duration_sec, delta_size_bytes)."""
    if delta_path.exists():
        delta_path.unlink()
    # oci-delta create is extremely CPU+IO intensive (binary diffing of
    # multi-GB archives).  Pin it to half the available cores via taskset
    # on top of the nice/ionice wrapper so it can't starve the desktop.
    ncpus = os.cpu_count() or 4
    half = max(1, ncpus // 2)
    cpu_mask = ",".join(str(c) for c in range(half))
    log.info("Creating delta: %s → %s (pinned to CPUs %s)",
             old_archive.name, new_archive.name, cpu_mask)
    start = time.time()
    subprocess.run(
        NICE_PREFIX + ["taskset", "-c", cpu_mask,
         str(oci_delta_bin), "create", "--verbose",
         str(old_archive), str(new_archive), str(delta_path)],
        check=True,
    )
    duration = time.time() - start
    size = delta_path.stat().st_size
    log.info("Delta created: %s (%.1f MB) in %.1fs",
             delta_path.name, size / 1024**2, duration)
    return duration, size


def prepare_delta_artifacts(
    base_image: str,
    target_image: str,
    oci_delta_bin: Path,
    output_dir: Path,
) -> dict:
    """Prepare OCI archives and delta file for a given base→target pair.

    Done once per target, outside the iteration loop.
    Returns a dict with paths and timing info.
    """
    archives_dir = output_dir / "archives"
    archives_dir.mkdir(parents=True, exist_ok=True)

    authfile = _find_authfile()

    # Sanitize image refs for filenames
    def safe_name(ref: str) -> str:
        return ref.split("/")[-1].replace(":", "-")

    old_archive = archives_dir / f"{safe_name(base_image)}.oci-archive"
    new_archive = archives_dir / f"{safe_name(target_image)}.oci-archive"
    delta_file = archives_dir / f"{safe_name(base_image)}-to-{safe_name(target_image)}.delta"

    # Export both images
    old_export_time = export_oci_archive(base_image, old_archive, authfile)
    new_export_time = export_oci_archive(target_image, new_archive, authfile)

    # Create delta
    delta_time, delta_size = create_delta(
        oci_delta_bin, old_archive, new_archive, delta_file
    )

    return {
        "old_archive": str(old_archive),
        "new_archive": str(new_archive),
        "delta_file": str(delta_file),
        "old_archive_size_bytes": old_archive.stat().st_size,
        "new_archive_size_bytes": new_archive.stat().st_size,
        "delta_size_bytes": delta_size,
        "old_export_duration_sec": round(old_export_time, 2),
        "new_export_duration_sec": round(new_export_time, 2),
        "delta_create_duration_sec": round(delta_time, 2),
    }


def run_iteration_delta(
    iteration_num: int,
    base_qcow2: Path,
    target_image: str,
    ssh_key_path: Path,
    vm_mgr: VMManager,
    work_dir: Path,
    oci_delta_bin: Path,
    delta_artifacts: dict,
    target_layer_info: Optional[dict] = None,
    vm_memory_mb: int = DEFAULT_VM_MEMORY_MB,
    vm_vcpus: int = DEFAULT_VM_VCPUS,
) -> IterationResult:
    """Run a single benchmark iteration using oci-delta."""
    result = IterationResult(iteration=iteration_num, mode="delta")
    vm_name = f"bootc-bench-delta-{iteration_num}-{uuid.uuid4().hex[:8]}"
    disk_path = work_dir / f"{vm_name}.qcow2"
    dom = None
    ssh = None

    delta_file = Path(delta_artifacts["delta_file"])
    result.delta_file_size_bytes = delta_artifacts["delta_size_bytes"]

    # Pre-populate layer info from pre-collected data
    if target_layer_info:
        result.layer_details = target_layer_info.get("layers", [])
        result.layers_pulled = target_layer_info.get("layer_count")
        result.download_size_bytes = delta_artifacts["delta_size_bytes"]

    try:
        # 1. Copy base qcow2
        log.info("[Delta Iter %d] Copying base qcow2...", iteration_num)
        subprocess.run(NICE_PREFIX + ["cp", str(base_qcow2), str(disk_path)], check=True)
        subprocess.run(
            NICE_PREFIX + ["qemu-img", "resize", str(disk_path), f"{DEFAULT_VM_DISK_GB}G"],
            check=True, capture_output=True,
        )

        # 2. Create and start VM
        log.info("[Delta Iter %d] Starting VM %s...", iteration_num, vm_name)
        dom = vm_mgr.create_vm(vm_name, str(disk_path),
                               memory_mb=vm_memory_mb, vcpus=vm_vcpus)

        # 3. Get VM IP and connect via SSH
        vm_ip = vm_mgr.get_vm_ip(dom)
        ssh = SSH(vm_ip, str(ssh_key_path))
        ssh.connect()

        # 4. Pre-upgrade baseline
        log.info("[Delta Iter %d] Collecting pre-upgrade baseline...", iteration_num)
        result.pre_upgrade = {
            "bootc_status": collect_bootc_status(ssh),
            "disk_usage": collect_disk_usage(ssh),
        }

        # 5. Start resource monitor
        monitor = ResourceMonitor(vm_mgr, dom)
        monitor.start()

        # 6. Transfer delta file + oci-delta binary into VM
        log.info("[Delta Iter %d] Transferring delta (%.1f MB) and oci-delta binary...",
                 iteration_num, delta_file.stat().st_size / 1024**2)
        transfer_start = time.time()
        transfer_start_ts = datetime.now().isoformat()

        ssh.upload_file(str(delta_file), "/tmp/update.delta")
        ssh.upload_file(str(oci_delta_bin), "/tmp/oci-delta")
        ssh.run("chmod +x /tmp/oci-delta")

        transfer_end = time.time()
        transfer_duration = transfer_end - transfer_start

        result.delta_transfer_phase = PhaseResult(
            name="delta_transfer",
            duration_sec=round(transfer_duration, 2),
            start_time=transfer_start_ts,
            end_time=datetime.now().isoformat(),
        )
        log.info("[Delta Iter %d] Transfer completed in %.1fs",
                 iteration_num, transfer_duration)

        # 7. Apply delta → reconstruct OCI archive
        log.info("[Delta Iter %d] Applying delta...", iteration_num)
        apply_start = time.time()
        apply_start_ts = datetime.now().isoformat()

        rc, out, err = ssh.run(
            "bash -c 'set -o pipefail; /tmp/oci-delta apply "
            "/tmp/update.delta /tmp/new.oci-archive 2>&1 | cat'",
            timeout=1800,
        )

        apply_end = time.time()
        apply_duration = apply_end - apply_start

        result.delta_apply_phase = PhaseResult(
            name="delta_apply",
            duration_sec=round(apply_duration, 2),
            start_time=apply_start_ts,
            end_time=datetime.now().isoformat(),
        )
        result.delta_apply_output = out

        if rc != 0:
            result.error = f"oci-delta apply failed (rc={rc}): {out}"
            log.error("[Delta Iter %d] %s", iteration_num, result.error)
            monitor.stop()
            return result

        log.info("[Delta Iter %d] Delta apply completed in %.1fs",
                 iteration_num, apply_duration)

        # 8. Stage phase: bootc switch to local OCI archive
        log.info("[Delta Iter %d] Running bootc switch (oci-archive)...", iteration_num)
        stage_start = time.time()
        stage_start_ts = datetime.now().isoformat()

        rc, out, err = ssh.run(
            "bash -c 'set -o pipefail; bootc switch "
            "--transport=oci-archive /tmp/new.oci-archive 2>&1 | cat'",
            timeout=1800,
        )

        stage_end = time.time()
        stage_duration = stage_end - stage_start

        result.stage_phase = PhaseResult(
            name="stage",
            duration_sec=round(stage_duration, 2),
            start_time=stage_start_ts,
            end_time=datetime.now().isoformat(),
        )
        result.bootc_switch_output = out

        parsed = parse_bootc_switch_output(out)
        result.bootc_parsed = parsed

        if rc != 0:
            result.error = f"bootc switch failed (rc={rc}): {out}"
            log.error("[Delta Iter %d] %s", iteration_num, result.error)
            monitor.stop()
            return result

        log.info("[Delta Iter %d] Stage phase completed in %.1fs",
                 iteration_num, stage_duration)

        # 9. Reboot phase
        log.info("[Delta Iter %d] Rebooting VM...", iteration_num)
        reboot_start = time.time()
        reboot_start_ts = datetime.now().isoformat()

        ssh.run("systemctl reboot", timeout=10)
        ssh.close()

        wait_for_ssh_down(vm_ip)
        time.sleep(5)

        ssh = SSH(vm_ip, str(ssh_key_path))
        ssh.connect(timeout=SSH_TIMEOUT_SEC)

        reboot_end = time.time()
        reboot_duration = reboot_end - reboot_start

        result.reboot_phase = PhaseResult(
            name="reboot",
            duration_sec=round(reboot_duration, 2),
            start_time=reboot_start_ts,
            end_time=datetime.now().isoformat(),
        )
        log.info("[Delta Iter %d] Reboot completed in %.1fs",
                 iteration_num, reboot_duration)

        # 10. Post-upgrade stats
        log.info("[Delta Iter %d] Collecting post-upgrade stats...", iteration_num)
        result.post_upgrade = {
            "bootc_status": collect_bootc_status(ssh),
            "disk_usage": collect_disk_usage(ssh),
        }

        pre_used = result.pre_upgrade.get("disk_usage", {}).get("used_bytes", 0)
        post_used = result.post_upgrade.get("disk_usage", {}).get("used_bytes", 0)
        if pre_used and post_used:
            result.disk_delta_bytes = post_used - pre_used

        # Total = transfer + apply + stage + reboot
        result.total_duration_sec = round(
            transfer_duration + apply_duration + stage_duration + reboot_duration, 2
        )

        # Stop monitor
        samples = monitor.stop()
        result.cpu_samples = [asdict(s) for s in samples]
        result.memory_samples = [
            {"timestamp": s.timestamp, "rss_kb": s.memory_rss_kb,
             "available_kb": s.memory_available_kb}
            for s in samples
        ]

        log.info(
            "[Delta Iter %d] Complete — transfer=%.1fs apply=%.1fs "
            "stage=%.1fs reboot=%.1fs total=%.1fs",
            iteration_num, transfer_duration, apply_duration,
            stage_duration, reboot_duration, result.total_duration_sec,
        )

    except Exception as e:
        result.error = str(e)
        log.error("[Delta Iter %d] Failed: %s", iteration_num, e, exc_info=True)

    finally:
        if ssh:
            ssh.close()
        if dom:
            vm_mgr.destroy_vm(dom)
        if disk_path.exists():
            disk_path.unlink(missing_ok=True)

    return result


# ---------------------------------------------------------------------------
# Summary statistics
# ---------------------------------------------------------------------------

def compute_summary(results: list[IterationResult]) -> dict:
    """Compute summary statistics across successful iterations."""
    successful = [r for r in results if r.error is None]
    if not successful:
        return {"error": "No successful iterations"}

    def stat(values: list[float]) -> dict:
        if len(values) < 2:
            return {
                "mean": values[0] if values else 0,
                "stddev": 0,
                "min": values[0] if values else 0,
                "max": values[0] if values else 0,
                "count": len(values),
            }
        return {
            "mean": round(statistics.mean(values), 2),
            "stddev": round(statistics.stdev(values), 2),
            "min": round(min(values), 2),
            "max": round(max(values), 2),
            "count": len(values),
        }

    summary = {}

    stage_times = [r.stage_phase.duration_sec for r in successful
                   if r.stage_phase]
    if stage_times:
        summary["stage_duration_sec"] = stat(stage_times)

    reboot_times = [r.reboot_phase.duration_sec for r in successful
                    if r.reboot_phase]
    if reboot_times:
        summary["reboot_duration_sec"] = stat(reboot_times)

    total_times = [r.total_duration_sec for r in successful]
    if total_times:
        summary["total_duration_sec"] = stat(total_times)

    disk_deltas = [r.disk_delta_bytes for r in successful
                   if r.disk_delta_bytes is not None]
    if disk_deltas:
        summary["disk_delta_bytes"] = stat(disk_deltas)

    # Delta-mode phases
    transfer_times = [r.delta_transfer_phase.duration_sec for r in successful
                      if r.delta_transfer_phase]
    if transfer_times:
        summary["delta_transfer_duration_sec"] = stat(transfer_times)

    apply_times = [r.delta_apply_phase.duration_sec for r in successful
                   if r.delta_apply_phase]
    if apply_times:
        summary["delta_apply_duration_sec"] = stat(apply_times)

    summary["successful_iterations"] = len(successful)
    summary["failed_iterations"] = len(results) - len(successful)

    return summary


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run_benchmark(args) -> dict:
    """Run the full benchmark suite."""
    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    # Work dir for temporary VM disks.  Defaults to /var/tmp/bootc-bench
    # which is world-accessible so the qemu process (uid 107) can read
    # disk images without sudo or ACLs.
    if args.work_dir:
        work_dir = Path(args.work_dir).resolve()
    else:
        work_dir = Path("/var/tmp/bootc-bench")
    work_dir.mkdir(parents=True, exist_ok=True)

    targets = args.targets or DEFAULT_TARGETS
    base_image = args.base_image

    # Generate SSH keys
    ssh_priv, ssh_pub = generate_ssh_keypair(output_dir)

    # Build base qcow2 (or use existing)
    if args.qcow2:
        base_qcow2 = Path(args.qcow2).resolve()
        if not base_qcow2.exists():
            raise FileNotFoundError(f"Provided qcow2 not found: {base_qcow2}")
        log.info("Using provided qcow2: %s", base_qcow2)
    else:
        base_qcow2 = build_base_qcow2(base_image, output_dir, ssh_pub)

    # Connect to libvirt
    vm_mgr = VMManager(uri=args.libvirt_uri)
    vm_mgr.connect()

    mode = getattr(args, "mode", "baseline")
    oci_delta_bin = None
    if mode == "delta":
        oci_delta_bin = Path(args.oci_delta_bin).resolve()
        if not oci_delta_bin.exists():
            raise FileNotFoundError(f"oci-delta binary not found: {oci_delta_bin}")

    all_results = {
        "config": {
            "base_image": base_image,
            "targets": targets,
            "iterations": args.iterations,
            "vm_memory_mb": args.vm_memory,
            "vm_vcpus": args.vm_vcpus,
            "mode": mode,
            "timestamp": datetime.now().isoformat(),
        },
        "benchmarks": [],
    }

    try:
        for target in targets:
            log.info("=" * 60)
            log.info("Benchmarking [%s] upgrade: %s -> %s",
                     mode, base_image, target)
            log.info("=" * 60)

            # Collect layer info once per target from the host
            log.info("Collecting target image layer info from host...")
            target_layer_info = get_image_layer_info_host(target)
            if "error" in target_layer_info:
                log.warning("Could not collect layer info: %s",
                            target_layer_info["error"])

            # Prepare delta artifacts if in delta mode
            delta_artifacts = None
            if mode == "delta":
                log.info("Preparing delta artifacts...")
                delta_artifacts = prepare_delta_artifacts(
                    base_image, target, oci_delta_bin, output_dir,
                )

            benchmark = {
                "base_image": base_image,
                "target_image": target,
                "target_layer_info": target_layer_info,
                "mode": mode,
                "delta_artifacts": delta_artifacts,
                "iterations": [],
                "summary": {},
            }

            iteration_results = []
            for i in range(1, args.iterations + 1):
                log.info("-" * 40)
                log.info("Iteration %d/%d [%s]", i, args.iterations, mode)
                log.info("-" * 40)

                if mode == "delta":
                    result = run_iteration_delta(
                        iteration_num=i,
                        base_qcow2=base_qcow2,
                        target_image=target,
                        ssh_key_path=ssh_priv,
                        vm_mgr=vm_mgr,
                        work_dir=work_dir,
                        oci_delta_bin=oci_delta_bin,
                        delta_artifacts=delta_artifacts,
                        target_layer_info=target_layer_info,
                        vm_memory_mb=args.vm_memory,
                        vm_vcpus=args.vm_vcpus,
                    )
                else:
                    result = run_iteration(
                        iteration_num=i,
                        base_qcow2=base_qcow2,
                        target_image=target,
                        ssh_key_path=ssh_priv,
                        vm_mgr=vm_mgr,
                        work_dir=work_dir,
                        target_layer_info=target_layer_info,
                        vm_memory_mb=args.vm_memory,
                        vm_vcpus=args.vm_vcpus,
                    )
                iteration_results.append(result)
                benchmark["iterations"].append(asdict(result))

            benchmark["summary"] = compute_summary(iteration_results)
            all_results["benchmarks"].append(benchmark)

    finally:
        vm_mgr.close()

    # Write results
    results_file = output_dir / "results.json"
    with open(results_file, "w") as f:
        json.dump(all_results, f, indent=2, default=str)
    log.info("Results written to %s", results_file)

    return all_results


def main():
    parser = argparse.ArgumentParser(
        description="bootc-bench: Benchmark bootc VM upgrades",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            Examples:
              # Run with defaults (3 iterations, all targets)
              %(prog)s

              # Single target, 5 iterations
              %(prog)s -t registry.redhat.io/rhel9/rhel-bootc:9.8 -n 5

              # Use a pre-built qcow2
              %(prog)s --qcow2 /path/to/base.qcow2

              # Custom output directory
              %(prog)s -o /tmp/bench-results
        """),
    )
    parser.add_argument(
        "-b", "--base-image", default=DEFAULT_BASE_IMAGE,
        help=f"Base bootc image (default: {DEFAULT_BASE_IMAGE})",
    )
    parser.add_argument(
        "-t", "--targets", nargs="+", default=None,
        help="Upgrade target image(s) (default: 9.8, 10.0, 10.2)",
    )
    parser.add_argument(
        "-n", "--iterations", type=int, default=DEFAULT_ITERATIONS,
        help=f"Number of iterations per target (default: {DEFAULT_ITERATIONS})",
    )
    parser.add_argument(
        "-o", "--output-dir", default="./bootc-bench-results",
        help="Output directory for results and artifacts",
    )
    parser.add_argument(
        "-w", "--work-dir", default=None,
        help="Working directory for temporary VM disks (default: <output-dir>/work)",
    )
    parser.add_argument(
        "--qcow2", default=None,
        help="Path to a pre-built base qcow2 (skip image building)",
    )
    parser.add_argument(
        "--libvirt-uri", default="qemu:///system",
        help="Libvirt connection URI (default: qemu:///system)",
    )
    parser.add_argument(
        "--vm-memory", type=int, default=DEFAULT_VM_MEMORY_MB,
        help=f"VM memory in MB (default: {DEFAULT_VM_MEMORY_MB})",
    )
    parser.add_argument(
        "--vm-vcpus", type=int, default=DEFAULT_VM_VCPUS,
        help=f"VM vCPUs (default: {DEFAULT_VM_VCPUS})",
    )
    parser.add_argument(
        "-m", "--mode", choices=["baseline", "delta"], default="baseline",
        help="Benchmark mode: 'baseline' (full pull) or 'delta' (oci-delta)",
    )
    parser.add_argument(
        "--oci-delta-bin", default=OCI_DELTA_BIN_DEFAULT,
        help=f"Path to oci-delta binary (default: {OCI_DELTA_BIN_DEFAULT})",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true",
        help="Enable verbose/debug logging",
    )

    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-8s %(message)s",
        datefmt="%H:%M:%S",
    )

    try:
        results = run_benchmark(args)
        # Print quick summary
        for bench in results.get("benchmarks", []):
            target = bench["target_image"]
            summary = bench.get("summary", {})
            total = summary.get("total_duration_sec", {})
            print(f"\n{'=' * 50}")
            print(f"Target: {target}")
            if isinstance(total, dict):
                print(f"  Total: {total.get('mean', 'N/A')}s "
                      f"(±{total.get('stddev', 'N/A')}s)")
            stage = summary.get("stage_duration_sec", {})
            if isinstance(stage, dict):
                print(f"  Stage: {stage.get('mean', 'N/A')}s "
                      f"(±{stage.get('stddev', 'N/A')}s)")
            reboot = summary.get("reboot_duration_sec", {})
            if isinstance(reboot, dict):
                print(f"  Reboot: {reboot.get('mean', 'N/A')}s "
                      f"(±{reboot.get('stddev', 'N/A')}s)")
            print(f"  Success: {summary.get('successful_iterations', 0)}/"
                  f"{summary.get('successful_iterations', 0) + summary.get('failed_iterations', 0)}")
    except KeyboardInterrupt:
        print("\nInterrupted")
        raise SystemExit(1)
    except Exception as e:
        log.error("Fatal: %s", e, exc_info=True)
        raise SystemExit(1)


if __name__ == "__main__":
    main()

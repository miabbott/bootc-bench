#!/usr/bin/env python3
"""bootc-bench: Benchmarking harness for bootc VM upgrades.

Measures timing, resource usage, and image statistics during bootc upgrades
of VMs provisioned via libvirt/qemu from bootc-image-builder qcow2 images.
"""

import argparse
import json
import logging
import os
import re
import shutil
import socket
import statistics
import subprocess
import tempfile
import textwrap
import threading
import urllib.request
import urllib.error
import time
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Optional
from xml.etree import ElementTree

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
# Upstream bootc-image-builder, used automatically for non-RHEL base images
# (e.g. Fedora) that don't have registry.redhat.io entitlements.
FEDORA_BIB_IMAGE = "quay.io/centos-bootc/bootc-image-builder:latest"
OCI_DELTA_BIN_DEFAULT = "./oci-delta"
REGISTRY_CONTAINER_NAME = "bootc-bench-registry"
REGISTRY_IMAGE = "docker.io/library/registry:2"
REGISTRY_HOST_PORT = 5000
# The host IP as seen from VMs on the default libvirt network.
LIBVIRT_HOST_IP = "192.168.122.1"
MONITOR_INTERVAL_SEC = 2
SSH_TIMEOUT_SEC = 300
SSH_PORT = 22

DEFAULT_PACKAGES = [
    "httpd", "postgresql-server", "vim-enhanced", "tmux",
    "git", "make", "python3-pip", "bind-utils",
]

DEFAULT_IMAGE_REPO = "registry.redhat.io/rhel9/rhel-bootc"

# Simulated network conditions, applied via tc netem on the VM's host-side
# tap device for the duration of the upgrade phase. Chosen to bracket a
# realistic range: a healthy broadband link vs. a constrained/edge link
# where oci-delta's smaller transfer size should matter more.
NETWORK_PROFILES = {
    "none": None,
    "broadband": {"rate": "100mbit", "delay": "10ms"},
    "constrained": {"rate": "5mbit", "delay": "150ms", "loss": "1%"},
}


# ---------------------------------------------------------------------------
# Tag discovery
# ---------------------------------------------------------------------------

def discover_zstream_tags(image_repo: str, y_stream: str) -> tuple[str, str]:
    """Discover the oldest and newest build tags for a y-stream.

    Queries the registry via skopeo list-tags and filters for tags
    matching the pattern '{y_stream}-{timestamp}' (excluding '-source' tags).

    Returns (oldest_tag, newest_tag) as full image references.
    """
    result = subprocess.run(
        ["skopeo", "list-tags", f"docker://{image_repo}"],
        capture_output=True, text=True, check=True,
    )
    all_tags = json.loads(result.stdout).get("Tags", [])

    build_tags = sorted([
        t for t in all_tags
        if re.match(rf'^{re.escape(y_stream)}-\d+$', t)
    ])

    if not build_tags:
        raise ValueError(
            f"No build tags found for {image_repo} y-stream {y_stream}. "
            f"Available tags with prefix '{y_stream}': "
            f"{[t for t in all_tags if t.startswith(y_stream)][:10]}"
        )

    oldest = f"{image_repo}:{build_tags[0]}"
    newest = f"{image_repo}:{build_tags[-1]}"
    log.info("Z-stream range for %s: %s → %s (%d builds)",
             y_stream, build_tags[0], build_tags[-1], len(build_tags))
    return oldest, newest


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class MonitorSample:
    """A single CPU/memory/network/disk sample from virsh domstats."""
    timestamp: float
    cpu_time_ns: int = 0
    cpu_user_ns: int = 0
    cpu_system_ns: int = 0
    memory_rss_kb: int = 0
    memory_available_kb: int = 0
    memory_used_kb: int = 0
    # Cumulative counters (as reported by libvirt) for the VM's primary
    # network interface and primary block device. These are running totals
    # since VM start, not deltas -- callers diff last-vs-first sample.
    net_rx_bytes: int = 0
    net_tx_bytes: int = 0
    disk_rd_bytes: int = 0
    disk_wr_bytes: int = 0


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
    variant: str = "vanilla"        # "vanilla" or "customized"
    upgrade_type: str = "y-stream"  # "z-stream" or "y-stream"
    network_profile: str = "none"   # "none", "broadband", "constrained"
    cpu_samples: list = field(default_factory=list)
    memory_samples: list = field(default_factory=list)
    network_samples: list = field(default_factory=list)
    disk_io_samples: list = field(default_factory=list)
    # Net/disk I/O observed during the monitored window (last sample minus
    # first sample of the corresponding cumulative libvirt counter).
    net_rx_bytes_delta: Optional[int] = None
    net_tx_bytes_delta: Optional[int] = None
    disk_rd_bytes_delta: Optional[int] = None
    disk_wr_bytes_delta: Optional[int] = None
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

    def get_primary_iface_dev(self, dom: libvirt.virDomain) -> Optional[str]:
        """Look up the host-side tap/vnet device name for the VM's first
        network interface, by parsing the live domain XML.

        Libvirt auto-assigns this (e.g. 'vnet0') when the VM starts since
        our domain XML doesn't pin a <target dev=.../>. Needed both for
        interfaceStats() sampling and for applying tc netem shaping.
        """
        try:
            xml_desc = dom.XMLDesc(0)
            root = ElementTree.fromstring(xml_desc)
            iface = root.find("./devices/interface")
            if iface is None:
                return None
            target = iface.find("target")
            if target is None:
                return None
            return target.get("dev")
        except (libvirt.libvirtError, ElementTree.ParseError) as e:
            log.debug("Failed to determine primary interface device: %s", e)
            return None

    def get_domstats(self, dom: libvirt.virDomain,
                      iface_dev: Optional[str] = None,
                      disk_dev: str = "vda") -> MonitorSample:
        """Collect a single stats sample via virsh domstats."""
        sample = MonitorSample(timestamp=time.time())
        try:
            stats = dom.memoryStats()
            sample.memory_rss_kb = stats.get("rss", 0)
            sample.memory_available_kb = stats.get("available", 0)
            sample.memory_used_kb = stats.get("rss", 0)
        except libvirt.libvirtError as e:
            log.debug("Failed to collect memory stats: %s", e)

        try:
            # CPU stats: array of per-vcpu stats + total
            info = dom.info()
            # info[4] is CPU time in nanoseconds
            sample.cpu_time_ns = info[4] if len(info) > 4 else 0
        except libvirt.libvirtError as e:
            log.debug("Failed to collect CPU stats: %s", e)

        if iface_dev:
            try:
                # (rx_bytes, rx_packets, rx_errs, rx_drop,
                #  tx_bytes, tx_packets, tx_errs, tx_drop)
                net_stats = dom.interfaceStats(iface_dev)
                sample.net_rx_bytes = net_stats[0]
                sample.net_tx_bytes = net_stats[4]
            except libvirt.libvirtError as e:
                log.debug("Failed to collect interface stats: %s", e)

        try:
            # (rd_req, rd_bytes, wr_req, wr_bytes, errs)
            block_stats = dom.blockStats(disk_dev)
            sample.disk_rd_bytes = block_stats[1]
            sample.disk_wr_bytes = block_stats[3]
        except libvirt.libvirtError as e:
            log.debug("Failed to collect block stats: %s", e)

        return sample

    def destroy_vm(self, dom: libvirt.virDomain):
        """Force-stop and undefine a VM."""
        name = dom.name()
        try:
            if dom.isActive():
                dom.destroy()
        except libvirt.libvirtError as e:
            log.debug("Failed to destroy VM %s: %s", name, e)
        try:
            dom.undefineFlags(
                libvirt.VIR_DOMAIN_UNDEFINE_NVRAM
                | libvirt.VIR_DOMAIN_UNDEFINE_SNAPSHOTS_METADATA
            )
        except libvirt.libvirtError:
            try:
                dom.undefine()
            except libvirt.libvirtError as e:
                log.debug("Failed to undefine VM %s: %s", name, e)
        log.info("VM %s destroyed", name)

    def reboot_vm(self, dom: libvirt.virDomain):
        """Send reboot signal to VM."""
        dom.reboot(0)
        log.info("VM %s reboot initiated", dom.name())


# ---------------------------------------------------------------------------
# Resource monitor (runs in background thread)
# ---------------------------------------------------------------------------

class ResourceMonitor:
    """Collect CPU/memory/network/disk samples in a background thread."""

    def __init__(self, vm_mgr: VMManager, dom: libvirt.virDomain,
                 interval: float = MONITOR_INTERVAL_SEC,
                 iface_dev: Optional[str] = None,
                 disk_dev: str = "vda"):
        self.vm_mgr = vm_mgr
        self.dom = dom
        self.interval = interval
        self.iface_dev = iface_dev
        self.disk_dev = disk_dev
        self.samples: list[MonitorSample] = []
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self):
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        log.info("Resource monitor started (iface=%s disk=%s)",
                 self.iface_dev, self.disk_dev)

    def stop(self) -> list[MonitorSample]:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=10)
        log.info("Resource monitor stopped (%d samples)", len(self.samples))
        return self.samples

    def _run(self):
        while not self._stop.is_set():
            try:
                sample = self.vm_mgr.get_domstats(
                    self.dom, iface_dev=self.iface_dev,
                    disk_dev=self.disk_dev)
                self.samples.append(sample)
            except Exception as e:
                log.debug("Monitor sample error: %s", e)
            self._stop.wait(self.interval)


# ---------------------------------------------------------------------------
# Network shaping (tc netem on the VM's host-side tap device)
# ---------------------------------------------------------------------------

def apply_network_shaping(iface_dev: str, profile: str):
    """Apply a tc netem profile to the VM's host-side tap device.

    No-op for profile 'none' or an unknown iface_dev. Requires root
    (CAP_NET_ADMIN); invoked via sudo like the rest of the harness's
    privileged operations.
    """
    params = NETWORK_PROFILES.get(profile)
    if not params or not iface_dev:
        return
    cmd = ["sudo", "tc", "qdisc", "add", "dev", iface_dev, "root", "netem"]
    for key, value in params.items():
        cmd.extend([key, value])
    log.info("Applying network profile '%s' to %s: %s",
             profile, iface_dev, " ".join(f"{k} {v}" for k, v in params.items()))
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        log.warning("Failed to apply network shaping on %s: %s",
                    iface_dev, result.stderr.strip())


def clear_network_shaping(iface_dev: Optional[str]):
    """Remove any tc netem qdisc from the VM's host-side tap device.

    Safe to call even if no shaping was applied (e.g. profile 'none') --
    errors from tc when there's nothing to delete are logged at debug
    level only.
    """
    if not iface_dev:
        return
    result = subprocess.run(
        ["sudo", "tc", "qdisc", "del", "dev", iface_dev, "root"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        log.debug("tc qdisc del on %s: %s", iface_dev, result.stderr.strip())


# ---------------------------------------------------------------------------
# Local registry management
# ---------------------------------------------------------------------------

def start_local_registry() -> str:
    """Start a local OCI registry container. Returns the host:port address.

    Uses rootless podman so no sudo is needed.  The registry listens on
    localhost:<REGISTRY_HOST_PORT> (host side) and is reachable from
    libvirt VMs at <LIBVIRT_HOST_IP>:<REGISTRY_HOST_PORT>.
    """
    addr = f"localhost:{REGISTRY_HOST_PORT}"
    vm_addr = f"{LIBVIRT_HOST_IP}:{REGISTRY_HOST_PORT}"

    # Check if already running
    rc = subprocess.run(
        ["podman", "inspect", REGISTRY_CONTAINER_NAME],
        capture_output=True,
    ).returncode
    if rc == 0:
        log.info("Local registry already running at %s (VM: %s)", addr, vm_addr)
        return vm_addr

    log.info("Starting local registry container (%s)...", REGISTRY_IMAGE)
    subprocess.run(
        ["podman", "run", "-d",
         "--name", REGISTRY_CONTAINER_NAME,
         "-p", f"{REGISTRY_HOST_PORT}:5000",
         REGISTRY_IMAGE],
        check=True,
    )
    # Wait briefly for the registry to be ready
    for _ in range(10):
        try:
            urllib.request.urlopen(f"http://{addr}/v2/", timeout=2)
            break
        except Exception:
            time.sleep(1)
    log.info("Local registry running at %s (VM: %s)", addr, vm_addr)
    return vm_addr


def stop_local_registry():
    """Stop and remove the local registry container."""
    subprocess.run(
        ["podman", "rm", "-f", REGISTRY_CONTAINER_NAME],
        capture_output=True,
    )
    log.info("Local registry stopped")


def push_to_local_registry(
    source_image: str,
    tag: str,
    compress_format: Optional[str] = None,
) -> None:
    """Push a container image to the local registry via skopeo.

    Args:
        source_image: Full source image reference (e.g. registry.redhat.io/...).
        tag: Destination tag in the local registry (e.g. "rhel-bootc:9.8-gzip").
        compress_format: Destination compression format (e.g. "zstd:chunked")
            or None for the source format.
    """
    dest = f"docker://localhost:{REGISTRY_HOST_PORT}/{tag}"
    authfile = _find_authfile()

    # Check if already pushed by querying the registry
    repo, ref = tag.split(":", 1) if ":" in tag else (tag, "latest")
    try:
        url = f"http://localhost:{REGISTRY_HOST_PORT}/v2/{repo}/manifests/{ref}"
        req = urllib.request.Request(url, headers={
            "Accept": "application/vnd.oci.image.manifest.v1+json,"
                      "application/vnd.docker.distribution.manifest.v2+json"
        })
        urllib.request.urlopen(req, timeout=5)
        fmt_label = compress_format or "source"
        log.info("Image already in local registry: %s (%s)", tag, fmt_label)
        return
    except Exception:
        pass  # Not present, need to push

    cmd = NICE_PREFIX + [
        "skopeo", "copy",
        "--dest-tls-verify=false",
    ]
    if compress_format:
        cmd.extend(["--dest-compress-format", compress_format])
    cmd.extend([f"docker://{source_image}", dest])
    if authfile:
        cmd.extend(["--src-authfile", authfile])

    fmt_label = compress_format or "source"
    log.info("Pushing %s to local registry as %s (%s)...",
             source_image, tag, fmt_label)
    subprocess.run(cmd, check=True)
    log.info("Pushed %s", tag)


def _push_local_image_to_registry(
    local_tag: str,
    registry_tag: str,
    compress_format: Optional[str] = None,
) -> None:
    """Push an image from local (root) podman storage to the local registry."""
    dest = f"docker://localhost:{REGISTRY_HOST_PORT}/{registry_tag}"
    cmd = NICE_PREFIX + [
        "sudo", "skopeo", "copy",
        "--dest-tls-verify=false",
    ]
    if compress_format:
        cmd.extend(["--dest-compress-format", compress_format])
    cmd.extend([f"containers-storage:{local_tag}", dest])

    log.info("Pushing local image %s to registry as %s...", local_tag, registry_tag)
    subprocess.run(cmd, check=True)
    log.info("Pushed %s", registry_tag)


def _get_local_image_layer_info(local_tag: str) -> dict:
    """Collect layer info from a local (root) podman image."""
    try:
        result = subprocess.run(
            ["sudo", "skopeo", "inspect", f"containers-storage:{local_tag}"],
            capture_output=True, text=True, check=True,
        )
        inspect_data = json.loads(result.stdout)
    except (subprocess.CalledProcessError, json.JSONDecodeError) as e:
        return {"error": str(e)}

    # skopeo inspect on containers-storage doesn't give layer details in the
    # same way as a registry manifest. Get what we can.
    layers = inspect_data.get("Layers", [])
    return {
        "layer_count": len(layers),
        "total_compressed_size_bytes": 0,  # Not available from local storage
        "layers": [],
        "digest": inspect_data.get("Digest", ""),
        "labels": inspect_data.get("Labels", {}),
    }


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


def generate_ssh_keypair(ssh_dir: Path) -> tuple[Path, Path]:
    """Generate an SSH keypair for VM access, return (private, public) paths."""
    ssh_dir.mkdir(parents=True, exist_ok=True)
    priv_key = ssh_dir / "bootc-bench-key"
    pub_key = ssh_dir / "bootc-bench-key.pub"
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
    bib_image: str = BIB_IMAGE,
) -> Path:
    """Build a qcow2 disk image from the base bootc container image.

    Steps:
    1. Create a derived Containerfile that bakes in the SSH public key.
    2. Build the derived container with podman.
    3. Run bootc-image-builder to produce a qcow2.

    Returns the path to the qcow2 file.
    """
    # Include the base image in the cache filename so different base images
    # don't collide (e.g. switching from 9.6 to 9.8 base).
    cache_key = safe_name(base_image)
    qcow2_path = output_dir / f"base-{cache_key}.qcow2"
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
        # Trust the local benchmark registry (insecure, no TLS)
        RUN printf '[[registry]]\\nlocation = "{LIBVIRT_HOST_IP}:{REGISTRY_HOST_PORT}"\\ninsecure = true\\n' \\
            > /etc/containers/registries.conf.d/bootc-bench-local.conf
    """))

    derived_tag = "localhost/bootc-bench-base:latest"
    log.info("Building derived container image: %s", derived_tag)

    # Find the host's registry auth file so sudo podman can pull images
    authfile = _find_authfile()
    build_cmd = NICE_PREFIX + ["sudo", "podman", "build"]
    if authfile:
        build_cmd.append(f"--authfile={authfile}")
        log.info("Using authfile: %s", authfile)
    build_cmd.extend(["-t", derived_tag, "-f", str(containerfile), str(build_dir)])

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
    bib_cmd.extend([bib_image, "--type", "qcow2", "--local", derived_tag])
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


def build_customized_image(
    base_image: str,
    packages: list[str],
    tag: str,
    ssh_pub_key_path: Optional[Path] = None,
    inject_bench_config: bool = True,
) -> str:
    """Build a customized container image with packages layered on top.

    Args:
        base_image: Base container image reference (e.g. registry.redhat.io/rhel9/rhel-bootc:9.6-xxx)
        packages: List of RPM package names to install
        tag: Local tag for the built image (e.g. localhost/bootc-bench-custom-base:latest)
        ssh_pub_key_path: Path to SSH public key to inject (for base images only)
        inject_bench_config: Whether to inject SSH key and insecure registry config

    Returns the tag of the built image.
    """
    with tempfile.TemporaryDirectory(prefix="bootc-bench-custom-") as build_dir_str:
        build_dir = Path(build_dir_str)

        # Build Containerfile
        lines = [f"FROM {base_image}"]

        if packages:
            pkg_str = " ".join(packages)
            lines.append(f"RUN dnf install -y {pkg_str} && dnf clean all")

        if inject_bench_config and ssh_pub_key_path:
            shutil.copy2(ssh_pub_key_path, build_dir / "authorized_keys")
            lines.extend([
                "RUN mkdir -p /root/.ssh && chmod 700 /root/.ssh",
                "COPY authorized_keys /root/.ssh/authorized_keys",
                "RUN chmod 600 /root/.ssh/authorized_keys",
                "RUN systemctl enable sshd",
                f'RUN printf \'[[registry]]\\nlocation = "{LIBVIRT_HOST_IP}:{REGISTRY_HOST_PORT}"\\ninsecure = true\\n\' '
                f'> /etc/containers/registries.conf.d/bootc-bench-local.conf',
            ])

        containerfile = build_dir / "Containerfile"
        containerfile.write_text("\n".join(lines) + "\n")

        log.info("Building customized image: %s (packages: %s)", tag, ", ".join(packages))

        authfile = _find_authfile()
        build_cmd = NICE_PREFIX + ["sudo", "podman", "build"]
        if authfile:
            build_cmd.append(f"--authfile={authfile}")
        build_cmd.extend(["-t", tag, "-f", str(containerfile), str(build_dir)])

        subprocess.run(build_cmd, check=True)
        log.info("Built customized image: %s", tag)

    return tag


def build_customized_qcow2(
    base_image: str,
    packages: list[str],
    output_dir: Path,
    ssh_pub_key_path: Path,
    bib_image: str = BIB_IMAGE,
) -> Path:
    """Build a qcow2 from a customized (packages-layered) base image.

    Similar to build_base_qcow2 but installs packages first, then
    bakes in the SSH key and registry config.

    Returns path to the qcow2 file.
    """
    cache_key = safe_name(base_image)
    qcow2_path = output_dir / f"customized-base-{cache_key}.qcow2"
    if qcow2_path.exists():
        log.info("Customized base qcow2 already exists at %s", qcow2_path)
        return qcow2_path

    derived_tag = "localhost/bootc-bench-custom-base:latest"
    build_customized_image(
        base_image, packages, derived_tag,
        ssh_pub_key_path=ssh_pub_key_path,
        inject_bench_config=True,
    )

    # Run bootc-image-builder
    bib_output = output_dir / "bib-output-customized"
    bib_output.mkdir(parents=True, exist_ok=True)

    authfile = _find_authfile()
    log.info("Running bootc-image-builder for customized base (this may take several minutes)...")
    bib_cmd = NICE_PREFIX + [
        "sudo", "podman", "run",
        "--rm", "--privileged", "--pull=newer",
        f"--authfile={authfile}" if authfile else None,
        "-v", f"{bib_output}:/output",
        "-v", "/var/lib/containers/storage:/var/lib/containers/storage",
    ]
    bib_cmd = [x for x in bib_cmd if x is not None]
    if authfile:
        bib_cmd.extend(["-v", f"{authfile}:/run/containers/0/auth.json:ro"])
    bib_cmd.extend([bib_image, "--type", "qcow2", "--local", derived_tag])
    subprocess.run(bib_cmd, check=True)

    built_qcow2 = bib_output / "qcow2" / "disk.qcow2"
    if not built_qcow2.exists():
        raise FileNotFoundError(
            f"bootc-image-builder did not produce expected output at {built_qcow2}"
        )

    subprocess.run(["sudo", "chown", f"{os.getuid()}:{os.getgid()}",
                    str(built_qcow2)], check=True)
    shutil.copy2(str(built_qcow2), str(qcow2_path))
    log.info("Customized base qcow2 ready: %s", qcow2_path)
    return qcow2_path


def build_customized_target(
    target_image: str,
    packages: list[str],
    cache_dir: Path,
) -> str:
    """Build a customized target image (packages layered on the target).

    The target image doesn't need SSH key or bench config -- it's just
    the upgrade destination. Only packages are layered.

    Returns the local image tag.
    """
    # Create a deterministic tag from the target ref
    safe = safe_name(target_image)
    tag = f"localhost/bootc-bench-custom-target:{safe}"

    # Check if already built in podman storage
    rc = subprocess.run(
        ["sudo", "podman", "image", "exists", tag],
        capture_output=True,
    ).returncode
    if rc == 0:
        log.info("Customized target already built: %s", tag)
        return tag

    build_customized_image(
        target_image, packages, tag,
        ssh_pub_key_path=None,
        inject_bench_config=False,
    )
    return tag


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
# Single iteration – shared lifecycle
# ---------------------------------------------------------------------------

def _run_iteration_lifecycle(
    iteration_num: int,
    base_qcow2: Path,
    ssh_key_path: Path,
    vm_mgr: VMManager,
    work_dir: Path,
    vm_memory_mb: int,
    vm_vcpus: int,
    result: IterationResult,
    vm_name: str,
    log_prefix: str,
    upgrade_fn,
    completion_log_fn,
    network_profile: str = "none",
) -> IterationResult:
    """Shared VM lifecycle for a single benchmark iteration.

    Handles VM provisioning, SSH setup, pre-upgrade baseline, resource
    monitoring, reboot, post-upgrade stats, and cleanup.  The mode-specific
    upgrade work is delegated to *upgrade_fn*.

    Args:
        upgrade_fn: ``(ssh: SSH) -> float`` — perform the upgrade, populate
            *result* fields (stage_phase, bootc_switch_output, etc.) via
            closure.  Return any extra duration (beyond stage + reboot) to
            include in total_duration_sec (0 for baseline modes, transfer +
            apply for delta).  If the upgrade fails, set ``result.error`` and
            the lifecycle handles the early return.
        completion_log_fn: ``(stage_dur, reboot_dur, total_dur) -> None`` —
            emit the mode-specific "Complete" log line.
        network_profile: key into NETWORK_PROFILES; shapes the VM's
            host-side tap device for the duration of the monitored window
            (applied once the VM is up, cleared unconditionally on exit).
    """
    disk_path = work_dir / f"{vm_name}.qcow2"
    dom = None
    ssh = None
    iface_dev = None
    result.network_profile = network_profile

    try:
        # 1. Copy base qcow2
        log.info("[%s %d] Copying base qcow2...", log_prefix, iteration_num)
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
        log.info("[%s %d] Starting VM %s...", log_prefix, iteration_num,
                 vm_name)
        dom = vm_mgr.create_vm(vm_name, str(disk_path),
                               memory_mb=vm_memory_mb, vcpus=vm_vcpus)

        # 3. Get VM IP and connect via SSH
        vm_ip = vm_mgr.get_vm_ip(dom)
        ssh = SSH(vm_ip, str(ssh_key_path))
        ssh.connect()

        # 3b. Resolve the host-side tap device and apply network shaping
        # (if requested) before any upgrade traffic flows.
        iface_dev = vm_mgr.get_primary_iface_dev(dom)
        if network_profile != "none":
            if iface_dev:
                apply_network_shaping(iface_dev, network_profile)
            else:
                log.warning("[%s %d] Could not resolve tap device; "
                            "network profile '%s' not applied",
                            log_prefix, iteration_num, network_profile)

        # 4. Pre-upgrade baseline
        log.info("[%s %d] Collecting pre-upgrade baseline...", log_prefix,
                  iteration_num)
        result.pre_upgrade = {
            "bootc_status": collect_bootc_status(ssh),
            "disk_usage": collect_disk_usage(ssh),
        }

        # 5. Start resource monitor
        monitor = ResourceMonitor(vm_mgr, dom, iface_dev=iface_dev)
        monitor.start()

        # 6. Mode-specific upgrade work
        extra_duration = upgrade_fn(ssh)

        if result.error:
            monitor.stop()
            return result

        stage_duration = (result.stage_phase.duration_sec
                          if result.stage_phase else 0.0)

        # 7. Reboot phase
        log.info("[%s %d] Rebooting VM...", log_prefix, iteration_num)
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

        log.info("[%s %d] Reboot phase completed in %.1fs",
                 log_prefix, iteration_num, reboot_duration)

        # 8. Post-upgrade stats
        log.info("[%s %d] Collecting post-upgrade stats...", log_prefix,
                 iteration_num)
        result.post_upgrade = {
            "bootc_status": collect_bootc_status(ssh),
            "disk_usage": collect_disk_usage(ssh),
        }

        # Compute disk delta
        pre_used = result.pre_upgrade.get("disk_usage", {}).get(
            "used_bytes", 0)
        post_used = result.post_upgrade.get("disk_usage", {}).get(
            "used_bytes", 0)
        if pre_used and post_used:
            result.disk_delta_bytes = post_used - pre_used

        # Total duration
        result.total_duration_sec = round(
            extra_duration + stage_duration + reboot_duration, 2)

        # 9. Stop monitor and collect samples
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
        result.network_samples = [
            {
                "timestamp": s.timestamp,
                "rx_bytes": s.net_rx_bytes,
                "tx_bytes": s.net_tx_bytes,
            }
            for s in samples
        ]
        result.disk_io_samples = [
            {
                "timestamp": s.timestamp,
                "rd_bytes": s.disk_rd_bytes,
                "wr_bytes": s.disk_wr_bytes,
            }
            for s in samples
        ]

        # libvirt interface/block stats are cumulative counters since VM
        # start, so the I/O attributable to this iteration's monitored
        # window is simply the last sample minus the first.
        if len(samples) >= 2:
            first, last = samples[0], samples[-1]
            result.net_rx_bytes_delta = last.net_rx_bytes - first.net_rx_bytes
            result.net_tx_bytes_delta = last.net_tx_bytes - first.net_tx_bytes
            result.disk_rd_bytes_delta = last.disk_rd_bytes - first.disk_rd_bytes
            result.disk_wr_bytes_delta = last.disk_wr_bytes - first.disk_wr_bytes

        completion_log_fn(stage_duration, reboot_duration,
                          result.total_duration_sec)

    except Exception as e:
        result.error = str(e)
        log.error("[%s %d] Failed: %s", log_prefix, iteration_num, e,
                  exc_info=True)

    finally:
        # Cleanup
        clear_network_shaping(iface_dev)
        if ssh:
            ssh.close()
        if dom:
            vm_mgr.destroy_vm(dom)
        if disk_path.exists():
            disk_path.unlink(missing_ok=True)
            log.debug("Deleted disk %s", disk_path)

    return result


# ---------------------------------------------------------------------------
# Single iteration – baseline / gzip / zstd modes
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
    mode: str = "baseline",
    network_profile: str = "none",
) -> IterationResult:
    """Run a single benchmark iteration."""
    result = IterationResult(iteration=iteration_num, mode=mode)
    vm_name = f"bootc-bench-{iteration_num}-{uuid.uuid4().hex[:8]}"

    # Pre-populate layer info (collected once per target)
    if target_layer_info:
        result.layer_details = target_layer_info.get("layers", [])
        result.layers_pulled = target_layer_info.get("layer_count")
        result.download_size_bytes = target_layer_info.get(
            "total_compressed_size_bytes")

    def upgrade_fn(ssh: SSH) -> float:
        # Copy registry credentials (needed for remote pull)
        copy_registry_auth(ssh)

        # Stage phase: run bootc switch
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
            return 0.0

        log.info("[Iter %d] Stage phase completed in %.1fs",
                 iteration_num, stage_duration)
        return 0.0  # no extra duration beyond stage + reboot

    def completion_log_fn(stage_dur, reboot_dur, total_dur):
        log.info(
            "[Iter %d] Complete — stage=%.1fs reboot=%.1fs total=%.1fs",
            iteration_num, stage_dur, reboot_dur, total_dur,
        )

    return _run_iteration_lifecycle(
        iteration_num=iteration_num,
        base_qcow2=base_qcow2,
        ssh_key_path=ssh_key_path,
        vm_mgr=vm_mgr,
        work_dir=work_dir,
        vm_memory_mb=vm_memory_mb,
        vm_vcpus=vm_vcpus,
        result=result,
        vm_name=vm_name,
        log_prefix="Iter",
        upgrade_fn=upgrade_fn,
        completion_log_fn=completion_log_fn,
        network_profile=network_profile,
    )


# ---------------------------------------------------------------------------
# Delta mode: preparation and iteration
# ---------------------------------------------------------------------------

def export_oci_archive(image_ref: str, output_path: Path,
                       authfile: Optional[str] = None,
                       from_local_storage: bool = False) -> float:
    """Export a container image to an OCI archive via skopeo. Returns duration.

    Args:
        image_ref: Image reference (registry ref or local image tag).
        output_path: Destination OCI archive path.
        authfile: Registry auth file (only used for registry pulls).
        from_local_storage: If True, pull from local podman storage via
            ``containers-storage:`` instead of ``docker://``.  Requires
            sudo because the derived image lives in the root podman store.
    """
    if output_path.exists():
        log.info("OCI archive already exists: %s", output_path)
        return 0.0
    log.info("Exporting %s to %s ...", image_ref, output_path)
    if from_local_storage:
        # Export from local (root) podman storage
        cmd = NICE_PREFIX + ["sudo", "skopeo", "copy",
               "--remove-signatures",
               f"containers-storage:{image_ref}",
               f"oci-archive:{output_path}"]
    else:
        cmd = NICE_PREFIX + ["skopeo", "copy",
               "--override-arch", "amd64",
               "--remove-signatures",
               f"docker://{image_ref}",
               f"oci-archive:{output_path}"]
        if authfile:
            cmd.extend(["--authfile", authfile])
    start = time.time()
    subprocess.run(cmd, check=True)
    # Fix ownership if exported via sudo
    if from_local_storage:
        subprocess.run(
            ["sudo", "chown", f"{os.getuid()}:{os.getgid()}", str(output_path)],
            check=False,
        )
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


def format_bytes(b: int | float) -> str:
    """Human-readable byte size."""
    b = float(b)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(b) < 1024:
            return f"{b:.1f} {unit}"
        b /= 1024
    return f"{b:.1f} PB"


def safe_name(ref: str) -> str:
    """Sanitize an image reference for use in filenames."""
    return ref.split("/")[-1].replace(":", "-")


def short_ref(ref: str) -> str:
    """Shorten an image reference for log messages."""
    return ref.split("/")[-1] if "/" in ref else ref


def _create_blob_supplement(
    delta_file: Path,
    target_archive: Path,
    supplement_path: Path,
) -> list[str]:
    """Find blobs that oci-delta skips and extract them from the target archive.

    oci-delta skips layers that are byte-identical between old and new images.
    The apply step then produces an incomplete archive.  This function
    identifies the missing blobs by comparing the delta's layer list against
    the target manifest and extracts them into a small supplement tar.

    Returns a list of missing blob digests (empty if none are skipped).
    """
    import tarfile as _tarfile

    # 1. Read target manifest to get all required blob digests
    with _tarfile.open(str(target_archive)) as tgt:
        idx = json.load(tgt.extractfile("index.json"))
        manifest_path = idx["manifests"][0]["digest"].replace(
            "sha256:", "blobs/sha256/"
        )
        manifest = json.load(tgt.extractfile(manifest_path))
        required = {}  # digest -> blob path
        required[manifest["config"]["digest"]] = manifest["config"]["digest"].replace(
            "sha256:", "blobs/sha256/"
        )
        for layer in manifest["layers"]:
            required[layer["digest"]] = layer["digest"].replace(
                "sha256:", "blobs/sha256/"
            )

    # 2. Read delta manifest to find which target digests it covers.
    #    Each delta layer has an annotation with the target layer digest
    #    it will reconstruct, or it's a copy of the original blob.
    with _tarfile.open(str(delta_file)) as dtf:
        didx = json.load(dtf.extractfile("index.json"))
        dmanifest_path = didx["manifests"][0]["digest"].replace(
            "sha256:", "blobs/sha256/"
        )
        dmanifest = json.load(dtf.extractfile(dmanifest_path))

        # Collect digests the delta covers:
        # - tar-diff layers: the annotation 'io.github.containers.delta.to'
        #   gives the target compressed digest
        # - original-copy layers: the blob digest IS the target digest
        # - config/manifest entries: marked with delta.content annotations
        covered = set()
        delta_blob_names = set(dtf.getnames())

        for layer in dmanifest["layers"]:
            ann = layer.get("annotations", {})
            content_type = ann.get("io.github.containers.delta.content", "")

            if content_type == "image-config":
                # The delta stores the target config directly
                covered.add(manifest["config"]["digest"])
            elif content_type == "image-manifest":
                # Not a blob we need in the output
                pass
            elif content_type == "image-layer":
                # tar-diff: the 'to' annotation has the OLD compressed digest,
                # but the reconstructed layer gets a NEW digest after
                # decompression + recompression.  The delta apply matches by
                # diff_id (uncompressed), so it covers the layer regardless
                # of the final compressed digest.
                # Mark as covered by position.
                pass
            else:
                # Original blob copy — its digest is the target digest
                blob_path = layer["digest"].replace("sha256:", "blobs/sha256/")
                if blob_path in delta_blob_names:
                    covered.add(layer["digest"])

    # 3. Since tar-diffs are matched by position/diff_id, we can't easily
    #    map them to target digests here.  Instead, do a simpler check:
    #    count how many layers the delta processes (tar-diffs + copies)
    #    vs how many the target needs.  Any shortfall = skipped layers.
    #
    #    More reliable: just count non-meta layers in the delta manifest.
    delta_layer_count = sum(
        1 for l in dmanifest["layers"]
        if l.get("annotations", {}).get(
            "io.github.containers.delta.content", ""
        ) == "image-layer"
    )
    # Add original-copy layers (no delta.content annotation, and blob exists in delta)
    delta_copy_count = sum(
        1 for l in dmanifest["layers"]
        if not l.get("annotations", {}).get("io.github.containers.delta.content")
        and l["digest"].replace("sha256:", "blobs/sha256/") in delta_blob_names
    )
    total_delta_layers = delta_layer_count + delta_copy_count
    target_layer_count = len(manifest["layers"])

    if total_delta_layers >= target_layer_count:
        # No skipped layers
        if supplement_path.exists():
            supplement_path.unlink()
        return []

    # 4. We know there are skipped layers.  To find exactly which ones,
    #    do a test: run oci-delta apply to a temp location and check
    #    which blobs are missing.  This is the most reliable method.
    #    But it requires the source image too.
    #
    #    Simpler heuristic: layers in the target that also exist in the
    #    OLD archive with the same digest are the ones oci-delta skips.
    with _tarfile.open(str(target_archive)) as tgt:
        # Get path to old archive — it's the source used to create the delta.
        # We figure out which archive by looking at what prepare_delta_artifacts
        # computed.  For now, find it from the delta filename.
        old_archive_path = delta_file.parent / (
            delta_file.name.split("-to-")[0] + ".oci-archive"
        )
        if not old_archive_path.exists():
            log.warning("Cannot find old archive to identify skipped blobs: %s",
                        old_archive_path)
            return []

        with _tarfile.open(str(old_archive_path)) as old:
            old_blob_names = set(old.getnames())

            # Blobs that exist in both old and target archives (by path = same digest)
            target_blob_paths = set()
            for layer in manifest["layers"]:
                target_blob_paths.add(
                    layer["digest"].replace("sha256:", "blobs/sha256/")
                )

            shared_blobs = target_blob_paths & old_blob_names
            # The skipped count should match: target_layers - delta_layers
            expected_skipped = target_layer_count - total_delta_layers
            if len(shared_blobs) != expected_skipped:
                log.warning(
                    "Expected %d skipped blobs but found %d shared; "
                    "extracting all shared blobs to be safe",
                    expected_skipped, len(shared_blobs),
                )

            if not shared_blobs:
                return []

            # 5. Extract shared (skipped) blobs from target archive into supplement
            log.info("Extracting %d skipped blobs into supplement...",
                     len(shared_blobs))
            with _tarfile.open(str(supplement_path), "w") as sup:
                for blob_path in sorted(shared_blobs):
                    member = tgt.getmember(blob_path)
                    sup.addfile(member, tgt.extractfile(blob_path))

    return sorted(shared_blobs)


DERIVED_IMAGE_TAG = "localhost/bootc-bench-base:latest"


def prepare_delta_artifacts(
    base_image: str,
    target_image: str,
    oci_delta_bin: Path,
    output_dir: Path,
    derived_image: Optional[str] = None,
) -> dict:
    """Prepare OCI archives and delta file for a given base→target pair.

    Done once per target, outside the iteration loop.

    When a derived image tag is provided (e.g.
    ``localhost/bootc-bench-base:latest``), the "old" OCI archive is
    exported from local podman storage instead of the registry.  This
    ensures the delta's source config-digest matches the ostree ref
    deployed inside the VM so ``oci-delta apply --ostree-repo`` works.

    Returns a dict with paths and timing info.
    """
    archives_dir = output_dir / "archives"
    archives_dir.mkdir(parents=True, exist_ok=True)

    authfile = _find_authfile()

    # Determine what to use as the "old" (source) image for the delta.
    # If a derived image exists in local storage, use it — its config
    # digest will match the VM's ostree repo.
    old_image_ref = derived_image or base_image
    old_from_local = derived_image is not None

    # Use distinct labels for vanilla vs customized to avoid archive collisions
    if old_from_local and derived_image and "custom" in derived_image:
        old_label = "customized-derived"
    elif old_from_local:
        old_label = "derived"
    else:
        old_label = safe_name(base_image)
    old_archive = archives_dir / f"{old_label}.oci-archive"
    new_archive = archives_dir / f"{safe_name(target_image)}.oci-archive"
    delta_file = archives_dir / f"{old_label}-to-{safe_name(target_image)}.delta"

    # Detect whether the target image is in local podman storage
    # (customized targets start with "localhost/")
    new_from_local = target_image.startswith("localhost/")

    # Export both images
    old_export_time = export_oci_archive(
        old_image_ref, old_archive, authfile,
        from_local_storage=old_from_local,
    )
    new_export_time = export_oci_archive(
        target_image, new_archive, authfile,
        from_local_storage=new_from_local,
    )

    # Create delta
    delta_time, delta_size = create_delta(
        oci_delta_bin, old_archive, new_archive, delta_file
    )

    # Workaround for oci-delta bug: layers identical between old/new
    # images are skipped during create and missing from apply output.
    # Extract them from the target archive into a small supplement tar
    # so we can inject them after apply.
    supplement_file = archives_dir / f"{old_label}-to-{safe_name(target_image)}.supplement.tar"
    skipped = _create_blob_supplement(delta_file, new_archive, supplement_file)
    if skipped:
        log.info("Created blob supplement with %d skipped blobs (%s)",
                 len(skipped), format_bytes(supplement_file.stat().st_size))

    return {
        "old_image": old_image_ref,
        "old_from_local_storage": old_from_local,
        "old_archive": str(old_archive),
        "new_archive": str(new_archive),
        "delta_file": str(delta_file),
        "supplement_file": str(supplement_file) if skipped else None,
        "supplement_blob_count": len(skipped),
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
    network_profile: str = "none",
) -> IterationResult:
    """Run a single benchmark iteration using oci-delta."""
    result = IterationResult(iteration=iteration_num, mode="delta")
    vm_name = f"bootc-bench-delta-{iteration_num}-{uuid.uuid4().hex[:8]}"

    delta_file = Path(delta_artifacts["delta_file"])
    result.delta_file_size_bytes = delta_artifacts["delta_size_bytes"]

    # Pre-populate layer info from pre-collected data
    if target_layer_info:
        result.layer_details = target_layer_info.get("layers", [])
        result.layers_pulled = target_layer_info.get("layer_count")
        result.download_size_bytes = delta_artifacts["delta_size_bytes"]

    # Track durations across sub-phases for the completion log
    extra_durations = {}

    def upgrade_fn(ssh: SSH) -> float:
        # 1. Transfer delta file + oci-delta binary + supplement into VM
        supplement_file = delta_artifacts.get("supplement_file")
        log.info("[Delta Iter %d] Transferring delta (%.1f MB) and oci-delta binary...",
                 iteration_num, delta_file.stat().st_size / 1024**2)
        transfer_start = time.time()
        transfer_start_ts = datetime.now().isoformat()

        ssh.upload_file(str(delta_file), "/tmp/update.delta")
        ssh.upload_file(str(oci_delta_bin), "/tmp/oci-delta")
        ssh.run("chmod +x /tmp/oci-delta")
        if supplement_file:
            ssh.upload_file(supplement_file, "/tmp/supplement.tar")

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

        # 2. Apply delta → reconstruct OCI archive
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
            return 0.0

        log.info("[Delta Iter %d] Delta apply completed in %.1fs",
                 iteration_num, apply_duration)

        # 2b. Inject any skipped blobs (oci-delta bug workaround)
        if supplement_file:
            log.info("[Delta Iter %d] Injecting supplement blobs...", iteration_num)
            # The supplement tar contains blobs at their OCI paths
            # (e.g. blobs/sha256/<digest>).  Append them to the
            # oci-archive tar so bootc can find them.
            rc, out, err = ssh.run(
                "bash -c '"
                "WORK=$(mktemp -d) && "
                "tar xf /tmp/supplement.tar -C $WORK && "
                "tar rf /tmp/new.oci-archive -C $WORK blobs && "
                "rm -rf $WORK /tmp/supplement.tar"
                "'",
                timeout=60,
            )
            if rc != 0:
                log.warning("[Delta Iter %d] Supplement injection returned rc=%d: %s",
                            iteration_num, rc, out)

        # 3. Stage phase: bootc switch to local OCI archive
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
            return 0.0

        log.info("[Delta Iter %d] Stage phase completed in %.1fs",
                 iteration_num, stage_duration)

        # Stash sub-phase durations for the completion log
        extra_durations["transfer"] = transfer_duration
        extra_durations["apply"] = apply_duration

        return transfer_duration + apply_duration

    def completion_log_fn(stage_dur, reboot_dur, total_dur):
        log.info(
            "[Delta Iter %d] Complete — transfer=%.1fs apply=%.1fs "
            "stage=%.1fs reboot=%.1fs total=%.1fs",
            iteration_num,
            extra_durations.get("transfer", 0.0),
            extra_durations.get("apply", 0.0),
            stage_dur, reboot_dur, total_dur,
        )

    return _run_iteration_lifecycle(
        iteration_num=iteration_num,
        base_qcow2=base_qcow2,
        ssh_key_path=ssh_key_path,
        vm_mgr=vm_mgr,
        work_dir=work_dir,
        vm_memory_mb=vm_memory_mb,
        vm_vcpus=vm_vcpus,
        result=result,
        vm_name=vm_name,
        log_prefix="Delta Iter",
        upgrade_fn=upgrade_fn,
        completion_log_fn=completion_log_fn,
        network_profile=network_profile,
    )


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

    # Network/disk I/O observed via libvirt during the monitored window
    # (see MonitorSample / ResourceMonitor). Distinct from disk_delta_bytes
    # above, which is a before/after `df` snapshot on the guest filesystem.
    net_rx = [r.net_rx_bytes_delta for r in successful
              if r.net_rx_bytes_delta is not None]
    if net_rx:
        summary["net_rx_bytes"] = stat(net_rx)

    net_tx = [r.net_tx_bytes_delta for r in successful
              if r.net_tx_bytes_delta is not None]
    if net_tx:
        summary["net_tx_bytes"] = stat(net_tx)

    disk_rd = [r.disk_rd_bytes_delta for r in successful
               if r.disk_rd_bytes_delta is not None]
    if disk_rd:
        summary["disk_rd_bytes"] = stat(disk_rd)

    disk_wr = [r.disk_wr_bytes_delta for r in successful
               if r.disk_wr_bytes_delta is not None]
    if disk_wr:
        summary["disk_wr_bytes"] = stat(disk_wr)

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

    # Shared cache for expensive reusable artifacts (qcow2, OCI archives,
    # delta files, SSH keys).  Persists across timestamped runs so only
    # the first run pays the build cost.
    if args.cache_dir:
        cache_dir = Path(args.cache_dir).resolve()
    else:
        cache_dir = Path.home() / ".cache" / "bootc-bench"

    if args.fresh and cache_dir.exists():
        log.info("--fresh: wiping cache at %s", cache_dir)
        shutil.rmtree(cache_dir)

    cache_dir.mkdir(parents=True, exist_ok=True)
    log.info("Cache directory: %s", cache_dir)

    # Work dir for temporary VM disks.  Defaults to /var/tmp/bootc-bench
    # which is world-accessible so the qemu process (uid 107) can read
    # disk images without sudo or ACLs.
    if args.work_dir:
        work_dir = Path(args.work_dir).resolve()
    else:
        work_dir = Path("/var/tmp/bootc-bench")
    work_dir.mkdir(parents=True, exist_ok=True)

    base_image = args.base_image
    packages = args.packages if args.packages is not None else DEFAULT_PACKAGES
    customize = not args.no_customize
    network_profile = getattr(args, "network_profile", "none")

    # Pick a working bootc-image-builder image. registry.redhat.io's BIB
    # requires RHEL entitlements; auto-switch to the upstream image for
    # non-RHEL (e.g. Fedora) base images unless explicitly overridden.
    bib_image = args.bib_image
    if not bib_image:
        bib_image = (FEDORA_BIB_IMAGE if "fedora" in base_image.lower()
                     else BIB_IMAGE)
    log.info("Using bootc-image-builder image: %s", bib_image)

    # Z-stream discovery
    zstream_target = None
    if args.base_stream:
        log.info("Discovering z-stream range for %s...", args.base_stream)
        oldest_ref, newest_ref = discover_zstream_tags(args.image_repo, args.base_stream)
        base_image = oldest_ref
        zstream_target = newest_ref
        log.info("Base (oldest z-stream): %s", base_image)
        log.info("Z-stream target (newest): %s", zstream_target)

    # Y-stream targets from CLI (explicit --targets)
    ystream_targets = args.targets or []
    # If no explicit targets AND no base-stream, use defaults
    if not ystream_targets and not args.base_stream:
        ystream_targets = DEFAULT_TARGETS

    # Generate SSH keys (cached)
    ssh_priv, ssh_pub = generate_ssh_keypair(cache_dir / "ssh")

    # Build vanilla base qcow2
    if args.qcow2:
        vanilla_qcow2 = Path(args.qcow2).resolve()
        if not vanilla_qcow2.exists():
            raise FileNotFoundError(f"Provided qcow2 not found: {vanilla_qcow2}")
        log.info("Using provided qcow2: %s", vanilla_qcow2)
    else:
        vanilla_qcow2 = build_base_qcow2(base_image, cache_dir, ssh_pub,
                                          bib_image=bib_image)

    # Build customized base qcow2
    custom_qcow2 = None
    if customize:
        log.info("Building customized base with packages: %s", ", ".join(packages))
        custom_qcow2 = build_customized_qcow2(base_image, packages, cache_dir, ssh_pub,
                                               bib_image=bib_image)

    # Construct scenario list
    # Each scenario = dict with upgrade_type, variant, base_image, target_image,
    # qcow2 to use, and optionally custom_target_image and packages.
    scenarios = []

    # Z-stream scenarios
    if zstream_target:
        scenarios.append({
            "upgrade_type": "z-stream",
            "variant": "vanilla",
            "base_image": base_image,
            "target_image": zstream_target,
            "qcow2": vanilla_qcow2,
        })
        if customize:
            # Build customized target for z-stream
            custom_zstream_target = build_customized_target(
                zstream_target, packages, cache_dir
            )
            scenarios.append({
                "upgrade_type": "z-stream",
                "variant": "customized",
                "base_image": base_image,
                "target_image": zstream_target,
                "custom_target_image": custom_zstream_target,
                "qcow2": custom_qcow2,
                "packages": packages,
            })

    # Y-stream scenarios
    for target in ystream_targets:
        scenarios.append({
            "upgrade_type": "y-stream",
            "variant": "vanilla",
            "base_image": base_image,
            "target_image": target,
            "qcow2": vanilla_qcow2,
        })
        if customize:
            custom_ystream_target = build_customized_target(
                target, packages, cache_dir
            )
            scenarios.append({
                "upgrade_type": "y-stream",
                "variant": "customized",
                "base_image": base_image,
                "target_image": target,
                "custom_target_image": custom_ystream_target,
                "qcow2": custom_qcow2,
                "packages": packages,
            })

    if not scenarios:
        raise ValueError("No upgrade scenarios to benchmark. Provide --base-stream and/or --targets.")

    log.info("Benchmark scenarios (%d total):", len(scenarios))
    for i, s in enumerate(scenarios, 1):
        log.info("  %d. [%s] [%s] %s → %s", i, s["upgrade_type"], s["variant"],
                 short_ref(s["base_image"]), short_ref(s.get("custom_target_image", s["target_image"])))

    # Connect to libvirt
    vm_mgr = VMManager(uri=args.libvirt_uri)
    vm_mgr.connect()

    mode = getattr(args, "mode", "baseline")
    uses_local_registry = mode in ("baseline-gzip", "baseline-zstd")
    oci_delta_bin = None
    if mode == "delta":
        oci_delta_bin = Path(args.oci_delta_bin).resolve()
        if not oci_delta_bin.exists():
            raise FileNotFoundError(f"oci-delta binary not found: {oci_delta_bin}")

    registry_addr = None
    if uses_local_registry:
        registry_addr = start_local_registry()

    all_results = {
        "config": {
            "base_image": base_image,
            "base_stream": getattr(args, 'base_stream', None),
            "image_repo": getattr(args, 'image_repo', DEFAULT_IMAGE_REPO),
            "zstream_target": zstream_target,
            "ystream_targets": ystream_targets,
            "packages": packages if customize else None,
            "customize": customize,
            "iterations": args.iterations,
            "vm_memory_mb": args.vm_memory,
            "vm_vcpus": args.vm_vcpus,
            "mode": mode,
            "network_profile": network_profile,
            "timestamp": datetime.now().isoformat(),
        },
        "benchmarks": [],
    }

    try:
        for scenario in scenarios:
            upgrade_type = scenario["upgrade_type"]
            variant = scenario["variant"]
            target = scenario["target_image"]
            base_qcow2 = scenario["qcow2"]

            # For customized variants, the effective target is the custom-built local image
            # For customized + local registry modes, we need to push the custom target
            if variant == "customized":
                effective_target = scenario["custom_target_image"]
            else:
                effective_target = target

            log.info("=" * 60)
            log.info("Scenario: [%s] [%s] [%s]", upgrade_type, variant, mode)
            log.info("  Base: %s", scenario["base_image"])
            log.info("  Target: %s (effective: %s)", target, effective_target)
            log.info("=" * 60)

            # For local registry modes, push the target to local registry
            if uses_local_registry:
                base_name = effective_target.split("/")[-1].replace(":", "-")
                suffix = "gzip" if mode == "baseline-gzip" else "zstd-chunked"
                compress_fmt = None if mode == "baseline-gzip" else "zstd:chunked"
                local_tag = f"{base_name}-{suffix}"

                if variant == "customized":
                    # Push from local podman storage (the custom target is local)
                    # Need to use containers-storage: transport instead of docker://
                    _push_local_image_to_registry(effective_target, local_tag, compress_fmt)
                else:
                    push_to_local_registry(effective_target, local_tag, compress_fmt)

                effective_target = f"{registry_addr}/{local_tag}"
                log.info("Using local registry target: %s", effective_target)

            # Collect layer info
            log.info("Collecting target image layer info...")
            if variant == "customized":
                # For custom images in local storage, inspect differently
                target_layer_info = _get_local_image_layer_info(scenario["custom_target_image"])
            else:
                target_layer_info = get_image_layer_info_host(target)
            if "error" in target_layer_info:
                log.warning("Could not collect layer info: %s", target_layer_info["error"])

            # Prepare delta artifacts if in delta mode
            delta_artifacts = None
            if mode == "delta":
                log.info("Preparing delta artifacts...")
                if variant == "customized":
                    derived_image = "localhost/bootc-bench-custom-base:latest"
                else:
                    derived_image = DERIVED_IMAGE_TAG

                # Check if the derived image exists in podman storage
                rc = subprocess.run(
                    ["sudo", "podman", "image", "exists", derived_image],
                    capture_output=True,
                ).returncode
                if rc != 0:
                    log.warning(
                        "Derived image %s not found; falling back to base image",
                        derived_image,
                    )
                    derived_image = None

                # Determine the target ref for delta creation
                delta_target = scenario.get("custom_target_image", target) if variant == "customized" else target

                delta_artifacts = prepare_delta_artifacts(
                    base_image,
                    delta_target,
                    oci_delta_bin, cache_dir,
                    derived_image=derived_image,
                )

            benchmark = {
                "base_image": scenario["base_image"],
                "target_image": target,
                "effective_target": effective_target,
                "target_layer_info": target_layer_info,
                "mode": mode,
                "network_profile": network_profile,
                "upgrade_type": upgrade_type,
                "variant": variant,
                "packages": scenario.get("packages"),
                "delta_artifacts": delta_artifacts,
                "iterations": [],
                "summary": {},
            }

            iteration_results = []
            for i in range(1, args.iterations + 1):
                log.info("-" * 40)
                log.info("Iteration %d/%d [%s] [%s] [%s]",
                         i, args.iterations, upgrade_type, variant, mode)
                log.info("-" * 40)

                if mode == "delta":
                    result = run_iteration_delta(
                        iteration_num=i,
                        base_qcow2=base_qcow2,
                        target_image=effective_target,
                        ssh_key_path=ssh_priv,
                        vm_mgr=vm_mgr,
                        work_dir=work_dir,
                        oci_delta_bin=oci_delta_bin,
                        delta_artifacts=delta_artifacts,
                        target_layer_info=target_layer_info,
                        vm_memory_mb=args.vm_memory,
                        vm_vcpus=args.vm_vcpus,
                        network_profile=network_profile,
                    )
                else:
                    result = run_iteration(
                        iteration_num=i,
                        base_qcow2=base_qcow2,
                        target_image=effective_target,
                        ssh_key_path=ssh_priv,
                        vm_mgr=vm_mgr,
                        work_dir=work_dir,
                        target_layer_info=target_layer_info,
                        vm_memory_mb=args.vm_memory,
                        vm_vcpus=args.vm_vcpus,
                        mode=mode,
                        network_profile=network_profile,
                    )

                # Tag the result with scenario metadata
                result.variant = variant
                result.upgrade_type = upgrade_type

                iteration_results.append(result)
                benchmark["iterations"].append(asdict(result))

            benchmark["summary"] = compute_summary(iteration_results)
            all_results["benchmarks"].append(benchmark)

    finally:
        vm_mgr.close()
        if uses_local_registry:
            stop_local_registry()

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

              # Realistic z-stream + y-stream benchmark
              %(prog)s --base-stream 9.6 -t registry.redhat.io/rhel9/rhel-bootc:9.8

              # Use a pre-built qcow2
              %(prog)s --qcow2 /path/to/base.qcow2

              # Custom packages and output directory
              %(prog)s --base-stream 9.6 --packages httpd nginx -o /tmp/bench-results
        """),
    )
    parser.add_argument(
        "-b", "--base-image", default=DEFAULT_BASE_IMAGE,
        help=f"Base bootc image (default: {DEFAULT_BASE_IMAGE})",
    )
    parser.add_argument(
        "--base-stream", default=None,
        help="Y-stream version to auto-discover z-stream range (e.g. '9.6'). "
             "When set, overrides --base-image with the oldest build tag for "
             "this stream and adds the newest as a z-stream upgrade target.",
    )
    parser.add_argument(
        "--image-repo", default=DEFAULT_IMAGE_REPO,
        help=f"Image repository for tag discovery (default: {DEFAULT_IMAGE_REPO})",
    )
    parser.add_argument(
        "--packages", nargs="+", default=None,
        help="RPM packages to install for customized variants "
             f"(default: {' '.join(DEFAULT_PACKAGES)}). "
             "Pass --no-customize to skip customized variants entirely.",
    )
    parser.add_argument(
        "--no-customize", action="store_true",
        help="Skip customized (package-layered) upgrade variants",
    )
    parser.add_argument(
        "-t", "--targets", nargs="+", default=None,
        help="Upgrade target image(s) (default: 9.8, 10.0, 10.2)",
    )
    parser.add_argument(
        "-n", "--iterations", type=int, default=DEFAULT_ITERATIONS,
        help=f"Number of iterations per target (default: {DEFAULT_ITERATIONS})",
    )
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    parser.add_argument(
        "-o", "--output-dir", default=f"./bootc-bench-results-{timestamp}",
        help="Output directory for results and artifacts "
             "(default: ./bootc-bench-results-YYYYMMDD-HHMMSS)",
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
        "-m", "--mode",
        choices=["baseline", "baseline-gzip", "baseline-zstd", "delta"],
        default="baseline",
        help="Benchmark mode: 'baseline' (pull from source registry), "
             "'baseline-gzip' (pull gzip from local registry), "
             "'baseline-zstd' (pull zstd:chunked from local registry), "
             "or 'delta' (oci-delta)",
    )
    parser.add_argument(
        "--oci-delta-bin", default=OCI_DELTA_BIN_DEFAULT,
        help=f"Path to oci-delta binary (default: {OCI_DELTA_BIN_DEFAULT})",
    )
    parser.add_argument(
        "--network-profile",
        choices=sorted(NETWORK_PROFILES.keys()),
        default="none",
        help="Simulated network condition applied via tc netem on the VM's "
             "host-side tap device for the duration of the upgrade phase: "
             "'none' (host network, unshaped), 'broadband' (~100mbit/10ms), "
             "'constrained' (~5mbit/150ms/1%% loss). Applies to any mode. "
             "(default: none)",
    )
    parser.add_argument(
        "--bib-image", default=None,
        help="bootc-image-builder container image to use. Defaults to "
             f"{FEDORA_BIB_IMAGE!r} when --base-image contains 'fedora', "
             f"otherwise {BIB_IMAGE!r}.",
    )
    parser.add_argument(
        "--cache-dir", default=None,
        help="Shared cache directory for reusable artifacts "
             "(default: ~/.cache/bootc-bench)",
    )
    parser.add_argument(
        "--fresh", action="store_true",
        help="Wipe the cache and rebuild all artifacts from scratch",
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
            upgrade_type = bench.get("upgrade_type", "y-stream")
            variant = bench.get("variant", "vanilla")
            summary = bench.get("summary", {})
            total = summary.get("total_duration_sec", {})
            print(f"\n{'=' * 50}")
            print(f"Target: {target} [{upgrade_type}] [{variant}]")
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

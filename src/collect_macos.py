# SPDX-License-Identifier: GPL-3.0-or-later
from __future__ import annotations

import platform
import re
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .model import Snapshot
from .runner import Runner
from .util import normalize_macos_device, read_json


def _system_profiler_nvme(runner: Runner, notes: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    notes = notes if notes is not None else []
    res = runner.run(["system_profiler", "SPNVMeDataType", "-json"], timeout=25.0)
    if not res.available:
        notes.append("built-in tool missing: system_profiler")
        return []
    if res.returncode != 0:
        detail = (res.stderr or res.stdout).strip().splitlines()
        notes.append(f"system_profiler SPNVMeDataType failed: {(detail[-1] if detail else 'unknown error')[:240]}")
        return []
    payload = read_json(res.stdout)
    if not isinstance(payload, dict):
        notes.append("could not parse JSON from system_profiler SPNVMeDataType")
        return []

    items: List[Dict[str, Any]] = []

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            # Physical NVMe device records expose bsd_name + device_model.
            # Avoid treating nested volumes as controllers by requiring model.
            if value.get("bsd_name") and (value.get("device_model") or value.get("smart_status") or value.get("device_revision")):
                items.append(dict(value))
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(payload.get("SPNVMeDataType", payload))
    dedup: Dict[str, Dict[str, Any]] = {}
    for item in items:
        bsd = str(item.get("bsd_name", "")).strip()
        if bsd:
            dedup[bsd] = item
    return list(dedup.values())



def _system_profiler_link_text(runner: Runner, bsd_name: str) -> Dict[str, str]:
    """Best-effort link parsing from the human-readable profiler output.

    Some macOS releases expose Link Width/Link Speed in the text view even
    when the corresponding JSON keys are absent or renamed.
    """
    res = runner.run(["system_profiler", "SPNVMeDataType"], timeout=25.0)
    if not res.available or res.returncode != 0:
        return {}
    last_width = None
    last_speed = None
    for raw in res.stdout.splitlines():
        line = raw.strip()
        if line.startswith("Link Width:"):
            last_width = line.split(":", 1)[1].strip()
        elif line.startswith("Link Speed:"):
            last_speed = line.split(":", 1)[1].strip()
        elif line.startswith("BSD Name:"):
            current = line.split(":", 1)[1].strip()
            if current == bsd_name:
                out: Dict[str, str] = {}
                if last_width:
                    out["current_link_width"] = last_width
                if last_speed:
                    out["current_link_speed"] = last_speed
                return out
            last_width = None
            last_speed = None
    return {}

def _parse_smartctl_scan(text: str) -> List[Dict[str, str]]:
    """Parse smartctl --scan-open output, retaining NVMe bridge type hints."""
    out: List[Dict[str, str]] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or "nvme" not in line.lower():
            continue
        # Typical lines:
        # /dev/disk0 -d nvme # /dev/disk0, NVMe device
        # /dev/disk4 -d sntrealtek # ... [USB NVMe Realtek], NVMe device
        m = re.match(r"^(?P<device>/\S+)(?:\s+-d\s+(?P<type>\S+))?", line)
        if not m:
            continue
        item = {"device": m.group("device")}
        if m.group("type"):
            item["smartctl_type"] = m.group("type")
        item["scan_line"] = line
        out.append(item)
    return out


def _smartctl_scan(runner: Runner) -> Tuple[List[Dict[str, str]], Optional[str]]:
    res = runner.run(["smartctl", "--scan-open"], timeout=15.0)
    if not res.available:
        return [], "smartctl is not installed; full NVMe health data on macOS requires smartmontools (for Homebrew: `brew install smartmontools`)"
    items = _parse_smartctl_scan(res.stdout)
    if items:
        return items, None

    # Non-root list operations may be unable to open disks. Plain --scan is
    # still useful for discovery and keeps bridge type hints when available.
    fallback = runner.run(["smartctl", "--scan"], timeout=10.0)
    if fallback.available:
        items = _parse_smartctl_scan(fallback.stdout)
        if items:
            return items, None

    detail = (res.stderr or res.stdout).strip().splitlines()
    if res.returncode != 0:
        return [], f"smartctl --scan-open failed: {(detail[-1] if detail else f'exit status {res.returncode}')[:240]}"
    return [], None


def _smartctl_json(runner: Runner, device: str, device_type: Optional[str], notes: List[str]) -> Optional[Dict[str, Any]]:
    argv = ["smartctl", "-a", "-j"]
    if device_type:
        argv += ["-d", device_type]
    argv.append(device)
    res = runner.run(argv, timeout=20.0)
    if not res.available:
        notes.append("smartctl is not installed; full NVMe health data on macOS requires smartmontools (for Homebrew: `brew install smartmontools`)")
        return None
    payload = read_json(res.stdout) if res.stdout.strip() else None
    # smartctl exit status is a bitmask. Bits 0..2 indicate actual collection
    # trouble; higher bits are health findings, and JSON remains useful.
    if isinstance(payload, dict):
        if res.returncode & 0x07:
            notes.append(f"smartctl returned collection-status bits 0x{res.returncode & 0x07:02x}; valid JSON was retained")
        return payload
    detail = (res.stderr or res.stdout).strip().splitlines()
    notes.append(f"smartctl -a failed: {(detail[-1] if detail else f'exit status {res.returncode}')[:240]}")
    return None


def _profiler_to_controller(item: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {
        "state": "present",
        "transport": "NVMe",
        "backend": "system_profiler",
    }
    mapping = {
        "device_model": "model",
        "device_serial": "serial",
        "device_revision": "firmware_rev",
        "smart_status": "smart_status",
        "bsd_name": "bsd_name",
        "size": "size",
        "size_in_bytes": "size_in_bytes",
        "spnvme_trim_support": "trim_support",
        "spnvme_vendor-id": "vendor_id",
        "spnvme_device-id": "device_id",
        "spnvme_controller-id": "controller_id",
        "spnvme_nqn": "nqn",
    }
    for src, dst in mapping.items():
        value = item.get(src)
        if value not in (None, ""):
            out[dst] = value
    return out


def _merge_smartctl_identity(snapshot: Snapshot, payload: Dict[str, Any]) -> None:
    ci = snapshot.controller_info
    mapping = {
        "model_name": "model",
        "serial_number": "serial",
        "firmware_version": "firmware_rev",
        "nvme_version": "nvme_version",
    }
    for src, dst in mapping.items():
        value = payload.get(src)
        if value not in (None, "") and not ci.get(dst):
            ci[dst] = value
    device = payload.get("device")
    if isinstance(device, dict):
        if device.get("protocol"):
            ci["protocol"] = device["protocol"]
        if device.get("type"):
            ci["smartctl_device_type"] = device["type"]
    ci["state"] = "live"


def _macos_log(runner: Runner, tokens: Iterable[str], max_lines: int) -> List[str]:
    # Unified logs are not guaranteed to expose a stable controller identifier.
    # Collect only lines containing target identity tokens; otherwise return no
    # lines, preventing unrelated storage events from influencing diagnosis.
    clean = [str(t).strip() for t in tokens if t and len(str(t).strip()) >= 4]
    if not clean:
        return []
    predicate = 'eventMessage CONTAINS[c] "NVMe" OR eventMessage CONTAINS[c] "IONVMe" OR eventMessage CONTAINS[c] "ANS"'
    res = runner.run(["log", "show", "--last", "1d", "--style", "compact", "--predicate", predicate], timeout=15.0)
    if not res.available or res.returncode != 0:
        return []
    selected = []
    lowers = [t.lower() for t in clean]
    for line in res.stdout.splitlines():
        low = line.lower()
        if any(token in low for token in lowers):
            selected.append(line)
    return selected[-max_lines:]


def discover_controllers_macos(runner: Optional[Runner] = None) -> List[Dict[str, Any]]:
    runner = runner or Runner()
    profiler = _system_profiler_nvme(runner)
    by_device: Dict[str, Dict[str, Any]] = {}
    for item in profiler:
        bsd = str(item.get("bsd_name", "")).strip()
        if not bsd:
            continue
        ci = _profiler_to_controller(item)
        by_device[f"/dev/{bsd}"] = {
            "controller": bsd,
            "device": f"/dev/{bsd}",
            "model": ci.get("model"),
            "serial": ci.get("serial"),
            "firmware": ci.get("firmware_rev"),
            "state": ci.get("state", "present"),
            "transport": "NVMe",
            "smart_status": ci.get("smart_status"),
        }

    scan, _ = _smartctl_scan(runner)
    for item in scan:
        dev = item["device"]
        name = dev.rsplit("/", 1)[-1]
        current = by_device.setdefault(dev, {
            "controller": name,
            "device": dev,
            "state": "present",
            "transport": "NVMe",
        })
        if item.get("smartctl_type"):
            current["smartctl_type"] = item["smartctl_type"]
        current["smartctl_scan"] = True
    return sorted(by_device.values(), key=lambda x: x.get("device", ""))


def collect_snapshot_macos(
    requested_device: str,
    runner: Optional[Runner] = None,
    kernel_lines: int = 300,
) -> Snapshot:
    runner = runner or Runner()
    controller, device_path = normalize_macos_device(requested_device)
    snapshot = Snapshot(requested_device, controller, device_path)
    snapshot.host = {
        "platform": "darwin",
        "platform_label": "macOS",
        "hostname": platform.node(),
        "kernel": platform.release(),
        "machine": platform.machine(),
        "python": platform.python_version(),
        "macos_version": platform.mac_ver()[0],
    }
    snapshot.capabilities = {
        "nvme_smart": False,
        "nvme_error_log": False,
        "device_inventory": True,
        "pcie_link": False,
        "pcie_aer": False,
        "pcie_topology": False,
        "power_management": False,
        "targeted_kernel_log": False,
    }

    profiler_items = _system_profiler_nvme(runner, snapshot.collection_notes)
    profiler_item = next((x for x in profiler_items if str(x.get("bsd_name")) == controller), None)
    if profiler_item:
        snapshot.controller_info.update(_profiler_to_controller(profiler_item))
        # Some macOS versions expose Link Width/Link Speed in SPNVMeDataType.
        link_speed = profiler_item.get("link_speed") or profiler_item.get("spnvme_link_speed")
        link_width = profiler_item.get("link_width") or profiler_item.get("spnvme_link_width")
        if link_speed or link_width:
            snapshot.pci = {}
            if link_speed:
                snapshot.pci["current_link_speed"] = link_speed
            if link_width:
                snapshot.pci["current_link_width"] = link_width
            snapshot.pci["source"] = "system_profiler"
            snapshot.capabilities["pcie_link"] = "partial"
        else:
            text_link = _system_profiler_link_text(runner, controller)
            if text_link:
                snapshot.pci = dict(text_link)
                snapshot.pci["source"] = "system_profiler-text"
                snapshot.capabilities["pcie_link"] = "partial"
    else:
        snapshot.controller_info.update({"state": "unknown", "backend": "macos"})
        snapshot.collection_notes.append(f"{device_path} was not found in system_profiler SPNVMeDataType")

    scan, scan_note = _smartctl_scan(runner)
    if scan_note:
        snapshot.collection_notes.append(scan_note)
    scan_item = next((x for x in scan if x.get("device") == device_path), None)
    device_type = scan_item.get("smartctl_type") if scan_item else None
    if scan_item:
        snapshot.tools["smartctl_scan"] = scan_item

    smartctl = _smartctl_json(runner, device_path, device_type, snapshot.collection_notes)
    if isinstance(smartctl, dict):
        snapshot.tools["smartctl"] = smartctl
        _merge_smartctl_identity(snapshot, smartctl)
        health = smartctl.get("nvme_smart_health_information_log")
        if isinstance(health, dict):
            snapshot.smart = health
            snapshot.capabilities["nvme_smart"] = True
        errors = smartctl.get("nvme_error_information_log")
        if errors is not None:
            snapshot.error_log = errors
            snapshot.capabilities["nvme_error_log"] = True

    # system_profiler's Verified/Failing status is useful context but is not a
    # substitute for the standards-defined NVMe SMART/Health log.
    if profiler_item and profiler_item.get("smart_status"):
        snapshot.tools["macos_smart_status"] = profiler_item.get("smart_status")

    identity_tokens = [
        snapshot.controller_info.get("serial"),
        snapshot.controller_info.get("model"),
    ]
    log_lines = _macos_log(runner, identity_tokens, max(20, kernel_lines))
    if log_lines:
        snapshot.kernel_lines = log_lines
        snapshot.capabilities["targeted_kernel_log"] = True

    snapshot.collection_notes = list(dict.fromkeys(snapshot.collection_notes))
    return snapshot

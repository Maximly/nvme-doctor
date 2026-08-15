# SPDX-License-Identifier: GPL-3.0-or-later
from __future__ import annotations

import os
import platform
import plistlib
import re
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from .model import Snapshot
from .runner import Runner
from .util import normalize_macos_device, read_json, ata_media_wear_indicator, ata_reserved_space_indicator, smartctl_percent, to_int


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


def _canonical_macos_path(device: str) -> str:
    """Normalize /dev/diskN, /dev/rdiskN and slices to /dev/diskN."""
    try:
        _controller, path = normalize_macos_device(device)
        return path
    except ValueError:
        return device


def _read_plist(text: str) -> Optional[Dict[str, Any]]:
    try:
        value = plistlib.loads(text.encode("utf-8", errors="replace"))
    except (ValueError, plistlib.InvalidFileException):
        return None
    return value if isinstance(value, dict) else None


def _diskutil_physical_disks(runner: Runner, notes: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    """Return real whole disks from diskutil, excluding APFS synthesized disks.

    system_profiler SPNVMeDataType is good for native NVMe controllers but USB
    NVMe bridges commonly appear only as generic external physical disks.
    diskutil provides the stable BSD whole-disk inventory used to find those
    candidates; smartctl is then used to prove that the transport behind the
    bridge is actually NVMe.
    """
    notes = notes if notes is not None else []
    res = runner.run(["diskutil", "list", "-plist"], timeout=12.0)
    if not res.available:
        notes.append("built-in tool missing: diskutil")
        return []
    if res.returncode != 0:
        detail = (res.stderr or res.stdout).strip().splitlines()
        notes.append(f"diskutil list -plist failed: {(detail[-1] if detail else 'unknown error')[:240]}")
        return []
    payload = _read_plist(res.stdout)
    if not payload:
        notes.append("could not parse plist from diskutil list -plist")
        return []

    identifiers: List[str] = []
    for item in payload.get("AllDisksAndPartitions", []):
        if not isinstance(item, dict):
            continue
        ident = str(item.get("DeviceIdentifier") or "").strip()
        if re.fullmatch(r"disk\d+", ident):
            identifiers.append(ident)

    out: List[Dict[str, Any]] = []
    for ident in identifiers:
        info_res = runner.run(["diskutil", "info", "-plist", f"/dev/{ident}"], timeout=8.0)
        if not info_res.available or info_res.returncode != 0:
            continue
        info = _read_plist(info_res.stdout)
        if not info:
            continue
        whole = info.get("WholeDisk")
        if whole is False:
            continue
        virtual = str(info.get("VirtualOrPhysical") or "").lower()
        # APFS synthesized containers are virtual and must never be presented
        # as physical NVMe devices (e.g. disk5 backed by physical disk4s2).
        if virtual == "virtual":
            continue
        device = str(info.get("DeviceIdentifier") or ident)
        if not re.fullmatch(r"disk\d+", device):
            continue
        out.append({
            "controller": device,
            "device": f"/dev/{device}",
            "model": info.get("MediaName") or info.get("DeviceModel") or info.get("IORegistryEntryName"),
            "serial": info.get("SerialNumber"),
            "firmware": info.get("FirmwareRevision"),
            "size_bytes": to_int(info.get("TotalSize")),
            "internal": info.get("Internal"),
            "removable": info.get("RemovableMedia"),
            "solid_state": info.get("SolidState"),
            "bus_protocol": info.get("BusProtocol") or info.get("Protocol"),
            "virtual_or_physical": info.get("VirtualOrPhysical"),
            "smart_status": info.get("SMARTStatus"),
            "state": "present",
        })
    return out


def _diskutil_target_disk(
    runner: Runner,
    controller: str,
    notes: Optional[List[str]] = None,
) -> Optional[Dict[str, Any]]:
    """Return fast diskutil metadata for one requested whole disk.

    This is intentionally used before expensive native-NVMe collectors. On
    macOS, an external USB SSD can be classified from one `diskutil info` call
    without waiting for system_profiler, smartctl --scan-open, or unified-log
    queries that cannot provide native PCIe/NVMe data for that transport.
    """
    notes = notes if notes is not None else []
    res = runner.run(["diskutil", "info", "-plist", f"/dev/{controller}"], timeout=5.0)
    if not res.available:
        notes.append("built-in tool missing: diskutil")
        return None
    if res.returncode != 0:
        detail = (res.stderr or res.stdout).strip().splitlines()
        notes.append(
            f"diskutil info -plist /dev/{controller} failed: "
            f"{(detail[-1] if detail else 'unknown error')[:240]}"
        )
        return None
    info = _read_plist(res.stdout)
    if not info:
        notes.append(f"could not parse plist from diskutil info /dev/{controller}")
        return None

    virtual = str(info.get("VirtualOrPhysical") or "").lower()
    if virtual == "virtual":
        return None

    device = str(info.get("DeviceIdentifier") or controller).strip()
    if device != controller:
        return None

    return {
        "controller": controller,
        "device": f"/dev/{controller}",
        "model": info.get("MediaName") or info.get("DeviceModel") or info.get("IORegistryEntryName"),
        "serial": info.get("SerialNumber"),
        "firmware": info.get("FirmwareRevision"),
        "size_bytes": to_int(info.get("TotalSize")),
        "internal": info.get("Internal"),
        "removable": info.get("RemovableMedia"),
        "solid_state": info.get("SolidState"),
        "bus_protocol": info.get("BusProtocol") or info.get("Protocol"),
        "virtual_or_physical": info.get("VirtualOrPhysical"),
        "smart_status": info.get("SMARTStatus"),
        "state": "present",
    }


def _smartctl_protocol(payload: Any) -> Optional[str]:
    if not isinstance(payload, dict):
        return None
    device = payload.get("device") if isinstance(payload.get("device"), dict) else {}
    protocol = str(device.get("protocol") or "").strip().lower()
    dtype = str(device.get("type") or "").strip().lower()
    if protocol == "nvme" or "nvme" in dtype or payload.get("nvme_version") is not None or isinstance(payload.get("nvme_smart_health_information_log"), dict):
        return "NVMe"
    ata_evidence = protocol in {"ata", "sata"} or payload.get("sata_version") is not None or isinstance(payload.get("ata_smart_attributes"), dict)
    if ata_evidence or ((dtype == "ata" or dtype.startswith("sat")) and protocol not in {"scsi", "sas"}):
        return "ATA"
    return None


def _ata_attr_raw(payload: Dict[str, Any], attr_id: int) -> Optional[int]:
    attrs = payload.get("ata_smart_attributes")
    table = attrs.get("table") if isinstance(attrs, dict) else None
    if not isinstance(table, list):
        return None
    for row in table:
        if not isinstance(row, dict) or to_int(row.get("id")) != attr_id:
            continue
        raw = row.get("raw")
        if isinstance(raw, dict):
            return to_int(raw.get("value"))
        return to_int(raw)
    return None


def _ata_failed_attributes(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    attrs = payload.get("ata_smart_attributes")
    table = attrs.get("table") if isinstance(attrs, dict) else None
    if not isinstance(table, list):
        return []
    out: List[Dict[str, Any]] = []
    for row in table:
        if not isinstance(row, dict):
            continue
        when_failed = str(row.get("when_failed") or "").strip()
        value = to_int(row.get("value"))
        thresh = to_int(row.get("thresh"))
        if (when_failed and when_failed not in {"-", "Never"}) or (value is not None and thresh is not None and thresh > 0 and value <= thresh):
            out.append(row)
    return out


def _apply_ata_payload(snapshot: Snapshot, payload: Dict[str, Any], *, usb: bool = False) -> None:
    snapshot.tools["smartctl"] = payload
    ci = snapshot.controller_info
    ci.update({
        "state": "live",
        "protocol": "ATA",
        "native_nvme": False,
        "transport": "USB -> SATA" if usb else "SATA",
        "model": payload.get("model_name") or ci.get("model"),
        "serial": payload.get("serial_number") or ci.get("serial"),
        "firmware_rev": payload.get("firmware_version") or ci.get("firmware_rev"),
        "rotation_rate": payload.get("rotation_rate"),
        "form_factor": payload.get("form_factor"),
        "sata_version": payload.get("sata_version"),
        "interface_speed": payload.get("interface_speed"),
    })
    dev = payload.get("device") if isinstance(payload.get("device"), dict) else {}
    if dev.get("type"):
        ci["smartctl_device_type"] = dev.get("type")
    status = payload.get("smart_status") if isinstance(payload.get("smart_status"), dict) else {}
    temp = payload.get("temperature") if isinstance(payload.get("temperature"), dict) else {}
    poh = payload.get("power_on_time") if isinstance(payload.get("power_on_time"), dict) else {}
    err = payload.get("ata_smart_error_log") if isinstance(payload.get("ata_smart_error_log"), dict) else {}
    summary = err.get("summary") if isinstance(err.get("summary"), dict) else {}
    smart = {
        "smart_passed": status.get("passed"),
        "temperature": temp.get("current"),
        "power_on_hours": poh.get("hours"),
        "power_cycles": payload.get("power_cycle_count"),
        "reallocated_sectors": _ata_attr_raw(payload, 5),
        "reported_uncorrectable": _ata_attr_raw(payload, 187),
        "command_timeouts": _ata_attr_raw(payload, 188),
        "current_pending_sectors": _ata_attr_raw(payload, 197),
        "offline_uncorrectable": _ata_attr_raw(payload, 198),
        "udma_crc_errors": _ata_attr_raw(payload, 199),
        "ata_error_count": to_int(summary.get("count")),
        "failed_attributes": _ata_failed_attributes(payload),
    }
    reserve = ata_reserved_space_indicator(payload)
    if reserve:
        smart["reserved_space"] = reserve.get("value")
        smart["reserved_space_threshold"] = reserve.get("threshold")
        smart["reserved_space_margin"] = reserve.get("margin")
        smart["reserved_space_raw"] = reserve.get("raw")
        smart["reserved_space_source"] = {
            "attribute_id": reserve.get("attribute_id"),
            "attribute_name": reserve.get("attribute_name"),
            "sources": reserve.get("sources"),
        }
    wear = ata_media_wear_indicator(payload)
    if wear:
        smart["media_wear_indicator"] = wear.get("value")
        smart["media_wear_threshold"] = wear.get("threshold")
        smart["media_wear_raw"] = wear.get("raw")
        smart["media_wear_source"] = {
            "attribute_id": wear.get("attribute_id"),
            "attribute_name": wear.get("attribute_name"),
        }
    snapshot.smart = {k: v for k, v in smart.items() if v is not None and v != []}
    snapshot.capabilities["ata_smart"] = bool(snapshot.smart or status)
    if err:
        snapshot.error_log = err
        snapshot.capabilities["ata_error_log"] = True
    selftest = payload.get("ata_smart_self_test_log")
    if isinstance(selftest, dict):
        snapshot.tools["ata_smart_self_test_log"] = selftest
        snapshot.capabilities["ata_self_test_log"] = True
    if payload.get("interface_speed") is not None or payload.get("sata_version") is not None:
        snapshot.capabilities["sata_link"] = True


def _smartctl_identity_probe_any(runner: Runner, device: str, device_type: Optional[str] = None, timeout: float = 6.0) -> Optional[Dict[str, Any]]:
    argv = ["smartctl", "-i", "-j"]
    if device_type:
        argv += ["-d", device_type]
    argv.append(device)
    res = runner.run(argv, timeout=timeout)
    if not res.available or not res.stdout.strip():
        return None
    payload = read_json(res.stdout)
    return payload if isinstance(payload, dict) and _smartctl_protocol(payload) else None


def _smartctl_identity_probe(
    runner: Runner,
    device: str,
    device_type: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Read only identity data and return it only when smartctl proves NVMe."""
    argv = ["smartctl", "-i", "-j"]
    if device_type:
        argv += ["-d", device_type]
    argv.append(device)
    res = runner.run(argv, timeout=10.0)
    if not res.available or not res.stdout.strip():
        return None
    payload = read_json(res.stdout)
    if not isinstance(payload, dict):
        return None
    dev = payload.get("device")
    protocol = str(dev.get("protocol") if isinstance(dev, dict) else "").lower()
    dtype = str(dev.get("type") if isinstance(dev, dict) else "").lower()
    if protocol != "nvme" and "nvme" not in dtype and payload.get("nvme_version") is None:
        return None
    return payload


def _raw_macos_path(device: str) -> str:
    """Return the raw BSD device path for a whole disk when possible."""
    canonical = _canonical_macos_path(device)
    if canonical.startswith("/dev/disk"):
        return "/dev/rdisk" + canonical[len("/dev/disk"):]
    return canonical


def _darwin_usb_nvme_passthrough_limitation() -> str:
    """Explain the current smartmontools/Darwin USB-NVMe limitation.

    smartmontools SNT backends (sntrealtek/sntjmicron/sntasmedia) are
    NVMe-over-SCSI tunnel devices.  The Darwin smartmontools backend does not
    currently implement a SCSI device interface, so forcing those backends on
    /dev/diskN or /dev/rdiskN cannot work on macOS.  Native Apple NVMe remains
    accessible through IONVMeSMARTInterface and is handled separately.
    """
    return (
        "macOS limitation: smartmontools' Darwin backend does not provide the SCSI "
        "pass-through device required by sntrealtek/sntjmicron/sntasmedia. "
        "The USB SSD is visible as block storage, but its underlying NVMe SMART/admin "
        "interface is not reachable through smartctl on this platform."
    )


def _identity_fields(payload: Dict[str, Any]) -> Dict[str, Any]:
    device = payload.get("device")
    return {
        "model": payload.get("model_name"),
        "serial": payload.get("serial_number"),
        "firmware": payload.get("firmware_version"),
        "protocol": device.get("protocol") if isinstance(device, dict) else None,
    }

def _parse_smartctl_scan(text: str) -> List[Dict[str, str]]:
    """Parse smartctl scan output, retaining NVMe bridge type hints.

    Do not depend on the comment containing the literal word ``NVMe``: retain
    any SNT type that smartctl itself reports (useful for forward compatibility
    or alternate builds). ``/dev/rdiskN`` is canonicalized to ``/dev/diskN``
    so it merges with system_profiler/diskutil inventory.
    """
    out: List[Dict[str, str]] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        m = re.match(r"^(?P<device>/\S+)(?:\s+-d\s+(?P<type>\S+))?", line)
        if not m:
            continue
        raw_device = m.group("device")
        dtype = m.group("type") or ""
        low = line.lower()
        dtype_low = dtype.lower()
        supported = (
            "nvme" in low or "ata" in low or "sata" in low
            or dtype_low == "nvme" or dtype_low.startswith("snt")
            or dtype_low == "ata" or dtype_low.startswith("sat")
        )
        if not supported:
            continue
        item = {"device": _canonical_macos_path(raw_device)}
        if raw_device != item["device"]:
            item["smartctl_device"] = raw_device
        if dtype:
            item["smartctl_type"] = dtype
        if "nvme" in low or dtype_low == "nvme" or dtype_low.startswith("snt"):
            item["protocol_hint"] = "NVMe"
        elif "ata" in low or "sata" in low or dtype_low == "ata" or dtype_low.startswith("sat"):
            item["protocol_hint"] = "ATA"
        item["scan_line"] = line
        out.append(item)
    return out


def _smartctl_scan(runner: Runner) -> Tuple[List[Dict[str, str]], Optional[str]]:
    """Merge smartctl --scan-open and --scan results.

    On macOS an external USB bridge can be present in plain --scan while
    --scan-open reports only devices it could successfully open.  Returning
    early after finding the internal Apple NVMe therefore hid external disks.
    """
    opened = runner.run(["smartctl", "--scan-open"], timeout=15.0)
    if not opened.available:
        return [], "smartctl is not installed; full NVMe health data on macOS requires smartmontools (for Homebrew: `brew install smartmontools`)"

    plain = runner.run(["smartctl", "--scan"], timeout=10.0)
    candidates = _parse_smartctl_scan(opened.stdout)
    if plain.available:
        candidates.extend(_parse_smartctl_scan(plain.stdout))

    dedup: Dict[Tuple[str, str], Dict[str, str]] = {}
    for item in candidates:
        key = (item.get("device", ""), item.get("smartctl_type", ""))
        # Preserve scan-open's raw device path if both commands saw the same
        # target; otherwise plain scan contributes devices scan-open skipped.
        dedup.setdefault(key, item)
    items = list(dedup.values())
    if items:
        return items, None

    detail = (opened.stderr or opened.stdout).strip().splitlines()
    if opened.returncode != 0:
        return [], f"smartctl --scan-open failed: {(detail[-1] if detail else f'exit status {opened.returncode}')[:240]}"
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
    protocol = None
    if isinstance(device, dict):
        if device.get("protocol"):
            protocol = str(device["protocol"])
            ci["protocol"] = device["protocol"]
        if device.get("type"):
            ci["smartctl_device_type"] = device["type"]
    # A generic USB/SCSI identity proves that the block device is reachable,
    # not that NVMe admin passthrough works.  Preserve the `limited`
    # state unless smartctl actually reached an NVMe device.
    if (protocol and protocol.lower() == "nvme") or payload.get("nvme_version") is not None:
        ci["state"] = "live"


def _macos_log(runner: Runner, tokens: Iterable[str], max_lines: int) -> List[str]:
    # Unified logs are not guaranteed to expose a stable controller identifier.
    # Collect only lines containing target identity tokens; otherwise return no
    # lines, preventing unrelated storage events from influencing diagnosis.
    clean = [str(t).strip() for t in tokens if t and len(str(t).strip()) >= 4]
    if not clean:
        return []
    predicate = 'eventMessage CONTAINS[c] "NVMe" OR eventMessage CONTAINS[c] "IONVMe" OR eventMessage CONTAINS[c] "ANS" OR eventMessage CONTAINS[c] "SATA" OR eventMessage CONTAINS[c] "AHCI" OR eventMessage CONTAINS[c] "I/O error"'
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
            "protocol": "NVMe",
            "smart_status": ci.get("smart_status"),
            "backend": "system_profiler",
        }

    scan, _ = _smartctl_scan(runner)
    for item in scan:
        dev = _canonical_macos_path(item["device"])
        name = dev.rsplit("/", 1)[-1]
        hint = item.get("protocol_hint") or "NVMe"
        current = by_device.setdefault(dev, {
            "controller": name,
            "device": dev,
            "state": "present",
            "transport": "SATA" if hint == "ATA" else "NVMe",
            "protocol": hint,
            "backend": "smartctl-scan",
        })
        if item.get("smartctl_type"):
            current["smartctl_type"] = item["smartctl_type"]
            dtype = str(item["smartctl_type"]).lower()
            if dtype.startswith("snt"):
                current["transport"] = "USB -> NVMe"
            elif dtype.startswith("sat"):
                current["transport"] = "USB -> SATA"
        if item.get("smartctl_device"):
            current["smartctl_device"] = item["smartctl_device"]
        current["smartctl_scan"] = True
        probe_device = item.get("smartctl_device") or dev
        identity = _smartctl_identity_probe_any(runner, probe_device, item.get("smartctl_type"))
        if identity:
            proto = _smartctl_protocol(identity)
            current["protocol"] = proto
            if proto == "ATA":
                current["model"] = identity.get("model_name") or current.get("model")
                current["serial"] = identity.get("serial_number") or current.get("serial")
                current["firmware"] = identity.get("firmware_version") or current.get("firmware")
                if str(item.get("smartctl_type") or "").lower().startswith("sat"):
                    current["transport"] = "USB -> SATA"
                else:
                    current["transport"] = "SATA"
                current["rotation_rate"] = identity.get("rotation_rate")
            else:
                for key, value in _identity_fields(identity).items():
                    if value not in (None, "") and not current.get(key):
                        current[key] = value

    # SPNVMeDataType normally omits USB-attached NVMe.  Use diskutil only to
    # enumerate real external whole disks, then prove NVMe with one of the
    # smartmontools SNT bridge backends.  Synthesized APFS disks are excluded.
    for candidate in _diskutil_physical_disks(runner):
        dev = candidate["device"]
        if dev in by_device:
            current = by_device[dev]
            current.setdefault("bus_protocol", candidate.get("bus_protocol"))
            current.setdefault("internal", candidate.get("internal"))
            current.setdefault("size_bytes", candidate.get("size_bytes"))
            continue
        bus = str(candidate.get("bus_protocol") or "").lower()
        if candidate.get("internal") is True:
            if "sata" in bus or "ata" in bus:
                by_device[dev] = {
                    "controller": candidate["controller"],
                    "device": dev,
                    "model": candidate.get("model"),
                    "serial": candidate.get("serial"),
                    "firmware": candidate.get("firmware"),
                    "state": "probe-needed",
                    "transport": "SATA",
                    "protocol": "ATA",
                    "bus_protocol": candidate.get("bus_protocol"),
                    "internal": True,
                    "size_bytes": candidate.get("size_bytes"),
                    "smart_status": candidate.get("smart_status"),
                    "backend": "diskutil",
                }
            continue
        # SNT/SAT are USB/SCSI translation mechanisms. Avoid bridge probes
        # against unrelated Thunderbolt/FireWire physical disks.
        if bus and "usb" not in bus:
            continue

        # `smartctl --scan` is not a complete physical-disk inventory on macOS.
        # A USB-SATA bridge may be absent from scan output while `diskutil` sees
        # the disk and `smartctl -d sat` can still identify the underlying ATA
        # device. Probe identity only (short timeout) so `list` classifies SATA
        # HDDs/SSDs instead of hiding them behind a generic "USB disk" row.
        sat_identity = _smartctl_identity_probe_any(runner, dev, "sat", timeout=1.5)
        if isinstance(sat_identity, dict) and _smartctl_protocol(sat_identity) == "ATA":
            device_info = sat_identity.get("device") if isinstance(sat_identity.get("device"), dict) else {}
            by_device[dev] = {
                "controller": candidate["controller"],
                "device": dev,
                "model": sat_identity.get("model_name") or candidate.get("model"),
                "serial": sat_identity.get("serial_number") or candidate.get("serial"),
                "firmware": sat_identity.get("firmware_version") or candidate.get("firmware"),
                "state": "present",
                "transport": "USB -> SATA",
                "protocol": "ATA",
                "smartctl_type": device_info.get("type") or "sat",
                "smartctl_device": dev,
                "bus_protocol": candidate.get("bus_protocol") or "USB",
                "internal": candidate.get("internal"),
                "size_bytes": candidate.get("size_bytes"),
                "smart_status": candidate.get("smart_status"),
                "rotation_rate": sat_identity.get("rotation_rate"),
                "backend": "diskutil+smartctl-sat",
            }
            continue

        # Current smartmontools Darwin builds have no SCSI device backend for
        # SNT NVMe bridges. Keep unclassified USB media visible below.
        # The snt* bridge modes therefore cannot be forced on /dev/diskN or
        # /dev/rdiskN: they require a SCSI tunnel device underneath.  Keep
        # the physical SSD visible but explicitly limited.  If a future
        # smartctl scan reports this device as NVMe/SNT, the scan path above
        # will take precedence automatically.
        if not bus or "usb" in bus:
            is_ssd = candidate.get("solid_state") is True
            by_device[dev] = {
                "controller": candidate["controller"],
                "device": dev,
                "model": candidate.get("model"),
                "serial": candidate.get("serial"),
                "firmware": candidate.get("firmware"),
                "state": "limited",
                "transport": "USB SSD" if is_ssd else "USB disk",
                "bus_protocol": candidate.get("bus_protocol") or "USB",
                "internal": candidate.get("internal"),
                "size_bytes": candidate.get("size_bytes"),
                "smart_status": candidate.get("smart_status"),
                "nvme_passthrough": False if is_ssd else None,
                "passthrough_reason": "darwin-no-scsi-passthrough" if is_ssd else "smart-passthrough-unavailable",
                "backend": "diskutil",
            }

    # Non-disruptive direct-USB capability discovery for RTL9210.  This does
    # not claim the interface or send any command; it only reports that
    # the deeper macOS path is available when explicitly requested.
    opaque_usb = [
        x for x in by_device.values()
        if x.get("transport") == "USB SSD" and x.get("state") == "limited"
    ]
    if len(opaque_usb) == 1:
        try:
            from .macos_usb_nvme import rtl9210_inventory
            bridges = rtl9210_inventory()
        except Exception:
            bridges = []
        if len(bridges) == 1:
            current = opaque_usb[0]
            bridge = bridges[0]
            current["state"] = "direct-ready"
            current["transport"] = "USB -> NVMe"
            current["direct_usb"] = True
            current["usb_bridge"] = "Realtek RTL9210"
            current["usb_vid_pid"] = "0bda:9210"
            current["usb_product"] = bridge.get("product")
            current["direct_usb_requires_root"] = True
            current["direct_usb_disruptive"] = True

    return sorted(by_device.values(), key=lambda x: x.get("device", ""))


def _populate_external_usb_snapshot(
    snapshot: Snapshot,
    physical: Dict[str, Any],
    *,
    controller: str,
    device_path: str,
    direct_usb: bool,
    auto_direct_usb: bool = False,
    direct_usb_mount_state_checked: bool = False,
    direct_usb_mounts_before: Optional[List[str]] = None,
    usb_extra_logs: bool = False,
    progress: Optional[Callable[[str], None]] = None,
) -> None:
    """Populate an external USB SSD snapshot without slow native-NVMe probes."""
    snapshot.controller_info.update({
        "backend": "diskutil",
        "state": "limited",
        "model": physical.get("model"),
        "usb_bridge_model": physical.get("model"),
        "serial": physical.get("serial"),
        "firmware_rev": physical.get("firmware"),
        "transport": "USB SSD",
        "bus_protocol": physical.get("bus_protocol") or "USB",
        "size_in_bytes": physical.get("size_bytes"),
        "solid_state": physical.get("solid_state"),
        "smart_status": physical.get("smart_status"),
    })

    if not direct_usb and auto_direct_usb:
        if progress:
            progress("Detecting RTL9210 for automatic privileged direct access")
        try:
            from .macos_usb_nvme import rtl9210_inventory
            bridges = rtl9210_inventory(progress=progress)
        except Exception as exc:
            bridges = []
            if progress:
                progress(f"RTL9210 capability probe failed: {exc}")
        if len(bridges) == 1:
            direct_usb = True
            if progress:
                progress("RTL9210 detected under sudo: entering direct read-only NVMe mode")
        elif len(bridges) > 1 and progress:
            progress(f"{len(bridges)} RTL9210 bridges detected; refusing ambiguous automatic capture")

    if direct_usb:
        try:
            from .macos_usb_nvme import read_rtl9210_nvme
            direct = read_rtl9210_nvme(
                device_path,
                mount_state_checked=direct_usb_mount_state_checked,
                mounts_before=direct_usb_mounts_before,
                collect_optional_logs=usb_extra_logs,
                progress=progress,
            )
            ident = direct.get("identify") or {}
            bridge = direct.get("bridge") or {}
            snapshot.tools["macos_direct_usb"] = {"bridge": bridge}
            snapshot.controller_info.update({
                "backend": "macos-direct-usb",
                "state": "live",
                "transport": "USB -> NVMe",
                "bus_protocol": "USB",
                "usb_bridge": "Realtek RTL9210",
                "usb_vid_pid": "0bda:9210",
                "usb_bridge_model": physical.get("model"),
                "usb_bridge_manufacturer": bridge.get("manufacturer"),
                "usb_bridge_product": bridge.get("product"),
                "nvme_passthrough": True,
                "direct_usb": True,
            })
            for src, dst in [
                ("model", "model"), ("serial", "serial"),
                ("firmware_rev", "firmware_rev"), ("nvme_version", "nvme_version"),
                ("nvme_version_raw", "nvme_version_raw"), ("controller_id", "controller_id"),
                ("tnvmcap", "tnvmcap"), ("unvmcap", "unvmcap"),
                ("vid", "pci_vendor_id"), ("ssvid", "pci_subsystem_vendor_id"),
                ("ieee_oui", "ieee_oui"), ("mdts", "mdts"),
                ("oacs", "oacs"), ("oncs", "oncs"),
            ]:
                if ident.get(src) not in (None, ""):
                    snapshot.controller_info[dst] = ident[src]
            smart = direct.get("smart")
            if isinstance(smart, dict):
                snapshot.smart = smart
                snapshot.capabilities["nvme_smart"] = True
            error_log = direct.get("error_log")
            if isinstance(error_log, list):
                snapshot.error_log = error_log
                snapshot.capabilities["nvme_error_log"] = True
            fw_slots = direct.get("firmware_slot_log")
            if isinstance(fw_slots, dict):
                snapshot.tools["nvme_firmware_slot_log"] = fw_slots
            for note in direct.get("optional_log_errors") or []:
                snapshot.collection_notes.append(f"optional RTL9210 NVMe log unavailable: {note}")
            detail = "Identify + SMART"
            if usb_extra_logs:
                detail += " + optional Error/Firmware Slot logs"
            snapshot.collection_notes.append(
                f"Direct macOS USB mode temporarily acquired the RTL9210, read NVMe {detail} "
                "through BOT, then restored the original disk mount state."
            )
        except Exception as exc:
            if progress:
                progress(f"Direct USB access failed: {exc}")
            snapshot.controller_info["nvme_passthrough"] = False
            snapshot.controller_info["passthrough_reason"] = "direct-usb-failed"
            snapshot.collection_notes.append(f"direct macOS USB NVMe access failed: {exc}")
        return

    snapshot.controller_info["nvme_passthrough"] = False
    snapshot.controller_info["passthrough_reason"] = "darwin-no-scsi-passthrough"
    snapshot.collection_notes.append(_darwin_usb_nvme_passthrough_limitation())
    if progress:
        progress("Checking direct RTL9210 capability")
    try:
        from .macos_usb_nvme import rtl9210_inventory
        bridges = rtl9210_inventory(progress=progress)
    except Exception:
        bridges = []
    if len(bridges) == 1:
        bridge = bridges[0]
        snapshot.controller_info.update({
            "state": "direct-ready",
            "transport": "USB -> NVMe",
            "direct_usb": True,
            "usb_bridge": "Realtek RTL9210",
            "usb_vid_pid": "0bda:9210",
            "usb_bridge_model": physical.get("model"),
            "usb_bridge_manufacturer": bridge.get("manufacturer"),
            "usb_bridge_product": bridge.get("product"),
        })
        snapshot.collection_notes.append(
            f"Direct RTL9210 access is available; rerun with `sudo nvme-doctor check {controller}` "
            "to temporarily unmount/capture the enclosure and read NVMe SMART."
        )


def collect_snapshot_macos(
    requested_device: str,
    runner: Optional[Runner] = None,
    kernel_lines: int = 300,
    direct_usb: bool = False,
    auto_direct_usb: bool = False,
    direct_usb_mount_state_checked: bool = False,
    direct_usb_mounts_before: Optional[List[str]] = None,
    usb_extra_logs: bool = False,
    progress: Optional[Callable[[str], None]] = None,
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
        "euid": os.geteuid(),
    }
    snapshot.capabilities = {
        "nvme_smart": False,
        "nvme_error_log": False,
        "ata_smart": False,
        "ata_error_log": False,
        "ata_self_test_log": False,
        "sata_link": False,
        "device_inventory": True,
        "pcie_link": False,
        "pcie_aer": False,
        "pcie_topology": False,
        "power_management": False,
        "targeted_kernel_log": False,
    }

    if progress:
        progress(f"Inspecting {device_path} and its storage transport")

    # Fast-path external USB SSDs before native-NVMe collectors. system_profiler
    # SPNVMeDataType, smartctl --scan-open and unified-log queries can each take
    # many seconds on macOS and cannot reveal native PCIe/AER/NUMA information
    # through an opaque USB bridge anyway.
    if progress:
        progress("Reading disk metadata with diskutil")
    target_physical = _diskutil_target_disk(runner, controller, snapshot.collection_notes)
    if target_physical:
        bus = str(target_physical.get("bus_protocol") or "").lower()

        # Native SATA/ATA on macOS: smartctl can expose ATA SMART directly.
        if "sata" in bus or "ata" in bus:
            if progress:
                progress("Reading ATA/SATA SMART through smartctl")
            payload = _smartctl_json(runner, device_path, None, snapshot.collection_notes)
            if isinstance(payload, dict) and _smartctl_protocol(payload) == "ATA":
                snapshot.controller_info.update({
                    "backend": "diskutil+smartctl",
                    "bus_protocol": target_physical.get("bus_protocol") or "SATA",
                    "size_in_bytes": target_physical.get("size_bytes"),
                    "solid_state": target_physical.get("solid_state"),
                })
                _apply_ata_payload(snapshot, payload, usb=False)
                if progress:
                    progress("Collecting target-scoped macOS storage events")
                log_lines = _macos_log(runner, [snapshot.controller_info.get("serial"), snapshot.controller_info.get("model")], max(20, kernel_lines))
                if log_lines:
                    snapshot.kernel_lines = log_lines
                    snapshot.capabilities["targeted_kernel_log"] = True
                if progress:
                    progress("Evidence collection complete")
                return snapshot

        # For a physical USB disk, first test the standard SAT backend. This
        # covers USB-SATA HDDs/SSDs without disturbing the enclosure. If SAT
        # does not identify ATA and the media is an SSD, continue to the
        # existing RTL9210/NVMe path.
        if target_physical.get("internal") is not True and (not bus or "usb" in bus):
            if progress:
                progress("Checking USB-SATA SAT passthrough")
            ata_identity = _smartctl_identity_probe_any(runner, device_path, "sat", timeout=1.5)
            if isinstance(ata_identity, dict) and _smartctl_protocol(ata_identity) == "ATA":
                payload = _smartctl_json(runner, device_path, "sat", snapshot.collection_notes)
                if isinstance(payload, dict) and _smartctl_protocol(payload) == "ATA":
                    snapshot.controller_info.update({
                        "backend": "diskutil+smartctl-sat",
                        "bus_protocol": target_physical.get("bus_protocol") or "USB",
                        "size_in_bytes": target_physical.get("size_bytes"),
                        "solid_state": target_physical.get("solid_state"),
                    })
                    _apply_ata_payload(snapshot, payload, usb=True)
                    if progress:
                        progress("Evidence collection complete")
                    return snapshot

            if target_physical.get("solid_state") is True:
                _populate_external_usb_snapshot(
                    snapshot,
                    target_physical,
                    controller=controller,
                    device_path=device_path,
                    direct_usb=direct_usb,
                    auto_direct_usb=auto_direct_usb,
                    direct_usb_mount_state_checked=direct_usb_mount_state_checked,
                    direct_usb_mounts_before=direct_usb_mounts_before,
                    usb_extra_logs=usb_extra_logs,
                    progress=progress,
                )
                if progress:
                    progress("Evidence collection complete")
                return snapshot
            elif target_physical.get("internal") is not True:
                snapshot.controller_info.update({
                    "backend": "diskutil",
                    "state": "limited",
                    "model": target_physical.get("model"),
                    "serial": target_physical.get("serial"),
                    "firmware_rev": target_physical.get("firmware"),
                    "transport": "USB disk",
                    "bus_protocol": target_physical.get("bus_protocol") or "USB",
                    "size_in_bytes": target_physical.get("size_bytes"),
                    "solid_state": target_physical.get("solid_state"),
                })
                snapshot.collection_notes.append("USB disk is visible, but ATA/SATA SMART passthrough was not available through smartctl -d sat")
                if progress:
                    progress("Evidence collection complete")
                return snapshot

    if progress:
        progress("Checking native NVMe inventory with system_profiler")
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
            if progress:
                progress("Reading native NVMe link details")
            text_link = _system_profiler_link_text(runner, controller)
            if text_link:
                snapshot.pci = dict(text_link)
                snapshot.pci["source"] = "system_profiler-text"
                snapshot.capabilities["pcie_link"] = "partial"
    else:
        snapshot.controller_info.update({"state": "unknown", "backend": "macos"})
        snapshot.collection_notes.append(f"{device_path} was not found in system_profiler SPNVMeDataType")

    if progress:
        progress("Scanning smartctl NVMe/bridge backends")
    scan, scan_note = _smartctl_scan(runner)
    if scan_note:
        snapshot.collection_notes.append(scan_note)
    scan_item = next((x for x in scan if _canonical_macos_path(x.get("device", "")) == device_path), None)
    device_type = scan_item.get("smartctl_type") if scan_item else None
    smartctl_device = scan_item.get("smartctl_device") if scan_item else None
    if scan_item:
        snapshot.tools["smartctl_scan"] = scan_item
        dtype = str(device_type or "").lower()
        hint = scan_item.get("protocol_hint")
        if dtype.startswith("snt"):
            snapshot.controller_info["transport"] = "USB -> NVMe"
            snapshot.controller_info["bus_protocol"] = "USB"
        elif dtype.startswith("sat") or hint == "ATA":
            snapshot.controller_info["transport"] = "USB -> SATA" if dtype.startswith("sat") else "SATA"
            snapshot.controller_info["protocol"] = "ATA"

    # USB NVMe may be absent from SPNVMeDataType and from smartctl's automatic
    # scan.  If diskutil proves this is an external physical disk, try the
    # supported NVMe-over-SCSI translation backends before giving up.
    if not scan_item and not profiler_item:
        if progress:
            progress("Enumerating physical disks with diskutil")
        physical = next((x for x in _diskutil_physical_disks(runner, snapshot.collection_notes) if x.get("device") == device_path), None)
        if physical and physical.get("internal") is not True:
            bus = str(physical.get("bus_protocol") or "").lower()
            if not bus or "usb" in bus:
                # Populate physical-device facts regardless of whether NVMe
                # admin passthrough works.  This makes an opaque USB enclosure
                # visible and lets Doctor explain the actual limitation.
                snapshot.controller_info.update({
                    "backend": "diskutil",
                    "state": "limited",
                    "model": physical.get("model"),
                    "serial": physical.get("serial"),
                    "firmware_rev": physical.get("firmware"),
                    "transport": "USB SSD",
                    "bus_protocol": physical.get("bus_protocol") or "USB",
                    "size_in_bytes": physical.get("size_bytes"),
                    "solid_state": physical.get("solid_state"),
                    "smart_status": physical.get("smart_status"),
                })

                if direct_usb:
                    try:
                        from .macos_usb_nvme import read_rtl9210_nvme
                        direct = read_rtl9210_nvme(
                            device_path,
                            mount_state_checked=direct_usb_mount_state_checked,
                            mounts_before=direct_usb_mounts_before,
                            collect_optional_logs=usb_extra_logs,
                            progress=progress,
                        )
                        ident = direct.get("identify") or {}
                        bridge = direct.get("bridge") or {}
                        snapshot.tools["macos_direct_usb"] = {"bridge": bridge}
                        snapshot.controller_info.update({
                            "backend": "macos-direct-usb",
                            "state": "live",
                            "transport": "USB -> NVMe",
                            "bus_protocol": "USB",
                            "usb_bridge": "Realtek RTL9210",
                            "usb_vid_pid": "0bda:9210",
                            "usb_bridge_product": bridge.get("product"),
                            "nvme_passthrough": True,
                            "direct_usb": True,
                        })
                        for src, dst in [
                            ("model", "model"), ("serial", "serial"),
                            ("firmware_rev", "firmware_rev"), ("nvme_version", "nvme_version"),
                            ("nvme_version_raw", "nvme_version_raw"), ("controller_id", "controller_id"),
                            ("tnvmcap", "tnvmcap"), ("unvmcap", "unvmcap"),
                            ("vid", "pci_vendor_id"), ("ssvid", "pci_subsystem_vendor_id"),
                            ("ieee_oui", "ieee_oui"), ("mdts", "mdts"),
                            ("oacs", "oacs"), ("oncs", "oncs"),
                        ]:
                            if ident.get(src) not in (None, ""):
                                snapshot.controller_info[dst] = ident[src]
                        smart = direct.get("smart")
                        if isinstance(smart, dict):
                            snapshot.smart = smart
                            snapshot.capabilities["nvme_smart"] = True
                        error_log = direct.get("error_log")
                        if isinstance(error_log, list):
                            snapshot.error_log = error_log
                            snapshot.capabilities["nvme_error_log"] = True
                        fw_slots = direct.get("firmware_slot_log")
                        if isinstance(fw_slots, dict):
                            snapshot.tools["nvme_firmware_slot_log"] = fw_slots
                        for note in direct.get("optional_log_errors") or []:
                            snapshot.collection_notes.append(f"optional RTL9210 NVMe log unavailable: {note}")
                        detail = "Identify + SMART"
                        if usb_extra_logs:
                            detail += " + optional Error/Firmware Slot logs"
                        snapshot.collection_notes.append(
                            f"Direct macOS USB mode temporarily acquired the RTL9210, read NVMe {detail} "
                            "through BOT, then restored the original disk mount state."
                        )
                    except Exception as exc:
                        snapshot.controller_info["nvme_passthrough"] = False
                        snapshot.controller_info["passthrough_reason"] = "direct-usb-failed"
                        snapshot.collection_notes.append(f"direct macOS USB NVMe access failed: {exc}")
                else:
                    snapshot.controller_info["nvme_passthrough"] = False
                    snapshot.controller_info["passthrough_reason"] = "darwin-no-scsi-passthrough"
                    snapshot.collection_notes.append(_darwin_usb_nvme_passthrough_limitation())
                    if progress:
                        progress("Checking direct RTL9210 capability")
                    try:
                        from .macos_usb_nvme import rtl9210_inventory
                        bridges = rtl9210_inventory(progress=progress)
                    except Exception:
                        bridges = []
                    if len(bridges) == 1:
                        snapshot.controller_info.update({
                            "state": "direct-ready",
                            "transport": "USB -> NVMe",
                            "direct_usb": True,
                            "usb_bridge": "Realtek RTL9210",
                            "usb_vid_pid": "0bda:9210",
                        })
                        snapshot.collection_notes.append(
                            f"Direct RTL9210 access is available; rerun with `sudo nvme-doctor check {controller}` "
                            "to temporarily unmount/capture the enclosure and read NVMe SMART."
                        )

    # Do not issue a generic `smartctl -a /dev/diskN` against a diskutil-only
    # external USB SSD on Darwin: without a scan-proven NVMe/SNT path it cannot
    # reach the underlying NVMe admin interface and only adds a misleading
    # collection error.  Native or scan-proven devices still use smartctl.
    smartctl = None
    if not snapshot.controller_info.get("direct_usb") or not snapshot.capabilities.get("nvme_smart"):
        if profiler_item or scan_item or snapshot.controller_info.get("nvme_passthrough") is not False:
            if progress:
                progress("Reading SMART / NVMe health data")
            smartctl = _smartctl_json(runner, smartctl_device or device_path, device_type, snapshot.collection_notes)
    if isinstance(smartctl, dict):
        proto = _smartctl_protocol(smartctl)
        if proto == "ATA":
            _apply_ata_payload(snapshot, smartctl, usb=str(snapshot.controller_info.get("transport") or "").startswith("USB"))
        else:
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
    if progress:
        progress("Collecting target-scoped macOS storage events")
    log_lines = _macos_log(runner, identity_tokens, max(20, kernel_lines))
    if log_lines:
        snapshot.kernel_lines = log_lines
        snapshot.capabilities["targeted_kernel_log"] = True

    snapshot.collection_notes = list(dict.fromkeys(snapshot.collection_notes))
    if progress:
        progress("Evidence collection complete")
    return snapshot

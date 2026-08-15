# SPDX-License-Identifier: GPL-3.0-or-later
from __future__ import annotations

import os
import platform
import re
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .model import Snapshot
from .runner import Runner
from .util import normalize_device, parse_counter_blob, read_json, read_text, ata_media_wear_indicator, ata_reserved_space_indicator, smartctl_percent, to_int
from .platforms import platform_key


SYS_CLASS_NVME = Path("/sys/class/nvme")
SYS_PCI = Path("/sys/bus/pci/devices")
SYS_CLASS_BLOCK = Path("/sys/class/block")


def _os_release() -> Dict[str, str]:
    data: Dict[str, str] = {}
    path = Path("/etc/os-release")
    text = read_text(path)
    if not text:
        return data
    for line in text.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        data[key] = value.strip().strip('"')
    return data


def _discover_native_controllers_linux(
    sys_class_nvme: Path = SYS_CLASS_NVME,
    sys_class_block: Path = SYS_CLASS_BLOCK,
) -> List[Dict[str, Any]]:
    result: List[Dict[str, Any]] = []
    if not sys_class_nvme.exists():
        return result
    for path in sorted(sys_class_nvme.glob("nvme[0-9]*")):
        if not re.fullmatch(r"nvme\d+", path.name):
            continue
        total_size = 0
        have_size = False
        if sys_class_block.exists():
            for ns in sorted(sys_class_block.glob(f"{path.name}n*")):
                if not re.fullmatch(rf"{re.escape(path.name)}n\d+", ns.name):
                    continue
                sectors = to_int(read_text(ns / "size"))
                if sectors is not None and sectors >= 0:
                    total_size += sectors * 512
                    have_size = True
        info = {
            "controller": path.name,
            "device": f"/dev/{path.name}",
            "model": read_text(path / "model"),
            "serial": read_text(path / "serial"),
            "firmware": read_text(path / "firmware_rev"),
            "state": read_text(path / "state"),
            "transport": read_text(path / "transport") or "PCIe/NVMe",
            "address": read_text(path / "address"),
            "native_nvme": True,
            "protocol": "NVMe",
            "size_bytes": total_size if have_size else None,
        }
        result.append(info)
    return result




def _smartctl_json_protocol(payload: Any) -> Optional[str]:
    """Classify a smartctl JSON payload as NVMe, ATA/SATA, or unknown."""
    if not isinstance(payload, dict):
        return None
    device = payload.get("device") if isinstance(payload.get("device"), dict) else {}
    protocol = str(device.get("protocol") or "").strip().lower()
    dev_type = str(device.get("type") or "").strip().lower()
    if (
        protocol == "nvme"
        or "nvme" in dev_type
        or payload.get("nvme_version") is not None
        or isinstance(payload.get("nvme_smart_health_information_log"), dict)
    ):
        return "NVMe"
    ata_evidence = (
        protocol in {"ata", "sata"}
        or payload.get("sata_version") is not None
        or isinstance(payload.get("ata_smart_attributes"), dict)
        or isinstance(payload.get("ata_smart_data"), dict)
    )
    if ata_evidence or ((dev_type == "ata" or dev_type.startswith("sat")) and protocol not in {"scsi", "sas"}):
        return "ATA"
    return None


def _smartctl_json_is_ata(payload: Any) -> bool:
    return _smartctl_json_protocol(payload) == "ATA"


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
            value = to_int(raw.get("value"))
            if value is not None:
                return value
        return to_int(raw)
    return None


def _ata_failed_attributes(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    attrs = payload.get("ata_smart_attributes")
    table = attrs.get("table") if isinstance(attrs, dict) else None
    if not isinstance(table, list):
        return []
    failed: List[Dict[str, Any]] = []
    for row in table:
        if not isinstance(row, dict):
            continue
        when_failed = str(row.get("when_failed") or "").strip()
        value = to_int(row.get("value"))
        thresh = to_int(row.get("thresh"))
        if when_failed and when_failed not in {"-", "Never"}:
            failed.append(row)
        elif value is not None and thresh is not None and thresh > 0 and value <= thresh:
            failed.append(row)
    return failed


def _apply_smartctl_ata_payload(snapshot: Snapshot, payload: Dict[str, Any], *, usb: bool = False) -> None:
    """Normalize smartctl ATA/SATA JSON into the common Snapshot model."""
    snapshot.tools["smartctl"] = payload
    ci = snapshot.controller_info
    ci["protocol"] = "ATA"
    ci["native_nvme"] = False
    ci["transport"] = "USB -> SATA" if usb else "SATA"
    ci["model"] = payload.get("model_name") or payload.get("model_family") or ci.get("model")
    ci["serial"] = payload.get("serial_number") or ci.get("serial")
    ci["firmware_rev"] = payload.get("firmware_version") or ci.get("firmware_rev")
    ci["wwn"] = payload.get("wwn")
    ci["rotation_rate"] = payload.get("rotation_rate")
    ci["form_factor"] = payload.get("form_factor")
    ci["sata_version"] = payload.get("sata_version")
    ci["interface_speed"] = payload.get("interface_speed")
    device = payload.get("device") if isinstance(payload.get("device"), dict) else {}
    if device.get("type"):
        ci["smartctl_device_type"] = device.get("type")

    smart_status = payload.get("smart_status") if isinstance(payload.get("smart_status"), dict) else {}
    temp = payload.get("temperature") if isinstance(payload.get("temperature"), dict) else {}
    poh = payload.get("power_on_time") if isinstance(payload.get("power_on_time"), dict) else {}
    error_log = payload.get("ata_smart_error_log") if isinstance(payload.get("ata_smart_error_log"), dict) else {}
    error_summary = error_log.get("summary") if isinstance(error_log.get("summary"), dict) else {}

    smart: Dict[str, Any] = {
        "smart_passed": smart_status.get("passed"),
        "temperature": temp.get("current"),
        "power_on_hours": poh.get("hours"),
        "power_cycles": payload.get("power_cycle_count"),
        "reallocated_sectors": _ata_attr_raw(payload, 5),
        "reported_uncorrectable": _ata_attr_raw(payload, 187),
        "command_timeouts": _ata_attr_raw(payload, 188),
        "current_pending_sectors": _ata_attr_raw(payload, 197),
        "offline_uncorrectable": _ata_attr_raw(payload, 198),
        "udma_crc_errors": _ata_attr_raw(payload, 199),
        "ata_error_count": to_int(error_summary.get("count")),
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
    snapshot.capabilities["ata_smart"] = bool(snapshot.smart or smart_status)
    if error_log:
        snapshot.error_log = error_log
        snapshot.capabilities["ata_error_log"] = True
    selftest = payload.get("ata_smart_self_test_log")
    if isinstance(selftest, dict):
        snapshot.tools["ata_smart_self_test_log"] = selftest
        snapshot.capabilities["ata_self_test_log"] = True
    if payload.get("interface_speed") is not None or payload.get("sata_version") is not None:
        snapshot.capabilities["sata_link"] = True

def _smartctl_json_is_nvme(payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False
    device = payload.get("device") if isinstance(payload.get("device"), dict) else {}
    protocol = str(device.get("protocol") or "").strip().lower()
    dev_type = str(device.get("type") or "").strip().lower()
    return (
        protocol == "nvme"
        or "nvme" in dev_type
        or payload.get("nvme_version") is not None
        or isinstance(payload.get("nvme_smart_health_information_log"), dict)
    )


def _smartctl_identity(payload: Dict[str, Any]) -> Dict[str, Any]:
    device = payload.get("device") if isinstance(payload.get("device"), dict) else {}
    return {
        "model": payload.get("model_name") or payload.get("model_number"),
        "serial": payload.get("serial_number"),
        "firmware": payload.get("firmware_version"),
        "nvme_version": payload.get("nvme_version"),
        "smartctl_device_type": device.get("type"),
        "protocol": device.get("protocol"),
    }


def _smartctl_scan_candidates_linux(runner: Runner) -> Dict[str, Dict[str, Any]]:
    """Return block devices mentioned by smartctl scans.

    Plain --scan can reveal devices that --scan-open cannot open for the
    current user, so merge both.  Device type/comment are retained because an
    SNT/NVMe hint is useful even when a subsequent information probe needs root.
    """
    out: Dict[str, Dict[str, Any]] = {}
    for argv in (["smartctl", "--scan-open"], ["smartctl", "--scan"]):
        res = runner.run(argv, timeout=12.0)
        if not res.available or not res.stdout:
            continue
        for raw in res.stdout.splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            match = re.match(r"^(\S+)(?:\s+-d\s+(\S+))?(?:\s+#\s*(.*))?$", line)
            if not match:
                continue
            device, dev_type, comment = match.groups()
            name = Path(device).name
            if not re.fullmatch(r"sd[a-z]+", name):
                continue
            row = out.setdefault(device, {"device": device})
            if dev_type:
                row["device_type"] = dev_type
            if comment:
                row["comment"] = comment
    return out



def _smartctl_args_linux(device: str, *, mode: str, device_type: Optional[str] = None) -> List[str]:
    """Build one smartctl command while preserving a scan-discovered bridge backend."""
    argv = ["smartctl"]
    if device_type:
        argv += ["-d", str(device_type)]
    argv += [mode, "-j", device]
    return argv


def _smartctl_candidate_linux(runner: Runner, device: str) -> Dict[str, Any]:
    return dict(_smartctl_scan_candidates_linux(runner).get(device) or {})


def _driver_name(path: Path) -> Optional[str]:
    try:
        return (path / "driver").resolve(strict=True).name
    except OSError:
        return None


def _collect_usb_path_linux(controller: str, sys_class_block: Path = SYS_CLASS_BLOCK) -> Dict[str, Any]:
    """Describe the host-visible USB/SCSI path for a translated block disk.

    The underlying NVMe PCIe endpoint remains hidden by the bridge, but Linux
    sysfs exposes enough USB/SCSI ancestry to correlate cable/port/UAS resets.
    """
    block = sys_class_block / controller
    try:
        leaf = (block / "device").resolve(strict=True)
    except OSError:
        return {}

    chain = [leaf, *leaf.parents]
    usb_dev = None
    usb_iface = None
    scsi_host = None
    scsi_target = None
    scsi_lun = None
    ata_port = None
    pci_host = None

    for node in chain:
        name = node.name
        if scsi_host is None and re.fullmatch(r"host\d+", name):
            scsi_host = name
        if scsi_target is None and re.fullmatch(r"target\d+:\d+:\d+", name):
            scsi_target = name
        if scsi_lun is None and re.fullmatch(r"\d+:\d+:\d+:\d+", name):
            scsi_lun = name
        if ata_port is None and re.fullmatch(r"ata\d+", name):
            ata_port = name
        if usb_iface is None and re.fullmatch(r"\d+-[\d.]+:\d+\.\d+", name):
            usb_iface = node
        if usb_dev is None and (node / "idVendor").exists() and (node / "idProduct").exists():
            usb_dev = node
        if pci_host is None and re.fullmatch(r"(?:[0-9a-fA-F]{4}:)?[0-9a-fA-F]{2}:[0-9a-fA-F]{2}\.[0-7]", name):
            pci_host = node

    if usb_dev is None:
        tokens = [controller, ata_port, scsi_host, scsi_target, scsi_lun]
        return {
            "sysfs_device": str(leaf),
            "ata_port": ata_port,
            "scsi_host": scsi_host,
            "scsi_target": scsi_target,
            "scsi_lun": scsi_lun,
            "kernel_tokens": [str(x) for x in tokens if x],
        }

    speed_raw = read_text(usb_dev / "speed")
    speed_mbps: Any = speed_raw
    if speed_raw is not None:
        try:
            speed_num = float(speed_raw)
            speed_mbps = int(speed_num) if speed_num.is_integer() else speed_num
        except ValueError:
            pass

    iface_driver = _driver_name(usb_iface) if usb_iface else None
    host_driver = _driver_name(pci_host) if pci_host else None
    vid = read_text(usb_dev / "idVendor")
    pid = read_text(usb_dev / "idProduct")
    port = usb_dev.name

    tokens = [controller, port, ata_port, scsi_host, scsi_target, scsi_lun]
    if vid and pid:
        tokens += [f"{vid}:{pid}", vid, pid]

    return {
        "sysfs_device": str(leaf),
        "usb_port": port,
        "vendor_id": vid,
        "product_id": pid,
        "vid_pid": f"{vid}:{pid}" if vid and pid else None,
        "manufacturer": read_text(usb_dev / "manufacturer"),
        "product": read_text(usb_dev / "product"),
        "serial": read_text(usb_dev / "serial"),
        "usb_version": read_text(usb_dev / "version"),
        "device_release": read_text(usb_dev / "bcdDevice"),
        "speed_mbps": speed_mbps,
        "busnum": read_text(usb_dev / "busnum"),
        "devnum": read_text(usb_dev / "devnum"),
        "interface_driver": iface_driver,
        "ata_port": ata_port,
        "scsi_host": scsi_host,
        "scsi_target": scsi_target,
        "scsi_lun": scsi_lun,
        "host_controller_bdf": pci_host.name if pci_host else None,
        "host_controller_driver": host_driver,
        "kernel_tokens": [str(x) for x in tokens if x],
    }


def _parse_udevadm_properties(text: str) -> Dict[str, str]:
    props: Dict[str, str] = {}
    for line in text.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        props[key.strip()] = value.strip()
    return props


def _udev_block_identity_linux(runner: Runner, device: str) -> Dict[str, Optional[str]]:
    """Read non-SMART block identity from udev.

    This is deliberately an inventory fallback, not a health source.  Native
    SATA disks can be fully present even when smartctl returns SCSI-flavoured
    or otherwise ambiguous JSON.  udev still normally carries the ATA model,
    serial and firmware revision populated by the kernel/libata stack.
    """
    res = runner.run(["udevadm", "info", "--query=property", f"--name={device}"], timeout=2.0)
    if not res.available or not res.stdout.strip():
        return {}
    props = _parse_udevadm_properties(res.stdout)
    model = props.get("ID_MODEL") or props.get("ID_MODEL_FROM_DATABASE")
    if model:
        model = model.replace("_", " ").strip()
    serial = props.get("ID_SERIAL_SHORT")
    if not serial:
        raw_serial = props.get("ID_SERIAL")
        # ID_SERIAL often has MODEL_SERIAL.  Do not guess-split unless udev
        # also provided a short serial; otherwise keep the complete value.
        serial = raw_serial
    firmware = props.get("ID_REVISION")
    return {
        "model": model or None,
        "serial": serial or None,
        "firmware": firmware or None,
        "bus": props.get("ID_BUS") or None,
    }


def _linux_block_state(candidate: Dict[str, Any]) -> str:
    """Normalize Linux block/SCSI state to nvme-doctor availability state."""
    raw = str(candidate.get("sysfs_state") or "").strip().lower()
    if raw in {"running", "live", "active"}:
        return "live"
    if raw in {"offline", "blocked", "quiesce", "transport-offline"}:
        return raw
    # An OS-visible whole block device is usable inventory-wise even when a
    # particular kernel driver does not export device/state.
    return "live" if candidate.get("sysfs_block") else "present"


def _fallback_block_identity_linux(runner: Runner, device: str, candidate: Dict[str, Any]) -> Dict[str, Optional[str]]:
    """Merge non-SMART identity sources for list/topology output."""
    udev = _udev_block_identity_linux(runner, device)
    return {
        "model": udev.get("model") or candidate.get("sysfs_model") or candidate.get("bridge_model"),
        "serial": udev.get("serial") or candidate.get("sysfs_serial"),
        "firmware": udev.get("firmware") or candidate.get("sysfs_firmware"),
    }


def _physical_block_candidates_linux(sys_class_block: Path = SYS_CLASS_BLOCK) -> Dict[str, Dict[str, Any]]:
    """Enumerate host-visible SCSI-style whole disks independently of smartctl.

    `smartctl --scan` is useful for backend hints, but it is not a reliable
    device inventory: permission/backend failures can omit perfectly visible
    SATA disks.  Start from Linux sysfs instead, then let smartctl classify the
    protocol.  SCSI peripheral type 0 is a direct-access block disk; optical
    and other non-disk sdX devices are excluded.
    """
    out: Dict[str, Dict[str, Any]] = {}
    if not sys_class_block.exists():
        return out
    for path in sorted(sys_class_block.glob("sd*")):
        if not re.fullmatch(r"sd[a-z]+", path.name):
            continue
        peripheral_type = read_text(path / "device" / "type")
        if peripheral_type not in (None, "0"):
            continue
        try:
            real_device = (path / "device").resolve(strict=True)
        except OSError:
            real_device = None
        model = read_text(path / "device" / "model")
        vendor = read_text(path / "device" / "vendor")
        revision = read_text(path / "device" / "rev")
        serial = read_text(path / "device" / "serial")
        state = read_text(path / "device" / "state")
        label = " ".join(x for x in (vendor, model) if x).strip() or None
        real_text = str(real_device).lower() if real_device else ""
        is_usb = bool(real_device and "usb" in real_text)
        # Linux libata exposes native SATA disks through a SCSI-compatible
        # sdX block device, but the resolved sysfs path still contains an
        # ataN component.  Treat that path as authoritative transport evidence
        # when smartctl's generic identity probe is ambiguous.
        is_ata = bool(real_device and any(re.fullmatch(r"ata\d+", part.lower()) for part in real_device.parts))
        sectors = to_int(read_text(path / "size"))
        out[f"/dev/{path.name}"] = {
            "device": f"/dev/{path.name}",
            "bridge_model": label,
            "sysfs_model": model,
            "sysfs_serial": serial,
            "sysfs_firmware": revision,
            "sysfs_state": state,
            "sysfs_block": True,
            "sysfs_usb": is_usb,
            "sysfs_transport": "SATA" if is_ata and not is_usb else ("USB" if is_usb else "SCSI"),
            "rotational": read_text(path / "queue" / "rotational"),
            "size_bytes": sectors * 512 if sectors is not None else None,
        }
    return out


def _usb_solid_state_candidates_linux(sys_class_block: Path = SYS_CLASS_BLOCK) -> Dict[str, Dict[str, Any]]:
    """Find USB whole-disk sdX devices that look solid-state from sysfs.

    This is deliberately a *candidate* source.  USB bridges hide the underlying
    protocol, so NVMe identity is confirmed by smartctl when permissions allow.
    Keeping candidates lets an unprivileged `list` at least show that a physical
    external SSD exists and needs a privileged protocol probe.
    """
    out: Dict[str, Dict[str, Any]] = {}
    if not sys_class_block.exists():
        return out
    for path in sorted(sys_class_block.glob("sd*")):
        if not re.fullmatch(r"sd[a-z]+", path.name):
            continue
        try:
            real_device = (path / "device").resolve(strict=True)
        except OSError:
            continue
        if "usb" not in str(real_device).lower():
            continue
        rotational = read_text(path / "queue" / "rotational")
        if rotational not in (None, "0"):
            continue
        model = read_text(path / "device" / "model")
        vendor = read_text(path / "device" / "vendor")
        label = " ".join(x for x in (vendor, model) if x).strip() or None
        out[f"/dev/{path.name}"] = {
            "device": f"/dev/{path.name}",
            "bridge_model": label,
            "sysfs_usb": True,
        }
    return out


def discover_controllers_linux(
    sys_class_nvme: Path = SYS_CLASS_NVME,
    runner: Optional[Runner] = None,
    sys_class_block: Path = SYS_CLASS_BLOCK,
) -> List[Dict[str, Any]]:
    """Discover native NVMe plus ATA/SATA and USB-translated storage devices."""
    runner = runner or Runner()
    result = _discover_native_controllers_linux(sys_class_nvme, sys_class_block)
    seen = {str(item.get("device")) for item in result}

    candidates = _smartctl_scan_candidates_linux(runner)
    # OS inventory is authoritative for presence. smartctl scan output only
    # enriches it with backend/protocol hints. This keeps native SATA disks in
    # `list` even when smartctl --scan omits them.
    for device, row in _physical_block_candidates_linux(sys_class_block).items():
        candidates.setdefault(device, {}).update({k: v for k, v in row.items() if v is not None})
    for device, row in _usb_solid_state_candidates_linux(sys_class_block).items():
        candidates.setdefault(device, {}).update({k: v for k, v in row.items() if v is not None})

    for device in sorted(candidates):
        if device in seen:
            continue
        candidate = candidates[device]
        scan_hint = " ".join(
            str(candidate.get(key) or "") for key in ("device_type", "comment")
        ).lower()
        likely_nvme = "nvme" in scan_hint or "snt" in scan_hint

        # Identification is intentionally cheap; full SMART is collected only
        # by `check`.  smartctl may return non-zero status bits while still
        # producing valid JSON, so parse stdout regardless of return code.
        device_type = candidate.get("device_type")
        probe = runner.run(_smartctl_args_linux(device, mode="-i", device_type=device_type), timeout=10.0)
        payload = read_json(probe.stdout) if probe.available and probe.stdout.strip() else None

        # Some libata/SATA devices are exposed as sdX and smartctl's generic
        # probe can return valid but SCSI-flavoured JSON.  If sysfs proves this
        # is a native ataN path, retry with the explicit ATA backend before
        # giving up classification.
        if (
            not _smartctl_json_is_nvme(payload)
            and not _smartctl_json_is_ata(payload)
            and candidate.get("sysfs_transport") == "SATA"
            and not device_type
        ):
            ata_probe = runner.run(_smartctl_args_linux(device, mode="-i", device_type="ata"), timeout=6.0)
            ata_payload = read_json(ata_probe.stdout) if ata_probe.available and ata_probe.stdout.strip() else None
            if _smartctl_json_is_ata(ata_payload):
                probe = ata_probe
                payload = ata_payload
                device_type = "ata"

        if _smartctl_json_is_nvme(payload):
            ident = _smartctl_identity(payload)
            result.append({
                "controller": Path(device).name,
                "device": device,
                "model": ident.get("model") or candidate.get("bridge_model"),
                "serial": ident.get("serial"),
                "firmware": ident.get("firmware"),
                "state": "present",
                "transport": "USB -> NVMe" if candidate.get("sysfs_usb") or "usb" in scan_hint else "translated NVMe",
                "smartctl_device_type": ident.get("smartctl_device_type") or candidate.get("device_type"),
                "protocol": "NVMe",
                "native_nvme": False,
                "size_bytes": candidate.get("size_bytes"),
                "usb_path": _collect_usb_path_linux(Path(device).name, sys_class_block),
            })
            continue

        if _smartctl_json_is_ata(payload):
            device_info = payload.get("device") if isinstance(payload.get("device"), dict) else {}
            usb_path = _collect_usb_path_linux(Path(device).name, sys_class_block)
            is_usb = bool(usb_path.get("usb_port"))
            fallback_ident = {}
            if not payload.get("model_name") or not payload.get("serial_number") or not payload.get("firmware_version"):
                fallback_ident = _fallback_block_identity_linux(runner, device, candidate)
            result.append({
                "controller": Path(device).name,
                "device": device,
                "model": payload.get("model_name") or fallback_ident.get("model") or candidate.get("bridge_model"),
                "serial": payload.get("serial_number") or fallback_ident.get("serial"),
                "firmware": payload.get("firmware_version") or fallback_ident.get("firmware"),
                "state": _linux_block_state(candidate),
                "transport": "USB -> SATA" if is_usb else "SATA",
                "smartctl_device_type": device_info.get("type") or candidate.get("device_type"),
                "protocol": "ATA",
                "native_nvme": False,
                "usb_path": usb_path if is_usb else {},
                "rotation_rate": payload.get("rotation_rate"),
                "size_bytes": candidate.get("size_bytes"),
            })
            continue

        # If smartctl's scan already identifies an NVMe/SNT bridge, keep it in
        # the inventory even when the current user cannot open it.
        if likely_nvme:
            result.append({
                "controller": Path(device).name,
                "device": device,
                "model": candidate.get("bridge_model"),
                "serial": None,
                "firmware": None,
                "state": "limited",
                "transport": "USB -> NVMe",
                "smartctl_device_type": candidate.get("device_type"),
                "protocol": "NVMe",
                "native_nvme": False,
                "usb_path": _collect_usb_path_linux(Path(device).name, sys_class_block),
                "probe_note": "NVMe bridge detected; run list/check as root for identity/health access",
                "size_bytes": candidate.get("size_bytes"),
            })
            continue

        # Last-resort visibility: an OS-visible whole disk must never disappear
        # merely because smartctl returned unsupported/ambiguous JSON.  Native
        # libata sysfs paths are strong enough to label the transport SATA even
        # when SMART identity remains unavailable.
        if candidate.get("sysfs_block"):
            is_usb = bool(candidate.get("sysfs_usb"))
            rotational = str(candidate.get("rotational") or "").strip()
            sysfs_transport = candidate.get("sysfs_transport")
            fallback_ident = _fallback_block_identity_linux(runner, device, candidate)
            if sysfs_transport == "SATA":
                transport = "SATA"
                protocol = "ATA"
                state = _linux_block_state(candidate)
                note = "SATA transport identified from Linux libata sysfs; SMART protocol probe was ambiguous"
            elif is_usb:
                transport = "USB disk (protocol unverified)"
                protocol = None
                state = "probe-needed"
                note = "USB disk is present, but its underlying protocol could not be identified from SMART"
            else:
                transport = "SCSI disk (protocol unverified)"
                protocol = None
                state = "probe-needed"
                note = "physical disk is present, but its protocol could not be identified from SMART"
            result.append({
                "controller": Path(device).name,
                "device": device,
                "model": fallback_ident.get("model") or candidate.get("bridge_model"),
                "serial": fallback_ident.get("serial"),
                "firmware": fallback_ident.get("firmware"),
                "state": state,
                "transport": transport,
                "protocol": protocol,
                "native_nvme": False,
                "smartctl_device_type": "ata" if sysfs_transport == "SATA" else candidate.get("device_type"),
                "rotation_rate": None if rotational == "0" else ("rotational" if rotational == "1" else None),
                "usb_path": _collect_usb_path_linux(Path(device).name, sys_class_block) if is_usb else {},
                "probe_note": note,
                "size_bytes": candidate.get("size_bytes"),
            })

    return result


def _read_tool_json(
    runner: Runner,
    argv: List[str],
    notes: List[str],
    *,
    accept_json_on_nonzero: bool = False,
) -> Any:
    """Run a JSON-producing helper.

    Some tools, notably smartctl, use a non-zero exit status as a health/status
    bitmask even when stdout contains complete, valid diagnostic JSON.  In that
    case callers can request that valid JSON is retained.
    """
    res = runner.run(argv)
    label = " ".join(argv[:2])
    if not res.available:
        notes.append(f"optional tool missing: {argv[0]}")
        return None

    parsed = read_json(res.stdout) if res.stdout.strip() else None
    if res.returncode == 0:
        if parsed is None:
            notes.append(f"could not parse JSON from {label}")
        return parsed

    if accept_json_on_nonzero and parsed is not None:
        # For smartctl, bits 0..2 indicate command/device/SMART-command
        # collection failures. Higher bits are health findings and do not make
        # the JSON unusable. Keep the JSON in either case, but call out actual
        # collection trouble.
        if argv and argv[0] == "smartctl" and (res.returncode & 0x07):
            notes.append(
                f"smartctl returned collection-status bits 0x{res.returncode & 0x07:02x}; valid JSON was retained"
            )
        return parsed

    err_lines = (res.stderr or res.stdout).strip().splitlines()
    detail = err_lines[-1][:240] if err_lines else f"exit status {res.returncode}"
    notes.append(f"{label} failed: {detail}")
    return None


def _collect_controller_sysfs(controller_path: Path) -> Dict[str, Any]:
    fields = [
        "model",
        "serial",
        "firmware_rev",
        "state",
        "transport",
        "address",
        "cntlid",
        "queue_count",
        "sqsize",
    ]
    out: Dict[str, Any] = {}
    for field in fields:
        value = read_text(controller_path / field)
        if value is not None:
            out[field] = value
    return out


def _bdf_for_controller(controller_path: Path) -> Optional[str]:
    device = controller_path / "device"
    try:
        real = device.resolve(strict=True)
    except OSError:
        return None
    for part in reversed(real.parts):
        if re.fullmatch(r"[0-9a-fA-F]{4}:[0-9a-fA-F]{2}:[0-9a-fA-F]{2}\.[0-7]", part):
            return part
    return None


def _collect_pci_sysfs(bdf: Optional[str], sys_pci: Path = SYS_PCI) -> Dict[str, Any]:
    if not bdf:
        return {}
    path = sys_pci / bdf
    out: Dict[str, Any] = {"bdf": bdf, "present": path.exists()}
    if not path.exists():
        return out

    for field in [
        "vendor",
        "device",
        "subsystem_vendor",
        "subsystem_device",
        "class",
        "current_link_speed",
        "current_link_width",
        "max_link_speed",
        "max_link_width",
        "numa_node",
        "local_cpulist",
        "power_state",
        "reset_method",
    ]:
        value = read_text(path / field)
        if value is not None:
            out[field] = value

    driver = path / "driver"
    try:
        out["driver"] = driver.resolve(strict=True).name
    except OSError:
        pass

    aer: Dict[str, Any] = {}
    try:
        entries = list(path.iterdir())
    except OSError:
        entries = []
    for entry in entries:
        if not entry.name.startswith("aer_"):
            continue
        text = read_text(entry)
        if text is None:
            continue
        counters = parse_counter_blob(text)
        aer[entry.name] = counters if counters else text
    if aer:
        out["aer"] = aer
    return out


def _collect_power() -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    files = {
        "nvme_default_ps_max_latency_us": Path("/sys/module/nvme_core/parameters/default_ps_max_latency_us"),
        "pcie_aspm_policy": Path("/sys/module/pcie_aspm/parameters/policy"),
    }
    for key, path in files.items():
        value = read_text(path)
        if value is not None:
            out[key] = value
    return out


def _topology_for_bdf(bdf: Optional[str], sys_pci: Path = SYS_PCI) -> List[Dict[str, Any]]:
    if not bdf:
        return []
    path = sys_pci / bdf
    try:
        real = path.resolve(strict=True)
    except OSError:
        return []
    chain: List[Dict[str, Any]] = []
    for parent in [real] + list(real.parents):
        name = parent.name
        if not re.fullmatch(r"[0-9a-fA-F]{4}:[0-9a-fA-F]{2}:[0-9a-fA-F]{2}\.[0-7]", name):
            continue
        node: Dict[str, Any] = {"bdf": name}
        for field in ["class", "vendor", "device", "current_link_speed", "current_link_width", "max_link_speed", "max_link_width", "numa_node", "local_cpulist", "power_state"]:
            value = read_text(parent / field)
            if value is not None:
                node[field] = value
        try:
            node["driver"] = (parent / "driver").resolve(strict=True).name
        except OSError:
            pass
        chain.append(node)
    return chain



def _pci_class_role(class_code: Any, *, endpoint: bool = False) -> str:
    if endpoint:
        return "NVMe controller"
    text = str(class_code or "").strip().lower()
    if text.startswith("0x"):
        text = text[2:]
    text = text.zfill(6)
    if text.startswith("0604"):
        return "PCIe bridge / port"
    if text.startswith("0600"):
        return "PCI host bridge"
    if text.startswith("0108"):
        return "NVMe controller"
    if text.startswith("060"):
        return "PCI bridge"
    return "PCI device"


def _lspci_description(runner: Runner, bdf: str) -> Optional[str]:
    res = runner.run(["lspci", "-D", "-s", bdf], timeout=5.0)
    if not res.available or res.returncode != 0 or not res.stdout.strip():
        return None
    line = res.stdout.strip().splitlines()[0].strip()
    # Strip the leading domain:bus:slot.func while retaining the standard
    # lspci class + vendor/device description.
    parts = line.split(None, 1)
    return parts[1].strip() if len(parts) == 2 else line


def _collect_namespaces(
    controller: str,
    controller_path: Path,
    sys_class_block: Path = SYS_CLASS_BLOCK,
) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    candidates: Dict[str, Path] = {}
    try:
        for child in controller_path.glob(f"{controller}n*"):
            if re.fullmatch(rf"{re.escape(controller)}n\d+", child.name):
                candidates[child.name] = child
    except OSError:
        pass
    try:
        for child in sys_class_block.glob(f"{controller}n*"):
            if re.fullmatch(rf"{re.escape(controller)}n\d+", child.name):
                candidates.setdefault(child.name, child)
    except OSError:
        pass

    for name in sorted(candidates, key=lambda x: int(x.rsplit("n", 1)[1])):
        ns_path = candidates[name]
        block_path = sys_class_block / name
        row: Dict[str, Any] = {"name": name, "device": f"/dev/{name}"}
        nsid = read_text(ns_path / "nsid") or read_text(block_path / "nsid")
        if nsid is not None:
            row["nsid"] = nsid
        sectors = read_text(block_path / "size")
        logical = read_text(block_path / "queue" / "logical_block_size")
        try:
            if sectors is not None:
                # Linux block-layer 'size' is always expressed in 512-byte sectors.
                row["capacity_bytes"] = int(sectors) * 512
        except ValueError:
            pass
        if logical is not None:
            try:
                row["logical_block_size"] = int(logical)
            except ValueError:
                row["logical_block_size"] = logical
        rows.append(row)
    return rows


def collect_topology_linux(
    requested_device: str,
    runner: Optional[Runner] = None,
    sys_class_nvme: Path = SYS_CLASS_NVME,
    sys_pci: Path = SYS_PCI,
    sys_class_block: Path = SYS_CLASS_BLOCK,
) -> Dict[str, Any]:
    """Collect the hardware path from NUMA locality to an NVMe namespace.

    This command is intentionally independent of SMART/admin-log collection, so
    it normally works without root and remains useful on a degraded drive.
    """
    runner = runner or Runner()
    controller, device_path = normalize_device(requested_device)
    if not _is_native_linux_nvme(controller):
        block = sys_class_block / controller
        bridge_candidate = _usb_solid_state_candidates_linux(sys_class_block).get(device_path, {})
        scan_candidate = _smartctl_candidate_linux(runner, device_path)
        device_type = scan_candidate.get("device_type")
        usb_path = _collect_usb_path_linux(controller, sys_class_block)
        ci: Dict[str, Any] = {
            "state": "present" if block.exists() else "missing",
            "transport": "USB/SCSI block device",
            "native_nvme": False,
            "usb_bridge_model": bridge_candidate.get("bridge_model"),
            "smartctl_device_type": device_type,
            "usb_path": usb_path,
        }
        probe = runner.run(_smartctl_args_linux(device_path, mode="-i", device_type=device_type), timeout=4.0)
        payload = read_json(probe.stdout) if probe.available and probe.stdout.strip() else None
        if _smartctl_json_is_nvme(payload):
            ident = _smartctl_identity(payload)
            ci.update({
                "model": ident.get("model"),
                "serial": ident.get("serial"),
                "firmware_rev": ident.get("firmware"),
                "nvme_version": ident.get("nvme_version"),
                "protocol": "NVMe",
                "transport": "USB -> NVMe" if usb_path.get("usb_port") else "translated NVMe",
                "identity_source": "smartctl",
            })
        elif _smartctl_json_is_ata(payload):
            ci.update({
                "model": payload.get("model_name"),
                "serial": payload.get("serial_number"),
                "firmware_rev": payload.get("firmware_version"),
                "protocol": "ATA",
                "transport": "USB -> SATA" if usb_path.get("usb_port") else "SATA",
                "sata_version": payload.get("sata_version"),
                "interface_speed": payload.get("interface_speed"),
                "identity_source": "smartctl",
            })
        else:
            ci["identity_source"] = "unavailable"
            if probe.available and probe.returncode not in (0, None):
                ci["identity_note"] = "storage identity was not readable; rerun topology with sudo"
        ns: Dict[str, Any] = {"name": controller, "device": device_path}
        sectors = read_text(block / "size")
        logical = read_text(block / "queue" / "logical_block_size")
        try:
            if sectors is not None:
                ns["capacity_bytes"] = int(sectors) * 512
        except ValueError:
            pass
        try:
            if logical is not None:
                ns["logical_block_size"] = int(logical)
        except ValueError:
            pass
        if ci.get("protocol") == "ATA":
            notes = [
                "The ATA/SATA drive is exposed as a Linux block device. SMART identity/health and SATA interface speed are available through smartctl; USB bridges may hide the native SATA host-controller path."
            ]
        else:
            notes = [
                "The SSD is accessed through a USB/SCSI bridge. Its native NVMe PCIe endpoint, PCIe generation/link, AER and NUMA ancestry are hidden behind the bridge; the host USB/block path and underlying NVMe identity are shown when available."
            ]
        if ci.get("identity_note"):
            notes.append(str(ci.get("identity_note")))
        return {
            "platform": "linux",
            "requested_device": requested_device,
            "controller": controller,
            "controller_device": device_path,
            "controller_info": ci,
            "numa_node": None,
            "local_cpulist": None,
            "pci_domain": None,
            "pci_endpoint": {},
            "pci_path": [],
            "namespaces": [ns],
            "complete": False,
            "notes": notes,
        }

    controller_path = sys_class_nvme / controller
    if not controller_path.exists():
        raise ValueError(f"{controller_path} is not present; controller may be disconnected or removed")

    ci = _collect_controller_sysfs(controller_path)
    bdf = _bdf_for_controller(controller_path)
    endpoint = _collect_pci_sysfs(bdf, sys_pci)
    raw_chain = _topology_for_bdf(bdf, sys_pci)
    # _topology_for_bdf walks endpoint -> parents; humans read topology root -> leaf.
    chain: List[Dict[str, Any]] = []
    for raw in reversed(raw_chain):
        node = dict(raw)
        node_bdf = str(node.get("bdf") or "")
        node["role"] = _pci_class_role(node.get("class"), endpoint=(node_bdf == bdf))
        desc = _lspci_description(runner, node_bdf) if node_bdf else None
        if desc:
            node["description"] = desc
        chain.append(node)

    numa_node = endpoint.get("numa_node")
    if numa_node in (None, "-1"):
        for node in reversed(chain):
            if node.get("numa_node") not in (None, "-1"):
                numa_node = node.get("numa_node")
                break
    local_cpus = endpoint.get("local_cpulist")
    if not local_cpus:
        for node in reversed(chain):
            if node.get("local_cpulist"):
                local_cpus = node.get("local_cpulist")
                break

    return {
        "platform": "linux",
        "requested_device": requested_device,
        "controller": controller,
        "controller_device": device_path,
        "controller_info": ci,
        "numa_node": numa_node,
        "local_cpulist": local_cpus,
        "pci_domain": bdf.split(":", 1)[0] if bdf else None,
        "pci_endpoint": endpoint,
        "pci_path": chain,
        "namespaces": _collect_namespaces(controller, controller_path, sys_class_block),
        "complete": bool(bdf and chain),
        "notes": [] if bdf else ["PCI BDF could not be resolved from NVMe sysfs"],
    }


def collect_topology(
    requested_device: str,
    runner: Optional[Runner] = None,
    sys_class_nvme: Path = SYS_CLASS_NVME,
    sys_pci: Path = SYS_PCI,
    sys_class_block: Path = SYS_CLASS_BLOCK,
    platform_name: Optional[str] = None,
    direct_usb: bool = False,
) -> Dict[str, Any]:
    key = platform_key(platform_name)
    if key == "linux":
        return collect_topology_linux(
            requested_device, runner=runner, sys_class_nvme=sys_class_nvme,
            sys_pci=sys_pci, sys_class_block=sys_class_block
        )
    if key == "darwin":
        from .collect_macos import collect_snapshot_macos
        snapshot = collect_snapshot_macos(
            requested_device, runner=runner, kernel_lines=0, direct_usb=direct_usb
        )
        ci = dict(snapshot.controller_info)
        bridge_model = ci.get("usb_bridge_model")
        # Without direct access, diskutil's model is the enclosure-facing SCSI
        # identity.  Do not mislabel it as the underlying NVMe model.
        if ci.get("direct_usb") and not ci.get("nvme_passthrough"):
            if bridge_model and ci.get("model") == bridge_model:
                ci["model"] = None
                ci["serial"] = None
                ci["firmware_rev"] = None
        return {
            "platform": "darwin",
            "requested_device": requested_device,
            "controller": snapshot.controller,
            "controller_device": snapshot.device_path,
            "controller_info": ci,
            "numa_node": None,
            "local_cpulist": None,
            "pci_domain": None,
            "pci_endpoint": {},
            "pci_path": [],
            "namespaces": [{
                "name": snapshot.controller,
                "device": snapshot.device_path,
                "capacity_bytes": ci.get("tnvmcap") or ci.get("size_in_bytes"),
            }],
            "complete": False,
            "notes": [
                "This SSD is accessed through a USB-to-NVMe bridge. macOS can show the USB/block endpoint, and RTL9210 direct mode can identify the underlying NVMe controller, but the SSD's native PCIe/NUMA/AER ancestry remains hidden behind USB."
            ],
        }
    raise ValueError(f"unsupported operating system: {key}")

def _kernel_log(
    runner: Runner,
    controller: str,
    bdfs: List[str],
    max_lines: int,
    extra_tokens: Optional[List[str]] = None,
) -> List[str]:
    candidates = [
        ["journalctl", "-k", "-b", "--no-pager", "-o", "short-monotonic"],
        ["dmesg", "--color=never", "--ctime"],
        ["dmesg", "--color=never"],
    ]
    text = ""
    for argv in candidates:
        res = runner.run(argv, timeout=12.0)
        if res.available and res.returncode == 0 and res.stdout:
            text = res.stdout
            break
    if not text:
        return []

    controller_re = re.compile(rf"\b{re.escape(controller)}(?:n\d+)?\b", re.I)
    bdf_tokens = set()
    for item in bdfs:
        low = item.lower()
        bdf_tokens.add(low)
        if low.startswith("0000:"):
            bdf_tokens.add(low[5:])

    token_set = {str(x).strip().lower() for x in (extra_tokens or []) if str(x).strip()}
    selected: List[str] = []
    for line in text.splitlines():
        lower = line.lower()
        if controller_re.search(line):
            selected.append(line)
            continue
        if any(token in lower for token in bdf_tokens):
            selected.append(line)
            continue
        if any(token in lower for token in token_set):
            selected.append(line)
    return selected[-max_lines:]




def _apply_smartctl_nvme_payload(snapshot: Snapshot, payload: Dict[str, Any], *, translated: bool = False) -> None:
    """Merge smartctl NVMe JSON into a Snapshot."""
    snapshot.tools["smartctl"] = payload
    ident = _smartctl_identity(payload)
    ci = snapshot.controller_info
    if ident.get("model"):
        ci["model"] = ident["model"]
    if ident.get("serial"):
        ci["serial"] = ident["serial"]
    if ident.get("firmware"):
        ci["firmware_rev"] = ident["firmware"]
    if ident.get("nvme_version") is not None:
        ci["nvme_version"] = ident["nvme_version"]
    if ident.get("smartctl_device_type"):
        ci["smartctl_device_type"] = ident["smartctl_device_type"]
    if ident.get("protocol"):
        ci["protocol"] = ident["protocol"]
    if translated:
        ci["state"] = "present"
        ci["transport"] = "USB -> NVMe"
        ci["native_nvme"] = False
        ci["nvme_passthrough"] = True

    fallback = payload.get("nvme_smart_health_information_log")
    if isinstance(fallback, dict):
        snapshot.smart = fallback
        snapshot.capabilities["nvme_smart"] = True

    for key in ("nvme_error_information_log", "nvme_error_log"):
        if payload.get(key) is not None:
            snapshot.error_log = payload.get(key)
            snapshot.capabilities["nvme_error_log"] = True
            break


def _is_native_linux_nvme(controller: str) -> bool:
    return bool(re.fullmatch(r"nvme\d+", controller))

def collect_snapshot_linux(
    requested_device: str,
    runner: Optional[Runner] = None,
    sys_class_nvme: Path = SYS_CLASS_NVME,
    sys_pci: Path = SYS_PCI,
    kernel_lines: int = 300,
    sys_class_block: Path = SYS_CLASS_BLOCK,
    progress: Optional[Callable[[str], None]] = None,
) -> Snapshot:
    runner = runner or Runner()
    controller, device_path = normalize_device(requested_device)
    native_nvme = _is_native_linux_nvme(controller)
    snapshot = Snapshot(requested_device, controller, device_path)
    snapshot.host = {
        "platform": "linux",
        "platform_label": "Linux",
        "hostname": platform.node(),
        "kernel": platform.release(),
        "machine": platform.machine(),
        "python": platform.python_version(),
        "euid": os.geteuid() if hasattr(os, "geteuid") else None,
        "os_release": _os_release(),
    }
    if progress:
        progress(f"Inspecting {device_path} and its storage transport")

    snapshot.capabilities = {
        "nvme_smart": False,
        "nvme_error_log": False,
        "ata_smart": False,
        "ata_error_log": False,
        "ata_self_test_log": False,
        "sata_link": False,
        "device_inventory": True,
        "pcie_link": native_nvme,
        "pcie_aer": native_nvme,
        "pcie_topology": native_nvme,
        "power_management": native_nvme,
        "targeted_kernel_log": True,
    }

    if not native_nvme:
        # USB/SCSI-translated NVMe appears as /dev/sdX. nvme-cli and native
        # NVMe sysfs cannot address that path, but smartctl can on Linux when
        # the bridge supports NVMe admin passthrough (as proven by smartctl -a).
        if progress:
            progress("Detecting block protocol / bridge backend")
        scan_candidate = _smartctl_candidate_linux(runner, device_path)
        physical_candidate = _physical_block_candidates_linux(sys_class_block).get(device_path, {})
        candidate = dict(physical_candidate)
        candidate.update(scan_candidate)
        sysfs_transport = candidate.get("sysfs_transport")
        fallback_ident = _fallback_block_identity_linux(runner, device_path, candidate)

        # `list` and `check` must resolve the same device identity/state.  Do
        # this before SMART collection so a health-backend failure cannot erase
        # model/serial/firmware or downgrade a running SATA disk to `present`.
        if progress:
            progress("Reading USB/SCSI path from sysfs")
        usb_path = _collect_usb_path_linux(controller, sys_class_block)
        is_usb = bool(isinstance(usb_path, dict) and usb_path.get("usb_port"))
        initial_transport = "SATA" if sysfs_transport == "SATA" else ("USB disk" if candidate.get("sysfs_usb") else "SCSI disk")
        scan_type = candidate.get("device_type")
        preferred_type = "ata" if sysfs_transport == "SATA" else scan_type
        snapshot.controller_info.update({
            "state": _linux_block_state(candidate),
            "sysfs_present": (sys_class_block / controller).exists(),
            "transport": initial_transport,
            "protocol": "ATA" if sysfs_transport == "SATA" else None,
            "native_nvme": False,
            "smartctl_device_type": preferred_type,
            "usb_path": usb_path if is_usb else {},
            "model": fallback_ident.get("model"),
            "serial": fallback_ident.get("serial"),
            "firmware_rev": fallback_ident.get("firmware"),
        })

        if progress:
            progress("Reading SMART identity and health through smartctl")

        # Native libata devices are sometimes reported as generic SCSI by a
        # smartctl auto/scan probe.  Try a small ordered backend ladder and keep
        # the payload with the strongest ATA evidence instead of letting one
        # ambiguous scan hint (for example `-d scsi`) suppress ATA SMART.
        backend_types = []
        if sysfs_transport == "SATA":
            for dtype in ("ata", None, "sat"):
                if dtype not in backend_types:
                    backend_types.append(dtype)
        else:
            backend_types.append(scan_type)

        smartctl = None
        selected_type = preferred_type
        best_score = -1
        selected_rc = None
        for dtype in backend_types:
            res = runner.run(_smartctl_args_linux(device_path, mode="-a", device_type=dtype), timeout=8.0)
            payload = read_json(res.stdout) if res.available and res.stdout.strip() else None
            if not isinstance(payload, dict):
                continue
            score = 0
            if _smartctl_json_is_nvme(payload):
                score = 1000
            elif _smartctl_json_is_ata(payload):
                score = 500
                if isinstance(payload.get("smart_status"), dict):
                    score += 100
                if isinstance(payload.get("ata_smart_attributes"), dict):
                    score += 200
                if isinstance(payload.get("ata_smart_data"), dict):
                    score += 100
                if isinstance(payload.get("ata_smart_error_log"), dict):
                    score += 25
            else:
                # Still preserve useful identity from an ambiguous response.
                score = sum(bool(payload.get(k)) for k in ("model_name", "model_number", "serial_number", "firmware_version"))
            if score > best_score:
                smartctl = payload
                selected_type = dtype
                selected_rc = res.returncode
                best_score = score
            # Full ATA health evidence is enough; do not add needless probes.
            if score >= 700:
                break

        if selected_type:
            snapshot.controller_info["smartctl_device_type"] = selected_type
        if selected_rc is not None and (selected_rc & 0x07):
            snapshot.collection_notes.append(
                f"smartctl returned collection-status bits 0x{selected_rc & 0x07:02x}; valid JSON was retained"
            )

        if isinstance(smartctl, dict) and _smartctl_json_is_nvme(smartctl):
            _apply_smartctl_nvme_payload(snapshot, smartctl, translated=True)
            snapshot.collection_notes.append(
                "NVMe is accessed through a USB/SCSI block device; native nvme-cli, PCIe AER/link, NUMA and endpoint topology are hidden by the bridge"
            )
        elif isinstance(smartctl, dict) and _smartctl_json_is_ata(smartctl):
            is_usb = bool(isinstance(usb_path, dict) and usb_path.get("usb_port"))
            _apply_smartctl_ata_payload(snapshot, smartctl, usb=is_usb)
            snapshot.controller_info["state"] = _linux_block_state(candidate)
            snapshot.controller_info["model"] = snapshot.controller_info.get("model") or fallback_ident.get("model")
            snapshot.controller_info["serial"] = snapshot.controller_info.get("serial") or fallback_ident.get("serial")
            snapshot.controller_info["firmware_rev"] = snapshot.controller_info.get("firmware_rev") or fallback_ident.get("firmware")
            snapshot.controller_info["usb_path"] = usb_path if is_usb else {}
            if is_usb:
                snapshot.collection_notes.append(
                    "SATA/ATA SMART is accessed through a USB-SATA/SAT bridge; native SATA host-controller details may be hidden by the enclosure"
                )
            else:
                snapshot.collection_notes.append("ATA/SATA health collected through smartctl")
        elif isinstance(smartctl, dict):
            snapshot.tools["smartctl"] = smartctl
            if sysfs_transport == "SATA":
                snapshot.controller_info["protocol"] = "ATA"
                snapshot.controller_info["transport"] = "SATA"
                snapshot.collection_notes.append(
                    f"{device_path} is on a Linux libata SATA path, but smartctl did not expose ATA SMART with the available backend"
                )
            else:
                snapshot.controller_info["protocol"] = _smartctl_json_protocol(smartctl) or "unknown"
                snapshot.collection_notes.append(
                    f"{device_path} is a block device, but smartctl did not identify a supported NVMe or ATA/SATA protocol"
                )
        else:
            snapshot.controller_info["passthrough_reason"] = "linux-smartctl-unavailable"

        # Keep kernel evidence scoped to the block-device name.  This can catch
        # USB/SCSI resets/disconnects even though the native NVMe PCIe BDF is
        # intentionally unavailable through the bridge.
        if progress:
            progress("Collecting target-scoped OS/kernel events")
        snapshot.kernel_lines = _kernel_log(
            runner,
            controller,
            [],
            kernel_lines,
            extra_tokens=(usb_path.get("kernel_tokens") if isinstance(usb_path, dict) else None),
        )
        snapshot.collection_notes = list(dict.fromkeys(snapshot.collection_notes))
        if progress:
            progress("Evidence collection complete")
        return snapshot

    if progress:
        progress("Reading native NVMe sysfs and PCIe topology")
    controller_path = sys_class_nvme / controller
    if controller_path.exists():
        snapshot.controller_info.update(_collect_controller_sysfs(controller_path))
        snapshot.controller_info["native_nvme"] = True
        snapshot.controller_info["protocol"] = "NVMe"
    else:
        snapshot.controller_info.update({"state": "missing", "sysfs_present": False, "native_nvme": True})
        snapshot.collection_notes.append(f"{controller_path} is not present; controller may be disconnected or removed")

    bdf = _bdf_for_controller(controller_path) if controller_path.exists() else None
    snapshot.pci = _collect_pci_sysfs(bdf, sys_pci)
    snapshot.topology = _topology_for_bdf(bdf, sys_pci)
    snapshot.power = _collect_power()

    # nvme-cli is the preferred source for standards-defined controller data.
    if progress:
        progress("Reading NVMe SMART / Health log")
    smart = _read_tool_json(runner, ["nvme", "smart-log", device_path, "-o", "json"], snapshot.collection_notes)
    if isinstance(smart, dict):
        snapshot.smart = smart
        snapshot.capabilities["nvme_smart"] = True

    if progress:
        progress("Reading NVMe Identify Controller")
    ctrl = _read_tool_json(runner, ["nvme", "id-ctrl", device_path, "-o", "json"], snapshot.collection_notes)
    if isinstance(ctrl, dict):
        snapshot.controller_info["nvme_id_ctrl"] = ctrl
        if ctrl.get("ver") is not None:
            snapshot.controller_info["nvme_version_raw"] = ctrl.get("ver")
        for key in ("tnvmcap", "unvmcap", "nn", "vwc"):
            if ctrl.get(key) is not None:
                snapshot.controller_info[key] = ctrl.get(key)

    if progress:
        progress("Reading NVMe Error Information log")
    errors = _read_tool_json(
        runner,
        ["nvme", "error-log", device_path, "-e", "64", "-o", "json"],
        snapshot.collection_notes,
    )
    if errors is not None:
        snapshot.error_log = errors
        snapshot.capabilities["nvme_error_log"] = True

    # smartctl is supplemental for native NVMe and fallback health evidence.
    if progress:
        progress("Reading supplemental SMART/identity data")
    smartctl = _read_tool_json(
        runner,
        ["smartctl", "-a", "-j", device_path],
        snapshot.collection_notes,
        accept_json_on_nonzero=True,
    )
    if isinstance(smartctl, dict):
        # Keep nvme-cli SMART authoritative when available, but use smartctl to
        # enrich identity/version and as the fallback health source.
        existing_smart = snapshot.smart
        _apply_smartctl_nvme_payload(snapshot, smartctl, translated=False)
        if existing_smart:
            snapshot.smart = existing_smart
            snapshot.capabilities["nvme_smart"] = True
        elif snapshot.smart:
            snapshot.collection_notes.append(
                "using smartctl NVMe health JSON as fallback because nvme-cli SMART data was unavailable"
            )

    if bdf:
        if progress:
            progress("Reading detailed PCIe link/controller data")
        lspci = runner.run(["lspci", "-vv", "-s", bdf], timeout=8.0)
        if lspci.available and lspci.returncode == 0:
            snapshot.tools["lspci"] = lspci.stdout
        elif not lspci.available:
            snapshot.collection_notes.append("optional tool missing: lspci")

    relevant_bdfs = [node.get("bdf") for node in snapshot.topology if node.get("bdf")]
    if bdf and bdf not in relevant_bdfs:
        relevant_bdfs.append(bdf)
    if progress:
        progress("Collecting target-scoped OS/kernel events")
    snapshot.kernel_lines = _kernel_log(runner, controller, relevant_bdfs, kernel_lines)
    snapshot.collection_notes = list(dict.fromkeys(snapshot.collection_notes))
    if progress:
        progress("Evidence collection complete")
    return snapshot


def discover_controllers(
    sys_class_nvme: Path = SYS_CLASS_NVME,
    runner: Optional[Runner] = None,
    platform_name: Optional[str] = None,
) -> List[Dict[str, Any]]:
    key = platform_key(platform_name)
    if key == "darwin":
        from .collect_macos import discover_controllers_macos
        return discover_controllers_macos(runner=runner)
    if key == "linux":
        return discover_controllers_linux(sys_class_nvme=sys_class_nvme, runner=runner)
    return []


def collect_snapshot(
    requested_device: str,
    runner: Optional[Runner] = None,
    sys_class_nvme: Path = SYS_CLASS_NVME,
    sys_pci: Path = SYS_PCI,
    kernel_lines: int = 300,
    platform_name: Optional[str] = None,
    direct_usb: bool = False,
    auto_direct_usb: bool = False,
    direct_usb_mount_state_checked: bool = False,
    direct_usb_mounts_before: Optional[List[str]] = None,
    usb_extra_logs: bool = False,
    progress: Optional[Callable[[str], None]] = None,
) -> Snapshot:
    key = platform_key(platform_name)
    if key == "darwin":
        from .collect_macos import collect_snapshot_macos
        return collect_snapshot_macos(
            requested_device, runner=runner, kernel_lines=kernel_lines, direct_usb=direct_usb,
            auto_direct_usb=auto_direct_usb, direct_usb_mount_state_checked=direct_usb_mount_state_checked,
            direct_usb_mounts_before=direct_usb_mounts_before, usb_extra_logs=usb_extra_logs,
            progress=progress,
        )
    if key == "linux":
        return collect_snapshot_linux(
            requested_device, runner=runner, sys_class_nvme=sys_class_nvme,
            sys_pci=sys_pci, kernel_lines=kernel_lines, progress=progress
        )
    raise ValueError(f"unsupported operating system: {key}")

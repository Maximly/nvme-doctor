# SPDX-License-Identifier: GPL-3.0-or-later
from __future__ import annotations

import os
import platform
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from .model import Snapshot
from .runner import Runner
from .util import normalize_device, parse_counter_blob, read_json, read_text
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


def _discover_native_controllers_linux(sys_class_nvme: Path = SYS_CLASS_NVME) -> List[Dict[str, Any]]:
    result: List[Dict[str, Any]] = []
    if not sys_class_nvme.exists():
        return result
    for path in sorted(sys_class_nvme.glob("nvme[0-9]*")):
        if not re.fullmatch(r"nvme\d+", path.name):
            continue
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
        }
        result.append(info)
    return result


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
    """Discover both native NVMe and USB/SCSI-translated NVMe devices."""
    runner = runner or Runner()
    result = _discover_native_controllers_linux(sys_class_nvme)
    seen = {str(item.get("device")) for item in result}

    candidates = _smartctl_scan_candidates_linux(runner)
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
        probe = runner.run(["smartctl", "-i", "-j", device], timeout=10.0)
        payload = read_json(probe.stdout) if probe.available and probe.stdout.strip() else None
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
                "probe_note": "NVMe bridge detected; run list/check as root for identity/health access",
            })
            continue

        # Last-resort visibility for an unprivileged list: show a USB solid-state
        # block device as an *unverified candidate*, never as confirmed NVMe.
        if candidate.get("sysfs_usb") and (not probe.available or probe.returncode != 0 or payload is None):
            result.append({
                "controller": Path(device).name,
                "device": device,
                "model": candidate.get("bridge_model"),
                "serial": None,
                "firmware": None,
                "state": "probe-needed",
                "transport": "USB SSD (NVMe unverified)",
                "native_nvme": False,
                "probe_note": "protocol could not be identified without SMART access; rerun with sudo",
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
        ci: Dict[str, Any] = {
            "state": "present" if block.exists() else "missing",
            "transport": "USB/SCSI -> NVMe",
            "native_nvme": False,
            "usb_bridge_model": bridge_candidate.get("bridge_model"),
        }
        probe = runner.run(["smartctl", "-i", "-j", device_path], timeout=4.0)
        payload = read_json(probe.stdout) if probe.available and probe.stdout.strip() else None
        if _smartctl_json_is_nvme(payload):
            ident = _smartctl_identity(payload)
            ci.update({
                "model": ident.get("model"),
                "serial": ident.get("serial"),
                "firmware_rev": ident.get("firmware"),
                "nvme_version": ident.get("nvme_version"),
                "protocol": "NVMe",
                "identity_source": "smartctl",
            })
        else:
            ci["identity_source"] = "unavailable"
            if probe.available and probe.returncode not in (0, None):
                ci["identity_note"] = "underlying NVMe identity was not readable; rerun topology with sudo"
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

def _kernel_log(runner: Runner, controller: str, bdfs: List[str], max_lines: int) -> List[str]:
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

    selected: List[str] = []
    for line in text.splitlines():
        lower = line.lower()
        if controller_re.search(line):
            selected.append(line)
            continue
        if any(token in lower for token in bdf_tokens):
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
    snapshot.capabilities = {
        "nvme_smart": False,
        "nvme_error_log": False,
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
        snapshot.controller_info.update({
            "state": "present",
            "sysfs_present": (SYS_CLASS_BLOCK / controller).exists(),
            "transport": "USB/SCSI -> NVMe",
            "native_nvme": False,
        })
        smartctl = _read_tool_json(
            runner,
            ["smartctl", "-a", "-j", device_path],
            snapshot.collection_notes,
            accept_json_on_nonzero=True,
        )
        if isinstance(smartctl, dict) and _smartctl_json_is_nvme(smartctl):
            _apply_smartctl_nvme_payload(snapshot, smartctl, translated=True)
            snapshot.collection_notes.append(
                "NVMe is accessed through a USB/SCSI block device; native nvme-cli, PCIe AER/link, NUMA and endpoint topology are hidden by the bridge"
            )
        elif isinstance(smartctl, dict):
            snapshot.tools["smartctl"] = smartctl
            snapshot.controller_info["nvme_passthrough"] = False
            snapshot.controller_info["passthrough_reason"] = "linux-smartctl-not-nvme"
            snapshot.collection_notes.append(
                f"{device_path} is a block device, but smartctl did not identify an NVMe protocol behind it"
            )
        else:
            snapshot.controller_info["nvme_passthrough"] = False
            snapshot.controller_info["passthrough_reason"] = "linux-smartctl-unavailable"

        # Keep kernel evidence scoped to the block-device name.  This can catch
        # USB/SCSI resets/disconnects even though the native NVMe PCIe BDF is
        # intentionally unavailable through the bridge.
        snapshot.kernel_lines = _kernel_log(runner, controller, [], kernel_lines)
        snapshot.collection_notes = list(dict.fromkeys(snapshot.collection_notes))
        return snapshot

    controller_path = sys_class_nvme / controller
    if controller_path.exists():
        snapshot.controller_info.update(_collect_controller_sysfs(controller_path))
        snapshot.controller_info["native_nvme"] = True
    else:
        snapshot.controller_info.update({"state": "missing", "sysfs_present": False, "native_nvme": True})
        snapshot.collection_notes.append(f"{controller_path} is not present; controller may be disconnected or removed")

    bdf = _bdf_for_controller(controller_path) if controller_path.exists() else None
    snapshot.pci = _collect_pci_sysfs(bdf, sys_pci)
    snapshot.topology = _topology_for_bdf(bdf, sys_pci)
    snapshot.power = _collect_power()

    # nvme-cli is the preferred source for standards-defined controller data.
    smart = _read_tool_json(runner, ["nvme", "smart-log", device_path, "-o", "json"], snapshot.collection_notes)
    if isinstance(smart, dict):
        snapshot.smart = smart
        snapshot.capabilities["nvme_smart"] = True

    ctrl = _read_tool_json(runner, ["nvme", "id-ctrl", device_path, "-o", "json"], snapshot.collection_notes)
    if isinstance(ctrl, dict):
        snapshot.controller_info["nvme_id_ctrl"] = ctrl
        if ctrl.get("ver") is not None:
            snapshot.controller_info["nvme_version_raw"] = ctrl.get("ver")
        for key in ("tnvmcap", "unvmcap", "nn", "vwc"):
            if ctrl.get(key) is not None:
                snapshot.controller_info[key] = ctrl.get(key)

    errors = _read_tool_json(
        runner,
        ["nvme", "error-log", device_path, "-e", "64", "-o", "json"],
        snapshot.collection_notes,
    )
    if errors is not None:
        snapshot.error_log = errors
        snapshot.capabilities["nvme_error_log"] = True

    # smartctl is supplemental for native NVMe and fallback health evidence.
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
        lspci = runner.run(["lspci", "-vv", "-s", bdf], timeout=8.0)
        if lspci.available and lspci.returncode == 0:
            snapshot.tools["lspci"] = lspci.stdout
        elif not lspci.available:
            snapshot.collection_notes.append("optional tool missing: lspci")

    relevant_bdfs = [node.get("bdf") for node in snapshot.topology if node.get("bdf")]
    if bdf and bdf not in relevant_bdfs:
        relevant_bdfs.append(bdf)
    snapshot.kernel_lines = _kernel_log(runner, controller, relevant_bdfs, kernel_lines)
    snapshot.collection_notes = list(dict.fromkeys(snapshot.collection_notes))
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
) -> Snapshot:
    key = platform_key(platform_name)
    if key == "darwin":
        from .collect_macos import collect_snapshot_macos
        return collect_snapshot_macos(
            requested_device, runner=runner, kernel_lines=kernel_lines, direct_usb=direct_usb
        )
    if key == "linux":
        return collect_snapshot_linux(
            requested_device, runner=runner, sys_class_nvme=sys_class_nvme,
            sys_pci=sys_pci, kernel_lines=kernel_lines
        )
    raise ValueError(f"unsupported operating system: {key}")

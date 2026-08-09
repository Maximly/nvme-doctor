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


def discover_controllers_linux(sys_class_nvme: Path = SYS_CLASS_NVME) -> List[Dict[str, Any]]:
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
            "transport": read_text(path / "transport"),
            "address": read_text(path / "address"),
        }
        result.append(info)
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
) -> Dict[str, Any]:
    key = platform_key(platform_name)
    if key == "linux":
        return collect_topology_linux(
            requested_device, runner=runner, sys_class_nvme=sys_class_nvme,
            sys_pci=sys_pci, sys_class_block=sys_class_block
        )
    if key == "darwin":
        from .collect_macos import discover_controllers_macos
        from .util import normalize_macos_device
        controller, device_path = normalize_macos_device(requested_device)
        items = discover_controllers_macos(runner=runner)
        item = next((x for x in items if x.get("device") == device_path), None) or {}
        return {
            "platform": "darwin",
            "requested_device": requested_device,
            "controller": controller,
            "controller_device": device_path,
            "controller_info": item,
            "numa_node": None,
            "local_cpulist": None,
            "pci_domain": None,
            "pci_endpoint": {},
            "pci_path": [],
            "namespaces": [{"name": controller, "device": device_path}],
            "complete": False,
            "notes": ["macOS backend does not expose a supported NUMA/PCIe ancestry path; only the NVMe endpoint can be shown"],
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


def collect_snapshot_linux(
    requested_device: str,
    runner: Optional[Runner] = None,
    sys_class_nvme: Path = SYS_CLASS_NVME,
    sys_pci: Path = SYS_PCI,
    kernel_lines: int = 300,
) -> Snapshot:
    runner = runner or Runner()
    controller, device_path = normalize_device(requested_device)
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
        "pcie_link": True,
        "pcie_aer": True,
        "pcie_topology": True,
        "power_management": True,
        "targeted_kernel_log": True,
    }

    controller_path = sys_class_nvme / controller
    if controller_path.exists():
        snapshot.controller_info.update(_collect_controller_sysfs(controller_path))
    else:
        snapshot.controller_info.update({"state": "missing", "sysfs_present": False})
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
        # Promote a few high-value identity/capability fields so renderers do
        # not have to depend on one nvme-cli JSON spelling forever.
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

    # smartctl is supplemental. Keep its JSON without making it mandatory.
    smartctl = _read_tool_json(
        runner,
        ["smartctl", "-a", "-j", device_path],
        snapshot.collection_notes,
        accept_json_on_nonzero=True,
    )
    if isinstance(smartctl, dict):
        snapshot.tools["smartctl"] = smartctl
        if smartctl.get("nvme_version") is not None and not snapshot.controller_info.get("nvme_version"):
            snapshot.controller_info["nvme_version"] = smartctl.get("nvme_version")
        # smartctl is a useful fallback if nvme-cli is absent or fails.
        # Its NVMe health fields map closely to nvme-cli's SMART JSON.
        if not snapshot.smart:
            fallback = smartctl.get("nvme_smart_health_information_log")
            if isinstance(fallback, dict):
                snapshot.smart = fallback
                snapshot.capabilities["nvme_smart"] = True
                snapshot.collection_notes.append("using smartctl NVMe health JSON as fallback because nvme-cli SMART data was unavailable")

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
        return discover_controllers_linux(sys_class_nvme=sys_class_nvme)
    return []


def collect_snapshot(
    requested_device: str,
    runner: Optional[Runner] = None,
    sys_class_nvme: Path = SYS_CLASS_NVME,
    sys_pci: Path = SYS_PCI,
    kernel_lines: int = 300,
    platform_name: Optional[str] = None,
) -> Snapshot:
    key = platform_key(platform_name)
    if key == "darwin":
        from .collect_macos import collect_snapshot_macos
        return collect_snapshot_macos(requested_device, runner=runner, kernel_lines=kernel_lines)
    if key == "linux":
        return collect_snapshot_linux(
            requested_device, runner=runner, sys_class_nvme=sys_class_nvme,
            sys_pci=sys_pci, kernel_lines=kernel_lines
        )
    raise ValueError(f"unsupported operating system: {key}")

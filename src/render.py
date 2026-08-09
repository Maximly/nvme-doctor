# SPDX-License-Identifier: GPL-3.0-or-later
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from .model import Report, Snapshot
from .util import (
    first,
    format_bytes_decimal,
    format_duration_minutes,
    format_nvme_version,
    nvme_data_units_to_bytes,
    parse_temperature_c,
    pcie_generation,
    to_int,
)


SEV = {"critical": "CRITICAL", "warning": "WARNING", "info": "INFO"}


def _v(value: Any, fallback: str = "-") -> str:
    if value is None or value == "":
        return fallback
    return str(value)


def _count_text(value: Any) -> Optional[str]:
    number = to_int(value)
    if number is None:
        return None
    return f"{number:,}"


def _power_on_text(value: Any) -> Optional[str]:
    hours = to_int(value)
    if hours is None or hours < 0:
        return None
    if hours < 48:
        return f"{hours:,} h"
    return f"{hours:,} h ({hours / 24.0:.1f} d)"


def _critical_warning_text(value: Any) -> Optional[str]:
    number = to_int(value)
    if number is None:
        return None
    if number == 0:
        return "none"
    labels = [
        (0, "spare"),
        (1, "temperature"),
        (2, "reliability"),
        (3, "read-only"),
        (4, "volatile-backup"),
        (5, "persistent-memory"),
    ]
    active = [label for bit, label in labels if number & (1 << bit)]
    suffix = ", ".join(active) if active else "unknown bit(s)"
    return f"0x{number:02x} ({suffix})"


def _unsafe_text(smart: Dict[str, Any]) -> Optional[str]:
    unsafe = to_int(first(smart, "unsafe_shutdowns", "unsafe_shutdown_count"))
    if unsafe is None:
        return None
    cycles = to_int(first(smart, "power_cycles"))
    if cycles and cycles > 0:
        return f"{unsafe} ({unsafe * 100.0 / cycles:.1f}% of power cycles)"
    return str(unsafe)


def _data_volume(smart: Dict[str, Any], *keys: str) -> Optional[str]:
    raw = first(smart, *keys)
    total = nvme_data_units_to_bytes(raw)
    return format_bytes_decimal(total) if total is not None else None


def _smart_health_summary(snapshot: Snapshot) -> List[str]:
    smart = snapshot.smart or {}
    out: List[str] = []
    if not smart:
        return out
    temp = parse_temperature_c(first(smart, "temperature", "composite_temperature"))
    values = [
        ("Temperature", f"{temp}°C" if temp is not None else None),
        ("Critical warning", _critical_warning_text(first(smart, "critical_warning", "critical_warning_raw"))),
        ("Percentage used", first(smart, "percent_used", "percentage_used")),
        ("Available spare", first(smart, "avail_spare", "available_spare")),
        ("Spare threshold", first(smart, "spare_thresh", "available_spare_threshold")),
        ("Media errors", first(smart, "media_errors", "media_and_data_integrity_errors")),
        ("Error log entries", first(smart, "num_err_log_entries", "error_information_log_entries")),
    ]
    for label, value in values:
        if value is not None:
            suffix = "%" if label in {"Percentage used", "Available spare", "Spare threshold"} and "%" not in str(value) else ""
            out.append(f"  {label:<20} {_v(value)}{suffix}")
    return out


def _smart_lifetime_summary(snapshot: Snapshot) -> List[str]:
    smart = snapshot.smart or {}
    if not smart:
        return []
    values = [
        ("Power cycles", _count_text(first(smart, "power_cycles"))),
        ("Power-on hours", _power_on_text(first(smart, "power_on_hours"))),
        ("Unsafe shutdowns", _unsafe_text(smart)),
        ("Data read", _data_volume(smart, "data_units_read")),
        ("Data written", _data_volume(smart, "data_units_written")),
        ("Host read commands", _count_text(first(smart, "host_read_commands", "host_reads"))),
        ("Host write commands", _count_text(first(smart, "host_write_commands", "host_writes"))),
        ("Controller busy", format_duration_minutes(first(smart, "controller_busy_time"))),
        ("Warning-temp time", format_duration_minutes(first(smart, "warning_temp_time"))),
        ("Critical-temp time", format_duration_minutes(first(smart, "critical_comp_time"))),
    ]
    # Thermal Management Temperature 1/2 counters are optional in SMART.
    t1_count = first(smart, "thm_temp1_trans_count", "thermal_mgmt_temp1_transition_count")
    t1_time = first(smart, "thm_temp1_total_time", "thermal_mgmt_temp1_total_time")
    t2_count = first(smart, "thm_temp2_trans_count", "thermal_mgmt_temp2_transition_count")
    t2_time = first(smart, "thm_temp2_total_time", "thermal_mgmt_temp2_total_time")
    if (to_int(t1_count) or 0) > 0 or (to_int(t1_time) or 0) > 0:
        text = f"{_count_text(t1_count) or '0'} transition(s)"
        duration = format_duration_minutes(t1_time)
        if duration:
            text += f", {duration} total"
        values.append(("Thermal mgmt T1", text))
    if (to_int(t2_count) or 0) > 0 or (to_int(t2_time) or 0) > 0:
        text = f"{_count_text(t2_count) or '0'} transition(s)"
        duration = format_duration_minutes(t2_time)
        if duration:
            text += f", {duration} total"
        values.append(("Thermal mgmt T2", text))

    out: List[str] = []
    for label, value in values:
        if value is not None:
            out.append(f"  {label:<20} {_v(value)}")
    return out


def _nvme_version(snapshot: Snapshot) -> Optional[str]:
    ci = snapshot.controller_info
    value = ci.get("nvme_version")
    if value is None:
        value = ci.get("nvme_version_raw")
    if value is None:
        smartctl = snapshot.tools.get("smartctl")
        if isinstance(smartctl, dict):
            value = smartctl.get("nvme_version")
    return format_nvme_version(value)


def _capacity_text(snapshot: Snapshot) -> Optional[str]:
    ci = snapshot.controller_info
    total = to_int(ci.get("tnvmcap"))
    if total is not None and total > 0:
        return format_bytes_decimal(total)
    smartctl = snapshot.tools.get("smartctl")
    if isinstance(smartctl, dict):
        capacity = smartctl.get("user_capacity")
        if isinstance(capacity, dict):
            total = to_int(capacity.get("bytes"))
            if total is not None:
                return format_bytes_decimal(total)
    return None


def _error_records(snapshot: Snapshot) -> List[Dict[str, Any]]:
    payload = snapshot.error_log
    if isinstance(payload, list):
        rows = payload
    elif isinstance(payload, dict):
        rows = []
        for key in ("errors", "error_log", "entries"):
            if isinstance(payload.get(key), list):
                rows = payload[key]
                break
    else:
        rows = []
    result = []
    for item in rows:
        if not isinstance(item, dict):
            continue
        count = to_int(first(item, "error_count", "error_count_raw"))
        if count is None or count > 0:
            result.append(item)
    return result


def _error_history_summary(snapshot: Snapshot, limit: int = 3) -> List[str]:
    records = _error_records(snapshot)
    if not records:
        return []
    out = []
    for item in records[:limit]:
        fields = []
        for label, keys in [
            ("count", ("error_count",)),
            ("status", ("status_field", "status")),
            ("sqid", ("sqid", "submission_queue_id")),
            ("cmdid", ("cmdid", "command_id")),
            ("nsid", ("nsid", "namespace_id")),
            ("lba", ("lba",)),
        ]:
            value = first(item, *keys)
            if value not in (None, 0, "0", "0x0"):
                fields.append(f"{label}={value}")
        if fields:
            out.append("  " + "  ".join(fields))
    return out


def render_text(report: Report, verbose: bool = False) -> str:
    s = report.snapshot
    ci = s.controller_info
    pci = s.pci
    lines: List[str] = []
    lines.append(f"NVMe Doctor  |  {report.status}")
    if report.assessment:
        lines.append("")
        lines.append("DOCTOR'S ASSESSMENT")
        lines.append(f"  Verdict              {_v(report.assessment.get('verdict'))}")
        headline = report.assessment.get("headline")
        if headline:
            lines.append(f"  Conclusion           {headline}")
        for row in report.assessment.get("domains", []):
            if not isinstance(row, dict):
                continue
            label = str(row.get("label") or "Check")
            state = str(row.get("state") or "-")
            detail = str(row.get("detail") or "")
            text = f"{state} — {detail}" if detail else state
            lines.append(f"  {label:<20} {text}")
        recommendation = report.assessment.get("recommendation")
        if recommendation:
            lines.append(f"  Recommendation       {recommendation}")
    lines.append("")
    lines.append("DEVICE")
    platform_name = s.host.get("platform_label") or s.host.get("platform") or "-"
    platform_version = s.host.get("macos_version") or s.host.get("os_release", {}).get("PRETTY_NAME") or s.host.get("kernel")
    lines.append(f"  Platform             {_v(platform_name)}{(' ' + str(platform_version)) if platform_version else ''}")
    lines.append(f"  Controller           {s.controller}")
    lines.append(f"  Device               {s.device_path}")
    lines.append(f"  Model                {_v(ci.get('model'))}")
    lines.append(f"  Serial               {_v(ci.get('serial'))}")
    lines.append(f"  Firmware             {_v(ci.get('firmware_rev'))}")
    nvme_ver = _nvme_version(s)
    if nvme_ver:
        lines.append(f"  NVMe version         {nvme_ver}")
    capacity = _capacity_text(s)
    if capacity:
        lines.append(f"  NVM capacity         {capacity}")
    lines.append(f"  State                {_v(ci.get('state'))}")
    if pci:
        if pci.get("bdf"):
            lines.append(f"  PCI address          {_v(pci.get('bdf'))}")
        cur_gen = pcie_generation(pci.get("current_link_speed"))
        max_gen = pcie_generation(pci.get("max_link_speed"))
        if cur_gen is not None or max_gen is not None:
            if cur_gen is not None and max_gen is not None:
                gen_text = f"Gen{cur_gen}" + (f" (max Gen{max_gen})" if max_gen != cur_gen else "")
            elif cur_gen is not None:
                gen_text = f"Gen{cur_gen}"
            else:
                gen_text = f"max Gen{max_gen}"
            lines.append(f"  PCIe generation      {gen_text}")
        if pci.get("current_link_speed") or pci.get("current_link_width"):
            width = str(pci.get("current_link_width") or "-")
            width = width if width.lower().startswith("x") else f"x{width}"
            lines.append(f"  PCI link             {_v(pci.get('current_link_speed'))} {width}")
        if pci.get("max_link_speed") or pci.get("max_link_width"):
            width = str(pci.get("max_link_width") or "-")
            width = width if width.lower().startswith("x") else f"x{width}"
            lines.append(f"  PCI max              {_v(pci.get('max_link_speed'))} {width}")
        if pci.get("numa_node") is not None:
            lines.append(f"  NUMA node            {_v(pci.get('numa_node'))}")

    health_lines = _smart_health_summary(s)
    if health_lines:
        lines.append("")
        lines.append("HEALTH")
        lines.extend(health_lines)

    lifetime_lines = _smart_lifetime_summary(s)
    if lifetime_lines:
        lines.append("")
        lines.append("LIFETIME / USAGE")
        lines.extend(lifetime_lines)

    error_lines = _error_history_summary(s)
    if error_lines:
        lines.append("")
        lines.append("RECENT NVME ERROR LOG")
        lines.extend(error_lines)

    lines.append("")
    lines.append("FINDINGS / EVIDENCE")
    if not report.findings:
        if report.status == "INCOMPLETE":
            lines.append("  Diagnostic evidence is incomplete; a clean-health conclusion cannot be made.")
            if s.host.get("euid") not in (None, 0):
                lines.append(f"  Re-run as root to allow NVMe admin/log-page access: sudo nvme-doctor check {s.controller}")
        else:
            lines.append("  No high-signal problem was detected from the available evidence.")
    else:
        for idx, finding in enumerate(report.findings, 1):
            lines.append(f"  {idx}. [{SEV.get(finding.severity, finding.severity.upper())}] {finding.title}")
            lines.append(f"     {finding.summary}")
            lines.append(f"     Confidence: {finding.confidence.upper()}")
            if finding.evidence:
                lines.append("     Evidence:")
                for item in finding.evidence:
                    one = " ".join(str(item).split())
                    lines.append(f"       - {one[:360]}")
            if finding.actions:
                lines.append("     Suggested next step(s):")
                for item in finding.actions:
                    lines.append(f"       - {item}")

    if s.host.get("platform") == "darwin" or verbose:
        lines.append("")
        lines.append("CAPABILITIES")
        labels = [
            ("NVMe SMART/Health", "nvme_smart"),
            ("NVMe error log", "nvme_error_log"),
            ("PCIe link", "pcie_link"),
            ("PCIe AER", "pcie_aer"),
            ("PCIe topology", "pcie_topology"),
            ("Power-state analysis", "power_management"),
            ("Target-scoped OS log", "targeted_kernel_log"),
        ]
        for label, key in labels:
            value = s.capabilities.get(key, False)
            if value is True:
                text = "yes"
            elif value is False:
                text = "unavailable"
            else:
                text = str(value)
            lines.append(f"  {label:<22} {text}")

    if s.collection_notes:
        lines.append("")
        lines.append("COLLECTION NOTES")
        for note in s.collection_notes:
            lines.append(f"  - {note}")

    if verbose:
        if s.topology:
            lines.append("")
            lines.append("PCI TOPOLOGY")
            for node in s.topology:
                lines.append(
                    f"  {node.get('bdf', '-')}  class={node.get('class', '-')}  "
                    f"link={node.get('current_link_speed', '-')} x{node.get('current_link_width', '-')}  "
                    f"numa={node.get('numa_node', '-')}"
                )
        if s.kernel_lines:
            lines.append("")
            lines.append("RELEVANT KERNEL LOG")
            for line in s.kernel_lines[-80:]:
                lines.append(f"  {line}")

    lines.append("")
    lines.append("Safety: NVMe Doctor is diagnostic-only; it does not reset controllers or change OS/storage power settings.")
    return "\n".join(lines) + "\n"


def render_json(report: Report, pretty: bool = True) -> str:
    return json.dumps(report.to_dict(), indent=2 if pretty else None, sort_keys=False) + "\n"


def _topology_link_text(node: Dict[str, Any]) -> Optional[str]:
    cur_speed = node.get("current_link_speed")
    cur_width = node.get("current_link_width")
    max_speed = node.get("max_link_speed")
    max_width = node.get("max_link_width")
    cur_gen = pcie_generation(cur_speed)
    max_gen = pcie_generation(max_speed)

    current = None
    if cur_gen is not None or cur_speed or cur_width:
        bits = []
        if cur_gen is not None:
            bits.append(f"Gen{cur_gen}")
        elif cur_speed:
            bits.append(str(cur_speed).replace(" PCIe", ""))
        if cur_width not in (None, ""):
            width = str(cur_width)
            bits.append(width if width.lower().startswith("x") else f"x{width}")
        current = " ".join(bits)

    maximum = None
    if max_gen is not None or max_speed or max_width:
        bits = []
        if max_gen is not None:
            bits.append(f"Gen{max_gen}")
        elif max_speed:
            bits.append(str(max_speed).replace(" PCIe", ""))
        if max_width not in (None, ""):
            width = str(max_width)
            bits.append(width if width.lower().startswith("x") else f"x{width}")
        maximum = " ".join(bits)

    if current and maximum and current != maximum:
        return f"{current}, max {maximum}"
    return current or maximum


def render_topology_text(topology: Dict[str, Any]) -> str:
    lines: List[str] = ["NVMe Doctor  |  TOPOLOGY", ""]
    ci = topology.get("controller_info") or {}
    model = ci.get("model") or ci.get("model_name") or "-"
    lines.append(f"Controller           {topology.get('controller') or '-'}")
    lines.append(f"Model                {model}")
    if ci.get("serial"):
        lines.append(f"Serial               {ci.get('serial')}")

    lines.append("")
    lines.append("HARDWARE PATH")
    platform_name = topology.get("platform")
    numa = topology.get("numa_node")
    cpus = topology.get("local_cpulist")
    if platform_name == "linux":
        if numa not in (None, "-1"):
            root = f"NUMA node {numa}"
            if cpus:
                root += f"  [local CPUs {cpus}]"
        else:
            root = "NUMA locality unknown"
        lines.append(root)
        domain = topology.get("pci_domain")
        prefix = "└─ "
        if domain:
            lines.append(f"{prefix}PCI domain {domain}")
            indent = "   "
        else:
            indent = ""

        path = topology.get("pci_path") or []
        for idx, node in enumerate(path):
            is_last_pci = idx == len(path) - 1
            branch = "└─ "
            bdf = node.get("bdf") or "-"
            role = node.get("role") or "PCI device"
            desc = node.get("description")
            label = f"{bdf}  {role}"
            if desc:
                label += f" — {desc}"
            lines.append(f"{indent}{branch}{label}")
            details: List[str] = []
            link = _topology_link_text(node)
            if link:
                details.append(link)
            if node.get("driver"):
                details.append(f"driver {node.get('driver')}")
            if node.get("power_state"):
                details.append(f"power {node.get('power_state')}")
            if node.get("numa_node") not in (None, "-1", numa):
                details.append(f"NUMA {node.get('numa_node')}")
            if details:
                lines.append(f"{indent}   {'; '.join(details)}")
            # All descendants after a PCI node are indented beneath it.  We do
            # not try to draw sibling switch branches because this command only
            # displays the selected device's path.
            indent += "   "

        namespaces = topology.get("namespaces") or []
        if namespaces:
            for idx, ns in enumerate(namespaces):
                branch = "└─ " if idx == len(namespaces) - 1 else "├─ "
                label = ns.get("device") or ns.get("name") or "namespace"
                extra: List[str] = []
                if ns.get("nsid") is not None:
                    extra.append(f"NSID {ns.get('nsid')}")
                if ns.get("capacity_bytes") is not None:
                    extra.append(format_bytes_decimal(to_int(ns.get("capacity_bytes"))) or str(ns.get("capacity_bytes")))
                if ns.get("logical_block_size") is not None:
                    extra.append(f"LBA {ns.get('logical_block_size')} B")
                if extra:
                    label += "  [" + ", ".join(extra) + "]"
                lines.append(f"{indent}{branch}{label}")
        elif path:
            lines.append(f"{indent}└─ {topology.get('controller_device') or topology.get('controller')}")
    else:
        lines.append("macOS NVMe endpoint")
        lines.append(f"└─ {topology.get('controller_device') or topology.get('controller')} — {model}")

    endpoint = topology.get("pci_endpoint") or {}
    if endpoint:
        lines.append("")
        lines.append("PATH CHECK")
        endpoint_link = _topology_link_text(endpoint)
        cur_gen = pcie_generation(endpoint.get("current_link_speed"))
        max_gen = pcie_generation(endpoint.get("max_link_speed"))
        cur_width = to_int(endpoint.get("current_link_width"))
        max_width = to_int(endpoint.get("max_link_width"))
        endpoint_down = (cur_gen is not None and max_gen is not None and cur_gen < max_gen) or (
            cur_width is not None and max_width is not None and cur_width < max_width
        )
        if endpoint_link:
            lines.append(f"  Endpoint link        {endpoint_link} — {'DOWN-TRAINED' if endpoint_down else 'at device maximum'}")

        downtrained = []
        for node in topology.get("pci_path") or []:
            cgen = pcie_generation(node.get("current_link_speed"))
            mgen = pcie_generation(node.get("max_link_speed"))
            cw = to_int(node.get("current_link_width"))
            mw = to_int(node.get("max_link_width"))
            if ((cgen is not None and mgen is not None and cgen < mgen) or
                    (cw is not None and mw is not None and cw < mw)):
                downtrained.append(f"{node.get('bdf')}: {_topology_link_text(node) or 'link below maximum'}")
        if downtrained:
            lines.append(f"  Down-trained hops    {len(downtrained)}")
            for item in downtrained:
                lines.append(f"                       - {item}")
        else:
            lines.append("  Down-trained hops    none detected")
        if topology.get("numa_node") not in (None, "-1"):
            locality = f"NUMA {topology.get('numa_node')}"
            if topology.get("local_cpulist"):
                locality += f"; CPUs {topology.get('local_cpulist')}"
            lines.append(f"  CPU locality         {locality}")

        lines.append("")
        lines.append("PATH SUMMARY")
        lines.append(f"  NUMA node            {_v(topology.get('numa_node'), 'unknown')}")
        if topology.get("local_cpulist"):
            lines.append(f"  Local CPUs           {topology.get('local_cpulist')}")
        lines.append(f"  PCI endpoint         {_v(endpoint.get('bdf'))}")
        link = _topology_link_text(endpoint)
        if link:
            lines.append(f"  Endpoint link        {link}")
        if endpoint.get("driver"):
            lines.append(f"  Driver               {endpoint.get('driver')}")
        lines.append(f"  Namespace count      {len(topology.get('namespaces') or [])}")

    notes = topology.get("notes") or []
    if notes:
        lines.append("")
        lines.append("NOTES")
        for note in notes:
            lines.append(f"  - {note}")
    return "\n".join(lines) + "\n"


def render_topology_json(topology: Dict[str, Any]) -> str:
    return json.dumps(topology, indent=2, sort_keys=False) + "\n"

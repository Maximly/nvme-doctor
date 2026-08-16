# SPDX-License-Identifier: GPL-3.0-or-later
from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .model import Finding, Report, Snapshot
from .util import (
    first,
    parse_pcie_speed,
    parse_pcie_width,
    parse_temperature_c,
    pcie_generation,
    to_int,
)


SEVERITY_ORDER = {"critical": 0, "warning": 1, "info": 2}


def _smart_int(smart: Dict[str, Any], *keys: str) -> Optional[int]:
    return to_int(first(smart, *keys))


def _aer_totals(pci: Dict[str, Any]) -> Tuple[int, int, int]:
    corrected = nonfatal = fatal = 0
    aer = pci.get("aer")
    if not isinstance(aer, dict):
        return corrected, nonfatal, fatal
    for name, payload in aer.items():
        if isinstance(payload, dict):
            total = sum(max(0, to_int(v) or 0) for v in payload.values())
        else:
            nums = [int(x) for x in re.findall(r"(?<![A-Za-z0-9])\d+", str(payload))]
            total = sum(nums)
        lower = name.lower()
        if "nonfatal" in lower or "non_fatal" in lower:
            nonfatal += total
        elif "fatal" in lower:
            fatal += total
        elif "correct" in lower:
            corrected += total
    return corrected, nonfatal, fatal


def _kernel_flags(lines: Iterable[str]) -> Dict[str, Any]:
    patterns = {
        "timeout": re.compile(r"timeout|timed out|I/O .* timeout", re.I),
        "reset": re.compile(r"resetting controller|controller reset|reset controller|resetting device|reset failed", re.I),
        "down": re.compile(r"controller is down|device not ready|aborting reset|CSTS=0x[fF]+|removing after probe failure|device disconnected", re.I),
        "aer_uncorrected": re.compile(r"AER:.*uncorrect|uncorrected.*error|severity=(?:fatal|uncorrected)", re.I),
        "aer_corrected": re.compile(r"AER:.*corrected|corrected error", re.I),
        "link": re.compile(r"link down|link retrain|surprise down|DPC:.*containment", re.I),
        "usb_reset": re.compile(r"(?:usb .*reset|reset (?:super|high|full)-speed usb device|uas_eh_.*reset|usb-storage.*reset)", re.I),
        "usb_disconnect": re.compile(r"(?:usb .*disconnect|USB disconnect|device descriptor read|device not accepting address|cannot enable)", re.I),
        "uas_error": re.compile(r"(?:uas_eh_abort_handler|uas_eh_device_reset_handler|uas.*(?:abort|reset|failed|error))", re.I),
        "scsi_io": re.compile(r"(?:I/O error, dev sd[a-z]+|blk_update_request.*sd[a-z]+|Buffer I/O error.*sd[a-z]+)", re.I),
        "ata_reset": re.compile(r"(?:ata\d+:.*(?:hard resetting link|softreset failed|reset failed)|SATA link down)", re.I),
        "ata_error": re.compile(r"(?:ata\d+:.*(?:failed command|exception Emask|SError|status: \{)|end_request: I/O error.*sd[a-z]+)", re.I),
        "ata_link": re.compile(r"(?:ata\d+: SATA link (?:up|down)|SATA link speed|limiting SATA link speed)", re.I),
    }
    out: Dict[str, Any] = {key: [] for key in patterns}
    for line in lines:
        for key, pat in patterns.items():
            if pat.search(line):
                out[key].append(line)
    return out


def _active_aspm(policy: str) -> Optional[str]:
    match = re.search(r"\[([^\]]+)\]", policy)
    if match:
        return match.group(1)
    text = policy.strip()
    return text or None



def _decode_critical_warning(value: int) -> List[str]:
    labels = [
        (0, "available spare below threshold"),
        (1, "temperature threshold exceeded"),
        (2, "NVM subsystem reliability degraded"),
        (3, "media placed in read-only mode"),
        (4, "volatile memory backup failed"),
        (5, "persistent memory region read-only/unreliable"),
    ]
    return [label for bit, label in labels if value & (1 << bit)]



def _finding_severity(findings: List[Finding], codes: set[str]) -> Optional[str]:
    matches = [f.severity for f in findings if f.code in codes]
    if "critical" in matches:
        return "critical"
    if "warning" in matches:
        return "warning"
    if "info" in matches:
        return "info"
    return None


_MEDIA_RISK_CODES = {
    "nvme-critical-warning",
    "available-spare",
    "media-errors",
    "endurance-near-limit",
    "endurance-used",
    "ata-smart-failed",
    "ata-threshold-failure",
    "ata-unstable-sectors",
    "ata-reallocated-sectors",
    "ata-reported-uncorrectable",
    "ata-reserve-low",
    "ata-reserve-margin-low",
}


def _assessment_summary(findings: List[Finding], status: str, protocol: str) -> Dict[str, Any]:
    """Separate current health from future-risk/trend language.

    A single snapshot can establish current faults and risk signals, but it cannot
    establish a trend.  Trend therefore remains UNKNOWN unless a future caller
    explicitly supplies comparison evidence.
    """
    warning_media = any(f.severity == "warning" and f.code in _MEDIA_RISK_CODES for f in findings)

    if status == "CRITICAL":
        verdict = "CRITICAL"
        risk_state = "HIGH"
        risk_detail = "Serious current failure/reliability evidence is present."
    elif status == "WARNING":
        if warning_media:
            verdict = "AT RISK"
            risk_state = "ELEVATED"
            risk_detail = "Current media/endurance evidence warrants attention; this is not a time-to-failure prediction."
        else:
            verdict = "DEGRADED"
            risk_state = "ELEVATED"
            risk_detail = "A current transport, controller, link, or thermal problem is present; this is not a time-to-failure prediction."
    elif status == "INCOMPLETE":
        verdict = "INCOMPLETE"
        risk_state = "UNKNOWN"
        risk_detail = "Primary health evidence is incomplete, so near-term risk cannot be assessed reliably."
    else:
        verdict = "HEALTHY"
        risk_state = "LOW"
        risk_detail = "No warning/critical health signal was detected in the evidence collected by this check."

    if status == "OK":
        headline = f"No material current {protocol} health fault was detected in the available evidence."
    elif status == "INCOMPLETE":
        headline = f"There is not enough {protocol} health evidence to issue a clean verdict."
    elif verdict == "AT RISK":
        headline = f"Current {protocol} media/endurance evidence indicates elevated risk and needs investigation."
    elif verdict == "DEGRADED":
        headline = f"The {protocol} drive or storage path has a current degraded condition that needs investigation."
    else:
        headline = f"Active evidence indicates a serious {protocol} storage problem."

    return {
        "verdict": verdict,
        "headline": headline,
        "near_term_risk": {"state": risk_state, "detail": risk_detail},
        "trend": {
            "state": "UNKNOWN",
            "detail": "No historical comparison was supplied to this check; save a baseline and use `nvme-doctor diff` to establish direction of change.",
        },
    }



def _build_ata_assessment(snapshot: Snapshot, findings: List[Finding], status: str) -> Dict[str, Any]:
    smart = snapshot.smart or {}
    rows: List[Dict[str, str]] = []
    summary = _assessment_summary(findings, status, "ATA/SATA")
    verdict = summary["verdict"]
    headline = summary["headline"]

    media_codes = {"ata-smart-failed", "ata-threshold-failure", "ata-unstable-sectors", "ata-reallocated-sectors", "ata-reported-uncorrectable"}
    media_sev = _finding_severity(findings, media_codes)
    if media_sev in {"critical", "warning"}:
        detail = "; ".join(f.title for f in findings if f.code in media_codes and f.severity in {"critical", "warning"})
        rows.append({"label": "Media / integrity", "state": "PROBLEM", "detail": detail})
    elif smart:
        overall = smart.get("smart_passed")
        if overall is True:
            prefix = "SMART overall-health passed"
        elif overall is False:
            prefix = "SMART overall-health failed"
        else:
            prefix = "SMART attributes collected; no threshold failure detected"
        rows.append({"label": "Media / integrity", "state": "CLEAN", "detail": f"{prefix}; pending={_smart_int(smart, 'current_pending_sectors') or 0}, offline-uncorrectable={_smart_int(smart, 'offline_uncorrectable') or 0}"})
    else:
        rows.append({"label": "Media / integrity", "state": "UNKNOWN", "detail": "ATA SMART data was not collected"})

    reserve = _smart_int(smart, "reserved_space")
    reserve_threshold = _smart_int(smart, "reserved_space_threshold")
    reserve_margin = _smart_int(smart, "reserved_space_margin")
    wear = _smart_int(smart, "media_wear_indicator")
    if reserve is not None:
        detail_parts = [f"reserved-space indicator {reserve}/100" if 0 <= reserve <= 100 else f"reserved-space indicator {reserve}"]
        if reserve_threshold is not None:
            detail_parts.append(f"threshold {reserve_threshold}/100" if 0 <= reserve_threshold <= 100 else f"threshold {reserve_threshold}")
        if reserve_margin is not None:
            detail_parts.append(f"margin {reserve_margin:+d}")
        if wear is not None:
            detail_parts.append(f"media-wear indicator {wear}/100" if 0 <= wear <= 100 else f"media-wear indicator {wear}")
        reserve_sev = _finding_severity(findings, {"ata-reserve-low", "ata-reserve-margin-low"})
        rows.append({"label": "Flash reserve", "state": "PROBLEM" if reserve_sev in {"critical", "warning"} else "HEALTHY", "detail": "; ".join(detail_parts)})

    usb_path = snapshot.controller_info.get("usb_path")
    if isinstance(usb_path, dict) and usb_path.get("usb_port"):
        usb_codes = {"usb-transport-instability", "usb-link-downshift"}
        usb_sev = _finding_severity(findings, usb_codes)
        usb = usb_path or {}
        if usb_sev in {"critical", "warning"}:
            rows.append({"label": "USB transport", "state": "PROBLEM", "detail": "; ".join(f.title for f in findings if f.code in usb_codes and f.severity in {"critical", "warning"})})
        else:
            bits = [str(x) for x in (usb.get("usb_port"), f"{usb.get('speed_mbps')} Mb/s" if usb.get("speed_mbps") is not None else None, usb.get("interface_driver")) if x]
            rows.append({"label": "USB transport", "state": "CLEAN", "detail": "; ".join(bits) or "USB bridge path identified"})
    elif str(snapshot.controller_info.get("transport") or "").lower().startswith("usb"):
        rows.append({
            "label": "USB / SATA transport",
            "state": "UNKNOWN" if not snapshot.capabilities.get("ata_smart") else "KNOWN",
            "detail": "USB-SATA path identified; native SATA link details and SMART are hidden by this macOS bridge path" if not snapshot.capabilities.get("ata_smart") else "USB-SATA SMART passthrough is available",
        })
    else:
        iface = snapshot.controller_info.get("interface_speed")
        sata = snapshot.controller_info.get("sata_version")
        details = []
        if isinstance(iface, dict):
            for key in ("current", "max"):
                val = iface.get(key)
                if isinstance(val, dict):
                    val = val.get("string")
                if val:
                    details.append(f"{key}={val}")
        if isinstance(sata, dict) and sata.get("string"):
            details.append(str(sata.get("string")))
        rows.append({"label": "SATA transport", "state": "KNOWN" if details else "UNKNOWN", "detail": "; ".join(details) if details else "SATA link details unavailable"})

    temp = parse_temperature_c(smart.get("temperature"))
    thermal_sev = _finding_severity(findings, {"temperature-critical", "temperature-warning"})
    if thermal_sev in {"critical", "warning"}:
        rows.append({"label": "Thermal", "state": "PROBLEM", "detail": next(f.summary for f in findings if f.code in {"temperature-critical", "temperature-warning"})})
    elif temp is not None:
        rows.append({"label": "Thermal", "state": "NORMAL", "detail": f"{temp}°C"})

    crc = _smart_int(smart, "udma_crc_errors") or 0
    timeouts = _smart_int(smart, "command_timeouts") or 0
    if crc or timeouts:
        rows.append({"label": "Interface history", "state": "REVIEW", "detail": f"CRC errors={crc}; command timeouts={timeouts}"})

    if status == "OK":
        recommendation = "No immediate action is indicated. Save a JSON report as a baseline and compare counters if symptoms appear."
    elif status == "INCOMPLETE":
        if snapshot.controller_info.get("ata_passthrough") is False:
            recommendation = "The SSD is identified as ATA/SATA, but this macOS USB path does not expose ATA SMART. Use a SAT-capable USB bridge/path, native SATA, or inspect it on Linux before making a health judgement."
        elif isinstance(usb_path, dict) and usb_path.get("usb_port"):
            recommendation = "Restore ATA SMART access through the correct SAT/USB bridge backend before trusting a clean result."
        else:
            recommendation = "Restore ATA SMART access through the native libata/ATA backend before trusting a clean result."
    else:
        actionable = [f.actions[0] for f in findings if f.severity in {"critical", "warning"} and f.actions]
        recommendation = actionable[0] if actionable else "Review the warning/critical findings before stressing or modifying the drive."
    return {
        "verdict": verdict,
        "headline": headline,
        "near_term_risk": summary["near_term_risk"],
        "trend": summary["trend"],
        "domains": rows,
        "recommendation": recommendation,
    }

def _build_assessment(snapshot: Snapshot, findings: List[Finding], status: str) -> Dict[str, Any]:
    """Build a concise doctor-style interpretation from the collected evidence."""
    if _is_ata_snapshot(snapshot):
        return _build_ata_assessment(snapshot, findings, status)
    smart = snapshot.smart or {}
    pci = snapshot.pci or {}
    kernel = _kernel_flags(snapshot.kernel_lines) if snapshot.capabilities.get("targeted_kernel_log", True) else _kernel_flags([])
    rows: List[Dict[str, str]] = []

    summary = _assessment_summary(findings, status, "NVMe")
    verdict = summary["verdict"]
    headline = summary["headline"]

    # Media / integrity
    media_sev = _finding_severity(findings, {"nvme-critical-warning", "available-spare", "media-errors"})
    if snapshot.controller_info.get("native_nvme") is False:
        usb = snapshot.controller_info.get("usb_path") if isinstance(snapshot.controller_info.get("usb_path"), dict) else {}
        usb_reset = kernel.get("usb_reset", [])
        usb_disconnect = kernel.get("usb_disconnect", [])
        uas_error = kernel.get("uas_error", [])
        scsi_io = kernel.get("scsi_io", [])
        transport_events = len(usb_reset) + len(usb_disconnect) + len(uas_error)
        if transport_events or scsi_io:
            evidence = []
            if usb.get("usb_port"):
                evidence.append(f"usb_port={usb.get('usb_port')}")
            if usb.get("vid_pid"):
                evidence.append(f"usb_vid_pid={usb.get('vid_pid')}")
            if usb.get("interface_driver"):
                evidence.append(f"usb_driver={usb.get('interface_driver')}")
            evidence += (usb_disconnect + uas_error + usb_reset + scsi_io)[-6:]
            findings.append(Finding(
                "usb-transport-instability",
                "warning",
                "USB storage transport shows reset/disconnect evidence",
                "Target-correlated host logs contain USB/UAS/SCSI transport recovery or I/O events. When NVMe media SMART remains clean, this evidence points more strongly to the enclosure, cable, port, power delivery, or USB host path than to NAND media.",
                "high" if transport_events >= 2 or scsi_io else "medium",
                evidence,
                [
                    "Retest the enclosure on a direct host USB port with a known-good short cable and avoid hubs/docks for the comparison.",
                    "Compare the same SSD on native PCIe/M.2 if the resets continue; preserve the before/after nvme-doctor reports.",
                ],
            ))

        speed = usb.get("speed_mbps")
        version = str(usb.get("usb_version") or "").strip()
        try:
            speed_num = float(speed) if speed is not None else None
        except (TypeError, ValueError):
            speed_num = None
        try:
            version_num = float(version) if version else None
        except ValueError:
            version_num = None
        if version_num is not None and version_num >= 3.0 and speed_num is not None and speed_num <= 480:
            findings.append(Finding(
                "usb-link-downshift",
                "warning",
                "USB 3.x bridge is operating at USB 2.0 speed",
                f"The bridge reports USB {version} capability but the current sysfs link speed is only {speed_num:g} Mb/s.",
                "high",
                [
                    f"usb_version={version}",
                    f"speed_mbps={speed_num:g}",
                    f"usb_port={usb.get('usb_port') or 'unknown'}",
                ],
                ["Replace/reseat the cable and bypass hubs/docks, then confirm the link returns to SuperSpeed (5 Gb/s or faster)."],
            ))

        if str(usb.get("interface_driver") or "").lower() == "usb-storage":
            findings.append(Finding(
                "usb-bot-transport",
                "info",
                "USB mass storage is using usb-storage/BOT",
                "The host bound the enclosure to the bulk-only usb-storage path rather than UAS. This is not a health failure, but it can limit queueing/performance and is useful when comparing bridge behavior between ports/hosts.",
                "high",
                [f"interface_driver={usb.get('interface_driver')}", f"usb_port={usb.get('usb_port') or 'unknown'}"],
                ["If the enclosure is expected to support UAS, compare on another host/port and check whether the bridge is being quirked to usb-storage."],
            ))

    critical_warning = _smart_int(smart, "critical_warning", "critical_warning_raw")
    media_errors = _smart_int(smart, "media_errors", "media_and_data_integrity_errors")
    if media_sev in {"critical", "warning"}:
        detail = "; ".join(f.title for f in findings if f.code in {"nvme-critical-warning", "available-spare", "media-errors"})
        rows.append({"label": "Media / integrity", "state": "PROBLEM", "detail": detail})
    elif smart:
        rows.append({"label": "Media / integrity", "state": "CLEAN", "detail": f"SMART critical warning clear; {media_errors or 0} media/data-integrity errors"})
    else:
        rows.append({"label": "Media / integrity", "state": "UNKNOWN", "detail": "NVMe SMART/Health data was not collected"})

    # USB transport is a separate failure domain from the underlying NVMe media.
    if snapshot.controller_info.get("native_nvme") is False:
        usb_codes = {"usb-transport-instability", "usb-link-downshift"}
        usb_sev = _finding_severity(findings, usb_codes)
        usb = snapshot.controller_info.get("usb_path") if isinstance(snapshot.controller_info.get("usb_path"), dict) else {}
        if usb_sev in {"critical", "warning"}:
            detail = "; ".join(f.title for f in findings if f.code in usb_codes and f.severity in {"critical", "warning"})
            rows.append({"label": "USB transport", "state": "PROBLEM", "detail": detail})
        elif usb:
            bits = []
            if usb.get("usb_port"):
                bits.append(f"port {usb.get('usb_port')}")
            if usb.get("speed_mbps") is not None:
                bits.append(f"{usb.get('speed_mbps')} Mb/s")
            if usb.get("interface_driver"):
                bits.append(str(usb.get("interface_driver")))
            usb_events = len(kernel.get("usb_reset", [])) + len(kernel.get("usb_disconnect", [])) + len(kernel.get("uas_error", []))
            if snapshot.capabilities.get("targeted_kernel_log", True) and usb_events == 0:
                bits.append("no target-scoped USB reset/disconnect evidence")
            rows.append({"label": "USB transport", "state": "CLEAN", "detail": "; ".join(bits) or "host USB path identified"})
        else:
            rows.append({"label": "USB transport", "state": "UNKNOWN", "detail": "USB bridge path details were not available"})

    # PCIe / controller path
    pcie_codes = {"pcie-aer-fatal", "pcie-aer-nonfatal", "pcie-aer-corrected", "pcie-link-degraded", "kernel-controller-down", "kernel-reset-timeout", "likely-pcie-path"}
    pcie_sev = _finding_severity(findings, pcie_codes)
    if pcie_sev in {"critical", "warning"}:
        detail = "; ".join(f.title for f in findings if f.code in pcie_codes and f.severity in {"critical", "warning"})
        rows.append({"label": "PCIe / controller", "state": "PROBLEM", "detail": detail})
    elif pci:
        cur_gen = pcie_generation(pci.get("current_link_speed"))
        max_gen = pcie_generation(pci.get("max_link_speed"))
        cur_width = parse_pcie_width(pci.get("current_link_width"))
        max_width = parse_pcie_width(pci.get("max_link_width"))
        link = []
        if cur_gen is not None:
            link.append(f"Gen{cur_gen}")
        if cur_width is not None:
            link.append(f"x{cur_width}")
        if cur_gen is not None and max_gen == cur_gen and cur_width is not None and max_width == cur_width:
            link.append("at endpoint maximum")
        reset_count = len(kernel["reset"]) + len(kernel["timeout"]) + len(kernel["down"])
        suffix = "no target-scoped reset/timeout evidence in the collected boot log" if snapshot.capabilities.get("targeted_kernel_log", True) and reset_count == 0 else "no warning-level PCIe/controller finding"
        rows.append({"label": "PCIe / controller", "state": "CLEAN", "detail": (" ".join(link) + "; " + suffix).strip("; ")})
    else:
        rows.append({"label": "PCIe / controller", "state": "UNKNOWN", "detail": "PCIe path details are unavailable on this platform/device"})

    # Thermals
    temp = parse_temperature_c(first(smart, "temperature", "composite_temperature"))
    thermal_sev = _finding_severity(findings, {"temperature-critical", "temperature-warning"})
    warning_time = _smart_int(smart, "warning_temp_time")
    critical_time = _smart_int(smart, "critical_comp_time")
    if thermal_sev in {"critical", "warning"}:
        rows.append({"label": "Thermal", "state": "PROBLEM", "detail": next(f.summary for f in findings if f.code in {"temperature-critical", "temperature-warning"})})
    elif temp is not None:
        history = []
        if warning_time is not None:
            history.append(f"warning-temp history {warning_time} min")
        if critical_time is not None:
            history.append(f"critical-temp history {critical_time} min")
        rows.append({"label": "Thermal", "state": "NORMAL", "detail": f"{temp}°C" + (("; " + ", ".join(history)) if history else "")})

    # Endurance
    used = _smart_int(smart, "percent_used", "percentage_used")
    spare = _smart_int(smart, "avail_spare", "available_spare")
    endurance_sev = _finding_severity(findings, {"endurance-used", "endurance-near-limit", "available-spare"})
    if endurance_sev in {"critical", "warning"}:
        rows.append({"label": "Endurance", "state": "PROBLEM", "detail": "; ".join(f.summary for f in findings if f.code in {"endurance-used", "endurance-near-limit", "available-spare"})})
    elif used is not None or spare is not None:
        parts = []
        if used is not None:
            parts.append(f"{used}% used")
        if spare is not None:
            parts.append(f"{spare}% spare")
        state = "EXCELLENT" if (used is not None and used < 10 and (spare is None or spare >= 90)) else "NORMAL"
        rows.append({"label": "Endurance", "state": state, "detail": "; ".join(parts)})

    # Historical counters are deliberately separated from current-health verdict.
    unsafe = _smart_int(smart, "unsafe_shutdowns", "unsafe_shutdown_count")
    power_cycles = _smart_int(smart, "power_cycles")
    if unsafe and unsafe > 0:
        detail = f"{unsafe} unsafe shutdowns recorded"
        if power_cycles and power_cycles > 0:
            detail += f" ({unsafe * 100.0 / power_cycles:.1f}% of {power_cycles} power cycles)"
        detail += "; historical counter, not proof of a current SSD fault"
        rows.append({"label": "History", "state": "REVIEW", "detail": detail})

    if status == "OK":
        if unsafe and unsafe > 0:
            recommendation = "No immediate SSD repair/replacement is indicated. To see whether unsafe shutdowns are still occurring, capture before/after JSON reports around a normal reboot and run `nvme-doctor diff`."
        else:
            recommendation = "No immediate action is indicated. Save a JSON report as a baseline and compare again if symptoms appear."
    elif status == "INCOMPLETE":
        if snapshot.controller_info.get("nvme_passthrough") is False:
            if snapshot.controller_info.get("passthrough_reason") == "darwin-no-scsi-passthrough":
                if snapshot.controller_info.get("direct_usb"):
                    recommendation = f"Direct RTL9210 access is available. Close applications using the disk, then run `sudo nvme-doctor check {snapshot.controller}`; NVMe Doctor will temporarily unmount/capture the enclosure, read Identify + SMART, release it, and restore its original mount state."
                else:
                    recommendation = "The SSD is present, but smartctl cannot use the SNT pass-through path on macOS. Use a native NVMe path, inspect it on Linux, or use a bridge supported by NVMe Doctor's direct USB backend."
            else:
                recommendation = "The SSD is present, but the USB bridge is hiding NVMe admin/SMART data. Use a passthrough-capable adapter or direct PCIe/M.2 connection for a full diagnosis."
        else:
            recommendation = "Restore the missing primary health evidence (typically run as root and ensure nvme-cli or smartctl can read the controller) before trusting a clean result."
    else:
        actionable = [f.actions[0] for f in findings if f.severity in {"critical", "warning"} and f.actions]
        recommendation = actionable[0] if actionable else "Review the warning/critical findings below before stressing or modifying the drive."

    return {
        "verdict": verdict,
        "headline": headline,
        "near_term_risk": summary["near_term_risk"],
        "trend": summary["trend"],
        "domains": rows,
        "recommendation": recommendation,
    }



def _is_ata_snapshot(snapshot: Snapshot) -> bool:
    protocol = str(snapshot.controller_info.get("protocol") or "").strip().lower()
    return protocol in {"ata", "sata"} or bool(snapshot.capabilities.get("ata_smart"))


def _diagnose_ata(snapshot: Snapshot) -> Report:
    findings: List[Finding] = []
    smart = snapshot.smart or {}
    kernel = _kernel_flags(snapshot.kernel_lines) if snapshot.capabilities.get("targeted_kernel_log", True) else _kernel_flags([])
    state = str(snapshot.controller_info.get("state", "")).strip().lower()
    ata_passthrough_unavailable = snapshot.controller_info.get("ata_passthrough") is False

    if state and state not in {"live", "new", "connecting", "present"} and not (state == "limited" and ata_passthrough_unavailable):
        severity = "critical" if state in {"missing", "dead", "deleting"} else "warning"
        findings.append(Finding(
            "device-state", severity, "Storage device is not fully live", f"Device state is {state!r}.",
            "high", [f"device state={state}"], ["Inspect host/storage logs and physical connectivity before attempting destructive operations."],
        ))

    if ata_passthrough_unavailable:
        findings.append(Finding(
            "ata-passthrough-unavailable", "info",
            "ATA SMART is unavailable through this macOS USB-SATA path",
            "The drive is identified as ATA/SATA, but this USB bridge/path did not expose ATA SMART through either smartctl automatic bridge detection or explicit -d sat on macOS. This is an evidence-access limitation, not a drive-health failure.",
            "high",
            [
                f"transport={snapshot.controller_info.get('transport') or 'USB -> SATA'}",
                f"model={snapshot.controller_info.get('model') or 'unknown'}",
                f"classification={snapshot.controller_info.get('protocol_source') or 'ATA identity'}",
            ],
            [
                "For full ATA SMART data, inspect the SSD through a SAT-capable bridge/path or connect it to native SATA; Linux often provides broader bridge passthrough support.",
                "Do not interpret unavailable SMART over this USB path as evidence that the SSD itself is unhealthy.",
            ],
        ))

    usb = snapshot.controller_info.get("usb_path") if isinstance(snapshot.controller_info.get("usb_path"), dict) else {}
    if usb:
        usb_events = kernel.get("usb_disconnect", []) + kernel.get("uas_error", []) + kernel.get("usb_reset", []) + kernel.get("scsi_io", [])
        if usb_events:
            findings.append(Finding(
                "usb-transport-instability", "warning", "USB storage transport shows reset/disconnect evidence",
                "Target-correlated USB/UAS/SCSI events indicate transport instability independent of the ATA media-health counters.",
                "high" if len(usb_events) >= 2 else "medium",
                ([f"usb_port={usb.get('usb_port')}"] if usb.get("usb_port") else []) + usb_events[-6:],
                ["Retest with a known-good short cable on a direct host port and bypass hubs/docks."],
            ))
        speed = usb.get("speed_mbps")
        version = str(usb.get("usb_version") or "").strip()
        try:
            speed_num = float(speed) if speed is not None else None
            version_num = float(version) if version else None
        except (TypeError, ValueError):
            speed_num = version_num = None
        if version_num is not None and version_num >= 3.0 and speed_num is not None and speed_num <= 480:
            findings.append(Finding(
                "usb-link-downshift", "warning", "USB 3.x bridge is operating at USB 2.0 speed",
                f"The bridge reports USB {version} capability but the current link speed is only {speed_num:g} Mb/s.",
                "high", [f"usb_version={version}", f"speed_mbps={speed_num:g}"],
                ["Replace/reseat the cable and bypass hubs/docks, then verify SuperSpeed negotiation."],
            ))

    passed = smart.get("smart_passed")
    if passed is False:
        findings.append(Finding(
            "ata-smart-failed", "critical", "ATA SMART overall-health test failed",
            "The drive's ATA SMART RETURN STATUS indicates a failing health condition.", "high",
            ["smart_status.passed=false"], ["Back up important data immediately and plan drive replacement."],
        ))

    failed_attrs = smart.get("failed_attributes") if isinstance(smart.get("failed_attributes"), list) else []
    if failed_attrs:
        evidence = []
        for row in failed_attrs[:6]:
            if isinstance(row, dict):
                evidence.append(f"SMART {row.get('id')} {row.get('name')}: value={row.get('value')} thresh={row.get('thresh')} when_failed={row.get('when_failed')}")
        findings.append(Finding(
            "ata-threshold-failure", "critical", "One or more ATA SMART attributes crossed failure thresholds",
            f"{len(failed_attrs)} SMART attribute(s) report a threshold failure.", "high", evidence,
            ["Back up data and inspect the failing attributes before further stress testing."],
        ))

    realloc = _smart_int(smart, "reallocated_sectors") or 0
    pending = _smart_int(smart, "current_pending_sectors") or 0
    offline = _smart_int(smart, "offline_uncorrectable") or 0
    reported = _smart_int(smart, "reported_uncorrectable") or 0
    if pending > 0 or offline > 0:
        findings.append(Finding(
            "ata-unstable-sectors", "critical", "Unstable or uncorrectable SATA sectors are present",
            f"SMART reports pending={pending} and offline_uncorrectable={offline} sector(s).", "high",
            [f"current_pending_sectors={pending}", f"offline_uncorrectable={offline}"],
            ["Back up readable data before running write-based remediation or destructive surface tests."],
        ))
    if realloc > 0:
        findings.append(Finding(
            "ata-reallocated-sectors", "warning", "Reallocated SATA sectors have been recorded",
            f"SMART reports {realloc} reallocated sector(s).", "high", [f"reallocated_sectors={realloc}"],
            ["Track whether the count grows; growth together with pending/uncorrectable sectors is strong replacement evidence."],
        ))
    if reported > 0:
        findings.append(Finding(
            "ata-reported-uncorrectable", "warning", "Reported uncorrectable ATA errors are present",
            f"SMART reports {reported} reported uncorrectable command error(s).", "high", [f"reported_uncorrectable={reported}"],
            ["Correlate with OS I/O errors and back up data if these are recent or increasing."],
        ))

    crc = _smart_int(smart, "udma_crc_errors") or 0
    if crc > 0:
        findings.append(Finding(
            "ata-crc-errors", "warning" if crc >= 10 else "info", "SATA interface CRC errors have been recorded",
            f"SMART UDMA CRC Error Count is {crc}. This is usually a link/cable/backplane signal rather than a media-sector counter.",
            "high", [f"udma_crc_errors={crc}"],
            ["Reseat/replace the SATA cable or USB-SATA path and compare whether the counter stops increasing."],
        ))

    timeouts = _smart_int(smart, "command_timeouts") or 0
    if timeouts > 0:
        findings.append(Finding(
            "ata-command-timeouts", "warning", "ATA command timeouts have been recorded",
            f"SMART Command Timeout raw count is {timeouts}.", "medium", [f"command_timeouts={timeouts}"],
            ["Correlate with host resets, power events, cable/backplane errors and workload timing."],
        ))

    ata_errors = _smart_int(smart, "ata_error_count") or 0
    if ata_errors > 0:
        findings.append(Finding(
            "ata-error-log", "info", "ATA error log contains historical entries",
            f"The ATA SMART error log reports {ata_errors} error entr{'y' if ata_errors == 1 else 'ies'}.", "medium",
            [f"ata_error_count={ata_errors}"], ["Inspect the ATA error log and correlate entries with current counters and OS I/O errors."],
        ))

    reserve = _smart_int(smart, "reserved_space")
    reserve_threshold = _smart_int(smart, "reserved_space_threshold")
    reserve_margin = _smart_int(smart, "reserved_space_margin")
    if reserve is not None and reserve_threshold is not None:
        if reserve <= reserve_threshold:
            findings.append(Finding(
                "ata-reserve-low", "critical", "SSD reserved-space indicator reached its failure threshold",
                f"The explicit ATA reserved-space SMART attribute is {reserve} with a threshold of {reserve_threshold}.",
                "high", [f"reserved_space={reserve}", f"reserved_space_threshold={reserve_threshold}"],
                ["Back up important data and inspect the vendor SMART attributes; reserve exhaustion is replacement evidence."],
            ))
        elif reserve_margin is not None and reserve_margin <= 10:
            findings.append(Finding(
                "ata-reserve-margin-low", "warning", "SSD reserved-space margin is low",
                f"The explicit ATA reserved-space SMART attribute is {reserve}, only {reserve_margin} normalized point(s) above its threshold of {reserve_threshold}.",
                "high", [f"reserved_space={reserve}", f"reserved_space_threshold={reserve_threshold}", f"reserved_space_margin={reserve_margin}"],
                ["Track this attribute over time and plan replacement if the reserve indicator continues to fall."],
            ))

    temp = parse_temperature_c(smart.get("temperature"))
    if temp is not None:
        if temp >= 65:
            findings.append(Finding("temperature-critical", "critical", "Drive temperature is critical", f"Drive temperature is {temp}°C.", "high", [f"temperature={temp}°C"], ["Reduce load and improve cooling before further stress testing."]))
        elif temp >= 55:
            findings.append(Finding("temperature-warning", "warning", "Drive temperature is high", f"Drive temperature is {temp}°C.", "high", [f"temperature={temp}°C"], ["Check airflow, enclosure cooling and workload temperature behavior."]))

    ata_resets = kernel.get("ata_reset", [])
    ata_errors = kernel.get("ata_error", [])
    if ata_resets or ata_errors:
        findings.append(Finding(
            "ata-host-link-errors", "warning", "Host reports SATA link reset/error activity",
            f"Target-scoped host logs contain {len(ata_resets)} SATA reset event(s) and {len(ata_errors)} ATA error event(s).",
            "high", (ata_errors + ata_resets)[-6:],
            ["Correlate these events with CRC/timeout counters, then inspect the SATA cable/backplane/power path before blaming media."],
        ))

    scsi_io = kernel.get("scsi_io", [])
    if scsi_io and not (pending or offline or reported):
        findings.append(Finding(
            "host-io-errors", "warning", "Host reports block I/O errors without matching ATA media counters",
            "Target-scoped host logs contain block I/O errors while the strongest ATA media counters are currently clear, increasing suspicion of transport/power/controller issues.",
            "medium", scsi_io[-4:], ["Inspect SATA/USB cabling, power and controller resets; compare whether SMART counters increase at the same time."],
        ))

    findings.sort(key=lambda f: (SEVERITY_ORDER.get(f.severity, 9), f.code))
    if any(f.severity == "critical" for f in findings):
        status = "CRITICAL"
    elif any(f.severity == "warning" for f in findings):
        status = "WARNING"
    elif not snapshot.capabilities.get("ata_smart") or not snapshot.smart:
        status = "INCOMPLETE"
    else:
        status = "OK"
    return Report(snapshot, findings, status, _build_assessment(snapshot, findings, status))

def diagnose(snapshot: Snapshot) -> Report:
    if _is_ata_snapshot(snapshot):
        return _diagnose_ata(snapshot)

    findings: List[Finding] = []
    smart = snapshot.smart or {}
    pci = snapshot.pci or {}
    power = snapshot.power or {}
    kernel = _kernel_flags(snapshot.kernel_lines) if snapshot.capabilities.get("targeted_kernel_log", True) else _kernel_flags([])

    state = str(snapshot.controller_info.get("state", "")).strip().lower()
    passthrough_unavailable = snapshot.controller_info.get("nvme_passthrough") is False
    if passthrough_unavailable:
        darwin_no_scsi = snapshot.controller_info.get("passthrough_reason") == "darwin-no-scsi-passthrough"
        direct_ready = bool(snapshot.controller_info.get("direct_usb"))
        findings.append(Finding(
            "nvme-passthrough-unavailable",
            "info",
            ("NVMe SMART is available through explicit direct USB mode" if (darwin_no_scsi and direct_ready)
             else "Underlying NVMe health is unavailable through this macOS USB path" if darwin_no_scsi
             else "Underlying NVMe health is hidden by the USB bridge"),
            ("The external SSD is visible through an RTL9210 bridge. smartctl cannot use its SNT path on macOS, but NVMe Doctor can read the underlying NVMe Identify/SMART data by temporarily capturing the USB device when nvme-doctor is run with sudo (or --direct-usb is explicitly requested)."
             if (darwin_no_scsi and direct_ready)
             else "The external physical SSD is visible to macOS, but current smartmontools Darwin builds do not implement the SCSI device pass-through required by sntrealtek/sntjmicron/sntasmedia USB-NVMe bridge backends." if darwin_no_scsi
             else "The external physical SSD is visible to the OS, but NVMe admin/SMART passthrough could not be opened through the USB enclosure."),
            "high",
            [
                f"transport={snapshot.controller_info.get('transport') or 'USB'}",
                f"usb_bridge={snapshot.controller_info.get('usb_bridge') or 'unknown'}",
            ],
            ([
                f"Re-run explicitly as `sudo nvme-doctor check {snapshot.controller}` to temporarily unmount/capture the RTL9210 and read NVMe Identify + SMART.",
                "Direct USB mode is read-only at the NVMe command level, but it temporarily unmounts and reattaches the enclosure; close applications using the disk first.",
            ] if (darwin_no_scsi and direct_ready) else [
                "For full NVMe health/admin data on this device, use a native NVMe path visible to macOS or inspect it on Linux.",
                "Do not interpret macOS diskutil 'SMART Status: Not Supported' as an SSD health failure; it describes unavailable SMART access through this transport.",
            ] if darwin_no_scsi else [
                "Try the enclosure's latest firmware and a direct host USB port/cable, then rerun the check.",
                "For full NVMe health data, use an enclosure/adapter with working NVMe SMART passthrough or connect the SSD directly to PCIe/M.2.",
            ]),
        ))
    elif state and state not in {"live", "new", "connecting", "present"}:
        severity = "critical" if state in {"missing", "dead", "deleting"} else "warning"
        findings.append(Finding(
            "controller-state",
            severity,
            "NVMe controller is not live",
            f"Controller state is {state!r}.",
            "high",
            [f"sysfs controller state: {state}"],
            ["Inspect the OS storage/kernel logs for the first reset, timeout, PCIe, or hotplug event before attempting a reset."],
        ))

    if snapshot.controller_info.get("native_nvme") is False and snapshot.controller_info.get("nvme_passthrough") is True:
        findings.append(Finding(
            "usb-nvme-bridge",
            "info",
            "NVMe is accessed through a USB/SCSI bridge",
            "NVMe SMART/admin passthrough is working through the bridge, but the SSD's native PCIe link, AER counters and NUMA locality are hidden from the host.",
            "high",
            [f"device={snapshot.device_path}", f"transport={snapshot.controller_info.get('transport') or 'USB/SCSI -> NVMe'}"],
            ["Use the SMART/Health results normally; connect the SSD through native PCIe/M.2 only when PCIe-link/AER/NUMA diagnosis is required."],
        ))

    if snapshot.controller_info.get("native_nvme") is False:
        usb = snapshot.controller_info.get("usb_path") if isinstance(snapshot.controller_info.get("usb_path"), dict) else {}
        usb_reset = kernel.get("usb_reset", [])
        usb_disconnect = kernel.get("usb_disconnect", [])
        uas_error = kernel.get("uas_error", [])
        scsi_io = kernel.get("scsi_io", [])
        transport_events = len(usb_reset) + len(usb_disconnect) + len(uas_error)
        if transport_events or scsi_io:
            evidence = []
            if usb.get("usb_port"):
                evidence.append(f"usb_port={usb.get('usb_port')}")
            if usb.get("vid_pid"):
                evidence.append(f"usb_vid_pid={usb.get('vid_pid')}")
            if usb.get("interface_driver"):
                evidence.append(f"usb_driver={usb.get('interface_driver')}")
            evidence += (usb_disconnect + uas_error + usb_reset + scsi_io)[-6:]
            findings.append(Finding(
                "usb-transport-instability",
                "warning",
                "USB storage transport shows reset/disconnect evidence",
                "Target-correlated host logs contain USB/UAS/SCSI transport recovery or I/O events. When NVMe media SMART remains clean, this evidence points more strongly to the enclosure, cable, port, power delivery, or USB host path than to NAND media.",
                "high" if transport_events >= 2 or scsi_io else "medium",
                evidence,
                [
                    "Retest the enclosure on a direct host USB port with a known-good short cable and avoid hubs/docks for the comparison.",
                    "Compare the same SSD on native PCIe/M.2 if the resets continue; preserve the before/after nvme-doctor reports.",
                ],
            ))

        speed = usb.get("speed_mbps")
        version = str(usb.get("usb_version") or "").strip()
        try:
            speed_num = float(speed) if speed is not None else None
        except (TypeError, ValueError):
            speed_num = None
        try:
            version_num = float(version) if version else None
        except ValueError:
            version_num = None
        if version_num is not None and version_num >= 3.0 and speed_num is not None and speed_num <= 480:
            findings.append(Finding(
                "usb-link-downshift",
                "warning",
                "USB 3.x bridge is operating at USB 2.0 speed",
                f"The bridge reports USB {version} capability but the current sysfs link speed is only {speed_num:g} Mb/s.",
                "high",
                [
                    f"usb_version={version}",
                    f"speed_mbps={speed_num:g}",
                    f"usb_port={usb.get('usb_port') or 'unknown'}",
                ],
                ["Replace/reseat the cable and bypass hubs/docks, then confirm the link returns to SuperSpeed (5 Gb/s or faster)."],
            ))

        if str(usb.get("interface_driver") or "").lower() == "usb-storage":
            findings.append(Finding(
                "usb-bot-transport",
                "info",
                "USB mass storage is using usb-storage/BOT",
                "The host bound the enclosure to the bulk-only usb-storage path rather than UAS. This is not a health failure, but it can limit queueing/performance and is useful when comparing bridge behavior between ports/hosts.",
                "high",
                [f"interface_driver={usb.get('interface_driver')}", f"usb_port={usb.get('usb_port') or 'unknown'}"],
                ["If the enclosure is expected to support UAS, compare on another host/port and check whether the bridge is being quirked to usb-storage."],
            ))

    critical_warning = _smart_int(smart, "critical_warning", "critical_warning_raw")
    if critical_warning:
        decoded = _decode_critical_warning(critical_warning)
        findings.append(Finding(
            "nvme-critical-warning",
            "critical",
            "NVMe SMART critical warning is set",
            f"The controller reports critical_warning=0x{critical_warning:02x}" +
            ((": " + "; ".join(decoded)) if decoded else "."),
            "high",
            [f"critical_warning=0x{critical_warning:02x}"] + [f"bit: {item}" for item in decoded],
            ["Back up important data before stress testing or firmware changes.", "Correlate the asserted warning bit(s) with SMART, error-log and host evidence before taking destructive action."],
        ))

    spare = _smart_int(smart, "avail_spare", "available_spare")
    spare_threshold = _smart_int(smart, "spare_thresh", "available_spare_threshold")
    if spare is not None and spare_threshold is not None and spare < spare_threshold:
        findings.append(Finding(
            "available-spare",
            "critical",
            "Available spare is below the controller threshold",
            f"Available spare is {spare}% while the threshold is {spare_threshold}%.",
            "high",
            [f"available_spare={spare}%", f"spare_threshold={spare_threshold}%"],
            ["Treat this as a drive-health issue and plan replacement after securing data."],
        ))

    media_errors = _smart_int(smart, "media_errors", "media_and_data_integrity_errors")
    if media_errors and media_errors > 0:
        severity = "critical" if media_errors >= 100 else "warning"
        findings.append(Finding(
            "media-errors",
            severity,
            "NVMe media/data-integrity errors were recorded",
            f"The SMART log reports {media_errors} media/data-integrity error(s).",
            "high",
            [f"media_errors={media_errors}"],
            ["Back up important data and inspect the NVMe error log.", "Do not attribute these errors to PCIe power management without additional evidence."],
        ))

    used = _smart_int(smart, "percent_used", "percentage_used")
    if used is not None:
        if used >= 100:
            findings.append(Finding(
                "endurance-used",
                "critical",
                "Rated endurance has been consumed",
                f"Percentage Used is {used}%.",
                "high",
                [f"percentage_used={used}%"],
                ["Plan drive replacement and verify workload write volume."],
            ))
        elif used >= 90:
            findings.append(Finding(
                "endurance-near-limit",
                "warning",
                "Drive endurance is near its rated limit",
                f"Percentage Used is {used}%.",
                "high",
                [f"percentage_used={used}%"],
                ["Plan replacement and check write amplification/workload behavior."],
            ))
        elif used >= 80:
            findings.append(Finding(
                "endurance-high",
                "info",
                "Drive endurance usage is high",
                f"Percentage Used is {used}%.",
                "high",
                [f"percentage_used={used}%"],
                ["Track this value over time; no immediate action is implied by this value alone."],
            ))

    temp = parse_temperature_c(first(smart, "temperature", "composite_temperature"))
    id_ctrl = snapshot.controller_info.get("nvme_id_ctrl")
    warning_temp = critical_temp = None
    if isinstance(id_ctrl, dict):
        warning_temp = parse_temperature_c(first(id_ctrl, "wctemp", "warning_comp_temp_threshold"))
        critical_temp = parse_temperature_c(first(id_ctrl, "cctemp", "critical_comp_temp_threshold"))
    if temp is not None:
        if (critical_temp is not None and temp >= critical_temp) or temp >= 85:
            findings.append(Finding(
                "temperature-critical",
                "critical",
                "NVMe temperature is critical",
                f"Composite temperature is {temp}°C.",
                "high",
                [f"temperature={temp}°C"] + ([f"controller critical threshold={critical_temp}°C"] if critical_temp else []),
                ["Reduce load and verify airflow/heatsink contact before further stress testing."],
            ))
        elif (warning_temp is not None and temp >= warning_temp) or temp >= 70:
            findings.append(Finding(
                "temperature-warning",
                "warning",
                "NVMe temperature is high",
                f"Composite temperature is {temp}°C.",
                "high",
                [f"temperature={temp}°C"] + ([f"controller warning threshold={warning_temp}°C"] if warning_temp else []),
                ["Check cooling, airflow, and whether thermal throttling coincides with performance drops."],
            ))

    corrected, nonfatal, fatal = _aer_totals(pci)
    if fatal > 0:
        findings.append(Finding(
            "pcie-aer-fatal",
            "critical",
            "Fatal PCIe AER errors are recorded",
            f"PCIe sysfs counters contain {fatal} fatal error(s).",
            "high",
            [f"fatal AER count={fatal}", f"BDF={pci.get('bdf', 'unknown')}"],
            ["Inspect the endpoint, upstream bridge/root-port AER logs, cabling/backplane/adapter, and firmware before blaming the filesystem."],
        ))
    if nonfatal > 0:
        findings.append(Finding(
            "pcie-aer-nonfatal",
            "warning",
            "Uncorrectable non-fatal PCIe AER errors are recorded",
            f"PCIe sysfs counters contain {nonfatal} uncorrectable non-fatal error(s).",
            "high",
            [f"nonfatal AER count={nonfatal}", f"BDF={pci.get('bdf', 'unknown')}"],
            ["Correlate timestamps with NVMe resets/timeouts and inspect the PCIe path."],
        ))
    if corrected > 0:
        findings.append(Finding(
            "pcie-aer-corrected",
            "warning" if corrected >= 100 else "info",
            "Corrected PCIe AER errors are present",
            f"PCIe sysfs counters contain {corrected} corrected error(s).",
            "medium",
            [f"corrected AER count={corrected}", f"BDF={pci.get('bdf', 'unknown')}"],
            ["Watch whether the counter increases under load or across warm reboot; a rising count can indicate signal/link instability."],
        ))

    cur_speed = parse_pcie_speed(pci.get("current_link_speed"))
    max_speed = parse_pcie_speed(pci.get("max_link_speed"))
    cur_width = parse_pcie_width(pci.get("current_link_width"))
    max_width = parse_pcie_width(pci.get("max_link_width"))
    degraded = []
    if cur_speed is not None and max_speed is not None and cur_speed + 0.01 < max_speed:
        degraded.append(f"link speed {cur_speed:g} GT/s < maximum {max_speed:g} GT/s")
    if cur_width is not None and max_width is not None and cur_width < max_width:
        degraded.append(f"link width x{cur_width} < maximum x{max_width}")
    if degraded:
        findings.append(Finding(
            "pcie-link-degraded",
            "warning",
            "PCIe link is negotiated below the endpoint maximum",
            "; ".join(degraded) + ".",
            "medium",
            degraded,
            ["Check slot/adapter/backplane capabilities, BIOS settings, link errors, and whether the link retrains down under load."],
        ))

    timeout_count = len(kernel["timeout"])
    reset_count = len(kernel["reset"])
    down_count = len(kernel["down"])
    link_count = len(kernel["link"])
    uncorrected_log_count = len(kernel["aer_uncorrected"])

    if down_count:
        findings.append(Finding(
            "kernel-controller-down",
            "critical",
            "Kernel reports controller-down or failed-reset symptoms",
            f"Found {down_count} high-severity controller failure event(s) in the current boot log.",
            "high",
            kernel["down"][-3:],
            ["Preserve the log before rebooting; inspect the earliest preceding timeout/AER/link event to identify the trigger."],
        ))
    elif timeout_count or reset_count:
        findings.append(Finding(
            "kernel-reset-timeout",
            "warning",
            "NVMe reset/timeout activity is present in the kernel log",
            f"Found {timeout_count} timeout event(s) and {reset_count} reset event(s) in the selected boot log.",
            "high",
            (kernel["timeout"] + kernel["reset"])[-4:],
            ["Correlate these events with PCIe AER/link state, workload, temperature, and power-management transitions."],
        ))

    # Correlation: reset/timeouts with AER/link evidence and little/no media-health evidence.
    pcie_evidence = fatal + nonfatal + corrected + uncorrected_log_count + link_count
    if (timeout_count or reset_count or down_count) and pcie_evidence > 0 and not media_errors and not critical_warning:
        evidence = []
        if timeout_count or reset_count:
            evidence.append(f"kernel: {timeout_count} timeout(s), {reset_count} reset(s)")
        if fatal or nonfatal or corrected:
            evidence.append(f"PCIe AER counters: corrected={corrected}, nonfatal={nonfatal}, fatal={fatal}")
        if link_count:
            evidence.append(f"kernel: {link_count} PCIe link/DPC event(s)")
        evidence.append("SMART does not currently show media errors or a critical warning")
        findings.append(Finding(
            "likely-pcie-path",
            "warning" if not down_count else "critical",
            "PCIe path instability is a stronger suspect than media failure",
            "NVMe reset/timeout symptoms coincide with PCIe error/link evidence while SMART media-health indicators are clean.",
            "high" if (fatal or nonfatal or link_count or uncorrected_log_count) else "medium",
            evidence,
            ["Test the same drive/adapter in another slot or path if possible.", "Check upstream bridge/root-port AER counters and firmware.", "Compare cold boot versus warm reboot behavior before changing filesystem or NVMe format settings."],
        ))

    # Correlation: timeouts/resets + APST/ASPM enabled -> suggest a reversible diagnostic test, not an automatic fix.
    apst_latency = to_int(power.get("nvme_default_ps_max_latency_us"))
    aspm_policy = _active_aspm(str(power.get("pcie_aspm_policy", "")))
    if (timeout_count or reset_count or down_count) and ((apst_latency is not None and apst_latency > 0) or (aspm_policy and aspm_policy != "performance")):
        evidence = []
        if apst_latency is not None:
            evidence.append(f"nvme_core.default_ps_max_latency_us={apst_latency}")
        if aspm_policy:
            evidence.append(f"active PCIe ASPM policy={aspm_policy}")
        findings.append(Finding(
            "power-state-hypothesis",
            "info",
            "Power-state/link recovery is worth testing",
            "Power management is enabled while reset/timeout symptoms are present. This is a hypothesis, not proof of causality.",
            "low",
            evidence,
            ["For diagnosis only, compare behavior with a temporary boot using `nvme_core.default_ps_max_latency_us=0` and/or `pcie_aspm=off`; revert after the test.", "Prefer a controlled A/B test over permanently disabling power management."],
        ))

    unsafe = _smart_int(smart, "unsafe_shutdowns", "unsafe_shutdown_count")
    power_cycles = _smart_int(smart, "power_cycles")
    if unsafe and unsafe > 0:
        ratio_text = ""
        evidence = [f"unsafe_shutdowns={unsafe}"]
        if power_cycles and power_cycles > 0:
            ratio = unsafe * 100.0 / power_cycles
            ratio_text = f" ({ratio:.1f}% of {power_cycles} recorded power cycles)"
            evidence.append(f"power_cycles={power_cycles}")
            evidence.append(f"unsafe_shutdown_ratio={ratio:.1f}%")
        findings.append(Finding(
            "unsafe-shutdowns",
            "info",
            "Unsafe shutdowns have been recorded",
            f"SMART reports {unsafe} unsafe shutdown(s){ratio_text}. The NVMe SMART counter does not store a per-event reason or timestamp.",
            "high",
            evidence,
            [
                "Treat this as historical host/power context; it does not by itself prove a current drive fault.",
                "An unsafe-shutdown increment means the controller did not receive the normal NVMe shutdown notification before power was lost; correlate new increments with host reboot/power logs.",
                "For a reproducible reboot/power event, save JSON reports before and after it and run `nvme-doctor diff before.json after.json` to see whether this counter increased.",
            ],
        ))

    error_entries = _smart_int(smart, "num_err_log_entries", "error_information_log_entries")
    if error_entries and error_entries > 0:
        findings.append(Finding(
            "error-log-history",
            "info",
            "NVMe error-information log has entries",
            f"SMART reports {error_entries} error-information log entr{'y' if error_entries == 1 else 'ies'}.",
            "medium",
            [f"error_information_log_entries={error_entries}"],
            ["Inspect `nvme error-log`; entries can be historical, so correlate them with timestamps/counters rather than assuming an active failure."],
        ))

    findings.sort(key=lambda f: (SEVERITY_ORDER.get(f.severity, 9), f.code))
    if any(f.severity == "critical" for f in findings):
        status = "CRITICAL"
    elif any(f.severity == "warning" for f in findings):
        status = "WARNING"
    elif not snapshot.smart:
        # Never issue a clean bill of health without standards-defined NVMe
        # health evidence (from nvme-cli or smartctl fallback).
        status = "INCOMPLETE"
    else:
        status = "OK"
    return Report(snapshot, findings, status, _build_assessment(snapshot, findings, status))

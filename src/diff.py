# SPDX-License-Identifier: GPL-3.0-or-later
from __future__ import annotations

from typing import Any, Dict, List, Optional

from .util import first, format_bytes_decimal, nvme_data_units_to_bytes, pcie_generation, to_int


_COUNTERS = [
    ("Power cycles", ("power_cycles",), "count"),
    ("Power-on hours", ("power_on_hours",), "hours"),
    ("Unsafe shutdowns", ("unsafe_shutdowns", "unsafe_shutdown_count"), "count"),
    ("Media errors", ("media_errors", "media_and_data_integrity_errors"), "count"),
    ("Error log entries", ("num_err_log_entries", "error_information_log_entries"), "count"),
    ("Data read", ("data_units_read",), "data_units"),
    ("Data written", ("data_units_written",), "data_units"),
    ("Warning-temp time", ("warning_temp_time",), "minutes"),
    ("Critical-temp time", ("critical_comp_time",), "minutes"),
    ("Thermal mgmt T1", ("thm_temp1_trans_count", "thermal_mgmt_temp1_transition_count"), "count"),
    ("Thermal mgmt T2", ("thm_temp2_trans_count", "thermal_mgmt_temp2_transition_count"), "count"),
]


def _snapshot(report: Dict[str, Any]) -> Dict[str, Any]:
    value = report.get("snapshot")
    return value if isinstance(value, dict) else {}


def _fmt_delta(delta: int, kind: str) -> str:
    sign = "+" if delta >= 0 else ""
    if kind == "data_units":
        amount = format_bytes_decimal(abs(nvme_data_units_to_bytes(delta) or 0)) or "0 B"
        return ("+" if delta >= 0 else "-") + amount
    if kind == "hours":
        return f"{sign}{delta:,} h"
    if kind == "minutes":
        return f"{sign}{delta:,} min"
    return f"{sign}{delta:,}"


def compare_reports(before: Dict[str, Any], after: Dict[str, Any]) -> Dict[str, Any]:
    bs = _snapshot(before)
    a_s = _snapshot(after)
    bsmart = bs.get("smart") if isinstance(bs.get("smart"), dict) else {}
    asmart = a_s.get("smart") if isinstance(a_s.get("smart"), dict) else {}

    changes: List[Dict[str, Any]] = []
    by_label: Dict[str, Dict[str, Any]] = {}
    for label, keys, kind in _COUNTERS:
        b = to_int(first(bsmart, *keys))
        a = to_int(first(asmart, *keys))
        if b is None or a is None:
            continue
        entry = {"label": label, "before": b, "after": a, "delta": a - b, "kind": kind}
        changes.append(entry)
        by_label[label] = entry

    bpci = bs.get("pci") if isinstance(bs.get("pci"), dict) else {}
    apci = a_s.get("pci") if isinstance(a_s.get("pci"), dict) else {}
    bgen = pcie_generation(bpci.get("current_link_speed"))
    agen = pcie_generation(apci.get("current_link_speed"))
    bw = to_int(bpci.get("current_link_width"))
    aw = to_int(apci.get("current_link_width"))

    interpretations: List[Dict[str, str]] = []
    unsafe = by_label.get("Unsafe shutdowns")
    cycles = by_label.get("Power cycles")
    if unsafe and unsafe["delta"] > 0:
        detail = f"Unsafe-shutdown counter increased by {unsafe['delta']} during the captured interval."
        if cycles and cycles["delta"] > 0:
            detail += f" Power cycles increased by {cycles['delta']}."
        detail += " NVMe SMART does not record the individual reason or timestamp; the increment proves only that normal NVMe shutdown notification was not received before the relevant power-loss event(s)."
        interpretations.append({"severity": "info", "title": "Unsafe shutdown occurred in this interval", "detail": detail})

    media = by_label.get("Media errors")
    if media and media["delta"] > 0:
        interpretations.append({
            "severity": "warning",
            "title": "Media/data-integrity errors increased",
            "detail": f"Media error count increased by {media['delta']} in the captured interval.",
        })

    errs = by_label.get("Error log entries")
    if errs and errs["delta"] > 0:
        interpretations.append({
            "severity": "info",
            "title": "NVMe error-log activity increased",
            "detail": f"Error-information log count increased by {errs['delta']} in the captured interval; inspect the after-report error records.",
        })

    if bgen is not None and agen is not None and bgen != agen:
        severity = "warning" if agen < bgen else "info"
        interpretations.append({
            "severity": severity,
            "title": "PCIe generation changed",
            "detail": f"Negotiated PCIe generation changed from Gen{bgen} to Gen{agen}.",
        })
    if bw is not None and aw is not None and bw != aw:
        severity = "warning" if aw < bw else "info"
        interpretations.append({
            "severity": severity,
            "title": "PCIe link width changed",
            "detail": f"Negotiated PCIe width changed from x{bw} to x{aw}.",
        })

    after_codes = {f.get("code") for f in after.get("findings", []) if isinstance(f, dict)}
    if unsafe and unsafe["delta"] > 0 and "likely-pcie-path" in after_codes:
        interpretations.append({
            "severity": "warning",
            "title": "Unsafe shutdown coincides with PCIe-path evidence",
            "detail": "The after-report also diagnoses PCIe-path instability. This correlation is stronger than the unsafe-shutdown counter alone, but it still does not prove the exact power-loss cause.",
        })

    return {
        "schema_version": 1,
        "before_collected_at": bs.get("collected_at"),
        "after_collected_at": a_s.get("collected_at"),
        "before_device": bs.get("device_path"),
        "after_device": a_s.get("device_path"),
        "changes": changes,
        "pcie": {
            "before_generation": bgen,
            "after_generation": agen,
            "before_width": bw,
            "after_width": aw,
        },
        "interpretations": interpretations,
    }


def render_diff_text(diff: Dict[str, Any]) -> str:
    lines = ["NVMe Doctor  |  CHANGE REPORT", "", "INTERVAL"]
    lines.append(f"  Before               {diff.get('before_collected_at') or '-'}")
    lines.append(f"  After                {diff.get('after_collected_at') or '-'}")
    lines.append(f"  Device               {diff.get('after_device') or diff.get('before_device') or '-'}")
    lines += ["", "COUNTER CHANGES"]
    meaningful = False
    for item in diff.get("changes", []):
        delta = item.get("delta", 0)
        if delta == 0:
            continue
        meaningful = True
        lines.append(
            f"  {item['label']:<20} {_fmt_delta(delta, item['kind']):>12}  "
            f"({item['before']:,} -> {item['after']:,})"
        )
    if not meaningful:
        lines.append("  No tracked SMART lifetime counter changed.")

    pcie = diff.get("pcie", {})
    if pcie.get("before_generation") != pcie.get("after_generation") or pcie.get("before_width") != pcie.get("after_width"):
        lines += ["", "PCIe LINK CHANGE"]
        bg, ag = pcie.get("before_generation"), pcie.get("after_generation")
        bw, aw = pcie.get("before_width"), pcie.get("after_width")
        if bg is not None or ag is not None:
            lines.append(f"  Generation           {('Gen'+str(bg)) if bg else '-'} -> {('Gen'+str(ag)) if ag else '-'}")
        if bw is not None or aw is not None:
            lines.append(f"  Width                {('x'+str(bw)) if bw else '-'} -> {('x'+str(aw)) if aw else '-'}")

    lines += ["", "INTERPRETATION"]
    interpretations = diff.get("interpretations", [])
    if not interpretations:
        lines.append("  No high-signal change was detected in the compared counters/link state.")
    else:
        for idx, item in enumerate(interpretations, 1):
            lines.append(f"  {idx}. [{str(item.get('severity', 'info')).upper()}] {item.get('title', '')}")
            lines.append(f"     {item.get('detail', '')}")
    return "\n".join(lines) + "\n"

# SPDX-License-Identifier: GPL-3.0-or-later
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Tuple


_NVME_RE = re.compile(r"^(nvme\d+)(?:n\d+(?:p\d+)?)?$")
_LINUX_SD_RE = re.compile(r"^(sd[a-z]+)(?:\d+)?$")
_MAC_DISK_RE = re.compile(r"^(?:r)?(disk\d+)(?:s\d+)?$")
_BDF_RE = re.compile(r"^(?:[0-9a-fA-F]{4}:)?[0-9a-fA-F]{2}:[0-9a-fA-F]{2}\.[0-7]$")


def controller_from_device(value: str) -> str:
    name = Path(value).name
    match = _NVME_RE.match(name)
    if not match:
        raise ValueError(f"not an NVMe controller or namespace: {value}")
    return match.group(1)


def normalize_device(value: str) -> Tuple[str, str]:
    """Normalize a Linux NVMe target.

    Native NVMe controller/namespace names are normalized to /dev/nvmeX.
    USB/SCSI-translated NVMe devices may appear as /dev/sdX; whole-device and
    partition spellings are accepted and normalized to the whole block device.
    Whether an sdX target is actually NVMe is verified by the Linux collector
    through smartctl rather than assumed from its block-device name.
    """
    name = Path(value).name
    match = _NVME_RE.match(name)
    if match:
        controller = match.group(1)
        return controller, f"/dev/{controller}"
    match = _LINUX_SD_RE.match(name)
    if match:
        disk = match.group(1)
        return disk, f"/dev/{disk}"
    raise ValueError(f"not a Linux NVMe controller/namespace or supported USB/SCSI block target: {value}")


def normalize_macos_device(value: str) -> Tuple[str, str]:
    """Normalize a macOS BSD disk name to /dev/diskN.

    Partitions/slices and raw-disk names are accepted but normalized to the
    whole disk because NVMe SMART data belongs to the controller/device.
    """
    name = Path(value).name
    match = _MAC_DISK_RE.match(name)
    if not match:
        raise ValueError(f"not a macOS whole-disk or disk slice name: {value}")
    disk = match.group(1)
    return disk, f"/dev/{disk}"


def is_bdf(value: str) -> bool:
    return bool(_BDF_RE.match(value))


def read_text(path: Path) -> Optional[str]:
    try:
        return path.read_text(errors="replace").strip()
    except (OSError, UnicodeError):
        return None


def read_json(text: str) -> Any:
    try:
        return json.loads(text)
    except (TypeError, json.JSONDecodeError):
        return None


def to_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    text = str(value).strip().replace(",", "")
    if not text:
        return None
    if text.lower().startswith("0x"):
        try:
            return int(text, 16)
        except ValueError:
            return None
    match = re.search(r"[-+]?\d+", text)
    if not match:
        return None
    try:
        return int(match.group(0))
    except ValueError:
        return None



def smartctl_percent(value: Any) -> Optional[int]:
    """Normalize smartctl percentage values that may be scalars or JSON objects.

    Some ATA SSD vendors expose fields such as ``spare_available`` and
    ``endurance_used`` as ``{"current_percent": N}`` instead of a scalar.
    Keep this strict rather than relying on ``to_int(dict)`` string parsing.
    """
    if isinstance(value, dict):
        for key in ("current_percent", "percent", "value"):
            if key in value:
                return to_int(value.get(key))
        return None
    return to_int(value)


def ata_smart_attributes(payload: Any) -> list[Dict[str, Any]]:
    """Return ATA SMART attribute rows from a smartctl JSON payload."""
    if not isinstance(payload, dict):
        return []
    attrs = payload.get("ata_smart_attributes")
    table = attrs.get("table") if isinstance(attrs, dict) else None
    return [row for row in table if isinstance(row, dict)] if isinstance(table, list) else []


def _ata_attr_raw_value(row: Dict[str, Any]) -> Optional[int]:
    raw = row.get("raw")
    if isinstance(raw, dict):
        return to_int(raw.get("value"))
    return to_int(raw)


def ata_reserved_space_indicator(payload: Any) -> Optional[Dict[str, Any]]:
    """Find an explicit ATA reserved/spare-space SMART attribute.

    smartctl may synthesize top-level ``spare_available`` from unrelated
    normalized attributes (notably SMART 5 Reallocated_Sector_Ct).  That is
    not safe to interpret as a literal spare-NAND percentage.  Prefer only
    attributes whose *name itself* explicitly describes available reserved
    or spare space.
    """
    candidates = []
    for row in ata_smart_attributes(payload):
        name = str(row.get("name") or "").strip()
        norm = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
        explicit = (
            ("available" in norm and ("reserv" in norm or "spare" in norm))
            or ("remaining" in norm and "spare" in norm)
            or ("spare" in norm and "block" in norm)
        )
        if not explicit or "realloc" in norm or "retir" in norm:
            continue
        value = to_int(row.get("value"))
        if value is None:
            continue
        attr_id = to_int(row.get("id"))
        # Prefer well-known explicit reserve IDs when several aliases exist.
        priority = 0 if attr_id in {170, 232} else 1
        candidates.append((priority, attr_id if attr_id is not None else 9999, row))
    if not candidates:
        return None
    candidates.sort(key=lambda x: (x[0], x[1]))
    row = candidates[0][2]
    value = to_int(row.get("value"))
    threshold = to_int(row.get("thresh"))
    result: Dict[str, Any] = {
        "value": value,
        "threshold": threshold,
        "worst": to_int(row.get("worst")),
        "raw": _ata_attr_raw_value(row),
        "attribute_id": to_int(row.get("id")),
        "attribute_name": row.get("name"),
    }
    if value is not None and threshold is not None:
        result["margin"] = value - threshold
    # Preserve duplicate/alias sources for expert JSON output.
    sources = []
    for _, _, src in candidates:
        sources.append({
            "id": to_int(src.get("id")),
            "name": src.get("name"),
            "value": to_int(src.get("value")),
            "worst": to_int(src.get("worst")),
            "threshold": to_int(src.get("thresh")),
            "raw": _ata_attr_raw_value(src),
        })
    result["sources"] = sources
    return result


def ata_media_wear_indicator(payload: Any) -> Optional[Dict[str, Any]]:
    """Return an explicit normalized ATA media-wear/life indicator.

    The number remains vendor-defined, so expose it as a normalized indicator
    rather than converting it to a generic ``percent used`` value.
    """
    candidates = []
    for row in ata_smart_attributes(payload):
        name = str(row.get("name") or "").strip()
        norm = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
        if not any(token in norm for token in ("wearout_indicator", "media_wear", "wear_indicator", "life_left", "remaining_life")):
            continue
        value = to_int(row.get("value"))
        if value is None:
            continue
        attr_id = to_int(row.get("id"))
        priority = 0 if attr_id in {233, 231} else 1
        candidates.append((priority, attr_id if attr_id is not None else 9999, row))
    if not candidates:
        return None
    candidates.sort(key=lambda x: (x[0], x[1]))
    row = candidates[0][2]
    return {
        "value": to_int(row.get("value")),
        "threshold": to_int(row.get("thresh")),
        "worst": to_int(row.get("worst")),
        "raw": _ata_attr_raw_value(row),
        "attribute_id": to_int(row.get("id")),
        "attribute_name": row.get("name"),
    }

def first(mapping: Dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in mapping:
            return mapping[key]
    return None


def parse_temperature_c(value: Any) -> Optional[int]:
    number = to_int(value)
    if number is None:
        return None
    # Some tools expose Kelvin while nvme-cli usually exposes Celsius.
    if 200 <= number <= 500:
        number -= 273
    return number


def parse_pcie_speed(value: Any) -> Optional[float]:
    if value is None:
        return None
    match = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*GT/s", str(value), re.I)
    return float(match.group(1)) if match else None


def parse_pcie_width(value: Any) -> Optional[int]:
    if value is None:
        return None
    text = str(value).strip()
    match = re.search(r"x\s*(\d+)", text, re.I)
    if match:
        return int(match.group(1))
    return to_int(text)



def pcie_generation(value: Any) -> Optional[int]:
    """Return the PCIe generation implied by a link rate.

    Linux sysfs reports PCIe link speed in GT/s.  Keep this mapping tolerant
    of minor formatting/rounding differences and future-proof through Gen6.
    """
    speed = parse_pcie_speed(value)
    if speed is None:
        return None
    generations = [
        (2.5, 1),
        (5.0, 2),
        (8.0, 3),
        (16.0, 4),
        (32.0, 5),
        (64.0, 6),
    ]
    # sysfs values are normally exact decimal rates, but tolerate a few percent
    # because other platform backends may stringify them differently.
    best = min(generations, key=lambda item: abs(item[0] - speed))
    return best[1] if abs(best[0] - speed) <= max(0.15, best[0] * 0.03) else None


def format_bytes_decimal(value: Any) -> Optional[str]:
    number = to_int(value)
    if number is None or number < 0:
        return None
    units = [(10**15, "PB"), (10**12, "TB"), (10**9, "GB"), (10**6, "MB"), (10**3, "kB")]
    for scale, suffix in units:
        if number >= scale:
            return f"{number / scale:.2f} {suffix}"
    return f"{number} B"


def nvme_data_units_to_bytes(value: Any) -> Optional[int]:
    """Convert NVMe SMART Data Units Read/Written to bytes.

    One NVMe data unit is 1000 * 512 bytes.
    """
    units = to_int(value)
    if units is None or units < 0:
        return None
    return units * 512_000


def format_duration_minutes(value: Any) -> Optional[str]:
    minutes = to_int(value)
    if minutes is None or minutes < 0:
        return None
    if minutes < 60:
        return f"{minutes} min"
    hours, rem = divmod(minutes, 60)
    if hours < 48:
        return f"{hours} h {rem} min" if rem else f"{hours} h"
    days, hours = divmod(hours, 24)
    return f"{days} d {hours} h" if hours else f"{days} d"


def format_nvme_version(value: Any) -> Optional[str]:
    """Format textual, packed, or smartctl-style NVMe version values.

    smartctl may expose the version as e.g.
    {"string": "2.0", "value": 131072}; prefer the human string but
    retain the packed-value fallback for other producers.
    """
    if value is None:
        return None
    if isinstance(value, dict):
        for key in ("string", "version", "text"):
            nested = value.get(key)
            if nested not in (None, ""):
                formatted = format_nvme_version(nested)
                if formatted:
                    return formatted
        for key in ("value", "raw"):
            nested = value.get(key)
            if nested is not None:
                formatted = format_nvme_version(nested)
                if formatted:
                    return formatted
        return None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        # smartctl commonly returns strings such as "1.4" or "2.0".
        if re.fullmatch(r"\d+(?:\.\d+){1,2}", text):
            return text
        if text.lower().startswith("0x"):
            try:
                value = int(text, 16)
            except ValueError:
                return text
        elif text.isdigit():
            value = int(text)
        else:
            return text
    if isinstance(value, int):
        major = (value >> 16) & 0xFFFF
        minor = (value >> 8) & 0xFF
        tertiary = value & 0xFF
        if major == 0 and minor == 0 and tertiary == 0:
            return None
        return f"{major}.{minor}" + (f".{tertiary}" if tertiary else "")
    return str(value)

def parse_counter_blob(text: str) -> Dict[str, int]:
    result: Dict[str, int] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        match = re.match(r"(.+?)\s+(-?\d+)\s*$", line)
        if match:
            result[match.group(1).strip()] = int(match.group(2))
            continue
        match = re.match(r"([^:=]+)[:=]\s*(-?\d+)\s*$", line)
        if match:
            result[match.group(1).strip()] = int(match.group(2))
    return result


def sum_positive(values: Iterable[Any]) -> int:
    total = 0
    for value in values:
        number = to_int(value)
        if number is not None and number > 0:
            total += number
    return total

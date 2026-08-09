# SPDX-License-Identifier: GPL-3.0-or-later
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Tuple


_NVME_RE = re.compile(r"^(nvme\d+)(?:n\d+(?:p\d+)?)?$")
_MAC_DISK_RE = re.compile(r"^(?:r)?(disk\d+)(?:s\d+)?$")
_BDF_RE = re.compile(r"^(?:[0-9a-fA-F]{4}:)?[0-9a-fA-F]{2}:[0-9a-fA-F]{2}\.[0-7]$")


def controller_from_device(value: str) -> str:
    name = Path(value).name
    match = _NVME_RE.match(name)
    if not match:
        raise ValueError(f"not an NVMe controller or namespace: {value}")
    return match.group(1)


def normalize_device(value: str) -> Tuple[str, str]:
    """Normalize a Linux NVMe controller/namespace to its controller device."""
    controller = controller_from_device(value)
    return controller, f"/dev/{controller}"


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

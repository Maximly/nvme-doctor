# SPDX-License-Identifier: GPL-3.0-or-later
from __future__ import annotations

import platform
from typing import Optional


def platform_key(name: Optional[str] = None) -> str:
    value = (name or platform.system()).strip().lower()
    if value == "darwin":
        return "darwin"
    if value == "linux":
        return "linux"
    return value or "unknown"


def platform_label(name: Optional[str] = None) -> str:
    key = platform_key(name)
    return {"darwin": "macOS", "linux": "Linux"}.get(key, key or "unknown")

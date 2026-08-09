# SPDX-License-Identifier: GPL-3.0-or-later
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


@dataclass
class CommandResult:
    argv: List[str]
    returncode: int
    stdout: str = ""
    stderr: str = ""
    available: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class Finding:
    code: str
    severity: str
    title: str
    summary: str
    confidence: str = "medium"
    evidence: List[str] = field(default_factory=list)
    actions: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class Snapshot:
    requested_device: str
    controller: str
    device_path: str
    collected_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    host: Dict[str, Any] = field(default_factory=dict)
    controller_info: Dict[str, Any] = field(default_factory=dict)
    smart: Dict[str, Any] = field(default_factory=dict)
    error_log: Any = field(default_factory=list)
    pci: Dict[str, Any] = field(default_factory=dict)
    power: Dict[str, Any] = field(default_factory=dict)
    topology: List[Dict[str, Any]] = field(default_factory=list)
    kernel_lines: List[str] = field(default_factory=list)
    tools: Dict[str, Any] = field(default_factory=dict)
    capabilities: Dict[str, Any] = field(default_factory=dict)
    collection_notes: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class Report:
    snapshot: Snapshot
    findings: List[Finding]
    status: str
    assessment: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": 1,
            "status": self.status,
            "assessment": self.assessment,
            "snapshot": self.snapshot.to_dict(),
            "findings": [f.to_dict() for f in self.findings],
        }

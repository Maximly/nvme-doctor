# Diagnostic rules

Shared SMART/Health rules run on any platform only when standards-defined NVMe health evidence was collected. Linux-only PCIe/kernel rules require the corresponding capability/data source.

| Finding code | Severity | Basis |
|---|---|---|
| `controller-state` | warning/critical | controller/device not in a normal present/live state |
| `nvme-critical-warning` | critical | NVMe SMART critical-warning field |
| `available-spare` | critical | spare below controller threshold |
| `media-errors` | warning/critical | media/data-integrity error counter |
| `endurance-used` | critical | Percentage Used >= 100% |
| `endurance-near-limit` | warning | Percentage Used >= 90% |
| `endurance-high` | info | Percentage Used >= 80% |
| `temperature-critical` | critical | controller threshold or conservative fallback |
| `temperature-warning` | warning | controller threshold or conservative fallback |
| `pcie-aer-fatal` | critical | Linux endpoint fatal AER counters |
| `pcie-aer-nonfatal` | warning | Linux endpoint uncorrectable non-fatal AER counters |
| `pcie-aer-corrected` | info/warning | Linux corrected AER counters |
| `pcie-link-degraded` | warning | negotiated endpoint link below known endpoint maximum |
| `kernel-controller-down` | critical | high-signal target-scoped controller-down/failed-reset log text |
| `kernel-reset-timeout` | warning | reset/timeout activity in target-scoped OS/kernel log |
| `likely-pcie-path` | warning/critical | reset symptoms + PCIe evidence + clean media-health indicators |
| `power-state-hypothesis` | info | Linux reset symptoms while APST/ASPM active; deliberately low confidence |
| `unsafe-shutdowns` | info | historical SMART counter |
| `error-log-history` | info | historical NVMe error-log entry count |

## Capability rule

Unavailable evidence must never be converted into a clean result. For example, macOS does not expose Linux-style AER counters; an empty AER object on macOS is not equivalent to a measured zero-error count.

Likewise, a macOS `SMART Status: Verified` value is recorded as context but does not allow `OK` if the NVMe SMART/Health log itself could not be read.

Thresholds and wording should evolve only with test coverage and field evidence from real systems.

## PCIe generation and protocol version

NVMe Doctor keeps two concepts separate:

- **NVMe version** is the controller protocol/specification version (for example 1.4 or 2.0).
- **PCIe generation** is derived from the negotiated PCIe transfer rate (8 GT/s = Gen3, 16 GT/s = Gen4, 32 GT/s = Gen5). The endpoint maximum is reported separately so a Gen5 device currently negotiating Gen4 is visible and can trigger the degraded-link rule.

## Unsafe shutdowns

`unsafe_shutdowns` is a cumulative SMART lifetime counter. It is high-confidence evidence that a normal NVMe shutdown notification was not received before a power-loss event, but it contains no per-event timestamp or reason. NVMe Doctor therefore never attributes an historical unsafe shutdown to a specific PSU, OS crash, reset, firmware bug, enclosure, or user action from this counter alone.

For controlled before/after checks, save JSON reports around the event and use `nvme-doctor diff`. Counter deltas can establish that an unsafe shutdown happened in that interval; exact cause still requires host/platform evidence.

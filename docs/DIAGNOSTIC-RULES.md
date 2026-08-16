# Diagnostic rules

NVMe Doctor prefers direct evidence over inferred failure labels. A rule must verify that the required capability was actually collected before interpreting a missing/zero value.

## Current-health assessment

The human assessment is intentionally separate from raw process status:

- `HEALTHY` — no material current warning/critical health signal in collected evidence;
- `DEGRADED` — current transport/controller/link/thermal degradation without warning-level media/endurance evidence;
- `AT RISK` — current warning-level media/endurance evidence;
- `CRITICAL` — serious current reliability/failure evidence;
- `INCOMPLETE` — primary evidence required for a clean judgement is unavailable.

Near-term risk is `LOW`, `ELEVATED`, `HIGH`, or `UNKNOWN`. It is not a remaining-life or time-to-failure estimate.

A single snapshot cannot establish a trend. `Trend` therefore remains `UNKNOWN` unless comparison data exists; use saved reports plus `nvme-doctor diff` for before/after evidence.

## NVMe rules

| Finding code | Severity | Basis |
|---|---|---|
| `controller-state` | warning/critical | native NVMe controller state is abnormal |
| `nvme-passthrough-unavailable` | info | USB/translated NVMe is visible but SMART/admin path is unavailable |
| `usb-nvme-bridge` | info | confirmed USB-to-NVMe transport context |
| `nvme-critical-warning` | critical | NVMe SMART critical-warning field |
| `available-spare` | critical | standardized NVMe available spare below controller threshold |
| `media-errors` | warning/critical | NVMe media/data-integrity error counter |
| `endurance-used` | critical | Percentage Used >= 100% |
| `endurance-near-limit` | warning | Percentage Used >= 90% |
| `endurance-high` | info | Percentage Used >= 80% |
| `error-log-history` | info | historical NVMe Error Information entries |
| `unsafe-shutdowns` | info | historical unsafe-shutdown counter |

## ATA/SATA rules

| Finding code | Severity | Basis |
|---|---|---|
| `device-state` | warning/critical | block/ATA device state is abnormal |
| `ata-passthrough-unavailable` | info | ATA drive identified but bridge/OS does not expose SMART |
| `ata-smart-failed` | critical | ATA SMART overall-health reports failure |
| `ata-threshold-failure` | critical | SMART attribute threshold failure |
| `ata-unstable-sectors` | warning/critical | pending/offline-uncorrectable sectors |
| `ata-reallocated-sectors` | info/warning | RAW reallocated-sector evidence |
| `ata-reported-uncorrectable` | warning/critical | reported uncorrectable errors |
| `ata-command-timeouts` | warning | ATA command-timeout evidence |
| `ata-crc-errors` | info/warning | UDMA CRC history, interpreted primarily as transport/cable/path evidence |
| `ata-error-log` | info/warning | ATA SMART error-log history |
| `ata-host-link-errors` | warning | host/link error evidence |
| `ata-reserve-low` | critical | explicit ATA reserve-space indicator at/below its threshold |
| `ata-reserve-margin-low` | warning | explicit ATA reserve-space indicator close to threshold |

### ATA spare/reserve semantics

ATA does not have one universal NVMe-style Available Spare field. NVMe Doctor therefore does not blindly trust smartctl's synthesized top-level ATA `spare_available` value.

Rules prefer explicit SMART attributes whose names/metadata establish reserve/spare-space meaning. SMART attribute 5 (`Reallocated_Sector_Ct`) is interpreted from its **RAW** count for reallocated-sector evidence; its vendor-normalized VALUE is not treated as remaining spare capacity.

Vendor-specific wear attributes are kept separate from generic remaining-life claims unless their meaning is sufficiently established.

## Shared temperature, transport and host-path rules

| Finding code | Severity | Basis |
|---|---|---|
| `temperature-critical` | critical | controller threshold or conservative fallback |
| `temperature-warning` | warning | controller threshold or conservative fallback |
| `usb-transport-instability` | warning | target-correlated USB/UAS/SCSI recovery/I/O events |
| `usb-link-downshift` | warning | USB 3.x-capable path currently operating at a much lower link speed |
| `usb-bot-transport` | info | USB BOT fallback/transport context |
| `host-io-errors` | warning/critical | target-scoped host I/O failures |

## Linux PCIe/kernel rules

| Finding code | Severity | Basis |
|---|---|---|
| `pcie-aer-fatal` | critical | fatal AER counters/evidence |
| `pcie-aer-nonfatal` | warning | uncorrectable non-fatal AER evidence |
| `pcie-aer-corrected` | info/warning | corrected AER evidence |
| `pcie-link-degraded` | warning | native NVMe endpoint negotiated below known endpoint maximum |
| `kernel-controller-down` | critical | high-signal target-scoped controller-down/failed-reset log text |
| `kernel-reset-timeout` | warning | target-scoped reset/timeout activity |
| `likely-pcie-path` | warning/critical | reset symptoms plus PCIe evidence while media-health evidence remains comparatively clean |
| `power-state-hypothesis` | info | reset symptoms while APST/ASPM are active; deliberately low confidence |

## Capability rule

Unavailable evidence must never be converted into a clean result. Examples:

- macOS lacking Linux AER counters is not “AER = 0”;
- `diskutil`/`system_profiler` saying a device is present is not a substitute for SMART media-health evidence;
- a USB bridge hiding native PCIe ancestry is not evidence that the native PCIe path is healthy;
- a SATA disk behind a PCIe AHCI controller must not inherit the controller's PCIe generation as the drive's link generation.

## PCIe generation versus protocol/link speed

NVMe Doctor keeps these separate:

- **NVMe version** — controller protocol/specification version;
- **PCIe generation** — PCIe negotiated/max transfer rate for native PCIe devices/ports;
- **SATA link** — SATA drive-side negotiated/max speed, e.g. 6.0 Gb/s.

For SATA behind AHCI, a host controller may itself be attached through Gen4/Gen5 PCIe. That describes the controller interconnect, not a “Gen5 SATA SSD”.

## Unsafe shutdowns

`unsafe_shutdowns` is a cumulative NVMe lifetime counter. It establishes that normal shutdown notification was missed at some point; it does not contain a per-event timestamp or cause.

Use controlled before/after JSON reports and `nvme-doctor diff` to prove that the counter changed during a specific interval. Cause still requires host/platform evidence.

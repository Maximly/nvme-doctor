# Architecture

NVMe Doctor separates **collection**, **diagnosis**, and **rendering** so platform gaps cannot silently become clean-health claims.

```text
CLI (cli.py)
 |
 +-- collect.py ---------------- Linux collection + platform dispatch
 |      +-- sysfs / udev
 |      +-- nvme-cli
 |      +-- smartctl
 |      +-- lspci
 |      +-- journalctl / dmesg
 |
 +-- collect_macos.py ---------- macOS collection
 |      +-- diskutil physical-disk inventory
 |      +-- system_profiler native-NVMe detail where needed
 |      +-- smartctl auto / scan / SAT paths
 |      +-- macos_usb_nvme.py -- RTL9210 direct read-only NVMe path
 |      +-- macos_usb_sata.py -- generic direct read-only SAT/BOT path
 |
 +-- diagnose.py --------------- evidence/correlation rules
 +-- render.py ----------------- human/JSON reports and topology
 +-- diff.py ------------------- before/after report comparison
```

## Source and distribution layout

```text
src/
├── __init__.py
├── cli.py
├── collect.py
├── collect_macos.py
├── diagnose.py
├── diff.py
├── macos_usb_nvme.py
├── macos_usb_sata.py
├── model.py
├── platforms.py
├── render.py
├── runner.py
└── util.py
```

`tools/build_single.py` bundles these modules into the root-level standalone `nvme-doctor` executable. The bundled file exposes the same public `nvme_doctor` package name internally so relative and dynamic imports continue to work.

`build.sh` rebuilds the standalone. `install.sh` deliberately does not build; it validates and copies the existing root executable into the selected prefix.

## Snapshot and capability contract

A collected `Snapshot` contains observations plus a `capabilities` map. Diagnosis rules must distinguish:

- measured zero/clean evidence;
- unavailable evidence;
- unsupported evidence;
- collection failure.

For example, macOS not exposing Linux-style PCIe AER is not equivalent to measured AER counters of zero.

## Linux collection

### Native NVMe

Primary evidence sources:

- `/sys/class/nvme` and `/sys/class/block` for controller/namespace identity;
- `/sys/bus/pci/devices` for endpoint BDF, current/max link, NUMA and AER;
- `nvme-cli` for SMART, Identify Controller and Error Information;
- `smartctl` as supplemental/fallback health evidence;
- target-scoped current-boot kernel events.

### ATA/SATA

Physical `sdX` inventory starts from the OS/sysfs rather than relying only on `smartctl --scan`. Native libata ancestry (`.../ataN/hostN/...`) is treated as authoritative SATA evidence.

Identity/health collection uses smartctl with conservative backend selection and retains OS identity even when SMART access is incomplete. Explicit ATA reserve-space attributes and raw media/transport counters are normalized into shared fields without assigning universal meaning to unrelated vendor-specific IDs.

### USB storage

USB/SCSI-translated devices are kept separate from the underlying media protocol. The collector records the USB/SCSI path where possible, including UAS versus `usb-storage`, physical USB path, SCSI address, and target-correlated reset/I/O evidence.

A USB bridge may expose NVMe or ATA health while still hiding the drive's native PCIe/SATA host ancestry. Hidden evidence is marked unavailable rather than inferred.

## macOS collection

### Native/internal drives

`diskutil` provides whole-disk identity/transport data. Native NVMe detail paths can additionally use `system_profiler SPNVMeDataType` and smartctl where available.

### External USB-to-SATA

Probe order is intentionally non-disruptive first:

1. plain smartctl automatic detection;
2. explicit SAT smartctl mode;
3. direct read-only libusb SAT fallback only when interaction/flags permit it.

This order matters because some bridges work with smartctl automatic detection but fail if `-d sat` is forced.

### External RTL9210 USB-to-NVMe

When normal Darwin smartctl cannot access the underlying NVMe admin path, the RTL9210 backend may temporarily acquire the enclosure through libusb and issue only read-only NVMe Identify/SMART commands.

### Fast all-drive topology

Parameterless macOS `topology` has a dedicated fast path. It uses `diskutil` physical whole-disk inventory and does not run SMART health collection or repeated `system_profiler` scans merely to draw the tree.

## Topology model

Topology is protocol-aware rather than `sdX`-or-NVMe-name driven.

### Native NVMe

Linux renders:

```text
NUMA -> PCI bridge(s) -> NVMe endpoint -> namespace(s)
```

The endpoint's negotiated PCIe generation/width can be compared with its known maximum.

### Native SATA

Linux renders:

```text
NUMA -> PCI bridge(s) -> SATA/AHCI controller
     -> libata port / SCSI attachment -> SATA link -> /dev/sdX
```

The AHCI controller's host-side PCIe generation is **not** the SATA drive generation. Drive-side SATA speed is reported separately.

Multiple SATA drives can share one AHCI PCI function; their `ataN`/host/SCSI attachment differentiates them below that controller.

### All-drive tree

Parameterless `topology` collects all physical drives and merges identical hardware prefixes, so a shared PCIe bridge or AHCI controller appears once rather than once per disk.

## Diagnosis layer

A `Finding` contains:

- stable code;
- severity (`critical`, `warning`, `info`);
- human-readable diagnosis;
- confidence;
- supporting evidence;
- conservative next actions.

Current-health verdict and future-looking interpretation are deliberately separate:

- `HEALTHY`, `DEGRADED`, `AT RISK`, `CRITICAL`, `INCOMPLETE`;
- near-term risk `LOW`, `ELEVATED`, `HIGH`, or `UNKNOWN`;
- trend is `UNKNOWN` for a single snapshot and requires comparison evidence.

## Safety model

Drive/admin commands used by diagnostic collection are read-only. NVMe Doctor does not format/sanitize media, flash firmware, change namespaces, reset controllers, or modify power-policy settings.

macOS direct USB backends are a special operational case: the drive commands remain read-only, but the enclosure may be temporarily unmounted/ejected/captured and later restored. That action is gated by root/interaction or explicit `--direct-usb` permission.

## JSON/report stability

Reports currently use `schema_version: 1`. The schema is additive where possible. Stable finding codes are intended for scripts; explanatory wording may evolve as field evidence improves.

# Architecture

NVMe Doctor is split into platform-specific collection and a shared diagnosis layer.

```text
CLI
 |
 +-- collect.py ---------------- platform dispatch
 |      |
 |      +-- Linux backend
 |      |     +-- sysfs
 |      |     +-- nvme-cli
 |      |     +-- smartctl
 |      |     +-- lspci
 |      |     +-- journalctl/dmesg
 |      |
 |      +-- collect_macos.py
 |            +-- system_profiler SPNVMeDataType -json
 |            +-- diskutil physical-disk inventory
 |            +-- smartctl --scan[-open] / smartctl -a -j
 |            +-- macos_usb_nvme.py (RTL9210 direct USB path)
 |            +-- macos_usb_sata.py (generic read-only SAT/BOT USB-SATA path)
 |            +-- target-scoped unified-log evidence where possible
 |
 +-- diagnose.py -------------- shared evidence/correlation rules
 |
 +-- render.py ---------------- text / JSON + capability disclosure
```

## Source and distribution layout

The maintainable implementation remains flat under `src/`:

```text
src/
├── __init__.py
├── cli.py
├── collect.py
├── collect_macos.py
├── diagnose.py
├── diff.py
├── macos_usb_nvme.py
├── model.py
├── platforms.py
├── render.py
├── runner.py
└── util.py
```

`tools/build_single.py` packages these modules into the root-level standalone `nvme-doctor` executable using a small in-memory importer. Relative imports and dynamic platform-specific imports continue to use the public `nvme_doctor` package name inside the bundled file.

`build.sh` invokes `tools/build_single.py` to rebuild the root-level standalone file. `install.sh` is intentionally build-free: it validates and copies the already-built root-level `nvme-doctor` into the selected prefix. The installed command therefore does not depend on a companion Python package directory.

## Platform contract

A `Snapshot` contains observations plus a `capabilities` map. Rules must not infer that an unavailable platform facility is clean merely because it produced no data.

For example, macOS currently marks PCIe AER and detailed PCIe topology unavailable. Empty AER data on macOS therefore means “not collected”, not “zero errors”.

## Linux collection

Primary sources:

- `/sys/class/nvme` for native controller identity/state;
- `/sys/bus/pci/devices` for native-NVMe BDF, PCIe link state, NUMA locality and AER counters;
- `nvme-cli` for native NVMe SMART, Identify Controller and error-log data;
- `smartctl --scan[-open]` plus USB solid-state sysfs candidates to discover NVMe devices translated to `/dev/sdX`;
- `smartctl -a -j /dev/sdX` as the primary health source for confirmed USB/SCSI-translated NVMe and as supplemental/fallback evidence for native NVMe;
- current-boot kernel logs restricted to the target controller/block-device name and, for native NVMe, its PCIe ancestry.

For a translated `/dev/sdX` device, the backend deliberately marks native PCIe link/AER/NUMA facilities unavailable: the bridge can pass NVMe admin/SMART commands while still hiding the SSD's PCIe endpoint from Linux.

## macOS collection

Primary sources:

- built-in `system_profiler SPNVMeDataType -json` for native NVMe inventory, identity and any exposed link metadata;
- `smartctl --scan-open` with `--scan` fallback for device discovery and bridge-type hints;
- Darwin `smartctl -a -j` for standards-defined NVMe SMART/Health and error-information data;
- unified log only when lines can be tied to the selected device by a stable identity token.

macOS `SMART Status: Verified` is stored as contextual evidence but does not satisfy the requirement for full NVMe SMART/Health data.

If smartctl itself reports a usable NVMe path/type on macOS, that exact reported path is retained. Current Darwin builds do not implement the SCSI backend required to force `sntrealtek`/`sntjmicron`/`sntasmedia` against ordinary `/dev/diskN` devices, so NVMe Doctor does not invent or force those modes.

## Diagnosis layer

Rules emit a `Finding` containing:

- stable code;
- severity (`critical`, `warning`, `info`);
- human-readable diagnosis;
- confidence;
- exact supporting evidence;
- conservative next actions.

The rule engine distinguishes direct evidence from correlation. Platform-specific rules must check capability availability before treating absence of evidence as evidence of absence.

## Safety model

NVMe commands used for health collection are read-only. Remediation text may suggest a reversible A/B test, but the program does not perform controller resets, power-policy changes, firmware updates, namespace operations or destructive commands. On macOS RTL9210 direct access temporarily unmounts and captures the USB device, then restores its original mount state.

## JSON schema

Reports currently contain `schema_version: 1`. The `capabilities` object was added without changing the schema number because it is additive. Stable finding codes are intended for scripts; human-readable wording may evolve.

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
 |            +-- smartctl --scan[-open]
 |            +-- smartctl -a -j
 |            +-- target-scoped unified-log evidence where possible
 |
 +-- diagnose.py -------------- shared evidence/correlation rules
 |
 +-- render.py ---------------- text / JSON + capability disclosure
```

## Source layout

The repository intentionally keeps the implementation flat under `src/`:

```text
src/
├── __init__.py
├── cli.py
├── collect.py
├── collect_macos.py
├── diagnose.py
├── diff.py
├── model.py
├── platforms.py
├── render.py
├── runner.py
└── util.py
```

The repository launcher executes `src.cli` directly from the checkout. Packaging and the optional system-wide installer map these same files to the public installed Python package `nvme_doctor`; no `src/nvme_doctor/` directory is required in the repository.

## Platform contract

A `Snapshot` contains observations plus a `capabilities` map. Rules must not infer that an unavailable platform facility is clean merely because it produced no data.

For example, macOS currently marks PCIe AER and detailed PCIe topology unavailable. Empty AER data on macOS therefore means “not collected”, not “zero errors”.

## Linux collection

Primary sources:

- `/sys/class/nvme` for controller identity/state;
- `/sys/bus/pci/devices` for BDF, PCIe link state, NUMA locality and AER counters;
- `nvme-cli` for SMART, Identify Controller and error-log data;
- `smartctl` as supplemental/fallback health evidence;
- current-boot kernel logs restricted to the target controller and its PCIe ancestry.

## macOS collection

Primary sources:

- built-in `system_profiler SPNVMeDataType -json` for native NVMe inventory, identity and any exposed link metadata;
- `smartctl --scan-open` with `--scan` fallback for device discovery and bridge-type hints;
- Darwin `smartctl -a -j` for standards-defined NVMe SMART/Health and error-information data;
- unified log only when lines can be tied to the selected device by a stable identity token.

macOS `SMART Status: Verified` is stored as contextual evidence but does not satisfy the requirement for full NVMe SMART/Health data.

USB-NVMe bridge types returned by smartctl (for example `sntrealtek`) are retained and supplied back via `smartctl -d TYPE` for the selected disk.

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

Collectors and rules are read-only. Remediation text may suggest a reversible A/B test, but the program does not perform controller resets, power-policy changes, firmware updates, namespace operations or destructive commands.

## JSON schema

Reports currently contain `schema_version: 1`. The `capabilities` object was added without changing the schema number because it is additive. Stable finding codes are intended for scripts; human-readable wording may evolve.

# Changelog

## 1.1

- ATA/SATA SSD and HDD support on Linux and macOS: discovery and SMART collection through smartctl, USB-SATA SAT bridges, and SATA-specific diagnosis, rendering, topology and diff counters.
- `list` inventory improvements: quick non-disruptive health (`GOOD`/`WARN`/`FAIL`/`-`), temperature and TBW (total bytes written, from the NVMe SMART log, ATA Device Statistics or SMART attribute counters) columns, Size from already-collected OS capacity data, natural controller ordering and compact columns.
- All-drive `topology`: one merged tree with libata ports, SCSI attachments, PCIe/NUMA ancestry and protocol-aware USB leaves; fast non-disruptive macOS all-drive inventory.
- macOS USB-SATA support: smartctl automatic detection first, then explicit SAT, then a conservative direct read-only SAT/BOT fallback with safe eject/restore.
- Assessment clarity: `HEALTHY`/`DEGRADED`/`AT RISK`/`CRITICAL`/`INCOMPLETE` verdicts, near-term risk separated from trend, and vendor-neutral ATA spare/wear interpretation.
- Self-install CLI commands `nvme-doctor install` / `nvme-doctor remove` replace the separate `install.sh`; the Makefile `install`/`uninstall` targets delegate to them.
- Single-source release version in the repo-root `VERSION` file, substituted into the standalone build, the README release line and package metadata at build time.
- Documentation refreshed against current CLI/source behavior, with staleness regression checks.

## 1.0

- Initial cross-platform NVMe diagnostics for Linux and macOS.
- Native Linux NVMe SMART, error log, PCIe/AER and NUMA/topology analysis.
- USB/SCSI-translated NVMe support on Linux through smartctl pass-through.
- Direct Realtek RTL9210 NVMe Identify and SMART access on macOS through libusb, with a safe automatic direct-access policy.
- Standalone single-file `nvme-doctor` executable built by `build.sh` from the maintainable flat `src/*.py` tree.
- JSON reports, before/after diff, watch mode, and lifetime/endurance and thermal interpretation.
- Live check progress with an in-place spinner, `--debug` stage timing, `--no-progress`, and target-scoped USB/kernel-event diagnostics.

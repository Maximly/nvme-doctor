# Changelog

## 1.0.1 - 2026-08-09

- Cross-platform NVMe SSD health and root-cause diagnostics for Linux and macOS.
- Linux native-NVMe collection from sysfs, `nvme-cli`, `smartctl`, PCIe/AER state, NUMA locality, topology, and target-scoped kernel logs.
- Linux support for NVMe SSDs exposed through USB/SCSI bridges as `/dev/sdX` when the bridge provides NVMe SMART/admin pass-through.
- macOS native-NVMe discovery and health collection using `system_profiler`, `diskutil`, and `smartctl` where supported.
- macOS direct read-only NVMe Identify and SMART/Health access through Realtek RTL9210 USB bridges using libusb, with controlled unmount/capture/restore handling.
- Evidence-based verdicts (`OK`, `WARNING`, `CRITICAL`, `INCOMPLETE`) that distinguish unavailable evidence from measured-clean evidence.
- PCIe generation/width checks, AER interpretation, controller reset/timeout correlation, endurance, thermal, lifetime-I/O, unsafe-shutdown, and error-log analysis.
- `list`, `check`, `report`, `diff`, `topology`, and `watch` commands with text and JSON output.
- Single-file distribution: `nvme-doctor` is a standalone executable Python file built from the flat `src/*.py` sources; `install.sh` rebuilds and installs that file.

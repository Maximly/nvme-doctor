# Changelog

## 1.0.3 - 2026-08-09

- Cross-platform NVMe diagnostics for Linux and macOS.
- Native Linux NVMe SMART, error, PCIe/AER and NUMA/topology analysis.
- USB/SCSI-translated NVMe support on Linux through smartctl pass-through.
- Direct Realtek RTL9210 NVMe Identify and SMART access on macOS through libusb.
- Safe automatic macOS direct-access policy: automatic when proven unmounted, interactive confirmation when mounted, explicit `--direct-usb` for non-interactive use.
- Topology output distinguishes USB bridge identity from the underlying NVMe SSD and no longer presents enclosure names as NVMe endpoints.
- Standalone single-file `nvme-doctor` executable, rebuilt by `install.sh` from the maintainable flat `src/*.py` source tree.
- JSON reports, before/after diff, watch mode, lifetime/endurance and thermal interpretation.

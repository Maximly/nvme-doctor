# macOS backend notes

## Data sources

NVMe Doctor uses different paths for native NVMe and USB-NVMe bridges.

### Native NVMe

- `system_profiler SPNVMeDataType -json` provides native NVMe inventory.
- `smartctl -a -j /dev/diskN` provides standards-defined NVMe health where Darwin smartmontools can reach Apple's native NVMe SMART user client.
- `smartctl --scan-open` and `--scan` are merged for device hints.

### External physical disks

`diskutil` inventories real whole disks and excludes synthesized APFS devices. An external USB SSD remains visible even when SMART/admin access is unavailable.

## RTL9210 direct USB backend

NVMe Doctor includes a direct backend for Realtek RTL9210 (`0bda:9210`). It is used because current smartmontools Darwin builds do not implement the SCSI device layer required by `sntrealtek` on `/dev/diskN`.

For an interactive `sudo nvme-doctor check diskN`, direct RTL9210 health access is automatic when the target is proven unmounted. If mounted volumes are detected, NVMe Doctor asks before temporary unmount/capture. Non-interactive or machine-readable use requires explicit permission:

```sh
sudo nvme-doctor check disk4 --direct-usb
```

The direct path:

1. verifies the target is the only external physical USB SSD;
2. verifies exactly one openable RTL9210 bridge is connected;
3. cleanly unmounts the whole disk;
4. captures/detaches the macOS USB storage driver with libusb;
5. claims interface 0 and selects the bridge's BOT alternate setting;
6. sends RTL9210 vendor SCSI CDB `0xE4` carrying read-only NVMe commands;
7. reads NVMe Identify Controller (`0x06`, CNS 1);
8. reads the 512-byte NVMe SMART/Health log (`Get Log Page 0x02`, LID 0x02);
9. releases the interface, reattaches the macOS storage driver, and remounts the disk.

The backend does **not** send format, sanitize, firmware activation, namespace-management, write, reset, or feature-changing commands. The NVMe operations are read-only, but unmount/capture/remount is operationally disruptive, so close applications using the disk first.

Homebrew libusb is required for this optional path:

```sh
brew install libusb
```

The repository itself still has no pip dependency for normal use.

## Why BOT is used

The tested RTL9210 exposes both:

- interface 0 alternate setting 0: USB Mass Storage Bulk-Only Transport (BOT);
- interface 0 alternate setting 1: UAS/UASP.

macOS normally owns the UAS path. After explicit capture, NVMe Doctor selects BOT because the RTL9210 vendor SCSI CDB can then be carried with the much simpler standard BOT CBW/data/CSW sequence.

## Current direct-backend scope

The direct implementation is intentionally narrow and conservative:

- Realtek RTL9210 (`0bda:9210`) only;
- exactly one external physical USB SSD;
- exactly one RTL9210 bridge;
- root required;
- Identify + SMART only.

This avoids guessing which bridge belongs to which `diskN` when macOS does not expose a reliable disk-to-USB VID:PID mapping through `diskutil`. Other bridge chipsets remain capability-dependent.

## Intentional macOS limitations

Even with direct USB SMART access, macOS does not expose the same native SSD PCIe path information as Linux. NVMe Doctor therefore does not claim to provide, for a USB-connected SSD:

- native SSD PCIe generation/link state;
- PCIe AER counters;
- upstream root-port/switch correlation;
- NUMA locality;
- Linux-style ASPM/APST policy inspection.

Those fields remain unavailable rather than being reported as clean.

# macOS backend notes

macOS exposes less native storage-path detail than Linux, so NVMe Doctor separates **physical-disk inventory**, **health access**, and **optional direct USB access**.

## Physical disk inventory

`diskutil` is the primary source for real whole disks and excludes synthesized APFS containers/volumes from physical-drive discovery.

For parameterless:

```sh
sudo nvme-doctor topology
```

NVMe Doctor uses a dedicated fast `diskutil` inventory path. It does not run SMART health collection or repeated `system_profiler` scans solely to draw the all-drive tree. Human mode displays a `Collecting topology...` spinner; `--debug` shows timed stages.

## Native NVMe

Detailed native-NVMe collection may use:

- `system_profiler SPNVMeDataType -json` for controller identity and exposed link metadata;
- `smartctl -a -j /dev/diskN` for standards-defined health where Darwin smartmontools can reach it;
- target-scoped unified-log evidence only when the selected disk can be matched reliably.

macOS `SMART Status: Verified` is contextual evidence and is not treated as a substitute for full NVMe SMART/Health data.

## USB-to-SATA

### Normal smartctl probe order

For an identified ATA/SATA external disk, NVMe Doctor tries:

1. smartctl automatic detection on `/dev/diskN`;
2. explicit `-d sat` only if automatic detection does not provide ATA SMART;
3. direct read-only USB SAT as a last resort when permitted.

Automatic detection comes first because real bridges exist that successfully expose full ATA SMART through plain smartctl but fail when `-d sat` is forced.

### Direct USB-SATA fallback

When normal smartctl access is unavailable and the drive is positively identified as ATA/SATA, an interactive root check (or explicit `--direct-usb`) may temporarily acquire the USB mass-storage interface through libusb.

The direct path:

1. records the target disk/mount state and performs a clean whole-disk eject;
2. identifies a suitable BOT-capable USB mass-storage interface conservatively;
3. switches an UAS-capable enclosure to its BOT fallback when available;
4. performs standard USB Mass Storage BOT reset/clear-halt recovery;
5. verifies the transport with standard SCSI INQUIRY;
6. tries read-only SAT ATA PASS THROUGH(16), then PASS THROUGH(12) if required;
7. reads ATA IDENTIFY, SMART data, SMART thresholds, and SMART RETURN STATUS where supported;
8. releases the interface and restores/re-probes the original disk/mount state.

A failed BOT/SAT command is recovered before trying the alternate SAT CDB size so a stale CSW/halted endpoint does not poison the fallback attempt.

The implementation does not assign vendor-specific SMART meanings merely from attribute numbers. Unknown vendor attributes stay raw unless their meaning is established by reliable identity/metadata.

If mapping or SAT behavior is ambiguous, the result remains `INCOMPLETE` rather than guessing.

## RTL9210 USB-to-NVMe direct backend

NVMe Doctor includes a narrow direct backend for Realtek RTL9210 (`0bda:9210`) because Darwin smartmontools may not expose the SCSI/SNT path needed to reach the underlying NVMe admin interface.

For an interactive root check, direct health access may proceed automatically when the target is proven unmounted. If mounted volumes are present, NVMe Doctor asks before temporary unmount/eject/capture. Non-interactive direct access requires explicit permission:

```sh
sudo nvme-doctor check disk4 --direct-usb
```

The direct RTL9210 path:

1. validates the external USB target conservatively;
2. performs a clean whole-disk eject before opening the bridge to avoid stale libusb handles;
3. captures the bridge and uses its BOT alternate setting;
4. sends the RTL9210 vendor tunnel carrying read-only NVMe Identify Controller and SMART/Health commands;
5. optionally reads Error Information/Firmware Slot logs with `--usb-extra-logs`;
6. releases the bridge, lets macOS re-probe the disk, and restores the original mount state.

It does **not** send format, sanitize, firmware activation, namespace-management, write, reset, or feature-changing NVMe commands.

## Direct USB safety

Direct USB paths are read-only at the drive-command level but operationally disruptive because temporary unmount/eject/driver capture can occur. Close applications using the external disk first.

`nvme-doctor list` and parameterless `topology` never use direct/eject/capture access merely to improve presentation.

Homebrew libusb is required only for optional direct USB backends:

```sh
brew install libusb
```

## Intentional macOS limitations

NVMe Doctor does not invent Linux-only evidence. Depending on the device/path, macOS may not expose:

- native SSD PCIe generation/width behind a USB bridge;
- PCIe AER counters;
- full upstream root-port/switch ancestry;
- NUMA locality;
- Linux-style ASPM/APST policy state;
- sufficiently target-scoped historical storage logs.

Unavailable fields remain unavailable rather than being reported as clean.

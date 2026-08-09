# macOS backend notes

## Data sources

NVMe Doctor deliberately uses public command-line surfaces rather than a privileged helper or custom kernel/system extension.

### `system_profiler`

`system_profiler SPNVMeDataType -json` is used for native NVMe inventory. Typical fields include the BSD disk name, model, firmware revision, serial number, size, SMART status and, on some systems, PCIe link width/speed.

### smartmontools

Full health diagnosis uses `smartctl -a -j /dev/diskN`. smartmontools implements a Darwin NVMe backend using Apple's NVMe SMART user-client interface.

`smartctl --scan-open` is also used to discover device-type hints. This matters for some USB-to-NVMe enclosures, where smartctl may require a bridge-specific `-d` value rather than plain `nvme`.

## Intentional limitations

macOS does not expose the same Linux sysfs/AER/PCI hierarchy used by the Linux backend. the macOS backend therefore does not claim to provide:

- PCIe AER counter diagnosis;
- upstream root-port/bridge error correlation;
- Linux-style ASPM/APST policy inspection;
- NUMA locality;
- guaranteed controller-scoped kernel-log history.

These appear as unavailable in the report.

## Privileges

`nvme-doctor list` can normally discover native devices without root through `system_profiler`. Full `smartctl` access may require `sudo`, depending on the device and smartmontools path.

## External enclosures

Whether SMART passthrough works is determined by the enclosure bridge, firmware, macOS driver path and smartmontools support. NVMe Doctor does not fake health data when passthrough is unavailable: the report becomes `INCOMPLETE` and keeps the bridge/scan evidence in the JSON report.

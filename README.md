# NVMe Doctor

**NVMe and SATA/ATA SSD/HDD diagnostics and root-cause analysis for Linux and macOS.**

NVMe Doctor combines drive health data with the host-side evidence that is actually available on the current platform. It is designed to answer not only “what do the counters say?”, but also “what is the strongest explanation supported by those counters, the transport, and the host path?”

NVMe Doctor is GPL-3.0-or-later software from KernelSoft.

## Current release

**1.1.21**

Current scope:

- native NVMe on Linux and macOS;
- native SATA/ATA SSDs and HDDs;
- USB-to-NVMe where the OS/bridge exposes NVMe health, plus direct Realtek RTL9210 read-only access on macOS;
- USB-to-SATA through smartctl automatic/SAT pass-through, with a conservative direct read-only SAT fallback on macOS;
- protocol-aware hardware topology;
- one-tree topology for all physical drives;
- quick non-disruptive health in `list`;
- doctor-style assessment, JSON reports, before/after diff, and change watching.

The repository contains the maintainable source under `src/` and a pre-built standalone `./nvme-doctor` executable. The standalone uses Python's standard library and can be copied directly to another Linux or macOS machine with Python 3.9+.

## Quick start

No installation is required:

```bash
./nvme-doctor --version
./nvme-doctor list
sudo ./nvme-doctor check nvme0
sudo ./nvme-doctor check sda
sudo ./nvme-doctor topology
```

Device names may be given with or without `/dev/`, for example `nvme0`, `/dev/nvme0n1`, `sda`, `/dev/sda`, or `disk4` on macOS.

If exactly one supported physical drive is discoverable, `check` may omit the device argument.

## Commands

### `list`

```bash
nvme-doctor list
nvme-doctor list --json
```

Human output is intentionally compact:

```text
Controller   Health  Proto  Transport            Size  Model                                Firmware     Serial
nvme0        GOOD    NVMe   pcie              4.00 TB  ...                                  ...          ...
sda          GOOD    ATA    SATA              1.92 TB  ...                                  ...          ...
disk4        -       ATA    USB -> SATA       2.00 TB  ...                                  ...          ...
```

Quick health values are:

- `GOOD` — a cheap, non-disruptive health check passed;
- `WARN` — a warning signal was available in the quick health data;
- `FAIL` — the quick health source reported failure;
- `-` — trustworthy health is not available quickly/non-disruptively.

`list` never ejects or captures a macOS USB device just to populate `Health`.

### `check`

```bash
sudo nvme-doctor check nvme0
sudo nvme-doctor check /dev/sda
sudo nvme-doctor check disk4
nvme-doctor check nvme0 --json
nvme-doctor check nvme0 --debug
```

Normal human checks print the version immediately and show an in-place `Checking...` spinner while evidence is collected. `--debug` replaces the spinner with timed collection stages. JSON output remains machine-readable.

The assessment separates three concepts:

- **Verdict** — current health state: `HEALTHY`, `DEGRADED`, `AT RISK`, `CRITICAL`, or `INCOMPLETE`;
- **Near-term risk** — `LOW`, `ELEVATED`, `HIGH`, or `UNKNOWN` based on current evidence, not a time-to-failure prediction;
- **Trend** — a single check reports `UNKNOWN`; trend requires comparison data.

Unavailable evidence is never treated as clean evidence. For example, a USB bridge that hides SMART produces `INCOMPLETE`, not `HEALTHY`.

A nonexistent device is rejected before collection:

```text
nvme-doctor: device not found: /dev/sdb
```

### `topology`

Show all physical drives in one merged tree:

```bash
sudo nvme-doctor topology
sudo nvme-doctor topology --debug
nvme-doctor topology --json
```

Or inspect one drive:

```bash
nvme-doctor topology nvme0
nvme-doctor topology /dev/sda
nvme-doctor topology disk4
```

On Linux, common NUMA/PCIe/controller branches are merged. Native SATA disks behind the same AHCI controller are separated by their libata/SCSI attachment, so two disks do not appear to occupy the same physical path.

Example SATA shape:

```text
NUMA node 0
└─ PCI domain 0000
   └─ ... PCIe bridge / port
      └─ ... SATA/AHCI controller
         host PCIe Gen5 x16
         ├─ libata port ata3 — host3
         │  └─ SCSI address 3:0:0:0
         │     └─ SATA link — 6.0 Gb/s
         │        └─ /dev/sda
         └─ libata port ata4 — host4
            └─ SCSI address 4:0:0:0
               └─ SATA link — 6.0 Gb/s
                  └─ /dev/sdb
```

For SATA, a PCIe generation shown at the AHCI controller is the **host-controller PCIe interconnect**, not the SATA drive link. The actual drive-side link is reported separately as SATA speed.

On macOS, parameterless `topology` uses a fast non-disruptive physical-disk inventory and shows `Collecting topology...` while it runs. It does not perform SMART health collection or repeatedly invoke slow native-NVMe inventory just to draw the all-drive tree.

### `report`

Save a human or JSON report:

```bash
sudo nvme-doctor report nvme0 -o report.txt
sudo nvme-doctor report nvme0 --json -o report.json
```

### `diff`

Compare two saved JSON reports:

```bash
sudo nvme-doctor report nvme0 --json -o before.json
# perform one controlled reboot / power / workload event
sudo nvme-doctor report nvme0 --json -o after.json
nvme-doctor diff before.json after.json
```

This is the preferred way to establish counter changes or a real trend. Lifetime counters such as NVMe unsafe shutdowns do not contain per-event cause or timestamp information by themselves.

### `watch`

```bash
sudo nvme-doctor watch nvme0
sudo nvme-doctor watch nvme0 --interval 10 --count 12
sudo nvme-doctor watch nvme0 --json-lines
```

`watch` reports meaningful changes in collected counters/findings; it does not perform remediation.

## What is collected

### NVMe

Where supported by the OS/tooling:

- NVMe SMART/Health critical warnings;
- available spare and threshold;
- Percentage Used/endurance;
- media/data-integrity errors;
- temperature and thermal history;
- unsafe shutdowns, power cycles and power-on hours;
- data read/written and host command counters;
- controller busy time;
- NVMe error-information history.

Linux can additionally expose:

- PCIe current/max generation and width;
- endpoint/upstream topology and down-training;
- NUMA locality;
- PCIe AER counters;
- target-scoped reset/timeout/controller-down events;
- ASPM/APST context as a hypothesis, never as proof by itself.

### SATA / ATA

NVMe Doctor uses high-signal ATA SMART evidence without assigning universal meaning to every vendor-specific attribute:

- SMART overall-health and threshold failures;
- reallocated, pending and offline-uncorrectable sectors;
- reported uncorrectable errors;
- command timeouts;
- UDMA CRC errors as cable/connector/backplane/transport evidence;
- temperature, power-on hours and power cycles;
- SMART error log and self-test log;
- SATA revision and current/max interface speed when exposed;
- explicit reserved-space attributes and their thresholds;
- vendor-normalized media-wear indicators when their meaning is sufficiently established.

For ATA, smartctl's synthesized top-level `spare_available` is **not** blindly treated as literal spare NAND remaining. Explicit reserve-space SMART attributes take precedence, and SMART 5 uses its RAW reallocated-sector count rather than its vendor-normalized VALUE.

## USB storage

USB is treated as a separate failure domain from the underlying SSD/HDD.

### Linux

NVMe Doctor records the USB/SCSI path where available, including enclosure path, UAS versus `usb-storage`, SCSI attachment, and target-correlated USB/SCSI reset or I/O events. Native SSD PCIe ancestry hidden behind a USB bridge is reported as unavailable rather than invented.

### macOS USB-to-SATA

Probe order is conservative and non-disruptive first:

1. smartctl automatic device detection;
2. explicit SAT mode;
3. direct read-only USB SAT only as a last resort when explicitly/interaction-safely permitted.

Some bridges work with plain `smartctl /dev/diskN` but fail when `-d sat` is forced; automatic detection therefore comes first.

### macOS Realtek RTL9210 USB-to-NVMe

When smartctl cannot reach NVMe SMART through the enclosure, NVMe Doctor can use a direct read-only libusb backend for RTL9210. Interactive root checks may temporarily unmount/eject/capture the enclosure when safe; mounted media requires confirmation. Non-interactive direct access requires `--direct-usb`.

Optional slower RTL9210 Error Information/Firmware Slot reads require `--usb-extra-logs`.

Direct USB access is operationally disruptive even though the drive commands are read-only: close applications using the disk first. See [docs/MACOS.md](docs/MACOS.md) for the exact safety model and limitations.

## Platform capability summary

| Capability | Linux | macOS |
|---|---|---|
| Native NVMe SMART | `nvme-cli`, fallback/supplemental `smartctl` | `smartctl` when exposed |
| ATA/SATA SMART | `smartctl` | `smartctl` |
| PCIe generation/width | sysfs | partial for native NVMe where exposed |
| PCIe AER | yes | unavailable |
| PCIe/NUMA ancestry | yes | limited |
| SATA link speed | `smartctl` when exposed | `smartctl` when exposed |
| USB-to-SATA health | smartctl/SAT where supported | smartctl auto → SAT → conservative direct SAT fallback |
| USB-to-NVMe health | smartctl/SNT where supported | smartctl where exposed; RTL9210 direct backend available |
| Target-scoped OS events | kernel log | only when a stable target identity can be matched |

Capability disclosure is part of the diagnosis. Missing platform data does not become a zero/clean reading.

## Requirements

### Base

- Python 3.9+

The pre-built standalone itself has no pip runtime dependencies.

### Linux recommended tools

Depending on the drive/path:

```bash
# Debian/Ubuntu
sudo apt install nvme-cli smartmontools pciutils
```

Useful built-ins include sysfs, `journalctl`/`dmesg`, and udev metadata.

### macOS recommended tools

```bash
brew install smartmontools
```

For optional direct USB backends:

```bash
brew install libusb
```

Built-in `diskutil` and, for native NVMe detail paths, `system_profiler` are used where appropriate.

## Build and installation

The root `nvme-doctor` executable is pre-built.

Rebuild from `src/`:

```bash
./build.sh
# or
make build
```

Install the existing pre-built artifact:

```bash
sudo ./install.sh install
# or
sudo make install
```

**Build and install are intentionally separate.** `install.sh` never rebuilds the program; if the root executable is missing, run `./build.sh` first.

Default install location is `/usr/local/bin/nvme-doctor`; override with `PREFIX`/`DESTDIR` if required.

## Exit codes

- `0` — collection completed without warning/critical findings;
- `1` — warning/degraded result;
- `2` — critical result;
- `3` — evidence incomplete;
- `4` — command/input/collection error.

The assessment's human verdict (`HEALTHY`, `DEGRADED`, `AT RISK`, `CRITICAL`, `INCOMPLETE`) is intentionally richer than the process-status category used for exit codes.

## Safety and privacy

The diagnostic commands do not format, sanitize, flash firmware, reset controllers, modify namespaces, change ASPM/APST settings, or write user data.

macOS direct USB backends may temporarily unmount/eject/capture an enclosure to issue read-only drive commands and then restore the original mount state. This is a deliberate exception to “non-disruptive collection” and requires appropriate permission.

Reports can contain drive serial numbers, hostnames, PCI addresses, USB paths, and log fragments. Review them before publishing.

See [SECURITY.md](SECURITY.md) for the concise security policy.

## Development

```bash
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install -e . pytest
python3 -m pytest
./build.sh
./nvme-doctor --version
```

Additional design notes:

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
- [docs/DIAGNOSTIC-RULES.md](docs/DIAGNOSTIC-RULES.md)
- [docs/MACOS.md](docs/MACOS.md)
- [CONTRIBUTING.md](CONTRIBUTING.md)

## License

GPL-3.0-or-later. See [LICENSE](LICENSE).

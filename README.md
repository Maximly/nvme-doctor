# NVMe Doctor

**NVMe SSD diagnostics and root-cause analysis for Linux and macOS.**

NVMe Doctor correlates standards-defined NVMe SMART/error data with the evidence the host OS can expose, then explains the strongest diagnosis supported by that evidence.

It is designed to answer a different question from `nvme-cli`, `smartctl`, `lspci`, or `system_profiler`:

> Not only “what do the counters say?”, but “what is the strongest explanation supported by the available evidence?”

NVMe Doctor is GPL-3.0-or-later software from KernelSoft.

## Status

`0.4.0` adds a dedicated hardware-topology command that traces NUMA locality through the PCIe ancestry to the NVMe controller and namespaces, including per-hop link capabilities and down-training checks. It retains the richer health/lifetime diagnostics and Linux/macOS backends from earlier releases. The normal diagnostic path is **read-only**. NVMe Doctor does not automatically reset controllers, change ASPM/APST or other power settings, format namespaces, sanitize drives, or flash firmware.

## Quick start

No installation is required.

### Linux

```bash
git clone https://github.com/KernelSoft/nvme-doctor.git
cd nvme-doctor
sudo ./nvme-doctor check nvme0
```

### macOS

The built-in macOS backend can discover native NVMe devices with `system_profiler`. Full NVMe SMART/Health data requires `smartctl` from smartmontools.

With Homebrew:

```bash
brew install smartmontools
git clone https://github.com/KernelSoft/nvme-doctor.git
cd nvme-doctor
sudo ./nvme-doctor check disk0
```

If the Mac has exactly one discoverable NVMe device, the device may be omitted:

```bash
sudo ./nvme-doctor check
```

The repository launcher uses only Python's standard library; there are no pip dependencies for normal use. Python 3.9+ is required.

## Platform capabilities

Linux exposes considerably more low-level PCIe/NVMe evidence than macOS. NVMe Doctor reports this explicitly instead of pretending the same checks ran on both platforms.

| Capability | Linux | macOS |
|---|---|---|
| NVMe SMART/Health | `nvme-cli`, fallback `smartctl` | `smartctl` |
| NVMe error information | `nvme-cli`, supplemental `smartctl` | `smartctl` when exposed |
| Device discovery | sysfs | `system_profiler SPNVMeDataType` + `smartctl --scan[-open]` |
| PCIe link speed/width | sysfs | partial, where `system_profiler` exposes it |
| PCIe AER counters | yes | not exposed by this backend |
| PCIe ancestry/topology | yes | not exposed by this backend |
| ASPM/APST context | yes | not exposed by this backend |
| Target-scoped OS logs | current-boot kernel log | only when a stable target identity can be matched |
| USB-NVMe bridge type | via normal Linux tools | preserves `smartctl -d ...` hints from scan results |

A macOS report therefore includes a `CAPABILITIES` section such as:

```text
CAPABILITIES
  NVMe SMART/Health      yes
  NVMe error log         yes
  PCIe link              partial
  PCIe AER               unavailable
  PCIe topology          unavailable
  Power-state analysis   unavailable
  Target-scoped OS log   unavailable
```

## Example

```text
$ sudo ./nvme-doctor check disk0
NVMe Doctor  |  OK

DEVICE
  Platform             macOS 15.x
  Controller           disk0
  Device               /dev/disk0
  Model                APPLE SSD ...
  Firmware             ...
  State                live

HEALTH
  Temperature          41°C
  Percentage used      3%
  Available spare      100%
  Media errors         0
  Unsafe shutdowns     2
  Error log entries    0

DIAGNOSIS
  1. [INFO] Unsafe shutdowns have been recorded
     SMART reports 2 unsafe shutdown(s).
     Confidence: HIGH
```

On Linux, additional sections can include PCI address, current/max link negotiation, NUMA node, AER counters, PCIe topology, and controller-specific kernel events.

## Why another NVMe tool?

Existing tools are excellent at exposing mechanisms and counters:

- `nvme-cli` exposes Linux NVMe controller commands and logs;
- `smartctl` exposes standards-defined drive health on Linux and macOS;
- `lspci` and Linux sysfs expose PCIe state;
- `journalctl`/`dmesg` contain Linux reset and timeout evidence;
- macOS `system_profiler` exposes native NVMe inventory and some link metadata.

The difficult part during a real failure is correlating the evidence without overclaiming. NVMe Doctor is the diagnosis layer.

## What it checks

### Cross-platform NVMe health

Where the OS/tooling exposes the relevant NVMe SMART/Health log:

- critical warning;
- available spare versus threshold;
- Percentage Used / endurance;
- media/data-integrity errors;
- temperature;
- unsafe-shutdown history plus its ratio to recorded power cycles;
- power cycles and power-on hours;
- lifetime data read/written and host command counts;
- controller busy time and temperature/thermal-management history;
- NVMe error-information log count.

### Linux-specific evidence

- I/O timeouts and controller resets;
- failed resets/controller-down symptoms;
- PCIe link/DPC events;
- corrected and uncorrected AER evidence;
- current versus maximum PCIe generation, speed and width;
- endpoint BDF, driver and NUMA locality;
- parent PCIe topology;
- `nvme_core.default_ps_max_latency_us`;
- active PCIe ASPM policy.

Power management is deliberately handled as a **hypothesis**. NVMe Doctor does not claim ASPM/APST caused a reset merely because it is enabled.

### macOS-specific behavior

- discovers native NVMe devices through `system_profiler SPNVMeDataType -json`;
- normalizes `diskN`, `/dev/diskN`, `rdiskN`, and slices to the whole disk;
- gets full NVMe health from Darwin `smartctl` when available;
- records macOS `SMART Status` as context, but does **not** treat `Verified` alone as a complete NVMe health assessment;
- preserves `smartctl --scan-open` device types such as `nvme` or USB-NVMe bridge types (`snt...`) and reuses them for the health query;
- never lets an unscoped macOS storage log event create a target-specific reset/timeout diagnosis.

## Hardware topology

On Linux, `topology` traces the selected SSD from CPU/NUMA locality through its actual sysfs PCIe ancestry to the NVMe endpoint and block namespaces. It does not run SMART/admin commands and normally does not require root:

```bash
./nvme-doctor topology nvme0
./nvme-doctor topology /dev/nvme0n1
./nvme-doctor topology nvme0 --json
```

Example:

```text
NVMe Doctor  |  TOPOLOGY

Controller           nvme0
Model                Samsung SSD 9100 PRO 2TB

HARDWARE PATH
NUMA node 0  [local CPUs 0-31]
└─ PCI domain 0000
   └─ 0000:00:03.1  PCIe bridge / port — PCI bridge: AMD Root Port
      Gen5 x16; driver pcieport; power D0
      └─ 0000:70:00.0  PCIe bridge / port — PCI bridge: PCIe Switch Downstream Port
         Gen5 x8; driver pcieport; power D0
         └─ 0000:71:00.0  NVMe controller — Non-Volatile memory controller: Samsung Electronics ...
            Gen5 x4; driver nvme; power D0
            └─ /dev/nvme0n1  [NSID 1, 2.00 TB, LBA 512 B]

PATH CHECK
  Endpoint link        Gen5 x4 — at device maximum
  Down-trained hops    none detected
  CPU locality         NUMA 0; CPUs 0-31
```

For each PCIe hop NVMe Doctor reports the BDF, best-effort `lspci` description, current/max generation and width, driver, power state, and NUMA data when the kernel exposes them. A hop whose negotiated generation or width is below that device/port's own maximum is called out as down-trained.

macOS does not expose an equivalent supported PCIe/NUMA ancestry through this backend, so `topology` returns the NVMe endpoint plus an explicit incomplete-topology note rather than inventing a path.

## Compare before/after state

NVMe SMART stores a cumulative unsafe-shutdown count, but it does **not** store a per-event reason or timestamp. To determine whether a particular reboot, power event, firmware test, or workload changed the drive state, save reports before and after it:

```bash
sudo ./nvme-doctor report nvme4 --json -o before.json
# perform one controlled reboot/power/workload event
sudo ./nvme-doctor report nvme4 --json -o after.json
./nvme-doctor diff before.json after.json
```

The comparison reports deltas for power cycles, unsafe shutdowns, media errors, error-log entries, data read/written, temperature-history counters and negotiated PCIe generation/width. If the unsafe-shutdown counter increased, NVMe Doctor states that the exact cause cannot be recovered from SMART alone and points you back to the host/platform evidence from that interval.

## Requirements

### Linux

- Linux
- Python 3.9+
- `/sys` mounted normally

Strongly recommended:

- `nvme-cli`
- `pciutils` (`lspci`)
- `smartmontools` (`smartctl`)
- systemd `journalctl` or `dmesg`

Ubuntu/Debian:

```bash
sudo apt install nvme-cli pciutils smartmontools
```

Fedora/RHEL-family:

```bash
sudo dnf install nvme-cli pciutils smartmontools
```

### macOS

- macOS
- Python 3.9+
- built-in `system_profiler`
- `smartmontools` strongly recommended and required for a complete NVMe SMART/Health assessment

Homebrew:

```bash
brew install smartmontools
```

NVMe Doctor intentionally does not bundle or auto-install Homebrew or smartmontools.

## Doctor-style assessment

`check` does more than print SMART counters. It turns the collected evidence into a current-health assessment before showing raw details:

```text
DOCTOR'S ASSESSMENT
  Verdict              HEALTHY NOW
  Media / integrity    CLEAN — no SMART critical warning or media errors
  PCIe / controller    CLEAN — Gen5 x4 at endpoint maximum; no reset/timeout evidence
  Thermal              NORMAL — 46°C
  Endurance            EXCELLENT — 0% used; 100% spare
  History              REVIEW — unsafe shutdowns are historical context, not proof of a current fault
```

The detailed `FINDINGS / EVIDENCE` section explains why each non-clean condition was raised and suggests the next diagnostic step.

## Usage

### List devices

```bash
./nvme-doctor list
```

Linux examples are normally `nvme0`, `/dev/nvme0`, or a namespace such as `/dev/nvme0n1`.

macOS examples are normally `disk0` or `/dev/disk0`. Raw-device and slice forms such as `/dev/rdisk0` and `disk0s1` are normalized to `/dev/disk0`.

### Diagnose

Linux:

```bash
sudo ./nvme-doctor check /dev/nvme0
```

macOS:

```bash
sudo ./nvme-doctor check /dev/disk0
```

The shorthand form also works:

```bash
sudo ./nvme-doctor /dev/nvme0
sudo ./nvme-doctor /dev/disk0
```

### Detailed evidence

```bash
sudo ./nvme-doctor check /dev/nvme0 --verbose
sudo ./nvme-doctor check /dev/disk0 --verbose
```

### JSON report

```bash
sudo ./nvme-doctor report /dev/disk0 --json -o disk0-report.json
```

### Watch for changes

```bash
sudo ./nvme-doctor watch /dev/nvme0 --interval 2
sudo ./nvme-doctor watch /dev/disk0 --interval 2
```

`watch` emits a new report only when relevant health/counter/finding state changes.

## Exit codes

| Code | Meaning |
|---:|---|
| 0 | no warning/critical finding and sufficient health evidence |
| 1 | one or more warnings |
| 2 | one or more critical findings |
| 3 | diagnostic evidence is incomplete |
| 4 | usage/collection/program error |

Informational findings do not by themselves make the command fail.

## Interpretation philosophy

NVMe Doctor separates **facts**, **correlations**, and **hypotheses**.

Examples:

- `media_errors > 0` is direct controller evidence;
- repeated Linux reset/timeouts plus target-related AER/link errors plus clean media SMART is a strong PCIe-path correlation;
- reset/timeouts while ASPM/APST is enabled is only a reason to run a reversible A/B test, not proof that power management is broken;
- macOS `SMART Status: Verified` is useful context but insufficient by itself for an `OK` result if the NVMe SMART/Health log could not be read.

## Optional system-wide installation

Installation is not required. If you want `nvme-doctor` in `/usr/local/bin`:

```bash
sudo ./install.sh install
```

Remove it with:

```bash
sudo ./install.sh remove
```

## Development

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e . pytest
pytest
```

GitHub Actions runs tests on both Ubuntu and macOS.

## Privacy

A JSON report can contain hardware serial numbers, hostname, OS log fragments and PCI addresses. Review reports before posting them publicly.

## Roadmap

- real-hardware validation across Apple Silicon and Intel Macs;
- broader USB-NVMe enclosure/bridge compatibility reporting;
- before/after reboot snapshots for disappearing-drive cases;
- upstream bridge/root-port AER correlation on Linux;
- persistent history with counter deltas;
- vendor/firmware advisory matching without sending telemetry by default;
- performance diagnosis (thermal throttling, bad link negotiation, queue behavior);
- anonymized report mode for public bug reports.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Diagnostic rules should be evidence-based, conservative, and accompanied by tests.

## License

GNU General Public License v3.0 or later. See [LICENSE](LICENSE).

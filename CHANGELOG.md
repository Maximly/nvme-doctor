# Changelog

## 0.4.2 - 2026-08-09

- Keep the flattened repository source layout (`src/*.py`) as the canonical layout.
- Fix the repository launcher to execute `src.cli` from the checkout.
- Update all test imports to the flattened source package.
- Update the Makefile test path for the new layout.
- Update optional installation to copy flat source modules into the installed `nvme_doctor` package.
- Update setuptools mapping so `pip install .` still exposes the public `nvme_doctor` package and `nvme-doctor` command without requiring `src/nvme_doctor/`.
- Add a regression test that rejects accidental recreation of `src/nvme_doctor/` and verifies the repository launcher.

## 0.4.0 - 2026-08-09

- Added `nvme-doctor topology [device]` with human-readable and JSON output.
- Walks the real Linux sysfs PCI ancestry from NUMA locality/root domain through PCIe bridges/switch ports to the NVMe endpoint.
- Shows current/max PCIe generation and width, BDF, driver, power state, optional `lspci` descriptions, and local CPU list.
- Lists NVMe namespaces with NSID, capacity, and logical block size.
- Adds topology health checks for endpoint/hop down-training relative to each hop's own maximum capability.
- Topology collection is independent of SMART/admin commands and normally works without root.
- macOS returns an explicit partial topology because supported NUMA/PCI ancestry is not available from the current backend.

## 0.3.1 - 2026-08-09

- Fix smartctl-style NVMe version rendering (`{"string":"2.0","value":131072}` now prints `2.0`).
- Add a prominent `DOCTOR'S ASSESSMENT` section with an overall verdict, current-fault conclusion, and domain-specific interpretation for media/integrity, PCIe/controller path, thermals, endurance, and historical counters.
- Separate current-health conclusions from historical counters such as unsafe shutdowns so an old counter does not look like an active SSD fault.
- Add an explicit next recommendation for each report.
- Rename the detailed section to `FINDINGS / EVIDENCE`; raw facts remain available but no longer substitute for diagnosis.

## 0.3.0 - 2026-08-09

- Show negotiated PCIe generation (`Gen3`/`Gen4`/`Gen5`, with Gen1/2/6 mapping supported) separately from the NVMe protocol version.
- Show NVMe protocol version when exposed by `nvme id-ctrl` or `smartctl`.
- Expand SMART output into `HEALTH` and `LIFETIME / USAGE` sections.
- Add spare threshold, power cycles, power-on hours, unsafe-shutdown ratio, lifetime data read/written, host command counts, controller-busy time, temperature-history time and thermal-management transitions.
- Decode NVMe SMART critical-warning bits.
- Show recent non-empty NVMe error-log records with status/queue/command/namespace/LBA context when available.
- Clarify unsafe-shutdown semantics: SMART stores a cumulative count, not per-event reasons or timestamps.
- Add `nvme-doctor diff before.json after.json` to identify exactly which lifetime counters and PCIe link parameters changed across a controlled reboot/power/workload interval.
- Add regression tests for PCIe-generation mapping, lifetime-unit conversion, enriched rendering and report comparison; total suite now 32 tests.

## 0.2.0 - 2026-08-08

- Add a native macOS/Darwin backend.
- Discover native NVMe devices through `system_profiler SPNVMeDataType -json`.
- Accept macOS `diskN`, `rdiskN`, and slice names and normalize them to the whole disk.
- Use Darwin `smartctl -a -j` for standards-defined NVMe SMART/Health information.
- Preserve `smartctl --scan-open` `-d` hints, including USB-NVMe bridge types, and reuse them for health queries.
- Fall back from `smartctl --scan-open` to `smartctl --scan` for non-root discovery.
- Add explicit per-platform capability reporting so unavailable macOS PCIe/AER checks cannot be mistaken for clean evidence.
- Record macOS `SMART Status` as context without treating it as a complete NVMe health assessment.
- Prevent unscoped macOS storage log events from creating target-specific reset/timeout findings.
- Add macOS to the GitHub Actions test matrix.
- Add macOS/backend regression tests for discovery, health, bridge passthrough and capability isolation; total suite now 25 tests.

## 0.1.1 - 2026-08-08

- Fix `nvme-cli` JSON option ordering (`nvme <command> <device> -o json`).
- Preserve valid `smartctl -j` JSON when smartctl returns non-zero health-status bits.
- Use smartctl NVMe health JSON as a fallback when nvme-cli health collection is unavailable.
- Never report `OK` when NVMe health evidence could not be collected; report `INCOMPLETE` instead.
- Add a repository-local `./nvme-doctor` launcher so installation is optional.
- Add regression tests for collector failures and smartctl exit-status semantics.

## 0.1.0 - 2026-08-08

Initial public release.

- Discover NVMe controllers from sysfs.
- Collect NVMe SMART, controller identity and error logs through `nvme-cli` when available.
- Collect PCIe link state, NUMA locality and AER counters from sysfs.
- Collect relevant current-boot kernel log evidence.
- Detect SMART critical warnings, low spare, endurance, media errors and high temperature.
- Detect PCIe AER errors and degraded link negotiation.
- Correlate NVMe resets/timeouts with PCIe evidence.
- Offer a conservative APST/ASPM A/B-test hypothesis when power management and reset symptoms coexist.
- JSON/text reports and change-oriented watch mode.
- Diagnostic-only safety model; no automatic controller resets or power-policy changes.

# Changelog

## 1.1.21 - 2026-08-16

- Audited and refreshed README, architecture, diagnostic-rule, macOS, contributing, and security documentation against current CLI/source behavior.
- Removed stale README references to 1.1.8, `probe-needed`, obsolete list layout, and pre-all-drive topology behavior.
- Documented current `list` Health/Size columns, assessment semantics, merged all-drive topology, SATA host-vs-drive link distinction, libata port mapping, and fast macOS topology path.
- Updated macOS USB-SATA documentation to the real probe order: smartctl automatic detection -> explicit SAT -> direct read-only SAT fallback, including SAT16/SAT12 and BOT recovery behavior.
- Corrected historical 1.1.3-1.1.5 and 1.1.9-1.1.10 changelog entries that had drifted from the actual releases.
- Added documentation-staleness regression checks so key user-facing docs must track the current version and output semantics.

## 1.1.20

- Make parameterless `nvme-doctor topology` show an immediate `Collecting topology...` spinner on human consoles; `topology --debug` exposes elapsed collection stages instead.
- Replace the macOS all-drive topology path with a lightweight `diskutil` physical-disk collector. Topology no longer runs SMART health probes or repeatedly invokes `system_profiler` merely to draw the tree.
- Fix a major macOS latency bug where all-drive topology performed expensive discovery and then repeated full per-device macOS snapshot collection, causing `system_profiler` and smartctl scans to run multiple times even with one internal SSD.
- Classify internal Apple SSDs from fast diskutil transport/model evidence for all-drive topology, avoiding a slow `system_profiler SPNVMeDataType` round trip when it adds no topology detail.
- Keep all-drive macOS topology non-disruptive and health-independent; explicit per-device topology behavior is unchanged.

## 1.1.19

- `nvme-doctor topology` with no device now discovers all physical drives and renders them in one merged hardware tree.
- Shared NUMA, PCIe bridge and controller branches are deduplicated, so multiple SATA disks behind one AHCI controller appear as separate libata-port branches under the same controller.
- USB storage is grouped by host/port/bridge without guessing the underlying ATA/NVMe protocol.
- All-drive topology collection runs per-device probes concurrently and remains non-disruptive on macOS; direct USB capture still requires an explicit device.
- `topology --json` without a device returns the per-drive topology records plus any collection errors.

## 1.1.18

- Linux ATA/SATA topology now shows the per-disk libata port (`ataN`), SCSI host and SCSI address beneath a shared AHCI controller.
- Two SATA disks on the same PCI AHCI controller no longer appear to occupy the same host-side location.
- The `ataN` value is labeled as a Linux libata port rather than a physical chassis connector number.

## 1.1.17

- Fixed SATA topology wording so a SATA drive is never described as having a PCIe Gen link.
- Native ATA paths now distinguish the AHCI controller's host-side PCIe link from the drive-side SATA link.
- Added an explicit SATA link node plus `Drive SATA link` summary when smartctl reports interface speed.
- Replaced ATA `Endpoint link`/`PCI endpoint` labels with `Host PCIe link`/`Host controller`.
- Added regression coverage for a Gen5 x16 AHCI controller hosting a 6.0 Gb/s SATA SSD.

## 1.1.16

- Fix `topology` for non-NVMe disks: native Linux ATA/SATA `sdX` devices are no longer rendered as USB/SCSI bridges containing an NVMe SSD.
- Resolve native Linux libata block-device ancestry back to the actual PCI SATA/AHCI controller and show the host PCI/NUMA path when available.
- Render USB storage leaves by confirmed protocol (`ATA/SATA drive`, `NVMe SSD`, `SCSI disk`, or generic storage device) instead of assuming every translated block device is NVMe.
- Use udev/sysfs identity and libata path evidence when an unprivileged smartctl identity probe fails; topology no longer tells SATA users to rerun for “NVMe identity”.
- Make topology notes and path summaries protocol-aware on Linux and macOS, including USB-to-SATA devices.
- Add regressions for native Solidigm-style SATA `/dev/sda` topology and macOS USB-SATA topology.

## 1.1.15

- Prefer smartctl's automatic device detection for macOS USB-SATA disks before forcing `-d sat`; observed UGREEN USB-C/SATA bridges expose full ATA SMART with plain smartctl even when explicit SAT mode fails.
- `list` can now report `GOOD/WARN/FAIL` for macOS USB-SATA devices when discovery has already proven a fast, non-disruptive smartctl backend; direct/eject USB access is still never used by `list`.
- Recover USB Mass Storage Bulk-Only Transport after switching UAS-capable USB-SATA bridges to their BOT fallback alternate setting.
- Perform the standard Bulk-Only Mass Storage Reset and clear both bulk endpoint halts before SAT commands and between failed SAT16/SAT12 attempts.
- Verify BOT responsiveness with standard SCSI INQUIRY before issuing ATA PASS THROUGH, so transport failure is distinguished from unsupported SAT.
- Preserve UAS-vs-BOT-fallback details in direct USB-SATA bridge evidence.

## 1.1.14

- Added a macOS direct USB-SATA SAT fallback for positively identified ATA/SATA SSDs when Darwin smartctl cannot access SMART through the enclosure.
- The direct path uses safe whole-disk eject/restore plus libusb BOT and read-only ATA PASS THROUGH(16) for IDENTIFY, SMART data, thresholds, and SMART RETURN STATUS when available.
- Direct SAT refuses ambiguous USB mappings and falls back to `INCOMPLETE` without guessing when the enclosure does not support the required path.
- Direct ATA parsing no longer assigns Solidigm-specific meanings to vendor SMART IDs on unrelated drives; unknown vendor attributes remain raw by ID.
- ATA assessment no longer says SMART overall-health passed when an overall SMART status was not actually obtained; it reports collected attributes/threshold state instead.

## 1.1.13

- Fixed macOS USB SSD protocol fallback: a failed `smartctl -d sat` probe no longer automatically turns every external SSD into an NVMe/SNT candidate.
- Added conservative USB media protocol hints from explicit identity strings; WD/SanDisk SA510 is correctly retained as ATA/SATA when its USB bridge hides SAT SMART.
- macOS USB-SATA checks with unavailable SMART now report `INCOMPLETE` with ATA/SATA-specific capabilities and recommendations instead of misleading NVMe/SNT passthrough text.
- macOS `list` keeps such drives as `ATA` / `USB -> SATA`; quick health remains `-` when SMART is not non-disruptively available.

## 1.1.12

- `check` now rejects nonexistent device nodes before any SMART/NVMe probing.
- A missing target such as `/dev/sdb` now returns a clear `device not found` error and exit code 4 instead of a fabricated `INCOMPLETE` SCSI snapshot.

## 1.1.11

- Rename the healthy quick-list status from `OK` to `GOOD` for clearer user-facing health semantics.
- `nvme-doctor list` health values are now `GOOD`, `WARN`, `FAIL`, or `-`.

## 1.1.10

- Removed the `State` column from human `nvme-doctor list` output because internal labels such as `live`/`direct-ready` were not useful as a quick inventory field.
- Added the `Size` column using already-collected OS capacity data so listing does not require another slow probe.
- Native NVMe controller size is derived from visible namespaces; Linux SATA/USB uses block-device size; macOS uses diskutil capacity.

## 1.1.9

- Added the compact `Health` column to `nvme-doctor list`.
- Quick list health uses only non-disruptive SMART/OS evidence and reports `OK`, `WARN`, `FAIL`, or `-` in this release.
- Per-disk quick-health probes run concurrently with a short timeout so many-drive systems do not serialize the delay.
- macOS USB bridges that would require eject/direct capture are deliberately shown as `-`; use `check` for full health collection.

## 1.1.8

- Replace the ambiguous `HEALTHY NOW` assessment with `HEALTHY`.
- Separate current health from `Near-term risk` and `Trend`.
- Use `DEGRADED` for warning-level transport/controller/link/thermal conditions.
- Use `AT RISK` for warning-level media/endurance evidence.
- A single check no longer claims a stable/deteriorating trend; it reports `UNKNOWN` until reports are compared with `nvme-doctor diff`.
- Keep future-risk wording explicit that it is not a time-to-failure prediction.

## 1.1.7

- Fix misleading ATA SSD spare interpretation: ignore smartctl's synthesized top-level `spare_available` when explicit ATA reserve-space attributes are available.
- Use explicit `Available_*Reserv*` / spare-space SMART attributes for SATA reserve reporting, including threshold and margin.
- Keep SMART 5 `Reallocated_Sector_Ct` strictly as a RAW reallocated-sector counter; its normalized VALUE is never interpreted as spare capacity.
- Report vendor-defined media wear as a separate normalized indicator instead of converting it to generic endurance-used percentage.
- Add a Solidigm D3-S4520 regression fixture reproducing `SMART 5 VALUE=47 RAW=0`, reserve attributes 170/232 at 100 with threshold 10, and wear indicator 233 at 100.

## 1.1.6

- Fixed SATA SSD percentage rendering when smartctl returns vendor JSON objects such as `spare_available: {"current_percent": 47}`.
- Normalize ATA `spare_available` and `endurance_used` to scalar percentages before diagnosis/rendering on Linux and macOS.

## 1.1.5

- Unified `check` and `list` SATA identity/state resolution so a native libata drive keeps model, serial, firmware, protocol and live state even when smartctl returns partial/non-zero status.
- Ignore misleading `-d scsi` scan hints for native Linux libata disks and probe ATA backends in a conservative order (`-d ata`, automatic detection, then SAT where appropriate).
- Keep valid ATA SMART JSON even when smartctl returns status bits, rather than discarding useful evidence solely because the process exit code is non-zero.
- Native SATA no longer reports a USB transport status line, and ATA capacity is labeled `Capacity` rather than `NVM capacity`.
- Added regression coverage for native SATA with smartctl status `0x04` plus udev/sysfs identity.

## 1.1.4

- Normalize Linux `/sys/class/block/sdX/device/state` value `running` to the user-facing live state.
- Add SATA identity fallback precedence across smartctl, udev, and sysfs so list output retains model, serial and firmware when SMART probing is ambiguous.

## 1.1.3

- Fixed native Linux SATA disks being dropped when smartctl returned valid but SCSI-like/ambiguous JSON.
- Treat a native `.../ataN/...` sysfs ancestry as SATA evidence.
- Retry ambiguous native SATA identity with `smartctl -d ata` and retain OS-visible physical disks instead of silently omitting them.

## 1.1.2

- Fix `nvme-doctor list` omitting SATA disks when `smartctl --scan` does not report them.
- Linux discovery now starts from `/sys/class/block/sd*` physical-disk inventory, then uses smartctl only to classify/enrich each disk.
- Keep Linux physical disks visible as `probe-needed` when SMART access requires root or an unsupported backend instead of silently dropping them.
- macOS discovery now probes `diskutil`-visible USB physical disks with a short read-only `smartctl -d sat -i` identity request so USB-SATA HDDs/SSDs can be classified even when smartctl scan output is incomplete.
- Add regression coverage for native Linux SATA without scan output, unprivileged Linux SATA inventory, and macOS USB-SATA without scan output.

## 1.1.1

- Separate build and install flows.
- Add `build.sh` as the release/developer build entry point; it rebuilds the root-level standalone `nvme-doctor`.
- `install.sh` no longer invokes Python, `tools/build_single.py`, or any temporary build directory. It installs only the pre-built root-level `nvme-doctor` and fails with a clear message when that artifact is missing or non-executable.
- `make build` delegates to `build.sh`; `make install` installs the existing artifact without an implicit rebuild.

## 1.1.0

- Add ATA/SATA SSD and HDD discovery and health collection on Linux and macOS.
- Add USB-SATA SAT bridge support through smartctl when pass-through is available.
- Normalize ATA SMART overall status, sector integrity counters, CRC errors, command timeouts, temperature, lifetime counters, error log and self-test log.
- Add SATA-specific diagnosis, assessment, rendering, capabilities and diff counters.
- Add SATA revision/current/max interface-speed reporting when smartctl exposes it.
- Preserve existing NVMe/RTL9210 behavior and keep the normal one-line Checking spinner UX.


## 1.0.13 - 2026-08-15

- Render the normal `Checking` spinner with fixed-width frames (`Checking.  ` / `Checking.. ` / `Checking...`) so cycling from three dots back to one never leaves stale characters.
- Hide the terminal cursor while the spinner is active and restore it before the final status/report, including exception cleanup.

## 1.0.12 - 2026-08-15

- Normal human `check` output now shows `NVMe Doctor 1.0.12` immediately followed by a single in-place `Checking.` / `Checking..` / `Checking...` animation.
- The transient `Checking...` line is erased completely before the final `NVMe Doctor  |  STATUS` report is printed.
- `--debug` disables the spinner and keeps the detailed elapsed-time stage trace on stderr. JSON/report output remains free of spinner control characters.

## 1.0.11 - 2026-08-15

- Simplify normal console output: print `NVMe Doctor 1.0.11` immediately, then stay quiet until the final status/report.
- Move all per-stage/timing output behind `--debug`; debug output is unbuffered on stderr.
- Fix macOS RTL9210 automatic capability probing crash: `rtl9210_inventory()` no longer references an undefined `progress` variable and accepts an optional debug callback.

## 1.0.10 - 2026-08-15

- Make progress a true top-level `check` UI: human text checks write unbuffered progress to stdout, while JSON/report modes keep it on stderr.
- Include the running build number in every progress line, e.g. `[CHECK 1.0.10 +0.4s] ...`, so an outdated installed binary is immediately obvious.
- On macOS, `sudo nvme-doctor check diskN` now automatically enters the read-only RTL9210 direct backend when the bridge is detected and NVMe SMART is otherwise unavailable. `--direct-usb` remains an explicit force option, but is no longer required for normal privileged checks.
- Preserve the preflight mount-state result and restore the original mounted state after direct access. JSON/report automation remains non-disruptive unless direct mode is explicitly requested.

## 1.0.9 - 2026-08-15

- Make live progress a global `check`/`report` feature instead of a macOS RTL9210-only feature.
- Use one unbuffered `[CHECK +Xs]` timer on stderr for native NVMe, USB/SNT-translated NVMe, and macOS direct-USB paths.
- Linux native NVMe now reports sysfs/PCIe discovery, SMART, Identify, Error Log, supplemental smartctl, PCIe details, and target-scoped kernel-log collection stages.
- Linux USB/SCSI NVMe now reports bridge-backend detection, sysfs USB/SCSI path collection, translated SMART/identity reads, and target-scoped kernel-event collection.
- macOS native and external-disk paths now report diskutil metadata, system_profiler inventory/link queries, smartctl scans/health reads, RTL9210 capability checks, and target-scoped storage-log collection.
- Rename the old `[USB ...]` progress prefix to transport-neutral `[CHECK ...]`.
- `--no-progress` now suppresses progress for every check path, not only macOS direct USB.

## 1.0.8 - 2026-08-15

- macOS USB progress is now written unbuffered to stderr even when stderr is not a TTY, so sudo/piped/wrapped invocations no longer look hung.
- Fixed the direct RTL9210 sequence: the bridge is now enumerated/opened only after `diskutil eject`, avoiding stale libusb handles and the long timeout/INCOMPLETE regression introduced by the safe-eject change.
- Direct-USB failures are reported immediately in progress output as well as in collection notes.
- Reduced post-read disk re-detection wait from 15 s to 8 s.

## 1.0.7 - 2026-08-15

- Show elapsed-time progress on stderr during interactive macOS checks, including disk inspection, mount/eject, RTL9210 capture, NVMe Identify/SMART reads, driver restoration, and remount.
- Keep JSON/report stdout clean; progress can be disabled with `--no-progress`.
- Progress callbacks are best-effort and cannot interfere with USB recovery/cleanup.


## 1.0.6 - 2026-08-15

- Fix macOS “Disk Not Ejected Properly” notifications during RTL9210 direct USB checks.
- Properly `diskutil eject` the whole USB disk before temporarily detaching the macOS USB storage driver.
- Reattach/re-probe the storage driver after the read-only BOT session and restore mounted state only when it existed before capture.
- Keep already-unmounted disks unmounted after the check.

## 1.0.5 - 2026-08-15

- Speed up macOS RTL9210 direct checks by reusing the preflight mount-state result instead of repeating `diskutil list`/`diskutil apfs list`.
- Skip redundant `diskutil unmountDisk` when the target disk is already proven to have no mounted volumes.
- Remove the fixed 300 ms post-detach sleep; claim the USB interface immediately and retry briefly only when libusb reports BUSY/NOT_FOUND.
- Reduce the normal RTL9210 BOT transfer timeout from 7 s to 2 s so a stalled bridge fails faster.
- Keep the normal USB health path to NVMe Identify + SMART only. Error Information/Firmware Slot reads are now opt-in with `--usb-extra-logs` because some RTL9210 firmware revisions stall before rejecting optional log pages.
- Add regression coverage for the unmounted fast path and mount-state reuse.

## 1.0.4 - 2026-08-15

- Preserve Linux smartctl bridge backends (`-d sntrealtek`, `sntjmicron`, `sntasmedia`, etc.) from scan through list/topology/check.
- Add Linux USB sysfs path inventory: VID:PID, port, negotiated speed, USB version, UAS/BOT driver, SCSI ancestry and host controller.
- Correlate target kernel logs with USB port/SCSI host tokens and diagnose USB resets, disconnects, UAS recovery, SCSI I/O errors and USB 3.x -> 480 Mb/s downshifts.
- Show USB transport details in text reports and topology output.
- macOS RTL9210 direct mode now restores the original mount state instead of mounting a disk that was already unmounted.
- Harden BOT CSW/tag/residue validation.
- RTL9210 direct mode opportunistically collects NVMe Error Information and Firmware Slot logs in addition to Identify + SMART; unsupported optional logs do not fail health collection.


## 1.0.3 - 2026-08-09

- Cross-platform NVMe diagnostics for Linux and macOS.
- Native Linux NVMe SMART, error, PCIe/AER and NUMA/topology analysis.
- USB/SCSI-translated NVMe support on Linux through smartctl pass-through.
- Direct Realtek RTL9210 NVMe Identify and SMART access on macOS through libusb.
- Safe automatic macOS direct-access policy: automatic when proven unmounted, interactive confirmation when mounted, explicit `--direct-usb` for non-interactive use.
- Topology output distinguishes USB bridge identity from the underlying NVMe SSD and no longer presents enclosure names as NVMe endpoints.
- Standalone single-file `nvme-doctor` executable, rebuilt by `install.sh` from the maintainable flat `src/*.py` source tree.
- JSON reports, before/after diff, watch mode, lifetime/endurance and thermal interpretation.

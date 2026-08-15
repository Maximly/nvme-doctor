# Changelog

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

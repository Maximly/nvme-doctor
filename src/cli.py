# SPDX-License-Identifier: GPL-3.0-or-later
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional

from . import __version__
from .collect import collect_snapshot, collect_topology, discover_controllers
from .diagnose import diagnose
from .diff import compare_reports, render_diff_text
from .render import render_json, render_text, render_topology_json, render_topology_text
from .util import format_bytes_decimal, normalize_device, normalize_macos_device, read_json, to_int
from .runner import Runner
from .platforms import platform_key, platform_label


EXIT_OK = 0
EXIT_WARNING = 1
EXIT_CRITICAL = 2
EXIT_INCOMPLETE = 3
EXIT_ERROR = 4


def _status_code(status: str) -> int:
    return {"OK": EXIT_OK, "WARNING": EXIT_WARNING, "CRITICAL": EXIT_CRITICAL, "INCOMPLETE": EXIT_INCOMPLETE}.get(status, EXIT_ERROR)


def _default_device() -> Optional[str]:
    controllers = discover_controllers()
    if len(controllers) == 1:
        return controllers[0]["device"]
    return None


def _device_or_error(value: Optional[str]) -> str:
    value = value or _default_device()
    if not value:
        raise ValueError("device is required when zero or multiple supported storage devices are present; run `nvme-doctor list`")

    if platform_key() == "darwin":
        _controller, device_path = normalize_macos_device(value)
    else:
        _controller, device_path = normalize_device(value)

    # Validate the normalized whole-device node before invoking smartctl/nvme
    # or constructing a diagnostic snapshot.  Without this guard smartctl can
    # return valid JSON/error bits for a nonexistent /dev/sdX and the collector
    # used to manufacture an INCOMPLETE ``SCSI disk`` record for it.
    if not os.path.exists(device_path):
        raise ValueError(f"device not found: {device_path}")

    return value


def _write_output(text: str, output: Optional[str]) -> None:
    if output:
        path = Path(output)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    else:
        sys.stdout.write(text)


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("device", nargs="?", help="NVMe controller/namespace or SATA/USB block device, e.g. /dev/nvme0, /dev/nvme0n1, /dev/sda, or disk4")
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    parser.add_argument("--verbose", "-v", action="store_true", help="include PCI topology and relevant kernel log lines")
    parser.add_argument("--kernel-lines", type=int, default=300, help="maximum number of relevant kernel log lines to keep (default: 300)")
    parser.add_argument(
        "--direct-usb", action="store_true",
        help="macOS RTL9210 only: temporarily unmount/capture the USB enclosure and read NVMe Identify/SMART directly (requires sudo + libusb)",
    )
    parser.add_argument(
        "--usb-extra-logs", action="store_true",
        help="macOS RTL9210 direct mode: also request NVMe Error Information and Firmware Slot logs (slower on bridges that reject optional pages)",
    )
    parser.add_argument(
        "--debug", action="store_true",
        help="show detailed collection stages and elapsed timing",
    )
    # Backward-compatible switch: suppress the normal console spinner.
    parser.add_argument("--no-progress", action="store_true", help=argparse.SUPPRESS)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="nvme-doctor",
        description="NVMe and SATA storage diagnostics and root-cause analyzer for Linux and macOS",
    )
    parser.add_argument("--version", action="version", version=f"nvme-doctor {__version__}")
    sub = parser.add_subparsers(dest="command")

    p_list = sub.add_parser("list", help="list supported NVMe and SATA/ATA devices visible to the current OS backend")
    p_list.add_argument("--json", action="store_true", help="emit JSON")

    p_check = sub.add_parser("check", help="collect evidence and diagnose one NVMe or SATA/ATA drive")
    _add_common(p_check)

    p_topology = sub.add_parser("topology", help="show the available host/transport topology for one storage device")
    p_topology.add_argument("device", nargs="?", help="NVMe controller/namespace or SATA/USB block device")
    p_topology.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    p_topology.add_argument(
        "--direct-usb", action="store_true",
        help="macOS RTL9210 only: permit temporary unmount/capture to identify the underlying NVMe SSD",
    )

    p_report = sub.add_parser("report", help="write a diagnostic report")
    _add_common(p_report)
    p_report.add_argument("-o", "--output", required=True, help="output file (.json recommended with --json)")

    p_diff = sub.add_parser("diff", help="compare two saved JSON reports (useful around reboot/power events)")
    p_diff.add_argument("before", help="before-report JSON file")
    p_diff.add_argument("after", help="after-report JSON file")
    p_diff.add_argument("--json", action="store_true", help="emit machine-readable JSON")

    p_watch = sub.add_parser("watch", help="watch counters/findings and report meaningful changes")
    p_watch.add_argument("device", nargs="?", help="NVMe controller/namespace or SATA/USB block device")
    p_watch.add_argument("--interval", type=float, default=5.0, help="poll interval in seconds (default: 5)")
    p_watch.add_argument("--count", type=int, default=0, help="number of samples; 0 means until interrupted")
    p_watch.add_argument("--json-lines", action="store_true", help="emit one compact JSON object per changed sample")
    p_watch.add_argument("--kernel-lines", type=int, default=300)

    return parser


def _normalize_argv(argv: List[str]) -> List[str]:
    # Friendly shorthand: `nvme-doctor /dev/nvme0` == `nvme-doctor check /dev/nvme0`.
    if not argv:
        return argv
    known = {"list", "check", "topology", "report", "diff", "watch", "-h", "--help", "--version"}
    if argv[0] not in known:
        name = Path(argv[0]).name
        if "nvme" in name or name.startswith("disk") or name.startswith("rdisk") or re.fullmatch(r"sd[a-z]+(?:\d+)?", name):
            return ["check"] + argv
    return argv


def _watch_key(report_dict: Dict[str, Any]) -> Dict[str, Any]:
    snapshot = report_dict.get("snapshot", {})
    smart = snapshot.get("smart", {})
    pci = snapshot.get("pci", {})
    return {
        "status": report_dict.get("status"),
        "finding_codes": [f.get("code") for f in report_dict.get("findings", [])],
        "state": snapshot.get("controller_info", {}).get("state"),
        "media_errors": smart.get("media_errors", smart.get("media_and_data_integrity_errors")),
        "error_entries": smart.get("num_err_log_entries", smart.get("error_information_log_entries")),
        "unsafe_shutdowns": smart.get("unsafe_shutdowns", smart.get("unsafe_shutdown_count")),
        "percentage_used": smart.get("percent_used", smart.get("percentage_used")),
        "temperature": smart.get("temperature", smart.get("composite_temperature")),
        "reallocated_sectors": smart.get("reallocated_sectors"),
        "pending_sectors": smart.get("current_pending_sectors"),
        "offline_uncorrectable": smart.get("offline_uncorrectable"),
        "udma_crc_errors": smart.get("udma_crc_errors"),
        "aer": pci.get("aer"),
    }


def _existing_quick_health(item: Dict[str, Any]) -> Optional[str]:
    """Map an already-collected OS/SMART status to a compact list health value."""
    value = item.get("smart_status")
    if isinstance(value, dict):
        passed = value.get("passed")
        if passed is True:
            return "GOOD"
        if passed is False:
            return "FAIL"
    if isinstance(value, bool):
        return "GOOD" if value else "FAIL"
    text = str(value or "").strip().lower()
    if text in {"verified", "ok", "passed", "pass", "healthy"}:
        return "GOOD"
    if any(word in text for word in ("fail", "fatal", "bad")):
        return "FAIL"
    return None


def _quick_health_from_payload(payload: Any) -> str:
    if not isinstance(payload, dict):
        return "-"
    status = payload.get("smart_status") if isinstance(payload.get("smart_status"), dict) else {}
    passed = status.get("passed")
    nvme = payload.get("nvme_smart_health_information_log")
    if isinstance(nvme, dict):
        critical = to_int(nvme.get("critical_warning"))
        if critical:
            return "WARN"
    if passed is False:
        return "FAIL"
    if passed is True:
        return "GOOD"
    return "-"


def _quick_health_probe(item: Dict[str, Any]) -> str:
    """Non-disruptive, short health probe used only by `list`.

    Direct/capture-style USB access is deliberately never attempted here.
    """
    existing = _existing_quick_health(item)
    if existing:
        return existing

    transport = str(item.get("transport") or "").lower()
    protocol = str(item.get("protocol") or "").upper()
    if protocol not in {"NVME", "ATA"}:
        return "-"

    # macOS USB NVMe/SATA health may require a translated/direct bridge path,
    # eject, or capture. `list` must remain non-disruptive and quick.
    if platform_key() == "darwin" and "usb" in transport:
        return "-"

    device = str(item.get("device") or "").strip()
    if not device:
        return "-"

    dtype = item.get("smartctl_device_type") or item.get("smartctl_type")
    if not dtype and protocol == "ATA" and platform_key() == "linux" and "usb" not in transport:
        dtype = "ata"

    argv = ["smartctl"]
    if dtype:
        argv += ["-d", str(dtype)]
    argv += ["-H", "-j", device]
    res = Runner().run(argv, timeout=1.5)
    if not res.available or not res.stdout.strip():
        return "-"
    return _quick_health_from_payload(read_json(res.stdout))


def _add_quick_health(controllers: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Add compact Health values without serializing per-disk probe latency."""
    if not controllers:
        return controllers
    rows = [dict(item) for item in controllers]
    workers = min(8, len(rows))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        pending = {pool.submit(_quick_health_probe, row): idx for idx, row in enumerate(rows)}
        for future in as_completed(pending):
            idx = pending[future]
            try:
                rows[idx]["health"] = future.result() or "-"
            except Exception:
                rows[idx]["health"] = "-"
    for row in rows:
        row.setdefault("health", "-")
    return rows


def command_list(args: argparse.Namespace) -> int:
    controllers = _add_quick_health(discover_controllers())
    if args.json:
        sys.stdout.write(json.dumps(controllers, indent=2) + "\n")
        return EXIT_OK
    if not controllers:
        print(f"No supported NVMe/SATA devices found by the {platform_label()} backend.")
        return EXIT_WARNING
    print(f"{'Controller':<12} {'Health':<7} {'Proto':<6} {'Transport':<14} {'Size':>10}  {'Model':<36} {'Firmware':<12} Serial")
    for item in controllers:
        size = format_bytes_decimal(item.get('size_bytes')) or '-'
        print(
            f"{item.get('controller','-'):<12} {str(item.get('health') or '-')[:6]:<7} {str(item.get('protocol') or '-')[:5]:<6} "
            f"{str(item.get('transport') or '-')[:13]:<14} {size:>10}  "
            f"{str(item.get('model') or '-')[:35]:<36} {str(item.get('firmware') or '-'):<12} {item.get('serial') or '-'}"
        )
    return EXIT_OK


class _ConsoleSpinner:
    """Single-line console spinner used by normal human checks."""

    def __init__(self, interval: float = 0.4) -> None:
        self.interval = interval
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._width = len("Checking...")

    @staticmethod
    def _write(text: str) -> None:
        data = text.encode("utf-8", "replace")
        try:
            os.write(1, data)
        except Exception:
            sys.stdout.write(text)
            sys.stdout.flush()

    def start(self) -> None:
        self._stop.clear()
        # Fixed-width frames prevent remnants when cycling from ... back to .
        # Hide the terminal cursor for the duration of the transient spinner.
        self._write("\x1b[?25lChecking.  ")
        self._thread = threading.Thread(target=self._run, name="nvme-doctor-spinner", daemon=True)
        try:
            self._thread.start()
        except Exception:
            self._thread = None
            self._write("\r" + (" " * self._width) + "\r\x1b[?25h")
            raise

    def _run(self) -> None:
        states = ("Checking.. ", "Checking...", "Checking.  ")
        idx = 0
        while not self._stop.wait(self.interval):
            self._write("\r" + states[idx])
            idx = (idx + 1) % len(states)

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=max(0.2, self.interval * 2))
            self._thread = None
        # Remove the transient line completely and restore the cursor before
        # printing the final status/report.
        self._write("\r" + (" " * self._width) + "\r\x1b[?25h")


def _spinner_for_check(args: argparse.Namespace, *, output: Optional[str] = None) -> Optional[_ConsoleSpinner]:
    if output or getattr(args, "json", False) or getattr(args, "debug", False) or getattr(args, "no_progress", False):
        return None
    return _ConsoleSpinner()


def _progress_callback(
    args: argparse.Namespace, *, output: Optional[str] = None
) -> Optional[Callable[[str], None]]:
    """Return an unbuffered elapsed-time debug logger.

    Normal checks are intentionally quiet after the immediate version banner.
    Detailed collector timing is shown only with --debug.  Debug output goes to
    stderr so JSON/report output remains machine-readable.
    """
    if not getattr(args, "debug", False):
        return None
    started = time.monotonic()

    def emit(message: str) -> None:
        elapsed = time.monotonic() - started
        line = f"[DEBUG +{elapsed:4.1f}s] {message}\n"
        try:
            os.write(2, line.encode("utf-8", "replace"))
        except Exception:
            print(line, end="", file=sys.stderr, flush=True)

    return emit


def _direct_usb_auto_permission(
    device: str,
    args: argparse.Namespace,
    snapshot,
    *,
    output: Optional[str] = None,
    progress: Optional[Callable[[str], None]] = None,
) -> bool:
    """Decide whether a normal macOS check may use direct RTL9210 access.

    For an interactive human `check`, sudo is the opt-in to temporary exclusive
    access: if the target is an RTL9210 bridge and SMART is otherwise
    unavailable, enter the read-only direct backend automatically.  JSON and
    report-to-file automation remain non-disruptive unless --direct-usb is
    explicitly supplied.
    """
    if getattr(args, "direct_usb", False):
        return True
    if platform_key() != "darwin":
        return False
    if not snapshot.controller_info.get("direct_usb"):
        return False
    if snapshot.capabilities.get("nvme_smart"):
        return False
    if os.geteuid() != 0:
        return False

    # Do not surprise machine-readable/report automation with an unmount/eject.
    if getattr(args, "json", False) or output:
        return False

    try:
        from .macos_usb_nvme import mounted_volumes
        if progress:
            progress("Checking mounted volumes before automatic direct USB access")
        mounts = mounted_volumes(device)
    except Exception:
        mounts = None

    snapshot.controller_info["_direct_usb_mount_state_checked"] = True
    snapshot.controller_info["_direct_usb_mounts_before"] = mounts

    if progress:
        if mounts:
            progress("sudo + RTL9210 detected: entering direct mode; mounted volumes will be restored")
        elif mounts == []:
            progress("sudo + RTL9210 detected: entering direct mode; disk is already unmounted")
        else:
            progress("sudo + RTL9210 detected: entering direct mode; mount state will be handled conservatively")
    return True


def command_check(args: argparse.Namespace, output: Optional[str] = None) -> int:
    # Human console contract: show build immediately, then a single transient
    # Checking./Checking../Checking... line until the final status/report is ready.
    human_console = not getattr(args, "json", False) and not output
    if human_console:
        print(f"NVMe Doctor {__version__}", flush=True)

    progress = _progress_callback(args, output=output)
    spinner = _spinner_for_check(args, output=output)
    if spinner:
        spinner.start()

    try:
        if progress:
            progress(f"Starting check of {args.device or 'auto-selected device'}")
        device = _device_or_error(args.device)
        explicit_direct = bool(getattr(args, "direct_usb", False))
        if progress and device != (args.device or device):
            progress(f"Resolved target to {device}")
        if explicit_direct and platform_key() != "darwin":
            raise ValueError("--direct-usb is currently supported only on macOS with Realtek RTL9210 USB-NVMe bridges")

        auto_direct = (
            platform_key() == "darwin"
            and os.geteuid() == 0
            and not getattr(args, "json", False)
            and not output
            and not explicit_direct
        )
        snapshot = collect_snapshot(
            device, kernel_lines=max(20, args.kernel_lines), direct_usb=explicit_direct,
            auto_direct_usb=auto_direct,
            usb_extra_logs=bool(getattr(args, "usb_extra_logs", False)),
            progress=progress,
        )

        if progress:
            progress("Analyzing collected health data")
        report = diagnose(snapshot)
        text = render_json(report) if args.json else render_text(report, verbose=args.verbose)
    finally:
        if spinner:
            spinner.stop()

    _write_output(text, output)
    if progress:
        progress("Check complete")
    return _status_code(report.status)


def command_topology(args: argparse.Namespace) -> int:
    device = _device_or_error(args.device)
    explicit_direct = bool(getattr(args, "direct_usb", False))
    if explicit_direct and platform_key() != "darwin":
        raise ValueError("--direct-usb is currently supported only on macOS with Realtek RTL9210 USB-NVMe bridges")

    topology = collect_topology(device, direct_usb=explicit_direct)

    if platform_key() == "darwin" and not explicit_direct:
        from types import SimpleNamespace
        ci = topology.get("controller_info") or {}
        pseudo_snapshot = SimpleNamespace(
            controller_info=ci,
            capabilities={"nvme_smart": bool(ci.get("nvme_passthrough"))},
        )
        if _direct_usb_auto_permission(device, args, pseudo_snapshot):
            topology = collect_topology(device, direct_usb=True)

    sys.stdout.write(render_topology_json(topology) if args.json else render_topology_text(topology))
    return EXIT_OK if topology.get("complete") else EXIT_INCOMPLETE



def command_diff(args: argparse.Namespace) -> int:
    before_path = Path(args.before)
    after_path = Path(args.after)
    try:
        before = json.loads(before_path.read_text())
        after = json.loads(after_path.read_text())
    except OSError as exc:
        raise ValueError(f"could not read report: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON report: {exc}") from exc
    diff = compare_reports(before, after)
    if args.json:
        sys.stdout.write(json.dumps(diff, indent=2) + "\n")
    else:
        sys.stdout.write(render_diff_text(diff))
    if any(item.get("severity") == "warning" for item in diff.get("interpretations", [])):
        return EXIT_WARNING
    return EXIT_OK

def command_watch(args: argparse.Namespace) -> int:
    if args.interval < 0.25:
        raise ValueError("--interval must be at least 0.25 seconds")
    device = _device_or_error(args.device)
    previous = None
    sample = 0
    worst = EXIT_OK
    try:
        while args.count == 0 or sample < args.count:
            sample += 1
            report = diagnose(collect_snapshot(device, kernel_lines=max(20, args.kernel_lines)))
            data = report.to_dict()
            key = _watch_key(data)
            changed = previous is None or key != previous
            if changed:
                if args.json_lines:
                    event = {
                        "sample": sample,
                        "collected_at": data["snapshot"]["collected_at"],
                        "status": data["status"],
                        "changed_state": key,
                        "findings": data["findings"],
                    }
                    print(json.dumps(event, separators=(",", ":")), flush=True)
                else:
                    print(f"--- sample {sample}: {data['snapshot']['collected_at']} ---")
                    sys.stdout.write(render_text(report, verbose=False))
                    sys.stdout.flush()
            previous = key
            worst = max(worst, _status_code(report.status))
            if args.count and sample >= args.count:
                break
            time.sleep(args.interval)
    except KeyboardInterrupt:
        if not args.json_lines:
            print("watch stopped")
    return worst


def main(argv: Optional[List[str]] = None) -> int:
    argv = _normalize_argv(list(sys.argv[1:] if argv is None else argv))
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return EXIT_OK
    try:
        if args.command == "list":
            return command_list(args)
        if args.command == "check":
            return command_check(args)
        if args.command == "topology":
            return command_topology(args)
        if args.command == "report":
            return command_check(args, output=args.output)
        if args.command == "diff":
            return command_diff(args)
        if args.command == "watch":
            return command_watch(args)
        parser.error("unknown command")
        return EXIT_ERROR
    except ValueError as exc:
        print(f"nvme-doctor: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except PermissionError as exc:
        print(f"nvme-doctor: permission denied: {exc}", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    raise SystemExit(main())

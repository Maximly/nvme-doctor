# SPDX-License-Identifier: GPL-3.0-or-later
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from . import __version__
from .collect import collect_snapshot, collect_topology, discover_controllers
from .diagnose import diagnose
from .diff import compare_reports, render_diff_text
from .render import render_json, render_text, render_topology_json, render_topology_text
from .util import normalize_device, normalize_macos_device
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
        raise ValueError("device is required when zero or multiple NVMe controllers are present; run `nvme-doctor list`")
    if platform_key() == "darwin":
        normalize_macos_device(value)
    else:
        normalize_device(value)
    return value


def _write_output(text: str, output: Optional[str]) -> None:
    if output:
        path = Path(output)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    else:
        sys.stdout.write(text)


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("device", nargs="?", help="NVMe controller/namespace or USB-translated block device, e.g. /dev/nvme0, /dev/nvme0n1, or /dev/sdf")
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    parser.add_argument("--verbose", "-v", action="store_true", help="include PCI topology and relevant kernel log lines")
    parser.add_argument("--kernel-lines", type=int, default=300, help="maximum number of relevant kernel log lines to keep (default: 300)")
    parser.add_argument(
        "--direct-usb", action="store_true",
        help="macOS RTL9210 only: temporarily unmount/capture the USB enclosure and read NVMe Identify/SMART directly (requires sudo + libusb)",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="nvme-doctor",
        description="NVMe SSD diagnostics and root-cause analyzer for Linux and macOS",
    )
    parser.add_argument("--version", action="version", version=f"nvme-doctor {__version__}")
    sub = parser.add_subparsers(dest="command")

    p_list = sub.add_parser("list", help="list NVMe devices visible to the current OS backend")
    p_list.add_argument("--json", action="store_true", help="emit JSON")

    p_check = sub.add_parser("check", help="collect evidence and diagnose one NVMe controller")
    _add_common(p_check)

    p_topology = sub.add_parser("topology", help="show NUMA -> PCIe path -> NVMe namespaces")
    p_topology.add_argument("device", nargs="?", help="NVMe controller, namespace, or USB-translated block device")
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
    p_watch.add_argument("device", nargs="?", help="NVMe controller or namespace")
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
        "aer": pci.get("aer"),
    }


def command_list(args: argparse.Namespace) -> int:
    controllers = discover_controllers()
    if args.json:
        sys.stdout.write(json.dumps(controllers, indent=2) + "\n")
        return EXIT_OK
    if not controllers:
        print(f"No NVMe devices found by the {platform_label()} backend.")
        return EXIT_WARNING
    print(f"{'Controller':<12} {'State':<12} {'Transport':<14} {'Model':<36} {'Firmware':<12} Serial")
    for item in controllers:
        print(
            f"{item.get('controller','-'):<12} {str(item.get('state') or '-'):<12} "
            f"{str(item.get('transport') or '-')[:13]:<14} "
            f"{str(item.get('model') or '-')[:35]:<36} {str(item.get('firmware') or '-'):<12} {item.get('serial') or '-'}"
        )
    return EXIT_OK


def _direct_usb_auto_permission(
    device: str,
    args: argparse.Namespace,
    snapshot,
    *,
    output: Optional[str] = None,
) -> bool:
    """Decide whether a normal macOS check may use direct RTL9210 access.

    Explicit --direct-usb always wins.  Without it, an already-unmounted disk
    may be read automatically.  A mounted disk requires an interactive yes/no
    confirmation.  JSON/non-interactive/report automation never gets an
    implicit unmount; scripts must pass --direct-usb explicitly.
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

    try:
        from .macos_usb_nvme import mounted_volumes
        mounts = mounted_volumes(device)
    except Exception:
        mounts = None

    # Machine-readable/non-interactive use must never acquire permission by
    # surprise, even when no filesystem is currently mounted. --direct-usb is
    # the explicit automation opt-in.
    if getattr(args, "json", False) or output or not sys.stdin.isatty() or not sys.stderr.isatty():
        return False

    # In an interactive terminal, if we can prove nothing is mounted, direct
    # access can proceed automatically without a filesystem disruption.
    if mounts == []:
        return True

    print(
        f"Direct NVMe access for {snapshot.controller_info.get('model') or device} requires temporary exclusive USB access.",
        file=sys.stderr,
    )
    if mounts:
        print("Mounted volumes:", file=sys.stderr)
        for mount in mounts:
            print(f"  {mount}", file=sys.stderr)
    else:
        print("Mounted-volume state could not be determined reliably.", file=sys.stderr)
    print(
        "NVMe Doctor will temporarily unmount the disk if needed, read the underlying NVMe identity/health data, then restore it.",
        file=sys.stderr,
    )
    try:
        print("Continue? [y/N] ", end="", file=sys.stderr, flush=True)
        answer = sys.stdin.readline().strip().lower()
    except (EOFError, KeyboardInterrupt):
        print(file=sys.stderr)
        return False
    return answer in {"y", "yes"}


def command_check(args: argparse.Namespace, output: Optional[str] = None) -> int:
    device = _device_or_error(args.device)
    explicit_direct = bool(getattr(args, "direct_usb", False))
    if explicit_direct and platform_key() != "darwin":
        raise ValueError("--direct-usb is currently supported only on macOS with Realtek RTL9210 USB-NVMe bridges")

    # Ordinary macOS external-USB collection is intentionally fast and
    # non-disruptive.  It classifies the target and direct-USB capability first.
    snapshot = collect_snapshot(
        device, kernel_lines=max(20, args.kernel_lines), direct_usb=explicit_direct
    )

    if not explicit_direct and _direct_usb_auto_permission(device, args, snapshot, output=output):
        snapshot = collect_snapshot(
            device, kernel_lines=max(20, args.kernel_lines), direct_usb=True
        )

    report = diagnose(snapshot)
    text = render_json(report) if args.json else render_text(report, verbose=args.verbose)
    _write_output(text, output)
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

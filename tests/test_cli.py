from src.cli import _normalize_argv, _status_code


def test_shorthand_device_becomes_check():
    assert _normalize_argv(["/dev/nvme0", "--json"]) == ["check", "/dev/nvme0", "--json"]
    assert _normalize_argv(["/dev/sdf", "--json"]) == ["check", "/dev/sdf", "--json"]


def test_exit_codes():
    assert _status_code("OK") == 0
    assert _status_code("WARNING") == 1
    assert _status_code("CRITICAL") == 2
    assert _status_code("INCOMPLETE") == 3


def _direct_snapshot():
    from types import SimpleNamespace
    return SimpleNamespace(
        controller_info={"direct_usb": True, "model": "RTL9210 Enclosure"},
        capabilities={"nvme_smart": False},
    )


class _TTYInput:
    def __init__(self, text=""):
        import io
        self._buf = io.StringIO(text)

    def isatty(self):
        return True

    def readline(self, *args, **kwargs):
        return self._buf.readline(*args, **kwargs)


class _TTYOutput:
    def __init__(self):
        import io
        self._buf = io.StringIO()

    def isatty(self):
        return True

    def write(self, value):
        return self._buf.write(value)

    def flush(self):
        return None


def test_direct_usb_auto_when_disk_is_proven_unmounted(monkeypatch):
    import argparse
    import src.cli as cli
    import src.macos_usb_nvme as direct

    monkeypatch.setattr(cli, "platform_key", lambda: "darwin")
    monkeypatch.setattr(cli.os, "geteuid", lambda: 0)
    monkeypatch.setattr(direct, "mounted_volumes", lambda device: [])
    monkeypatch.setattr(cli.sys, "stdin", _TTYInput())
    monkeypatch.setattr(cli.sys, "stderr", _TTYOutput())
    args = argparse.Namespace(direct_usb=False, json=False)
    snapshot = _direct_snapshot()
    assert cli._direct_usb_auto_permission("disk4", args, snapshot) is True
    assert snapshot.controller_info["_direct_usb_mount_state_checked"] is True
    assert snapshot.controller_info["_direct_usb_mounts_before"] == []


def test_direct_usb_noninteractive_mounted_auto_for_human_check(monkeypatch):
    import argparse
    import src.cli as cli
    import src.macos_usb_nvme as direct

    monkeypatch.setattr(cli, "platform_key", lambda: "darwin")
    monkeypatch.setattr(cli.os, "geteuid", lambda: 0)
    monkeypatch.setattr(direct, "mounted_volumes", lambda device: ["/Volumes/External"])
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: False)
    args = argparse.Namespace(direct_usb=False, json=False)
    assert cli._direct_usb_auto_permission("disk4", args, _direct_snapshot()) is True


def test_direct_usb_noninteractive_unmounted_auto_for_human_check(monkeypatch):
    import argparse
    import src.cli as cli
    import src.macos_usb_nvme as direct

    monkeypatch.setattr(cli, "platform_key", lambda: "darwin")
    monkeypatch.setattr(cli.os, "geteuid", lambda: 0)
    monkeypatch.setattr(direct, "mounted_volumes", lambda device: [])
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: False)
    args = argparse.Namespace(direct_usb=False, json=False)
    assert cli._direct_usb_auto_permission("disk4", args, _direct_snapshot()) is True


def test_direct_usb_interactive_mounted_can_be_approved(monkeypatch):
    import argparse
    import src.cli as cli
    import src.macos_usb_nvme as direct

    monkeypatch.setattr(cli, "platform_key", lambda: "darwin")
    monkeypatch.setattr(cli.os, "geteuid", lambda: 0)
    monkeypatch.setattr(direct, "mounted_volumes", lambda device: ["/Volumes/External"])
    monkeypatch.setattr(cli.sys, "stdin", _TTYInput("y\n"))
    monkeypatch.setattr(cli.sys, "stderr", _TTYOutput())
    args = argparse.Namespace(direct_usb=False, json=False)
    assert cli._direct_usb_auto_permission("disk4", args, _direct_snapshot()) is True


def test_direct_usb_json_remains_explicit(monkeypatch):
    import argparse
    import src.cli as cli

    monkeypatch.setattr(cli, "platform_key", lambda: "darwin")
    monkeypatch.setattr(cli.os, "geteuid", lambda: 0)
    args = argparse.Namespace(direct_usb=False, json=True)
    assert cli._direct_usb_auto_permission("disk4", args, _direct_snapshot()) is False

def test_check_debug_is_quiet_by_default():
    import argparse
    import src.cli as cli
    args = argparse.Namespace(debug=False, json=False, no_progress=False)
    assert cli._progress_callback(args) is None


def test_check_debug_is_unbuffered_on_stderr(monkeypatch):
    import argparse
    import src.cli as cli

    writes = []
    monkeypatch.setattr(cli.os, "write", lambda fd, data: writes.append((fd, data)) or len(data))
    args = argparse.Namespace(debug=True, json=False, no_progress=False)
    cb = cli._progress_callback(args)
    assert cb is not None
    cb("Reading NVMe SMART / Health log")
    assert writes
    assert writes[0][0] == 2
    assert b"[DEBUG +" in writes[0][1]
    assert b"Reading NVMe SMART / Health log" in writes[0][1]


def test_check_no_progress_legacy_flag_does_not_enable_debug():
    import argparse
    import src.cli as cli
    assert cli._progress_callback(argparse.Namespace(debug=False, no_progress=True)) is None



def test_check_parser_accepts_debug():
    import src.cli as cli
    args = cli.build_parser().parse_args(["check", "disk4", "--debug"])
    assert args.debug is True

def test_command_check_root_macos_requests_auto_direct_in_first_collection(monkeypatch):
    import argparse
    from types import SimpleNamespace
    import src.cli as cli

    calls = []
    monkeypatch.setattr(cli, "_device_or_error", lambda device: device)
    monkeypatch.setattr(cli, "platform_key", lambda: "darwin")
    monkeypatch.setattr(cli.os, "geteuid", lambda: 0)

    snapshot = SimpleNamespace()
    def fake_collect(device, **kwargs):
        calls.append((device, kwargs))
        return snapshot
    monkeypatch.setattr(cli, "collect_snapshot", fake_collect)
    monkeypatch.setattr(cli, "diagnose", lambda snap: SimpleNamespace(status="OK"))
    monkeypatch.setattr(cli, "render_text", lambda report, verbose=False: "REPORT\n")
    monkeypatch.setattr(cli, "_write_output", lambda text, output: None)

    args = argparse.Namespace(
        device="disk4", direct_usb=False, usb_extra_logs=False,
        kernel_lines=300, json=False, verbose=False, no_progress=False, debug=False,
    )
    assert cli.command_check(args) == 0
    assert len(calls) == 1
    assert calls[0][0] == "disk4"
    assert calls[0][1]["auto_direct_usb"] is True
    assert calls[0][1]["direct_usb"] is False
    assert calls[0][1]["progress"] is None


def test_normal_check_uses_spinner_and_clears_before_report(monkeypatch):
    import argparse
    from types import SimpleNamespace
    import src.cli as cli

    writes = []
    monkeypatch.setattr(cli.os, "write", lambda fd, data: writes.append((fd, data)) or len(data))
    monkeypatch.setattr(cli, "_device_or_error", lambda device: device)
    monkeypatch.setattr(cli, "platform_key", lambda: "linux")
    monkeypatch.setattr(cli, "collect_snapshot", lambda *a, **kw: SimpleNamespace())
    monkeypatch.setattr(cli, "diagnose", lambda snap: SimpleNamespace(status="OK"))
    monkeypatch.setattr(cli, "render_text", lambda report, verbose=False: "NVMe Doctor  |  OK\n")
    monkeypatch.setattr(cli, "_write_output", lambda text, output: writes.append((1, text.encode())))

    args = argparse.Namespace(
        device="/dev/nvme0", direct_usb=False, usb_extra_logs=False,
        kernel_lines=300, json=False, verbose=False, no_progress=False, debug=False,
    )
    assert cli.command_check(args) == 0
    stream = b"".join(data for fd, data in writes if fd == 1)
    assert b"\x1b[?25lChecking.  " in stream
    assert b"\r           \r\x1b[?25h" in stream
    assert stream.find(b"\r           \r\x1b[?25h") < stream.find(b"NVMe Doctor  |  OK")


def test_debug_check_does_not_use_spinner():
    import argparse
    import src.cli as cli
    args = argparse.Namespace(debug=True, json=False, no_progress=False)
    assert cli._spinner_for_check(args) is None


def test_spinner_cycles_dots(monkeypatch):
    import time
    import src.cli as cli

    writes = []
    monkeypatch.setattr(cli.os, "write", lambda fd, data: writes.append((fd, data)) or len(data))
    spinner = cli._ConsoleSpinner(interval=0.01)
    spinner.start()
    time.sleep(0.045)
    spinner.stop()
    stream = b"".join(data for fd, data in writes if fd == 1)
    assert b"\x1b[?25lChecking.  " in stream
    assert b"\rChecking.. " in stream
    assert b"\rChecking..." in stream
    assert b"\rChecking.  " in stream
    assert stream.endswith(b"\r           \r\x1b[?25h")


def test_list_quick_health_maps_smart_pass_fail_and_nvme_warning():
    import src.cli as cli

    assert cli._quick_health_from_payload({"smart_status": {"passed": True}}) == "GOOD"
    assert cli._quick_health_from_payload({"smart_status": {"passed": False}}) == "FAIL"
    assert cli._quick_health_from_payload({
        "smart_status": {"passed": True},
        "nvme_smart_health_information_log": {"critical_warning": 1},
    }) == "WARN"
    assert cli._quick_health_from_payload({}) == "-"


def test_list_quick_health_macos_usb_is_not_disruptively_probed(monkeypatch):
    import src.cli as cli

    monkeypatch.setattr(cli, "platform_key", lambda: "darwin")
    row = {
        "device": "/dev/disk4",
        "protocol": "NVMe",
        "transport": "USB -> NVMe",
        "state": "direct-ready",
    }
    assert cli._quick_health_probe(row) == "-"


def test_list_quick_health_macos_usb_sata_uses_proven_smartctl_auto(monkeypatch):
    import src.cli as cli
    from src.model import CommandResult

    monkeypatch.setattr(cli, "platform_key", lambda: "darwin")

    class FakeRunner:
        def run(self, argv, timeout=8.0):
            assert argv == ["smartctl", "-H", "-j", "/dev/disk4"]
            return CommandResult(argv, 0, stdout='{"smart_status":{"passed":true}}')

    monkeypatch.setattr(cli, "Runner", FakeRunner)
    row = {
        "device": "/dev/disk4",
        "protocol": "ATA",
        "transport": "USB -> SATA",
        "backend": "diskutil+smartctl-auto",
    }
    assert cli._quick_health_probe(row) == "GOOD"


def test_list_quick_health_uses_existing_macos_status_without_probe(monkeypatch):
    import src.cli as cli

    monkeypatch.setattr(cli, "platform_key", lambda: "darwin")
    row = {
        "device": "/dev/disk0",
        "protocol": "NVMe",
        "transport": "NVMe",
        "smart_status": "Verified",
    }
    assert cli._quick_health_probe(row) == "GOOD"


def test_list_output_has_health_column(monkeypatch, capsys):
    import argparse
    import src.cli as cli

    monkeypatch.setattr(cli, "discover_controllers", lambda: [{
        "controller": "sda",
        "device": "/dev/sda",
        "state": "live",
        "protocol": "ATA",
        "transport": "SATA",
        "model": "SOLIDIGM TEST",
        "firmware": "FW1",
        "serial": "SER1",
        "size_bytes": 1_920_000_000_000,
    }])
    monkeypatch.setattr(cli, "_add_quick_health", lambda rows: [dict(rows[0], health="GOOD")])
    assert cli.command_list(argparse.Namespace(json=False)) == 0
    out = capsys.readouterr().out
    assert "Health" in out
    assert "Size" in out
    assert "State" not in out.splitlines()[0]
    assert "1.92 TB" in out
    assert "sda" in out
    assert "GOOD" in out


def test_missing_linux_device_is_hard_error_before_collection(monkeypatch, capsys):
    import src.cli as cli

    monkeypatch.setattr(cli, "platform_key", lambda: "linux")
    monkeypatch.setattr(cli.os.path, "exists", lambda path: False)
    monkeypatch.setattr(
        cli, "collect_snapshot",
        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("collector must not run for missing device")),
    )

    rc = cli.main(["check", "sdb", "--no-progress"])
    captured = capsys.readouterr()
    assert rc == cli.EXIT_ERROR
    assert "device not found: /dev/sdb" in captured.err
    assert "INCOMPLETE" not in captured.out


def test_device_validation_checks_normalized_whole_device(monkeypatch):
    import src.cli as cli

    seen = []
    monkeypatch.setattr(cli, "platform_key", lambda: "linux")
    monkeypatch.setattr(cli.os.path, "exists", lambda path: seen.append(path) or True)
    assert cli._device_or_error("/dev/sda2") == "/dev/sda2"
    assert seen == ["/dev/sda"]


def test_command_topology_without_device_renders_merged_all_drive_tree(monkeypatch, capsys):
    import argparse
    import src.cli as cli

    merged = {
        "platform": "linux",
        "all_devices": True,
        "devices": [{"controller": "sda"}, {"controller": "sdb"}],
        "errors": [],
        "complete": True,
    }
    monkeypatch.setattr(cli, "_collect_all_topologies", lambda progress=None: merged)
    monkeypatch.setattr(cli, "render_topology_all_text", lambda value: "MERGED TREE\n")
    args = argparse.Namespace(device=None, direct_usb=False, json=False, debug=False, no_progress=True)
    assert cli.command_topology(args) == cli.EXIT_OK
    assert capsys.readouterr().out == "MERGED TREE\n"

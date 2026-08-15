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

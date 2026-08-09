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
    assert cli._direct_usb_auto_permission("disk4", args, _direct_snapshot()) is True


def test_direct_usb_noninteractive_mounted_requires_explicit_flag(monkeypatch):
    import argparse
    import src.cli as cli
    import src.macos_usb_nvme as direct

    monkeypatch.setattr(cli, "platform_key", lambda: "darwin")
    monkeypatch.setattr(cli.os, "geteuid", lambda: 0)
    monkeypatch.setattr(direct, "mounted_volumes", lambda device: ["/Volumes/External"])
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: False)
    args = argparse.Namespace(direct_usb=False, json=False)
    assert cli._direct_usb_auto_permission("disk4", args, _direct_snapshot()) is False


def test_direct_usb_noninteractive_unmounted_still_requires_explicit_flag(monkeypatch):
    import argparse
    import src.cli as cli
    import src.macos_usb_nvme as direct

    monkeypatch.setattr(cli, "platform_key", lambda: "darwin")
    monkeypatch.setattr(cli.os, "geteuid", lambda: 0)
    monkeypatch.setattr(direct, "mounted_volumes", lambda device: [])
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: False)
    args = argparse.Namespace(direct_usb=False, json=False)
    assert cli._direct_usb_auto_permission("disk4", args, _direct_snapshot()) is False


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


def test_direct_usb_interactive_mounted_can_be_declined(monkeypatch):
    import argparse
    import src.cli as cli
    import src.macos_usb_nvme as direct

    monkeypatch.setattr(cli, "platform_key", lambda: "darwin")
    monkeypatch.setattr(cli.os, "geteuid", lambda: 0)
    monkeypatch.setattr(direct, "mounted_volumes", lambda device: ["/Volumes/External"])
    monkeypatch.setattr(cli.sys, "stdin", _TTYInput("n\n"))
    monkeypatch.setattr(cli.sys, "stderr", _TTYOutput())
    args = argparse.Namespace(direct_usb=False, json=False)
    assert cli._direct_usb_auto_permission("disk4", args, _direct_snapshot()) is False

from src.cli import _normalize_argv, _status_code


def test_shorthand_device_becomes_check():
    assert _normalize_argv(["/dev/nvme0", "--json"]) == ["check", "/dev/nvme0", "--json"]


def test_exit_codes():
    assert _status_code("OK") == 0
    assert _status_code("WARNING") == 1
    assert _status_code("CRITICAL") == 2
    assert _status_code("INCOMPLETE") == 3

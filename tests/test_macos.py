import json

from src.collect_macos import (
    _parse_smartctl_scan,
    _system_profiler_nvme,
    collect_snapshot_macos,
    discover_controllers_macos,
)
from src.diagnose import diagnose
from src.model import CommandResult, Snapshot
from src.util import normalize_macos_device


PROFILER = {
    "SPNVMeDataType": [
        {
            "_items": [
                {
                    "_name": "APPLE SSD AP1024Z",
                    "bsd_name": "disk0",
                    "device_model": "APPLE SSD AP1024Z",
                    "device_revision": "123.45.6",
                    "device_serial": "MACNVME123",
                    "size": "1 TB",
                    "size_in_bytes": 1000000000000,
                    "smart_status": "Verified",
                    "spnvme_trim_support": "Yes",
                    "link_width": "x4",
                    "link_speed": "16.0 GT/s",
                    "volumes": [{"_name": "Macintosh HD", "bsd_name": "disk3s1"}],
                }
            ]
        }
    ]
}

SMARTCTL = {
    "device": {"name": "/dev/disk0", "type": "nvme", "protocol": "NVMe"},
    "model_name": "APPLE SSD AP1024Z",
    "serial_number": "MACNVME123",
    "firmware_version": "123.45.6",
    "nvme_version": {"string": "1.4"},
    "nvme_smart_health_information_log": {
        "critical_warning": 0,
        "temperature": 41,
        "available_spare": 100,
        "available_spare_threshold": 99,
        "percentage_used": 3,
        "unsafe_shutdowns": 2,
        "media_errors": 0,
        "num_err_log_entries": 0,
    },
    "nvme_error_information_log": [],
}


class FakeMacRunner:
    def __init__(self, smartctl=True):
        self.smartctl = smartctl
        self.calls = []

    def run(self, argv, timeout=8.0):
        argv = list(argv)
        self.calls.append(argv)
        if argv[:3] == ["system_profiler", "SPNVMeDataType", "-json"]:
            return CommandResult(argv, 0, stdout=json.dumps(PROFILER))
        if argv[:2] == ["system_profiler", "SPNVMeDataType"]:
            return CommandResult(argv, 0, stdout="")
        if argv[:2] == ["smartctl", "--scan-open"]:
            if not self.smartctl:
                return CommandResult(argv, 127, stderr="smartctl not found", available=False)
            return CommandResult(argv, 0, stdout="/dev/disk0 -d nvme # /dev/disk0, NVMe device\n")
        if argv and argv[0] == "smartctl":
            if not self.smartctl:
                return CommandResult(argv, 127, stderr="smartctl not found", available=False)
            return CommandResult(argv, 0, stdout=json.dumps(SMARTCTL))
        if argv[:2] == ["log", "show"]:
            return CommandResult(argv, 0, stdout="")
        return CommandResult(argv, 127, stderr="not found", available=False)


def test_normalize_macos_device_accepts_disk_slice_and_raw_name():
    assert normalize_macos_device("disk0") == ("disk0", "/dev/disk0")
    assert normalize_macos_device("/dev/rdisk2s1") == ("disk2", "/dev/disk2")


def test_parse_smartctl_scan_keeps_usb_nvme_bridge_type():
    items = _parse_smartctl_scan(
        "/dev/disk0 -d nvme # /dev/disk0, NVMe device\n"
        "/dev/disk4 -d sntrealtek # /dev/disk4 [USB NVMe Realtek], NVMe device\n"
        "/dev/disk5 -d sat # /dev/disk5, ATA device\n"
    )
    assert items == [
        {"device": "/dev/disk0", "smartctl_type": "nvme", "scan_line": "/dev/disk0 -d nvme # /dev/disk0, NVMe device"},
        {"device": "/dev/disk4", "smartctl_type": "sntrealtek", "scan_line": "/dev/disk4 -d sntrealtek # /dev/disk4 [USB NVMe Realtek], NVMe device"},
    ]


def test_system_profiler_parser_ignores_nested_volume_as_controller():
    items = _system_profiler_nvme(FakeMacRunner())
    assert len(items) == 1
    assert items[0]["bsd_name"] == "disk0"


def test_macos_discovery_works_without_smartctl():
    devices = discover_controllers_macos(FakeMacRunner(smartctl=False))
    assert len(devices) == 1
    assert devices[0]["device"] == "/dev/disk0"
    assert devices[0]["model"] == "APPLE SSD AP1024Z"


def test_macos_collect_uses_smartctl_health_and_link_info():
    runner = FakeMacRunner()
    snapshot = collect_snapshot_macos("disk0", runner=runner)
    assert snapshot.host["platform"] == "darwin"
    assert snapshot.controller_info["model"] == "APPLE SSD AP1024Z"
    assert snapshot.smart["percentage_used"] == 3
    assert snapshot.capabilities["nvme_smart"] is True
    assert snapshot.capabilities["nvme_error_log"] is True
    assert snapshot.pci["current_link_width"] == "x4"
    assert snapshot.pci["current_link_speed"] == "16.0 GT/s"
    assert ["smartctl", "-a", "-j", "-d", "nvme", "/dev/disk0"] in runner.calls
    assert diagnose(snapshot).status == "OK"


def test_macos_without_smartctl_is_incomplete_not_ok():
    snapshot = collect_snapshot_macos("disk0", runner=FakeMacRunner(smartctl=False))
    report = diagnose(snapshot)
    assert report.status == "INCOMPLETE"
    assert not snapshot.smart
    assert any("brew install smartmontools" in x for x in snapshot.collection_notes)


def test_unscoped_macos_log_cannot_create_timeout_finding():
    snapshot = Snapshot("disk0", "disk0", "/dev/disk0")
    snapshot.host = {"platform": "darwin"}
    snapshot.controller_info = {"state": "present"}
    snapshot.smart = {"critical_warning": 0, "media_errors": 0}
    snapshot.kernel_lines = ["kernel: unrelated NVMe controller reset timed out"]
    snapshot.capabilities = {"targeted_kernel_log": False}
    report = diagnose(snapshot)
    assert not any(f.code == "kernel-reset-timeout" for f in report.findings)


class FakeUsbMacRunner(FakeMacRunner):
    def run(self, argv, timeout=8.0):
        argv = list(argv)
        self.calls.append(argv)
        if argv[:3] == ["system_profiler", "SPNVMeDataType", "-json"]:
            return CommandResult(argv, 0, stdout=json.dumps({"SPNVMeDataType": []}))
        if argv[:2] == ["system_profiler", "SPNVMeDataType"]:
            return CommandResult(argv, 0, stdout="")
        if argv[:2] == ["smartctl", "--scan-open"]:
            return CommandResult(argv, 0, stdout="/dev/disk4 -d sntrealtek # /dev/disk4 [USB NVMe Realtek], NVMe device\n")
        if argv and argv[0] == "smartctl":
            payload = dict(SMARTCTL)
            payload["device"] = {"name": "/dev/disk4", "type": "sntrealtek", "protocol": "NVMe"}
            payload["model_name"] = "External NVMe"
            return CommandResult(argv, 0, stdout=json.dumps(payload))
        if argv[:2] == ["log", "show"]:
            return CommandResult(argv, 0, stdout="")
        return CommandResult(argv, 127, stderr="not found", available=False)


def test_macos_usb_nvme_reuses_bridge_type_from_scan():
    runner = FakeUsbMacRunner()
    snapshot = collect_snapshot_macos("disk4", runner=runner)
    assert snapshot.controller_info["model"] == "External NVMe"
    assert snapshot.tools["smartctl_scan"]["smartctl_type"] == "sntrealtek"
    assert ["smartctl", "-a", "-j", "-d", "sntrealtek", "/dev/disk4"] in runner.calls
    assert diagnose(snapshot).status == "OK"

class FakeTextLinkRunner(FakeMacRunner):
    def run(self, argv, timeout=8.0):
        argv = list(argv)
        self.calls.append(argv)
        if argv[:3] == ["system_profiler", "SPNVMeDataType", "-json"]:
            payload = json.loads(json.dumps(PROFILER))
            item = payload["SPNVMeDataType"][0]["_items"][0]
            item.pop("link_width", None)
            item.pop("link_speed", None)
            return CommandResult(argv, 0, stdout=json.dumps(payload))
        if argv[:2] == ["system_profiler", "SPNVMeDataType"]:
            text = """NVMExpress:\n\n    APPLE SSD AP1024Z:\n\n      Link Width: x4\n      Link Speed: 16.0 GT/s\n      BSD Name: disk0\n"""
            return CommandResult(argv, 0, stdout=text)
        if argv[:2] == ["smartctl", "--scan-open"]:
            return CommandResult(argv, 0, stdout="/dev/disk0 -d nvme # /dev/disk0, NVMe device\n")
        if argv and argv[0] == "smartctl":
            return CommandResult(argv, 0, stdout=json.dumps(SMARTCTL))
        if argv[:2] == ["log", "show"]:
            return CommandResult(argv, 0, stdout="")
        return CommandResult(argv, 127, stderr="not found", available=False)


def test_macos_text_profiler_fills_link_when_json_omits_it():
    snapshot = collect_snapshot_macos("disk0", runner=FakeTextLinkRunner())
    assert snapshot.pci["current_link_width"] == "x4"
    assert snapshot.pci["current_link_speed"] == "16.0 GT/s"
    assert snapshot.pci["source"] == "system_profiler-text"
    assert snapshot.capabilities["pcie_link"] == "partial"

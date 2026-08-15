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

import plistlib


def _plist(data):
    return plistlib.dumps(data, fmt=plistlib.FMT_XML).decode()


class FakeDiskutilUsbRunner(FakeMacRunner):
    """Model a representative layout: disk0 internal, disk4 external, APFS synthesized disks."""

    def run(self, argv, timeout=8.0):
        argv = list(argv)
        self.calls.append(argv)
        if argv[:3] == ["system_profiler", "SPNVMeDataType", "-json"]:
            return CommandResult(argv, 0, stdout=json.dumps(PROFILER))
        if argv[:2] == ["system_profiler", "SPNVMeDataType"]:
            return CommandResult(argv, 0, stdout="")
        if argv[:2] == ["smartctl", "--scan-open"]:
            # Automatic scan sees only the native Apple controller.
            return CommandResult(argv, 0, stdout="/dev/disk0 -d nvme # native controller\n")
        if argv[:3] == ["diskutil", "list", "-plist"]:
            return CommandResult(argv, 0, stdout=_plist({
                "AllDisksAndPartitions": [
                    {"DeviceIdentifier": "disk0"},
                    {"DeviceIdentifier": "disk3"},
                    {"DeviceIdentifier": "disk4"},
                    {"DeviceIdentifier": "disk5"},
                ]
            }))
        if argv[:3] == ["diskutil", "info", "-plist"]:
            ident = argv[3].rsplit("/", 1)[-1]
            infos = {
                "disk0": {
                    "DeviceIdentifier": "disk0", "WholeDisk": True,
                    "VirtualOrPhysical": "Physical", "Internal": True,
                    "BusProtocol": "Apple Fabric", "SolidState": True,
                    "MediaName": "APPLE SSD AP1024Z", "TotalSize": 8_000_000_000_000,
                },
                "disk3": {
                    "DeviceIdentifier": "disk3", "WholeDisk": True,
                    "VirtualOrPhysical": "Virtual", "Internal": True,
                    "MediaName": "AppleAPFSMedia", "TotalSize": 8_000_000_000_000,
                },
                "disk4": {
                    "DeviceIdentifier": "disk4", "WholeDisk": True,
                    "VirtualOrPhysical": "Physical", "Internal": False,
                    "BusProtocol": "USB", "SolidState": True,
                    "MediaName": "USB NVMe Bridge", "TotalSize": 2_000_000_000_000,
                },
                "disk5": {
                    "DeviceIdentifier": "disk5", "WholeDisk": True,
                    "VirtualOrPhysical": "Virtual", "Internal": False,
                    "MediaName": "AppleAPFSMedia", "TotalSize": 2_000_000_000_000,
                },
            }
            return CommandResult(argv, 0, stdout=_plist(infos[ident]))
        if argv[:3] == ["smartctl", "-i", "-j"]:
            # Native disk can identify automatically.
            if argv[-1] == "/dev/disk0":
                return CommandResult(argv, 0, stdout=json.dumps(SMARTCTL))
            if argv[-1] == "/dev/disk4" and "-d" in argv:
                dtype = argv[argv.index("-d") + 1]
                if dtype == "sntjmicron":
                    payload = dict(SMARTCTL)
                    payload["device"] = {"name": "/dev/disk4", "type": dtype, "protocol": "NVMe"}
                    payload["model_name"] = "Samsung SSD 990 PRO 2TB"
                    payload["serial_number"] = "USB990PRO123"
                    payload["firmware_version"] = "5B2QJXD7"
                    return CommandResult(argv, 0, stdout=json.dumps(payload))
                return CommandResult(argv, 2, stdout=json.dumps({"device": {"name": "/dev/disk4", "type": dtype, "protocol": "SCSI"}}))
        if argv[:3] == ["smartctl", "-a", "-j"]:
            if argv[-1] == "/dev/disk4" and "sntjmicron" in argv:
                payload = dict(SMARTCTL)
                payload["device"] = {"name": "/dev/disk4", "type": "sntjmicron", "protocol": "NVMe"}
                payload["model_name"] = "Samsung SSD 990 PRO 2TB"
                payload["serial_number"] = "USB990PRO123"
                payload["firmware_version"] = "5B2QJXD7"
                return CommandResult(argv, 0, stdout=json.dumps(payload))
            return CommandResult(argv, 0, stdout=json.dumps(SMARTCTL))
        if argv[:2] == ["log", "show"]:
            return CommandResult(argv, 0, stdout="")
        return CommandResult(argv, 127, stderr="not found", available=False)


def test_snt_scan_line_does_not_need_nvme_word_and_normalizes_rdisk():
    items = _parse_smartctl_scan("/dev/rdisk4 -d sntjmicron # USB bridge\n")
    assert items == [{
        "device": "/dev/disk4",
        "smartctl_device": "/dev/rdisk4",
        "smartctl_type": "sntjmicron",
        "scan_line": "/dev/rdisk4 -d sntjmicron # USB bridge",
    }]


def test_macos_list_keeps_external_usb_ssd_without_forced_snt_probe():
    runner = FakeDiskutilUsbRunner()
    devices = discover_controllers_macos(runner)
    assert [x["controller"] for x in devices] == ["disk0", "disk4"]
    external = next(x for x in devices if x["controller"] == "disk4")
    assert external["model"] == "USB NVMe Bridge"
    assert external["transport"] == "USB SSD"
    assert external["state"] == "limited"
    assert external["nvme_passthrough"] is False
    assert external["passthrough_reason"] == "darwin-no-scsi-passthrough"
    assert not any(x["controller"] == "disk5" for x in devices)
    assert not any("sntrealtek" in call or "sntjmicron" in call or "sntasmedia" in call for call in runner.calls)


def test_macos_check_external_usb_ssd_reports_darwin_snt_limitation():
    runner = FakeDiskutilUsbRunner()
    snapshot = collect_snapshot_macos("disk4", runner=runner)
    report = diagnose(snapshot)
    assert snapshot.controller_info["model"] == "USB NVMe Bridge"
    assert snapshot.controller_info["transport"] == "USB SSD"
    assert snapshot.controller_info["state"] == "limited"
    assert snapshot.controller_info["nvme_passthrough"] is False
    assert snapshot.controller_info["passthrough_reason"] == "darwin-no-scsi-passthrough"
    assert snapshot.capabilities["nvme_smart"] is False
    assert report.status == "INCOMPLETE"
    assert any("does not provide the SCSI" in note for note in snapshot.collection_notes)
    assert not any(call[:3] == ["smartctl", "-a", "-j"] and call[-1] == "/dev/disk4" for call in runner.calls)
    assert not any("sntrealtek" in call or "sntjmicron" in call or "sntasmedia" in call for call in runner.calls)

class FakeSplitScanRunner(FakeMacRunner):
    def run(self, argv, timeout=8.0):
        argv = list(argv)
        self.calls.append(argv)
        if argv[:3] == ["system_profiler", "SPNVMeDataType", "-json"]:
            return CommandResult(argv, 0, stdout=json.dumps(PROFILER))
        if argv[:2] == ["system_profiler", "SPNVMeDataType"]:
            return CommandResult(argv, 0, stdout="")
        if argv[:2] == ["smartctl", "--scan-open"]:
            return CommandResult(argv, 0, stdout="/dev/disk0 -d nvme # native\n")
        if argv[:2] == ["smartctl", "--scan"]:
            return CommandResult(argv, 0, stdout=(
                "/dev/disk0 -d nvme # native\n"
                "/dev/rdisk4 -d sntrealtek # USB bridge\n"
            ))
        if argv[:3] == ["smartctl", "-i", "-j"]:
            dtype = argv[argv.index("-d") + 1] if "-d" in argv else "nvme"
            dev = argv[-1]
            payload = dict(SMARTCTL)
            payload["device"] = {"name": dev, "type": dtype, "protocol": "NVMe"}
            if "disk4" in dev:
                payload["model_name"] = "External NVMe"
            return CommandResult(argv, 0, stdout=json.dumps(payload))
        if argv[:3] == ["diskutil", "list", "-plist"]:
            return CommandResult(argv, 0, stdout=_plist({"AllDisksAndPartitions": []}))
        return CommandResult(argv, 127, stderr="not found", available=False)


def test_macos_scan_merges_plain_scan_when_scan_open_only_sees_internal():
    devices = discover_controllers_macos(FakeSplitScanRunner())
    assert [x["controller"] for x in devices] == ["disk0", "disk4"]
    external = next(x for x in devices if x["controller"] == "disk4")
    assert external["smartctl_type"] == "sntrealtek"
    assert external["smartctl_device"] == "/dev/rdisk4"
    assert external["model"] == "External NVMe"

class FakeOpaqueUsbMacRunner(FakeDiskutilUsbRunner):
    """User-like case: disk4 exists, but smartctl sees only native Apple NVMe."""

    def run(self, argv, timeout=8.0):
        argv = list(argv)
        # Reuse diskutil/system_profiler fixtures from the parent.
        if argv[:3] == ["system_profiler", "SPNVMeDataType", "-json"] or argv[:2] == ["system_profiler", "SPNVMeDataType"] or argv[:3] == ["diskutil", "list", "-plist"] or argv[:3] == ["diskutil", "info", "-plist"]:
            return super().run(argv, timeout)
        self.calls.append(argv)
        if argv[:2] == ["smartctl", "--scan-open"] or argv[:2] == ["smartctl", "--scan"]:
            # Mirrors the observed output: only Apple's native IOService NVMe.
            return CommandResult(argv, 0, stdout="IOService:/AppleARMPE/.../AppleANS3CGv2Controller/NS_01@1 -d nvme # NVMe device\n")
        if argv[:3] == ["smartctl", "-i", "-j"]:
            # Every forced SNT backend fails on both disk4 and rdisk4.
            return CommandResult(argv, 2, stdout=json.dumps({
                "device": {"name": argv[-1], "type": argv[argv.index("-d") + 1] if "-d" in argv else "scsi", "protocol": "SCSI"},
                "smartctl": {"exit_status": 2},
            }))
        if argv[:3] == ["smartctl", "-a", "-j"]:
            return CommandResult(argv, 2, stdout=json.dumps({
                "device": {"name": argv[-1], "type": "scsi", "protocol": "SCSI"},
                "model_name": "RTL9210 Enclosure",
                "smartctl": {"exit_status": 2},
            }))
        if argv[:2] == ["log", "show"]:
            return CommandResult(argv, 0, stdout="")
        return CommandResult(argv, 127, stderr="not found", available=False)


def test_macos_list_keeps_opaque_external_usb_ssd_visible():
    devices = discover_controllers_macos(FakeOpaqueUsbMacRunner())
    assert [x["controller"] for x in devices] == ["disk0", "disk4"]
    external = next(x for x in devices if x["controller"] == "disk4")
    assert external["state"] == "limited"
    assert external["transport"] == "USB SSD"
    assert external["model"] == "USB NVMe Bridge"
    assert external["nvme_passthrough"] is False


def test_macos_check_opaque_usb_ssd_is_incomplete_and_explains_passthrough():
    runner = FakeOpaqueUsbMacRunner()
    snapshot = collect_snapshot_macos("disk4", runner=runner)
    report = diagnose(snapshot)
    assert snapshot.controller_info["state"] == "limited"
    assert snapshot.controller_info["transport"] == "USB SSD"
    assert snapshot.controller_info["nvme_passthrough"] is False
    assert report.status == "INCOMPLETE"
    assert snapshot.controller_info["passthrough_reason"] == "darwin-no-scsi-passthrough"
    assert any("does not provide the SCSI" in note for note in snapshot.collection_notes)
    # Current Darwin smartmontools has no SCSI device backend, so forced SNT
    # bridge probes would be guaranteed failures and must not be issued.
    assert not any("sntrealtek" in call or "sntjmicron" in call or "sntasmedia" in call for call in runner.calls)


def test_macos_direct_usb_check_uses_underlying_nvme(monkeypatch):
    import src.macos_usb_nvme as direct_mod

    monkeypatch.setattr(direct_mod, "read_rtl9210_nvme", lambda device, **kwargs: {
        "bridge": {
            "vendor_id": 0x0BDA,
            "product_id": 0x9210,
            "manufacturer": "Realtek",
            "product": "RTL9210 USB Drive",
            "serial": "012345678955",
            "uas_supported": True,
        },
        "identify": {
            "vid": 0x144D,
            "ssvid": 0x144D,
            "model": "Samsung SSD 990 EVO Plus 2TB",
            "serial": "TESTNVME000000000001",
            "firmware_rev": "1B2QKXG7",
            "nvme_version": "2.0",
            "nvme_version_raw": 0x00020000,
            "controller_id": 1,
            "tnvmcap": 2_000_398_934_016,
            "unvmcap": 0,
            "ieee_oui": "00:25:38",
            "mdts": 7,
            "oacs": 0x0017,
            "oncs": 0x00DF,
        },
        "smart": {
            "critical_warning": 0,
            "temperature": 37,
            "avail_spare": 100,
            "spare_thresh": 10,
            "percent_used": 0,
            "data_units_read": 3_611_000,
            "data_units_written": 3_736_800,
            "host_read_commands": 75_187_516,
            "host_write_commands": 1_114_058_618,
            "controller_busy_time": 179,
            "power_cycles": 139,
            "power_on_hours": 56,
            "unsafe_shutdowns": 55,
            "media_errors": 0,
            "num_err_log_entries": 0,
            "warning_temp_time": 0,
            "critical_comp_time": 0,
        },
    })

    snapshot = collect_snapshot_macos("disk4", runner=FakeOpaqueUsbMacRunner(), direct_usb=True)
    report = diagnose(snapshot)
    assert snapshot.controller_info["state"] == "live"
    assert snapshot.controller_info["transport"] == "USB -> NVMe"
    assert snapshot.controller_info["model"] == "Samsung SSD 990 EVO Plus 2TB"
    assert snapshot.controller_info["serial"] == "TESTNVME000000000001"
    assert snapshot.controller_info["nvme_version"] == "2.0"
    assert snapshot.controller_info["tnvmcap"] == 2_000_398_934_016
    assert snapshot.controller_info["ieee_oui"] == "00:25:38"
    assert snapshot.smart["temperature"] == 37
    assert snapshot.smart["power_cycles"] == 139
    assert snapshot.smart["unsafe_shutdowns"] == 55
    assert snapshot.capabilities["nvme_smart"] is True
    assert report.status == "OK"
    assert not any(f.code == "nvme-passthrough-unavailable" for f in report.findings)


def test_macos_external_usb_fast_path_skips_slow_native_collectors():
    runner = FakeOpaqueUsbMacRunner()
    snapshot = collect_snapshot_macos("disk4", runner=runner)
    assert snapshot.controller_info["model"] == "USB NVMe Bridge"
    # A targeted external-USB check must classify via one fast diskutil info
    # call and must not invoke collectors that can each stall for 10-25s.
    assert ["diskutil", "info", "-plist", "/dev/disk4"] in runner.calls
    assert not any(call[:2] == ["system_profiler", "SPNVMeDataType"] for call in runner.calls)
    assert not any(call[:2] == ["smartctl", "--scan-open"] for call in runner.calls)
    assert not any(call[:2] == ["smartctl", "--scan"] for call in runner.calls)
    assert not any(call[:2] == ["log", "show"] for call in runner.calls)
    assert not any(call[:3] == ["diskutil", "list", "-plist"] for call in runner.calls)

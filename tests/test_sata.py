import json
import plistlib

from src.collect import collect_snapshot_linux, discover_controllers_linux
from src.collect_macos import collect_snapshot_macos, discover_controllers_macos, _parse_smartctl_scan
from src.diagnose import diagnose
from src.model import CommandResult
from src.render import render_text


ATA_PAYLOAD = {
    "device": {"name": "/dev/sda", "type": "ata", "protocol": "ATA"},
    "model_name": "Samsung SSD 870 EVO 2TB",
    "serial_number": "SATA123",
    "firmware_version": "SVT02B6Q",
    "rotation_rate": 0,
    "sata_version": {"string": "SATA 3.3", "value": 511},
    "interface_speed": {
        "max": {"sata_value": 14, "string": "6.0 Gb/s"},
        "current": {"sata_value": 14, "string": "6.0 Gb/s"},
    },
    "smart_status": {"passed": True},
    "temperature": {"current": 34},
    "power_on_time": {"hours": 1234},
    "power_cycle_count": 98,
    "ata_smart_error_log": {"summary": {"count": 0}},
    "ata_smart_attributes": {
        "table": [
            {"id": 5, "name": "Reallocated_Sector_Ct", "value": 100, "worst": 100, "thresh": 10, "when_failed": "-", "raw": {"value": 0}},
            {"id": 187, "name": "Reported_Uncorrect", "value": 100, "worst": 100, "thresh": 0, "when_failed": "-", "raw": {"value": 0}},
            {"id": 188, "name": "Command_Timeout", "value": 100, "worst": 100, "thresh": 0, "when_failed": "-", "raw": {"value": 0}},
            {"id": 197, "name": "Current_Pending_Sector", "value": 100, "worst": 100, "thresh": 0, "when_failed": "-", "raw": {"value": 0}},
            {"id": 198, "name": "Offline_Uncorrectable", "value": 100, "worst": 100, "thresh": 0, "when_failed": "-", "raw": {"value": 0}},
            {"id": 199, "name": "UDMA_CRC_Error_Count", "value": 200, "worst": 200, "thresh": 0, "when_failed": "-", "raw": {"value": 0}},
        ]
    },
    "ata_smart_self_test_log": {"standard": {"table": []}},
}


class LinuxSataRunner:
    def run(self, argv, timeout=None):
        if argv[:2] in (["smartctl", "--scan-open"], ["smartctl", "--scan"]):
            return CommandResult(argv, 0, stdout="/dev/sda -d ata # /dev/sda, ATA device\n")
        if argv and argv[0] == "smartctl" and "-a" in argv:
            return CommandResult(argv, 0, stdout=json.dumps(ATA_PAYLOAD))
        if argv and argv[0] in {"journalctl", "dmesg"}:
            return CommandResult(argv, 0, stdout="")
        return CommandResult(argv, 0, stdout="")


def test_linux_sata_snapshot_and_render(tmp_path):
    snap = collect_snapshot_linux(
        "/dev/sda",
        runner=LinuxSataRunner(),
        sys_class_nvme=tmp_path / "nvme",
        sys_pci=tmp_path / "pci",
        sys_class_block=tmp_path / "block",
    )
    assert snap.controller_info["protocol"] == "ATA"
    assert snap.controller_info["transport"] == "SATA"
    assert snap.controller_info["model"] == "Samsung SSD 870 EVO 2TB"
    assert snap.smart["smart_passed"] is True
    assert snap.smart["reallocated_sectors"] == 0
    assert snap.capabilities["ata_smart"] is True
    assert snap.capabilities["sata_link"] is True
    report = diagnose(snap)
    assert report.status == "OK"
    text = render_text(report)
    assert "Protocol             ATA" in text
    assert "SATA version" in text
    assert "SMART overall        PASSED" in text


def test_sata_pending_sector_is_critical(tmp_path):
    payload = json.loads(json.dumps(ATA_PAYLOAD))
    for row in payload["ata_smart_attributes"]["table"]:
        if row["id"] == 197:
            row["raw"]["value"] = 3

    class Runner(LinuxSataRunner):
        def run(self, argv, timeout=None):
            if argv and argv[0] == "smartctl" and "-a" in argv:
                return CommandResult(argv, 0, stdout=json.dumps(payload))
            return super().run(argv, timeout)

    snap = collect_snapshot_linux(
        "/dev/sda", runner=Runner(), sys_class_nvme=tmp_path / "nvme",
        sys_pci=tmp_path / "pci", sys_class_block=tmp_path / "block",
    )
    report = diagnose(snap)
    assert report.status == "CRITICAL"
    assert any(f.code == "ata-unstable-sectors" for f in report.findings)


def test_macos_scan_keeps_sat_devices():
    items = _parse_smartctl_scan(
        "/dev/disk4 -d sat # /dev/disk4 [USB SATA], ATA device\n"
    )
    assert items == [{
        "device": "/dev/disk4",
        "smartctl_type": "sat",
        "protocol_hint": "ATA",
        "scan_line": "/dev/disk4 -d sat # /dev/disk4 [USB SATA], ATA device",
    }]


class MacSataRunner:
    def run(self, argv, timeout=None):
        if argv[:3] == ["diskutil", "info", "-plist"]:
            payload = {
                "DeviceIdentifier": "disk2",
                "WholeDisk": True,
                "VirtualOrPhysical": "Physical",
                "Internal": True,
                "SolidState": True,
                "BusProtocol": "SATA",
                "MediaName": "Samsung SSD 870 EVO 2TB",
                "TotalSize": 2_000_398_934_016,
            }
            return CommandResult(argv, 0, stdout=plistlib.dumps(payload).decode("utf-8"))
        if argv and argv[0] == "smartctl":
            payload = json.loads(json.dumps(ATA_PAYLOAD))
            payload["device"]["name"] = "/dev/disk2"
            return CommandResult(argv, 0, stdout=json.dumps(payload))
        if argv and argv[0] == "log":
            return CommandResult(argv, 0, stdout="")
        return CommandResult(argv, 0, stdout="")


def test_macos_native_sata_fast_path():
    snap = collect_snapshot_macos("disk2", runner=MacSataRunner())
    assert snap.controller_info["protocol"] == "ATA"
    assert snap.controller_info["transport"] == "SATA"
    assert snap.capabilities["ata_smart"] is True
    assert snap.smart["power_on_hours"] == 1234
    assert diagnose(snap).status == "OK"



def test_linux_list_discovers_native_sata_even_when_smartctl_scan_is_empty(tmp_path):
    block = tmp_path / "block"
    disk = block / "sda"
    dev = tmp_path / "devices" / "pci0000:00" / "0000:00:17.0" / "ata1" / "host0" / "target0:0:0" / "0:0:0:0"
    dev.mkdir(parents=True)
    disk.mkdir(parents=True)
    (disk / "queue").mkdir()
    (disk / "queue" / "rotational").write_text("0\n")
    (dev / "type").write_text("0\n")
    (dev / "vendor").write_text("ATA\n")
    (dev / "model").write_text("Samsung SSD 870 EVO 2TB\n")
    (disk / "device").symlink_to(dev, target_is_directory=True)

    class Runner:
        def run(self, argv, timeout=None):
            if argv[:2] in (["smartctl", "--scan-open"], ["smartctl", "--scan"]):
                return CommandResult(argv, 0, stdout="")
            if argv == ["smartctl", "-i", "-j", "/dev/sda"]:
                return CommandResult(argv, 0, stdout=json.dumps(ATA_PAYLOAD))
            return CommandResult(argv, 127, stderr="not found", available=False)

    items = discover_controllers_linux(
        sys_class_nvme=tmp_path / "nvme", runner=Runner(), sys_class_block=block,
    )
    assert len(items) == 1
    assert items[0]["device"] == "/dev/sda"
    assert items[0]["protocol"] == "ATA"
    assert items[0]["transport"] == "SATA"
    assert items[0]["model"] == "Samsung SSD 870 EVO 2TB"


def test_linux_list_keeps_native_disk_visible_when_smartctl_needs_privilege(tmp_path):
    block = tmp_path / "block"
    disk = block / "sdb"
    dev = tmp_path / "devices" / "pci0000:00" / "0000:00:17.0" / "ata2" / "host1" / "target1:0:0" / "1:0:0:0"
    dev.mkdir(parents=True)
    disk.mkdir(parents=True)
    (disk / "queue").mkdir()
    (disk / "queue" / "rotational").write_text("1\n")
    (dev / "type").write_text("0\n")
    (dev / "vendor").write_text("ATA\n")
    (dev / "model").write_text("WDC WD40EFRX\n")
    (disk / "device").symlink_to(dev, target_is_directory=True)

    class Runner:
        def run(self, argv, timeout=None):
            if argv[:2] in (["smartctl", "--scan-open"], ["smartctl", "--scan"]):
                return CommandResult(argv, 2, stderr="permission denied")
            if argv == ["smartctl", "-i", "-j", "/dev/sdb"]:
                return CommandResult(argv, 2, stderr="permission denied")
            return CommandResult(argv, 127, stderr="not found", available=False)

    items = discover_controllers_linux(
        sys_class_nvme=tmp_path / "nvme", runner=Runner(), sys_class_block=block,
    )
    assert len(items) == 1
    assert items[0]["device"] == "/dev/sdb"
    assert items[0]["state"] == "live"
    assert items[0]["protocol"] == "ATA"
    assert items[0]["transport"] == "SATA"


class MacUsbSataListRunner:
    def run(self, argv, timeout=None):
        if argv[:3] == ["system_profiler", "SPNVMeDataType", "-json"]:
            return CommandResult(argv, 0, stdout=json.dumps({"SPNVMeDataType": []}))
        if argv[:2] == ["system_profiler", "SPNVMeDataType"]:
            return CommandResult(argv, 0, stdout="")
        if argv[:2] in (["smartctl", "--scan-open"], ["smartctl", "--scan"]):
            return CommandResult(argv, 0, stdout="")
        if argv[:3] == ["diskutil", "list", "-plist"]:
            return CommandResult(argv, 0, stdout=plistlib.dumps({
                "AllDisksAndPartitions": [{"DeviceIdentifier": "disk4"}]
            }).decode("utf-8"))
        if argv[:3] == ["diskutil", "info", "-plist"]:
            return CommandResult(argv, 0, stdout=plistlib.dumps({
                "DeviceIdentifier": "disk4", "WholeDisk": True,
                "VirtualOrPhysical": "Physical", "Internal": False,
                "SolidState": False, "BusProtocol": "USB",
                "MediaName": "USB3.0 SATA Bridge", "TotalSize": 4_000_787_030_016,
            }).decode("utf-8"))
        if argv[:3] == ["smartctl", "-i", "-j"] and "sat" in argv and argv[-1] == "/dev/disk4":
            payload = json.loads(json.dumps(ATA_PAYLOAD))
            payload["device"] = {"name": "/dev/disk4", "type": "sat", "protocol": "ATA"}
            payload["model_name"] = "WDC WD40EFRX-68WT0N0"
            payload["rotation_rate"] = 5400
            return CommandResult(argv, 0, stdout=json.dumps(payload))
        return CommandResult(argv, 127, stderr="not found", available=False)


def test_macos_list_discovers_usb_sata_from_diskutil_even_when_scan_is_empty():
    items = discover_controllers_macos(MacUsbSataListRunner())
    assert len(items) == 1
    assert items[0]["device"] == "/dev/disk4"
    assert items[0]["protocol"] == "ATA"
    assert items[0]["transport"] == "USB -> SATA"
    assert items[0]["smartctl_type"] == "sat"
    assert items[0]["model"] == "WDC WD40EFRX-68WT0N0"


def _make_native_sata_sysfs(tmp_path, name="sda"):
    block = tmp_path / "block"
    disk = block / name
    dev = tmp_path / "devices" / "pci0000:00" / "0000:00:17.0" / "ata1" / "host0" / "target0:0:0" / "0:0:0:0"
    dev.mkdir(parents=True)
    disk.mkdir(parents=True)
    (disk / "queue").mkdir()
    (disk / "queue" / "rotational").write_text("0\n")
    (dev / "type").write_text("0\n")
    (dev / "vendor").write_text("ATA\n")
    (dev / "model").write_text("Samsung SSD 870 EVO 2TB\n")
    (disk / "device").symlink_to(dev, target_is_directory=True)
    return block


def test_linux_list_retries_explicit_ata_when_generic_probe_is_scsi_json(tmp_path):
    block = _make_native_sata_sysfs(tmp_path)
    scsi_payload = {"device": {"name": "/dev/sda", "type": "scsi", "protocol": "SCSI"}, "model_name": "ATA disk"}

    class Runner:
        def run(self, argv, timeout=None):
            if argv[:2] in (["smartctl", "--scan-open"], ["smartctl", "--scan"]):
                return CommandResult(argv, 0, stdout="")
            if argv == ["smartctl", "-i", "-j", "/dev/sda"]:
                return CommandResult(argv, 0, stdout=json.dumps(scsi_payload))
            if argv == ["smartctl", "-d", "ata", "-i", "-j", "/dev/sda"]:
                return CommandResult(argv, 0, stdout=json.dumps(ATA_PAYLOAD))
            return CommandResult(argv, 127, stderr="not found", available=False)

    items = discover_controllers_linux(
        sys_class_nvme=tmp_path / "nvme", runner=Runner(), sys_class_block=block,
    )
    assert len(items) == 1
    assert items[0]["device"] == "/dev/sda"
    assert items[0]["protocol"] == "ATA"
    assert items[0]["transport"] == "SATA"
    assert items[0]["model"] == "Samsung SSD 870 EVO 2TB"


def test_linux_list_never_drops_physical_disk_on_valid_unknown_smartctl_json(tmp_path):
    block = _make_native_sata_sysfs(tmp_path)
    unknown = {"device": {"name": "/dev/sda", "type": "scsi", "protocol": "SCSI"}, "model_name": "ambiguous"}

    class Runner:
        def run(self, argv, timeout=None):
            if argv[:2] in (["smartctl", "--scan-open"], ["smartctl", "--scan"]):
                return CommandResult(argv, 0, stdout="")
            if argv and argv[0] == "smartctl":
                return CommandResult(argv, 0, stdout=json.dumps(unknown))
            return CommandResult(argv, 127, stderr="not found", available=False)

    items = discover_controllers_linux(
        sys_class_nvme=tmp_path / "nvme", runner=Runner(), sys_class_block=block,
    )
    assert len(items) == 1
    assert items[0]["device"] == "/dev/sda"
    assert items[0]["protocol"] == "ATA"
    assert items[0]["transport"] == "SATA"
    assert items[0]["state"] == "live"


def test_linux_check_retries_explicit_ata_backend_for_native_sata(tmp_path):
    block = _make_native_sata_sysfs(tmp_path)
    scsi_payload = {"device": {"name": "/dev/sda", "type": "scsi", "protocol": "SCSI"}}

    class Runner:
        def run(self, argv, timeout=None):
            if argv[:2] in (["smartctl", "--scan-open"], ["smartctl", "--scan"]):
                return CommandResult(argv, 0, stdout="")
            if argv == ["smartctl", "-a", "-j", "/dev/sda"]:
                return CommandResult(argv, 0, stdout=json.dumps(scsi_payload))
            if argv == ["smartctl", "-d", "ata", "-a", "-j", "/dev/sda"]:
                return CommandResult(argv, 0, stdout=json.dumps(ATA_PAYLOAD))
            if argv and argv[0] in {"journalctl", "dmesg"}:
                return CommandResult(argv, 0, stdout="")
            return CommandResult(argv, 127, stderr="not found", available=False)

    snap = collect_snapshot_linux(
        "/dev/sda", runner=Runner(), sys_class_nvme=tmp_path / "nvme",
        sys_pci=tmp_path / "pci", sys_class_block=block,
    )
    assert snap.controller_info["protocol"] == "ATA"
    assert snap.controller_info["transport"] == "SATA"
    assert snap.capabilities["ata_smart"] is True
    assert snap.smart["power_on_hours"] == 1234


def test_linux_sata_fallback_uses_live_state_and_udev_identity(tmp_path):
    block = _make_native_sata_sysfs(tmp_path)
    # Model/revision available from sysfs as a secondary fallback.
    dev = (block / "sda" / "device").resolve()
    (dev / "state").write_text("running\n")
    (dev / "rev").write_text("SCV10200\n")
    unknown = {"device": {"name": "/dev/sda", "type": "scsi", "protocol": "SCSI"}}

    class Runner:
        def run(self, argv, timeout=None):
            if argv[:2] in (["smartctl", "--scan-open"], ["smartctl", "--scan"]):
                return CommandResult(argv, 0, stdout="")
            if argv and argv[0] == "smartctl":
                return CommandResult(argv, 0, stdout=json.dumps(unknown))
            if argv and argv[0] == "udevadm":
                return CommandResult(argv, 0, stdout=(
                    "ID_BUS=ata\n"
                    "ID_MODEL=SOLIDIGM_SSDSC2KB019T8\n"
                    "ID_SERIAL_SHORT=PHYG123456781P9DGN\n"
                    "ID_REVISION=SCV10200\n"
                ))
            return CommandResult(argv, 127, stderr="not found", available=False)

    items = discover_controllers_linux(
        sys_class_nvme=tmp_path / "nvme", runner=Runner(), sys_class_block=block,
    )
    assert len(items) == 1
    row = items[0]
    assert row["state"] == "live"
    assert row["protocol"] == "ATA"
    assert row["transport"] == "SATA"
    assert row["model"] == "SOLIDIGM SSDSC2KB019T8"
    assert row["serial"] == "PHYG123456781P9DGN"
    assert row["firmware"] == "SCV10200"


def test_linux_sata_fallback_uses_sysfs_revision_when_udev_unavailable(tmp_path):
    block = _make_native_sata_sysfs(tmp_path)
    dev = (block / "sda" / "device").resolve()
    (dev / "state").write_text("running\n")
    (dev / "rev").write_text("FW42\n")
    unknown = {"device": {"name": "/dev/sda", "type": "scsi", "protocol": "SCSI"}}

    class Runner:
        def run(self, argv, timeout=None):
            if argv[:2] in (["smartctl", "--scan-open"], ["smartctl", "--scan"]):
                return CommandResult(argv, 0, stdout="")
            if argv and argv[0] == "smartctl":
                return CommandResult(argv, 0, stdout=json.dumps(unknown))
            return CommandResult(argv, 127, stderr="not found", available=False)

    row = discover_controllers_linux(
        sys_class_nvme=tmp_path / "nvme", runner=Runner(), sys_class_block=block,
    )[0]
    assert row["state"] == "live"
    assert row["firmware"] == "FW42"
    assert row["model"] == "Samsung SSD 870 EVO 2TB"


def test_linux_check_native_sata_reuses_list_identity_state_and_ignores_scsi_scan_hint(tmp_path):
    block = _make_native_sata_sysfs(tmp_path)
    dev = (block / "sda" / "device").resolve()
    (dev / "state").write_text("running\n")
    (dev / "rev").write_text("7CV10130\n")
    scsi_payload = {
        "device": {"name": "/dev/sda", "type": "scsi", "protocol": "SCSI"},
        "model_name": "ATA SOLIDIGM SSDSC2K",
        "user_capacity": {"bytes": 1920383410176},
    }

    class Runner:
        def run(self, argv, timeout=None):
            if argv[:2] in (["smartctl", "--scan-open"], ["smartctl", "--scan"]):
                return CommandResult(argv, 0, stdout="/dev/sda -d scsi # SCSI device\n")
            if argv and argv[0] == "udevadm":
                return CommandResult(argv, 0, stdout=(
                    "ID_BUS=ata\n"
                    "ID_MODEL=SOLIDIGM_SSDSC2KB019TZ\n"
                    "ID_SERIAL_SHORT=BTYI549203JJ1P9DGN\n"
                    "ID_REVISION=7CV10130\n"
                ))
            if argv == ["smartctl", "-d", "ata", "-a", "-j", "/dev/sda"]:
                return CommandResult(argv, 4, stdout=json.dumps(scsi_payload))
            if argv == ["smartctl", "-a", "-j", "/dev/sda"]:
                return CommandResult(argv, 4, stdout=json.dumps(scsi_payload))
            if argv == ["smartctl", "-d", "sat", "-a", "-j", "/dev/sda"]:
                return CommandResult(argv, 4, stdout=json.dumps(scsi_payload))
            if argv and argv[0] in {"journalctl", "dmesg"}:
                return CommandResult(argv, 0, stdout="")
            return CommandResult(argv, 127, stderr="not found", available=False)

    snap = collect_snapshot_linux(
        "/dev/sda", runner=Runner(), sys_class_nvme=tmp_path / "nvme",
        sys_pci=tmp_path / "pci", sys_class_block=block,
    )
    assert snap.controller_info["protocol"] == "ATA"
    assert snap.controller_info["transport"] == "SATA"
    assert snap.controller_info["state"] == "live"
    assert snap.controller_info["model"] == "SOLIDIGM SSDSC2KB019TZ"
    assert snap.controller_info["serial"] == "BTYI549203JJ1P9DGN"
    assert snap.controller_info["firmware_rev"] == "7CV10130"
    assert snap.controller_info["smartctl_device_type"] == "ata"
    assert snap.controller_info.get("usb_path") == {}

    report = diagnose(snap)
    assert report.status == "INCOMPLETE"
    text = render_text(report)
    assert "SATA transport" in text
    assert "USB transport" not in text
    assert "Capacity" in text
    assert "NVM capacity" not in text
    assert "State                live" in text
    assert "SOLIDIGM SSDSC2KB019TZ" in text
    assert "BTYI549203JJ1P9DGN" in text
    assert "7CV10130" in text


def test_sata_smartctl_synthetic_spare_is_ignored_in_favor_of_explicit_reserve(tmp_path):
    # Real Solidigm D3-S4520-style data: smartctl synthesizes spare_available=47
    # from SMART 5's normalized VALUE even though RAW reallocations are zero.
    payload = json.loads(json.dumps(ATA_PAYLOAD))
    payload["model_name"] = "SOLIDIGM SSDSC2KB019TZ"
    payload["spare_available"] = {"current_percent": 47}
    payload["endurance_used"] = None
    payload["ata_smart_attributes"]["table"] = [
        {"id": 5, "name": "Reallocated_Sector_Ct", "value": 47, "worst": 47, "thresh": 0, "when_failed": "", "raw": {"value": 0, "string": "0"}},
        {"id": 170, "name": "Available_Reservd_Space", "value": 100, "worst": 100, "thresh": 10, "when_failed": "", "raw": {"value": 0, "string": "0"}},
        {"id": 226, "name": "Workld_Media_Wear_Indic", "value": 100, "worst": 100, "thresh": 0, "when_failed": "", "raw": {"value": 0, "string": "0"}},
        {"id": 232, "name": "Available_Reservd_Space", "value": 100, "worst": 100, "thresh": 10, "when_failed": "", "raw": {"value": 0, "string": "0"}},
        {"id": 233, "name": "Media_Wearout_Indicator", "value": 100, "worst": 100, "thresh": 0, "when_failed": "", "raw": {"value": 0, "string": "0"}},
    ]

    class Runner(LinuxSataRunner):
        def run(self, argv, timeout=None):
            if argv and argv[0] == "smartctl" and "-a" in argv:
                return CommandResult(argv, 0, stdout=json.dumps(payload))
            return super().run(argv, timeout)

    snap = collect_snapshot_linux(
        "/dev/sda", runner=Runner(), sys_class_nvme=tmp_path / "nvme",
        sys_pci=tmp_path / "pci", sys_class_block=tmp_path / "block",
    )
    assert snap.smart["reallocated_sectors"] == 0
    assert "spare_available" not in snap.smart
    assert "endurance_used" not in snap.smart
    assert snap.smart["reserved_space"] == 100
    assert snap.smart["reserved_space_threshold"] == 10
    assert snap.smart["reserved_space_margin"] == 90
    assert snap.smart["media_wear_indicator"] == 100
    assert snap.smart["reserved_space_source"]["attribute_id"] == 170
    assert [x["id"] for x in snap.smart["reserved_space_source"]["sources"]] == [170, 232]

    report = diagnose(snap)
    assert report.status == "OK"
    text = render_text(report)
    assert "Available spare" not in text
    assert "47%" not in text
    assert "Reserved space       100/100" in text
    assert "Reserve threshold    10/100" in text
    assert "Reserve margin       +90" in text
    assert "Media wear indicator 100/100" in text
    assert "Flash reserve" in text
    assert "reserved-space indicator 100/100; threshold 10/100; margin +90; media-wear indicator 100/100" in text

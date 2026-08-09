import json
from pathlib import Path

from src.collect import collect_snapshot_linux, discover_controllers_linux
from src.model import CommandResult
from src.util import normalize_device


SMARTCTL_NVME = {
    "device": {"name": "/dev/sdf", "type": "nvme", "protocol": "NVMe"},
    "model_name": "Samsung SSD 990 EVO Plus 2TB",
    "serial_number": "TESTNVME000000000001",
    "firmware_version": "1B2QKXG7",
    "nvme_version": {"string": "2.0", "value": 131072},
    "user_capacity": {"bytes": 2000398934016},
    "nvme_smart_health_information_log": {
        "critical_warning": 0,
        "temperature": 35,
        "available_spare": 100,
        "available_spare_threshold": 10,
        "percentage_used": 0,
        "data_units_read": 3601520,
        "data_units_written": 3736796,
        "host_reads": 75054394,
        "host_writes": 1114056301,
        "controller_busy_time": 177,
        "power_cycles": 138,
        "power_on_hours": 55,
        "unsafe_shutdowns": 55,
        "media_errors": 0,
        "num_err_log_entries": 0,
        "warning_temp_time": 0,
        "critical_comp_time": 0,
    },
    "nvme_error_information_log": [],
}


class FakeRunner:
    def __init__(self, mapping):
        self.mapping = mapping
        self.calls = []

    def run(self, argv, timeout=8.0):
        key = tuple(argv)
        self.calls.append(key)
        value = self.mapping.get(key)
        if value is None:
            return CommandResult(list(argv), 127, stderr=f"{argv[0]} not found", available=False)
        return value


def result(argv, returncode=0, stdout="", stderr=""):
    return CommandResult(list(argv), returncode, stdout=stdout, stderr=stderr, available=True)


def test_normalize_linux_usb_block_target():
    assert normalize_device("sdf") == ("sdf", "/dev/sdf")
    assert normalize_device("/dev/sdf1") == ("sdf", "/dev/sdf")


def test_linux_list_discovers_usb_translated_nvme_from_smartctl_scan(tmp_path):
    runner = FakeRunner({
        ("smartctl", "--scan-open"): result(["smartctl"], stdout="/dev/sdf -d nvme # USB NVMe device\n"),
        ("smartctl", "--scan"): result(["smartctl"], stdout="/dev/sdf -d nvme # USB NVMe device\n"),
        ("smartctl", "-i", "-j", "/dev/sdf"): result(["smartctl"], stdout=json.dumps(SMARTCTL_NVME)),
    })
    items = discover_controllers_linux(
        sys_class_nvme=tmp_path / "nvme",
        runner=runner,
        sys_class_block=tmp_path / "block",
    )
    assert len(items) == 1
    item = items[0]
    assert item["controller"] == "sdf"
    assert item["device"] == "/dev/sdf"
    assert item["model"] == "Samsung SSD 990 EVO Plus 2TB"
    assert item["protocol"] == "NVMe"
    assert item["native_nvme"] is False


def test_linux_check_sdf_uses_smartctl_as_primary_nvme_source(tmp_path):
    runner = FakeRunner({
        ("smartctl", "-a", "-j", "/dev/sdf"): result(["smartctl"], stdout=json.dumps(SMARTCTL_NVME)),
    })
    snap = collect_snapshot_linux(
        "sdf",
        runner=runner,
        sys_class_nvme=tmp_path / "nvme",
        sys_pci=tmp_path / "pci",
        kernel_lines=20,
    )
    assert snap.controller == "sdf"
    assert snap.device_path == "/dev/sdf"
    assert snap.controller_info["model"] == "Samsung SSD 990 EVO Plus 2TB"
    assert snap.controller_info["serial"] == "TESTNVME000000000001"
    assert snap.controller_info["nvme_passthrough"] is True
    assert snap.controller_info["native_nvme"] is False
    assert snap.smart["unsafe_shutdowns"] == 55
    assert snap.capabilities["nvme_smart"] is True
    assert snap.capabilities["pcie_link"] is False
    assert not any(call and call[0] == "nvme" for call in runner.calls)


def test_unprivileged_list_keeps_usb_ssd_as_unverified_candidate(tmp_path):
    block = tmp_path / "block"
    disk = block / "sdf"
    usb_dev = tmp_path / "devices" / "pci0000:00" / "usb1" / "1-1" / "host0" / "target0:0:0" / "0:0:0:0"
    usb_dev.mkdir(parents=True)
    disk.mkdir(parents=True)
    (disk / "queue").mkdir()
    (disk / "queue" / "rotational").write_text("0\n")
    (usb_dev / "model").write_text("RTL9210 Enclosure\n")
    (usb_dev / "vendor").write_text("ACASIS\n")
    (disk / "device").symlink_to(usb_dev, target_is_directory=True)

    runner = FakeRunner({
        ("smartctl", "--scan-open"): result(["smartctl"], returncode=2, stderr="permission denied"),
        ("smartctl", "--scan"): result(["smartctl"], stdout=""),
        ("smartctl", "-i", "-j", "/dev/sdf"): result(["smartctl"], returncode=2, stderr="permission denied"),
    })
    items = discover_controllers_linux(
        sys_class_nvme=tmp_path / "nvme",
        runner=runner,
        sys_class_block=block,
    )
    assert len(items) == 1
    assert items[0]["controller"] == "sdf"
    assert items[0]["state"] == "probe-needed"
    assert "unverified" in items[0]["transport"].lower()

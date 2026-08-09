from src.diagnose import diagnose
from src.model import Snapshot
from src.render import render_text


def test_render_shows_pcie_generation_and_lifetime_context():
    s = Snapshot("nvme4", "nvme4", "/dev/nvme4")
    s.host = {"platform": "linux", "platform_label": "Linux", "os_release": {"PRETTY_NAME": "Ubuntu 26.04 LTS"}}
    s.controller_info = {
        "state": "live",
        "model": "INTEL SSDPF2KX960HZ",
        "serial": "PHAO405001BW960RGN",
        "firmware_rev": "YCV10200",
        "nvme_version_raw": 0x00010400,
    }
    s.pci = {
        "bdf": "0000:f2:00.0",
        "current_link_speed": "16.0 GT/s PCIe",
        "current_link_width": "4",
        "max_link_speed": "16.0 GT/s PCIe",
        "max_link_width": "4",
        "numa_node": "4",
    }
    s.smart = {
        "critical_warning": 0,
        "temperature": 43,
        "percent_used": 0,
        "avail_spare": 100,
        "spare_thresh": 10,
        "media_errors": 0,
        "num_err_log_entries": 0,
        "power_cycles": 100,
        "power_on_hours": 1234,
        "unsafe_shutdowns": 50,
        "data_units_read": 1_000_000,
        "data_units_written": 2_000_000,
        "controller_busy_time": 120,
    }
    text = render_text(diagnose(s))
    assert "PCIe generation      Gen4" in text
    assert "NVMe version         1.4" in text
    assert "Unsafe shutdowns     50 (50.0% of power cycles)" in text
    assert "Data read            512.00 GB" in text
    assert "Data written         1.02 TB" in text
    assert "Controller busy      2 h" in text
    assert "does not store a per-event reason or timestamp" in text


def test_render_shows_gen5_and_downgrade_when_current_gen4():
    s = Snapshot("nvme0", "nvme0", "/dev/nvme0")
    s.controller_info = {"state": "live"}
    s.smart = {"critical_warning": 0, "media_errors": 0, "unsafe_shutdowns": 0}
    s.pci = {
        "current_link_speed": "16.0 GT/s PCIe",
        "current_link_width": "4",
        "max_link_speed": "32.0 GT/s PCIe",
        "max_link_width": "4",
    }
    report = diagnose(s)
    text = render_text(report)
    assert "PCIe generation      Gen4 (max Gen5)" in text
    assert report.status == "WARNING"


def test_render_has_doctor_assessment_and_smartctl_version_object():
    s = Snapshot("nvme0", "nvme0", "/dev/nvme0")
    s.host = {"platform": "linux", "platform_label": "Linux"}
    s.controller_info = {"state": "live", "nvme_version": {"string": "2.0", "value": 131072}}
    s.smart = {
        "critical_warning": 0,
        "temperature": 46,
        "percent_used": 0,
        "avail_spare": 100,
        "spare_thresh": 10,
        "media_errors": 0,
        "num_err_log_entries": 0,
        "power_cycles": 72,
        "unsafe_shutdowns": 23,
        "warning_temp_time": 0,
        "critical_comp_time": 0,
    }
    s.pci = {
        "current_link_speed": "32.0 GT/s PCIe",
        "current_link_width": "4",
        "max_link_speed": "32.0 GT/s PCIe",
        "max_link_width": "4",
    }
    s.capabilities = {"targeted_kernel_log": True}
    report = diagnose(s)
    text = render_text(report)
    assert "NVMe version         2.0" in text
    assert "{'string':" not in text
    assert "DOCTOR'S ASSESSMENT" in text
    assert "Verdict              HEALTHY NOW" in text
    assert "Media / integrity    CLEAN" in text
    assert "PCIe / controller    CLEAN" in text
    assert "Thermal              NORMAL" in text
    assert "Endurance            EXCELLENT" in text
    assert "History              REVIEW" in text
    assert "No immediate SSD repair/replacement is indicated" in text
    assert "FINDINGS / EVIDENCE" in text

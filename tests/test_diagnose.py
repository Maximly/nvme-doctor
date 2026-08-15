from src.diagnose import diagnose
from src.model import Snapshot


def snap():
    s = Snapshot("/dev/nvme0", "nvme0", "/dev/nvme0")
    s.controller_info = {"state": "live", "model": "Example", "firmware_rev": "1.0"}
    s.smart = {
        "critical_warning": 0,
        "temperature": 40,
        "avail_spare": 100,
        "spare_thresh": 10,
        "percent_used": 12,
        "media_errors": 0,
        "num_err_log_entries": 0,
        "unsafe_shutdowns": 0,
    }
    return s


def codes(report):
    return {f.code for f in report.findings}


def test_clean_snapshot_is_ok():
    report = diagnose(snap())
    assert report.status == "OK"


def test_media_error_warning():
    s = snap()
    s.smart["media_errors"] = 2
    report = diagnose(s)
    assert report.status == "WARNING"
    assert "media-errors" in codes(report)


def test_spare_below_threshold_is_critical():
    s = snap()
    s.smart["avail_spare"] = 5
    report = diagnose(s)
    assert report.status == "CRITICAL"
    assert "available-spare" in codes(report)


def test_pcie_correlation_beats_media_hypothesis():
    s = snap()
    s.pci = {
        "bdf": "0000:41:00.0",
        "aer": {"aer_dev_correctable": {"RxErr": 7}},
    }
    s.kernel_lines = [
        "nvme nvme0: I/O 3 QID 0 timeout, reset controller",
        "pcieport 0000:40:01.1: AER: Corrected error received",
    ]
    report = diagnose(s)
    assert "likely-pcie-path" in codes(report)


def test_power_hypothesis_is_low_confidence_info():
    s = snap()
    s.power = {
        "nvme_default_ps_max_latency_us": "100000",
        "pcie_aspm_policy": "default powersave [powersupersave] performance",
    }
    s.kernel_lines = ["nvme nvme0: I/O timeout, resetting controller"]
    report = diagnose(s)
    finding = next(f for f in report.findings if f.code == "power-state-hypothesis")
    assert finding.severity == "info"
    assert finding.confidence == "low"


def test_missing_health_evidence_is_incomplete_not_ok():
    s = snap()
    s.smart = {}
    report = diagnose(s)
    assert report.status == "INCOMPLETE"


def test_usb_reset_is_transport_warning_even_with_clean_smart():
    s = snap()
    s.controller = "sdf"
    s.device_path = "/dev/sdf"
    s.controller_info.update({
        "native_nvme": False,
        "transport": "USB -> NVMe",
        "nvme_passthrough": True,
        "usb_path": {
            "usb_port": "2-3", "vid_pid": "0bda:9210",
            "usb_version": "3.20", "speed_mbps": 10000, "interface_driver": "uas",
        },
    })
    s.kernel_lines = [
        "usb 2-3: reset SuperSpeed USB device number 5 using xhci_hcd",
        "uas_eh_device_reset_handler 6:0:0:0: reset",
    ]
    report = diagnose(s)
    assert report.status == "WARNING"
    assert "usb-transport-instability" in codes(report)
    assert any(row["label"] == "USB transport" and row["state"] == "PROBLEM" for row in report.assessment["domains"])


def test_usb3_bridge_at_480mbps_is_downshift_warning():
    s = snap()
    s.controller = "sdf"
    s.device_path = "/dev/sdf"
    s.controller_info.update({
        "native_nvme": False,
        "transport": "USB -> NVMe",
        "nvme_passthrough": True,
        "usb_path": {"usb_port": "2-3", "usb_version": "3.20", "speed_mbps": 480, "interface_driver": "uas"},
    })
    report = diagnose(s)
    assert "usb-link-downshift" in codes(report)
    assert report.status == "WARNING"

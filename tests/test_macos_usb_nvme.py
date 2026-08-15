import struct

from src.macos_usb_nvme import parse_error_log, parse_firmware_slot_log, parse_identify_controller, parse_smart_log


def test_parse_rtl9210_nvme_identify_samsung_990_evo_plus():
    raw = bytearray(4096)
    struct.pack_into("<H", raw, 0, 0x144D)
    struct.pack_into("<H", raw, 2, 0x144D)
    raw[4:24] = b"TESTNVME000000000001".ljust(20, b" ")
    raw[24:64] = b"Samsung SSD 990 EVO Plus 2TB".ljust(40, b" ")
    raw[64:72] = b"1B2QKXG7"
    raw[73:76] = bytes([0x38, 0x25, 0x00])
    raw[77] = 7
    struct.pack_into("<H", raw, 78, 1)
    struct.pack_into("<I", raw, 80, 0x00020000)
    struct.pack_into("<H", raw, 256, 0x0017)
    raw[280:296] = (2_000_398_934_016).to_bytes(16, "little")
    struct.pack_into("<H", raw, 520, 0x00DF)

    ident = parse_identify_controller(bytes(raw))
    assert ident["model"] == "Samsung SSD 990 EVO Plus 2TB"
    assert ident["serial"] == "TESTNVME000000000001"
    assert ident["firmware_rev"] == "1B2QKXG7"
    assert ident["nvme_version"] == "2.0"
    assert ident["ieee_oui"] == "00:25:38"
    assert ident["tnvmcap"] == 2_000_398_934_016
    assert ident["oacs"] == 0x0017
    assert ident["oncs"] == 0x00DF


def test_parse_rtl9210_nvme_smart():
    raw = bytearray(512)
    raw[0] = 0
    struct.pack_into("<H", raw, 1, 310)  # 37 C
    raw[3] = 100
    raw[4] = 10
    raw[5] = 0
    raw[32:48] = (3_611_000).to_bytes(16, "little")
    raw[48:64] = (3_736_800).to_bytes(16, "little")
    raw[64:80] = (75_187_516).to_bytes(16, "little")
    raw[80:96] = (1_114_058_618).to_bytes(16, "little")
    raw[96:112] = (179).to_bytes(16, "little")
    raw[112:128] = (139).to_bytes(16, "little")
    raw[128:144] = (56).to_bytes(16, "little")
    raw[144:160] = (55).to_bytes(16, "little")
    raw[160:176] = (0).to_bytes(16, "little")
    raw[176:192] = (0).to_bytes(16, "little")
    struct.pack_into("<H", raw, 200, 315)  # 42 C sensor 1
    struct.pack_into("<H", raw, 202, 310)  # 37 C sensor 2

    smart = parse_smart_log(bytes(raw))
    assert smart["temperature"] == 37
    assert smart["avail_spare"] == 100
    assert smart["power_cycles"] == 139
    assert smart["power_on_hours"] == 56
    assert smart["unsafe_shutdowns"] == 55
    assert smart["host_write_commands"] == 1_114_058_618
    assert smart["temperature_sensors"] == [42, 37]


def test_mounted_volumes_maps_apfs_physical_store(monkeypatch):
    import plistlib
    from types import SimpleNamespace
    import src.macos_usb_nvme as mod

    disk_list = plistlib.dumps({
        "AllDisksAndPartitions": [{
            "DeviceIdentifier": "disk4",
            "Partitions": [{
                "DeviceIdentifier": "disk4s2",
                "Content": "Apple_APFS",
            }],
        }]
    })
    apfs_list = plistlib.dumps({
        "Containers": [{
            "PhysicalStores": [{"DeviceIdentifier": "disk4s2"}],
            "Volumes": [
                {"DeviceIdentifier": "disk5s1", "MountPoint": "/Volumes/External"},
                {"DeviceIdentifier": "disk5s2"},
            ],
        }]
    })

    def fake_run(argv):
        if argv[:3] == ["diskutil", "list", "-plist"]:
            return SimpleNamespace(returncode=0, stdout=disk_list, stderr=b"")
        if argv[:4] == ["diskutil", "apfs", "list", "-plist"]:
            return SimpleNamespace(returncode=0, stdout=apfs_list, stderr=b"")
        raise AssertionError(argv)

    monkeypatch.setattr(mod, "_run", fake_run)
    assert mod.mounted_volumes("disk4") == ["/Volumes/External"]


def test_mounted_volumes_does_not_assume_unmounted_if_apfs_state_unknown(monkeypatch):
    import plistlib
    from types import SimpleNamespace
    import src.macos_usb_nvme as mod

    disk_list = plistlib.dumps({
        "AllDisksAndPartitions": [{
            "DeviceIdentifier": "disk4",
            "Partitions": [{"DeviceIdentifier": "disk4s2", "Content": "Apple_APFS"}],
        }]
    })

    def fake_run(argv):
        if argv[:3] == ["diskutil", "list", "-plist"]:
            return SimpleNamespace(returncode=0, stdout=disk_list, stderr=b"")
        if argv[:4] == ["diskutil", "apfs", "list", "-plist"]:
            return SimpleNamespace(returncode=1, stdout=b"", stderr=b"failed")
        raise AssertionError(argv)

    monkeypatch.setattr(mod, "_run", fake_run)
    assert mod.mounted_volumes("disk4") is None


def test_parse_rtl9210_nvme_error_log():
    raw = bytearray(4096)
    struct.pack_into("<Q", raw, 0, 7)
    struct.pack_into("<H", raw, 8, 3)
    struct.pack_into("<H", raw, 10, 0x1234)
    struct.pack_into("<H", raw, 12, 0x4002)
    struct.pack_into("<Q", raw, 16, 0x11223344)
    struct.pack_into("<I", raw, 24, 1)
    entries = parse_error_log(bytes(raw))
    assert len(entries) == 1
    assert entries[0]["error_count"] == 7
    assert entries[0]["cmdid"] == 0x1234
    assert entries[0]["lba"] == 0x11223344
    assert entries[0]["nsid"] == 1


def test_parse_rtl9210_firmware_slot_log():
    raw = bytearray(512)
    raw[0] = 0x21
    raw[8:16] = b"FW1.000 "
    raw[16:24] = b"FW2.000 "
    log = parse_firmware_slot_log(bytes(raw))
    assert log["active_slot"] == 1
    assert log["next_active_slot"] == 2
    assert log["slots"][0] == {"slot": 1, "firmware_rev": "FW1.000"}


def test_direct_fast_path_skips_redundant_unmount_and_optional_logs(monkeypatch):
    import ctypes
    from types import SimpleNamespace
    import src.macos_usb_nvme as mod

    class FakeLib:
        def libusb_init(self, ctx): return 0
        def libusb_exit(self, ctx): return None
        def libusb_detach_kernel_driver(self, handle, iface): return 0
        def libusb_attach_kernel_driver(self, handle, iface): return 0
        def libusb_claim_interface(self, handle, iface): return 0
        def libusb_release_interface(self, handle, iface): return 0
        def libusb_set_interface_alt_setting(self, handle, iface, alt): return 0
        def libusb_close(self, handle): return None

    fake = FakeLib()
    handle = ctypes.c_void_p(0x1234)
    commands = []
    diskutil_calls = []
    sequence = []

    identify = bytearray(4096)
    struct.pack_into("<H", identify, 0, 0x144D)
    struct.pack_into("<H", identify, 2, 0x144D)
    identify[4:24] = b"TESTNVME000000000001".ljust(20, b" ")
    identify[24:64] = b"Samsung SSD 990 EVO Plus 2TB".ljust(40, b" ")
    identify[64:72] = b"1B2QKXG7"
    struct.pack_into("<I", identify, 80, 0x00020000)

    smart = bytearray(512)
    struct.pack_into("<H", smart, 1, 310)
    smart[3] = 100
    smart[4] = 10

    monkeypatch.setattr(mod.os, "geteuid", lambda: 0)
    monkeypatch.setattr(mod, "_diskutil_info", lambda disk: {
        "BusProtocol": "USB", "Internal": False, "SolidState": True,
    })
    monkeypatch.setattr(mod, "_external_usb_ssds", lambda: ["disk4"])
    monkeypatch.setattr(mod, "_load_libusb", lambda: (fake, "/fake/libusb.dylib"))
    monkeypatch.setattr(mod, "_setup", lambda lib: None)
    def fake_enumerate(lib, ctx):
        sequence.append("enumerate")
        return [{
            "handle": handle,
            "open_rc": 0,
            "manufacturer": "Realtek",
            "product": "RTL9210",
            "serial": "bridge1",
            "uas": True,
            "bot": {"number": 0, "alt": 1, "bulk_in": 0x81, "bulk_out": 0x02},
        }]
    monkeypatch.setattr(mod, "_enumerate_rtl9210", fake_enumerate)

    def fake_run(argv):
        diskutil_calls.append(list(argv))
        if argv[:2] == ["diskutil", "eject"]:
            sequence.append("eject")
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(mod, "_run", fake_run)

    def fake_rtl_read(lib, h, bot, opcode, cdw10_low, size, tag, timeout_ms=mod.USB_TIMEOUT_MS):
        commands.append((opcode, cdw10_low, size, timeout_ms))
        if opcode == mod.NVME_ADMIN_IDENTIFY:
            return bytes(identify)
        if cdw10_low == mod.NVME_SMART_LOG_ID:
            return bytes(smart)
        raise AssertionError("optional log should not be requested in fast mode")

    monkeypatch.setattr(mod, "_rtl_read", fake_rtl_read)

    progress = []
    result = mod.read_rtl9210_nvme(
        "/dev/disk4",
        mount_state_checked=True,
        mounts_before=[],
        collect_optional_logs=False,
        progress=progress.append,
    )

    assert result["identify"]["model"] == "Samsung SSD 990 EVO Plus 2TB"
    assert len(commands) == 2
    assert not any(call[:2] == ["diskutil", "unmountDisk"] for call in diskutil_calls)
    assert any(call[:2] == ["diskutil", "eject"] for call in diskutil_calls)
    assert sequence.index("eject") < sequence.index("enumerate")
    assert not any(call[:2] == ["diskutil", "mountDisk"] for call in diskutil_calls)
    assert "Ejecting the whole disk cleanly" in progress
    assert "Reading NVMe Identify Controller" in progress
    assert "Reading NVMe SMART / Health log" in progress
    assert progress[-1] == "Direct USB NVMe read complete"


def test_rtl9210_inventory_default_progress_is_defined(monkeypatch):
    import src.macos_usb_nvme as usb

    class FakeLib:
        def libusb_init(self, ctx): return 0
        def libusb_exit(self, ctx): return None
        def libusb_close(self, handle): return None

    fake = FakeLib()
    monkeypatch.setattr(usb, "_load_libusb", lambda: (fake, "/fake/libusb.dylib"))
    monkeypatch.setattr(usb, "_setup", lambda lib: None)
    monkeypatch.setattr(usb, "_enumerate_rtl9210", lambda lib, ctx: [])
    assert usb.rtl9210_inventory() == []


import struct

from src.macos_usb_nvme import parse_identify_controller, parse_smart_log


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

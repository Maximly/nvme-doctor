import struct

from src.macos_usb_sata import (
    ATA_IDENTIFY_DEVICE,
    ATA_PASS_THROUGH_16,
    ATA_PASS_THROUGH_12,
    ATA_SMART,
    ATA_SMART_CYL_HIGH,
    ATA_SMART_CYL_LOW,
    ATA_SMART_READ_DATA,
    _ata_cdb16,
    _ata_cdb12,
    _ata_return_descriptor,
    build_smartctl_like_payload,
    parse_identify_device,
    parse_smart_attributes,
)


def _put_ata_string(buf: bytearray, start_word: int, words: int, text: str) -> None:
    raw = text.ljust(words * 2)[: words * 2].encode("ascii")
    swapped = bytearray(raw)
    for i in range(0, len(swapped), 2):
        swapped[i:i+2] = swapped[i:i+2][::-1]
    buf[start_word * 2:(start_word + words) * 2] = swapped


def test_sat16_identify_cdb_is_read_only_pio_data_in():
    cdb = _ata_cdb16(command=ATA_IDENTIFY_DEVICE)
    assert len(cdb) == 16
    assert cdb[0] == ATA_PASS_THROUGH_16
    assert cdb[1] == 8  # PIO data-in protocol=4
    assert cdb[2] == 0x0E  # data-in, blocks, transfer length from sector count
    assert cdb[6] == 1
    assert cdb[14] == ATA_IDENTIFY_DEVICE




def test_sat12_identify_cdb_is_read_only_pio_data_in():
    cdb = _ata_cdb12(command=ATA_IDENTIFY_DEVICE)
    assert len(cdb) == 12
    assert cdb[0] == ATA_PASS_THROUGH_12
    assert cdb[1] == 8
    assert cdb[2] == 0x0E
    assert cdb[4] == 1
    assert cdb[9] == ATA_IDENTIFY_DEVICE

def test_sat16_smart_read_data_registers():
    cdb = _ata_cdb16(
        command=ATA_SMART,
        feature=ATA_SMART_READ_DATA,
        lba_mid=ATA_SMART_CYL_LOW,
        lba_high=ATA_SMART_CYL_HIGH,
    )
    assert cdb[4] == ATA_SMART_READ_DATA
    assert cdb[10] == 0x4F
    assert cdb[12] == 0xC2
    assert cdb[14] == ATA_SMART


def test_parse_identify_device_extracts_identity_capacity_and_ssd_flag():
    raw = bytearray(512)
    _put_ata_string(raw, 10, 10, "WD-SERIAL-123")
    _put_ata_string(raw, 23, 4, "520201WD")
    _put_ata_string(raw, 27, 20, "WDC WDS200T3B0A-00AXR0")
    sectors = 3_907_029_168
    struct.pack_into("<HHHH", raw, 200, sectors & 0xFFFF, (sectors >> 16) & 0xFFFF, (sectors >> 32) & 0xFFFF, (sectors >> 48) & 0xFFFF)
    struct.pack_into("<H", raw, 217 * 2, 1)
    parsed = parse_identify_device(bytes(raw))
    assert parsed["model"] == "WDC WDS200T3B0A-00AXR0"
    assert parsed["serial"] == "WD-SERIAL-123"
    assert parsed["firmware_rev"] == "520201WD"
    assert parsed["capacity_bytes"] == sectors * 512
    assert parsed["solid_state"] is True


def _put_smart_attr(buf: bytearray, idx: int, attr_id: int, value: int, worst: int, raw_value: int, flags: int = 0x32):
    off = 2 + idx * 12
    buf[off] = attr_id
    struct.pack_into("<H", buf, off + 1, flags)
    buf[off + 3] = value
    buf[off + 4] = worst
    buf[off + 5:off + 11] = int(raw_value).to_bytes(6, "little")


def _put_threshold(buf: bytearray, idx: int, attr_id: int, threshold: int):
    off = 2 + idx * 12
    buf[off] = attr_id
    buf[off + 1] = threshold


def test_parse_direct_sat_smart_keeps_vendor_unknowns_and_thresholds():
    data = bytearray(512)
    thresh = bytearray(512)
    _put_smart_attr(data, 0, 5, 100, 100, 0)
    _put_smart_attr(data, 1, 194, 70, 65, 31)
    _put_smart_attr(data, 2, 170, 99, 99, 123)
    _put_threshold(thresh, 0, 5, 10)
    _put_threshold(thresh, 1, 194, 0)
    _put_threshold(thresh, 2, 170, 5)
    rows = parse_smart_attributes(bytes(data), bytes(thresh))
    by_id = {r["id"]: r for r in rows}
    assert by_id[5]["name"] == "Reallocated_Sector_Ct"
    assert by_id[5]["raw"]["value"] == 0
    assert by_id[5]["thresh"] == 10
    # Attribute IDs are vendor-defined. Do not invent Solidigm semantics for
    # an arbitrary USB-SATA drive merely because the numeric ID is 170.
    assert by_id[170]["name"] == "Vendor_Attribute_170"
    assert by_id[170]["raw"]["value"] == 123


def test_direct_sat_payload_does_not_fake_smart_overall_status_when_unavailable():
    identify = {
        "model": "WDC WDS200T3B0A-00AXR0",
        "serial": "WD-SERIAL-123",
        "firmware_rev": "520201WD",
        "capacity_bytes": 2_000_398_934_016,
        "solid_state": True,
        "rotation_rate": 1,
    }
    rows = [{
        "id": 5, "name": "Reallocated_Sector_Ct", "value": 100, "worst": 100,
        "thresh": 10, "when_failed": "", "flags": {"value": 0x32},
        "raw": {"value": 0, "string": "0"},
    }]
    payload = build_smartctl_like_payload(identify, rows, None)
    assert "smart_status" not in payload
    payload_ok = build_smartctl_like_payload(identify, rows, True)
    assert payload_ok["smart_status"]["passed"] is True


def test_parse_ata_return_descriptor_for_smart_return_status():
    sense = bytearray(32)
    sense[0] = 0x72
    sense[7] = 14
    sense[8] = 0x09
    sense[9] = 0x0C
    sense[17] = 0x4F  # descriptor +9 = LBA mid
    sense[19] = 0xC2  # descriptor +11 = LBA high
    sense[21] = 0x50  # ATA status
    regs = _ata_return_descriptor(bytes(sense))
    assert regs is not None
    assert regs["lba_mid"] == 0x4F
    assert regs["lba_high"] == 0xC2


def test_bot_reset_recovery_uses_standard_mass_storage_reset_and_clears_endpoints(monkeypatch):
    from src.macos_usb_sata import _bot_reset_recovery

    calls = []

    class Lib:
        def libusb_control_transfer(self, handle, req_type, request, value, index, data, length, timeout):
            calls.append(("control", req_type, request, value, index, length, timeout))
            return 0

        def libusb_clear_halt(self, handle, endpoint):
            calls.append(("clear", endpoint))
            return 0

    monkeypatch.setattr("src.macos_usb_sata.time.sleep", lambda _: None)
    bot = {"number": 3, "bulk_in": 0x81, "bulk_out": 0x02}
    _bot_reset_recovery(Lib(), object(), bot, timeout_ms=777)

    assert calls == [
        ("control", 0x21, 0xFF, 0, 3, 0, 777),
        ("clear", 0x81),
        ("clear", 0x02),
    ]

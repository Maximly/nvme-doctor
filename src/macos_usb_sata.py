# SPDX-License-Identifier: GPL-3.0-or-later
from __future__ import annotations

import ctypes
from ctypes import byref, c_void_p
import os
import struct
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from .macos_usb_nvme import (
    LIBUSB_ERROR_NOT_FOUND,
    LIBUSB_SUCCESS,
    USB_CLASS_MASS_STORAGE,
    USB_PROTOCOL_BOT,
    USB_PROTOCOL_UAS,
    DeviceDescriptor,
    _diskutil_info,
    _err,
    _external_usb_ssds,
    _find_bot,
    _get_string,
    _interfaces,
    _load_libusb,
    _normalize_disk,
    _progress,
    _run,
    _setup,
    _wait_for_disk,
    _bot_scsi_read,
    _bulk,
    BOT_CBW_SIGNATURE,
    BOT_CSW_SIGNATURE,
    USB_TIMEOUT_MS,
    mounted_volumes,
)

ProgressCallback = Optional[Callable[[str], None]]

ATA_PASS_THROUGH_16 = 0x85
ATA_PASS_THROUGH_12 = 0xA1
ATA_IDENTIFY_DEVICE = 0xEC
ATA_SMART = 0xB0
ATA_SMART_READ_DATA = 0xD0
ATA_SMART_READ_THRESHOLDS = 0xD1
ATA_SMART_RETURN_STATUS = 0xDA
ATA_SMART_CYL_LOW = 0x4F
ATA_SMART_CYL_HIGH = 0xC2
ATA_SECTOR_SIZE = 512

# Names are intentionally limited to attributes whose semantics nvme-doctor
# actually interprets. Unknown vendor attributes remain available by ID/raw.
SMART_NAMES = {
    5: "Reallocated_Sector_Ct",
    9: "Power_On_Hours",
    12: "Power_Cycle_Count",
    184: "End-to-End_Error",
    187: "Reported_Uncorrect",
    188: "Command_Timeout",
    190: "Airflow_Temperature_Cel",
    194: "Temperature_Celsius",
    197: "Current_Pending_Sector",
    198: "Offline_Uncorrectable",
    199: "UDMA_CRC_Error_Count",
    241: "Total_LBAs_Written",
    242: "Total_LBAs_Read",
}


def _enumerate_mass_storage(lib: ctypes.CDLL, ctx: c_void_p) -> List[Dict[str, Any]]:
    dev_pp = ctypes.POINTER(c_void_p)
    dev_list = dev_pp()
    count = lib.libusb_get_device_list(ctx, byref(dev_list))
    if count < 0:
        raise RuntimeError(f"libusb_get_device_list failed: {_err(int(count))}")
    out: List[Dict[str, Any]] = []
    try:
        for i in range(count):
            dev = dev_list[i]
            desc = DeviceDescriptor()
            if lib.libusb_get_device_descriptor(dev, byref(desc)) != 0:
                continue
            ifaces = _interfaces(lib, dev)
            bot = _find_bot(ifaces)
            if not bot:
                continue
            handle = c_void_p()
            open_rc = lib.libusb_open(dev, byref(handle))
            manufacturer = product = serial = None
            if open_rc == 0 and handle.value:
                manufacturer = _get_string(lib, handle, int(desc.iManufacturer))
                product = _get_string(lib, handle, int(desc.iProduct))
                serial = _get_string(lib, handle, int(desc.iSerialNumber))
            out.append({
                "handle": handle if handle.value else None,
                "open_rc": int(open_rc),
                "vid": int(desc.idVendor),
                "pid": int(desc.idProduct),
                "manufacturer": manufacturer,
                "product": product,
                "serial": serial,
                "bot": bot,
                "uas": any(x.get("protocol") == USB_PROTOCOL_UAS for x in ifaces),
            })
    finally:
        lib.libusb_free_device_list(dev_list, 1)
    return out




def _bot_reset_recovery(
    lib: ctypes.CDLL, handle: c_void_p, bot: Dict[str, Any], *, timeout_ms: int = 1000
) -> None:
    """Reset one USB Mass Storage BOT interface after capture or a failed command.

    UAS-capable bridges commonly expose a BOT alternate setting for compatibility.
    Switching alternate settings is not sufficient on every bridge: stale UAS/BOT
    command state or halted bulk endpoints can otherwise make the first CBW/data
    stage time out.  The reset is the standard USB Mass Storage Bulk-Only Reset
    request followed by clearing both bulk endpoint halts.
    """
    iface_num = int(bot["number"])
    rc = lib.libusb_control_transfer(
        handle, 0x21, 0xFF, 0, iface_num, None, 0, timeout_ms
    )
    if rc < 0:
        raise RuntimeError(f"BOT mass-storage reset failed: {_err(rc)}")
    for endpoint in (int(bot["bulk_in"]), int(bot["bulk_out"])):
        rc = lib.libusb_clear_halt(handle, endpoint)
        if rc not in (LIBUSB_SUCCESS, LIBUSB_ERROR_NOT_FOUND):
            raise RuntimeError(
                f"BOT clear-halt failed on endpoint 0x{endpoint:02x}: {_err(rc)}"
            )
    time.sleep(0.05)


def _scsi_inquiry(
    lib: ctypes.CDLL, handle: c_void_p, bot: Dict[str, Any], tag: int = 0x494e5101
) -> Dict[str, str]:
    """Verify the selected BOT transport with standard SCSI INQUIRY."""
    alloc = 96
    cdb = bytes([0x12, 0x00, 0x00, 0x00, alloc, 0x00])
    raw = _bot_scsi_read(lib, handle, bot, cdb, alloc, tag)
    if len(raw) < 36:
        raise RuntimeError(f"short SCSI INQUIRY response: {len(raw)} bytes")
    return {
        "vendor": raw[8:16].decode("ascii", "replace").strip(),
        "product": raw[16:32].decode("ascii", "replace").strip(),
        "revision": raw[32:36].decode("ascii", "replace").strip(),
    }


def _ata_cdb16(
    *,
    command: int,
    feature: int = 0,
    sector_count: int = 1,
    lba_low: int = 0,
    lba_mid: int = 0,
    lba_high: int = 0,
    device: int = 0,
    protocol: int = 4,
    data_in: bool = True,
) -> bytes:
    """Build SAT ATA PASS THROUGH(16) for read-only PIO/non-data commands."""
    cdb = bytearray(16)
    cdb[0] = ATA_PASS_THROUGH_16
    cdb[1] = (protocol & 0x0F) << 1
    if data_in:
        # T_DIR=1, BYT_BLOK=1, T_LENGTH=2 (sector-count field).
        cdb[2] = 0x0E
    cdb[4] = feature & 0xFF
    cdb[6] = sector_count & 0xFF
    cdb[8] = lba_low & 0xFF
    cdb[10] = lba_mid & 0xFF
    cdb[12] = lba_high & 0xFF
    cdb[13] = device & 0xFF
    cdb[14] = command & 0xFF
    return bytes(cdb)




def _ata_cdb12(
    *,
    command: int,
    feature: int = 0,
    sector_count: int = 1,
    lba_low: int = 0,
    lba_mid: int = 0,
    lba_high: int = 0,
    device: int = 0,
    protocol: int = 4,
    data_in: bool = True,
) -> bytes:
    """Build SAT ATA PASS THROUGH(12) for bridges that reject SAT16."""
    cdb = bytearray(12)
    cdb[0] = ATA_PASS_THROUGH_12
    cdb[1] = (protocol & 0x0F) << 1
    if data_in:
        cdb[2] = 0x0E
    cdb[3] = feature & 0xFF
    cdb[4] = sector_count & 0xFF
    cdb[5] = lba_low & 0xFF
    cdb[6] = lba_mid & 0xFF
    cdb[7] = lba_high & 0xFF
    cdb[8] = device & 0xFF
    cdb[9] = command & 0xFF
    return bytes(cdb)


def _ata_cdb(passthrough_len: int, **kwargs: Any) -> bytes:
    if passthrough_len == 12:
        return _ata_cdb12(**kwargs)
    if passthrough_len == 16:
        return _ata_cdb16(**kwargs)
    raise ValueError(f"unsupported SAT ATA PASS THROUGH length: {passthrough_len}")


def _bot_scsi_nodata_status(
    lib: ctypes.CDLL, handle: c_void_p, bot: Dict[str, Any], cdb: bytes, tag: int,
    timeout_ms: int = USB_TIMEOUT_MS,
) -> int:
    """Issue a BOT SCSI command with no data stage and return CSW status.

    Status 1 (command failed / CHECK CONDITION) is intentionally returned to
    the caller because SAT CK_COND uses CHECK CONDITION to deliver ATA output
    registers in sense data even when the ATA command itself succeeded.
    """
    cdb_padded = cdb + bytes(16 - len(cdb))
    cbw = struct.pack("<IIIBBB16s", BOT_CBW_SIGNATURE, tag, 0, 0x80, 0, len(cdb), cdb_padded)
    buf = bytearray(cbw)
    rc, n = _bulk(lib, handle, bot["bulk_out"], buf, timeout_ms)
    if rc != 0 or n != 31:
        raise RuntimeError(f"BOT CBW failed: {_err(rc)}, transferred {n}/31")
    csw = bytearray(13)
    rc, n_csw = _bulk(lib, handle, bot["bulk_in"], csw, timeout_ms)
    if rc != 0 or n_csw != 13:
        raise RuntimeError(f"BOT CSW failed: {_err(rc)}, transferred {n_csw}/13")
    sig, csw_tag, residue, status = struct.unpack("<IIIB", bytes(csw))
    if sig != BOT_CSW_SIGNATURE:
        raise RuntimeError(f"BOT command failed: invalid CSW signature 0x{sig:08x}")
    if csw_tag != tag:
        raise RuntimeError(f"BOT command failed: CSW tag 0x{csw_tag:08x} != CBW tag 0x{tag:08x}")
    if residue != 0:
        raise RuntimeError(f"BOT no-data command returned residue={residue}")
    if status not in (0, 1):
        raise RuntimeError(f"BOT command failed: status={status}")
    return int(status)


def _request_sense(lib: ctypes.CDLL, handle: c_void_p, bot: Dict[str, Any], tag: int) -> bytes:
    alloc = 252
    cdb = bytes([0x03, 0x00, 0x00, 0x00, alloc, 0x00])
    return _bot_scsi_read(lib, handle, bot, cdb, alloc, tag)


def _ata_return_descriptor(sense: bytes) -> Optional[Dict[str, int]]:
    """Parse SAT ATA Return Descriptor (descriptor code 0x09)."""
    if len(sense) < 8 or (sense[0] & 0x7F) not in (0x72, 0x73):
        return None
    end = min(len(sense), 8 + sense[7])
    off = 8
    while off + 2 <= end:
        code = sense[off]
        length = sense[off + 1]
        nxt = off + 2 + length
        if nxt > end:
            break
        if code == 0x09 and length >= 0x0C and off + 14 <= len(sense):
            return {
                "error": sense[off + 3],
                "sector_count": sense[off + 5],
                "lba_low": sense[off + 7],
                "lba_mid": sense[off + 9],
                "lba_high": sense[off + 11],
                "device": sense[off + 12],
                "status": sense[off + 13],
            }
        off = nxt
    return None


def _smart_return_status(lib: ctypes.CDLL, handle: c_void_p, bot: Dict[str, Any], passthrough_len: int) -> Optional[bool]:
    cdb = bytearray(_ata_cdb(passthrough_len,
        command=ATA_SMART,
        feature=ATA_SMART_RETURN_STATUS,
        sector_count=0,
        lba_mid=ATA_SMART_CYL_LOW,
        lba_high=ATA_SMART_CYL_HIGH,
        protocol=3,
        data_in=False,
    ))
    # CK_COND requests an ATA Return Descriptor in sense data.
    cdb[2] = 0x20
    status = _bot_scsi_nodata_status(lib, handle, bot, bytes(cdb), 0x41544104)
    if status == 0:
        # Some SATLs suppress CHECK CONDITION despite CK_COND. Without output
        # registers we cannot distinguish pass/fail reliably.
        return None
    sense = _request_sense(lib, handle, bot, 0x41544105)
    regs = _ata_return_descriptor(sense)
    if not regs:
        return None
    pair = (regs.get("lba_mid"), regs.get("lba_high"))
    if pair == (0x4F, 0xC2):
        return True
    if pair == (0xF4, 0x2C):
        return False
    return None


def _ata_read_sector(lib: ctypes.CDLL, handle: c_void_p, bot: Dict[str, Any], cdb: bytes, tag: int) -> bytes:
    raw = _bot_scsi_read(lib, handle, bot, cdb, ATA_SECTOR_SIZE, tag)
    if len(raw) != ATA_SECTOR_SIZE:
        raise RuntimeError(f"short SAT ATA response: {len(raw)} bytes")
    return raw


def _ata_string(raw: bytes, start_word: int, words: int) -> str:
    field = bytearray(raw[start_word * 2:(start_word + words) * 2])
    for i in range(0, len(field), 2):
        field[i:i + 2] = field[i:i + 2][::-1]
    return field.decode("ascii", "replace").replace("\x00", "").strip()


def parse_identify_device(raw: bytes) -> Dict[str, Any]:
    if len(raw) < ATA_SECTOR_SIZE:
        raise RuntimeError(f"short ATA IDENTIFY response: {len(raw)} bytes")
    words = struct.unpack("<256H", raw[:ATA_SECTOR_SIZE])
    lba48 = words[100] | (words[101] << 16) | (words[102] << 32) | (words[103] << 48)
    lba28 = words[60] | (words[61] << 16)
    sectors = lba48 or lba28
    logical_sector = 512
    # Word 106 bit 14=1 and bit 15=0 marks the sector-size words valid;
    # bit 12 means logical sector > 256 words, with size in words 117-118.
    if (words[106] & 0xC000) == 0x4000 and (words[106] & 0x1000):
        words_per_logical = words[117] | (words[118] << 16)
        if words_per_logical:
            logical_sector = words_per_logical * 2
    rotation = words[217]
    return {
        "model": _ata_string(raw, 27, 20),
        "serial": _ata_string(raw, 10, 10),
        "firmware_rev": _ata_string(raw, 23, 4),
        "lba_sectors": sectors,
        "logical_sector_size": logical_sector,
        "capacity_bytes": sectors * logical_sector if sectors else None,
        "rotation_rate": rotation,
        "solid_state": rotation == 1,
        "sata_capabilities_word": words[76],
    }


def _flags(value: int) -> Dict[str, Any]:
    return {
        "value": value,
        "prefailure": bool(value & 0x01),
        "updated_online": bool(value & 0x02),
        "performance": bool(value & 0x04),
        "error_rate": bool(value & 0x08),
        "event_count": bool(value & 0x10),
        "auto_keep": bool(value & 0x20),
    }


def parse_smart_attributes(data: bytes, thresholds: Optional[bytes] = None) -> List[Dict[str, Any]]:
    if len(data) < ATA_SECTOR_SIZE:
        raise RuntimeError(f"short ATA SMART READ DATA response: {len(data)} bytes")
    threshold_map: Dict[int, int] = {}
    if thresholds and len(thresholds) >= ATA_SECTOR_SIZE:
        for idx in range(30):
            off = 2 + idx * 12
            attr_id = thresholds[off]
            if attr_id:
                threshold_map[attr_id] = thresholds[off + 1]

    rows: List[Dict[str, Any]] = []
    for idx in range(30):
        off = 2 + idx * 12
        attr_id = data[off]
        if not attr_id:
            continue
        flags = struct.unpack_from("<H", data, off + 1)[0]
        value = data[off + 3]
        worst = data[off + 4]
        raw6 = data[off + 5:off + 11]
        raw_value = int.from_bytes(raw6, "little")
        thresh = threshold_map.get(attr_id, 0)
        failed = bool(thresh and value <= thresh)
        rows.append({
            "id": attr_id,
            "name": SMART_NAMES.get(attr_id, f"Vendor_Attribute_{attr_id}"),
            "value": value,
            "worst": worst,
            "thresh": thresh,
            "when_failed": "NOW" if failed else "",
            "flags": _flags(flags),
            "raw": {"value": raw_value, "string": str(raw_value)},
        })
    return rows


def _temperature_from_rows(rows: List[Dict[str, Any]]) -> Optional[int]:
    for attr_id in (194, 190):
        for row in rows:
            if row.get("id") != attr_id:
                continue
            raw = row.get("raw") if isinstance(row.get("raw"), dict) else {}
            value = raw.get("value")
            if isinstance(value, int):
                temp = value & 0xFF
                if 0 < temp < 100:
                    return temp
    return None


def _row_raw(rows: List[Dict[str, Any]], attr_id: int) -> Optional[int]:
    for row in rows:
        if row.get("id") == attr_id:
            raw = row.get("raw") if isinstance(row.get("raw"), dict) else {}
            value = raw.get("value")
            return value if isinstance(value, int) else None
    return None


def build_smartctl_like_payload(
    identify: Dict[str, Any], rows: List[Dict[str, Any]], smart_passed: Optional[bool] = None
) -> Dict[str, Any]:
    failed = [r for r in rows if r.get("when_failed")]
    payload: Dict[str, Any] = {
        "device": {"type": "sat-direct", "protocol": "ATA"},
        "model_name": identify.get("model"),
        "serial_number": identify.get("serial"),
        "firmware_version": identify.get("firmware_rev"),
        "user_capacity": {"bytes": identify.get("capacity_bytes")},
        "rotation_rate": 0 if identify.get("solid_state") else identify.get("rotation_rate"),
        "ata_smart_attributes": {"table": rows},
        "nvme_doctor_direct_sat": True,
    }
    if smart_passed is not None:
        payload["smart_status"] = {"passed": bool(smart_passed)}
    elif failed:
        # Threshold data is sufficient to prove a failing attribute even when
        # SMART RETURN STATUS output registers are unavailable.
        payload["smart_status"] = {"passed": False}

    temp = _temperature_from_rows(rows)
    if temp is not None:
        payload["temperature"] = {"current": temp}
    poh = _row_raw(rows, 9)
    if poh is not None:
        payload["power_on_time"] = {"hours": poh}
    cycles = _row_raw(rows, 12)
    if cycles is not None:
        payload["power_cycle_count"] = cycles
    return payload


def read_usb_sata_smart(
    device: str,
    *,
    mount_state_checked: bool = False,
    mounts_before: Optional[List[str]] = None,
    progress: ProgressCallback = None,
) -> Dict[str, Any]:
    """Temporarily capture a generic USB-SATA bridge and read ATA SMART via SAT.

    The ATA commands issued are read-only: IDENTIFY DEVICE, SMART READ DATA,
    SMART READ THRESHOLDS and SMART RETURN STATUS. Standard SAT16 is tried
    first, then SAT12 for bridge firmware that only accepts the shorter CDB.
    The whole disk is ejected cleanly before USB capture and its original mount
    state is restored afterwards. No USB-vendor-specific command is issued.
    """
    if os.geteuid() != 0:
        raise PermissionError("direct macOS USB-SATA access requires root (sudo)")
    disk = _normalize_disk(device)
    info = _diskutil_info(disk)
    bus = str(info.get("BusProtocol") or info.get("Protocol") or "").lower()
    if "usb" not in bus or info.get("Internal") is True:
        raise RuntimeError(f"/dev/{disk} is not an external USB disk")

    external_ssds = _external_usb_ssds()
    if disk not in external_ssds:
        raise RuntimeError(f"/dev/{disk} is not confirmed as an external physical USB SSD")
    if len(external_ssds) != 1:
        raise RuntimeError(
            f"{len(external_ssds)} external USB SSDs are connected ({', '.join(external_ssds)}); "
            "direct SAT mode refuses ambiguous disk-to-bridge mapping"
        )

    lib, libpath = _load_libusb()
    if not lib:
        raise RuntimeError("libusb is required for direct USB-SATA access (Homebrew: `brew install libusb`)")
    _setup(lib)
    ctx = c_void_p()
    rc = lib.libusb_init(byref(ctx))
    if rc != 0:
        raise RuntimeError(f"libusb_init failed: {_err(rc)}")

    if not mount_state_checked:
        mounts_before = mounted_volumes(disk)
    should_remount = mounts_before != []

    devices: List[Dict[str, Any]] = []
    handle = None
    claimed = detached = ejected = False
    iface_num = 0
    try:
        if mounts_before != []:
            _progress(progress, "Unmounting disk volumes cleanly")
            p = _run(["diskutil", "unmountDisk", f"/dev/{disk}"])
            if p.returncode != 0:
                raise RuntimeError(f"could not unmount /dev/{disk}: {p.stderr.decode('utf-8', 'replace').strip()}")

        _progress(progress, "Ejecting the whole disk cleanly")
        p = _run(["diskutil", "eject", f"/dev/{disk}"])
        if p.returncode != 0:
            raise RuntimeError(f"could not eject /dev/{disk}: {p.stderr.decode('utf-8', 'replace').strip()}")
        ejected = True

        _progress(progress, "Finding a BOT-capable USB mass-storage bridge")
        devices = _enumerate_mass_storage(lib, ctx)
        usable = [x for x in devices if x.get("open_rc") == 0 and x.get("handle")]
        if not usable:
            raise RuntimeError("no openable BOT-capable USB mass-storage bridge was found after eject")
        if len(usable) != 1:
            raise RuntimeError(
                f"{len(usable)} BOT-capable USB mass-storage devices are connected; "
                "direct SAT mode refuses ambiguous device selection"
            )
        target = usable[0]
        handle = target["handle"]
        bot = target["bot"]
        iface_num = int(bot["number"])

        _progress(progress, "Temporarily detaching the macOS USB storage driver")
        rc = lib.libusb_detach_kernel_driver(handle, iface_num)
        if rc not in (LIBUSB_SUCCESS, LIBUSB_ERROR_NOT_FOUND):
            raise RuntimeError(f"could not capture macOS USB storage driver: {_err(rc)}")
        detached = rc == LIBUSB_SUCCESS

        deadline = time.monotonic() + 0.35
        while True:
            rc = lib.libusb_claim_interface(handle, iface_num)
            if rc == 0:
                break
            if rc not in (-6, LIBUSB_ERROR_NOT_FOUND) or time.monotonic() >= deadline:
                raise RuntimeError(f"could not claim USB mass-storage interface: {_err(rc)}")
            time.sleep(0.02)
        claimed = True
        rc = lib.libusb_set_interface_alt_setting(handle, iface_num, int(bot["alt"]))
        if rc != 0:
            raise RuntimeError(f"could not switch USB mass-storage interface to BOT: {_err(rc)}")

        if target.get("uas"):
            _progress(progress, "Resetting UAS-capable bridge into BOT fallback mode")
        else:
            _progress(progress, "Resetting USB mass-storage BOT transport")
        _bot_reset_recovery(lib, handle, bot)

        _progress(progress, "Verifying BOT transport with SCSI INQUIRY")
        try:
            inquiry = _scsi_inquiry(lib, handle, bot)
        except Exception as exc:
            transport = "UAS-capable bridge BOT fallback" if target.get("uas") else "BOT transport"
            raise RuntimeError(f"{transport} is not responding after reset: {exc}") from exc

        identify_raw = None
        passthrough_len = None
        identify_errors: List[str] = []
        for cdb_len in (16, 12):
            try:
                _progress(progress, f"Reading ATA IDENTIFY through SAT{cdb_len}")
                identify_raw = _ata_read_sector(
                    lib, handle, bot,
                    _ata_cdb(cdb_len, command=ATA_IDENTIFY_DEVICE),
                    0x41544101 + (16 - cdb_len),
                )
                passthrough_len = cdb_len
                break
            except Exception as exc:
                identify_errors.append(f"SAT{cdb_len}: {exc}")
                # A failed BOT command may leave a pending CSW, halted endpoint,
                # or bridge command state behind. Recover before trying SAT12.
                try:
                    _bot_reset_recovery(lib, handle, bot)
                except Exception as recovery_exc:
                    identify_errors.append(f"BOT recovery: {recovery_exc}")
                    break
        if identify_raw is None or passthrough_len is None:
            raise RuntimeError("ATA IDENTIFY failed through standard SAT16 and SAT12: " + "; ".join(identify_errors))
        identify = parse_identify_device(identify_raw)

        _progress(progress, f"Reading ATA SMART attributes through SAT{passthrough_len}")
        smart_raw = _ata_read_sector(
            lib, handle, bot,
            _ata_cdb(
                passthrough_len,
                command=ATA_SMART,
                feature=ATA_SMART_READ_DATA,
                lba_mid=ATA_SMART_CYL_LOW,
                lba_high=ATA_SMART_CYL_HIGH,
            ),
            0x41544112,
        )

        thresholds_raw: Optional[bytes] = None
        try:
            _progress(progress, "Reading ATA SMART thresholds through SAT")
            thresholds_raw = _ata_read_sector(
                lib, handle, bot,
                _ata_cdb(
                    passthrough_len,
                    command=ATA_SMART,
                    feature=ATA_SMART_READ_THRESHOLDS,
                    lba_mid=ATA_SMART_CYL_LOW,
                    lba_high=ATA_SMART_CYL_HIGH,
                ),
                0x41544103,
            )
        except Exception:
            thresholds_raw = None

        rows = parse_smart_attributes(smart_raw, thresholds_raw)
        if not rows:
            raise RuntimeError("SAT SMART READ DATA returned no SMART attributes")
        smart_passed: Optional[bool] = None
        try:
            _progress(progress, "Reading ATA SMART overall-health status through SAT")
            smart_passed = _smart_return_status(lib, handle, bot, passthrough_len)
        except Exception:
            smart_passed = None
        payload = build_smartctl_like_payload(identify, rows, smart_passed)
        return {
            "bridge": {
                "vendor_id": target.get("vid"),
                "product_id": target.get("pid"),
                "manufacturer": target.get("manufacturer"),
                "product": target.get("product"),
                "serial": target.get("serial"),
                "uas_supported": bool(target.get("uas")),
                "active_transport": "BOT fallback" if target.get("uas") else "BOT",
                "access_mode": f"BOT + SAT ATA PASS THROUGH({passthrough_len})",
                "scsi_inquiry": inquiry,
                "sat_passthrough_len": passthrough_len,
                "libusb": libpath,
            },
            "identify": identify,
            "smartctl": payload,
            "thresholds_available": thresholds_raw is not None,
            "smart_return_status": smart_passed,
            "sat_passthrough_len": passthrough_len,
        }
    finally:
        if handle:
            if claimed:
                try:
                    lib.libusb_release_interface(handle, iface_num)
                except Exception:
                    pass
            if detached:
                try:
                    lib.libusb_attach_kernel_driver(handle, iface_num)
                except Exception:
                    pass
        seen = set()
        for item in devices:
            h = item.get("handle")
            if h and h.value and h.value not in seen:
                seen.add(h.value)
                try:
                    lib.libusb_close(h)
                except Exception:
                    pass
        try:
            lib.libusb_exit(ctx)
        except Exception:
            pass
        if ejected and should_remount:
            _progress(progress, "Waiting for macOS to re-detect the disk")
            if _wait_for_disk(disk, 8.0):
                _progress(progress, "Restoring the original mounted state")
                _run(["diskutil", "mountDisk", f"/dev/{disk}"])

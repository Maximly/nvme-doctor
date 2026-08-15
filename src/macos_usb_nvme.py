# SPDX-License-Identifier: GPL-3.0-or-later
from __future__ import annotations

import ctypes
import ctypes.util
from ctypes import POINTER, Structure, byref, c_int, c_ssize_t, c_uint8, c_uint16, c_uint32, c_void_p
import os
import plistlib
import re
import struct
import subprocess
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

USB_CLASS_MASS_STORAGE = 0x08
USB_PROTOCOL_BOT = 0x50
USB_PROTOCOL_UAS = 0x62

LIBUSB_SUCCESS = 0
LIBUSB_ERROR_NOT_FOUND = -5

BOT_CBW_SIGNATURE = 0x43425355
BOT_CSW_SIGNATURE = 0x53425355
USB_TIMEOUT_MS = 2000
USB_OPTIONAL_TIMEOUT_MS = 500

RTL9210_VID = 0x0BDA
RTL9210_PID = 0x9210
RTL9210_SCSI_OPCODE = 0xE4
NVME_ADMIN_GET_LOG_PAGE = 0x02
NVME_ADMIN_IDENTIFY = 0x06
NVME_IDENTIFY_CNS_CONTROLLER = 0x01
NVME_ERROR_LOG_ID = 0x01
NVME_SMART_LOG_ID = 0x02
NVME_FW_SLOT_LOG_ID = 0x03
NVME_IDENTIFY_LEN = 4096
NVME_SMART_LEN = 512
NVME_ERROR_LOG_LEN = 4096
NVME_FW_SLOT_LOG_LEN = 512



ProgressCallback = Optional[Callable[[str], None]]


def _progress(callback: ProgressCallback, message: str) -> None:
    if callback is None:
        return
    try:
        callback(message)
    except Exception:
        # Progress reporting must never affect diagnostics or device recovery.
        pass

ERR_NAMES = {
    0: "SUCCESS", -1: "IO", -2: "INVALID_PARAM", -3: "ACCESS", -4: "NO_DEVICE",
    -5: "NOT_FOUND", -6: "BUSY", -7: "TIMEOUT", -8: "OVERFLOW", -9: "PIPE",
    -10: "INTERRUPTED", -11: "NO_MEM", -12: "NOT_SUPPORTED",
}


class DeviceDescriptor(Structure):
    _fields_ = [
        ("bLength", c_uint8), ("bDescriptorType", c_uint8), ("bcdUSB", c_uint16),
        ("bDeviceClass", c_uint8), ("bDeviceSubClass", c_uint8), ("bDeviceProtocol", c_uint8),
        ("bMaxPacketSize0", c_uint8), ("idVendor", c_uint16), ("idProduct", c_uint16),
        ("bcdDevice", c_uint16), ("iManufacturer", c_uint8), ("iProduct", c_uint8),
        ("iSerialNumber", c_uint8), ("bNumConfigurations", c_uint8),
    ]


class EndpointDescriptor(Structure):
    _fields_ = [
        ("bLength", c_uint8), ("bDescriptorType", c_uint8), ("bEndpointAddress", c_uint8),
        ("bmAttributes", c_uint8), ("wMaxPacketSize", c_uint16), ("bInterval", c_uint8),
        ("bRefresh", c_uint8), ("bSynchAddress", c_uint8), ("extra", POINTER(c_uint8)),
        ("extra_length", c_int),
    ]


class InterfaceDescriptor(Structure):
    pass


class Interface(Structure):
    pass


InterfaceDescriptor._fields_ = [
    ("bLength", c_uint8), ("bDescriptorType", c_uint8), ("bInterfaceNumber", c_uint8),
    ("bAlternateSetting", c_uint8), ("bNumEndpoints", c_uint8), ("bInterfaceClass", c_uint8),
    ("bInterfaceSubClass", c_uint8), ("bInterfaceProtocol", c_uint8), ("iInterface", c_uint8),
    ("endpoint", POINTER(EndpointDescriptor)), ("extra", POINTER(c_uint8)), ("extra_length", c_int),
]
Interface._fields_ = [("altsetting", POINTER(InterfaceDescriptor)), ("num_altsetting", c_int)]


class ConfigDescriptor(Structure):
    _fields_ = [
        ("bLength", c_uint8), ("bDescriptorType", c_uint8), ("wTotalLength", c_uint16),
        ("bNumInterfaces", c_uint8), ("bConfigurationValue", c_uint8), ("iConfiguration", c_uint8),
        ("bmAttributes", c_uint8), ("MaxPower", c_uint8), ("interface", POINTER(Interface)),
        ("extra", POINTER(c_uint8)), ("extra_length", c_int),
    ]


def _run(argv: List[str]) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)


def _err(rc: int) -> str:
    return f"{ERR_NAMES.get(int(rc), 'UNKNOWN')} ({int(rc)})"


def _normalize_disk(value: str) -> str:
    value = value.strip().removeprefix("/dev/")
    if value.startswith("r") and re.fullmatch(r"rdisk\d+(?:s\d+)?", value):
        value = value[1:]
    m = re.fullmatch(r"(disk\d+)(?:s\d+)?", value)
    if not m:
        raise ValueError(f"expected diskN or /dev/diskN, got {value!r}")
    return m.group(1)


def _diskutil_info(disk: str) -> Dict[str, Any]:
    p = _run(["diskutil", "info", "-plist", f"/dev/{disk}"])
    if p.returncode != 0:
        raise RuntimeError(f"diskutil info failed: {p.stderr.decode('utf-8', 'replace').strip()}")
    value = plistlib.loads(p.stdout)
    return value if isinstance(value, dict) else {}




def _external_usb_ssds() -> List[str]:
    p = _run(["diskutil", "list", "-plist"])
    if p.returncode != 0:
        return []
    try:
        payload = plistlib.loads(p.stdout)
    except Exception:
        return []
    result: List[str] = []
    for item in payload.get("AllDisksAndPartitions", []) if isinstance(payload, dict) else []:
        if not isinstance(item, dict):
            continue
        ident = str(item.get("DeviceIdentifier") or "")
        if not re.fullmatch(r"disk\d+", ident):
            continue
        try:
            info = _diskutil_info(ident)
        except Exception:
            continue
        if str(info.get("VirtualOrPhysical") or "").lower() == "virtual":
            continue
        bus = str(info.get("BusProtocol") or info.get("Protocol") or "").lower()
        if "usb" not in bus:
            continue
        if info.get("Internal") is True:
            continue
        if info.get("SolidState") is not True:
            continue
        result.append(ident)
    return result

def _wait_for_disk(disk: str, timeout: float = 15.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _run(["diskutil", "info", f"/dev/{disk}"]).returncode == 0:
            return True
        time.sleep(0.5)
    return False


def _load_libusb() -> Tuple[Optional[ctypes.CDLL], Optional[str]]:
    candidates: List[str] = []
    found = ctypes.util.find_library("usb-1.0")
    if found:
        candidates.append(found)
    candidates += [
        "/opt/homebrew/lib/libusb-1.0.dylib", "/usr/local/lib/libusb-1.0.dylib",
        "/opt/local/lib/libusb-1.0.dylib",
    ]
    seen = set()
    for path in candidates:
        if not path or path in seen:
            continue
        seen.add(path)
        try:
            return ctypes.CDLL(path), path
        except OSError:
            pass
    return None, None


def libusb_available() -> bool:
    lib, _ = _load_libusb()
    return lib is not None


def mounted_volumes(device: str) -> Optional[List[str]]:
    """Return mount points backed by one whole external disk on macOS.

    An empty list means the disk is proven to have no mounted volumes. None
    means mount state could not be determined reliably. APFS volumes are
    synthesized devices, so correlate APFS container PhysicalStores back to
    the requested disk instead of trusting the whole-disk Mounted flag.
    """
    disk = _normalize_disk(device)
    points: List[str] = []

    p = _run(["diskutil", "list", "-plist", f"/dev/{disk}"])
    if p.returncode != 0:
        return None
    try:
        payload = plistlib.loads(p.stdout)
    except Exception:
        return None

    has_apfs_store = False

    def walk(value: Any) -> None:
        nonlocal has_apfs_store
        if isinstance(value, dict):
            ident = str(value.get("DeviceIdentifier") or "")
            content = str(value.get("Content") or value.get("PartitionType") or "")
            if ident.startswith(disk + "s") and "apfs" in content.lower():
                has_apfs_store = True
            mount = value.get("MountPoint")
            if ident.startswith(disk + "s") and mount:
                points.append(str(mount))
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(payload)

    if has_apfs_store:
        apfs = _run(["diskutil", "apfs", "list", "-plist"])
        if apfs.returncode != 0:
            return None
        try:
            apfs_payload = plistlib.loads(apfs.stdout)
        except Exception:
            return None
        if not isinstance(apfs_payload, dict):
            return None
        containers = apfs_payload.get("Containers")
        if containers is None:
            containers = apfs_payload.get("APFSContainers")
        if not isinstance(containers, list):
            return None

        matched_container = False
        for container in containers:
            if not isinstance(container, dict):
                continue
            stores = container.get("PhysicalStores") or []
            matches = False
            for store in stores if isinstance(stores, list) else []:
                if not isinstance(store, dict):
                    continue
                ident = str(store.get("DeviceIdentifier") or store.get("BSDName") or "")
                if ident.startswith(disk + "s"):
                    matches = True
                    matched_container = True
                    break
            if not matches:
                continue
            volumes = container.get("Volumes") or []
            for volume in volumes if isinstance(volumes, list) else []:
                if not isinstance(volume, dict):
                    continue
                mount = volume.get("MountPoint")
                if mount:
                    points.append(str(mount))
        if not matched_container:
            return None

    return list(dict.fromkeys(points))

def _setup(lib: ctypes.CDLL) -> None:
    dev_pp = POINTER(c_void_p)
    lib.libusb_init.argtypes = [POINTER(c_void_p)]; lib.libusb_init.restype = c_int
    lib.libusb_exit.argtypes = [c_void_p]
    lib.libusb_get_device_list.argtypes = [c_void_p, POINTER(dev_pp)]; lib.libusb_get_device_list.restype = c_ssize_t
    lib.libusb_free_device_list.argtypes = [dev_pp, c_int]
    lib.libusb_get_device_descriptor.argtypes = [c_void_p, POINTER(DeviceDescriptor)]; lib.libusb_get_device_descriptor.restype = c_int
    lib.libusb_get_active_config_descriptor.argtypes = [c_void_p, POINTER(POINTER(ConfigDescriptor))]; lib.libusb_get_active_config_descriptor.restype = c_int
    lib.libusb_free_config_descriptor.argtypes = [POINTER(ConfigDescriptor)]
    lib.libusb_open.argtypes = [c_void_p, POINTER(c_void_p)]; lib.libusb_open.restype = c_int
    lib.libusb_close.argtypes = [c_void_p]
    lib.libusb_get_string_descriptor_ascii.argtypes = [c_void_p, c_uint8, POINTER(c_uint8), c_int]; lib.libusb_get_string_descriptor_ascii.restype = c_int
    lib.libusb_kernel_driver_active.argtypes = [c_void_p, c_int]; lib.libusb_kernel_driver_active.restype = c_int
    lib.libusb_detach_kernel_driver.argtypes = [c_void_p, c_int]; lib.libusb_detach_kernel_driver.restype = c_int
    lib.libusb_attach_kernel_driver.argtypes = [c_void_p, c_int]; lib.libusb_attach_kernel_driver.restype = c_int
    lib.libusb_claim_interface.argtypes = [c_void_p, c_int]; lib.libusb_claim_interface.restype = c_int
    lib.libusb_release_interface.argtypes = [c_void_p, c_int]; lib.libusb_release_interface.restype = c_int
    lib.libusb_set_interface_alt_setting.argtypes = [c_void_p, c_int, c_int]; lib.libusb_set_interface_alt_setting.restype = c_int
    lib.libusb_bulk_transfer.argtypes = [c_void_p, c_uint8, POINTER(c_uint8), c_int, POINTER(c_int), c_uint32]; lib.libusb_bulk_transfer.restype = c_int


def _get_string(lib: ctypes.CDLL, handle: c_void_p, index: int) -> Optional[str]:
    if not handle or not index:
        return None
    buf = (c_uint8 * 256)()
    rc = lib.libusb_get_string_descriptor_ascii(handle, index, buf, len(buf))
    if rc < 0:
        return None
    return bytes(buf[:rc]).decode("utf-8", "replace").strip() or None


def _interfaces(lib: ctypes.CDLL, dev: c_void_p) -> List[Dict[str, Any]]:
    cfgp = POINTER(ConfigDescriptor)()
    rc = lib.libusb_get_active_config_descriptor(dev, byref(cfgp))
    if rc != 0 or not cfgp:
        return []
    out: List[Dict[str, Any]] = []
    try:
        cfg = cfgp.contents
        for ii in range(cfg.bNumInterfaces):
            iface = cfg.interface[ii]
            for aa in range(iface.num_altsetting):
                alt = iface.altsetting[aa]
                if alt.bInterfaceClass != USB_CLASS_MASS_STORAGE:
                    continue
                eps = []
                for e in range(alt.bNumEndpoints):
                    ep = alt.endpoint[e]
                    eps.append({"address": int(ep.bEndpointAddress), "attributes": int(ep.bmAttributes)})
                out.append({
                    "number": int(alt.bInterfaceNumber), "alt": int(alt.bAlternateSetting),
                    "protocol": int(alt.bInterfaceProtocol), "endpoints": eps,
                })
    finally:
        lib.libusb_free_config_descriptor(cfgp)
    return out


def _find_bot(ifaces: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    for iface in ifaces:
        if iface["protocol"] != USB_PROTOCOL_BOT:
            continue
        bulk_in = bulk_out = None
        for ep in iface["endpoints"]:
            if (ep["attributes"] & 0x03) != 0x02:
                continue
            if ep["address"] & 0x80:
                bulk_in = ep["address"]
            else:
                bulk_out = ep["address"]
        if bulk_in is not None and bulk_out is not None:
            return {**iface, "bulk_in": bulk_in, "bulk_out": bulk_out}
    return None


def _enumerate_rtl9210(lib: ctypes.CDLL, ctx: c_void_p) -> List[Dict[str, Any]]:
    dev_list = POINTER(c_void_p)()
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
            if int(desc.idVendor) != RTL9210_VID or int(desc.idProduct) != RTL9210_PID:
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
                "open_rc": int(open_rc), "manufacturer": manufacturer, "product": product,
                "serial": serial, "bot": bot,
                "uas": any(x.get("protocol") == USB_PROTOCOL_UAS for x in ifaces),
            })
    finally:
        lib.libusb_free_device_list(dev_list, 1)
    return out


def rtl9210_inventory(progress: ProgressCallback = None) -> List[Dict[str, Any]]:
    """Non-disruptive enumeration of connected RTL9210 mass-storage bridges."""
    lib, path = _load_libusb()
    if not lib:
        return []
    _setup(lib)
    ctx = c_void_p()
    if lib.libusb_init(byref(ctx)) != 0:
        return []
    rows: List[Dict[str, Any]] = []
    devices: List[Dict[str, Any]] = []
    try:
        _progress(progress, "Finding the RTL9210 USB bridge")
        devices = _enumerate_rtl9210(lib, ctx)
        for item in devices:
            rows.append({
                "vid": RTL9210_VID, "pid": RTL9210_PID, "manufacturer": item.get("manufacturer"),
                "product": item.get("product"), "serial": item.get("serial"), "bot": True,
                "uas": bool(item.get("uas")), "open": item.get("open_rc") == 0, "libusb": path,
            })
    finally:
        seen = set()
        for item in devices:
            h = item.get("handle")
            if h and h.value and h.value not in seen:
                seen.add(h.value)
                lib.libusb_close(h)
        lib.libusb_exit(ctx)
    return rows


def _bulk(lib: ctypes.CDLL, handle: c_void_p, endpoint: int, data: bytearray, timeout_ms: int) -> Tuple[int, int]:
    buf = (c_uint8 * len(data)).from_buffer(data)
    transferred = c_int(0)
    rc = lib.libusb_bulk_transfer(handle, endpoint, buf, len(data), byref(transferred), timeout_ms)
    return int(rc), int(transferred.value)


def _bot_scsi_read(lib: ctypes.CDLL, handle: c_void_p, bot: Dict[str, Any], cdb: bytes, data_len: int, tag: int, timeout_ms: int = USB_TIMEOUT_MS) -> bytes:
    cdb_padded = cdb + bytes(16 - len(cdb))
    cbw = struct.pack("<IIIBBB16s", BOT_CBW_SIGNATURE, tag, data_len, 0x80, 0, len(cdb), cdb_padded)
    buf = bytearray(cbw)
    rc, n = _bulk(lib, handle, bot["bulk_out"], buf, timeout_ms)
    if rc != 0 or n != 31:
        raise RuntimeError(f"BOT CBW failed: {_err(rc)}, transferred {n}/31")
    data = bytearray(data_len)
    rc, n_data = _bulk(lib, handle, bot["bulk_in"], data, timeout_ms)
    if rc != 0:
        raise RuntimeError(f"BOT data read failed: {_err(rc)}, transferred {n_data}/{data_len}")
    csw = bytearray(13)
    rc, n_csw = _bulk(lib, handle, bot["bulk_in"], csw, timeout_ms)
    if rc != 0 or n_csw != 13:
        raise RuntimeError(f"BOT CSW failed: {_err(rc)}, transferred {n_csw}/13")
    sig, csw_tag, residue, status = struct.unpack("<IIIB", bytes(csw))
    if sig != BOT_CSW_SIGNATURE:
        raise RuntimeError(f"BOT command failed: invalid CSW signature 0x{sig:08x}")
    if csw_tag != tag:
        raise RuntimeError(f"BOT command failed: CSW tag 0x{csw_tag:08x} != CBW tag 0x{tag:08x}")
    if status != 0:
        raise RuntimeError(f"BOT command failed: status={status}, residue={residue}")
    expected_residue = max(0, data_len - n_data)
    if residue != expected_residue:
        raise RuntimeError(
            f"BOT transfer accounting mismatch: data={n_data}/{data_len}, residue={residue}, "
            f"expected residue={expected_residue}"
        )
    return bytes(data[:n_data])


def _rtl_read(lib: ctypes.CDLL, handle: c_void_p, bot: Dict[str, Any], opcode: int, cdw10_low: int, size: int, tag: int, timeout_ms: int = USB_TIMEOUT_MS) -> bytes:
    cdb = bytearray(16)
    cdb[0] = RTL9210_SCSI_OPCODE
    struct.pack_into("<H", cdb, 1, size)
    cdb[3] = opcode
    cdb[4] = cdw10_low
    return _bot_scsi_read(lib, handle, bot, bytes(cdb), size, tag, timeout_ms=timeout_ms)


def _ascii(raw: bytes, start: int, length: int) -> str:
    return raw[start:start + length].decode("ascii", "replace").replace("\x00", "").strip()


def _version(value: int) -> str:
    major = (value >> 16) & 0xFFFF
    minor = (value >> 8) & 0xFF
    tertiary = value & 0xFF
    return f"{major}.{minor}" + (f".{tertiary}" if tertiary else "")


def parse_identify_controller(raw: bytes) -> Dict[str, Any]:
    if len(raw) < 522:
        raise RuntimeError(f"short NVMe Identify Controller response: {len(raw)} bytes")
    return {
        "vid": struct.unpack_from("<H", raw, 0)[0],
        "ssvid": struct.unpack_from("<H", raw, 2)[0],
        "serial": _ascii(raw, 4, 20), "model": _ascii(raw, 24, 40), "firmware_rev": _ascii(raw, 64, 8),
        "ieee_oui": ":".join(f"{b:02x}" for b in reversed(raw[73:76])), "mdts": raw[77],
        "controller_id": struct.unpack_from("<H", raw, 78)[0],
        "nvme_version_raw": struct.unpack_from("<I", raw, 80)[0],
        "nvme_version": _version(struct.unpack_from("<I", raw, 80)[0]),
        "oacs": struct.unpack_from("<H", raw, 256)[0],
        "tnvmcap": int.from_bytes(raw[280:296], "little"),
        "unvmcap": int.from_bytes(raw[296:312], "little"),
        "oncs": struct.unpack_from("<H", raw, 520)[0],
    }


def _u128(raw: bytes, offset: int) -> int:
    return int.from_bytes(raw[offset:offset + 16], "little")


def parse_smart_log(raw: bytes) -> Dict[str, Any]:
    if len(raw) < 512:
        raise RuntimeError(f"short NVMe SMART response: {len(raw)} bytes")
    temp_k = struct.unpack_from("<H", raw, 1)[0]
    smart: Dict[str, Any] = {
        "critical_warning": raw[0], "temperature": temp_k - 273 if temp_k else None,
        "avail_spare": raw[3], "spare_thresh": raw[4], "percent_used": raw[5],
        "data_units_read": _u128(raw, 32), "data_units_written": _u128(raw, 48),
        "host_read_commands": _u128(raw, 64), "host_write_commands": _u128(raw, 80),
        "controller_busy_time": _u128(raw, 96), "power_cycles": _u128(raw, 112),
        "power_on_hours": _u128(raw, 128), "unsafe_shutdowns": _u128(raw, 144),
        "media_errors": _u128(raw, 160), "num_err_log_entries": _u128(raw, 176),
        "warning_temp_time": struct.unpack_from("<I", raw, 192)[0],
        "critical_comp_time": struct.unpack_from("<I", raw, 196)[0],
        "thm_temp1_trans_count": struct.unpack_from("<I", raw, 216)[0],
        "thm_temp2_trans_count": struct.unpack_from("<I", raw, 220)[0],
        # Render expects minutes. NVMe thermal management total time is seconds.
        "thm_temp1_total_time": struct.unpack_from("<I", raw, 224)[0] // 60,
        "thm_temp2_total_time": struct.unpack_from("<I", raw, 228)[0] // 60,
    }
    sensors = []
    for i in range(8):
        k = struct.unpack_from("<H", raw, 200 + i * 2)[0]
        if k:
            sensors.append(k - 273)
    if sensors:
        smart["temperature_sensors"] = sensors
    return smart



def parse_error_log(raw: bytes) -> List[Dict[str, Any]]:
    """Parse NVMe Error Information Log entries (64 bytes each)."""
    out: List[Dict[str, Any]] = []
    for off in range(0, len(raw) - 63, 64):
        error_count = struct.unpack_from("<Q", raw, off)[0]
        if error_count == 0:
            continue
        entry = {
            "error_count": error_count,
            "sqid": struct.unpack_from("<H", raw, off + 8)[0],
            "cmdid": struct.unpack_from("<H", raw, off + 10)[0],
            "status_field": struct.unpack_from("<H", raw, off + 12)[0],
            "parm_error_location": struct.unpack_from("<H", raw, off + 14)[0],
            "lba": struct.unpack_from("<Q", raw, off + 16)[0],
            "nsid": struct.unpack_from("<I", raw, off + 24)[0],
            "vs": raw[off + 28],
            "trtype": raw[off + 29],
        }
        out.append(entry)
    return out


def parse_firmware_slot_log(raw: bytes) -> Dict[str, Any]:
    if len(raw) < 64:
        raise RuntimeError(f"short NVMe Firmware Slot Information response: {len(raw)} bytes")
    afi = raw[0]
    slots = []
    for slot in range(1, 8):
        off = 8 + (slot - 1) * 8
        rev = _ascii(raw, off, 8)
        if rev:
            slots.append({"slot": slot, "firmware_rev": rev})
    return {
        "active_slot": afi & 0x07,
        "next_active_slot": (afi >> 4) & 0x07,
        "slots": slots,
    }


def read_rtl9210_nvme(
    device: str,
    *,
    mount_state_checked: bool = False,
    mounts_before: Optional[List[str]] = None,
    collect_optional_logs: bool = False,
    progress: ProgressCallback = None,
) -> Dict[str, Any]:
    """Temporarily capture one RTL9210 and return read-only NVMe identity/SMART.

    Fast path rules:
    - when the caller already checked mount state, reuse it instead of invoking
      diskutil list/APFS list again;
    - always issue a whole-disk `diskutil eject` before detaching the macOS USB
      storage driver.  A plain unmount is not sufficient: detaching the driver
      while the media is still logically present makes macOS report “Disk Not
      Ejected Properly”;
    - Identify + SMART are the normal health check. Error/Firmware Slot logs are
      optional because some RTL9210 firmware revisions stall before rejecting them.

    If more than one RTL9210 is connected, refuse rather than risk selecting the
    wrong enclosure because diskutil does not expose a stable VID:PID mapping.
    """
    if os.geteuid() != 0:
        raise PermissionError("direct macOS USB NVMe access requires root (sudo)")
    disk = _normalize_disk(device)
    _progress(progress, f"Validating /dev/{disk} with diskutil")
    info = _diskutil_info(disk)  # prove target exists before capture
    bus = str(info.get("BusProtocol") or info.get("Protocol") or "").lower()
    if "usb" not in bus or info.get("Internal") is True:
        raise RuntimeError(f"/dev/{disk} is not an external USB disk")
    _progress(progress, "Verifying external USB SSD mapping")
    external_ssds = _external_usb_ssds()
    if disk not in external_ssds:
        raise RuntimeError(f"/dev/{disk} is not confirmed as an external physical USB SSD")
    if len(external_ssds) != 1:
        raise RuntimeError(
            f"{len(external_ssds)} external USB SSDs are connected ({', '.join(external_ssds)}); "
            "direct mode refuses ambiguous disk-to-bridge mapping"
        )
    _progress(progress, "Loading libusb")
    lib, libpath = _load_libusb()
    if not lib:
        raise RuntimeError("libusb is required for direct RTL9210 access (Homebrew: `brew install libusb`)")
    _setup(lib)
    ctx = c_void_p()
    _progress(progress, "Initializing USB access")
    rc = lib.libusb_init(byref(ctx))
    if rc != 0:
        raise RuntimeError(f"libusb_init failed: {_err(rc)}")

    if not mount_state_checked:
        _progress(progress, "Checking current mount state")
        mounts_before = mounted_volumes(disk)
    else:
        _progress(progress, "Using cached mount state")
    # If mount state is known-empty, preserve that state. If it is unknown,
    # retain the historical safe behavior of remounting after a successful unmount.
    should_remount = mounts_before != []

    devices: List[Dict[str, Any]] = []
    handle = None
    claimed = detached = unmounted = ejected = False
    iface_num = 0
    try:
        # Filesystems must be quiesced first when anything is mounted.  Eject
        # the disk before opening the bridge with libusb: a handle opened before
        # diskutil eject can become stale when macOS tears down the storage
        # service, causing long timeouts and an INCOMPLETE result.
        if mounts_before != []:
            _progress(progress, "Unmounting disk volumes cleanly")
            p = _run(["diskutil", "unmountDisk", f"/dev/{disk}"])
            if p.returncode != 0:
                raise RuntimeError(f"could not unmount /dev/{disk}: {p.stderr.decode('utf-8', 'replace').strip()}")
            unmounted = True

        _progress(progress, "Ejecting the whole disk cleanly")
        p = _run(["diskutil", "eject", f"/dev/{disk}"])
        if p.returncode != 0:
            raise RuntimeError(f"could not eject /dev/{disk}: {p.stderr.decode('utf-8', 'replace').strip()}")
        ejected = True

        # Enumerate/open only after eject so the handle belongs to the current
        # post-eject USB device state rather than a storage-stack incarnation
        # that diskutil just invalidated.
        _progress(progress, "Opening the RTL9210 after eject")
        devices = _enumerate_rtl9210(lib, ctx)
        usable = [x for x in devices if x.get("open_rc") == 0 and x.get("handle")]
        if not usable:
            raise RuntimeError("no openable Realtek RTL9210 USB-NVMe bridge was found after eject")
        if len(usable) != 1:
            raise RuntimeError(f"{len(usable)} RTL9210 bridges are connected; direct mode refuses ambiguous device selection")
        target = usable[0]
        handle = target["handle"]
        bot = target["bot"]
        iface_num = int(bot["number"])

        _progress(progress, "Temporarily detaching the macOS USB storage driver")
        rc = lib.libusb_detach_kernel_driver(handle, iface_num)
        if rc not in (LIBUSB_SUCCESS, LIBUSB_ERROR_NOT_FOUND):
            raise RuntimeError(f"could not capture macOS USB storage driver: {_err(rc)}")
        detached = rc == LIBUSB_SUCCESS

        _progress(progress, "Claiming the RTL9210 BOT interface")
        deadline = time.monotonic() + 0.35
        while True:
            rc = lib.libusb_claim_interface(handle, iface_num)
            if rc == 0:
                break
            if rc not in (-6, LIBUSB_ERROR_NOT_FOUND) or time.monotonic() >= deadline:
                raise RuntimeError(f"could not claim RTL9210 interface: {_err(rc)}")
            time.sleep(0.02)
        claimed = True
        rc = lib.libusb_set_interface_alt_setting(handle, iface_num, int(bot["alt"]))
        if rc != 0:
            raise RuntimeError(f"could not switch RTL9210 to BOT: {_err(rc)}")

        _progress(progress, "Reading NVMe Identify Controller")
        identify_raw = _rtl_read(lib, handle, bot, NVME_ADMIN_IDENTIFY, NVME_IDENTIFY_CNS_CONTROLLER, NVME_IDENTIFY_LEN, 0x4E560001)
        _progress(progress, "Reading NVMe SMART / Health log")
        smart_raw = _rtl_read(lib, handle, bot, NVME_ADMIN_GET_LOG_PAGE, NVME_SMART_LOG_ID, NVME_SMART_LEN, 0x4E560002)

        result: Dict[str, Any] = {
            "bridge": {
                "vendor_id": RTL9210_VID, "product_id": RTL9210_PID,
                "manufacturer": target.get("manufacturer"), "product": target.get("product"),
                "serial": target.get("serial"), "uas_supported": bool(target.get("uas")),
                "access_mode": "BOT alt setting", "libusb": libpath,
            },
            "identify": parse_identify_controller(identify_raw),
            "smart": parse_smart_log(smart_raw),
            "optional_log_errors": [],
        }
        if collect_optional_logs:
            try:
                _progress(progress, "Reading optional NVMe Error Information log")
                error_raw = _rtl_read(
                    lib, handle, bot, NVME_ADMIN_GET_LOG_PAGE, NVME_ERROR_LOG_ID,
                    NVME_ERROR_LOG_LEN, 0x4E560003, timeout_ms=USB_OPTIONAL_TIMEOUT_MS,
                )
                result["error_log"] = parse_error_log(error_raw)
            except Exception as exc:
                result["optional_log_errors"].append(f"error-information log: {exc}")
            try:
                _progress(progress, "Reading optional NVMe Firmware Slot log")
                fw_raw = _rtl_read(
                    lib, handle, bot, NVME_ADMIN_GET_LOG_PAGE, NVME_FW_SLOT_LOG_ID,
                    NVME_FW_SLOT_LOG_LEN, 0x4E560004, timeout_ms=USB_OPTIONAL_TIMEOUT_MS,
                )
                result["firmware_slot_log"] = parse_firmware_slot_log(fw_raw)
            except Exception as exc:
                result["optional_log_errors"].append(f"firmware-slot log: {exc}")
        return result
    finally:
        if handle:
            if claimed:
                _progress(progress, "Releasing the RTL9210 interface")
                try:
                    lib.libusb_release_interface(handle, iface_num)
                except Exception:
                    pass
            if detached:
                _progress(progress, "Returning the USB storage driver to macOS")
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
            else:
                _progress(progress, "Disk did not reappear before the restore timeout")
        _progress(progress, "Direct USB NVMe read complete")

from pathlib import Path

from src.collect import collect_topology, collect_topology_linux
from src.model import CommandResult
from src.render import render_topology_text


class LspciRunner:
    def run(self, argv, timeout=8.0):
        if argv[0] == "lspci":
            bdf = argv[-1]
            names = {
                "0000:00:01.0": "PCI bridge: Example Root Port",
                "0000:70:00.0": "PCI bridge: Example PCIe Switch Downstream Port",
                "0000:71:00.0": "Non-Volatile memory controller: Samsung Electronics NVMe SSD Controller",
            }
            return CommandResult(list(argv), 0, stdout=f"{bdf} {names.get(bdf, 'PCI device')}\n")
        return CommandResult(list(argv), 127, available=False)


def _write(path: Path, value: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value)


def _make_pci_node(path: Path, *, cls: str, speed: str, width: str, numa="0", cpus="0-31"):
    path.mkdir(parents=True, exist_ok=True)
    _write(path / "class", cls)
    _write(path / "vendor", "0x1234")
    _write(path / "device", "0x5678")
    _write(path / "current_link_speed", speed)
    _write(path / "current_link_width", width)
    _write(path / "max_link_speed", speed)
    _write(path / "max_link_width", width)
    _write(path / "numa_node", numa)
    _write(path / "local_cpulist", cpus)
    _write(path / "power_state", "D0")


def test_topology_walks_numa_pci_path_and_namespace(tmp_path):
    devices = tmp_path / "devices" / "pci0000:00"
    root = devices / "0000:00:01.0"
    switch = root / "0000:70:00.0"
    endpoint = switch / "0000:71:00.0"
    _make_pci_node(root, cls="0x060400", speed="32.0 GT/s PCIe", width="16")
    _make_pci_node(switch, cls="0x060400", speed="32.0 GT/s PCIe", width="8")
    _make_pci_node(endpoint, cls="0x010802", speed="32.0 GT/s PCIe", width="4")

    sys_pci = tmp_path / "sys_pci"
    sys_pci.mkdir()
    for bdf, target in [("0000:00:01.0", root), ("0000:70:00.0", switch), ("0000:71:00.0", endpoint)]:
        (sys_pci / bdf).symlink_to(target, target_is_directory=True)

    sys_nvme = tmp_path / "sys_nvme"
    ctrl = sys_nvme / "nvme0"
    ctrl.mkdir(parents=True)
    (ctrl / "device").symlink_to(endpoint, target_is_directory=True)
    _write(ctrl / "model", "Samsung SSD 9100 PRO 2TB")
    _write(ctrl / "serial", "SER123")
    _write(ctrl / "firmware_rev", "FW1")
    _write(ctrl / "state", "live")
    ns = ctrl / "nvme0n1"
    ns.mkdir()
    _write(ns / "nsid", "1")

    sys_block = tmp_path / "sys_block"
    block = sys_block / "nvme0n1"
    block.mkdir(parents=True)
    _write(block / "size", "3907029168")
    _write(block / "queue" / "logical_block_size", "512")

    topo = collect_topology_linux(
        "nvme0", runner=LspciRunner(), sys_class_nvme=sys_nvme,
        sys_pci=sys_pci, sys_class_block=sys_block,
    )
    assert topo["complete"] is True
    assert topo["numa_node"] == "0"
    assert topo["local_cpulist"] == "0-31"
    assert [n["bdf"] for n in topo["pci_path"]] == [
        "0000:00:01.0", "0000:70:00.0", "0000:71:00.0"
    ]
    assert topo["pci_path"][-1]["role"] == "NVMe controller"
    assert topo["namespaces"][0]["device"] == "/dev/nvme0n1"
    assert topo["namespaces"][0]["capacity_bytes"] == 3907029168 * 512

    text = render_topology_text(topo)
    assert "NUMA node 0" in text
    assert "PCI domain 0000" in text
    assert "0000:00:01.0" in text
    assert "Example PCIe Switch Downstream Port" in text
    assert "0000:71:00.0  NVMe controller" in text
    assert "Gen5 x4" in text
    assert "/dev/nvme0n1  [NSID 1, 2.00 TB, LBA 512 B]" in text


def test_topology_link_can_show_negotiated_below_max():
    topo = {
        "platform": "linux",
        "controller": "nvme0",
        "controller_info": {"model": "Test"},
        "numa_node": "1",
        "local_cpulist": "32-63",
        "pci_domain": "0000",
        "pci_path": [{
            "bdf": "0000:71:00.0", "role": "NVMe controller",
            "current_link_speed": "16.0 GT/s PCIe", "current_link_width": "4",
            "max_link_speed": "32.0 GT/s PCIe", "max_link_width": "4",
        }],
        "pci_endpoint": {
            "bdf": "0000:71:00.0",
            "current_link_speed": "16.0 GT/s PCIe", "current_link_width": "4",
            "max_link_speed": "32.0 GT/s PCIe", "max_link_width": "4",
        },
        "namespaces": [],
        "complete": True,
        "notes": [],
    }
    text = render_topology_text(topo)
    assert "Gen4 x4, max Gen5 x4" in text


class SmartctlUsbRunner:
    def run(self, argv, timeout=8.0):
        import json
        if argv[:3] == ["smartctl", "-i", "-j"]:
            payload = {
                "device": {"name": "/dev/sdf", "protocol": "NVMe", "type": "sntrealtek"},
                "model_name": "Samsung SSD 990 EVO Plus 2TB",
                "serial_number": "SERUSB1",
                "firmware_version": "1B2QKXG7",
                "nvme_version": {"string": "2.0", "value": 131072},
            }
            return CommandResult(list(argv), 0, stdout=json.dumps(payload))
        return CommandResult(list(argv), 127, available=False)


def test_usb_translated_topology_separates_bridge_and_nvme_identity(tmp_path):
    sys_block = tmp_path / "sys_block"
    block = sys_block / "sdf"
    dev = tmp_path / "devices" / "pci0000:00" / "usb1" / "1-1" / "host0" / "target0:0:0" / "0:0:0:0"
    dev.mkdir(parents=True)
    block.mkdir(parents=True)
    (block / "device").symlink_to(dev, target_is_directory=True)
    _write(block / "queue" / "rotational", "0")
    _write(block / "device" / "vendor", "ACASIS")
    _write(block / "device" / "model", "EC-6608Air")
    _write(block / "size", "3907029168")
    _write(block / "queue" / "logical_block_size", "512")

    topo = collect_topology_linux("sdf", runner=SmartctlUsbRunner(), sys_class_block=sys_block)
    assert topo["controller_info"]["model"] == "Samsung SSD 990 EVO Plus 2TB"
    assert topo["controller_info"]["usb_bridge_model"] == "ACASIS EC-6608Air"
    text = render_topology_text(topo)
    assert "Bridge               ACASIS EC-6608Air" in text
    assert "NVMe SSD — Samsung SSD 990 EVO Plus 2TB" in text
    assert "/dev/sdf  [2.00 TB, LBA 512 B]" in text
    assert "NUMA locality unknown" not in text
    assert "native NVMe PCIe endpoint" in text


def test_macos_usb_topology_render_does_not_call_bridge_nvme_endpoint():
    topo = {
        "platform": "darwin",
        "controller": "disk4",
        "controller_device": "/dev/disk4",
        "controller_info": {
            "model": "Samsung SSD 990 EVO Plus 2TB",
            "serial": "SERUSB1",
            "firmware_rev": "1B2QKXG7",
            "transport": "USB -> NVMe",
            "native_nvme": False,
            "usb_bridge": "Realtek RTL9210",
            "usb_bridge_model": "EC-6608Air",
        },
        "namespaces": [{"device": "/dev/disk4", "capacity_bytes": 2_000_398_934_016}],
        "pci_endpoint": {}, "pci_path": [], "notes": [], "complete": False,
    }
    text = render_topology_text(topo)
    assert "Bridge               EC-6608Air — Realtek RTL9210" in text
    assert "NVMe SSD — Samsung SSD 990 EVO Plus 2TB" in text
    assert "macOS NVMe endpoint" not in text



def test_macos_collect_topology_uses_snapshot_device_path(monkeypatch):
    from src.model import Snapshot
    import src.collect_macos as collect_macos

    snap = Snapshot("disk4", "disk4", "/dev/disk4")
    snap.controller_info = {
        "model": "Samsung SSD 990 EVO Plus 2TB",
        "serial": "SERUSB1",
        "firmware_rev": "1B2QKXG7",
        "transport": "USB -> NVMe",
        "direct_usb": True,
        "nvme_passthrough": True,
        "usb_bridge": "Realtek RTL9210",
        "usb_bridge_model": "EC-6608Air",
        "tnvmcap": 2_000_398_934_016,
    }

    monkeypatch.setattr(
        collect_macos,
        "collect_snapshot_macos",
        lambda requested_device, runner=None, kernel_lines=0, direct_usb=False: snap,
    )

    topo = collect_topology("disk4", platform_name="darwin")
    assert topo["controller_device"] == "/dev/disk4"
    assert topo["namespaces"][0]["device"] == "/dev/disk4"
    assert topo["controller_info"]["model"] == "Samsung SSD 990 EVO Plus 2TB"

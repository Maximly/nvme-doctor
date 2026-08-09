from pathlib import Path

from src.collect import collect_topology_linux
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

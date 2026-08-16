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

class NativeSataTopologyRunner:
    def run(self, argv, timeout=8.0):
        if argv[:2] in (["smartctl", "--scan-open"], ["smartctl", "--scan"]):
            return CommandResult(list(argv), 0, stdout="")
        if argv and argv[0] == "smartctl":
            # Reproduce a non-root/ambiguous identity probe: topology must still
            # use authoritative Linux libata + udev/sysfs evidence and must not
            # relabel the drive as USB/NVMe.
            return CommandResult(list(argv), 2, stdout="{}\n")
        if argv[:2] == ["udevadm", "info"]:
            return CommandResult(
                list(argv), 0,
                stdout=(
                    "ID_BUS=ata\n"
                    "ID_MODEL=SOLIDIGM_SSDSC2KB019TZ\n"
                    "ID_SERIAL_SHORT=BTYI549203JJ1P9DGN\n"
                    "ID_REVISION=7CV10130\n"
                ),
            )
        if argv and argv[0] == "lspci":
            bdf = argv[-1]
            desc = {
                "0000:00:03.1": "PCI bridge: Example Root Port",
                "0000:41:00.0": "SATA controller: Example AHCI Controller",
            }.get(bdf, "PCI device")
            return CommandResult(list(argv), 0, stdout=f"{bdf} {desc}\n")
        return CommandResult(list(argv), 127, available=False)


def test_native_sata_topology_is_not_rendered_as_usb_nvme(tmp_path):
    devices = tmp_path / "devices" / "pci0000:00"
    root = devices / "0000:00:03.1"
    sata = root / "0000:41:00.0"
    ata_leaf = sata / "ata3" / "host3" / "target3:0:0" / "3:0:0:0"
    ata_leaf.mkdir(parents=True)
    _make_pci_node(root, cls="0x060400", speed="16.0 GT/s PCIe", width="4")
    _make_pci_node(sata, cls="0x010601", speed="8.0 GT/s PCIe", width="1")

    sys_pci = tmp_path / "sys_pci"
    sys_pci.mkdir()
    (sys_pci / "0000:00:03.1").symlink_to(root, target_is_directory=True)
    (sys_pci / "0000:41:00.0").symlink_to(sata, target_is_directory=True)

    sys_block = tmp_path / "sys_block"
    block = sys_block / "sda"
    block.mkdir(parents=True)
    (block / "device").symlink_to(ata_leaf, target_is_directory=True)
    _write(block / "device" / "type", "0")
    _write(block / "device" / "model", "SSDSC2KB019TZ")
    _write(block / "device" / "vendor", "SOLIDIGM")
    _write(block / "device" / "rev", "7CV10130")
    _write(block / "device" / "state", "running")
    _write(block / "queue" / "rotational", "0")
    _write(block / "size", "3750748848")
    _write(block / "queue" / "logical_block_size", "512")

    topo = collect_topology_linux(
        "sda",
        runner=NativeSataTopologyRunner(),
        sys_pci=sys_pci,
        sys_class_block=sys_block,
    )
    ci = topo["controller_info"]
    assert ci["protocol"] == "ATA"
    assert ci["transport"] == "SATA"
    assert ci["model"] == "SOLIDIGM SSDSC2KB019TZ"
    assert topo["pci_endpoint"]["bdf"] == "0000:41:00.0"
    assert topo["pci_path"][-1]["role"] == "SATA/AHCI controller"

    text = render_topology_text(topo)
    assert "SATA/AHCI controller" in text
    assert "/dev/sda" in text
    assert "NVMe SSD" not in text
    assert "USB/SCSI bridge" not in text
    assert "native NVMe PCIe endpoint" not in text
    assert "rerun with sudo for NVMe identity" not in text
    assert "Namespace count" not in text
    assert "Block device         /dev/sda" in text

def test_macos_usb_sata_topology_uses_ata_not_nvme_wording(monkeypatch):
    from src.model import Snapshot
    import src.collect_macos as collect_macos

    snap = Snapshot("disk4", "disk4", "/dev/disk4")
    snap.controller_info = {
        "model": "WD Blue SA510 2.5 2TB",
        "serial": "23074M442603",
        "firmware_rev": "530309WD",
        "protocol": "ATA",
        "transport": "USB -> SATA",
        "size_in_bytes": 2_000_398_934_016,
    }
    monkeypatch.setattr(
        collect_macos,
        "collect_snapshot_macos",
        lambda requested_device, runner=None, kernel_lines=0, direct_usb=False: snap,
    )

    topo = collect_topology("disk4", platform_name="darwin")
    text = render_topology_text(topo)
    assert "ATA/SATA drive — WD Blue SA510 2.5 2TB" in text
    assert "USB-to-SATA bridge" in text
    assert "NVMe SSD" not in text
    assert "USB-to-NVMe" not in text
    assert "PCIe/NUMA/AER" not in text

class NativeSataSmartctlTopologyRunner(NativeSataTopologyRunner):
    def run(self, argv, timeout=8.0):
        if argv and argv[0] == "smartctl" and "-i" in argv:
            return CommandResult(
                list(argv), 0,
                stdout='''{
  "device": {"name": "/dev/sda", "type": "sat", "protocol": "ATA"},
  "model_name": "SOLIDIGM SSDSC2KB019TZ",
  "serial_number": "BTYI549203JJ1P9DGN",
  "firmware_version": "7CV10130",
  "sata_version": {"string": "SATA 3.3, 6.0 Gb/s"},
  "interface_speed": {
    "max": {"sata_value": 3, "string": "6.0 Gb/s"},
    "current": {"sata_value": 3, "string": "6.0 Gb/s"}
  }
}\n''',
            )
        return super().run(argv, timeout=timeout)


def test_native_sata_topology_distinguishes_host_pcie_from_drive_sata_link(tmp_path):
    devices = tmp_path / "devices" / "pci0000:00"
    root = devices / "0000:70:07.2"
    sata = root / "0000:73:00.0"
    ata_leaf = sata / "ata3" / "host3" / "target3:0:0" / "3:0:0:0"
    ata_leaf.mkdir(parents=True)
    _make_pci_node(root, cls="0x060400", speed="32.0 GT/s PCIe", width="16")
    _make_pci_node(sata, cls="0x010601", speed="32.0 GT/s PCIe", width="16")

    sys_pci = tmp_path / "sys_pci"
    sys_pci.mkdir()
    (sys_pci / "0000:70:07.2").symlink_to(root, target_is_directory=True)
    (sys_pci / "0000:73:00.0").symlink_to(sata, target_is_directory=True)

    sys_block = tmp_path / "sys_block"
    block = sys_block / "sda"
    block.mkdir(parents=True)
    (block / "device").symlink_to(ata_leaf, target_is_directory=True)
    _write(block / "device" / "type", "0")
    _write(block / "device" / "model", "SSDSC2KB019TZ")
    _write(block / "device" / "vendor", "SOLIDIGM")
    _write(block / "device" / "rev", "7CV10130")
    _write(block / "device" / "state", "running")
    _write(block / "queue" / "rotational", "0")
    _write(block / "size", "3750748848")
    _write(block / "queue" / "logical_block_size", "512")

    topo = collect_topology_linux(
        "sda",
        runner=NativeSataSmartctlTopologyRunner(),
        sys_pci=sys_pci,
        sys_class_block=sys_block,
    )
    text = render_topology_text(topo)
    assert "host PCIe Gen5 x16" in text
    assert "SATA link — 6.0 Gb/s" in text
    assert "Host PCIe link       Gen5 x16" in text
    assert "Drive SATA link      6.0 Gb/s" in text
    assert "Host controller      0000:73:00.0" in text
    assert "libata port ata3 — host3" in text
    assert "SCSI address 3:0:0:0" in text
    assert "libata port          ata3 (host3)" in text
    assert "Endpoint link        Gen5 x16" not in text
    assert "at device maximum" not in text

class TwoNativeSataRunner(NativeSataTopologyRunner):
    def run(self, argv, timeout=8.0):
        if argv and argv[0] == "smartctl" and "-i" in argv:
            dev = argv[-1]
            model = "SOLIDIGM SSDSC2KB019TZ" if dev.endswith("sda") else "SOLIDIGM SSDSC2KB076TZ"
            serial = "SER-A" if dev.endswith("sda") else "SER-B"
            return CommandResult(
                list(argv), 0,
                stdout=(
                    '{"device":{"name":"%s","type":"sat","protocol":"ATA"},'
                    '"model_name":"%s","serial_number":"%s","firmware_version":"7CV10130",'
                    '"sata_version":{"string":"SATA 3.3, 6.0 Gb/s"},'
                    '"interface_speed":{"max":{"string":"6.0 Gb/s"},"current":{"string":"6.0 Gb/s"}}}\n'
                ) % (dev, model, serial),
            )
        return super().run(argv, timeout=timeout)


def test_two_sata_disks_share_ahci_controller_but_show_distinct_libata_ports(tmp_path):
    devices = tmp_path / "devices" / "pci0000:00"
    root = devices / "0000:70:07.2"
    sata = root / "0000:73:00.0"
    leaf_a = sata / "ata3" / "host3" / "target3:0:0" / "3:0:0:0"
    leaf_b = sata / "ata4" / "host4" / "target4:0:0" / "4:0:0:0"
    leaf_a.mkdir(parents=True)
    leaf_b.mkdir(parents=True)
    _make_pci_node(root, cls="0x060400", speed="32.0 GT/s PCIe", width="16")
    _make_pci_node(sata, cls="0x010601", speed="32.0 GT/s PCIe", width="16")

    sys_pci = tmp_path / "sys_pci"
    sys_pci.mkdir()
    (sys_pci / "0000:70:07.2").symlink_to(root, target_is_directory=True)
    (sys_pci / "0000:73:00.0").symlink_to(sata, target_is_directory=True)

    sys_block = tmp_path / "sys_block"
    for name, leaf, model, sectors in [
        ("sda", leaf_a, "SSDSC2KB019TZ", "3750748848"),
        ("sdb", leaf_b, "SSDSC2KB076TZ", "15002992640"),
    ]:
        block = sys_block / name
        block.mkdir(parents=True)
        (block / "device").symlink_to(leaf, target_is_directory=True)
        _write(block / "device" / "type", "0")
        _write(block / "device" / "model", model)
        _write(block / "device" / "vendor", "SOLIDIGM")
        _write(block / "device" / "rev", "7CV10130")
        _write(block / "device" / "state", "running")
        _write(block / "queue" / "rotational", "0")
        _write(block / "size", sectors)
        _write(block / "queue" / "logical_block_size", "512")

    runner = TwoNativeSataRunner()
    topo_a = collect_topology_linux("sda", runner=runner, sys_pci=sys_pci, sys_class_block=sys_block)
    topo_b = collect_topology_linux("sdb", runner=runner, sys_pci=sys_pci, sys_class_block=sys_block)

    assert topo_a["pci_endpoint"]["bdf"] == topo_b["pci_endpoint"]["bdf"] == "0000:73:00.0"
    assert topo_a["controller_info"]["ata_port"] == "ata3"
    assert topo_b["controller_info"]["ata_port"] == "ata4"
    assert topo_a["controller_info"]["scsi_lun"] == "3:0:0:0"
    assert topo_b["controller_info"]["scsi_lun"] == "4:0:0:0"

    text_a = render_topology_text(topo_a)
    text_b = render_topology_text(topo_b)
    assert "libata port ata3 — host3" in text_a
    assert "SCSI address 3:0:0:0" in text_a
    assert "libata port          ata3 (host3)" in text_a
    assert "libata port ata4 — host4" in text_b
    assert "SCSI address 4:0:0:0" in text_b
    assert "libata port          ata4 (host4)" in text_b


def test_all_topology_merges_shared_ahci_controller_into_one_tree():
    from src.render import render_topology_all_text

    def sata(dev, port, host, lun, model, capacity):
        return {
            "platform": "linux",
            "controller": dev,
            "controller_device": f"/dev/{dev}",
            "controller_info": {
                "protocol": "ATA",
                "transport": "SATA",
                "model": model,
                "ata_port": port,
                "scsi_host": host,
                "scsi_lun": lun,
                "interface_speed": {"current": {"string": "6.0 Gb/s"}},
            },
            "numa_node": "0",
            "local_cpulist": "0-7,64-71",
            "pci_domain": "0000",
            "pci_path": [
                {
                    "bdf": "0000:70:07.2",
                    "role": "PCIe bridge / port",
                    "description": "AMD Turin Internal PCIe GPP Bridge",
                    "current_link_speed": "32.0 GT/s PCIe",
                    "current_link_width": "16",
                },
                {
                    "bdf": "0000:73:00.0",
                    "role": "SATA/AHCI controller",
                    "description": "AMD FCH SATA Controller",
                    "current_link_speed": "32.0 GT/s PCIe",
                    "current_link_width": "16",
                    "driver": "ahci",
                },
            ],
            "namespaces": [{
                "device": f"/dev/{dev}",
                "capacity_bytes": capacity,
                "logical_block_size": 512,
            }],
            "complete": True,
            "notes": [],
        }

    all_topo = {
        "platform": "linux",
        "all_devices": True,
        "devices": [
            sata("sda", "ata3", "host3", "3:0:0:0", "SOLIDIGM SSDSC2KB019TZ", 1_920_000_000_000),
            sata("sdb", "ata4", "host4", "4:0:0:0", "SOLIDIGM SSDSC2KB076TZ", 7_680_000_000_000),
        ],
        "errors": [],
        "complete": True,
    }
    text = render_topology_all_text(all_topo)
    assert text.count("0000:73:00.0  SATA/AHCI controller") == 1
    assert "├─ libata port ata3 — host3" in text
    assert "└─ libata port ata4 — host4" in text
    assert "/dev/sda — SOLIDIGM SSDSC2KB019TZ" in text
    assert "/dev/sdb — SOLIDIGM SSDSC2KB076TZ" in text
    assert "SATA link — 6.0 Gb/s" in text


def test_topology_parser_allows_no_device_for_all_drive_tree():
    from src import cli
    args = cli.build_parser().parse_args(["topology"])
    assert args.command == "topology"
    assert args.device is None

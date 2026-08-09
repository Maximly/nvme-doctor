from src.util import (
    controller_from_device,
    parse_counter_blob,
    parse_pcie_speed,
    parse_pcie_width,
    parse_temperature_c,
)


def test_controller_from_namespace():
    assert controller_from_device("/dev/nvme12n3p1") == "nvme12"


def test_pcie_parsers():
    assert parse_pcie_speed("16.0 GT/s PCIe") == 16.0
    assert parse_pcie_width("8") == 8
    assert parse_pcie_width("x4") == 4


def test_temperature_kelvin_and_celsius():
    assert parse_temperature_c(45) == 45
    assert parse_temperature_c(318) == 45


def test_counter_blob():
    parsed = parse_counter_blob("RxErr 4\nBadTLP 2\n")
    assert parsed == {"RxErr": 4, "BadTLP": 2}


def test_pcie_generation_mapping():
    from src.util import pcie_generation
    assert pcie_generation("8.0 GT/s PCIe") == 3
    assert pcie_generation("16.0 GT/s PCIe") == 4
    assert pcie_generation("32.0 GT/s PCIe") == 5
    assert pcie_generation("64.0 GT/s PCIe") == 6
    assert pcie_generation("12.0 GT/s") is None


def test_nvme_data_units_are_512000_bytes():
    from src.util import nvme_data_units_to_bytes
    assert nvme_data_units_to_bytes(1) == 512000
    assert nvme_data_units_to_bytes("2") == 1024000


def test_packed_nvme_version():
    from src.util import format_nvme_version
    assert format_nvme_version(0x00010400) == "1.4"
    assert format_nvme_version(0x00020000) == "2.0"
    assert format_nvme_version("2.0") == "2.0"


def test_smartctl_nvme_version_object():
    from src.util import format_nvme_version
    assert format_nvme_version({"string": "2.0", "value": 131072}) == "2.0"
    assert format_nvme_version({"value": 0x00010400}) == "1.4"

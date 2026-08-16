from pathlib import Path
import subprocess
import sys


def test_flat_source_layout_and_standalone_executable(tmp_path):
    root = Path(__file__).resolve().parents[1]
    assert (root / "src" / "cli.py").is_file()
    assert (root / "src" / "macos_usb_nvme.py").is_file()
    assert (root / "src" / "macos_usb_sata.py").is_file()
    assert not (root / "src" / "nvme_doctor").exists()

    result = subprocess.run(
        [str(root / "nvme-doctor"), "--version"],
        cwd=root,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "nvme-doctor 1.1.21"
    standalone = (root / "nvme-doctor").read_text(encoding="utf-8")
    assert "'nvme_doctor.macos_usb_sata':" in standalone

    rebuilt = tmp_path / "nvme-doctor"
    build = subprocess.run(
        [sys.executable, str(root / "tools" / "build_single.py"), str(rebuilt)],
        cwd=root,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    assert build.returncode == 0, build.stderr
    assert rebuilt.read_bytes() == (root / "nvme-doctor").read_bytes()


def test_build_script_rebuilds_root_standalone(tmp_path):
    root = Path(__file__).resolve().parents[1]
    original = (root / "nvme-doctor").read_bytes()
    try:
        result = subprocess.run(
            [str(root / "build.sh")],
            cwd=root,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        assert "nvme-doctor 1.1.21" in result.stdout
        assert (root / "nvme-doctor").is_file()
    finally:
        # A successful build is deterministic, but preserve the checked-in artifact
        # if this test is ever run against a deliberately modified fixture.
        if not (root / "nvme-doctor").exists():
            (root / "nvme-doctor").write_bytes(original)
            (root / "nvme-doctor").chmod(0o755)


def test_install_script_installs_prebuilt_without_building(tmp_path):
    root = Path(__file__).resolve().parents[1]
    prefix = tmp_path / "prefix"
    result = subprocess.run(
        [str(root / "install.sh"), "install"],
        cwd=root,
        env={**__import__("os").environ, "PREFIX": str(prefix)},
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    installed = prefix / "bin" / "nvme-doctor"
    assert installed.is_file()
    assert installed.read_bytes() == (root / "nvme-doctor").read_bytes()
    assert "Installed pre-built nvme-doctor" in result.stdout


def test_install_script_refuses_missing_prebuilt(tmp_path):
    root = Path(__file__).resolve().parents[1]
    artifact = root / "nvme-doctor"
    hidden = root / "nvme-doctor.test-hidden"
    artifact.rename(hidden)
    try:
        result = subprocess.run(
            [str(root / "install.sh"), "install"],
            cwd=root,
            env={**__import__("os").environ, "PREFIX": str(tmp_path / "prefix")},
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        assert result.returncode != 0
        assert "pre-built nvme-doctor is missing" in result.stderr
        assert "Run ./build.sh first" in result.stderr
    finally:
        hidden.rename(artifact)


def test_user_documentation_tracks_current_release_and_output_semantics():
    root = Path(__file__).resolve().parents[1]
    readme = (root / "README.md").read_text(encoding="utf-8")
    arch = (root / "docs" / "ARCHITECTURE.md").read_text(encoding="utf-8")
    rules = (root / "docs" / "DIAGNOSTIC-RULES.md").read_text(encoding="utf-8")
    macos = (root / "docs" / "MACOS.md").read_text(encoding="utf-8")

    assert "**1.1.21**" in readme
    assert "probe-needed" not in readme
    assert "Health  Proto  Transport" in readme
    assert "GOOD" in readme
    assert "Size" in readme
    assert "sudo nvme-doctor topology" in readme
    assert "all physical drives in one merged tree" in readme
    assert "host-controller PCIe interconnect" in readme
    assert "smartctl automatic device detection" in readme

    assert "macos_usb_sata.py" in arch
    assert "libata port / SCSI attachment" in arch
    assert "HEALTHY" in arch and "AT RISK" in arch and "INCOMPLETE" in arch

    assert "ata-reserve-low" in rules
    assert "Reallocated_Sector_Ct" in rules
    assert "Gen5 SATA SSD" in rules

    assert "smartctl automatic detection" in macos
    assert "PASS THROUGH(16)" in macos
    assert "PASS THROUGH(12)" in macos
    assert "Collecting topology..." in macos

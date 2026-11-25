from pathlib import Path
import subprocess
import sys


def test_flat_source_layout_and_standalone_executable(tmp_path):
    root = Path(__file__).resolve().parents[1]
    assert (root / "src" / "cli.py").is_file()
    assert (root / "src" / "macos_usb_nvme.py").is_file()
    assert (root / "src" / "macos_usb_sata.py").is_file()
    assert not (root / "src" / "nvme_doctor").exists()
    version = (root / "VERSION").read_text(encoding="utf-8").strip()
    assert version

    result = subprocess.run(
        [str(root / "nvme-doctor"), "--version"],
        cwd=root,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == f"nvme-doctor {version}"
    standalone = (root / "nvme-doctor").read_text(encoding="utf-8")
    assert "'nvme_doctor.macos_usb_sata':" in standalone
    assert "@@NVME_DOCTOR_VERSION@@" not in standalone

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
    version = (root / "VERSION").read_text(encoding="utf-8").strip()
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
        assert f"nvme-doctor {version}" in result.stdout
        assert (root / "nvme-doctor").is_file()
    finally:
        # A successful build is deterministic, but preserve the checked-in artifact
        # if this test is ever run against a deliberately modified fixture.
        if not (root / "nvme-doctor").exists():
            (root / "nvme-doctor").write_bytes(original)
            (root / "nvme-doctor").chmod(0o755)


def test_make_install_installs_built_artifact(tmp_path):
    root = Path(__file__).resolve().parents[1]
    version = (root / "VERSION").read_text(encoding="utf-8").strip()
    prefix = tmp_path / "prefix"
    result = subprocess.run(
        ["make", "install"],
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
    assert f"nvme-doctor {version}" in result.stdout


def test_make_uninstall_removes_installed_command(tmp_path):
    root = Path(__file__).resolve().parents[1]
    prefix = tmp_path / "prefix"
    install = subprocess.run(
        ["make", "install"],
        cwd=root,
        env={**__import__("os").environ, "PREFIX": str(prefix)},
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    assert install.returncode == 0, install.stderr
    installed = prefix / "bin" / "nvme-doctor"
    assert installed.is_file()
    result = subprocess.run(
        ["make", "uninstall"],
        cwd=root,
        env={**__import__("os").environ, "PREFIX": str(prefix)},
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert not installed.exists()


def test_user_documentation_tracks_current_release_and_output_semantics():
    root = Path(__file__).resolve().parents[1]
    version = (root / "VERSION").read_text(encoding="utf-8").strip()
    readme = (root / "README.md").read_text(encoding="utf-8")
    arch = (root / "docs" / "ARCHITECTURE.md").read_text(encoding="utf-8")
    rules = (root / "docs" / "DIAGNOSTIC-RULES.md").read_text(encoding="utf-8")
    macos = (root / "docs" / "MACOS.md").read_text(encoding="utf-8")

    assert f"**{version}**" in readme
    assert "sudo ./nvme-doctor install" in readme
    assert "sudo ./nvme-doctor remove" in readme
    assert "install.sh" not in readme
    assert "install.sh" not in arch
    assert "probe-needed" not in readme
    assert "Health Temp  Proto  Transport" in readme
    assert "TBW" in readme
    assert "natural order" in readme
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

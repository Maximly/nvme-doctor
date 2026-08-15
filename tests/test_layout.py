from pathlib import Path
import subprocess
import sys


def test_flat_source_layout_and_standalone_executable(tmp_path):
    root = Path(__file__).resolve().parents[1]
    assert (root / "src" / "cli.py").is_file()
    assert (root / "src" / "macos_usb_nvme.py").is_file()
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
    assert result.stdout.strip() == "nvme-doctor 1.1.12"

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
        assert "nvme-doctor 1.1.12" in result.stdout
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

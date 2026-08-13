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
    assert result.stdout.strip() == "nvme-doctor 1.0.3"

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

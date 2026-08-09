from pathlib import Path
import subprocess


def test_flat_source_layout_and_repository_launcher():
    root = Path(__file__).resolve().parents[1]
    assert (root / "src" / "cli.py").is_file()
    assert (root / "src" / "collect.py").is_file()
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
    assert result.stdout.strip() == "nvme-doctor 0.4.2"

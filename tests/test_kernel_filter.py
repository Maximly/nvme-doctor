from src.collect import _kernel_log
from src.model import CommandResult


class FakeRunner:
    def run(self, argv, timeout=8.0):
        if argv[0] == "journalctl":
            return CommandResult(list(argv), 0, stdout="\n".join([
                "nvme nvme0: I/O timeout, reset controller",
                "pcieport 0000:40:01.1: AER: Corrected error received",
                "pcieport 0000:80:01.1: AER: Uncorrected error received",
                "nvme nvme7: I/O timeout, reset controller",
            ]))
        return CommandResult(list(argv), 1)


def test_kernel_log_excludes_unrelated_nvme_and_aer():
    lines = _kernel_log(FakeRunner(), "nvme0", ["0000:40:01.1", "0000:41:00.0"], 100)
    joined = "\n".join(lines)
    assert "nvme0" in joined
    assert "40:01.1" in joined
    assert "80:01.1" not in joined
    assert "nvme7" not in joined

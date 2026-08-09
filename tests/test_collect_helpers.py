import json

from src.collect import _read_tool_json
from src.model import CommandResult


class OneResultRunner:
    def __init__(self, result):
        self.result = result
        self.argv = None

    def run(self, argv, timeout=8.0):
        self.argv = list(argv)
        return self.result


def test_smartctl_valid_json_survives_health_exit_status():
    payload = {
        "smartctl": {"exit_status": 8},
        "nvme_smart_health_information_log": {"critical_warning": 1},
    }
    runner = OneResultRunner(CommandResult(["smartctl"], 8, stdout=json.dumps(payload)))
    notes = []
    got = _read_tool_json(
        runner,
        ["smartctl", "-a", "-j", "/dev/nvme0"],
        notes,
        accept_json_on_nonzero=True,
    )
    assert got == payload
    assert notes == []


def test_smartctl_collection_failure_bits_are_noted_but_json_retained():
    payload = {"smartctl": {"exit_status": 2}}
    runner = OneResultRunner(CommandResult(["smartctl"], 2, stdout=json.dumps(payload)))
    notes = []
    got = _read_tool_json(
        runner,
        ["smartctl", "-a", "-j", "/dev/nvme0"],
        notes,
        accept_json_on_nonzero=True,
    )
    assert got == payload
    assert "collection-status bits" in notes[0]


def test_nvme_nonzero_is_failure_even_with_text_output():
    runner = OneResultRunner(CommandResult(["nvme"], 1, stdout="plugin help", stderr="bad syntax"))
    notes = []
    assert _read_tool_json(runner, ["nvme", "smart-log", "/dev/nvme0", "-o", "json"], notes) is None
    assert notes and notes[0].startswith("nvme smart-log failed:")

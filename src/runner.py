# SPDX-License-Identifier: GPL-3.0-or-later
from __future__ import annotations

import shutil
import subprocess
from typing import Iterable, Optional

from .model import CommandResult


class Runner:
    """Small subprocess wrapper to keep collectors testable."""

    def which(self, program: str) -> Optional[str]:
        return shutil.which(program)

    def run(self, argv: Iterable[str], timeout: float = 8.0) -> CommandResult:
        args = list(argv)
        if not args:
            return CommandResult([], 127, stderr="empty command", available=False)
        if self.which(args[0]) is None:
            return CommandResult(args, 127, stderr=f"{args[0]} not found", available=False)
        try:
            proc = subprocess.run(
                args,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                errors="replace",
                timeout=timeout,
                check=False,
            )
            return CommandResult(args, proc.returncode, proc.stdout, proc.stderr, True)
        except subprocess.TimeoutExpired as exc:
            stdout = exc.stdout if isinstance(exc.stdout, str) else ""
            stderr = exc.stderr if isinstance(exc.stderr, str) else ""
            return CommandResult(args, 124, stdout, (stderr + "\ncommand timed out").strip(), True)
        except OSError as exc:
            return CommandResult(args, 126, stderr=str(exc), available=True)

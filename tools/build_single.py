#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Build the standalone nvme-doctor executable from the flat src/*.py tree."""

from __future__ import annotations

import argparse
import re
from pathlib import Path

VERSION_PLACEHOLDER = "@@NVME_DOCTOR_VERSION@@"

MODULE_ORDER = [
    "__init__",
    "model",
    "runner",
    "util",
    "platforms",
    "macos_usb_nvme",
    "macos_usb_sata",
    "collect_macos",
    "collect",
    "diagnose",
    "diff",
    "render",
    "cli",
]

HEADER = '''#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Standalone build of NVMe Doctor. Rebuild with: python3 tools/build_single.py
from __future__ import annotations

import importlib.abc as _abc
import importlib.util as _util
import sys as _sys

_SOURCES = {
'''

FOOTER = r'''}

class _BundleImporter(_abc.MetaPathFinder, _abc.Loader):
    def find_spec(self, fullname, path=None, target=None):
        if fullname not in _SOURCES:
            return None
        return _util.spec_from_loader(
            fullname,
            self,
            is_package=(fullname == "nvme_doctor"),
        )

    def create_module(self, spec):
        return None

    def exec_module(self, module):
        source = _SOURCES[module.__name__]
        module.__file__ = f"<bundled:{module.__name__}>"
        exec(compile(source, module.__file__, "exec"), module.__dict__)

_sys.meta_path.insert(0, _BundleImporter())

from nvme_doctor.cli import main as _main

if __name__ == "__main__":
    raise SystemExit(_main())
'''


def module_name(stem: str) -> str:
    return "nvme_doctor" if stem == "__init__" else f"nvme_doctor.{stem}"


def read_version(root: Path) -> str:
    """Read and validate the sole release version from the repo-root VERSION file."""
    version_file = root / "VERSION"
    if not version_file.is_file():
        raise SystemExit(f"ERROR: VERSION file missing at {version_file}")
    version = version_file.read_text(encoding="utf-8").strip()
    if not re.fullmatch(r"[0-9]+([.][0-9]+)+([.-][A-Za-z0-9]+)*", version):
        raise SystemExit(f"ERROR: invalid VERSION contents: {version!r}")
    return version


def sync_readme_version(root: Path, version: str) -> None:
    """Keep the README release line in sync with the repo-root VERSION file."""
    readme = root / "README.md"
    if not readme.is_file():
        return
    text = readme.read_text(encoding="utf-8")
    updated, count = re.subn(
        r"\*\*[0-9]+(?:\.[0-9]+)+\*\*",
        f"**{version}**",
        text,
        count=1,
    )
    if count and updated != text:
        readme.write_text(updated, encoding="utf-8")
        print(f"Updated README.md release line to {version}")


def build(root: Path, output: Path) -> None:
    src = root / "src"
    missing = [name for name in MODULE_ORDER if not (src / f"{name}.py").is_file()]
    if missing:
        raise SystemExit("missing source module(s): " + ", ".join(missing))

    version = read_version(root)
    sync_readme_version(root, version)
    parts = [HEADER]
    for stem in MODULE_ORDER:
        source = (src / f"{stem}.py").read_text(encoding="utf-8")
        source = source.replace(VERSION_PLACEHOLDER, version)
        parts.append(f"    {module_name(stem)!r}: {source!r},\n")
    parts.append(FOOTER)
    text = "".join(parts)
    if VERSION_PLACEHOLDER in text:
        raise SystemExit(f"ERROR: VERSION placeholder remains in built output")

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(text, encoding="utf-8")
    output.chmod(0o755)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("output", nargs="?", default="nvme-doctor")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    build(root, Path(args.output).resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

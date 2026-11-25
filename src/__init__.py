"""NVMe Doctor package."""

from pathlib import Path

# Build-time fallback; tools/build_single.py replaces this placeholder with the
# repo-root VERSION file contents when building the standalone executable.
_BUNDLED_VERSION = "@@NVME_DOCTOR_VERSION@@"


def _resolve_version() -> str:
    # Source checkout: the repo-root VERSION file is the single source of truth.
    try:
        version_file = Path(__file__).resolve().parent.parent / "VERSION"
        if version_file.is_file():
            text = version_file.read_text(encoding="utf-8").strip()
            if text:
                return text
    except OSError:
        pass
    # Standalone bundle: module paths are "<bundled:...>", so use the constant
    # that tools/build_single.py substituted at build time.
    if str(__file__).startswith("<bundled:"):
        return _BUNDLED_VERSION
    # Installed package: rely on distribution metadata.
    try:
        from importlib.metadata import PackageNotFoundError, version

        return version("nvme-doctor")
    except (ImportError, PackageNotFoundError, ValueError):
        pass
    return _BUNDLED_VERSION


__version__ = _resolve_version()

#!/bin/sh
# SPDX-License-Identifier: GPL-3.0-or-later
set -eu

PREFIX=${PREFIX:-/usr/local}
DESTDIR=${DESTDIR:-}
BINDIR="$DESTDIR$PREFIX/bin"
CMD="$BINDIR/nvme-doctor"
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
ACTION=${1:-install}

case "$ACTION" in
  install)
    TMPDIR_BASE=${TMPDIR:-/tmp}
    BUILD_DIR=$(mktemp -d "$TMPDIR_BASE/nvme-doctor.XXXXXX")
    trap 'rm -rf "$BUILD_DIR"' EXIT HUP INT TERM
    python3 "$ROOT/tools/build_single.py" "$BUILD_DIR/nvme-doctor"
    install -d "$BINDIR"
    install -m 0755 "$BUILD_DIR/nvme-doctor" "$CMD"
    printf 'Installed nvme-doctor to %s\n' "$CMD"
    "$CMD" --version
    ;;
  remove|uninstall)
    rm -f "$CMD"
    printf 'Removed nvme-doctor from %s\n' "$PREFIX"
    ;;
  *)
    echo "usage: $0 [install|remove]" >&2
    exit 2
    ;;
esac

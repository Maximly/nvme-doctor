#!/bin/sh
# SPDX-License-Identifier: GPL-3.0-or-later
set -eu

PREFIX=${PREFIX:-/usr/local}
DESTDIR=${DESTDIR:-}
BINDIR="$DESTDIR$PREFIX/bin"
CMD="$BINDIR/nvme-doctor"
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
ARTIFACT="$ROOT/nvme-doctor"
ACTION=${1:-install}

case "$ACTION" in
  install)
    if [ ! -f "$ARTIFACT" ]; then
      echo "ERROR: pre-built nvme-doctor is missing: $ARTIFACT" >&2
      echo "Run ./build.sh first, then rerun install.sh." >&2
      exit 1
    fi
    if [ ! -x "$ARTIFACT" ]; then
      echo "ERROR: pre-built nvme-doctor is not executable: $ARTIFACT" >&2
      echo "Run ./build.sh first, then rerun install.sh." >&2
      exit 1
    fi
    install -d "$BINDIR"
    install -m 0755 "$ARTIFACT" "$CMD"
    printf 'Installed pre-built nvme-doctor to %s\n' "$CMD"
    "$CMD" --version
    ;;
  remove|uninstall)
    rm -f "$CMD"
    printf 'Removed nvme-doctor from %s\n' "$CMD"
    ;;
  *)
    echo "usage: $0 [install|remove]" >&2
    exit 2
    ;;
esac

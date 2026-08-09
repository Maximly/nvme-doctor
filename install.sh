#!/bin/sh
# SPDX-License-Identifier: GPL-3.0-or-later
set -eu

PREFIX=${PREFIX:-/usr/local}
DESTDIR=${DESTDIR:-}
LIBROOT="$DESTDIR$PREFIX/lib/nvme-doctor"
BINDIR="$DESTDIR$PREFIX/bin"
CMD="$BINDIR/nvme-doctor"
ACTION=${1:-install}

case "$ACTION" in
  install)
    install -d "$LIBROOT" "$BINDIR"
    rm -rf "$LIBROOT/nvme_doctor"
    install -d "$LIBROOT/nvme_doctor"
    cp "$(dirname "$0")"/src/*.py "$LIBROOT/nvme_doctor/"
    find "$LIBROOT/nvme_doctor" -type d -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true
    cat > "$CMD" <<WRAPPER
#!/bin/sh
PYTHONPATH="$PREFIX/lib/nvme-doctor\${PYTHONPATH:+:\$PYTHONPATH}"
export PYTHONPATH
exec python3 -m nvme_doctor.cli "\$@"
WRAPPER
    chmod 0755 "$CMD"
    printf 'Installed nvme-doctor to %s\n' "$CMD"
    ;;
  remove|uninstall)
    rm -f "$CMD"
    rm -rf "$LIBROOT"
    printf 'Removed nvme-doctor from %s\n' "$PREFIX"
    ;;
  *)
    echo "usage: $0 [install|remove]" >&2
    exit 2
    ;;
esac

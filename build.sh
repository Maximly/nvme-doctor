#!/bin/sh
# SPDX-License-Identifier: GPL-3.0-or-later
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
OUT="$ROOT/nvme-doctor"

python3 "$ROOT/tools/build_single.py" "$OUT"
chmod 0755 "$OUT"
printf 'Built nvme-doctor: %s\n' "$OUT"
"$OUT" --version

#!/bin/sh
set -eu
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
exec "${GOLD_PYTHON:-python3}" "$SCRIPT_DIR/cron_run_v2.py" "$@"

#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="${1:-.}"
if [ $# -gt 0 ]; then
    shift
fi

if [ $# -eq 0 ]; then
    python "$SCRIPT_DIR/main.py" workflow --repo "$REPO" --mode scan-only
elif [[ " $* " != *" --mode "* ]]; then
    python "$SCRIPT_DIR/main.py" workflow --repo "$REPO" --mode scan-only "$@"
else
    python "$SCRIPT_DIR/main.py" workflow --repo "$REPO" "$@"
fi

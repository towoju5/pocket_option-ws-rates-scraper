#!/usr/bin/env bash
# Sets up a virtualenv (if needed), installs dependencies, and runs the
# example trading bot from examples/main.py.
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &>/dev/null && pwd)"
cd "$SCRIPT_DIR"

# shellcheck disable=SC1091
source "$SCRIPT_DIR/scripts/common.sh"

setup_venv "$SCRIPT_DIR"
load_env_file "$SCRIPT_DIR/.env"
require_session_env

echo "Starting bot..."
exec python examples/main.py

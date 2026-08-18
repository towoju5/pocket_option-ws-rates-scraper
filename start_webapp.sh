#!/usr/bin/env bash
# Sets up a virtualenv (if needed), installs dependencies, and runs the
# live assets/prices web page from examples/webapp.py.
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &>/dev/null && pwd)"
cd "$SCRIPT_DIR"

# shellcheck disable=SC1091
source "$SCRIPT_DIR/scripts/common.sh"

setup_venv "$SCRIPT_DIR"
load_env_file "$SCRIPT_DIR/.env"
require_session_env

echo "Starting web app on http://${WEBAPP_HOST:-127.0.0.1}:${WEBAPP_PORT:-8081}"
exec python examples/webapp.py

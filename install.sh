#!/usr/bin/env bash
# First-time local installation: checks whether each requirement (a .env file, a
# 3.13+ Python, the .venv virtualenv, and its dependencies) is already in place and
# only does the work for whatever is missing. Safe to re-run any time - it skips
# everything that's already set up.
#
# Run once, from inside the repo:
#   ./install.sh
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &>/dev/null && pwd)"
cd "$SCRIPT_DIR"

# shellcheck disable=SC1091
source "$SCRIPT_DIR/scripts/common.sh"

echo "=== [1/4] .env file ==="
if [ -f "$SCRIPT_DIR/.env" ]; then
    echo "Already exists - skipping."
else
    echo "Not found - creating from .env.example..."
    cp "$SCRIPT_DIR/.env.example" "$SCRIPT_DIR/.env"
    echo "Created .env - edit it and fill in PO_SESSION and PO_UID before running the app"
    echo "(see README.md 'Getting Session ID and UID')."
fi

load_env_file "$SCRIPT_DIR/.env"

echo
echo "=== [2/4] Python 3.13+ ==="
python_bin="$(find_python)"
echo "Found $python_bin ($("$python_bin" --version))"

echo
echo "=== [3/4] Virtualenv (.venv) ==="
if [ -d "$SCRIPT_DIR/.venv" ]; then
    echo "Already exists - skipping."
else
    if ! "$python_bin" -m venv --help &>/dev/null; then
        echo "Error: the 'venv' module is unavailable for $python_bin." >&2
        echo "  Debian/Ubuntu: sudo apt-get install python3-venv" >&2
        exit 1
    fi
    echo "Not found - creating at .venv..."
    "$python_bin" -m venv "$SCRIPT_DIR/.venv"
fi

# shellcheck disable=SC1091
source "$SCRIPT_DIR/.venv/bin/activate"

echo
echo "=== [4/4] Dependencies ==="
check_imports=(pocket_option aiohttp pydantic socketio pytz)
install_target="$SCRIPT_DIR"
if [ "${WEBAPP_CANDLE_STORAGE:-}" = "redis" ]; then
    install_target="${SCRIPT_DIR}[redis]"
    check_imports+=(redis)
fi

python_import_check="import importlib
for m in [$(printf '"%s",' "${check_imports[@]}")]:
    importlib.import_module(m)"

if python -c "$python_import_check" &>/dev/null; then
    echo "Already installed - skipping."
else
    echo "Installing..."
    pip install --quiet --upgrade pip
    pip install --quiet -e "$install_target"
fi

echo
echo "=== Done ==="
if [ -z "${PO_SESSION:-}" ] || [ -z "${PO_UID:-}" ]; then
    echo "Next: edit .env and set PO_SESSION and PO_UID, then run one of:"
else
    echo "Next: run one of:"
fi
echo "  ./start.sh         - example trading bot"
echo "  ./start_webapp.sh  - live assets/prices web dashboard"

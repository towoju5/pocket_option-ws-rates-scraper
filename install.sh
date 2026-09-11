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

# Attempts to install Python 3.13 (plus its venv module) via whatever system package
# manager is available. Returns non-zero (rather than exiting) on anything
# unsupported/failed, so the caller can fall back to find_python's manual-install
# error message.
install_python313() {
    if [ -f /etc/os-release ]; then
        # shellcheck disable=SC1091
        . /etc/os-release
    fi

    local sudo_cmd=()
    if [ "$(id -u)" -ne 0 ]; then
        command -v sudo &>/dev/null || { echo "Error: need root or sudo to install packages." >&2; return 1; }
        sudo_cmd=(sudo)
    fi

    case "${ID:-}" in
        ubuntu)
            echo "Installing python3.13 via apt..."
            "${sudo_cmd[@]}" apt-get update -qq
            if "${sudo_cmd[@]}" apt-get install -y python3.13 python3.13-venv 2>/dev/null; then
                return 0
            fi
            echo "python3.13 isn't in the default repos for this release - adding the" \
                 "deadsnakes PPA (https://launchpad.net/~deadsnakes/+archive/ubuntu/ppa)..."
            "${sudo_cmd[@]}" apt-get install -y software-properties-common
            "${sudo_cmd[@]}" add-apt-repository -y ppa:deadsnakes/ppa
            "${sudo_cmd[@]}" apt-get update -qq
            "${sudo_cmd[@]}" apt-get install -y python3.13 python3.13-venv
            ;;
        debian)
            echo "Installing python3.13 via apt..."
            "${sudo_cmd[@]}" apt-get update -qq
            "${sudo_cmd[@]}" apt-get install -y python3.13 python3.13-venv
            ;;
        fedora)
            echo "Installing python3.13 via dnf..."
            "${sudo_cmd[@]}" dnf install -y python3.13
            ;;
        arch)
            echo "Installing latest Python via pacman..."
            "${sudo_cmd[@]}" pacman -Sy --noconfirm python
            ;;
        *)
            if [ "$(uname -s)" = "Darwin" ] && command -v brew &>/dev/null; then
                echo "Installing python@3.13 via Homebrew..."
                brew install python@3.13
            else
                return 1
            fi
            ;;
    esac
}

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
if python_bin="$(find_python 2>/dev/null)"; then
    echo "Found $python_bin ($("$python_bin" --version))"
else
    echo "Not found - attempting to install..."
    if ! install_python313; then
        echo "Error: don't know how to auto-install Python 3.13+ on this system." >&2
        echo "  Install it yourself, then re-run this script (or set PYTHON_BIN)." >&2
        exit 1
    fi
    python_bin="$(find_python)"
    echo "Installed $python_bin ($("$python_bin" --version))"
fi

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

find_python() {
    if [ -n "${PYTHON_BIN:-}" ]; then
        echo "$PYTHON_BIN"
        return
    fi
    for candidate in python3.13 python3.14 python3.15 python3; do
        if command -v "$candidate" &>/dev/null; then
            version="$("$candidate" -c 'import sys; print(f"{sys.version_info[0]}.{sys.version_info[1]}")')"
            major="${version%%.*}"
            minor="${version#*.}"
            if [ "$major" -eq 3 ] && [ "$minor" -ge 13 ]; then
                echo "$candidate"
                return
            fi
        fi
    done
    echo "Error: Python 3.13+ is required but was not found. Install it or set PYTHON_BIN." >&2
    exit 1
}

setup_venv() {
    local project_dir="$1"
    local venv_dir="$project_dir/.venv"
    local python_bin
    python_bin="$(find_python)"
    echo "Using $python_bin ($("$python_bin" --version))"

    if [ ! -d "$venv_dir" ]; then
        echo "Creating virtualenv at $venv_dir"
        "$python_bin" -m venv "$venv_dir"
    fi

    # shellcheck disable=SC1091
    source "$venv_dir/bin/activate"

    echo "Installing dependencies..."
    pip install --quiet --upgrade pip
    pip install --quiet -e "$project_dir"
}

load_env_file() {
    local env_file="$1"
    if [ -f "$env_file" ]; then
        echo "Loading environment from $env_file"
        set -a
        # shellcheck disable=SC1091
        source "$env_file"
        set +a
    fi
}

require_session_env() {
    if [ -z "${PO_SESSION:-}" ] || [ -z "${PO_UID:-}" ]; then
        echo "Error: PO_SESSION and PO_UID must be set (copy .env.example to .env and fill them in)." >&2
        exit 1
    fi
}

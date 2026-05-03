#!/bin/bash
# SANCTUM — Automatic Launcher & Environment Manager
# This script ensures the virtual environment is set up and dependencies are installed.

set -e

# Resolve the absolute path of the project root, even if called via a symlink
SOURCE="${BASH_SOURCE[0]}"
while [ -h "$SOURCE" ]; do # resolve $SOURCE until the file is no longer a symlink
  DIR="$( cd -P "$( dirname "$SOURCE" )" && pwd )"
  SOURCE="$(readlink "$SOURCE")"
  [[ $SOURCE != /* ]] && SOURCE="$DIR/$SOURCE" # if $SOURCE was a relative symlink, we need to resolve it relative to the path where the symlink file was located
done
SCRIPT_DIR="$( cd -P "$( dirname "$SOURCE" )" && pwd )"

VENV_DIR="$SCRIPT_DIR/.venv"
REQ_FILE="$SCRIPT_DIR/sanctum/requirements.txt"

# --- Internal Setup Command ---
if [ "$1" == "setup" ]; then
    echo "🔧 Setting up Sanctum for global access..."
    
    # 1. Create venv and install deps
    if [ ! -d "$VENV_DIR" ]; then
        python3 -m venv "$VENV_DIR"
    fi
    source "$VENV_DIR/bin/activate"
    echo "📦 Updating dependencies..."
    pip install -q --upgrade pip
    pip install -q -r "$REQ_FILE"
    
    # 2. Try to find a writable bin directory in PATH
    TARGET_BIN=""
    PATHS_TO_CHECK=("/opt/homebrew/bin" "/usr/local/bin" "$HOME/.local/bin" "$HOME/bin")
    
    for p in "${PATHS_TO_CHECK[@]}"; do
        if [ -w "$p" ]; then
            TARGET_BIN="$p/sanctum"
            break
        fi
    done
    
    if [ -n "$TARGET_BIN" ]; then
        ln -sf "$SCRIPT_DIR/cli_launcher.sh" "$TARGET_BIN"
        echo "✅ Created symlink at $TARGET_BIN"
    else
        echo "⚠️  No writable bin directory found in common locations."
        echo "Attempting to create symlink at /usr/local/bin/sanctum with sudo..."
        sudo ln -sf "$SCRIPT_DIR/cli_launcher.sh" "/usr/local/bin/sanctum"
        echo "✅ Created symlink at /usr/local/bin/sanctum (via sudo)"
    fi
    
    echo "🎉 Setup complete! You can now run 'sanctum' from any directory."
    exit 0
fi

# --- Normal Execution ---

# 1. Automatic Venv Creation (if not already done via setup)
if [ ! -d "$VENV_DIR" ]; then
    python3 -m venv "$VENV_DIR"
    source "$VENV_DIR/bin/activate"
    pip install -q --upgrade pip
    pip install -q -r "$REQ_FILE"
fi

# 2. Activate environment
source "$VENV_DIR/bin/activate"

# 3. Add sanctum to PYTHONPATH so internal imports work
export PYTHONPATH="$SCRIPT_DIR/sanctum:$PYTHONPATH"

# 4. Execute the main program with all passed arguments
exec python3 "$SCRIPT_DIR/sanctum/sanctum.py" "$@"

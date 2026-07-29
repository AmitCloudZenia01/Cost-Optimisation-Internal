#!/bin/bash
# ─────────────────────────────────────────────────────────────────
#  CloudZenia — AWS Cost Report   |   Mac Launcher
#  Double-click this file to run the report.
# ─────────────────────────────────────────────────────────────────

cd "$(dirname "$0")"

clear
echo ""
echo "  ╔══════════════════════════════════════════════════╗"
echo "  ║  CloudZenia — AWS Cost Optimization Report       ║"
echo "  ╚══════════════════════════════════════════════════╝"
echo ""

# ── Check for Python 3 ───────────────────────────────────────────
PYTHON=""

for cmd in python3 python; do
    if command -v "$cmd" &>/dev/null; then
        if "$cmd" -c "import sys; sys.exit(0 if sys.version_info>=(3,8) else 1)" 2>/dev/null; then
            PYTHON="$cmd"
            echo "  ✓  $($PYTHON --version) is installed."
            break
        fi
    fi
done

# ── Python not found — install it ────────────────────────────────
if [ -z "$PYTHON" ]; then
    echo ""
    echo "  →  Python 3 not found. Trying to install..."
    echo ""

    # Try Homebrew
    if command -v brew &>/dev/null; then
        echo "  →  Installing via Homebrew..."
        brew install python3
        PYTHON=python3

    # Try installing Homebrew + Python together
    elif /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)" 2>/dev/null; then
        # Add brew to PATH (Apple Silicon)
        export PATH="/opt/homebrew/bin:$PATH"
        brew install python3 && PYTHON=python3
    fi

    # Still nothing — open download page
    if [ -z "$PYTHON" ] || ! command -v "$PYTHON" &>/dev/null; then
        echo ""
        echo "  ✗  Could not install Python automatically."
        echo ""
        echo "  Please install it manually:"
        echo "  1. Your browser will open the Python download page"
        echo "  2. Download and run the installer"
        echo "  3. Double-click mac_start.command again"
        echo ""
        open "https://www.python.org/downloads/"
        osascript -e 'display dialog "Python 3 is required.\n\nThe download page has opened in your browser.\n\n1. Download and run the installer\n2. Double-click mac_start.command again" buttons {"OK"} default button "OK" with icon note with title "Python Required"' 2>/dev/null
        read -p "Press Enter to close..."
        exit 1
    fi
    echo "  ✓  Python installed."
fi

# ── Install pip packages ─────────────────────────────────────────
echo ""
echo "  →  Checking required packages..."
$PYTHON -m pip install --upgrade pip -q 2>/dev/null

if ! $PYTHON -m pip install -r requirements.txt -q 2>/dev/null; then
    $PYTHON -m pip install -r requirements.txt -q --user 2>/dev/null || {
        echo ""
        echo "  ✗  Package install failed."
        echo "  Try running in Terminal:  pip3 install -r requirements.txt"
        read -p "Press Enter to close..."
        exit 1
    }
fi
echo "  ✓  All packages ready."

# ── Run the report ───────────────────────────────────────────────
echo ""
$PYTHON run.py

echo ""
read -p "  Press Enter to close..."

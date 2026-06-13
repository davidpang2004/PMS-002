#!/bin/bash
# DMS Server launcher — installs Flask if needed, then starts the server.
# Double-clickable on Mac if saved with .command extension.

set -e

cd "$(dirname "$0")"

echo
echo "============================================================"
echo "  DMS Server launcher"
echo "============================================================"
echo

# Pick a Python 3 interpreter
if command -v python3 >/dev/null 2>&1; then
    PYTHON=python3
elif command -v python >/dev/null 2>&1; then
    PYTHON=python
else
    echo "ERROR: Python 3 is not installed."
    echo
    echo "Install it with one of these methods:"
    echo "  • Mac:    brew install python3"
    echo "  • Mac:    download from https://www.python.org/downloads/"
    echo "  • Linux:  sudo apt install python3 python3-pip"
    echo
    read -p "Press Enter to exit..."
    exit 1
fi

echo "Python found: $($PYTHON --version)"

# Check required Python packages
NEEDED_PACKAGES="flask pypdf reportlab Pillow"
MISSING=""
for pkg in $NEEDED_PACKAGES; do
    case "$pkg" in
        Pillow) import_name="PIL" ;;
        *)      import_name="$pkg" ;;
    esac
    if ! $PYTHON -c "import $import_name" 2>/dev/null; then
        MISSING="$MISSING $pkg"
    fi
done

if [ -n "$MISSING" ]; then
    echo "Installing missing packages:$MISSING"
    if ! $PYTHON -m pip install --user $MISSING; then
        echo
        echo "ERROR: Could not install required packages."
        echo "Try running this manually:"
        echo "    $PYTHON -m pip install $MISSING"
        echo
        read -p "Press Enter to exit..."
        exit 1
    fi
fi

echo "Python packages: OK"
echo

$PYTHON dms_server.py "$@"

#!/bin/bash
# DMS Build — packages the app into dist/DMS.app
# Double-click this file in Finder to run.

cd "$(dirname "$0")"

echo
echo "============================================================"
echo "  DMS Build Script"
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
    echo
    read -p "Press Enter to exit..."
    exit 1
fi

echo "Python found: $($PYTHON --version)"
echo

$PYTHON build.py

echo
read -p "Press Enter to close this window..."

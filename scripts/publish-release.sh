#!/bin/bash
# publish-release.sh — create/update a GitHub release with the DMS build zips,
# and ALWAYS attach fixed-name copies (DMS-Mac.zip / DMS-Windows.zip) so the
# permanent download links keep working:
#
#   https://github.com/davidpang2004/PMS-002/releases/latest/download/DMS-Mac.zip
#   https://github.com/davidpang2004/PMS-002/releases/latest/download/DMS-Windows.zip
#
# Those links always serve whatever asset named exactly "DMS-Mac.zip" /
# "DMS-Windows.zip" is on the newest release — this script's whole job is to
# make sure that asset is always there, on every release, without you having
# to remember the extra step.
#
# Usage:
#   scripts/publish-release.sh <mac_app_zip> <win_exe_zip> [tag] [title]
#
# Examples:
#   scripts/publish-release.sh ~/Desktop/DMS-App-260914A.zip ~/Desktop/DMS-EXE-260914A.zip
#   scripts/publish-release.sh DMS-App-260914A.zip DMS-EXE-260914A.zip v2026.09.14 "DMS.app v2026.09.14"
#
# If tag/title are omitted, tag defaults to v<YYYY.MM.DD> (today) and title
# defaults to "DMS.app <tag>". If a release with that tag already exists,
# assets are uploaded to it (existing same-named assets are overwritten).

set -euo pipefail

REPO="davidpang2004/PMS-002"

if [ $# -lt 2 ]; then
    echo "Usage: $0 <mac_app_zip> <win_exe_zip> [tag] [title]" >&2
    exit 1
fi

MAC_ZIP="$1"
WIN_ZIP="$2"
TAG="${3:-v$(date +%Y.%m.%d)}"
TITLE="${4:-DMS.app $TAG}"

for f in "$MAC_ZIP" "$WIN_ZIP"; do
    if [ ! -f "$f" ]; then
        echo "Error: file not found: $f" >&2
        exit 1
    fi
done

command -v gh >/dev/null 2>&1 || { echo "Error: gh CLI not found" >&2; exit 1; }

WORKDIR="$(mktemp -d)"
trap 'rm -rf "$WORKDIR"' EXIT

STABLE_MAC="$WORKDIR/DMS-Mac.zip"
STABLE_WIN="$WORKDIR/DMS-Windows.zip"
cp "$MAC_ZIP" "$STABLE_MAC"
cp "$WIN_ZIP" "$STABLE_WIN"

echo "Repo:  $REPO"
echo "Tag:   $TAG"
echo "Title: $TITLE"
echo

if gh release view "$TAG" --repo "$REPO" >/dev/null 2>&1; then
    echo "Release $TAG already exists — uploading assets to it."
else
    echo "Creating release $TAG..."
    gh release create "$TAG" --repo "$REPO" --title "$TITLE" --notes "Automated release: $TITLE"
fi

echo "Uploading original build files (dated names, for the record)..."
gh release upload "$TAG" --repo "$REPO" "$MAC_ZIP" "$WIN_ZIP" --clobber

echo "Uploading fixed-name copies (for the permanent links)..."
gh release upload "$TAG" --repo "$REPO" "$STABLE_MAC" "$STABLE_WIN" --clobber

echo
echo "Done. Permanent links:"
echo "  Mac:     https://github.com/$REPO/releases/latest/download/DMS-Mac.zip"
echo "  Windows: https://github.com/$REPO/releases/latest/download/DMS-Windows.zip"

#!/bin/sh
# Build and check LinkFinder on Linux or macOS - the twin of build.ps1. The
# first failure stops the run, so green means every step passed.
# Usage: ./build.sh [open|pw]
# Idea, design and architecture by Stas Mitin. SPDX-License-Identifier: MIT
set -e

PYTHON="${PYTHON:-python3}"
cd "$(dirname "$0")"

BRAND="${1:-open}"
CLI_NAME="linkfinder"
BRAND_ARGS=""
# No icon here: an ELF carries none, so branding on this side is the byline
# alone. The branded build still refuses to fall back to the open one.
if [ "$BRAND" = "pw" ]; then
    if [ ! -f branding/brand.json ]; then
        echo "branding/brand.json is missing - see branding/README.txt" >&2
        exit 1
    fi
    CLI_NAME="linkfinder-pw"
    BRAND_ARGS="branding/brand.json:."
    # Default lab logins travel with the internal build only, like the brand.
    # The open build never bundles this, and a gate proves it.
    [ -f branding/logins.json ] && LOGIN_ARGS="branding/logins.json:."
    # Radio NETCONF logins and the site-specific log-file rules, same deal.
    [ -f branding/radio-logins.json ] && RADIO_LOGIN_ARGS="branding/radio-logins.json:."
    [ -f branding/radio-sources.json ] && RADIO_SRC_ARGS="branding/radio-sources.json:."
elif [ "$BRAND" != "open" ]; then
    echo "unknown brand '$BRAND' - use open or pw" >&2
    exit 1
fi
GUI_NAME="$CLI_NAME-gui"
printf '== %s build: %s / %s\n' "$BRAND" "$CLI_NAME" "$GUI_NAME"

step() {
    printf '== %s\n' "$1"
    shift
    "$@"
}

# The cheapest check there is, so it goes first: a name nothing defines is
# a crash in whichever branch holds it, and the tests cannot reach a branch
# behind an option nobody turned on.
step "no dead names" "$PYTHON" tools/no_dead_names.py
step "no site refs" "$PYTHON" tools/no_site_refs.py
# Before anything is bundled. PyInstaller only WARNS about a hidden import
# it cannot find, so a build host without paramiko produced binaries that
# started, drew every menu, and could not open one SSH session.
step "the libraries it needs are here" "$PYTHON" tools/libraries_present.py
# Built before the binary, so the page inside it describes this build.
step "help page" "$PYTHON" tools/make_help.py

# Never pipe a test into tail: a pipeline's exit status is the last
# command's, so `python test.py | tail -1` always succeeds and set -e
# never fires. This build passed with failing tests because of it.
for suite in tests/test_*.py; do
    name=$(basename "$suite" .py | sed 's/^test_//')
    printf '== tests: %s\n' "$name"
    if ! said=$("$PYTHON" "$suite" 2>&1); then
        printf '%s\n' "$said"
        printf '== tests: %s FAILED\n' "$name" >&2
        exit 1
    fi
    printf '%s\n' "$said" | tail -1
done

if ! "$PYTHON" -c "import PyInstaller" >/dev/null 2>&1; then
    echo
    echo "PyInstaller is not installed - checks passed, nothing was packaged."
    echo "  pip install pyinstaller   then run this again for a single file"
    exit 0
fi

ASSETS="linkfinder/assets:linkfinder/assets"
# The libraries are imported lazily, so PyInstaller cannot see them and the
# SSH half would be missing from the binary with no error anywhere. The list
# is read out of the source rather than typed here: the typed one said
# paramiko and requests, and had never heard of urllib3.
set -- --noconfirm --onefile --clean --log-level WARN --add-data "$ASSETS"
for library in $("$PYTHON" tools/libraries_present.py --names); do
    set -- "$@" --hidden-import "$library"
done
if [ -n "$BRAND_ARGS" ]; then
    set -- "$@" --add-data "$BRAND_ARGS"
fi
if [ -n "${LOGIN_ARGS:-}" ]; then
    set -- "$@" --add-data "$LOGIN_ARGS"
fi
if [ -n "${RADIO_LOGIN_ARGS:-}" ]; then
    set -- "$@" --add-data "$RADIO_LOGIN_ARGS"
fi
if [ -n "${RADIO_SRC_ARGS:-}" ]; then
    set -- "$@" --add-data "$RADIO_SRC_ARGS"
fi

step "build $CLI_NAME (CLI)" "$PYTHON" -m PyInstaller "$@" \
    --name "$CLI_NAME" cli_entry.py

if "$PYTHON" -c "import tkinter" >/dev/null 2>&1; then
    step "build $GUI_NAME (GUI)" "$PYTHON" -m PyInstaller "$@" --windowed \
        --name "$GUI_NAME" gui_entry.py
else
    echo "== skipping the GUI: tkinter is not installed, so a binary built now"
    echo "   would fail only when someone opened it"
    echo "   apt install python3-tk    RHEL: dnf install python3-tkinter"
fi

# The suite runs from source, where every import works. The bundler reads
# the source for import statements, so a module reached by a name built at
# run time is left out and the feature silently does nothing in the
# product. Three link builders shipped missing that way once.
# Named rather than "everything in dist": the other brand's binaries are
# from an earlier run and have never heard of a module added since, which
# is no reason to stop this build. Run it bare to judge the lot.
built="dist/$CLI_NAME"
[ -f "dist/$GUI_NAME" ] && built="$built dist/$GUI_NAME"
step "the whole program is in the binary" \
    "$PYTHON" tools/all_modules_shipped.py $built

# Knowing what is inside it is not the same as knowing it works. This makes
# the built file merge two maps and compare them, which needs no network
# and no lab.
step "the binary starts and does its work" \
    "$PYTHON" tools/binary_runs.py

echo
ls -lh dist/

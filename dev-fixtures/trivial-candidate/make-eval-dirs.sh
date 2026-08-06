#!/usr/bin/env bash
# make-eval-dirs.sh — populate the model-facing workdir.
# The trivial fixture has nothing to install and nothing to seed;
# we just copy initial-prompt.md so the model has the task text.
set -euo pipefail
DEST="${1:?usage: make-eval-dirs.sh <destdir>}"
HERE="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
mkdir -p "$DEST"
cp "$HERE/initial-prompt.md" "$DEST/initial-prompt.md"

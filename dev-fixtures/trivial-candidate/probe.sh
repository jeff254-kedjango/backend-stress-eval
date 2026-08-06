#!/usr/bin/env bash
# probe.sh — exit 0 iff the model wrote "fixed" to answer.txt in the workdir.
set -euo pipefail
[[ -f answer.txt ]] || exit 1
[[ "$(cat answer.txt)" = "fixed" ]] || exit 1
exit 0

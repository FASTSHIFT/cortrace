#!/usr/bin/env bash
# Cortrace — Python format + lint for the host-side scripts (scripts/*.py).
# Formatting: black. Linting: pylint (see .pylintrc).
#
#   scripts/format-py.sh            format all Python sources in place (black)
#   scripts/format-py.sh --check    check only: black --check + pylint;
#                                    non-zero exit if either fails
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

BLACK="${BLACK:-black}"
PYLINT="${PYLINT:-pylint}"

if ! command -v "$BLACK" >/dev/null 2>&1; then
    echo "error: $BLACK not found (pip install black)" >&2
    exit 127
fi

mapfile -t FILES < <(git ls-files '*.py' 2>/dev/null || true)
if [[ ${#FILES[@]} -eq 0 ]]; then
    echo "no Python files to format"
    exit 0
fi

if [[ "${1:-}" != "--check" ]]; then
    "$BLACK" "${FILES[@]}"
    exit 0
fi

# --check: black formatting gate + pylint gate.
fail=0

if ! "$BLACK" --check --diff "${FILES[@]}"; then
    echo "error: run scripts/format-py.sh to fix formatting" >&2
    fail=1
fi

if ! command -v "$PYLINT" >/dev/null 2>&1; then
    echo "error: $PYLINT not found (pip install pylint)" >&2
    exit 127
fi

# Lint production and test files with their respective rcfiles. pylint uses the
# CWD's .pylintrc by default and does NOT auto-pick a per-directory one, so we
# point tests at scripts/tests/.pylintrc explicitly (it relaxes test-only rules
# like protected-access).
PROD=()
TESTS=()
for f in "${FILES[@]}"; do
    case "$f" in
        scripts/tests/*) TESTS+=("$f") ;;
        *) PROD+=("$f") ;;
    esac
done

if [[ ${#PROD[@]} -gt 0 ]] && ! "$PYLINT" "${PROD[@]}"; then
    echo "error: pylint reported issues (see .pylintrc)" >&2
    fail=1
fi
if [[ ${#TESTS[@]} -gt 0 ]] \
    && ! "$PYLINT" --rcfile=scripts/tests/.pylintrc "${TESTS[@]}"; then
    echo "error: pylint reported issues in tests (see scripts/tests/.pylintrc)" >&2
    fail=1
fi

exit "$fail"

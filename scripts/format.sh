#!/usr/bin/env bash
# Cortrace — clang-format runner (WebKit-based style, see .clang-format).
#
#   scripts/format.sh            format all sources in place
#   scripts/format.sh --check    check only; non-zero exit if anything differs
#
# Formats: include/**, src/**, tests/**, tools/** (*.hpp *.cpp *.h *.cc)
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

CLANG_FORMAT="${CLANG_FORMAT:-clang-format}"
if ! command -v "$CLANG_FORMAT" >/dev/null 2>&1; then
    echo "error: $CLANG_FORMAT not found" >&2
    exit 127
fi

CHECK=0
[[ "${1:-}" == "--check" ]] && CHECK=1

mapfile -t FILES < <(git ls-files \
    'include/*.hpp' 'include/*.h' \
    'src/*.cpp' 'src/*.hpp' \
    'tests/*.cpp' 'tests/*.hpp' \
    'tools/*.cpp' 'tools/*.hpp' 2>/dev/null || true)

if [[ ${#FILES[@]} -eq 0 ]]; then
    echo "no source files to format"
    exit 0
fi

if [[ $CHECK -eq 1 ]]; then
    fail=0
    for f in "${FILES[@]}"; do
        if ! diff -q <("$CLANG_FORMAT" --style=file "$f") "$f" >/dev/null; then
            echo "needs formatting: $f"
            fail=1
        fi
    done
    if [[ $fail -ne 0 ]]; then
        echo "error: run scripts/format.sh to fix formatting" >&2
        exit 1
    fi
    echo "format OK (${#FILES[@]} files)"
else
    "$CLANG_FORMAT" --style=file -i "${FILES[@]}"
    echo "formatted ${#FILES[@]} files"
fi

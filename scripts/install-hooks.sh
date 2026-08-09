#!/usr/bin/env bash
# Cortrace — install git hooks by pointing core.hooksPath at .githooks.
# Run once after cloning:  scripts/install-hooks.sh
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

chmod +x .githooks/* 2>/dev/null || true
git config core.hooksPath .githooks
echo "git hooks installed (core.hooksPath = .githooks)"
echo "pre-commit will enforce clang-format on staged C/C++ files."

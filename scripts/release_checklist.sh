#!/usr/bin/env bash
# Pre-release hygiene checks for a public llm-router tree.
# Run from the repository root: ./scripts/release_checklist.sh
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

echo "== llm-router release checklist =="

fail=0

echo "-- secrets / credential-looking strings (heuristic) --"
if grep -RInE \
  --exclude-dir=.git \
  --exclude-dir=.venv \
  --exclude-dir=venv \
  --exclude-dir=.mypy_cache \
  --exclude-dir=.ruff_cache \
  --exclude-dir=.pytest_cache \
  --exclude-dir=__pycache__ \
  --exclude='*.db' \
  -e 'sk-[a-zA-Z0-9]{20,}' \
  -e 'OPENAI_API_KEY=.+' \
  -e 'ANTHROPIC_API_KEY=.+' \
  -e 'DEEPSEEK_API_KEY=.+' \
  -e 'QWEN_API_KEY=.+' \
  . 2>/dev/null | grep -vE '\.env\.example|CHANGELOG|SECURITY|CONTRIBUTING|HARDENING_PLAN|client_example|test_|conftest|requirements|pyproject|\.md:|sk-demo|sk-test|sk-forced|placeholder|change-me' ; then
  echo "WARN: review matches above for real secrets" >&2
  fail=1
else
  echo "OK: no high-confidence secret matches outside known placeholders"
fi

echo "-- machine-absolute paths --"
if grep -RInE \
  --exclude-dir=.git \
  --exclude-dir=.venv \
  --exclude-dir=venv \
  --exclude-dir=.mypy_cache \
  --exclude-dir=.ruff_cache \
  --exclude-dir=.pytest_cache \
  --exclude-dir=__pycache__ \
  -e '[A-Za-z]:\\\\Users\\\\' \
  -e '/home/[^/]+/' \
  -e '/Users/[^/]+/' \
  . 2>/dev/null ; then
  echo "FAIL: absolute personal paths found" >&2
  fail=1
else
  echo "OK: no /home /Users or Windows Users paths"
fi

echo "-- required OSS files --"
for f in SECURITY.md CONTRIBUTING.md LICENSE CHANGELOG.md .github/PULL_REQUEST_TEMPLATE.md .github/workflows/ci.yml; do
  if [[ ! -f "$f" ]]; then
    echo "FAIL: missing $f" >&2
    fail=1
  else
    echo "OK: $f"
  fi
done

echo "-- pinned requirements present --"
if grep -qE '==[0-9]' requirements.txt; then
  echo "OK: requirements.txt appears pinned"
else
  echo "FAIL: requirements.txt missing == pins" >&2
  fail=1
fi

if [[ "$fail" -ne 0 ]]; then
  echo "Checklist FAILED" >&2
  exit 1
fi

echo "Checklist PASSED"
exit 0

#!/usr/bin/env bash
# Minimal curl client example against a local llm-router.
# Usage: ./scripts/client_example.sh
# Optional env: BASE_URL, API_KEY, MODEL
set -euo pipefail

BASE_URL="${BASE_URL:-http://127.0.0.1:8000}"
API_KEY="${API_KEY:-sk-demo-key}"
MODEL="${MODEL:-deepseek-chat}"

curl -sS "${BASE_URL}/v1/chat/completions" \
  -H "Authorization: Bearer ${API_KEY}" \
  -H "Content-Type: application/json" \
  -d "{\"model\":\"${MODEL}\",\"messages\":[{\"role\":\"user\",\"content\":\"Say hello in one short sentence.\"}]}"
echo

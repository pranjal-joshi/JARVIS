#!/usr/bin/env bash
# Times a few hub chat requests (plain reply, KB lookup, tool call) against the configured LLM.
# Usage: scripts/wsl/bench-llm.sh [runs]
set -euo pipefail
cd "$(dirname "$0")/../.."
set -a; source .env; set +a
runs=${1:-2}
hub=${HUB_URL:-http://localhost:8700}
prompts=(
  "Good evening. Who are you? One sentence."
  "What are my open tasks?"
  "What do I like to drink in the evening?"
)
model=$(curl -s -H "Authorization: Bearer $HUB_TOKEN_WEB" "$hub/v1/status" | sed -n 's/.*"model": *"\([^"]*\)".*/\1/p')
echo "model: ${model:-unknown}"
for p in "${prompts[@]}"; do
  for ((i = 1; i <= runs; i++)); do
    body=$(printf '{"model":"jarvis","messages":[{"role":"user","content":"%s"}]}' "$p")
    t=$(curl -s -o /tmp/bench-reply.json -w '%{time_total}' -H "Authorization: Bearer $HUB_TOKEN_WEB" \
      -H 'Content-Type: application/json' -d "$body" "$hub/v1/chat/completions")
    reply=$(sed -n 's/.*"content": *"\([^"]*\)".*/\1/p' /tmp/bench-reply.json | head -c 90)
    printf '%6.1fs  %-45s %s\n' "$t" "${p:0:45}" "$reply"
  done
done

#!/usr/bin/env bash
# First-run setup. Safe to re-run: never overwrites .env or an existing graph.
#  - seeds data/kb-graph (git-ignored) from kb/sample-graph
#  - creates .env from .env.example with fresh random tokens
set -euo pipefail
cd "$(dirname "$0")/../.."

if [[ ! -d data/kb-graph ]]; then
  mkdir -p data && cp -r kb/sample-graph data/kb-graph
  echo "Seeded data/kb-graph from kb/sample-graph (point KB_GRAPH_PATH at your own graph later)."
fi

if [[ -f .env ]]; then
  echo ".env already exists; leaving it alone."
  exit 0
fi

cp .env.example .env
for key in HUB_TOKEN_SATELLITE HUB_TOKEN_WEB HUB_TOKEN_PHONE VOICESTUDIO_API_KEY; do
  value=$(openssl rand -base64 32 | tr -d '/+=\n')
  sed -i "s|^${key}=change-me.*|${key}=${value}|" .env
done
chmod 600 .env
echo "Created .env with fresh tokens. Add HA_TOKEN when you set up Home Assistant (docs/ha-setup.md)."

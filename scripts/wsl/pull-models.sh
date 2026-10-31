#!/usr/bin/env bash
# Pull the LLM and embedding model named in .env into the Ollama container.
set -euo pipefail
cd "$(dirname "$0")/../.."
set -a; source .env; set +a

for model in "${EMBED_MODEL:-nomic-embed-text}" "${LLM_MODEL:-qwen3.5:4b}"; do
  echo "pulling $model"
  docker compose exec -T ollama ollama pull "$model"
done
docker compose exec -T ollama ollama list

#!/usr/bin/env bash
# Pull the embedding model and every local LLM named in .env (LLM_MODEL, LLM_MODELS) into the Ollama
# container. A name with a matching ollama/<name>.Modelfile is built from it (its FROM model is pulled
# first). Claude models (claude-*) run on the Anthropic API and are skipped.
set -euo pipefail
cd "$(dirname "$0")/../.."
set -a; source .env; set +a

IFS=, read -ra extra <<< "${LLM_MODELS:-}"
for model in "${EMBED_MODEL:-nomic-embed-text}" "${LLM_MODEL:-qwen3.5:4b}" "${extra[@]}"; do
  model="${model// /}"
  [[ -z "$model" || "$model" == claude-* ]] && continue
  file="ollama/${model}.Modelfile"
  if [[ -f "$file" ]]; then
    base=$(sed -n 's/^FROM //p' "$file")
    echo "pulling $base, building $model"
    docker compose exec -T ollama ollama pull "$base"
    docker compose exec -T ollama sh -c "cat > /tmp/Modelfile && ollama create '$model' -f /tmp/Modelfile" < "$file"
  else
    echo "pulling $model"
    docker compose exec -T ollama ollama pull "$model"
  fi
done
docker compose exec -T ollama ollama list

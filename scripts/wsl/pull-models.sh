#!/usr/bin/env bash
# Pull the LLM and embedding model named in .env into the Ollama container.
# A name with a matching ollama/<name>.Modelfile is built from it (its FROM model is pulled first).
set -euo pipefail
cd "$(dirname "$0")/../.."
set -a; source .env; set +a

for model in "${EMBED_MODEL:-nomic-embed-text}" "${LLM_MODEL:-qwen3.5:4b}"; do
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

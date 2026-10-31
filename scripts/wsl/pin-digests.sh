#!/usr/bin/env bash
# Print the digest of every pinned image after pulling, so tags can be locked
# as image: name:tag@sha256:... in compose.yaml. Read-only.
set -euo pipefail
cd "$(dirname "$0")/../.."
docker compose --profile voice --profile voicestudio config --images | sort -u | while read -r img; do
  [[ "$img" == jarvis/* ]] && continue
  digest=$(docker image inspect --format '{{index .RepoDigests 0}}' "$img" 2>/dev/null || echo "not pulled")
  printf '%-50s %s\n' "$img" "$digest"
done

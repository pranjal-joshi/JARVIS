#!/usr/bin/env bash
# Environment and stack health check. Read-only: changes nothing.
cd "$(dirname "$0")/../.."
ok()   { printf '  \e[36m✓\e[0m %s\n' "$*"; }
warn() { printf '  \e[33m!\e[0m %s\n' "$*"; }

echo "GPU"
if command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi --query-gpu=name,memory.total --format=csv,noheader 2>/dev/null; then
  ok "NVIDIA visible in this environment"
else
  warn "no usable NVIDIA GPU here: CPU mode (default compose)"
fi

echo "Docker"
docker info --format '  engine {{.ServerVersion}}, {{.NCPU}} CPUs, {{.MemTotal}} bytes RAM' 2>/dev/null || warn "docker not reachable"
docker network inspect ha_network >/dev/null 2>&1 && ok "ha_network exists (compose.ha.yaml can join it)" \
  || warn "ha_network missing (only needed for compose.ha.yaml)"

echo "Services"
docker compose --profile voice --profile voicestudio ps --format '  {{.Service}}: {{.Status}}' 2>/dev/null

echo "Hub"
if [[ -f .env ]]; then
  set -a; source .env; set +a
  curl -fsS localhost:8700/health && echo
  curl -fsS -H "Authorization: Bearer ${HUB_TOKEN_WEB}" localhost:8700/v1/status \
    | python3 -m json.tool 2>/dev/null | grep -E '"(model|up|vector|engine|voicestudio|homeassistant|builtin)"|connected|skipped|error' || warn "hub status unavailable"
else
  warn "no .env: run scripts/wsl/bootstrap.sh"
fi

#!/usr/bin/env zsh
set -euo pipefail

workspace_dir="$(cd "$(dirname "$0")/.." && pwd)"

set -a
source "$workspace_dir/.env"
set +a

export GRAFANA_URL="${GRAFANA_URL:-http://127.0.0.1:3000}"
export GRAFANA_USERNAME="${GRAFANA_USERNAME:-admin}"
export GRAFANA_PASSWORD="${GRAFANA_ADMIN_PASSWORD:?GRAFANA_ADMIN_PASSWORD must be set in .env}"

exec uvx mcp-grafana --disable-write
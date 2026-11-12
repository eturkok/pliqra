#!/bin/sh
# A persistent local workspace, isolated from the standard pliqra-data volume.
set -eu
project_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
action=${1:-start}

usage() {
  printf '%s\n' 'Usage: scripts/local-demo.sh [start|stop|status|logs]' \
    'Optional: PLIQRA_DEMO_PORT=9090 scripts/local-demo.sh start'
}

case "$action" in
  start|stop|status|logs) ;;
  -h|--help|help) usage; exit 0 ;;
  *) usage >&2; exit 2 ;;
esac
if ! command -v docker >/dev/null 2>&1; then
  printf '%s\n' 'Install Docker with Compose, then run this command again.' >&2
  exit 1
fi
if ! docker info >/dev/null 2>&1; then
  printf '%s\n' 'Start Docker Desktop (or your Docker engine), then run this command again.' >&2
  exit 1
fi
compose() {
  docker compose --project-name pliqra-demo --file "$project_dir/compose.demo.yaml" "$@"
}
case "$action" in
  start)
    port=${PLIQRA_DEMO_PORT:-8088}
    case "$port" in ''|*[!0-9]*) printf '%s\n' 'PLIQRA_DEMO_PORT must be a port number.' >&2; exit 2 ;; esac
    if [ "$port" -lt 1 ] || [ "$port" -gt 65535 ]; then
      printf '%s\n' 'PLIQRA_DEMO_PORT must be between 1 and 65535.' >&2
      exit 2
    fi
    compose up --detach --wait --wait-timeout 90
    printf '\nPliqra is ready: http://localhost:%s\n' "$port"
    printf '%s\n' 'Create your administrator, connect a model endpoint, then open Playground.' \
      'Your workspace and encryption key persist in the separate pliqra-demo-data volume.'
    ;;
  stop)
    compose stop
    printf '%s\n' 'Pliqra demo stopped. Your workspace is preserved.'
    ;;
  status) compose ps ;;
  logs) compose logs --tail 100 --follow ;;
esac

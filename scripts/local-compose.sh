#!/usr/bin/env bash

set -Eeuo pipefail

# 再起動と同じproject・Compose構成・local endpoint判定を使う。
# shellcheck source-path=SCRIPTDIR
# shellcheck source=restart-local.sh
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/restart-local.sh"

case "${1:-}" in
  status) compose_arguments=(ps --all) ;;
  logs) compose_arguments=(logs --follow --tail 100 api api-secondary gateway postgres redis) ;;
  stop) compose_arguments=(down) ;;
  *)
    printf 'Usage: ./scripts/local-compose.sh {status|logs|stop}\n' >&2
    exit 2
    ;;
esac
[[ $# -eq 1 ]] || exit 2

require_restart_command docker
require_local_docker_endpoint
run_docker_preflight false
run_local_compose "${compose_arguments[@]}"

#!/usr/bin/env bash
set -euo pipefail
repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"
action="${1:-}"
env_file="${2:-.local/tls/runtime.env}"
api_mode="${3:-single}"
case "$api_mode" in
  single|dual) ;;
  *) echo 'local_tls_api_mode_requires_single_or_dual' >&2; exit 64 ;;
esac
if [[ ! -f "$env_file" ]]; then
  echo 'local_tls_env_required: docs/LOCAL_TLS_RETEST.md' >&2
  exit 64
fi
compose=(docker compose --project-name gs-app-tomidaisai-tls --env-file "$env_file" -f compose.yaml)
api_services=(api)
if [[ "$api_mode" == dual ]]; then
  compose+=(-f compose.local-multi-api.yaml)
  api_services+=(api-secondary)
fi
compose+=(-f compose.local-tls.yaml)
if [[ "$api_mode" == dual ]]; then
  compose+=(-f compose.local-tls-multi-api.yaml)
fi
case "$action" in
  config)
    "${compose[@]}" config --quiet
    ;;
  build)
    # dualも共通imageを一度だけbuildし、別buildのartifactへ分岐させない。
    "${compose[@]}" build api gateway
    ;;
  up|up-offline)
    run_options=(--rm)
    up_options=(-d)
    if [[ "$action" == up-offline ]]; then
      # WANなしLANでは未準備imageを取得せず、停止前の準備へ戻す。
      run_options+=(--pull=never)
      up_options+=(--pull never --no-build)
    fi
    "${compose[@]}" config --quiet
    volume_name="$("${compose[@]}" config --format json | python3 -c 'import json,sys; print(json.load(sys.stdin)["volumes"]["postgres-data"]["name"])')"
    if ! docker volume inspect "$volume_name" >/dev/null 2>&1; then
      echo 'local_tls_existing_volume_required: DB initialization is a human task' >&2
      exit 64
    fi
    while IFS= read -r container; do
      [[ -z "$container" ]] && continue
      owner="$(docker inspect --format '{{index .Config.Labels "com.docker.compose.project"}}' "$container")"
      if [[ "$owner" != gs-app-tomidaisai-tls ]]; then
        echo 'local_tls_volume_owned_by_other_project' >&2
        exit 64
      fi
    done < <(docker ps -q --filter "volume=$volume_name")
    docker run "${run_options[@]}" --network none --read-only \
      --mount "type=volume,source=$volume_name,target=/existing,readonly" \
      --entrypoint sh postgres:16.10-alpine -c 'test -s /existing/PG_VERSION' || {
        echo 'local_tls_initialized_database_required' >&2; exit 64;
      }
    "${compose[@]}" up "${up_options[@]}" --wait --remove-orphans postgres redis "${api_services[@]}" gateway
    for api_service in "${api_services[@]}"; do
      "${compose[@]}" exec -T "$api_service" python -c \
        "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health/ready', timeout=5).close()"
    done
    ;;
  down)
    # volume削除・別projectへの操作は行わない。
    "${compose[@]}" down --remove-orphans
    ;;
  ps)
    "${compose[@]}" ps
    ;;
  *)
    echo 'usage: scripts/local-tls.sh {config|build|up|up-offline|down|ps} [env-file] [single|dual]' >&2
    exit 64
    ;;
esac

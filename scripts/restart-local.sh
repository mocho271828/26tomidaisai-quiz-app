#!/usr/bin/env bash

set -Eeuo pipefail

RESTART_SCRIPT_DIRECTORY="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
readonly RESTART_SCRIPT_DIRECTORY
RESTART_REPOSITORY_ROOT="$(cd -- "${RESTART_SCRIPT_DIRECTORY}/.." && pwd)"
readonly RESTART_REPOSITORY_ROOT
RESTART_COMPOSE_PROJECT="gs-app-tomidaisai"
readonly RESTART_COMPOSE_PROJECT
READINESS_MAX_ATTEMPTS=10
readonly READINESS_MAX_ATTEMPTS

restart_local_usage() {
  cat <<'EOF'
Usage: ./scripts/restart-local.sh [--bootstrap | --frontend-only] [--repair-colima]

Options:
  --bootstrap      専用local DBへmigrationと冪等な[DEMO] seedを明示適用する
  --frontend-only  gatewayだけをbuild・再作成して画面の変更を反映する
  --repair-colima  Docker接続断時に対象Colima profileの修復を許可する
  -h, --help       このhelpを表示する

通常実行はimageをbuildし直してPostgreSQL、Redis、2 API、gatewayを再作成します。
PostgreSQLのnamed volumeは削除せず、migrationとseedも実行しません。
--bootstrapは参加者ID poolを準備しません。DB初期化手順を別途確認してください。
EOF
}

restart_local_fail() {
  printf 'Error: %s\n' "$1" >&2
  return 1
}

require_restart_command() {
  command -v "$1" >/dev/null 2>&1 || {
    restart_local_fail "$1 commandが見つかりません"
    return 1
  }
}

run_docker_preflight() {
  local should_repair_colima="$1"

  if [[ "${should_repair_colima}" == true ]]; then
    "${RESTART_SCRIPT_DIRECTORY}/docker-preflight.sh" --repair-colima
  else
    "${RESTART_SCRIPT_DIRECTORY}/docker-preflight.sh"
  fi
}

require_local_docker_endpoint() {
  local context="${DOCKER_CONTEXT:-}"
  local endpoint="${DOCKER_HOST:-}"

  # Docker CLIと同じく、明示contextはDOCKER_HOSTより優先する。
  if [[ -n "${context}" || -z "${endpoint}" ]]; then
    if [[ -z "${context}" ]]; then
      context="$(docker context show 2>/dev/null)" || {
        restart_local_fail "Docker contextを取得できません"
        return
      }
    fi
    endpoint="$(
      docker context inspect "${context}" \
        --format '{{.Endpoints.docker.Host}}' 2>/dev/null
    )" || {
      restart_local_fail "Docker endpointを取得できません"
      return
    }
  fi

  case "${endpoint}" in
    unix:///*) ;;
    *)
      restart_local_fail \
        "remote Docker endpointではlocal環境を変更できません: ${endpoint}"
      return
      ;;
  esac
}

run_local_compose() {
  docker compose \
    --file "${RESTART_REPOSITORY_ROOT}/compose.yaml" \
    --file "${RESTART_REPOSITORY_ROOT}/compose.local-multi-api.yaml" \
    --project-directory "${RESTART_REPOSITORY_ROOT}" \
    --project-name "${RESTART_COMPOSE_PROJECT}" \
    "$@"
}

require_local_postgres_volume_available() {
  local containers container owner
  containers="$(docker ps -q --filter "volume=${RESTART_COMPOSE_PROJECT}_postgres-data")" || return
  while IFS= read -r container; do
    [[ -n "${container}" ]] || continue
    owner="$(docker inspect --format \
      '{{index .Config.Labels "com.docker.compose.project"}}|{{index .Config.Labels "com.docker.compose.service"}}' \
      "${container}")" || return
    if [[ "${owner}" != "${RESTART_COMPOSE_PROJECT}|postgres" ]]; then
      restart_local_fail '通常DBのvolumeを別環境が使用しています。TLS試験中なら make lan-server-stop の後に再起動してください。'
      return 1
    fi
  done <<<"${containers}"
}

wait_for_local_readiness() {
  local attempt
  local readiness
  local readiness_url="$1"

  for ((attempt = 1; attempt <= READINESS_MAX_ATTEMPTS; attempt += 1)); do
    if readiness="$(
      curl --fail --silent --show-error --max-time 3 "${readiness_url}"
    )"; then
      printf '%s\n' "${readiness}"
      return 0
    fi
    [[ "${attempt}" -eq "${READINESS_MAX_ATTEMPTS}" ]] || sleep 1
  done
  return 1
}

check_local_api_readiness() {
  local api_service="$1"
  case "${api_service}" in
    api | api-secondary) ;;
    *) restart_local_fail 'Readinessの対象APIが不正です' || return 2 ;;
  esac

  # gatewayの既存curlを使い、503でも秘密値を含まないhealth本文を表示する。
  # upstreamを固定し、負荷分散で片方だけの確認になることを避ける。
  printf '%s Readiness: ' "${api_service}"
  if run_local_compose exec -T gateway curl --fail-with-body --silent --show-error \
    --max-time 5 "http://${api_service}:8000/api/health/ready"; then
    printf '\n'
    return 0
  fi
  printf '\n' >&2
  printf 'Error: %sのReadiness確認が失敗しました\n' "${api_service}" >&2
  printf '%s\n' \
    'migrationがmissing/outdatedの場合はDBの準備が必要です。通常再起動はDBを変更しません。' \
    '通常ローカルDBを全削除してよい場合: make local-db-reset' \
    'データを保持する場合: docs/handover/README.md の「参加者IDの準備」で人手準備手順を確認してください。' >&2
  return 1
}

restart_local_main() {
  local should_bootstrap=false
  local should_update_frontend_only=false
  local should_repair_colima=false
  local gateway_display_endpoint
  local gateway_endpoint
  local gateway_port
  local readiness
  local readiness_url
  local api_service

  while [[ $# -gt 0 ]]; do
    case "$1" in
      --bootstrap) should_bootstrap=true ;;
      --frontend-only) should_update_frontend_only=true ;;
      --repair-colima) should_repair_colima=true ;;
      -h | --help)
        restart_local_usage
        return 0
        ;;
      *)
        restart_local_usage >&2
        return 2
        ;;
    esac
    shift
  done

  if [[ "${should_bootstrap}" == true && "${should_update_frontend_only}" == true ]]; then
    restart_local_fail "--frontend-onlyと--bootstrapは併用できません" || return 2
  fi

  require_restart_command docker || return
  require_restart_command curl || return
  require_local_docker_endpoint || return
  run_docker_preflight "${should_repair_colima}" || return
  cd "${RESTART_REPOSITORY_ROOT}"
  run_local_compose config --quiet || return

  if [[ "${should_update_frontend_only}" == true ]]; then
    run_local_compose build gateway || return
    # APIとDBを維持したまま、build済みの静的画面だけを置き換える。
    run_local_compose up \
      -d --no-deps --force-recreate --wait --wait-timeout 120 \
      gateway || return
  else
    require_local_postgres_volume_available || return
    if [[ "${should_bootstrap}" == true ]]; then
      printf 'bootstrap profileを含むimageをbuildします。\n'
      run_local_compose --profile bootstrap build api gateway migrate seed || return
    else
      # 両APIが同じimageを共有するため、secondaryの重複buildは行わない。
      run_local_compose build api gateway || return
    fi

    # build中のLAN切替も検出し、同じDBを別PostgreSQLへ同時mountしない。
    require_local_postgres_volume_available || return
    # DB volumeを残したままinfraを先に健康化し、appの接続失敗を避ける。
    run_local_compose up \
      -d --force-recreate --wait --wait-timeout 120 \
      postgres redis || return

    if [[ "${should_bootstrap}" == true ]]; then
      # migration中に旧appが部分適用状態のDBへ接続しないよう、両APIを停止する。
      run_local_compose stop gateway api api-secondary || return
      printf '専用local DBへmigrationと[DEMO] seedを適用します。\n'
      run_local_compose --profile bootstrap run --rm migrate || return
      run_local_compose --profile bootstrap run --rm seed || return
    fi

    run_local_compose up \
      -d --no-deps --force-recreate --wait --wait-timeout 120 \
      api api-secondary gateway || return
  fi

  # gateway経由の一度の成功では両APIの起動完了を保証できない。
  for api_service in api api-secondary; do
    check_local_api_readiness "${api_service}" || return
  done

  gateway_endpoint="$(run_local_compose port gateway 8080)" || {
    restart_local_fail "gatewayの公開portを取得できません"
    return
  }
  if [[ "${gateway_endpoint}" =~ ^(127\.0\.0\.1|0\.0\.0\.0):([0-9]+)$ ]]; then
    gateway_port="${BASH_REMATCH[2]}"
    gateway_display_endpoint="127.0.0.1:${gateway_port}"
  else
    restart_local_fail \
      "gatewayが期待するlocal公開先ではありません: ${gateway_endpoint}"
    return
  fi

  # 全interface bind時も、host自身からのhealth確認には到達可能なloopbackを使う。
  readiness_url="http://127.0.0.1:${gateway_port}/api/health/ready"
  if ! readiness="$(wait_for_local_readiness "${readiness_url}")"; then
    restart_local_fail \
      "Readiness確認が${READINESS_MAX_ATTEMPTS}回失敗しました。未bootstrapの専用local DBなら --bootstrap を明示してください"
    return
  fi

  if [[ "${should_update_frontend_only}" == true ]]; then
    printf 'Frontendの変更を反映しました: http://%s/\n' "${gateway_display_endpoint}"
  else
    printf 'Local環境を再起動しました: http://%s/\n' "${gateway_display_endpoint}"
  fi
  printf 'Readiness: %s\n' "${readiness}"
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  restart_local_main "$@"
fi

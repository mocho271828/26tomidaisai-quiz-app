#!/usr/bin/env bash

set -Eeuo pipefail

# shellcheck source-path=SCRIPTDIR
# shellcheck source=restart-local.sh
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/restart-local.sh"

readonly RESET_POSTGRES_VOLUME="gs-app-tomidaisai_postgres-data"

reset_local_db_usage() {
  cat <<'EOF'
Usage: ./scripts/reset-local-db.sh

通常HTTPのローカルDBを全削除し、設定済みの架空問題・運営アカウント・参加者ID割当を
作り直して2 APIと画面を起動します。既存の参加者・回答・得点は消えます。
対象: gs-app-tomidaisai / gs-app-tomidaisai_postgres-data / quiz_demo
TLS試験用DB・AWS・.local/round-backupsは変更しません。
EOF
}

require_reset_volume_owner() {
  local volume_names owner container_ids container_id
  volume_names="$(docker volume ls --format '{{.Name}}')" || return
  if ! printf '%s\n' "${volume_names}" | grep -Fx "${RESET_POSTGRES_VOLUME}" >/dev/null; then
    return 0
  fi
  owner="$(docker volume inspect "${RESET_POSTGRES_VOLUME}" --format \
    '{{index .Labels "com.docker.compose.project"}}|{{index .Labels "com.docker.compose.volume"}}')" || return
  [[ "${owner}" == "${RESTART_COMPOSE_PROJECT}|postgres-data" ]] || {
    restart_local_fail 'DB volumeの所有projectが一致しません'
    return
  }
  # 同じvolumeがTLS試験などへ接続されていれば、元の環境も停止しない。
  container_ids="$(docker ps -aq --filter "volume=${RESET_POSTGRES_VOLUME}")" || return
  while IFS= read -r container_id; do
    [[ -n "${container_id}" ]] || continue
    owner="$(docker inspect "${container_id}" --format \
      '{{index .Config.Labels "com.docker.compose.project"}}|{{index .Config.Labels "com.docker.compose.service"}}')" || return
    [[ "${owner}" == "${RESTART_COMPOSE_PROJECT}|postgres" ]] || {
      restart_local_fail 'DB volumeを別環境のcontainerが使用しています'
      return
    }
  done <<<"${container_ids}"
}

run_reset_psql() {
  run_local_compose exec -T postgres psql -X -v ON_ERROR_STOP=1 -U quiz -d quiz_demo "$@"
}

reset_local_db_main() {
  if [[ $# -eq 1 && ( "$1" == --help || "$1" == -h ) ]]; then
    reset_local_db_usage
    return 0
  fi
  if [[ $# -ne 0 ]]; then
    reset_local_db_usage >&2
    return 2
  fi

  local artifact_directory volume_names postgres_container mount_identity db_identity result api_service
  local expected_question_count
  require_restart_command docker || return
  require_restart_command curl || return
  require_local_docker_endpoint || return
  run_docker_preflight false || return
  run_local_compose --profile bootstrap config --quiet || return
  require_reset_volume_owner || return

  printf '==> 削除前にimageと参加者IDの準備SQLを作成します。\n'
  run_local_compose --profile bootstrap build api gateway migrate seed || return
  mkdir -p "${RESTART_REPOSITORY_ROOT}/.local" || return
  artifact_directory="$(mktemp -d "${RESTART_REPOSITORY_ROOT}/.local/db-reset.XXXXXX")" || return
  # SQL生成・設定検証を先に完了し、失敗時には既存DBへ手を付けない。
  if ! run_local_compose --profile bootstrap run --rm --no-deps -T seed python -c \
    'from quiz_app.bootstrap.demo_seed import DEMO_EVENT_ID, _validate_seed_configuration; from quiz_app.config import get_settings; from quiz_app.bootstrap.participant_id_pool_sql import build_pool_preparation_sql; _validate_seed_configuration(get_settings()); print(build_pool_preparation_sql(DEMO_EVENT_ID))' \
    >"${artifact_directory}/participant-id-pool.sql"; then
    restart_local_fail '初期化前の設定検証または参加者ID SQL生成が失敗しました（DBは未削除）'
    return
  fi
  chmod 600 "${artifact_directory}/participant-id-pool.sql" || return
  [[ -s "${artifact_directory}/participant-id-pool.sql" ]] || {
    restart_local_fail '参加者ID SQLが空です（DBは未削除）'
    return
  }
  if ! expected_question_count="$(run_local_compose --profile bootstrap run --rm --no-deps -T seed python -c \
    'from quiz_app.bootstrap.demo_content import DEMO_QUESTION_SEEDS; print(len(DEMO_QUESTION_SEEDS))')"; then
    restart_local_fail '初期化前の問題数検証が失敗しました（DBは未削除）'
    return
  fi
  [[ "${expected_question_count}" =~ ^([3-9]|[1-9][0-9]+)$ ]] || {
    restart_local_fail '初期化前の問題数が不正です（DBは未削除）'
    return
  }

  # build中に別環境がvolumeを接続していないか、停止直前に再確認する。
  require_reset_volume_owner || return
  printf '==> 通常ローカルDBを全削除します: %s\n' "${RESET_POSTGRES_VOLUME}"
  run_local_compose --profile bootstrap down || return
  volume_names="$(docker volume ls --format '{{.Name}}')" || return
  if printf '%s\n' "${volume_names}" | grep -Fx "${RESET_POSTGRES_VOLUME}" >/dev/null; then
    docker volume rm "${RESET_POSTGRES_VOLUME}" || return
  fi

  printf '==> 新しい空DBを起動し、接続先を確認します。\n'
  run_local_compose up -d --wait --wait-timeout 120 postgres redis || return
  postgres_container="$(run_local_compose ps --quiet postgres)" || return
  [[ -n "${postgres_container}" ]] || {
    restart_local_fail 'PostgreSQL containerを取得できません'
    return
  }
  mount_identity="$(docker inspect "${postgres_container}" --format \
    '{{range .Mounts}}{{if eq .Destination "/var/lib/postgresql/data"}}{{.Type}}|{{.Name}}{{end}}{{end}}')" || return
  [[ "${mount_identity}" == "volume|${RESET_POSTGRES_VOLUME}" ]] || {
    restart_local_fail '新DBのmountが初期化対象と一致しません'
    return
  }
  db_identity="$(run_reset_psql -Atc \
    "SELECT current_database() || '|' || current_user || '|' || (SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public')")" || return
  [[ "${db_identity}" == 'quiz_demo|quiz|0' ]] || {
    restart_local_fail '接続先が専用の空DBではありません'
    return
  }

  printf '==> 既存migration・設定済みの架空問題・運営アカウント・参加者IDを投入します。\n'
  run_local_compose --profile bootstrap run --rm --no-deps -T migrate || return
  run_local_compose --profile bootstrap run --rm --no-deps -T seed || return
  run_reset_psql <"${artifact_directory}/participant-id-pool.sql" || return

  # readinessはID poolの完成を保証しないため、採番を消費せず照合する。
  result="$(run_reset_psql -At -v "expected_question_count=${expected_question_count}" <<'SQL'
SELECT (SELECT count(*) FROM public.events) = 1
   AND (SELECT count(*) FROM public.questions) = :'expected_question_count'::bigint
   AND (SELECT count(*) FROM public.admin_accounts) = 1
   AND (SELECT count(*) FROM public.participants) = 0
   AND (SELECT count(*) FROM public.participant_id_pools WHERE capacity = 17576) = 1
   AND (SELECT count(*) = 17576 AND count(DISTINCT public_label) = 17576
               AND min(ordinal) = 1 AND max(ordinal) = 17576 FROM public.participant_id_slots)
   AND (SELECT last_value = 1 AND NOT is_called
          FROM public.participant_id_60000000000040008000000000000001);
SQL
)" || return
  [[ "${result}" == t ]] || {
    restart_local_fail '初期データまたは参加者ID割当の照合が失敗しました'
    return
  }

  printf '==> 2 APIと画面を起動します。\n'
  run_local_compose up -d --no-deps --wait --wait-timeout 120 api api-secondary gateway || return
  for api_service in api api-secondary; do
    check_local_api_readiness "${api_service}" || return
  done
  run_local_compose exec -T gateway curl --fail --silent --show-error --max-time 5 \
    --output /dev/null http://127.0.0.1:8080/api/health/ready || return
  printf 'ローカルDB初期化が完了しました。画面を再読込し、運営は再ログインしてください。\n'
  printf '準備SQL: %s/participant-id-pool.sql\n' "${artifact_directory}"
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  reset_local_db_main "$@"
fi

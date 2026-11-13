#!/usr/bin/env bash

set -Eeuo pipefail

RESET_TEST_SCRIPT_DIRECTORY="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
RESET_TEST_DIRECTORY="$(mktemp -d)"
readonly RESET_TEST_SCRIPT_DIRECTORY RESET_TEST_DIRECTORY
trap 'rm -rf -- "${RESET_TEST_DIRECTORY}"' EXIT

# readonlyなrepository rootもfixture内に閉じ、実DB・workspaceに書き込まない。
mkdir -p "${RESET_TEST_DIRECTORY}/scripts"
cp "${RESET_TEST_SCRIPT_DIRECTORY}/reset-local-db.sh" \
  "${RESET_TEST_SCRIPT_DIRECTORY}/restart-local.sh" "${RESET_TEST_DIRECTORY}/scripts/"
# shellcheck disable=SC1091
source "${RESET_TEST_DIRECTORY}/scripts/reset-local-db.sh"

RESET_CALLS_FILE="${RESET_TEST_DIRECTORY}/calls"
readonly RESET_CALLS_FILE

record_reset_call() {
  printf '%s\n' "$1" >>"${RESET_CALLS_FILE}"
  [[ "$1" != "${RESET_FAILURE}" ]]
}

run_docker_preflight() {
  [[ "$1" == false ]] || return 64
  record_reset_call preflight
}

curl() { return 0; }

docker() {
  case "$*" in
    'context show') printf 'fixture\n' ;;
    'context inspect fixture --format {{.Endpoints.docker.Host}}')
      printf '%s\n' "${RESET_ENDPOINT}"
      ;;
    'volume ls --format {{.Name}}')
      record_reset_call list-volumes || return
      [[ "${RESET_VOLUME_EXISTS}" == false ]] || printf '%s\n' "${RESET_POSTGRES_VOLUME}"
      ;;
    "volume inspect ${RESET_POSTGRES_VOLUME} --format "*)
      record_reset_call inspect-volume || return
      printf '%s\n' "${RESET_VOLUME_OWNER}"
      ;;
    "ps -aq --filter volume=${RESET_POSTGRES_VOLUME}")
      record_reset_call volume-users || return
      printf 'fixture-postgres\n'
      ;;
    'inspect fixture-postgres --format '*'.Config.Labels'*)
      record_reset_call inspect-owner || return
      printf '%s\n' "${RESET_CONTAINER_OWNER}"
      ;;
    "volume rm ${RESET_POSTGRES_VOLUME}") record_reset_call remove-volume ;;
    'inspect fixture-postgres --format '*'.Mounts'*)
      record_reset_call inspect-mount || return
      printf '%s\n' "${RESET_MOUNT}"
      ;;
    *) printf 'Unexpected Docker call: %s\n' "$*" >&2; return 64 ;;
  esac
}

run_local_compose() {
  local call="$*" sql
  case "${call}" in
    '--profile bootstrap config --quiet') record_reset_call config ;;
    '--profile bootstrap build api gateway migrate seed') record_reset_call build ;;
    '--profile bootstrap run --rm --no-deps -T seed python -c from quiz_app.bootstrap.demo_content import DEMO_QUESTION_SEEDS;'*)
      record_reset_call question-count || return
      printf '%s\n' "${RESET_QUESTION_COUNT}"
      ;;
    '--profile bootstrap run --rm --no-deps -T seed python -c '*)
      record_reset_call generate-sql || return
      [[ "${RESET_EMPTY_SQL}" == true ]] || printf '%s\n' '-- fixture pool SQL'
      ;;
    '--profile bootstrap down') record_reset_call down ;;
    'up -d --wait --wait-timeout 120 postgres redis') record_reset_call start-db ;;
    'ps --quiet postgres')
      record_reset_call postgres-container || return
      printf 'fixture-postgres\n'
      ;;
    'exec -T postgres psql '*'-Atc '*'current_database()'*)
      record_reset_call verify-empty-db || return
      printf '%s\n' "${RESET_DB_IDENTITY}"
      ;;
    '--profile bootstrap run --rm --no-deps -T migrate') record_reset_call migrate ;;
    '--profile bootstrap run --rm --no-deps -T seed') record_reset_call seed ;;
    'exec -T postgres psql '*'-At -v expected_question_count='*)
      record_reset_call verify-pool || return
      [[ "${call}" == *"-At -v expected_question_count=${RESET_QUESTION_COUNT}" ]] || return 64
      sql="$(cat)"
      [[ "${sql}" == *"(SELECT count(*) FROM public.questions) = :'expected_question_count'::bigint"* ]] || return 64
      printf '%s\n' "${RESET_POOL_VALID}"
      ;;
    'exec -T postgres psql '*)
      record_reset_call apply-sql || return
      [[ "$(cat)" == '-- fixture pool SQL' ]]
      ;;
    'up -d --no-deps --wait --wait-timeout 120 api api-secondary gateway') record_reset_call start-app ;;
    'exec -T gateway curl --fail-with-body --silent --show-error --max-time 5 http://api:8000/api/health/ready')
      printf '%s\n' "${RESET_READINESS_BODY}"
      record_reset_call ready-api
      ;;
    'exec -T gateway curl --fail-with-body --silent --show-error --max-time 5 http://api-secondary:8000/api/health/ready')
      printf '%s\n' "${RESET_READINESS_BODY}"
      record_reset_call ready-secondary
      ;;
    'exec -T gateway curl '*) record_reset_call ready-gateway ;;
    *) printf 'Unexpected Compose call: %s\n' "${call}" >&2; return 64 ;;
  esac
}

reset_reset_fakes() {
  RESET_FAILURE=''
  RESET_ENDPOINT='unix:///tmp/reset-fixture.sock'
  RESET_VOLUME_EXISTS=true
  RESET_VOLUME_OWNER="${RESTART_COMPOSE_PROJECT}|postgres-data"
  RESET_CONTAINER_OWNER="${RESTART_COMPOSE_PROJECT}|postgres"
  RESET_MOUNT="volume|${RESET_POSTGRES_VOLUME}"
  RESET_DB_IDENTITY='quiz_demo|quiz|0'
  RESET_POOL_VALID=t
  RESET_EMPTY_SQL=false
  RESET_QUESTION_COUNT=6
  RESET_READINESS_BODY='{"status":"ready","database":"ok","migration":"current","redis":"ok"}'
  : >"${RESET_CALLS_FILE}"
  unset DOCKER_HOST DOCKER_CONTEXT COMPOSE_FILE COMPOSE_PROJECT_NAME
}

run_reset() {
  if reset_local_db_main "$@" >"${RESET_TEST_DIRECTORY}/output" 2>&1; then
    RESET_STATUS=0
  else
    RESET_STATUS=$?
  fi
  RESET_OUTPUT="$(<"${RESET_TEST_DIRECTORY}/output")"
}

assert_reset_equal() {
  [[ "$1" == "$2" ]] || {
    printf 'Expected %q, got %q\n%s\n' "$2" "$1" "${RESET_OUTPUT}" >&2
    return 1
  }
}

assert_reset_absent() {
  if grep -Fxq -- "$1" "${RESET_CALLS_FILE}"; then
    printf 'Unexpected phase: %s\n%s\n' "$1" "${RESET_OUTPUT}" >&2
    return 1
  fi
}

assert_reset_failed() {
  [[ "${RESET_STATUS}" -ne 0 && "${RESET_OUTPUT}" != *'初期化が完了'* ]] || {
    printf 'Expected failure without success output: %s\n' "${RESET_OUTPUT}" >&2
    return 1
  }
}

assert_reset_order() {
  local phase index previous_index=0
  for phase in "$@"; do
    index="$(grep -nFx -- "${phase}" "${RESET_CALLS_FILE}" | head -1 | cut -d: -f1)"
    [[ -n "${index}" && "${index}" -gt "${previous_index}" ]] || {
      printf 'Missing or out-of-order phase: %s\n' "${phase}" >&2
      return 1
    }
    previous_index="${index}"
  done
}

reset_reset_fakes
run_reset
assert_reset_equal "${RESET_STATUS}" 0
[[ "${RESET_OUTPUT}" == *'初期化が完了'* ]]
assert_reset_order preflight config inspect-volume inspect-owner build generate-sql question-count down \
  remove-volume start-db inspect-mount verify-empty-db migrate seed apply-sql verify-pool \
  start-app ready-api ready-secondary ready-gateway
assert_reset_equal "$(grep -cFx inspect-volume "${RESET_CALLS_FILE}")" 2

for question_count in 3 7 8 12; do
  reset_reset_fakes
  RESET_QUESTION_COUNT="${question_count}"
  run_reset
  assert_reset_equal "${RESET_STATUS}" 0
  assert_reset_order question-count down verify-pool start-app
done

reset_reset_fakes
RESET_VOLUME_EXISTS=false
run_reset
assert_reset_equal "${RESET_STATUS}" 0
assert_reset_absent remove-volume
assert_reset_absent inspect-volume
assert_reset_order down start-db verify-empty-db migrate seed verify-pool start-app

# 削除前の失敗では既存DBを停止・削除しない。
for phase in preflight config list-volumes inspect-volume volume-users inspect-owner build generate-sql question-count; do
  reset_reset_fakes
  RESET_FAILURE="${phase}"
  run_reset
  assert_reset_failed
  assert_reset_absent down
  assert_reset_absent remove-volume
  assert_reset_absent start-app
done
for invalid_count in '' 0 1 2 -3 03 3.0 '3; SELECT 1' $'3\n4'; do
  reset_reset_fakes
  RESET_QUESTION_COUNT="${invalid_count}"
  run_reset
  assert_reset_failed
  assert_reset_absent down
  assert_reset_absent remove-volume
  assert_reset_absent start-app
done
for invalid_state in volume-owner container-owner endpoint empty-sql; do
  reset_reset_fakes
  case "${invalid_state}" in
    volume-owner) RESET_VOLUME_OWNER='other-project|postgres-data' ;;
    container-owner) RESET_CONTAINER_OWNER='tls-fixture|postgres' ;;
    endpoint) RESET_ENDPOINT='ssh://remote.example.test' ;;
    empty-sql) RESET_EMPTY_SQL=true ;;
  esac
  run_reset
  assert_reset_failed
  assert_reset_absent down
  assert_reset_absent remove-volume
done

# 初期化の途中で失敗した場合、後続処理とアプリ起動に進まない。
for transition in 'down:remove-volume' 'remove-volume:start-db' 'start-db:inspect-mount' \
  'postgres-container:inspect-mount' 'inspect-mount:verify-empty-db' \
  'verify-empty-db:migrate' 'migrate:seed' 'seed:apply-sql' 'apply-sql:verify-pool' 'verify-pool:start-app'; do
  reset_reset_fakes
  RESET_FAILURE="${transition%%:*}"
  run_reset
  assert_reset_failed
  assert_reset_absent "${transition#*:}"
  assert_reset_absent start-app
done
for invalid_state in mount nonempty-db wrong-db incomplete-pool; do
  reset_reset_fakes
  case "${invalid_state}" in
    mount) RESET_MOUNT='volume|tls-postgres-data' ;;
    nonempty-db) RESET_DB_IDENTITY='quiz_demo|quiz|1' ;;
    wrong-db) RESET_DB_IDENTITY='production|quiz|0' ;;
    incomplete-pool) RESET_POOL_VALID=f ;;
  esac
  run_reset
  assert_reset_failed
  assert_reset_absent start-app
  [[ "${invalid_state}" == incomplete-pool ]] || assert_reset_absent migrate
done
for phase in start-app ready-api ready-secondary ready-gateway; do
  reset_reset_fakes
  RESET_FAILURE="${phase}"
  run_reset
  assert_reset_failed
  case "${phase}" in
    start-app | ready-api) assert_reset_absent ready-secondary ;;
  esac
  [[ "${phase}" == ready-gateway ]] || assert_reset_absent ready-gateway
done

reset_reset_fakes
RESET_FAILURE=ready-api
RESET_READINESS_BODY='{"status":"not_ready","database":"ok","migration":"outdated","redis":"ok"}'
run_reset
assert_reset_failed
[[ "${RESET_OUTPUT}" == *"${RESET_READINESS_BODY}"* ]]
[[ "${RESET_OUTPUT}" == *'apiのReadiness確認が失敗しました'* ]]
assert_reset_absent ready-secondary
assert_reset_equal "$(grep -cFx remove-volume "${RESET_CALLS_FILE}")" 1
assert_reset_equal "$(grep -cFx migrate "${RESET_CALLS_FILE}")" 1
assert_reset_equal "$(grep -cFx seed "${RESET_CALLS_FILE}")" 1

reset_reset_fakes
RESET_READINESS_BODY='{"status":"degraded","database":"ok","migration":"current","redis":"degraded"}'
run_reset
assert_reset_equal "${RESET_STATUS}" 0
[[ "${RESET_OUTPUT}" == *"api Readiness: ${RESET_READINESS_BODY}"* ]]
[[ "${RESET_OUTPUT}" == *"api-secondary Readiness: ${RESET_READINESS_BODY}"* ]]

for help_option in --help -h; do
  reset_reset_fakes
  run_reset "${help_option}"
  assert_reset_equal "${RESET_STATUS}" 0
  assert_reset_equal "$(<"${RESET_CALLS_FILE}")" ''
done
for invalid_option in --unknown --bootstrap; do
  reset_reset_fakes
  run_reset "${invalid_option}"
  assert_reset_equal "${RESET_STATUS}" 2
  assert_reset_equal "$(<"${RESET_CALLS_FILE}")" ''
done
reset_reset_fakes
run_reset --help extra
assert_reset_equal "${RESET_STATUS}" 2
assert_reset_equal "$(<"${RESET_CALLS_FILE}")" ''

printf 'Local DB reset tests passed.\n'

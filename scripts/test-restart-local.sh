#!/usr/bin/env bash

set -Eeuo pipefail

TEST_SCRIPT_DIRECTORY="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
readonly TEST_SCRIPT_DIRECTORY
TEST_TEMP_DIRECTORY="$(mktemp -d)"
readonly TEST_TEMP_DIRECTORY
trap 'rm -rf "${TEST_TEMP_DIRECTORY}"' EXIT

# shellcheck disable=SC1091
source "${TEST_SCRIPT_DIRECTORY}/restart-local.sh"

TEST_DOCKER_CONTEXT="colima"
DOCKER_ENDPOINT="unix:///tmp/test/.colima/default/docker.sock"
DOCKER_FAILURE_PATTERN=""
DOCKER_FAILURE_OUTPUT=""
POSTGRES_VOLUME_USERS=""
POSTGRES_VOLUME_OWNER="${RESTART_COMPOSE_PROJECT}|postgres"
VOLUME_USERS_AFTER_BUILD=""
READINESS_BODY='{"status":"ready","database":"ok","migration":"current","redis":"ok"}'
GATEWAY_ENDPOINT="127.0.0.1:8080"
PREFLIGHT_ARGUMENT=""
PREFLIGHT_SUCCEEDS=true
READINESS_FAILURES_BEFORE_SUCCESS=0
RESTART_OUTPUT=""
RESTART_STATUS=0
CURL_COUNT_FILE="${TEST_TEMP_DIRECTORY}/curl-count"
DOCKER_CALLS_FILE="${TEST_TEMP_DIRECTORY}/docker-calls"
COMPOSE_COMMAND_PREFIX="compose --file ${RESTART_REPOSITORY_ROOT}/compose.yaml --file ${RESTART_REPOSITORY_ROOT}/compose.local-multi-api.yaml --project-directory ${RESTART_REPOSITORY_ROOT} --project-name ${RESTART_COMPOSE_PROJECT}"

docker() {
  local call="$*"

  printf '%s\n' "${call}" >>"${DOCKER_CALLS_FILE}"
  if [[ -n "${DOCKER_FAILURE_PATTERN}" && "${call}" == *"${DOCKER_FAILURE_PATTERN}"* ]]; then
    printf '%s\n' "${DOCKER_FAILURE_OUTPUT}"
    return 1
  fi
  case "${call}" in
    "context show") printf '%s\n' "${TEST_DOCKER_CONTEXT}" ;;
    "context inspect ${DOCKER_CONTEXT:-${TEST_DOCKER_CONTEXT}} --format {{.Endpoints.docker.Host}}")
      printf '%s\n' "${DOCKER_ENDPOINT}"
      ;;
    "ps -q --filter volume=${RESTART_COMPOSE_PROJECT}_postgres-data")
      printf '%s\n' "${POSTGRES_VOLUME_USERS}"
      ;;
    "inspect --format "*) printf '%s\n' "${POSTGRES_VOLUME_OWNER}" ;;
    *" build api gateway"*)
      if [[ -n "${VOLUME_USERS_AFTER_BUILD}" ]]; then POSTGRES_VOLUME_USERS="${VOLUME_USERS_AFTER_BUILD}"; fi
      ;;
    *" port gateway 8080") printf '%s\n' "${GATEWAY_ENDPOINT}" ;;
    *" exec -T gateway curl --fail-with-body --silent --show-error --max-time 5 http://api:8000/api/health/ready" | \
      *" exec -T gateway curl --fail-with-body --silent --show-error --max-time 5 http://api-secondary:8000/api/health/ready")
      printf '%s\n' "${READINESS_BODY}"
      ;;
  esac
}

curl() {
  local call_count

  call_count="$(<"${CURL_COUNT_FILE}")"
  call_count=$((call_count + 1))
  printf '%s\n' "${call_count}" >"${CURL_COUNT_FILE}"
  if [[ "${call_count}" -le "${READINESS_FAILURES_BEFORE_SUCCESS}" ]]; then
    return 22
  fi
  printf '%s\n' "${READINESS_BODY}"
}

sleep() {
  return 0
}

run_docker_preflight() {
  PREFLIGHT_ARGUMENT="$1"
  [[ "${PREFLIGHT_SUCCEEDS}" == true ]]
}

assert_equal() {
  local actual="$1"
  local expected="$2"

  [[ "${actual}" == "${expected}" ]] || {
    printf 'Expected %q, got %q\n' "${expected}" "${actual}" >&2
    return 1
  }
}

assert_contains() {
  local actual="$1"
  local expected="$2"

  [[ "${actual}" == *"${expected}"* ]] || {
    printf 'Expected output to contain %q\n' "${expected}" >&2
    return 1
  }
}

assert_not_contains() {
  local actual="$1"
  local unexpected="$2"

  [[ "${actual}" != *"${unexpected}"* ]] || {
    printf 'Expected output not to contain %q\n' "${unexpected}" >&2
    return 1
  }
}

call_index() {
  local call
  local expected="$1"
  local index=0

  while IFS= read -r call; do
    if [[ "${call}" == *"${expected}"* ]]; then
      printf '%s\n' "${index}"
      return 0
    fi
    index=$((index + 1))
  done <"${DOCKER_CALLS_FILE}"
  printf 'Expected docker call containing %q\n' "${expected}" >&2
  return 1
}

assert_less_than() {
  local first="$1"
  local second="$2"

  [[ "${first}" -lt "${second}" ]] || {
    printf 'Expected %s to be less than %s\n' "${first}" "${second}" >&2
    return 1
  }
}

reset_restart_fakes() {
  TEST_DOCKER_CONTEXT="colima"
  DOCKER_ENDPOINT="unix:///tmp/test/.colima/default/docker.sock"
  DOCKER_FAILURE_PATTERN=""
  DOCKER_FAILURE_OUTPUT=""
  POSTGRES_VOLUME_USERS=""
  POSTGRES_VOLUME_OWNER="${RESTART_COMPOSE_PROJECT}|postgres"
  VOLUME_USERS_AFTER_BUILD=""
  READINESS_BODY='{"status":"ready","database":"ok","migration":"current","redis":"ok"}'
  GATEWAY_ENDPOINT="127.0.0.1:8080"
  PREFLIGHT_ARGUMENT=""
  PREFLIGHT_SUCCEEDS=true
  READINESS_FAILURES_BEFORE_SUCCESS=0
  printf '0\n' >"${CURL_COUNT_FILE}"
  : >"${DOCKER_CALLS_FILE}"
  unset COMPOSE_FILE COMPOSE_PROJECT_NAME DOCKER_HOST DOCKER_CONTEXT
}

run_restart() {
  local output_file="${TEST_TEMP_DIRECTORY}/restart-output"

  if restart_local_main "$@" >"${output_file}" 2>&1; then
    RESTART_STATUS=0
  else
    RESTART_STATUS=$?
  fi
  RESTART_OUTPUT="$(<"${output_file}")"
}

reset_restart_fakes
run_restart
assert_equal "${RESTART_STATUS}" "0"
assert_equal "${PREFLIGHT_ARGUMENT}" "false"
DEFAULT_CALLS="$(<"${DOCKER_CALLS_FILE}")"
assert_contains "${DEFAULT_CALLS}" "${COMPOSE_COMMAND_PREFIX} config --quiet"
assert_contains "${DEFAULT_CALLS}" "${COMPOSE_COMMAND_PREFIX} build api gateway"
assert_contains "${DEFAULT_CALLS}" \
  "${COMPOSE_COMMAND_PREFIX} up -d --force-recreate --wait --wait-timeout 120 postgres redis"
assert_contains "${DEFAULT_CALLS}" \
  "${COMPOSE_COMMAND_PREFIX} up -d --no-deps --force-recreate --wait --wait-timeout 120 api api-secondary gateway"
for api_service in api api-secondary; do
  assert_contains "${DEFAULT_CALLS}" \
    "${COMPOSE_COMMAND_PREFIX} exec -T gateway curl --fail-with-body --silent --show-error --max-time 5 http://${api_service}:8000/api/health/ready"
done
assert_not_contains "${DEFAULT_CALLS}" "profile bootstrap run"
assert_not_contains "${DEFAULT_CALLS}" "compose down"
assert_not_contains "${DEFAULT_CALLS}" "-v"
assert_contains "${RESTART_OUTPUT}" "http://127.0.0.1:8080/"
assert_contains "${RESTART_OUTPUT}" '"status":"ready"'

reset_restart_fakes
run_restart --frontend-only
assert_equal "${RESTART_STATUS}" "0"
FRONTEND_CALLS="$(<"${DOCKER_CALLS_FILE}")"
assert_contains "${FRONTEND_CALLS}" "${COMPOSE_COMMAND_PREFIX} build gateway"
assert_contains "${FRONTEND_CALLS}" \
  "${COMPOSE_COMMAND_PREFIX} up -d --no-deps --force-recreate --wait --wait-timeout 120 gateway"
assert_not_contains "${FRONTEND_CALLS}" "build api"
assert_not_contains "${FRONTEND_CALLS}" "postgres"
assert_not_contains "${FRONTEND_CALLS}" "redis"
assert_not_contains "${FRONTEND_CALLS}" "120 api"
assert_contains "${FRONTEND_CALLS}" \
  "${COMPOSE_COMMAND_PREFIX} exec -T gateway curl --fail-with-body --silent --show-error --max-time 5 http://api-secondary:8000/api/health/ready"
assert_not_contains "${FRONTEND_CALLS}" "profile bootstrap"
assert_not_contains "${FRONTEND_CALLS}" " stop "
assert_not_contains "${FRONTEND_CALLS}" " down"
assert_contains "${RESTART_OUTPUT}" "Frontendの変更を反映しました"
assert_equal "$(<"${CURL_COUNT_FILE}")" "1"

reset_restart_fakes
run_restart --frontend-only --bootstrap
assert_equal "${RESTART_STATUS}" "2"
assert_equal "$(<"${DOCKER_CALLS_FILE}")" ""
assert_contains "${RESTART_OUTPUT}" "併用できません"

reset_restart_fakes
DOCKER_FAILURE_PATTERN="${COMPOSE_COMMAND_PREFIX} build gateway"
run_restart --frontend-only
assert_equal "${RESTART_STATUS}" "1"
assert_not_contains "$(<"${DOCKER_CALLS_FILE}")" " up "
assert_equal "$(<"${CURL_COUNT_FILE}")" "0"

reset_restart_fakes
run_restart --repair-colima --bootstrap
assert_equal "${RESTART_STATUS}" "0"
assert_equal "${PREFLIGHT_ARGUMENT}" "true"
BOOTSTRAP_CALLS="$(<"${DOCKER_CALLS_FILE}")"
assert_contains "${BOOTSTRAP_CALLS}" "${COMPOSE_COMMAND_PREFIX} --profile bootstrap build api gateway migrate seed"
assert_contains "${BOOTSTRAP_CALLS}" "${COMPOSE_COMMAND_PREFIX} stop gateway api api-secondary"
assert_contains "${BOOTSTRAP_CALLS}" \
  "${COMPOSE_COMMAND_PREFIX} --profile bootstrap run --rm migrate"
assert_contains "${BOOTSTRAP_CALLS}" \
  "${COMPOSE_COMMAND_PREFIX} --profile bootstrap run --rm seed"
BUILD_INDEX="$(call_index "--profile bootstrap build")"
INFRA_INDEX="$(call_index "up -d --force-recreate --wait --wait-timeout 120 postgres redis")"
STOP_INDEX="$(call_index "stop gateway api api-secondary")"
MIGRATE_INDEX="$(call_index "--profile bootstrap run --rm migrate")"
SEED_INDEX="$(call_index "--profile bootstrap run --rm seed")"
APP_INDEX="$(call_index "up -d --no-deps --force-recreate --wait --wait-timeout 120 api api-secondary gateway")"
assert_less_than "${BUILD_INDEX}" "${INFRA_INDEX}"
assert_less_than "${INFRA_INDEX}" "${STOP_INDEX}"
assert_less_than "${STOP_INDEX}" "${MIGRATE_INDEX}"
assert_less_than "${MIGRATE_INDEX}" "${SEED_INDEX}"
assert_less_than "${SEED_INDEX}" "${APP_INDEX}"

for unavailable_api in api api-secondary; do
  reset_restart_fakes
  DOCKER_FAILURE_PATTERN="http://${unavailable_api}:8000/api/health/ready"
  DOCKER_FAILURE_OUTPUT='{"status":"not_ready","database":"ok","migration":"outdated","redis":"ok"}'
  run_restart
  assert_equal "${RESTART_STATUS}" "1"
  assert_contains "${RESTART_OUTPUT}" "${unavailable_api}のReadiness確認が失敗しました"
  assert_contains "${RESTART_OUTPUT}" "${DOCKER_FAILURE_OUTPUT}"
  assert_contains "${RESTART_OUTPUT}" 'make local-db-reset'
  assert_contains "${RESTART_OUTPUT}" '人手準備手順'
  assert_not_contains "${RESTART_OUTPUT}" "Local環境を再起動しました"
  assert_not_contains "$(<"${DOCKER_CALLS_FILE}")" 'profile bootstrap'
  assert_not_contains "$(<"${DOCKER_CALLS_FILE}")" 'volume rm'
  assert_not_contains "$(<"${DOCKER_CALLS_FILE}")" ' down'
  assert_equal "$(<"${CURL_COUNT_FILE}")" "0"
done

reset_restart_fakes
DOCKER_FAILURE_PATTERN="http://api-secondary:8000/api/health/ready"
run_restart --frontend-only
assert_equal "${RESTART_STATUS}" "1"
assert_not_contains "${RESTART_OUTPUT}" "Frontendの変更を反映しました"

reset_restart_fakes
READINESS_BODY='{"status":"degraded","database":"ok","migration":"current","redis":"degraded"}'
run_restart
assert_equal "${RESTART_STATUS}" "0"
assert_contains "${RESTART_OUTPUT}" "api Readiness: ${READINESS_BODY}"
assert_contains "${RESTART_OUTPUT}" "api-secondary Readiness: ${READINESS_BODY}"
assert_contains "${RESTART_OUTPUT}" 'Local環境を再起動しました'

reset_restart_fakes
if RESTART_OUTPUT="$(check_local_api_readiness unrelated-service 2>&1)"; then
  RESTART_STATUS=0
else
  RESTART_STATUS=$?
fi
assert_equal "${RESTART_STATUS}" "2"
assert_contains "${RESTART_OUTPUT}" 'Readinessの対象APIが不正です'
assert_equal "$(<"${DOCKER_CALLS_FILE}")" ""

reset_restart_fakes
READINESS_FAILURES_BEFORE_SUCCESS=2
run_restart
assert_equal "${RESTART_STATUS}" "0"
assert_equal "$(<"${CURL_COUNT_FILE}")" "3"

reset_restart_fakes
READINESS_FAILURES_BEFORE_SUCCESS=10
run_restart
assert_equal "${RESTART_STATUS}" "1"
assert_contains "${RESTART_OUTPUT}" "--bootstrap"
assert_equal "$(<"${CURL_COUNT_FILE}")" "10"

reset_restart_fakes
GATEWAY_ENDPOINT="0.0.0.0:8080"
run_restart
assert_equal "${RESTART_STATUS}" "0"
assert_contains "${RESTART_OUTPUT}" "http://127.0.0.1:8080/"

reset_restart_fakes
GATEWAY_ENDPOINT="192.0.2.10:8080"
run_restart
assert_equal "${RESTART_STATUS}" "1"
assert_contains "${RESTART_OUTPUT}" "local公開先"

reset_restart_fakes
DOCKER_FAILURE_PATTERN="${COMPOSE_COMMAND_PREFIX} build"
run_restart
assert_equal "${RESTART_STATUS}" "1"
BUILD_FAILURE_CALLS="$(<"${DOCKER_CALLS_FILE}")"
assert_not_contains "${BUILD_FAILURE_CALLS}" " up "
assert_not_contains "${BUILD_FAILURE_CALLS}" " stop "

reset_restart_fakes
export DOCKER_HOST="tcp://remote.example.test:2376"
run_restart
assert_equal "${RESTART_STATUS}" "1"
assert_equal "$(<"${DOCKER_CALLS_FILE}")" ""
assert_contains "${RESTART_OUTPUT}" "remote Docker endpoint"

reset_restart_fakes
export DOCKER_CONTEXT="remote-fixture"
export DOCKER_HOST="unix:///tmp/test/otherwise-local.sock"
DOCKER_ENDPOINT="ssh://operator@remote.example.test"
run_restart
assert_equal "${RESTART_STATUS}" "1"
CONTEXT_OVERRIDE_CALLS="$(<"${DOCKER_CALLS_FILE}")"
assert_contains "${CONTEXT_OVERRIDE_CALLS}" "context inspect remote-fixture"
assert_not_contains "${CONTEXT_OVERRIDE_CALLS}" "${COMPOSE_COMMAND_PREFIX}"
assert_equal "${PREFLIGHT_ARGUMENT}" ""

reset_restart_fakes
export DOCKER_CONTEXT="local-fixture"
export DOCKER_HOST="ssh://operator@ignored.example.test"
run_restart
assert_equal "${RESTART_STATUS}" "0"
assert_contains "$(<"${DOCKER_CALLS_FILE}")" "context inspect local-fixture"

reset_restart_fakes
export DOCKER_HOST="unix:///tmp/test/explicit.sock"
DOCKER_ENDPOINT="ssh://operator@unused-context.example.test"
run_restart
assert_equal "${RESTART_STATUS}" "0"
assert_not_contains "$(<"${DOCKER_CALLS_FILE}")" "context "

reset_restart_fakes
DOCKER_FAILURE_PATTERN="context inspect colima"
run_restart
assert_equal "${RESTART_STATUS}" "1"
assert_not_contains "$(<"${DOCKER_CALLS_FILE}")" "${COMPOSE_COMMAND_PREFIX}"
assert_contains "${RESTART_OUTPUT}" "Docker endpointを取得できません"

reset_restart_fakes
DOCKER_ENDPOINT="ssh://operator@remote.example.test"
run_restart
assert_equal "${RESTART_STATUS}" "1"
REMOTE_CONTEXT_CALLS="$(<"${DOCKER_CALLS_FILE}")"
assert_not_contains "${REMOTE_CONTEXT_CALLS}" "${COMPOSE_COMMAND_PREFIX}"
assert_contains "${RESTART_OUTPUT}" "remote Docker endpoint"

reset_restart_fakes
export COMPOSE_FILE="/tmp/unrelated-compose.yaml"
export COMPOSE_PROJECT_NAME="unrelated-project"
cd "${TEST_TEMP_DIRECTORY}"
run_restart
assert_equal "${RESTART_STATUS}" "0"
FIXED_PROJECT_CALLS="$(<"${DOCKER_CALLS_FILE}")"
assert_contains "${FIXED_PROJECT_CALLS}" "${COMPOSE_COMMAND_PREFIX} config --quiet"
assert_not_contains "${FIXED_PROJECT_CALLS}" "unrelated-compose"
assert_not_contains "${FIXED_PROJECT_CALLS}" "unrelated-project"

reset_restart_fakes
PREFLIGHT_SUCCEEDS=false
run_restart
assert_equal "${RESTART_STATUS}" "1"
PREFLIGHT_FAILURE_CALLS="$(<"${DOCKER_CALLS_FILE}")"
assert_not_contains "${PREFLIGHT_FAILURE_CALLS}" "${COMPOSE_COMMAND_PREFIX}"

reset_restart_fakes
run_restart --unknown
assert_equal "${RESTART_STATUS}" "2"
assert_equal "$(<"${DOCKER_CALLS_FILE}")" ""

reset_restart_fakes
run_restart --help
assert_equal "${RESTART_STATUS}" "0"
assert_equal "$(<"${DOCKER_CALLS_FILE}")" ""
assert_contains "${RESTART_OUTPUT}" "Usage:"

for restart_mode in full bootstrap; do
  reset_restart_fakes
  POSTGRES_VOLUME_USERS='tls-postgres'
  POSTGRES_VOLUME_OWNER='gs-app-tomidaisai-tls|postgres'
  if [[ "${restart_mode}" == bootstrap ]]; then run_restart --bootstrap; else run_restart; fi
  assert_equal "${RESTART_STATUS}" "1"
  assert_contains "${RESTART_OUTPUT}" 'make lan-server-stop'
  SHARED_VOLUME_CALLS="$(<"${DOCKER_CALLS_FILE}")"
  assert_not_contains "${SHARED_VOLUME_CALLS}" ' build '
  assert_not_contains "${SHARED_VOLUME_CALLS}" ' up '
  assert_not_contains "${SHARED_VOLUME_CALLS}" ' stop '
  assert_not_contains "${SHARED_VOLUME_CALLS}" ' down'
done

reset_restart_fakes
POSTGRES_VOLUME_USERS='normal-postgres'
run_restart
assert_equal "${RESTART_STATUS}" "0"
assert_equal "$(grep -c '^ps -q --filter volume=' "${DOCKER_CALLS_FILE}")" "2"

for wrong_owner in 'other-project|postgres' 'gs-app-tomidaisai|unrelated-service' '|'; do
  reset_restart_fakes
  POSTGRES_VOLUME_USERS='unexpected-container'
  POSTGRES_VOLUME_OWNER="${wrong_owner}"
  run_restart
  assert_equal "${RESTART_STATUS}" "1"
  assert_not_contains "$(<"${DOCKER_CALLS_FILE}")" ' build '
  assert_not_contains "$(<"${DOCKER_CALLS_FILE}")" ' up '
done

for failed_probe in 'ps -q --filter volume=' 'inspect --format'; do
  reset_restart_fakes
  POSTGRES_VOLUME_USERS='container-to-inspect'
  DOCKER_FAILURE_PATTERN="${failed_probe}"
  run_restart
  assert_equal "${RESTART_STATUS}" "1"
  assert_not_contains "$(<"${DOCKER_CALLS_FILE}")" ' build '
  assert_not_contains "$(<"${DOCKER_CALLS_FILE}")" ' up '
done

reset_restart_fakes
VOLUME_USERS_AFTER_BUILD='tls-started-during-build'
POSTGRES_VOLUME_OWNER='gs-app-tomidaisai-tls|postgres'
run_restart
assert_equal "${RESTART_STATUS}" "1"
assert_contains "$(<"${DOCKER_CALLS_FILE}")" ' build api gateway'
assert_not_contains "$(<"${DOCKER_CALLS_FILE}")" ' up '

reset_restart_fakes
POSTGRES_VOLUME_USERS='tls-postgres'
POSTGRES_VOLUME_OWNER='gs-app-tomidaisai-tls|postgres'
run_restart --frontend-only
assert_equal "${RESTART_STATUS}" "0"
assert_not_contains "$(<"${DOCKER_CALLS_FILE}")" 'ps -q --filter volume='
assert_not_contains "$(<"${DOCKER_CALLS_FILE}")" '120 postgres redis'

printf 'Local restart script tests passed.\n'

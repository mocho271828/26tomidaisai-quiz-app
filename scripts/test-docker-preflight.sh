#!/usr/bin/env bash

set -Eeuo pipefail

SCRIPT_DIRECTORY="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
readonly SCRIPT_DIRECTORY
TEST_TEMP_DIRECTORY="$(mktemp -d)"
readonly TEST_TEMP_DIRECTORY
export TMPDIR="${TEST_TEMP_DIRECTORY}"
trap 'rm -rf "${TEST_TEMP_DIRECTORY}"' EXIT

# 実行時に同じdirectoryのscriptを絶対pathで読む。
# shellcheck disable=SC1091
source "${SCRIPT_DIRECTORY}/docker-preflight.sh"

unset DOCKER_CONTEXT DOCKER_HOST
TEST_DOCKER_CONTEXT="colima"
DOCKER_ENDPOINT="unix:///tmp/test/.colima/default/docker.sock"
DOCKER_FAILURE_PATTERN=""
DOCKER_CALLS_FILE="${TEST_TEMP_DIRECTORY}/docker-calls"
CURL_CALLS_FILE="${TEST_TEMP_DIRECTORY}/curl-calls"
DOCKER_HEALTHY=false
COLIMA_ACTION=""
COLIMA_ACTION_SUCCEEDS=true
COLIMA_COMMAND=""
COLIMA_PROFILE_NAME="default"
COLIMA_STATUS="Running"
CURL_AVAILABLE=true
GUEST_DOCKER_HEALTHY=true
RECOVERY_RESTORES_DOCKER=true

docker() {
  printf '%s\n' "$*" >>"${DOCKER_CALLS_FILE}"
  if [[ -n "${DOCKER_FAILURE_PATTERN}" && "$*" == *"${DOCKER_FAILURE_PATTERN}"* ]]; then
    return 1
  fi
  case "${1:-} ${2:-}" in
    "compose version") return 0 ;;
    "version --format") [[ "${DOCKER_HEALTHY}" == true ]] ;;
    "context show") printf '%s\n' "${TEST_DOCKER_CONTEXT}" ;;
    "context inspect") printf '%s\n' "${DOCKER_ENDPOINT}" ;;
    *) return 64 ;;
  esac
}

curl() {
  printf '%s\n' "$*" >>"${CURL_CALLS_FILE}"
  [[ "${DOCKER_HEALTHY}" == true ]] && printf 'OK'
}

docker_unix_probe_is_available() {
  [[ "${CURL_AVAILABLE}" == true ]]
}

colima() {
  COLIMA_COMMAND="$*"
  case "${1:-}" in
    list)
      printf '{"name":"%s","status":"%s"}\n' \
        "${COLIMA_PROFILE_NAME}" "${COLIMA_STATUS}"
      ;;
    ssh) [[ "${GUEST_DOCKER_HEALTHY}" == true ]] ;;
    restart)
      COLIMA_ACTION="restart"
      [[ "${COLIMA_ACTION_SUCCEEDS}" == true ]] || return 1
      if [[ "${RECOVERY_RESTORES_DOCKER}" == true ]]; then
        DOCKER_HEALTHY=true
      fi
      return 0
      ;;
    start)
      COLIMA_ACTION="start"
      [[ "${COLIMA_ACTION_SUCCEEDS}" == true ]] || return 1
      if [[ "${RECOVERY_RESTORES_DOCKER}" == true ]]; then
        DOCKER_HEALTHY=true
      fi
      return 0
      ;;
    *) return 64 ;;
  esac
}

sleep() {
  return 0
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

run_preflight() {
  local output_file="${TEST_TEMP_DIRECTORY}/preflight-output"

  : >"${DOCKER_CALLS_FILE}"
  : >"${CURL_CALLS_FILE}"
  if docker_preflight_main "$@" >"${output_file}" 2>&1; then
    PREFLIGHT_STATUS=0
  else
    PREFLIGHT_STATUS=$?
  fi
  PREFLIGHT_OUTPUT="$(<"${output_file}")"
}

DOCKER_HEALTHY=true
run_preflight
assert_equal "${PREFLIGHT_STATUS}" "0"
assert_contains "${PREFLIGHT_OUTPUT}" "Docker preflight passed"

DOCKER_HEALTHY=false
COLIMA_ACTION=""
run_preflight
assert_equal "${PREFLIGHT_STATUS}" "1"
assert_equal "${COLIMA_ACTION}" ""
assert_contains "${PREFLIGHT_OUTPUT}" "--repair-colima"

DOCKER_HEALTHY=false
COLIMA_ACTION=""
CURL_AVAILABLE=false
run_preflight --repair-colima
assert_equal "${PREFLIGHT_STATUS}" "1"
assert_equal "${COLIMA_ACTION}" ""
assert_contains "${PREFLIGHT_OUTPUT}" "curl command"
CURL_AVAILABLE=true

DOCKER_HEALTHY=false
COLIMA_ACTION=""
COLIMA_STATUS="Running"
GUEST_DOCKER_HEALTHY=true
run_preflight --repair-colima
assert_equal "${PREFLIGHT_STATUS}" "0"
assert_equal "${COLIMA_ACTION}" "restart"
assert_contains "${COLIMA_COMMAND}" "--profile default"

DOCKER_HEALTHY=false
COLIMA_ACTION=""
COLIMA_STATUS="Stopped"
run_preflight --repair-colima
assert_equal "${PREFLIGHT_STATUS}" "0"
assert_equal "${COLIMA_ACTION}" "start"

DOCKER_HEALTHY=false
COLIMA_ACTION=""
COLIMA_STATUS="Broken"
run_preflight --repair-colima
assert_equal "${PREFLIGHT_STATUS}" "1"
assert_equal "${COLIMA_ACTION}" ""
assert_contains "${PREFLIGHT_OUTPUT}" "Broken"

DOCKER_HEALTHY=false
COLIMA_ACTION=""
COLIMA_STATUS="Running"
GUEST_DOCKER_HEALTHY=false
run_preflight --repair-colima
assert_equal "${PREFLIGHT_STATUS}" "1"
assert_equal "${COLIMA_ACTION}" ""
assert_contains "${PREFLIGHT_OUTPUT}" "VM内のDocker"

DOCKER_HEALTHY=false
COLIMA_ACTION=""
COLIMA_ACTION_SUCCEEDS=false
GUEST_DOCKER_HEALTHY=true
run_preflight --repair-colima
assert_equal "${PREFLIGHT_STATUS}" "1"
assert_equal "${COLIMA_ACTION}" "restart"

DOCKER_HEALTHY=false
COLIMA_ACTION=""
COLIMA_ACTION_SUCCEEDS=true
COLIMA_PROFILE_NAME="another-profile"
run_preflight --repair-colima
assert_equal "${PREFLIGHT_STATUS}" "1"
assert_equal "${COLIMA_ACTION}" ""
assert_contains "${PREFLIGHT_OUTPUT}" "状態を判定できない"

DOCKER_HEALTHY=false
COLIMA_ACTION=""
COLIMA_PROFILE_NAME="default"
RECOVERY_RESTORES_DOCKER=false
run_preflight --repair-colima
assert_equal "${PREFLIGHT_STATUS}" "1"
assert_equal "${COLIMA_ACTION}" "restart"
assert_contains "${PREFLIGHT_OUTPUT}" "操作後も"

DOCKER_HEALTHY=false
COLIMA_ACTION=""
TEST_DOCKER_CONTEXT="default"
DOCKER_ENDPOINT="unix:///var/run/docker.sock"
GUEST_DOCKER_HEALTHY=true
RECOVERY_RESTORES_DOCKER=true
run_preflight --repair-colima
assert_equal "${PREFLIGHT_STATUS}" "1"
assert_equal "${COLIMA_ACTION}" ""
assert_contains "${PREFLIGHT_OUTPUT}" "Docker runtimeを起動"

DOCKER_HEALTHY=true
export DOCKER_HOST="unix:///tmp/test/explicit.sock"
DOCKER_ENDPOINT="unix:///tmp/test/.colima/unused/docker.sock"
run_preflight
assert_equal "${PREFLIGHT_STATUS}" "0"
assert_contains "$(<"${CURL_CALLS_FILE}")" "--unix-socket /tmp/test/explicit.sock"
assert_not_contains "$(<"${CURL_CALLS_FILE}")" ".colima/unused"
assert_not_contains "$(<"${DOCKER_CALLS_FILE}")" "context "

DOCKER_HEALTHY=false
COLIMA_ACTION=""
run_preflight --repair-colima
assert_equal "${PREFLIGHT_STATUS}" "1"
assert_equal "${COLIMA_ACTION}" ""
assert_contains "${PREFLIGHT_OUTPUT}" "Docker runtimeを起動"

export DOCKER_HOST="unix:///tmp/test/.colima/override/docker.sock"
COLIMA_PROFILE_NAME="override"
COLIMA_STATUS="Stopped"
run_preflight --repair-colima
assert_equal "${PREFLIGHT_STATUS}" "0"
assert_equal "${COLIMA_ACTION}" "start"
assert_contains "${COLIMA_COMMAND}" "--profile override"
assert_not_contains "$(<"${CURL_CALLS_FILE}")" ".colima/unused"

DOCKER_HEALTHY=true
export DOCKER_CONTEXT="explicit-context"
DOCKER_ENDPOINT="unix:///tmp/test/context.sock"
run_preflight
assert_equal "${PREFLIGHT_STATUS}" "0"
assert_contains "$(<"${DOCKER_CALLS_FILE}")" "context inspect explicit-context"
assert_contains "$(<"${CURL_CALLS_FILE}")" "--unix-socket /tmp/test/context.sock"
assert_not_contains "$(<"${CURL_CALLS_FILE}")" ".colima/override"

DOCKER_HEALTHY=false
COLIMA_ACTION=""
DOCKER_ENDPOINT="ssh://operator@remote.example.test"
run_preflight --repair-colima
assert_equal "${PREFLIGHT_STATUS}" "1"
assert_equal "${COLIMA_ACTION}" ""
assert_equal "$(<"${CURL_CALLS_FILE}")" ""
assert_contains "$(<"${DOCKER_CALLS_FILE}")" "version --format"

DOCKER_HEALTHY=true
DOCKER_FAILURE_PATTERN="context inspect"
run_preflight --repair-colima
assert_equal "${PREFLIGHT_STATUS}" "1"
assert_equal "${COLIMA_ACTION}" ""
assert_equal "$(<"${CURL_CALLS_FILE}")" ""
assert_contains "${PREFLIGHT_OUTPUT}" "Docker endpointを取得できません"
DOCKER_FAILURE_PATTERN=""
unset DOCKER_CONTEXT DOCKER_HOST

DOCKER_FAILURE_PATTERN="context show"
run_preflight
assert_equal "${PREFLIGHT_STATUS}" "1"
assert_contains "${PREFLIGHT_OUTPUT}" "Docker contextを取得できません"
DOCKER_FAILURE_PATTERN=""

DOCKER_ENDPOINT=""
run_preflight
assert_equal "${PREFLIGHT_STATUS}" "1"
assert_equal "$(<"${CURL_CALLS_FILE}")" ""
assert_contains "${PREFLIGHT_OUTPUT}" "Docker endpointが空です"

uv() {
  return 70
}

pnpm() {
  return 71
}

export CURL_AVAILABLE TEST_DOCKER_CONTEXT DOCKER_ENDPOINT DOCKER_HEALTHY
export DOCKER_CALLS_FILE CURL_CALLS_FILE DOCKER_FAILURE_PATTERN
export -f colima curl docker docker_unix_probe_is_available pnpm sleep uv

TEST_DOCKER_CONTEXT="colima"
DOCKER_ENDPOINT="unix:///tmp/test/.colima/default/docker.sock"
DOCKER_HEALTHY=false
TEST_LOCAL_OUTPUT_FILE="${TEST_TEMP_DIRECTORY}/test-local-output"
if "${SCRIPT_DIRECTORY}/test-local.sh" --compose >"${TEST_LOCAL_OUTPUT_FILE}" 2>&1; then
  TEST_LOCAL_STATUS=0
else
  TEST_LOCAL_STATUS=$?
fi
TEST_LOCAL_OUTPUT="$(<"${TEST_LOCAL_OUTPUT_FILE}")"
assert_equal "${TEST_LOCAL_STATUS}" "1"
assert_contains "${TEST_LOCAL_OUTPUT}" "--repair-colima"
if [[ "${TEST_LOCAL_OUTPUT}" == *"==> Host scripts"* || \
  "${TEST_LOCAL_OUTPUT}" == *"==> Backend"* ]]; then
  printf 'test-local.sh did not fail before the host suites.\n' >&2
  exit 1
fi

printf 'Docker preflight tests passed.\n'

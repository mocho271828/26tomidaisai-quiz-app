#!/usr/bin/env bash

set -Eeuo pipefail

AWS_PREFLIGHT_TEST_SCRIPT_DIRECTORY="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
readonly AWS_PREFLIGHT_TEST_SCRIPT_DIRECTORY
AWS_PREFLIGHT_TEST_TEMP_DIRECTORY="$(mktemp -d)"
readonly AWS_PREFLIGHT_TEST_TEMP_DIRECTORY
AWS_PREFLIGHT_TEST_COMMAND_LOG="${AWS_PREFLIGHT_TEST_TEMP_DIRECTORY}/commands.log"
readonly AWS_PREFLIGHT_TEST_COMMAND_LOG
trap 'rm -rf "${AWS_PREFLIGHT_TEST_TEMP_DIRECTORY}"' EXIT

# 実commandを呼ばず、公開scriptの制御順序と引数だけを検証する。
# shellcheck disable=SC1091
source "${AWS_PREFLIGHT_TEST_SCRIPT_DIRECTORY}/aws-demo-preflight.sh"

CANDIDATE_RELEASE_FILE="${AWS_PREFLIGHT_TEST_TEMP_DIRECTORY}/candidate.env"
ROLLBACK_RELEASE_FILE="${AWS_PREFLIGHT_TEST_TEMP_DIRECTORY}/rollback.env"
CANDIDATE_COMMIT="0123456789abcdef0123456789abcdef01234567"
ROLLBACK_COMMIT="89abcdef0123456789abcdef0123456789abcdef"
CANDIDATE_API_IMAGE="registry.invalid/api@sha256:$(printf '01%.0s' {1..32})"
CANDIDATE_GATEWAY_IMAGE="registry.invalid/gateway@sha256:$(printf '12%.0s' {1..32})"
ROLLBACK_API_IMAGE="registry.invalid/api@sha256:$(printf '23%.0s' {1..32})"
ROLLBACK_GATEWAY_IMAGE="registry.invalid/gateway@sha256:$(printf '34%.0s' {1..32})"
PROMETHEUS_IMAGE="registry.invalid/prometheus@sha256:$(printf '45%.0s' {1..32})"
POSTGRES_EXPORTER_IMAGE="registry.invalid/postgres-exporter@sha256:$(printf '56%.0s' {1..32})"
REDIS_EXPORTER_IMAGE="registry.invalid/redis-exporter@sha256:$(printf '67%.0s' {1..32})"
NODE_EXPORTER_IMAGE="registry.invalid/node-exporter@sha256:$(printf '78%.0s' {1..32})"
TOXIPROXY_IMAGE="registry.invalid/toxiproxy@sha256:$(printf '89%.0s' {1..32})"
TOXIPROXY_VERSION="2.12.0"
TOXIPROXY_REVISION="3ccd6a79cbc6c6a72b884d295ad314b75cdf3962"
TOXIPROXY_ARCHITECTURE="amd64"
EXECUTION_HOST="ip-10-0-1-25.aws.internal"
DOCKER_CONTEXT_NAME="aws-demo-local"
DOCKER_ENDPOINT="unix:///var/run/docker.sock"
DOCKER_ENGINE_VERSION="28.5.1"
FAIL_ROLLBACK_INSPECT=false
FAIL_CANDIDATE_INSPECT=false
FAIL_CANDIDATE_PREFLIGHT=false
FAIL_VALIDATOR=false
OMIT_ROLLBACK_FILE=false
ROUND_SCOPE="single-round"

run_aws_demo_docker_preflight() {
  return 0
}

uv() {
  local field=""
  local release_file=""

  printf 'uv %s\n' "$*" >>"${AWS_PREFLIGHT_TEST_COMMAND_LOG}"
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --release-file)
        release_file="$2"
        shift 2
        ;;
      --field)
        field="$2"
        shift 2
        ;;
      *) shift ;;
    esac
  done
  if [[ -z "${field}" ]]; then
    [[ "${FAIL_VALIDATOR}" != true ]]
    return
  fi

  case "${release_file}:${field}" in
    "${CANDIDATE_RELEASE_FILE}:source-commit") printf '%s\n' "${CANDIDATE_COMMIT}" ;;
    "${CANDIDATE_RELEASE_FILE}:api-image") printf '%s\n' "${CANDIDATE_API_IMAGE}" ;;
    "${CANDIDATE_RELEASE_FILE}:gateway-image") printf '%s\n' "${CANDIDATE_GATEWAY_IMAGE}" ;;
    "${ROLLBACK_RELEASE_FILE}:source-commit") printf '%s\n' "${ROLLBACK_COMMIT}" ;;
    "${ROLLBACK_RELEASE_FILE}:api-image") printf '%s\n' "${ROLLBACK_API_IMAGE}" ;;
    "${ROLLBACK_RELEASE_FILE}:gateway-image") printf '%s\n' "${ROLLBACK_GATEWAY_IMAGE}" ;;
    *:prometheus-image) printf '%s\n' "${PROMETHEUS_IMAGE}" ;;
    *:postgres-exporter-image) printf '%s\n' "${POSTGRES_EXPORTER_IMAGE}" ;;
    *:redis-exporter-image) printf '%s\n' "${REDIS_EXPORTER_IMAGE}" ;;
    *:node-exporter-image) printf '%s\n' "${NODE_EXPORTER_IMAGE}" ;;
    *:toxiproxy-image) printf '%s\n' "${TOXIPROXY_IMAGE}" ;;
    *:execution-host) printf '%s\n' "${EXECUTION_HOST}" ;;
    *:docker-context) printf '%s\n' "${DOCKER_CONTEXT_NAME}" ;;
    *:docker-endpoint) printf '%s\n' "${DOCKER_ENDPOINT}" ;;
    *:round-scope) printf '%s\n' "${ROUND_SCOPE}" ;;
    *) return 64 ;;
  esac
}

hostname() {
  [[ "${1:-}" == "-f" ]] || return 64
  printf '%s\n' "${EXECUTION_HOST}"
}

docker() {
  local format
  local image

  printf 'docker %s\n' "$*" >>"${AWS_PREFLIGHT_TEST_COMMAND_LOG}"
  if [[ "${1:-} ${2:-}" == "context show" ]]; then
    printf '%s\n' "${DOCKER_CONTEXT_NAME}"
    return 0
  fi
  if [[ "${1:-} ${2:-}" == "context inspect" ]]; then
    printf '%s\n' "${DOCKER_ENDPOINT}"
    return 0
  fi
  if [[ "${1:-} ${2:-}" == "version --format" ]]; then
    printf '%s\n' "${DOCKER_ENGINE_VERSION}"
    return 0
  fi
  if [[ "${1:-} ${2:-}" == "image inspect" ]]; then
    format="$4"
    image="$5"
    if [[ "${FAIL_ROLLBACK_INSPECT}" == true && "${image}" == "${ROLLBACK_API_IMAGE}" ]]; then
      return 1
    fi
    if [[ "${FAIL_CANDIDATE_INSPECT}" == true && "${image}" == "${CANDIDATE_API_IMAGE}" ]]; then
      return 1
    fi
    case "${format}:${image}" in
      *'{{.Id}}'*"${PROMETHEUS_IMAGE}" | \
        *'{{.Id}}'*"${POSTGRES_EXPORTER_IMAGE}" | \
        *'{{.Id}}'*"${REDIS_EXPORTER_IMAGE}" | \
        *'{{.Id}}'*"${NODE_EXPORTER_IMAGE}")
        printf 'sha256:%s\n' "$(printf 'ab%.0s' {1..32})"
        ;;
      *'{{.Id}}'*"${TOXIPROXY_IMAGE}")
        printf 'sha256:%s\n' "$(printf 'bc%.0s' {1..32})"
        ;;
      *org.opencontainers.image.version*"${TOXIPROXY_IMAGE}")
        printf '%s\n' "${TOXIPROXY_VERSION}"
        ;;
      *org.opencontainers.image.revision*"${TOXIPROXY_IMAGE}")
        printf '%s\n' "${TOXIPROXY_REVISION}"
        ;;
      *'{{.Architecture}}'*"${TOXIPROXY_IMAGE}")
        printf '%s\n' "${TOXIPROXY_ARCHITECTURE}"
        ;;
      *org.opencontainers.image.revision*"${CANDIDATE_API_IMAGE}")
        printf '%s\n' "${CANDIDATE_COMMIT}"
        ;;
      *org.opencontainers.image.revision*"${CANDIDATE_GATEWAY_IMAGE}")
        printf '%s\n' "${CANDIDATE_COMMIT}"
        ;;
      *org.opencontainers.image.revision*"${ROLLBACK_API_IMAGE}")
        printf '%s\n' "${ROLLBACK_COMMIT}"
        ;;
      *org.opencontainers.image.revision*"${ROLLBACK_GATEWAY_IMAGE}")
        printf '%s\n' "${ROLLBACK_COMMIT}"
        ;;
      *io.tomidaisai.deployment-environment*) printf 'aws-demo\n' ;;
      *) return 64 ;;
    esac
    return 0
  fi
  if [[ "${1:-}" == "compose" && "${FAIL_CANDIDATE_PREFLIGHT}" == true \
    && "$*" == *"--profile preflight run"* ]]; then
    return 1
  fi
  [[ "${1:-}" == "compose" ]]
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
    printf 'Expected command log to contain %q\n' "${expected}" >&2
    return 1
  }
}

run_test_preflight() {
  local output_file="${AWS_PREFLIGHT_TEST_TEMP_DIRECTORY}/output.log"
  local -a arguments=(--release-file "${CANDIDATE_RELEASE_FILE}")
  if [[ "${OMIT_ROLLBACK_FILE}" != true ]]; then
    arguments+=(--rollback-file "${ROLLBACK_RELEASE_FILE}")
  fi

  if aws_demo_preflight_main "${arguments[@]}" "$@" >"${output_file}" 2>&1; then
    TEST_PREFLIGHT_STATUS=0
  else
    TEST_PREFLIGHT_STATUS=$?
  fi
  TEST_PREFLIGHT_OUTPUT="$(<"${output_file}")"
}

: >"${AWS_PREFLIGHT_TEST_COMMAND_LOG}"
run_test_preflight
assert_equal "${TEST_PREFLIGHT_STATUS}" "0"
COMMAND_LOG="$(<"${AWS_PREFLIGHT_TEST_COMMAND_LOG}")"
assert_equal "$(grep -c '^docker image inspect ' "${AWS_PREFLIGHT_TEST_COMMAND_LOG}")" "8"
assert_contains "${COMMAND_LOG}" "${CANDIDATE_API_IMAGE}"
assert_contains "${COMMAND_LOG}" "${ROLLBACK_API_IMAGE}"
assert_contains "${COMMAND_LOG}" "config --quiet"
if [[ "${COMMAND_LOG}" == *"compose.load-test.yaml"* ]]; then
  printf 'single-round preflight unexpectedly selected the load-test override.\n' >&2
  exit 1
fi
assert_contains "${COMMAND_LOG}" "--profile preflight run --rm --no-deps --pull never preflight"
assert_equal "$(grep -c -- '--profile preflight run --rm --no-deps --pull never preflight' "${AWS_PREFLIGHT_TEST_COMMAND_LOG}")" "2"
assert_contains "${COMMAND_LOG}" "--env-file ${ROLLBACK_RELEASE_FILE}"
if [[ "${COMMAND_LOG}" == *"event-slug"* || "${COMMAND_LOG}" == *"event-id"* ]]; then
  printf 'Gateway image inspection unexpectedly depended on Event metadata.\n' >&2
  exit 1
fi
if grep '^docker compose ' "${AWS_PREFLIGHT_TEST_COMMAND_LOG}" \
  | grep -Eq '(^|[[:space:]])(up|build|pull|push)([[:space:]]|$)'; then
  printf 'AWS preflight attempted a deployment or image mutation command.\n' >&2
  exit 1
fi

: >"${AWS_PREFLIGHT_TEST_COMMAND_LOG}"
ROUND_SCOPE="rehearsal-six-round"
run_test_preflight
assert_equal "${TEST_PREFLIGHT_STATUS}" "0"
COMMAND_LOG="$(<"${AWS_PREFLIGHT_TEST_COMMAND_LOG}")"
assert_contains "${COMMAND_LOG}" "--file ${AWS_PREFLIGHT_COMPOSE_FILE}"
assert_contains "${COMMAND_LOG}" "--file ${AWS_PREFLIGHT_REHEARSAL_COMPOSE_FILE}"
assert_equal "$(grep -c '^docker image inspect ' "${AWS_PREFLIGHT_TEST_COMMAND_LOG}")" "8"
assert_equal "$(grep -c -- '--profile preflight run --rm --no-deps --pull never preflight' "${AWS_PREFLIGHT_TEST_COMMAND_LOG}")" "2"
if [[ "${COMMAND_LOG}" == *"compose.load-test.yaml"* ]]; then
  printf 'Rehearsal preflight unexpectedly selected the load-test override.\n' >&2
  exit 1
fi

: >"${AWS_PREFLIGHT_TEST_COMMAND_LOG}"
OMIT_ROLLBACK_FILE=true
run_test_preflight
assert_equal "${TEST_PREFLIGHT_STATUS}" "0"
COMMAND_LOG="$(<"${AWS_PREFLIGHT_TEST_COMMAND_LOG}")"
assert_contains "${COMMAND_LOG}" "--file ${AWS_PREFLIGHT_REHEARSAL_COMPOSE_FILE}"
assert_contains "${COMMAND_LOG}" "context inspect"
assert_contains "${COMMAND_LOG}" "config --quiet"
assert_equal "$(grep -c '^docker image inspect ' "${AWS_PREFLIGHT_TEST_COMMAND_LOG}")" "4"
assert_equal "$(grep -c -- '--profile preflight run --rm --no-deps --pull never preflight' "${AWS_PREFLIGHT_TEST_COMMAND_LOG}")" "1"
if [[ "${COMMAND_LOG}" == *"${ROLLBACK_RELEASE_FILE}"* || "${COMMAND_LOG}" == *"--rollback-file"* ]]; then
  printf 'Candidate-only rehearsal preflight unexpectedly required a rollback.\n' >&2
  exit 1
fi

: >"${AWS_PREFLIGHT_TEST_COMMAND_LOG}"
FAIL_VALIDATOR=true
run_test_preflight
assert_equal "${TEST_PREFLIGHT_STATUS}" "1"
if grep -q '^docker ' "${AWS_PREFLIGHT_TEST_COMMAND_LOG}"; then
  printf 'Docker ran after candidate-only configuration validation failed.\n' >&2
  exit 1
fi
FAIL_VALIDATOR=false

: >"${AWS_PREFLIGHT_TEST_COMMAND_LOG}"
FAIL_CANDIDATE_INSPECT=true
run_test_preflight
assert_equal "${TEST_PREFLIGHT_STATUS}" "1"
if grep -q '^docker compose ' "${AWS_PREFLIGHT_TEST_COMMAND_LOG}"; then
  printf 'Compose ran after candidate-only image inspection failed.\n' >&2
  exit 1
fi
FAIL_CANDIDATE_INSPECT=false

FAIL_CANDIDATE_PREFLIGHT=true
run_test_preflight
assert_equal "${TEST_PREFLIGHT_STATUS}" "1"
if [[ "${TEST_PREFLIGHT_OUTPUT}" == *"AWS demo preflight passed"* ]]; then
  printf 'Candidate-only rehearsal reported success after dependency checks failed.\n' >&2
  exit 1
fi
FAIL_CANDIDATE_PREFLIGHT=false

: >"${AWS_PREFLIGHT_TEST_COMMAND_LOG}"
run_test_preflight --rollback-operation
assert_equal "${TEST_PREFLIGHT_STATUS}" "2"
assert_equal "$(<"${AWS_PREFLIGHT_TEST_COMMAND_LOG}")" ""
for ROUND_SCOPE in single-round load-test-ten-round; do
  : >"${AWS_PREFLIGHT_TEST_COMMAND_LOG}"
  run_test_preflight
  assert_equal "${TEST_PREFLIGHT_STATUS}" "1"
  assert_contains "${TEST_PREFLIGHT_OUTPUT}" "--rollback-fileが必要"
  if grep -q '^docker ' "${AWS_PREFLIGHT_TEST_COMMAND_LOG}"; then
    printf 'Docker ran without the required rollback for %s.\n' "${ROUND_SCOPE}" >&2
    exit 1
  fi
done
OMIT_ROLLBACK_FILE=false

: >"${AWS_PREFLIGHT_TEST_COMMAND_LOG}"
ROUND_SCOPE="load-test-ten-round"
run_test_preflight
assert_equal "${TEST_PREFLIGHT_STATUS}" "0"
COMMAND_LOG="$(<"${AWS_PREFLIGHT_TEST_COMMAND_LOG}")"
assert_contains "${COMMAND_LOG}" "--file ${AWS_PREFLIGHT_COMPOSE_FILE}"
assert_contains "${COMMAND_LOG}" "--file ${AWS_PREFLIGHT_LOAD_TEST_COMPOSE_FILE}"
assert_equal "$(grep -c '^docker image inspect ' "${AWS_PREFLIGHT_TEST_COMMAND_LOG}")" "24"
for monitor_image in \
  "${PROMETHEUS_IMAGE}" "${POSTGRES_EXPORTER_IMAGE}" \
  "${REDIS_EXPORTER_IMAGE}" "${NODE_EXPORTER_IMAGE}"; do
  assert_equal "$(grep -c -- "${monitor_image}" "${AWS_PREFLIGHT_TEST_COMMAND_LOG}")" "2"
done
assert_equal "$(grep -c -- "${TOXIPROXY_IMAGE}" "${AWS_PREFLIGHT_TEST_COMMAND_LOG}")" "8"
assert_equal "$(grep -c -- '--profile preflight run --rm --no-deps --pull never preflight' "${AWS_PREFLIGHT_TEST_COMMAND_LOG}")" "2"
if grep '^docker compose ' "${AWS_PREFLIGHT_TEST_COMMAND_LOG}" \
  | grep -Eq '(^|[[:space:]])(up|build|pull|push)([[:space:]]|$)'; then
  printf 'Load-test preflight attempted a deployment or image mutation command.\n' >&2
  exit 1
fi
ROUND_SCOPE="single-round"

: >"${AWS_PREFLIGHT_TEST_COMMAND_LOG}"
ROUND_SCOPE="load-test-ten-round"
TOXIPROXY_VERSION="2.11.0"
run_test_preflight
assert_equal "${TEST_PREFLIGHT_STATUS}" "1"
assert_contains "${TEST_PREFLIGHT_OUTPUT}" "承認済みv2.12.0"
if grep -q '^docker compose ' "${AWS_PREFLIGHT_TEST_COMMAND_LOG}"; then
  printf 'Compose ran after Toxiproxy version validation failed.\n' >&2
  exit 1
fi
TOXIPROXY_VERSION="2.12.0"
ROUND_SCOPE="single-round"

: >"${AWS_PREFLIGHT_TEST_COMMAND_LOG}"
ROUND_SCOPE="load-test-ten-round"
DOCKER_ENGINE_VERSION="27.5.1"
run_test_preflight
assert_equal "${TEST_PREFLIGHT_STATUS}" "1"
assert_contains "${TEST_PREFLIGHT_OUTPUT}" "Docker Engine 28.0.0以上"
if grep -Eq "${PROMETHEUS_IMAGE}|${TOXIPROXY_IMAGE}" "${AWS_PREFLIGHT_TEST_COMMAND_LOG}"; then
  printf 'Load-test image inspection ran after Docker Engine version validation failed.\n' >&2
  exit 1
fi
DOCKER_ENGINE_VERSION="28.5.1"
ROUND_SCOPE="single-round"

: >"${AWS_PREFLIGHT_TEST_COMMAND_LOG}"
FAIL_ROLLBACK_INSPECT=true
run_test_preflight --rollback-operation
assert_equal "${TEST_PREFLIGHT_STATUS}" "0"
assert_equal "$(grep -c '^docker image inspect ' "${AWS_PREFLIGHT_TEST_COMMAND_LOG}")" "4"
assert_equal "$(grep -c -- '--profile preflight run --rm --no-deps --pull never preflight' "${AWS_PREFLIGHT_TEST_COMMAND_LOG}")" "1"
FAIL_ROLLBACK_INSPECT=false

: >"${AWS_PREFLIGHT_TEST_COMMAND_LOG}"
FAIL_ROLLBACK_INSPECT=true
run_test_preflight
assert_equal "${TEST_PREFLIGHT_STATUS}" "1"
if grep -q '^docker compose ' "${AWS_PREFLIGHT_TEST_COMMAND_LOG}"; then
  printf 'Compose ran after rollback image inspection failed.\n' >&2
  exit 1
fi
FAIL_ROLLBACK_INSPECT=false

: >"${AWS_PREFLIGHT_TEST_COMMAND_LOG}"
AWS_DEMO_API_IMAGE="ambient-override"
run_test_preflight
assert_equal "${TEST_PREFLIGHT_STATUS}" "1"
assert_contains "${TEST_PREFLIGHT_OUTPUT}" "shell環境へexportせず"
if [[ -s "${AWS_PREFLIGHT_TEST_COMMAND_LOG}" ]]; then
  printf 'External commands ran despite an ambient AWS_DEMO_* override.\n' >&2
  exit 1
fi
unset AWS_DEMO_API_IMAGE

: >"${AWS_PREFLIGHT_TEST_COMMAND_LOG}"
DOCKER_HOST="tcp://unapproved.invalid:2376"
run_test_preflight
assert_equal "${TEST_PREFLIGHT_STATUS}" "1"
assert_contains "${TEST_PREFLIGHT_OUTPUT}" "承認済みlocal Docker context"
if [[ -s "${AWS_PREFLIGHT_TEST_COMMAND_LOG}" ]]; then
  printf 'External commands ran despite an ambient DOCKER_HOST override.\n' >&2
  exit 1
fi
unset DOCKER_HOST

: >"${AWS_PREFLIGHT_TEST_COMMAND_LOG}"
DOCKER_CONFIG="${AWS_PREFLIGHT_TEST_TEMP_DIRECTORY}/unapproved-docker-config"
run_test_preflight
assert_equal "${TEST_PREFLIGHT_STATUS}" "1"
assert_contains "${TEST_PREFLIGHT_OUTPUT}" "承認済みlocal Docker context"
if [[ -s "${AWS_PREFLIGHT_TEST_COMMAND_LOG}" ]]; then
  printf 'External commands ran despite an ambient DOCKER_CONFIG override.\n' >&2
  exit 1
fi
unset DOCKER_CONFIG

: >"${AWS_PREFLIGHT_TEST_COMMAND_LOG}"
COMPOSE_REMOVE_ORPHANS=1
run_test_preflight
assert_equal "${TEST_PREFLIGHT_STATUS}" "1"
assert_contains "${TEST_PREFLIGHT_OUTPUT}" "COMPOSE_*"
if [[ -s "${AWS_PREFLIGHT_TEST_COMMAND_LOG}" ]]; then
  printf 'External commands ran despite an ambient COMPOSE_* override.\n' >&2
  exit 1
fi
unset COMPOSE_REMOVE_ORPHANS

printf 'AWS demo preflight tests passed.\n'

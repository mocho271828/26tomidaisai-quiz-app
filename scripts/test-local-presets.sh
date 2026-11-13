#!/usr/bin/env bash

set -Eeuo pipefail

PRESET_SCRIPT_DIRECTORY="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PRESET_REPOSITORY_ROOT="$(cd -- "${PRESET_SCRIPT_DIRECTORY}/.." && pwd)"
PRESET_TEST_DIRECTORY="$(mktemp -d)"
readonly PRESET_SCRIPT_DIRECTORY PRESET_REPOSITORY_ROOT PRESET_TEST_DIRECTORY
trap 'rm -rf -- "${PRESET_TEST_DIRECTORY}"' EXIT

mkdir "${PRESET_TEST_DIRECTORY}/bin"
cat >"${PRESET_TEST_DIRECTORY}/bin/docker" <<'EOF'
#!/usr/bin/env bash
set -Eeuo pipefail
printf '%q ' "$@" >>"${PRESET_DOCKER_CALLS}"
printf '\n' >>"${PRESET_DOCKER_CALLS}"
case "${1:-} ${2:-}" in
  'context show') printf 'local-fixture\n' ;;
  'context inspect') printf '%s\n' "${PRESET_CONTEXT_ENDPOINT}" ;;
  'compose version') printf 'Docker Compose fixture\n' ;;
  compose\ *)
    printf '%q ' "$@" >>"${PRESET_COMPOSE_CALLS}"
    printf '\n' >>"${PRESET_COMPOSE_CALLS}"
    ;;
  *) exit 64 ;;
esac
EOF
cat >"${PRESET_TEST_DIRECTORY}/bin/curl" <<'EOF'
#!/usr/bin/env bash
printf 'OK'
EOF
chmod +x "${PRESET_TEST_DIRECTORY}/bin/docker" "${PRESET_TEST_DIRECTORY}/bin/curl"

PRESET_DOCKER_HOST=""
PRESET_DOCKER_CONTEXT=""
PRESET_CONTEXT_ENDPOINT="unix:///tmp/local-presets-fixture.sock"

run_preset() {
  : >"${PRESET_TEST_DIRECTORY}/docker-calls"
  : >"${PRESET_TEST_DIRECTORY}/compose-calls"
  # 継承されたshell関数・Docker設定を除き、必ずfakeだけへ到達させる。
  if env -i \
    PATH="${PRESET_TEST_DIRECTORY}/bin:/usr/bin:/bin" \
    PRESET_DOCKER_CALLS="${PRESET_TEST_DIRECTORY}/docker-calls" \
    PRESET_COMPOSE_CALLS="${PRESET_TEST_DIRECTORY}/compose-calls" \
    PRESET_CONTEXT_ENDPOINT="${PRESET_CONTEXT_ENDPOINT}" \
    DOCKER_HOST="${PRESET_DOCKER_HOST}" \
    DOCKER_CONTEXT="${PRESET_DOCKER_CONTEXT}" \
    COMPOSE_FILE=/tmp/unrelated-compose.yaml \
    COMPOSE_PROJECT_NAME=unrelated-project \
    "${PRESET_SCRIPT_DIRECTORY}/local-compose.sh" "$@" \
    >"${PRESET_TEST_DIRECTORY}/output" 2>&1; then
    PRESET_STATUS=0
  else
    PRESET_STATUS=$?
  fi
}

assert_equal() {
  [[ "$1" == "$2" ]] || {
    printf 'Expected %q, got %q\n' "$2" "$1" >&2
    cat "${PRESET_TEST_DIRECTORY}/output" >&2
    return 1
  }
}

assert_compose_call() {
  local expected
  expected="$(printf '%q ' compose \
    --file "${PRESET_REPOSITORY_ROOT}/compose.yaml" \
    --file "${PRESET_REPOSITORY_ROOT}/compose.local-multi-api.yaml" \
    --project-directory "${PRESET_REPOSITORY_ROOT}" \
    --project-name gs-app-tomidaisai "$@")"
  assert_equal "${PRESET_STATUS}" 0
  assert_equal "$(<"${PRESET_TEST_DIRECTORY}/compose-calls")" "${expected}"
}

# 別の作業directoryとambient Compose設定でも対象が変わらない。
cd "${PRESET_TEST_DIRECTORY}"
run_preset status
assert_compose_call ps --all
run_preset logs
assert_compose_call logs --follow --tail 100 api api-secondary gateway postgres redis
run_preset stop
# exactなdownだけを許容し、--volumes/-vなどの追加を検出する。
assert_compose_call down

for invalid_action in '' unknown; do
  if [[ -z "${invalid_action}" ]]; then
    run_preset
  else
    run_preset "${invalid_action}"
  fi
  assert_equal "${PRESET_STATUS}" 2
  assert_equal "$(<"${PRESET_TEST_DIRECTORY}/docker-calls")" ''
done
for action in status logs stop; do
  run_preset "${action}" extra
  assert_equal "${PRESET_STATUS}" 2
  assert_equal "$(<"${PRESET_TEST_DIRECTORY}/docker-calls")" ''
done

for action in status logs stop; do
  PRESET_DOCKER_HOST='tcp://remote.example.test:2376'
  run_preset "${action}"
  assert_equal "${PRESET_STATUS}" 1
  assert_equal "$(<"${PRESET_TEST_DIRECTORY}/docker-calls")" ''

  PRESET_DOCKER_HOST='unix:///tmp/ignored-local.sock'
  PRESET_DOCKER_CONTEXT=remote-fixture
  PRESET_CONTEXT_ENDPOINT='ssh://operator@remote.example.test'
  run_preset "${action}"
  assert_equal "${PRESET_STATUS}" 1
  assert_equal "$(<"${PRESET_TEST_DIRECTORY}/compose-calls")" ''
  PRESET_DOCKER_CONTEXT=''
done

printf 'Local command preset tests passed.\n'

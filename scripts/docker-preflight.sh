#!/usr/bin/env bash

set -Eeuo pipefail

docker_preflight_usage() {
  cat <<'EOF'
Usage: ./scripts/docker-preflight.sh [--repair-colima]

Options:
  --repair-colima  Colimaの接続断を検出した場合だけ、対象profileを起動または再起動する
  -h, --help       このhelpを表示する
EOF
}

docker_preflight_fail() {
  printf 'Error: %s\n' "$1" >&2
  return 1
}

docker_server_is_available() {
  local endpoint="$1"
  local response

  case "${endpoint}" in
    unix://*)
      docker_unix_probe_is_available || return 1
      response="$(
        curl --fail --silent --show-error --max-time 3 \
          --unix-socket "${endpoint#unix://}" http://localhost/_ping 2>/dev/null
      )" || return 1
      [[ "${response}" == "OK" ]]
      ;;
    *) docker version --format '{{.Server.Version}}' >/dev/null 2>&1 ;;
  esac
}

docker_unix_probe_is_available() {
  command -v curl >/dev/null 2>&1
}

wait_for_docker_server() {
  local endpoint="$1"
  local attempt

  for ((attempt = 1; attempt <= 15; attempt += 1)); do
    docker_server_is_available "${endpoint}" && return 0
    [[ "${attempt}" -eq 15 ]] || sleep 1
  done
  return 1
}

colima_profile_status() {
  local line
  local list_output
  local profile="$1"

  list_output="$(colima list --json 2>/dev/null)" || return 1
  while IFS= read -r line; do
    [[ "${line}" == *"\"name\":\"${profile}\""* ]] || continue
    case "${line}" in
      *'"status":"Running"'*) printf 'Running\n'; return 0 ;;
      *'"status":"Stopped"'*) printf 'Stopped\n'; return 0 ;;
      *'"status":"Broken"'*) printf 'Broken\n'; return 0 ;;
      *) return 1 ;;
    esac
  done <<<"${list_output}"
  return 1
}

colima_guest_docker_is_available() {
  local profile="$1"

  colima ssh --profile "${profile}" -- \
    docker version --format '{{.Server.Version}}' >/dev/null 2>&1
}

docker_context_endpoint() {
  local context="$1"

  docker context inspect "${context}" \
    --format '{{.Endpoints.docker.Host}}' 2>/dev/null
}

colima_profile_from_endpoint() {
  local endpoint="$1"
  local profile
  local socket_directory
  local socket_path

  case "${endpoint}" in
    unix://*/.colima/*/docker.sock) ;;
    *) return 1 ;;
  esac

  socket_path="${endpoint#unix://}"
  socket_directory="${socket_path%/docker.sock}"
  profile="${socket_directory##*/}"
  [[ "${profile}" =~ ^[[:alnum:]][[:alnum:]_.-]*$ ]] || return 1
  printf '%s\n' "${profile}"
}

repair_colima_profile() {
  local endpoint="$1"
  local profile="$2"
  local lock_directory="${TMPDIR:-/tmp}"
  local lock_file="${lock_directory%/}/gs-app-tomidaisai-colima-${profile}.lock"
  local profile_status
  local repair_status=0

  if ! command -v colima >/dev/null 2>&1; then
    docker_preflight_fail "Colima contextですが、colima commandが見つかりません"
    return
  fi
  if ! command -v shlock >/dev/null 2>&1; then
    docker_preflight_fail "Colimaの多重再起動を防ぐshlock commandが見つかりません"
    return
  fi
  if ! shlock -p "$$" -f "${lock_file}"; then
    docker_preflight_fail "別processがColima profile ${profile} を修復中です"
    return
  fi

  # lock取得前に別processが復旧していれば、共有runtimeを再起動しない。
  if docker_server_is_available "${endpoint}"; then
    rm -f -- "${lock_file}"
    printf 'Docker daemonは別processによって復旧済みです。\n'
    return 0
  fi

  profile_status="$(colima_profile_status "${profile}" || true)"
  case "${profile_status}" in
    Running)
      if ! colima_guest_docker_is_available "${profile}"; then
        rm -f -- "${lock_file}"
        docker_preflight_fail \
          "Colima VM内のDockerも確認できないため、自動restartを中止しました"
        return
      fi
      printf 'Colima profile %q のsocket転送を再作成します。\n' "${profile}"
      colima restart --profile "${profile}" || repair_status=$?
      ;;
    Stopped)
      printf 'Colima profile %q を起動します。\n' "${profile}"
      colima start --profile "${profile}" || repair_status=$?
      ;;
    Broken)
      rm -f -- "${lock_file}"
      docker_preflight_fail \
        "Colima profile ${profile} はBrokenです。自動操作せずcolima listを確認してください"
      return
      ;;
    *)
      rm -f -- "${lock_file}"
      docker_preflight_fail \
        "Colima profile ${profile} の状態を判定できないため、自動操作を中止しました"
      return
      ;;
  esac

  rm -f -- "${lock_file}"
  return "${repair_status}"
}

docker_preflight_main() {
  local context="${DOCKER_CONTEXT:-}"
  local endpoint="${DOCKER_HOST:-}"
  local profile=""
  local repair_colima=false

  [[ $# -le 1 ]] || {
    docker_preflight_usage >&2
    return 2
  }

  case "${1:-}" in
    "") ;;
    --repair-colima) repair_colima=true ;;
    -h | --help)
      docker_preflight_usage
      return 0
      ;;
    *)
      docker_preflight_usage >&2
      return 2
      ;;
  esac

  if ! command -v docker >/dev/null 2>&1; then
    docker_preflight_fail "docker commandが見つかりません"
    return
  fi
  if ! docker compose version >/dev/null 2>&1; then
    docker_preflight_fail "docker composeを利用できません"
    return
  fi

  # Docker CLIと同じ優先順位で検査先と修復対象を決める。
  if [[ -n "${context}" || -z "${endpoint}" ]]; then
    if [[ -z "${context}" ]]; then
      context="$(docker context show 2>/dev/null)" || {
        docker_preflight_fail "Docker contextを取得できません"
        return
      }
    fi
    endpoint="$(docker_context_endpoint "${context}")" || {
      docker_preflight_fail "Docker endpointを取得できません"
      return
    }
  else
    context="DOCKER_HOST override"
  fi

  if [[ -z "${endpoint}" ]]; then
    docker_preflight_fail "Docker endpointが空です"
    return
  fi

  if [[ "${endpoint}" == unix://* ]] && ! docker_unix_probe_is_available; then
    docker_preflight_fail \
      "Unix socketの有限時間probeに必要なcurl commandが見つかりません"
    return
  fi

  if docker_server_is_available "${endpoint}"; then
    printf 'Docker preflight passed (context: %s).\n' "${context}"
    return 0
  fi

  profile="$(colima_profile_from_endpoint "${endpoint}" || true)"

  printf 'Docker daemonへ接続できません (context: %s, endpoint: %s)。\n' \
    "${context}" "${endpoint}" >&2

  if [[ -z "${profile}" ]]; then
    docker_preflight_fail \
      "Docker runtimeを起動し、docker versionでClientとServerの両方を確認してください"
    return
  fi

  if [[ "${repair_colima}" != true ]]; then
    docker_preflight_fail \
      "Colimaを修復するには ./scripts/docker-preflight.sh --repair-colima を明示実行してください"
    return
  fi

  repair_colima_profile "${endpoint}" "${profile}" || return
  if ! wait_for_docker_server "${endpoint}"; then
    docker_preflight_fail "Colima操作後もDocker daemonへ接続できません"
    return
  fi
  printf 'Docker preflight passed after Colima recovery (profile: %s).\n' "${profile}"
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  docker_preflight_main "$@"
fi

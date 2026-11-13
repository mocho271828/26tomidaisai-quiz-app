#!/usr/bin/env bash

set -Eeuo pipefail

SCRIPT_DIRECTORY="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
readonly SCRIPT_DIRECTORY
REPOSITORY_ROOT="$(cd -- "${SCRIPT_DIRECTORY}/.." && pwd)"
readonly REPOSITORY_ROOT

usage() {
  cat <<'EOF'
Usage: ./scripts/run-demo-acceptance.sh [options]

Options:
  --participant-base-url URL  参加者通信に使うprivate LAN URL
                              省略時はCompose内gatewayを使う
  --participants COUNT        模擬参加者数（2〜32、既定: 8）
  -h, --help                  このhelpを表示する

このrunnerは現在のdemo Roundを一回進行する。migration、seed、DB resetは実行しない。
EOF
}

fail() {
  printf 'Error: %s\n' "$1" >&2
  exit 1
}

participant_base_url="http://gateway:8080"
participants=8

while [[ $# -gt 0 ]]; do
  case "$1" in
    --participant-base-url)
      [[ $# -ge 2 ]] || fail "--participant-base-urlにはURLが必要です"
      participant_base_url="$2"
      shift 2
      ;;
    --participants)
      [[ $# -ge 2 ]] || fail "--participantsには人数が必要です"
      participants="$2"
      shift 2
      ;;
    -h | --help)
      usage
      exit 0
      ;;
    *)
      usage >&2
      fail "未対応のoptionです: $1"
      ;;
  esac
done

[[ "${participants}" =~ ^[0-9]+$ ]] || fail "参加者数は整数で指定してください"
((participants >= 2 && participants <= 32)) || fail "参加者数は2〜32で指定してください"

command -v docker >/dev/null 2>&1 || fail "docker commandが見つかりません"

cd "${REPOSITORY_ROOT}"
"${SCRIPT_DIRECTORY}/docker-preflight.sh"
docker compose config --quiet

printf '一回性の受入を開始します（参加者: %s人）。\n' "${participants}"
LOCAL_DEMO_PARTICIPANT_BASE_URL="${participant_base_url}" \
  DEMO_PARTICIPANT_COUNT="${participants}" \
  docker compose --profile acceptance run --rm acceptance

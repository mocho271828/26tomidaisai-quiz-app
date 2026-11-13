#!/usr/bin/env bash

set -Eeuo pipefail

SCRIPT_DIRECTORY="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
readonly SCRIPT_DIRECTORY
REPOSITORY_ROOT="$(cd -- "${SCRIPT_DIRECTORY}/.." && pwd)"
readonly REPOSITORY_ROOT

usage() {
  cat <<'EOF'
Usage: ./scripts/test-local.sh [--compose]

Options:
  --compose  Host上の品質確認後にDocker Composeの設定確認とbuildも実行する
  -h, --help このhelpを表示する
EOF
}

fail() {
  printf 'Error: %s\n' "$1" >&2
  exit 1
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || fail "必要なcommandが見つかりません: $1"
}

run_command() {
  printf '\n$'
  printf ' %q' "$@"
  printf '\n'
  "$@"
}

run_host_script_checks() {
  printf '\n==> Host scripts\n'
  run_command bash -n "${SCRIPT_DIRECTORY}/aws-demo-preflight.sh"
  run_command "${SCRIPT_DIRECTORY}/test-aws-demo-preflight.sh"
  run_command "${SCRIPT_DIRECTORY}/test-docker-preflight.sh"
  run_command "${SCRIPT_DIRECTORY}/test-restart-local.sh"
  run_command "${SCRIPT_DIRECTORY}/test-local-presets.sh"
  run_command "${SCRIPT_DIRECTORY}/test-lan-presets.sh"
  run_command "${SCRIPT_DIRECTORY}/test-reset-local-db.sh"
}

run_backend_checks() {
  printf '\n==> Backend\n'
  cd "${REPOSITORY_ROOT}/backend"
  run_command uv sync --frozen
  run_command uv run ruff check src ../tests ../validation
  run_command uv run ruff format --check src ../tests ../validation
  run_command uv run pytest
  # DBへ適用せず、migration SQLをoffline生成できることだけを確認する。
  run_command env APP_ENV=local-test ROUND_BACKUP_STORAGE_MODE=disabled \
    ROUND_BACKUP_DIRECTORY=/var/empty/quiz-round-backups ROUND_BACKUP_ENCRYPTION_KEY=disabled \
    uv run alembic upgrade head --sql
}

run_frontend_checks() {
  printf '\n==> Frontend\n'
  cd "${REPOSITORY_ROOT}/frontend"
  run_command pnpm install --frozen-lockfile
  run_command pnpm lint
  run_command pnpm typecheck
  run_command pnpm test
  run_command pnpm build
}

run_compose_checks() {
  printf '\n==> Docker Compose\n'
  cd "${REPOSITORY_ROOT}"
  run_command "${SCRIPT_DIRECTORY}/docker-preflight.sh"
  run_command docker compose config --quiet
  run_command docker compose build
}

include_compose=false

[[ $# -le 1 ]] || {
  usage >&2
  fail "optionは1つだけ指定できます"
}

case "${1:-}" in
  "") ;;
  --compose) include_compose=true ;;
  -h | --help)
    usage
    exit 0
    ;;
  *)
    usage >&2
    fail "未対応のoptionです: $1"
    ;;
esac

require_command uv
require_command pnpm

if [[ "${include_compose}" == true ]]; then
  require_command docker
  # 重いhost testの前にruntime不備を検出し、完走後の手戻りを防ぐ。
  run_command "${SCRIPT_DIRECTORY}/docker-preflight.sh"
fi

(run_host_script_checks)
(run_backend_checks)
(run_frontend_checks)

if [[ "${include_compose}" == true ]]; then
  (run_compose_checks)
fi

printf '\nAll local checks passed.\n'

#!/usr/bin/env bash
set -euo pipefail
repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"
env_file="${1:?env file required}"
output_directory="${2:?new output directory required}"
duration="${3:-300}"
project_name="${4:-gs-app-tomidaisai-tls}"
api_mode="${5:-single}"
case "$api_mode" in
  single|dual) ;;
  *) echo 'local_tls_api_mode_requires_single_or_dual' >&2; exit 64 ;;
esac
case "$project_name" in
  gs-app-tomidaisai-tls|gs-app-tomidaisai-tls-diagnostics) ;;
  *) echo 'local_tls_observation_project_refused' >&2; exit 64 ;;
esac
[[ "$duration" =~ ^[0-9]+$ ]] && (( duration >= 1 && duration <= 7200 )) || exit 64
umask 077
observer_python="$repo_root/backend/.venv/bin/python"
if [[ ! -x "$observer_python" ]]; then
  echo 'local_tls_observation_requires_backend_venv' >&2
  exit 64
fi
compose=(docker compose --project-name "$project_name" --env-file "$env_file" -f compose.yaml)
api_services=(api)
if [[ "$api_mode" == dual ]]; then
  compose+=(-f compose.local-multi-api.yaml)
  api_services+=(api-secondary)
fi
compose+=(-f compose.local-tls.yaml)
if [[ "$api_mode" == dual ]]; then
  compose+=(-f compose.local-tls-multi-api.yaml)
fi
if [[ "$api_mode" == single ]] && [[ -n "$(docker ps -q \
  --filter "label=com.docker.compose.project=$project_name" \
  --filter 'label=com.docker.compose.service=api-secondary')" ]]; then
  echo 'local_tls_observation_api_mode_mismatch: use dual for both API metrics' >&2
  exit 64
fi
container_ids=()
while IFS= read -r container; do
  [[ -n "$container" ]] && container_ids+=("$container")
done < <("${compose[@]}" ps -q)
if (( ${#container_ids[@]} == 0 )); then
  echo 'local_tls_observation_requires_running_project' >&2
  exit 64
fi
for api_service in "${api_services[@]}"; do
  if [[ -z "$("${compose[@]}" ps -q "$api_service")" ]]; then
    echo 'local_tls_observation_requires_all_api_processes' >&2
    exit 64
  fi
done
observer_args=(observe --directory "$output_directory" --duration "$duration" --api-mode "$api_mode")
for container in "${container_ids[@]}"; do
  observer_args+=(--container "$container")
done
# 常駐採取はAPIごとに一度だけexecし、DB/containerの疎なprobeと独立させる。
exec "$observer_python" -m quiz_app.observability.local_observation \
  "${observer_args[@]}" -- "${compose[@]}"

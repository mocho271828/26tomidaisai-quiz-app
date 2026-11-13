#!/usr/bin/env bash

set -Eeuo pipefail

AWS_PREFLIGHT_SCRIPT_DIRECTORY="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
readonly AWS_PREFLIGHT_SCRIPT_DIRECTORY
AWS_PREFLIGHT_REPOSITORY_ROOT="$(cd -- "${AWS_PREFLIGHT_SCRIPT_DIRECTORY}/.." && pwd)"
readonly AWS_PREFLIGHT_REPOSITORY_ROOT
AWS_PREFLIGHT_COMPOSE_FILE="${AWS_PREFLIGHT_REPOSITORY_ROOT}/deploy/aws-demo/compose.yaml"
readonly AWS_PREFLIGHT_COMPOSE_FILE
AWS_PREFLIGHT_LOAD_TEST_COMPOSE_FILE="${AWS_PREFLIGHT_REPOSITORY_ROOT}/deploy/aws-demo/compose.load-test.yaml"
readonly AWS_PREFLIGHT_LOAD_TEST_COMPOSE_FILE
AWS_PREFLIGHT_REHEARSAL_COMPOSE_FILE="${AWS_PREFLIGHT_REPOSITORY_ROOT}/deploy/aws-demo/compose.rehearsal.yaml"
readonly AWS_PREFLIGHT_REHEARSAL_COMPOSE_FILE
AWS_PREFLIGHT_VALIDATOR="${AWS_PREFLIGHT_REPOSITORY_ROOT}/validation/aws_demo_deployment.py"
readonly AWS_PREFLIGHT_VALIDATOR

aws_demo_preflight_usage() {
  cat <<'EOF'
Usage: ./scripts/aws-demo-preflight.sh --release-file FILE [--rollback-file FILE] [--rollback-operation]

このcommandは設定、image label、DB schema/Event/Round/operator、Redis接続だけを検証します。
AWS操作、image pull/build/push、API/gateway起動、migration、seed、公開切替は行いません。
--rollback-operationは直前成功releaseだけを実行検証し、失敗releaseのruntimeへ依存しません。
rehearsal-six-roundの配置前検証に限り、--rollback-fileを省略できます。
EOF
}

aws_demo_preflight_fail() {
  printf 'Error: %s\n' "$1" >&2
  return 1
}

require_aws_demo_preflight_command() {
  command -v "$1" >/dev/null 2>&1 || {
    aws_demo_preflight_fail "必要なcommandが見つかりません: $1"
    return 1
  }
}

reject_aws_demo_ambient_environment() {
  local variable_name

  while IFS= read -r variable_name; do
    aws_demo_preflight_fail \
      "AWS_DEMO_*はshell環境へexportせず、検証対象release fileだけへ記録してください: ${variable_name}"
    return 1
  done < <(compgen -A variable AWS_DEMO_ || true)

  while IFS= read -r variable_name; do
    aws_demo_preflight_fail \
      "COMPOSE_*はAWS demo preflightで使用できません: ${variable_name}"
    return 1
  done < <(compgen -A variable COMPOSE_ || true)

  for variable_name in \
    DOCKER_HOST DOCKER_CONTEXT DOCKER_TLS_VERIFY DOCKER_CERT_PATH DOCKER_CONFIG; do
    if declare -p "${variable_name}" >/dev/null 2>&1; then
      aws_demo_preflight_fail \
        "${variable_name}はAWS demo preflightで使用できません。承認済みlocal Docker contextを使ってください"
      return 1
    fi
  done
}

verify_aws_demo_execution_target() {
  local actual_context
  local actual_endpoint
  local actual_host
  local expected_context
  local expected_endpoint
  local expected_host
  local release_file="$1"

  expected_host="$(read_release_field execution-host "${release_file}")" || return
  expected_context="$(read_release_field docker-context "${release_file}")" || return
  expected_endpoint="$(read_release_field docker-endpoint "${release_file}")" || return
  actual_host="$(hostname -f)" || {
    aws_demo_preflight_fail "実行hostのFQDNを確認できません"
    return
  }
  actual_context="$(docker context show)" || {
    aws_demo_preflight_fail "current Docker contextを確認できません"
    return
  }
  actual_endpoint="$(
    docker context inspect \
      --format '{{ (index .Endpoints "docker").Host }}' \
      "${actual_context}"
  )" || {
    aws_demo_preflight_fail "current Docker endpointを確認できません"
    return
  }

  if [[ "${actual_host}" != "${expected_host}" ]]; then
    aws_demo_preflight_fail "実行hostがrelease記録と一致しません"
    return
  fi
  if [[ "${actual_context}" != "${expected_context}" ]]; then
    aws_demo_preflight_fail "Docker contextがrelease記録と一致しません"
    return
  fi
  if [[ "${actual_endpoint}" != "${expected_endpoint}" ]]; then
    aws_demo_preflight_fail "Docker endpointが承認済みlocal unix socketと一致しません"
    return
  fi
}

run_aws_demo_docker_preflight() {
  "${AWS_PREFLIGHT_SCRIPT_DIRECTORY}/docker-preflight.sh"
}

verify_load_test_docker_engine() {
  local engine_version
  local major
  local minor
  local patch

  engine_version="$(docker version --format '{{.Server.Version}}')" || {
    aws_demo_preflight_fail "Docker Engine server versionを確認できません"
    return
  }
  if [[ ! "${engine_version}" =~ ^([0-9]{2,3})\.([0-9]{1,3})\.([0-9]{1,3})$ ]]; then
    aws_demo_preflight_fail "Docker Engine server versionがstrict x.y.z形式ではありません"
    return
  fi
  major="${BASH_REMATCH[1]}"
  minor="${BASH_REMATCH[2]}"
  patch="${BASH_REMATCH[3]}"
  if ((10#${major} < 28)); then
    aws_demo_preflight_fail "負荷試験のisolated gatewayにはDocker Engine 28.0.0以上が必要です"
    return
  fi
  # strict parseで全要素を消費したことを明示し、将来の下限更新時に比較を局所化する。
  : "$((10#${minor}))" "$((10#${patch}))"
}

run_aws_demo_validator() {
  uv --directory "${AWS_PREFLIGHT_REPOSITORY_ROOT}/backend" run \
    --frozen --offline --no-sync --no-env-file python \
    "${AWS_PREFLIGHT_VALIDATOR}" \
    --repository-root "${AWS_PREFLIGHT_REPOSITORY_ROOT}" \
    "$@"
}

read_release_field() {
  local field="$1"
  local release_file="$2"

  run_aws_demo_validator --release-file "${release_file}" --field "${field}"
}

inspect_required_image_label() {
  local expected="$1"
  local image="$2"
  local label="$3"
  local value

  value="$(docker image inspect --format "{{ index .Config.Labels \"${label}\" }}" "${image}")" || {
    aws_demo_preflight_fail "検証対象imageをlocal Dockerで確認できません: ${image}"
    return
  }
  if [[ "${value}" != "${expected}" ]]; then
    aws_demo_preflight_fail "image labelがrelease記録と一致しません: ${image} (${label})"
    return
  fi
}

inspect_release_images() {
  local api_image
  local gateway_image
  local release_file="$1"
  local source_commit

  source_commit="$(read_release_field source-commit "${release_file}")" || return
  api_image="$(read_release_field api-image "${release_file}")" || return
  gateway_image="$(read_release_field gateway-image "${release_file}")" || return

  inspect_required_image_label \
    "${source_commit}" "${api_image}" org.opencontainers.image.revision || return
  inspect_required_image_label \
    aws-demo "${api_image}" io.tomidaisai.deployment-environment || return
  inspect_required_image_label \
    "${source_commit}" "${gateway_image}" org.opencontainers.image.revision || return
  inspect_required_image_label \
    aws-demo "${gateway_image}" io.tomidaisai.deployment-environment || return
}

inspect_observability_images() {
  local field
  local image
  local image_id
  local release_file="$1"

  for field in \
    prometheus-image postgres-exporter-image redis-exporter-image node-exporter-image; do
    image="$(read_release_field "${field}" "${release_file}")" || return
    image_id="$(docker image inspect --format '{{.Id}}' "${image}")" || {
      aws_demo_preflight_fail "監視imageをlocal Dockerで確認できません: ${image}"
      return
    }
    [[ "${image_id}" == sha256:* ]] || {
      aws_demo_preflight_fail "監視imageのlocal digestを確認できません: ${image}"
      return
    }
  done
}

inspect_toxiproxy_image() {
  local architecture
  local image
  local image_id
  local release_file="$1"
  local revision
  local version

  image="$(read_release_field toxiproxy-image "${release_file}")" || return
  image_id="$(docker image inspect --format '{{.Id}}' "${image}")" || {
    aws_demo_preflight_fail "Toxiproxy imageをlocal Dockerで確認できません: ${image}"
    return
  }
  [[ "${image_id}" == sha256:* ]] || {
    aws_demo_preflight_fail "Toxiproxy imageのlocal digestを確認できません: ${image}"
    return
  }
  version="$(
    docker image inspect \
      --format '{{ index .Config.Labels "org.opencontainers.image.version" }}' \
      "${image}"
  )" || return
  revision="$(
    docker image inspect \
      --format '{{ index .Config.Labels "org.opencontainers.image.revision" }}' \
      "${image}"
  )" || return
  architecture="$(docker image inspect --format '{{.Architecture}}' "${image}")" || return
  if [[ "${version}" != "2.12.0" \
    || "${revision}" != "3ccd6a79cbc6c6a72b884d295ad314b75cdf3962" ]]; then
    aws_demo_preflight_fail "Toxiproxy imageが承認済みv2.12.0 buildと一致しません"
    return
  fi
  case "${architecture}" in
    amd64 | arm64) ;;
    *)
      aws_demo_preflight_fail "Toxiproxy image architectureが承認対象外です"
      return
      ;;
  esac
}

run_aws_demo_compose() {
  local release_file="$1"
  local round_scope
  local -a compose_files=(--file "${AWS_PREFLIGHT_COMPOSE_FILE}")
  shift

  round_scope="$(read_release_field round-scope "${release_file}")" || return
  case "${round_scope}" in
    single-round) ;;
    rehearsal-six-round)
      compose_files+=(--file "${AWS_PREFLIGHT_REHEARSAL_COMPOSE_FILE}")
      ;;
    load-test-ten-round)
      compose_files+=(--file "${AWS_PREFLIGHT_LOAD_TEST_COMPOSE_FILE}")
      ;;
    *)
      aws_demo_preflight_fail "release記録のRound scopeが不正です"
      return
      ;;
  esac

  docker compose \
    --env-file "${release_file}" \
    "${compose_files[@]}" \
    --project-directory "${AWS_PREFLIGHT_REPOSITORY_ROOT}/deploy/aws-demo" \
    --project-name gs-app-tomidaisai-aws-demo \
    "$@"
}

aws_demo_preflight_main() {
  local release_file=""
  local rollback_file=""
  local rollback_operation=false

  while [[ $# -gt 0 ]]; do
    case "$1" in
      --release-file)
        [[ $# -ge 2 ]] || {
          aws_demo_preflight_usage >&2
          return 2
        }
        release_file="$2"
        shift 2
        ;;
      --rollback-file)
        [[ $# -ge 2 ]] || {
          aws_demo_preflight_usage >&2
          return 2
        }
        rollback_file="$2"
        shift 2
        ;;
      --rollback-operation)
        rollback_operation=true
        shift
        ;;
      -h | --help)
        aws_demo_preflight_usage
        return 0
        ;;
      *)
        aws_demo_preflight_usage >&2
        return 2
        ;;
    esac
  done

  [[ -n "${release_file}" ]] || {
    aws_demo_preflight_usage >&2
    return 2
  }
  if [[ "${rollback_operation}" == true && -z "${rollback_file}" ]]; then
    aws_demo_preflight_usage >&2
    return 2
  fi
  reject_aws_demo_ambient_environment || return
  require_aws_demo_preflight_command uv || return
  require_aws_demo_preflight_command docker || return
  require_aws_demo_preflight_command hostname || return

  local -a validator_arguments=(--release-file "${release_file}")
  if [[ -n "${rollback_file}" ]]; then
    validator_arguments+=(--rollback-file "${rollback_file}")
  elif [[ "$(read_release_field round-scope "${release_file}")" != rehearsal-six-round ]]; then
    aws_demo_preflight_fail "rehearsal-six-round以外には--rollback-fileが必要です"
    return
  fi
  if [[ "${rollback_operation}" == true ]]; then
    validator_arguments+=(--rollback-operation)
  fi
  run_aws_demo_validator "${validator_arguments[@]}" || return

  verify_aws_demo_execution_target "${release_file}" || return
  run_aws_demo_docker_preflight || return
  inspect_release_images "${release_file}" || return
  if [[ "$(read_release_field round-scope "${release_file}")" == load-test-ten-round ]]; then
    verify_load_test_docker_engine || return
    inspect_observability_images "${release_file}" || return
    inspect_toxiproxy_image "${release_file}" || return
  fi
  if [[ "${rollback_operation}" != true && -n "${rollback_file}" ]]; then
    inspect_release_images "${rollback_file}" || return
    if [[ "$(read_release_field round-scope "${rollback_file}")" == load-test-ten-round ]]; then
      inspect_observability_images "${rollback_file}" || return
      inspect_toxiproxy_image "${rollback_file}" || return
    fi
  fi

  # `config --quiet`だけを使い、secretを含む展開済みComposeを標準出力へ出さない。
  run_aws_demo_compose "${release_file}" config --quiet || return

  # 専用containerはSettings、DBのread-only検証、Redis PINGだけを実行し、app serviceは起動しない。
  run_aws_demo_compose "${release_file}" \
    --profile preflight run --rm --no-deps --pull never preflight || return
  if [[ "${rollback_operation}" != true && -n "${rollback_file}" ]]; then
    run_aws_demo_compose "${rollback_file}" \
      --profile preflight run --rm --no-deps --pull never preflight || return
  fi

  printf 'AWS demo preflight passed. No API or gateway service was deployed or published.\n'
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  aws_demo_preflight_main "$@"
fi

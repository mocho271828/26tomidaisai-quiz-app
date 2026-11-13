#!/usr/bin/env bash
set -Eeuo pipefail

# 既存のlocal endpoint判定を共有し、LAN準備からremote Dockerへ到達させない。
# shellcheck source-path=SCRIPTDIR
# shellcheck source=restart-local.sh
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/restart-local.sh"
cd "$RESTART_REPOSITORY_ROOT"
umask 077

LAN_SERVER_DIRECTORY="$RESTART_REPOSITORY_ROOT/.local/lan-server"
LAN_CLIENT_DIRECTORY="$RESTART_REPOSITORY_ROOT/.local/lan-client"
LAN_ENV_FILE="$LAN_SERVER_DIRECTORY/runtime.env"
LAN_CERT_DIRECTORY="$LAN_SERVER_DIRECTORY/certificates"
LAN_URL='https://10.0.1.2:8443'
LAN_VOLUME='gs-app-tomidaisai_postgres-data'
readonly LAN_SERVER_DIRECTORY LAN_CLIENT_DIRECTORY LAN_ENV_FILE LAN_CERT_DIRECTORY LAN_URL LAN_VOLUME

fail() { printf 'LAN: %s\n' "$1" >&2; return 1; }

require_ip() {
  # ${...}はshell展開ではなくJavaScriptのtemplate literalとして渡す。
  # shellcheck disable=SC2016
  node -e '
    const os = require("node:os");
    if (!Object.values(os.networkInterfaces()).flat().some(entry => entry?.address === process.argv[1])) {
      console.error(`LAN: ${process.argv[1]} がこのPCにありません。LAN接続とDHCP予約を確認してください。`);
      process.exit(1);
    }
  ' "$1"
}

require_clean_source() {
  [[ -z "$(git status --porcelain)" ]] || fail '未commitの変更があります。アプリと別PCの版を揃えてから準備してください。'
}

tls() { "$RESTART_SCRIPT_DIRECTORY/local-tls.sh" "$1" "$LAN_ENV_FILE" dual; }

tls_config() {
  docker compose --project-name gs-app-tomidaisai-tls --env-file "$LAN_ENV_FILE" \
    -f compose.yaml -f compose.local-multi-api.yaml \
    -f compose.local-tls.yaml -f compose.local-tls-multi-api.yaml config --format json
}

check_config() {
  [[ -f "$LAN_ENV_FILE" ]] || fail '先に make lan-server-prepare を実行してください。'
  tls_config | "$RESTART_REPOSITORY_ROOT/backend/.venv/bin/python" -m validation.lan_test_config --check
}

docker_ready() {
  require_restart_command docker
  require_local_docker_endpoint
  run_docker_preflight false
}

check_volume() {
  local owner
  owner="$(docker volume inspect "$LAN_VOLUME" --format '{{index .Labels "com.docker.compose.project"}}')"
  [[ "$owner" == gs-app-tomidaisai ]] || fail '通常ローカルDBのvolume所有情報が一致しません。初期化せず停止します。'
}

check_tls_project() {
  local containers container mounted_volume
  containers="$(docker ps -aq --filter label=com.docker.compose.project=gs-app-tomidaisai-tls \
    --filter label=com.docker.compose.service=postgres)"
  for container in $containers; do
    mounted_volume="$(docker inspect --format '{{range .Mounts}}{{if eq .Destination "/var/lib/postgresql/data"}}{{.Name}}{{end}}{{end}}' "$container")"
    [[ "$mounted_volume" == "$LAN_VOLUME" ]] || fail '既存TLS projectが別DBを使用しています。その試験を終了してから切り替えてください。'
  done
}

image_ids() {
  docker image inspect gs-app-tomidaisai-tls-api gs-app-tomidaisai-tls-gateway --format '{{.Id}}'
}

publish_client_files() {
  mkdir -p "$LAN_CLIENT_DIRECTORY"
  cp "$LAN_CERT_DIRECTORY/ca.crt" "$LAN_CLIENT_DIRECTORY/ca.crt"
  node --input-type=module - "$LAN_CLIENT_DIRECTORY" <<'NODE'
import {X509Certificate} from 'node:crypto';
import {readFileSync, writeFileSync} from 'node:fs';
import {execFileSync} from 'node:child_process';
import {join} from 'node:path';
const directory = process.argv[2];
const sourceCommit = execFileSync('git', ['rev-parse', 'HEAD'], {encoding: 'utf8'}).trim();
const ca = new X509Certificate(readFileSync(join(directory, 'ca.crt')));
writeFileSync(join(directory, 'server.json'), JSON.stringify({sourceCommit,
  baseUrl: 'https://10.0.1.2:8443', caSha256: ca.fingerprint256.replaceAll(':', '').toLowerCase()}, null, 2) + '\n', {mode: 0o600});
NODE
  image_ids > "$LAN_SERVER_DIRECTORY/images.txt"
  git rev-parse HEAD > "$LAN_SERVER_DIRECTORY/source-commit.txt"
  printf '別PCへ %s 内の ca.crt と server.json だけをコピーしてください。\n' "$LAN_CLIENT_DIRECTORY"
  openssl x509 -in "$LAN_CLIENT_DIRECTORY/ca.crt" -noout -fingerprint -sha256
}

check_build() {
  require_clean_source
  [[ -f "$LAN_SERVER_DIRECTORY/source-commit.txt" && -f "$LAN_SERVER_DIRECTORY/images.txt" ]] || \
    fail 'build記録がありません。make lan-server-prepare を実行してください。'
  [[ "$(git rev-parse HEAD)" == "$(cat "$LAN_SERVER_DIRECTORY/source-commit.txt")" ]] || \
    fail '準備後にcommitが変わりました。LAN接続前にprepareを再実行してください。'
  [[ "$(image_ids)" == "$(cat "$LAN_SERVER_DIRECTORY/images.txt")" ]] || \
    fail '準備後にDocker imageが変わりました。prepareを再実行してください。'
  docker image inspect postgres:16.10-alpine redis:7.4.5-alpine >/dev/null
}

check_certificates() {
  local service
  for service in gateway postgres redis; do
    openssl verify -CAfile "$LAN_CERT_DIRECTORY/ca.crt" -purpose sslserver \
      "$LAN_CERT_DIRECTORY/$service/server.crt" >/dev/null || \
      fail '試験証明書が不正または期限切れです。docs/LOCAL_LAN_PRESETS.mdの更新手順を確認してください。'
  done
}

server_prepare() {
  local preparation_commit
  require_clean_source
  preparation_commit="$(git rev-parse HEAD)"
  docker_ready
  check_volume
  uv sync --project backend --frozen
  mkdir -p "$LAN_SERVER_DIRECTORY"
  if [[ ! -d "$LAN_CERT_DIRECTORY" ]]; then
    uv run --project backend --no-sync python -m validation.tls_local_certificates \
      --directory "$LAN_CERT_DIRECTORY" --gateway-host 10.0.1.2
  fi
  if [[ ! -f "$LAN_ENV_FILE" ]]; then
    run_local_compose config --format json | \
      uv run --project backend --no-sync python -m validation.lan_test_config
  fi
  check_config
  check_certificates
  tls build
  docker pull postgres:16.10-alpine
  docker pull redis:7.4.5-alpine
  require_clean_source
  [[ "$(git rev-parse HEAD)" == "$preparation_commit" ]] || fail 'build中にcommitが変わりました。prepareを再実行してください。'
  publish_client_files
  printf 'LANサーバー準備完了。サービスの停止・起動とDB初期化は行っていません。\n'
}

server_start() {
  require_ip 10.0.1.2
  docker_ready
  check_config
  check_build
  check_certificates
  check_volume
  check_tls_project
  printf '通常HTTP環境を停止し、同じDBを保持してHTTPSへ切り替えます。\n'
  run_local_compose down
  tls up-offline
  server_check
  printf '運営画面: %s/operator\n' "$LAN_URL"
}

server_check() {
  curl --noproxy '*' --fail --silent --show-error --connect-timeout 9 --max-time 30 \
    --cacert "$LAN_CERT_DIRECTORY/ca.crt" "$LAN_URL/api/health/ready"
  printf '\n'
}

ca_action() {
  local role="$1" action="$2" ca fingerprint keychain
  [[ "$(uname -s)" == Darwin ]] || fail 'CAプリセットはmacOS用です。Linux/WSLはdocs/LOCAL_LAN_PRESETS.mdを参照してください。'
  if [[ "$role" == server ]]; then ca="$LAN_CERT_DIRECTORY/ca.crt"; else ca="$LAN_CLIENT_DIRECTORY/ca.crt"; fi
  [[ -f "$ca" ]] || fail 'CAファイルがありません。準備・コピーを先に行ってください。'
  fingerprint="$(openssl x509 -in "$ca" -noout -fingerprint -sha256)"
  fingerprint="${fingerprint#*=}"
  fingerprint="${fingerprint//:/}"
  keychain="$HOME/Library/Keychains/login.keychain-db"
  printf '対象CA SHA-256: %s\n' "$fingerprint"
  if [[ "$action" == trust ]]; then
    security add-trusted-cert -r trustRoot -p ssl -k "$keychain" "$ca"
    printf 'このユーザーのSSL信頼へ登録しました。ブラウザを再起動してください。\n'
  else
    security delete-certificate -t -Z "$fingerprint" "$keychain"
  fi
}

network_status() {
  # JavaScript側でinterface情報を整形するためshell展開しない。
  # shellcheck disable=SC2016
  node -e 'for (const [name, entries] of Object.entries(require("node:os").networkInterfaces()))
    for (const entry of entries ?? []) if (entry.family === "IPv4") console.log(`${name}: ${entry.address}/${entry.netmask}`);'
  if [[ "$(uname -s)" == Darwin ]]; then
    networksetup -listnetworkserviceorder
    route -n get default || true
    route -n get 10.0.1.2 || true
    route -n get 10.0.1.3 || true
  else
    ip route
  fi
  printf 'WANなしLANでInternet接続を維持する手順: docs/LOCAL_LAN_PRESETS.md\n'
}

[[ $# -eq 1 ]] || { printf 'Usage: make help (LAN presets)\n' >&2; exit 2; }
case "$1" in
  network) network_status ;;
  server-prepare) server_prepare ;;
  server-start) server_start ;;
  server-check) require_ip 10.0.1.2; server_check ;;
  server-status) docker_ready; check_config; check_tls_project; tls ps ;;
  server-stop) docker_ready; check_config; check_tls_project; tls down ;;
  server-observe)
    docker_ready; check_config; check_build; check_tls_project; server_check
    mkdir -p .local/lan-observations
    observation="$(mktemp -d "$RESTART_REPOSITORY_ROOT/.local/lan-observations/run.XXXXXX")"
    # 観測器は新規directoryを排他作成するので、親だけ予約して子を渡す。
    exec "$RESTART_SCRIPT_DIRECTORY/observe-local-tls.sh" "$LAN_ENV_FILE" "$observation/evidence" "${LAN_OBSERVE_SECONDS:-3600}" gs-app-tomidaisai-tls dual
    ;;
  server-ca-trust) ca_action server trust ;;
  server-ca-remove) ca_action server remove ;;
  client-ca-trust) ca_action client trust ;;
  client-ca-remove) ca_action client remove ;;
  client-prepare)
    pnpm --dir load-test/browser install --frozen-lockfile
    if [[ "$(uname -s)" == Linux ]]; then
      pnpm --dir load-test/browser exec playwright install --with-deps chromium
    else
      pnpm --dir load-test/browser exec playwright install chromium
    fi
    pnpm --dir load-test/browser build
    ;;
  client-check|client-50|client-600)
    require_ip 10.0.1.3
    # checkoutだけ更新して古いdistを起動しない。インストールや通信は行わない。
    pnpm --dir load-test/browser build
    pnpm --dir load-test/browser "lan:${1#client-}"
    ;;
  *) printf 'Unknown LAN preset. Run make help.\n' >&2; exit 2 ;;
esac

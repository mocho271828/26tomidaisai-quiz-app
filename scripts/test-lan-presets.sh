#!/usr/bin/env bash
set -Eeuo pipefail

TEST_SOURCE_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
TEST_DIRECTORY="$(mktemp -d)"
trap 'rm -rf -- "$TEST_DIRECTORY"' EXIT
mkdir -p "$TEST_DIRECTORY/scripts" "$TEST_DIRECTORY/bin" "$TEST_DIRECTORY/backend/.venv/bin" \
  "$TEST_DIRECTORY/.local/lan-server"
cp "$TEST_SOURCE_ROOT"/scripts/{lan-test,local-tls,restart-local,docker-preflight}.sh "$TEST_DIRECTORY/scripts/"
printf 'fixture\n' > "$TEST_DIRECTORY/.local/lan-server/runtime.env"
printf '0123456789012345678901234567890123456789\n' > "$TEST_DIRECTORY/.local/lan-server/source-commit.txt"
printf 'sha256:api\nsha256:gateway\n' > "$TEST_DIRECTORY/.local/lan-server/images.txt"

cat > "$TEST_DIRECTORY/bin/docker" <<'SH'
#!/usr/bin/env bash
set -Eeuo pipefail
printf '%s\n' "$*" >> "$LAN_TEST_CALLS"
case "$1 $2" in
  'context show') printf 'fixture\n' ;;
  'context inspect') printf '%s\n' "${LAN_TEST_ENDPOINT:-unix:///tmp/fixture.sock}" ;;
  'compose version') printf 'fixture\n' ;;
  'volume inspect') printf '%s\n' "${LAN_TEST_VOLUME_OWNER:-gs-app-tomidaisai}" ;;
  'image inspect') printf 'sha256:api\nsha256:gateway\n' ;;
  'ps -aq') [[ "${LAN_TEST_OTHER_TLS:-false}" != true ]] || printf 'other-tls\n' ;;
  'ps -q') ;;
  'inspect --format') printf 'another-database\n' ;;
  compose\ *)
    if [[ "$*" == *'config --format json'* ]]; then
      printf '{"volumes":{"postgres-data":{"name":"gs-app-tomidaisai_postgres-data"}}}\n'
    fi
    ;;
  'run --rm') ;;
  *) exit 64 ;;
esac
SH
cat > "$TEST_DIRECTORY/bin/curl" <<'SH'
#!/usr/bin/env bash
printf 'curl %s\n' "$*" >> "$LAN_TEST_CALLS"
printf 'OK'
SH
cat > "$TEST_DIRECTORY/bin/node" <<'SH'
#!/usr/bin/env bash
printf 'node\n' >> "$LAN_TEST_CALLS"
exit "${LAN_TEST_IP_STATUS:-0}"
SH
cat > "$TEST_DIRECTORY/bin/git" <<'SH'
#!/usr/bin/env bash
case "$1" in
  status) ;;
  rev-parse) printf '0123456789012345678901234567890123456789\n' ;;
  *) exit 64 ;;
esac
SH
cat > "$TEST_DIRECTORY/bin/openssl" <<'SH'
#!/usr/bin/env bash
exit "${LAN_TEST_CERT_STATUS:-0}"
SH
cat > "$TEST_DIRECTORY/backend/.venv/bin/python" <<'SH'
#!/usr/bin/env bash
cat >/dev/null
exit "${LAN_TEST_CONFIG_STATUS:-0}"
SH
chmod +x "$TEST_DIRECTORY/bin/"* "$TEST_DIRECTORY/backend/.venv/bin/python"

run_case() {
  local action="$1"
  shift
  : > "$TEST_DIRECTORY/calls"
  LAN_TEST_STATUS=0
  # 実Docker・OS信頼・ブラウザへ接続しない専用fixtureで順序と拒否を検証する。
  env -i PATH="$TEST_DIRECTORY/bin:/usr/bin:/bin" HOME="$TEST_DIRECTORY" \
    LAN_TEST_CALLS="$TEST_DIRECTORY/calls" "$@" \
    "$TEST_DIRECTORY/scripts/lan-test.sh" "$action" > "$TEST_DIRECTORY/output" 2>&1 || LAN_TEST_STATUS=$?
}

assert_status() {
  [[ "$LAN_TEST_STATUS" == "$1" ]] || { cat "$TEST_DIRECTORY/output" >&2; exit 1; }
}

assert_no_switch() {
  if grep -Eq 'compose .* (down|up)( |$)' "$TEST_DIRECTORY/calls"; then
    cat "$TEST_DIRECTORY/calls" >&2
    exit 1
  fi
}

run_case unknown
assert_status 2
[[ ! -s "$TEST_DIRECTORY/calls" ]]
run_case server-start LAN_TEST_IP_STATUS=1
assert_status 1
[[ "$(cat "$TEST_DIRECTORY/calls")" == node ]]
run_case server-start LAN_TEST_ENDPOINT=ssh://remote.invalid
assert_status 1
assert_no_switch
run_case server-start LAN_TEST_CONFIG_STATUS=1
assert_status 1
assert_no_switch
run_case server-start LAN_TEST_VOLUME_OWNER=unrelated-project
assert_status 1
assert_no_switch
run_case server-start LAN_TEST_CERT_STATUS=1
assert_status 1
assert_no_switch
run_case server-start LAN_TEST_OTHER_TLS=true
assert_status 1
assert_no_switch

run_case server-start
assert_status 0
http_stop="$(grep -n 'project-name gs-app-tomidaisai down$' "$TEST_DIRECTORY/calls" | cut -d: -f1)"
tls_start="$(grep -n ' up -d --pull never --no-build ' "$TEST_DIRECTORY/calls" | cut -d: -f1)"
[[ -n "$http_stop" && -n "$tls_start" && "$http_stop" -lt "$tls_start" ]]
grep -q 'run --rm --pull=never --network none --read-only' "$TEST_DIRECTORY/calls"
if grep -Eq -- '--volumes| migrate| seed| build| pull ' "$TEST_DIRECTORY/calls"; then
  cat "$TEST_DIRECTORY/calls" >&2
  exit 1
fi

run_case server-stop
assert_status 0
grep -q 'project-name gs-app-tomidaisai-tls .* down --remove-orphans$' "$TEST_DIRECTORY/calls"
if grep -Eq -- '--volumes|--project-name gs-app-tomidaisai down|security ' "$TEST_DIRECTORY/calls"; then exit 1; fi

run_case server-check
assert_status 0
grep -Fq -- '--connect-timeout 9 --max-time 30' "$TEST_DIRECTORY/calls"
grep -Fq -- "--cacert $TEST_DIRECTORY/.local/lan-server/certificates/ca.crt https://10.0.1.2:8443/api/health/ready" "$TEST_DIRECTORY/calls"
assert_no_switch
printf 'LAN preset mock tests passed.\n'

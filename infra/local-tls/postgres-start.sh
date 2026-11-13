#!/bin/sh
set -eu
# 空volumeを公式entrypointのinitdbへ渡さず、人の初期化へ戻す。
if [ ! -s "${PGDATA}/PG_VERSION" ]; then
  echo 'local_tls_existing_postgres_required' >&2
  exit 64
fi
mkdir -p /run/postgres-tls
cp /run/tls/server.key /run/postgres-tls/server.key
cp /run/tls/server.crt /run/postgres-tls/server.crt
chown -R postgres:postgres /run/postgres-tls
chmod 700 /run/postgres-tls
chmod 600 /run/postgres-tls/server.key
exec docker-entrypoint.sh postgres -c ssl=on -c ssl_cert_file=/run/postgres-tls/server.crt -c ssl_key_file=/run/postgres-tls/server.key

#!/bin/sh
set -eu
mkdir -p /run/redis-tls
cp /run/tls/server.key /run/redis-tls/server.key
cp /run/tls/server.crt /run/redis-tls/server.crt
cp /run/tls/ca.crt /run/redis-tls/ca.crt
# AUTH値をprocess引数やCompose configへ展開しない。
umask 077
printf 'requirepass %s\n' "$REDIS_PASSWORD" > /run/redis-tls/auth.conf
chown -R redis:redis /run/redis-tls
chmod 700 /run/redis-tls
exec docker-entrypoint.sh redis-server --port 0 --tls-port 6379 --tls-cert-file /run/redis-tls/server.crt --tls-key-file /run/redis-tls/server.key --tls-ca-cert-file /run/redis-tls/ca.crt --tls-auth-clients no --include /run/redis-tls/auth.conf

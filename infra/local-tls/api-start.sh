#!/bin/sh
set -eu
# 通常OS CAを残したままlocal CAを追加し、軽い専用CAだけで比較しない。
cat /etc/ssl/certs/ca-certificates.crt /run/local-ca/ca.crt > /run/quiz-tls/system-plus-local-ca.pem
exec uvicorn quiz_app.main:app --host 0.0.0.0 --port 8000 --timeout-keep-alive 5 --no-access-log

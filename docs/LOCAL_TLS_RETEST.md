# TLS Localdemo 再試験手順

今回の対象は専用local環境・6問・100独立ブラウザと実端末約5台。AWS再作成・増強・600VU/10問正式受入は含めない。ブラウザ操作・別PC分割・判定条件は[ブラウザ再試験](LOCAL_BROWSER_RETEST.md)を参照する。

2 APIで確認するときは[2 APIの起動・観測手順](#2-apiでの起動と観測)に従い、起動scriptの第3引数と観測scriptの第5引数へ`dual`を指定する。単一containerにUvicorn workerを追加せず、両APIの設定・readiness・metricsを確認する。

手動runnerを使う場合は[50〜650VU手順](LOCAL_BROWSER_CONTROL.md)を参照する。新規開催の参加者IDは[参加者IDの準備](handover/README.md#参加者idの準備)を参照する。

## 通信経路

| 経路 | 設定 | 確認 |
| --- | --- | --- |
| browser → gateway | HTTPS/WSS、SANに一致する接続先、試験用CA | browserの警告なし、Secure Cookie、WSS 101 |
| gateway → API | HTTP `api:8000` | 当日の構成を維持 |
| API → Redis | `rediss://`、AUTH、TLS server検証、通常OS CA＋local CA | 実handshake、PING、未知CA/hostname不一致拒否 |
| API → PostgreSQL | libpq `PGSSLMODE=verify-full` と `PGSSLROOTCERT` | `pg_stat_ssl` と接続probe、承認済みDB準備後に実測 |

Redisは当日設定のpassword認証・client証明書指定なしに合わせ、`tls-auth-clients no` とAUTHを組み合わせる。サーバー証明書のCA/hostname検証は維持する。Redisの平文portは無効。PostgreSQLは既存認証/pg_hbaを変更せずAPI・healthcheck双方をverify-fullに固定する。DB/Redis/metricsのhost portを開けず、LAN公開はgatewayだけ。

`compose.local-tls.yaml` は `compose.yaml` のoverride。Composeの `!override` 対応版が必要（確認環境5.4.0）。migration/seed/旧HTTP acceptance serviceのentrypointは明示拒否に変えてある。通常の起動scriptはschema適用を一切行わず、external volumeとPG_VERSIONを事前確認する。

Caddy 2.10.2既存imageのBusyBox wgetに `--ca-certificate` がなかったため、runtimeのOS依存としてcurlを追加した。TLS healthcheckは `curl --cacert`。Python/Nodeの新規ライブラリはTLS構成に追加していない。CA生成には既存cryptography/Pydanticを使う。

## CAと設定を準備する

全コマンドはrepoルートで実行する。以下の例のLAN IP `192.168.1.20` は実サーバーIPへ置き換える。DNSを使う場合は解決済みの名前を `--gateway-host` に追加する。CAはOSへ自動登録しない。

```bash
uv run --project backend python -m validation.tls_local_certificates \
  --directory .local/tls/certificates-lan \
  --gateway-host localhost --gateway-host 192.168.1.20
```

生成先は非上書き。CA有効30日、leaf14日。CA秘密鍵はleaf directoryと分離し、秘密鍵0600・directory0700。端末へ配布するのは **ca.crtだけ**。containerへCA秘密鍵をmountしない。loopback用の既定のCAは `.local/tls/certificates/ca.crt` にある。SANが増える場合は新directoryを使い、実行記録にCA指紋変更を記載する。

新規TLS環境では、通常のローカル環境と同じバックアップ保存先を使う。まず `make local-env` でこの環境の専用鍵を用意し、以下の例で `.env` から鍵だけを読み取って、TLSの設定にも保存する。既存DBを使う場合は、そのDBに対応するパスワードと既存のバックアップ鍵を担当者が確認する。

```bash
make local-env
python3 - <<'PY'
import os
import secrets
from pathlib import Path

key_name = 'ROUND_BACKUP_ENCRYPTION_KEY='
keys = [line.removeprefix(key_name) for line in Path('.env').read_text().splitlines()
        if line.startswith(key_name)]
if len(keys) != 1 or not keys[0]:
    raise SystemExit('既存の .env のバックアップ鍵を担当者と確認してください')

p = Path('.local/tls/runtime.env')
values = {
  'LOCAL_TLS_CERT_DIRECTORY': str(Path('.local/tls/certificates-lan').resolve()),
  'LOCAL_TLS_POSTGRES_VOLUME': '<準備済みの専用PostgreSQL volume名>',
  'LOCAL_TLS_REDIS_PASSWORD': secrets.token_urlsafe(32),
  'POSTGRES_PASSWORD': secrets.token_urlsafe(32),
  'DEMO_OPERATOR_USERNAME': 'demo-operator',
  'DEMO_OPERATOR_PASSWORD': secrets.token_urlsafe(32),
  'SESSION_SECRET': secrets.token_urlsafe(48),
  'ROUND_BACKUP_ENCRYPTION_KEY': keys[0],
  'LOCAL_TLS_ALLOWED_ORIGINS': '["https://localhost:8443","https://192.168.1.20:8443"]',
  'LOCAL_TLS_BIND_ADDRESS': '0.0.0.0',
  'LOCAL_TLS_PORT': '8443',
}
p.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
with os.fdopen(os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'w') as output:
    output.write(''.join(f'{key}={value}\n' for key, value in values.items()))
PY
scripts/local-tls.sh config
```

上の例は既存の `.local/tls/runtime.env` を上書きしない。過去の環境で鍵の欄がない場合は、担当者が保管している既存バックアップ用の鍵を、所有者だけが読み書きできる同ファイルに設定する。復元に必要な鍵はバックアップとともに非公開で引き継ぎ、起動のたびに作り直さない。編集する場合はローカルeditorを使い資格情報を報告書へ貼らない。passwordはURL-safe ASCIIで生成する。Originはscheme・host・portの完全一致で列挙し、wildcardやpathを含めない。運営画面はサーバーPCの `https://localhost:8443/operator` から使い、資格情報を別PCへ配布しない。

## 試験用DBの準備

TLSの起動スクリプトは、`LOCAL_TLS_POSTGRES_VOLUME` に指定した既存volumeとPostgreSQLの初期化状態を確認する。DBの所有者が、試験用DBのschema revisionと架空の問題、未使用の開催、参加者IDの準備を確認してから起動する。別projectで使用中のvolumeには接続しない。通常のHTTP環境で準備済みのDBをLANで使用する場合は[LAN試験の準備・切替手順](LOCAL_LAN_PRESETS.md)に従う。

## 起動・停止・観測

```bash
scripts/local-tls.sh build
scripts/local-tls.sh up
scripts/local-tls.sh ps
curl --fail --cacert .local/tls/certificates/ca.crt https://localhost:8443/api/health/ready
scripts/observe-local-tls.sh .local/tls/runtime.env .local/tls/run-001-observation 600
# 別shellでブラウザrunnerを実行。観測を先に開始する。
scripts/local-tls.sh down
```

CAパスはruntime.envが参照する実CAに合わせる。`down` はvolumeを消さない。資格情報・Cookieを含み得る `docker inspect` 全出力、`docker compose config` 全出力、アプリrawログを証跡へコピーしない。`config --quiet` の成功/失敗だけを記録する。

観測scriptはprojectのcontainer集合を固定し、空の場合は拒否する。計測v2ではAPIごとの常駐collectorで1秒目標のmetricsを採り、15秒目標のDB read-only snapshotとcontainer資源を独立して採る。時刻・monotonic開始・collector所要時間・実間隔・欠測を保存し、1秒等間隔の厳密計測とは扱わない。2 APIでは第5引数`dual`を必須とし、両processのcounter/histogram差分を同じ測定窓で集計する。[計測v2](LOCAL_BROWSER_RETEST.md#計測v2)を参照する。

- `process_open_fds` / `process_max_fds` はLinux API processの使用数/soft上限。container内 `/proc/1/limits` のsoft/hardと併記する。終了後も同じ間隔で採取する。
- HTTP route別count/duration/in-progress、WS active/close、Redis active購読/coalesced/cleanup、TLS context初期化、event loop delay、AnyIO thread借用/待機を観測する。
- DB `quiz_db_connection_acquisition_seconds` はpool queue待ちだけでなくconnect・TLS・pre-pingを含む。pure pool waitと呼ばない。
- `database_snapshot` は `pg_stat_activity` / `pg_stat_ssl` をread-only transactionで集約する。`longest_blocked_statement_seconds` はquery_startからの経過で**lock待ちの上限**。tuple/transactionid待ち件数は行競合の手掛かりで全件の正確な行ロック時間ではない。権限不足はunavailableを残し、GRANT/DB設定変更を自動でしない。
- metrics labelにrun/Event/Round/participant IDを入れない。browserの序数・run記録とサーバー時刻の窓で関連付ける。raw trace/HARは別途許可された非秘密形式だけ。

FD予算は `基礎FD + 同時HTTP/WSS socket + Redis購読/command接続 + DB pool接続 + reconnect/TLS中の一時重複 + 観測処理 + 余裕` として各項を実測する。100接続維持・一斉切断/再接続・終了後を比較し、FDが戻ることとsoft/hard余裕を確認する。固定512/1024等を解決値として採用していない。

必要性が測れた場合だけ、次の追加overrideをGit外に作り、測定から選んだ値を設定する。APIとgatewayの予算は分離し、比較前後の上限変更を記録する。

```yaml
# .local/tls/fd-budget.yaml
services:
  api:
    ulimits:
      nofile:
        soft: ${LOCAL_TLS_API_NOFILE:?measured API FD budget required}
        hard: ${LOCAL_TLS_API_NOFILE:?measured API FD budget required}
  gateway:
    ulimits:
      nofile:
        soft: ${LOCAL_TLS_GATEWAY_NOFILE:?measured gateway FD budget required}
        hard: ${LOCAL_TLS_GATEWAY_NOFILE:?measured gateway FD budget required}
```

人手でruntime.envに2つの値を追記して、起動時のCompose引数末尾へ `-f .local/tls/fd-budget.yaml` を追加する。通常scriptの再実行でoverrideを失わないよう、そのrunの全起動コマンドを記録する。上限を上げただけでFD leak・購読cleanup・TLS失敗を解消したとは判断しない。

## 2 APIでの起動と観測

HTTPは以下の構成で、既存の初期化済みDBを使う。

```sh
docker compose -f compose.yaml -f compose.local-multi-api.yaml build api gateway
docker compose -f compose.yaml -f compose.local-multi-api.yaml up -d --no-deps --wait api api-secondary gateway
```

TLSは`compose.yaml` → `compose.local-multi-api.yaml` → `compose.local-tls.yaml` → `compose.local-tls-multi-api.yaml`の順。Compose `extends`は後続overrideの設定を自動継承しないため、secondaryのTLS境界を専用overrideで明示している。HTTP/TLSとも合成後の両API設定がinstance ID以外一致することを回帰検証する。独自の後続overrideを使う場合も両APIへ適用する。

```sh
scripts/local-tls.sh config .local/tls/runtime.env dual
scripts/local-tls.sh build .local/tls/runtime.env dual
scripts/local-tls.sh up .local/tls/runtime.env dual
scripts/observe-local-tls.sh .local/tls/runtime.env .local/new-dual-observation 300 gs-app-tomidaisai-tls dual
```

観測出力は`api/<sample>.prom`と`api-secondary/<sample>.prom`に分け、採取時刻も各processのscrape直前に記録する。single指定でsecondaryが動いていたら拒否し、片側だけの計測を全体値として扱わない。DB snapshotとcontainer statsは出力rootへ保存する。TLSのsingleへの切替・downでは同projectのorphanを除去し、volumeは削除しない。

HTTPをsingleへ戻す場合は`docker compose -f compose.yaml up -d --remove-orphans --wait api gateway`を使う。`restart-local.sh`と`make local-restart`は2 API構成を標準とし、上記と同じoverrideを使う。画面だけの更新は`make local-frontend`でgatewayだけを再作成する。通常のTLS起動は既存volume/PG_VERSIONを確認し、schema/seedを適用しない。

## 別PC・実端末へのCA信頼と解除

端末所有者が試験用CAの指紋と登録先を確認して一時登録し、終了後に同じ指紋のCAを解除する。

別PC・実端末には`ca.crt` をUSB/AirDrop等の別経路で渡し、サーバー側の `openssl x509 -in ca.crt -noout -fingerprint -sha256` と指紋を照合する。秘密鍵やruntime.envは渡さない。証明書期限、接続先SAN、端末時計も確認する。CA信頼前の警告を「詳細から続行」で回避しない。

- **iPhone/iPad Safari:** ca.crtを開く→設定のダウンロード済みプロファイル（または一般→VPNとデバイス管理）からインストール→一般→情報→証明書信頼設定→該当CAの完全な信頼を有効化。終了後はVPNとデバイス管理から今回のプロファイルだけ削除し、信頼設定から消えたことを確認する。[AppleのSSL信頼手順](https://support.apple.com/en-ie/102390)
- **Android/Chrome:** 設定→セキュリティとプライバシー→その他のセキュリティ設定→暗号化と認証情報→証明書をインストール→CA証明書、でca.crtを選ぶ（OS/OEMにより名称差あり）。終了後はユーザー認証情報から今回のCAだけ削除する。会社管理端末で許可されない場合は未実施として記録する。[Googleの証明書追加/削除](https://support.google.com/pixelphone/answer/2844832?hl=en)
- **macOS Safari/Chrome:** キーチェーンアクセスでca.crtをログインキーチェーンへ読み込み、今回のCAをダブルクリック→信頼→SSLの信頼を設定する。browser再起動後に警告なく開くことを確認。終了後は指紋一致のCAだけを削除する。[Appleの信頼設定](https://support.apple.com/en-gu/guide/keychain-access/kyca11871/mac)
- **Ubuntu等の別PC:** 所有者が `sudo cp ca.crt /usr/local/share/ca-certificates/quiz-local-test.crt` と `sudo update-ca-certificates` を実行。終了後はその1ファイルだけ削除して同コマンドを再実行する。[UbuntuのCA登録](https://ubuntu.com/server/docs/how-to/security/install-a-root-ca-certificate-in-the-trust-store/)
- **Linux Chromium/Playwright:** OS CAだけで信頼されたと断定しない。ChromiumのNSS DBにも必要となる。`certutil` が利用可能な環境で、browserが使用するDBを確認し、`certutil -d sql:PATH -A -t 'C,,' -n quiz-local-test -i ca.crt`、解除は `certutil -d sql:PATH -D -n quiz-local-test`。現行既定は `~/.local/share/pki/nssdb`、既存 `~/.pki/nssdb` があればそちらが使われる。使用版・実DBを記録し、runner開始前に実browserで接続を確認する。[ChromiumのLinux証明書管理](https://chromium.googlesource.com/chromium/src/%2B/master/docs/linux/cert_management.md)

同じLANの `https://<サーバーIP>:8443/` を開き、警告なし・登録可能・問題受信待ち表示を確認する。gatewayポート以外をLANへ開けない。実端末のOS/browser/version、CA指紋、接続先、開始/終了/解除完了を記録し、操作・回答受付の記録は[ブラウザ再試験](LOCAL_BROWSER_RETEST.md)へ進む。実機未接続は自動browserで埋めて合格にしない。

## 実TLSprobe再実行

DBに依存しないRedis検査。通常OS CAとlocal CAを連結する。追加した `LOCAL_TLS_CA_BUNDLE` は試験container内のファイルだけを指す。

```bash
docker run --rm --network gs-app-tomidaisai-tls_default \
  --env-file .local/tls/runtime.env -e LOCAL_TLS_CA_BUNDLE=/run/system-plus-local-ca.pem \
  --mount "type=bind,source=$PWD/.local/tls/certificates/ca.crt,target=/run/local-ca.crt,readonly" \
  --mount "type=bind,source=$PWD/validation/tls_local_probe.py,target=/run/tls_local_probe.py,readonly" \
  --entrypoint sh gs-app-tomidaisai-tls-api -c \
  'cat /etc/ssl/certs/ca-certificates.crt /run/local-ca.crt > /run/system-plus-local-ca.pem; python /run/tls_local_probe.py'
```

trusted TLS・未知CAや異なるhostnameの拒否・認証付きPINGを確認する。CA・依存image・観測条件を比較記録に残す。

参考: [Redis TLS設定](https://redis.io/docs/latest/operate/oss_and_stack/management/security/encryption/)、[PostgreSQL 16 TLS](https://www.postgresql.org/docs/16/ssl-tcp.html)、[Caddy証明書設定](https://caddyserver.com/docs/caddyfile/directives/tls)。

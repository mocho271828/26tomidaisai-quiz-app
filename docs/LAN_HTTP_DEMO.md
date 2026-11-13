# LAN HTTP 実機デモ手順

TLSでのブラウザ試験は[検証付きTLS手順](LOCAL_TLS_RETEST.md)と[100ブラウザ手順](LOCAL_BROWSER_RETEST.md)を参照する。

## 目的と到達点

同一 LAN の参加者端末から HTTP でデモへ参加し、回答の保存完了、Round の結果画面（`Reveal`）、検証済み暗号化backup後の次Round、全6問終了後のEvent完了までを確認する。

Round間では前問の回答・得点・準備状態を持ち越さず、次問のpreview・正解が公開前の参加者payloadへ漏れないことも確認する。

## 安全条件

- 架空の `[DEMO]` 問題、local 専用アカウント、local 専用 secret だけを使う。
- 信頼できる同一 LAN 内で一時的に実施し、インターネットへ port forward しない。
- PostgreSQL と Redis は host へ公開せず、Caddy gateway の port だけを公開する。
- 実参加者データ、本番 password／token、実問題、本番 domain を使わない。
- password、Cookie、CSRF token、`.env` の内容を画面収録や検証ログへ残さない。

## 1. 事前条件を確認する

host と参加者端末を同じ LAN へ接続する。VPN、ゲスト Wi-Fi、AP isolation がある場合は端末間通信が遮断されるため、事前に無効化またはネットワーク管理者へ確認する。

host の LAN IPv4 address を確認し、以降の `<LAN_IP>` を実値へ置き換える。macOS の Wi-Fi が `en0` の場合は次で確認できる。

```sh
ipconfig getifaddr en0
```

何も表示されない場合は、使用中 interface の IPv4 address を macOS のネットワーク設定から確認する。IPv4 address を repository の文書や共有ログへ書き込まない。

repository root の `.env` で、少なくとも次を local demo 用に設定する。

```dotenv
APP_ENV=local-demo
GATEWAY_BIND_ADDRESS=0.0.0.0
GATEWAY_PORT=8080
REALTIME_ALLOWED_ORIGINS=["http://localhost:8080","http://<LAN_IP>:8080"]
SESSION_COOKIE_SECURE=false
ENABLE_LOCAL_ROUND_BACKUP=true
```

加えて、`POSTGRES_PASSWORD`、`DEMO_OPERATOR_PASSWORD`、32文字以上の `SESSION_SECRET` を local 専用値へ変更する。`.env` を表示するコマンドや、secret を展開する `docker compose config` は検証記録へ貼り付けない。

Round backupはhostの`./.local/round-backups`へ保存される。このdirectoryはGit管理外だが、回答・得点を含むため共有またはcloud syncせず、local demo終了後の削除判断は人間が行う。初回に `make local-env` で生成した環境専用のバックアップ鍵を使う。

## 2. 自動確認と起動を行う

DB を変更しない品質確認を先に実行する。

```sh
./scripts/test-local.sh
./scripts/docker-preflight.sh
docker compose config --quiet
```

すでにこの repository 専用の local demo DB を bootstrap 済みなら、DB volume を保持したまま再起動する。

```sh
./scripts/restart-local.sh
```

初回だけ、接続先がこの repository 専用の破棄可能な Compose DB であることを人間が確認してから migration と架空データ seed を明示する。

```sh
./scripts/restart-local.sh --bootstrap
```

この操作は local DB schema と demo data を変更する。本番・共有・外部 DB を接続した状態では実行しない。

host から loopback と LAN IP address の両方で readiness を確認する。どちらも HTTP 200 かつ `status` が `ready` になるまでデモを開始しない。

```sh
curl --fail-with-body http://127.0.0.1:8080/api/health/ready
curl --fail-with-body http://<LAN_IP>:8080/api/health/ready
```

参加者端末でも `http://<LAN_IP>:8080/api/health/ready` を開けることを確認する。開けない場合は、host firewall、LAN isolation、VPN、IP address、port を確認する。

架空の問題で1問だけ自動確認する場合は、別の準備済みデモDBで下記を実行する。8個の独立セッションが参加し、その開催のRoundを実際に進行する。

```sh
./scripts/run-demo-acceptance.sh --participant-base-url http://<LAN_IP>:8080 --participants 8
```

## 3. 参加者端末を先に参加させる

参加者端末で次を開く。

```text
http://<LAN_IP>:8080/
```

1. 参加画面が直接表示され、イベント ID の入力欄がないことを確認する。
2. 自動生成IDで参加するボタンを押す。IDの手入力や候補取得は行わない。
3. 参加完了後に半角英字大文字3文字の参加者IDが表示されることを確認する。重複時は画面が別IDで再試行する。
4. 接続表示が「リアルタイム接続中」、または Redis 縮退時は「1秒ごとに更新中」であることを確認する。
5. 参加者端末はこの画面のまま待機する。

問題の配信に余裕を取るため、登録後に運営が配信・開始予約を行う。途中参加も回答窓に従って参加できる。

## 4. 運営画面から回答を開始する

host で次を開く。運営資格情報は参加者端末へ入力しない。

```text
http://localhost:8080/operator
```

1. `.env` の local demo 運営アカウントでログインする。
2. イベントの参加者数が 1 人以上であることを確認する。
3. 操作理由の入力は不要。
4. 問題を変更する場合は、「問題を確定して配信」より先に選択する。
5. 「問題を確定して配信」を押す。
6. 各端末の問題受信・時刻同期を確認し、予約前に十分なマージンを取る。全員ACKや98%率の開始制限はない。
7. 「3秒後に開始予約」を押す。

参加者端末にカウントダウンが表示され、その後に問題文と選択肢が表示されることを確認する。

## 5. LAN HTTP で回答保存を確認する

参加者端末で、回答受付中の10秒以内に選択肢を1回だけ押す。

次を確認する。

- `crypto.randomUUID is not a function` などのエラー画面にならない。
- 選択した回答が「送信中」から「受付済み」になる。
- 「回答を保存しました。変更はできません。」と表示される。
- 全選択肢が lock され、追加 tap で回答が変わらない。
- 運営画面を「読み込む」と、回答数が 1 件以上になる。

対象ブラウザの LAN HTTP 環境で `crypto.randomUUID()` が利用できないことを確認したうえで回答が成功すれば、`crypto.getRandomValues()` を使った UUID v4 で回答 API まで到達できたと判断できる。

## 6. 回答完了から結果画面への遷移を確認する

1. 開始10秒の選択締切で参加者が「正解発表をお待ちください」へ進むことを確認する。開始14秒の到着終端後に集計が完了するまで待つ。
2. 運営画面で `ReadyToReveal + Withheld` と正解・統計previewを確認する。参加者へ正解が出ていないことを確認し、司会の発表に合わせて「正解・結果を公開」を押して `Reveal + Published` へ進める。
3. 参加者端末が同じ URL のまま結果画面へ更新されることを確認する。
4. 「マイスコア」「みんなの回答」を切り替え、「正解！」「不正解」「未回答」のいずれか、今回得点、正解の選択肢、公開設定で許可された回答件数が表示されることを確認する。
5. 本人順位が公開されている場合は「ランキング」タブを確認する。全体順位も公開されている場合は参加者画面にTOP3が表示されることを確認する。公開画面は使用しない。

Roundが`VoidCandidate`になった場合は、運営画面に表示された安全なエラーコードを記録して原因を直す。その後、「結果集計を再試行」を1回押す。通常進行ではこの復旧操作を使わない。

WebSocket が利用できない場合も、「1秒ごとに更新中」と表示されていれば polling により結果画面へ進む。数秒待っても変わらない場合は participant を再読込し、API readiness と運営画面のRound状態・エラーコードを確認する。

## 7. backupを検証して次Roundへ進む

最初の5問では、Reveal後の運営画面で未使用の次問を選ぶ。次問の本文、全選択肢、正解を運営画面だけで確認し、「次の問題へ進む」を1回押す。

次を確認する。

- 処理中は二重送信できず、失敗時は次Roundを作成せず再試行案内が出る。
- 成功時はhostの`./.local/round-backups`へ暗号化artifactが1件増える。
- 運営画面のRound IDが前問と異なり、状態が`PreparingAssets`になる。
- 参加者画面は次問の正解や全候補問題を表示しない。`PreparingAssets`では直前の公開結果を表示し、`ReadyToSchedule`で問題受信と時刻同期を確認すると開始待機へ切り替わる。
- 次Roundが`Scheduled`になると3秒カウントダウン、`Open`になると回答中の画面へ切り替わり、前問の回答受付状態や準備完了を持ち越さない。
- 使用済み問題は次問候補に戻らない。

新Roundごとに「問題を確定して配信」から同じ手順を繰り返す。全6問（練習1問＋本番5問）を進める。

6問目のReveal後は「Eventを完了」を押す。最終backupの復号・件数・SHA-256照合に成功した場合だけEventが`Completed`となり、新Roundが作成されないことを確認する。このlocal artifactは本番restoreを検証したものではない。

## 8. 証跡チェックリスト

- [ ] loopback と LAN IP address の readiness が HTTP 200 `ready` だった。
- [ ] 参加者端末を運営が配信して予約前のマージンを記録した。
- [ ] LAN HTTP の参加者端末に半角英字大文字3文字の参加者IDと接続状態が表示された。
- [ ] 10秒の回答時間内に1回だけ回答した。
- [ ] 「受付済み」と保存完了メッセージを確認した。
- [ ] 運営画面の回答数が増えた。
- [ ] 開始14秒の到着終端後に集計が完了しても参加者は発表待ちを維持し、運営が公開ボタンを押した後に `Reveal + Published` と結果表示へ進んだ。
- [ ] 参加者端末が同じ URL のまま結果画面へ更新された。
- [ ] マイスコア・みんなの回答・許可されたランキングのタブを確認した。
- [ ] 各進行操作で暗号化artifactが増え、Round IDが新しくなった。
- [ ] 次Roundの`PreparingAssets`では前問の公開結果を残し、受信・同期済みの`ReadyToSchedule`では開始待機、開始予約後はカウントダウン、回答開始後は回答画面へ切り替わった。
- [ ] 非表示タブでは通常の状態GETが止まり、復帰後は状態確認と時刻同期を待って回答操作が戻った。保存結果が不明な回答の照会・回復は継続した。
- [ ] 問題配信前の次問previewや未公開正答が参加者画面へ表示されなかった。
- [ ] 6問目のbackup検証後にEventが完了し、新Roundが作成されなかった。
- [ ] secret、Cookie、実データをスクリーンショットやログへ残していない。

記録する場合は、実施日時、端末 OS、browser 名／version、成功・失敗項目だけを残す。password、token、Cookie、CSRF token、回答本文、`.env` の内容は記録しない。

## 9. 再実施と終了処理

demo seed は、すでに進行した Round を初期状態へ戻さない。結果公開済みのデモを最初から再実施する場合は、対象がこの repository 専用の破棄可能な local Compose DB であることを人間が再確認する。確認できない場合は volume を削除しない。

専用 DB の破棄が承認済みの場合だけ、現在の Compose project の volume を削除して再 bootstrap する。

```sh
docker compose down -v
./scripts/restart-local.sh --bootstrap
```

確認終了後は `.env` を loopback 限定へ戻す。

```dotenv
GATEWAY_BIND_ADDRESS=127.0.0.1
REALTIME_ALLOWED_ORIGINS=["http://localhost:8080"]
```

再起動後、gateway の公開先が `127.0.0.1` であることを確認する。

```sh
./scripts/restart-local.sh
docker compose port gateway 8080
```

デモを終了する場合は DB volume を保持して停止する。

```sh
docker compose down
```

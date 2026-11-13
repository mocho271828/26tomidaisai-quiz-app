# AWSリハーサル

新規開催は[単一設定JSON](../backend/src/quiz_app/quiz_configuration/quiz-settings.json)と現在の問題JSONを使う。本番・本番前リハーサル・本番前テストに使う専用環境では、予算・撤去時間の事前確認・再確認や作成前見積の承認を必須にしない。終了時は専用環境とデモデータを全削除する。従来CLIのDB schema適用は下記の人手commandを使い、承認済みの委員向け手動workflowは[専用手順](GITHUB_ACTIONS_AWS.md)に従う。

## 普段編集するもの

[問題集JSON](../content/event-questions.json) の `selected_question_ids` が初期の出題順。通常の構成は練習1問、本番5問、予備2問。選択したIDがすべて問題集にあることを配置前に確認する。予備も選択IDへ含めて開催に登録し、必要に応じて未使用問題から配信する。各問の `options[].content` で選択肢を、`correct_label` で正解のA〜Dを変更する。問題本文や回答者名も同じJSONにある。解説は正答の選択肢から生成するので二重編集しない。

実際に最初に配信した1問は練習問題として、その問だけの得点・順位を表示する。2問目以降の表示は「第1問」からとし、総合得点には練習後の結果だけを加算し、途中参加者も総合ランキングへ含める。練習扱いは問題IDではなく実際の配信順で決まるため、運営画面で最初の問題を変更すれば練習問題の内容も変わる。

問題集は一意の正整数IDを持つ3問以上、選択IDは問題集に存在する重複なしの3件以上、各問は重複なしの4択が必須。全問に確定した正答を設定してから検証する。`correct_label` が `null` のまま、または入力案内が残る状態では検証が失敗する。回答者名は空でない文字列、`answer_note` は省略可能な確認メモである。

練習1問・可変問数・任意終了と端末計測方式は、新規開催へ現在の設定 `quiz-2026-10-23-v1` とともに反映する。schema 3・採点policy `scoring-2026-10-15-v2`を維持する。既存開催の設定が異なる場合は起動時に拒否し、稼働中の問題・得点を自動変換しない。既存開催の手動Event初期化も問題・設定を保持するため、今回の差替えには新規開催を準備する。[新規DB初期化手順](#人が1回行う新規db初期化)に従って、担当者がDBを現行revision `20261015_0007`へ準備する。

## オフライン確認

`gs-app-tomidaisai` のrepository rootで実行する。別のrepositoryから実行すると `backend` と `validation` が見つからない。以下はリポジトリのルートから実行する。DB・AWSへ接続しない。

```sh
uv run --project backend --frozen python -m validation.rehearsal check \
  --content content/event-questions.json
```

`status=ready`、出題順、`content_sha256` が返る。正答本文は標準出力に出さない。内容や選択を変更したら再確認する。実運用では問題集を担当者の非公開directoryへコピーして編集してよい。確認したSHA-256と実行対象の問題集を組にして管理する。

問題集はfrontend bundleにもimageにも含めていない。**問題・誤答・出題候補の選定だけならソースコード編集やimage再buildは不要**。変更はDB準備前に行う。投入後にJSONだけを変更してもDBは変わらず、稼働中の出題や正答の上書き機能は提供しない。

開始待機秒・採点・回答分布と順位の表示は問題集とは別の[単一設定JSON](../backend/src/quiz_app/quiz_configuration/quiz-settings.json)で管理する。こちらはAPI imageへ含まれるため、新規開催のDB準備前に設定を確定し、同じ設定を含むreleaseをbuildする。設定の意味と変更手順は[クイズ設定](QUIZ_CONFIGURATION.md)を参照する。

## AWSで使う構成

`APP_ENV=aws-demo`、`AWS_DEMO_ROUND_SCOPE=rehearsal-six-round` を使う。このscope名は配置識別子として維持するが、問題数は登録内容に従う。AWS用baseに [rehearsal override](../deploy/aws-demo/compose.rehearsal.yaml) を重ね、同一digestの `api` / `api-secondary` とgatewayを起動する。両APIはDB・Redis・session secret・backup鍵/保存先・実行UID/GIDを共有し、`API_INSTANCE_ID`だけを分ける。

- 同じReact/Vite、FastAPI、採点・締切・自動集計・順位・audience別projectionを使用する。
- PostgreSQLはCA検証付きTLS、Redisは認証付きTLS。ブラウザー/API/WebSocketは同じHTTPS OriginとSecure Cookieを使用する。
- 次問・終了は既存の暗号化backupの作成・復号・checksum・件数照合を通す。
- 公開入口と終了操作は [AWS CLIによる公開・終了](AWS_REHEARSAL_CLI.md) に従う。CloudFrontのURL取得、許可Origin設定、アプリ起動、公開の順で進める。
- gatewayは両APIへ分散し、live監視とGET限定retryを使う。Toxiproxy、600 VU、監視manifestなど正式負荷試験専用の構成は追加しない。このデモを600人性能や本番backup復元の合格証拠にはしない。

## 配置前に担当者が用意するもの

| 項目 | 用意する内容 |
| --- | --- |
| AWS利用条件 | account/profile、専用資源の台帳、撤去責任者と終了方法。予算・撤去時間の確認は不要 |
| image | 同じcommitから既存DockerfileでbuildしたAPI/gateway。40桁commitとregistryのdigestで固定 |
| release | [rehearsal release](../deploy/aws-demo/release.rehearsal.env.template) をrepository外へ作成。rollback fileは不要 |
| runtime | [非秘密template](../deploy/aws-demo/runtime.rehearsal.nonsecret.env.template) と [秘密値要件](AWS_DEMO_REQUIRED_SECRETS.md) からrepository外に通常file・`0600`で作成 |
| CA・backup | 承認済みCAとSHA-256。backupはrepository外の空directory・`0700`、実行accountのUID/GIDと一致。鍵は未公開のFernet鍵 |
| DB | 空の新規専用DBと管理接続情報。既存schema・app role・policy・運営account・選択問題は下記の人手初期化commandで作成 |
| 初期化秘密値 | 管理DB URL・app DB URL・運営名・運営passwordをrepository外の通常file・`0600`へ保存。所有者はreleaseの実行account |
| 開催入力 | [event template](../deploy/aws-demo/event.rehearsal.json.template) に新しいUUIDv4・開催名・slugを設定。採点versionは単一設定JSONの `scoring.version` と一致させる（現行値 `scoring-2026-10-15-v2`）。表示versionは `demo-display-name-v1`。UUIDをreleaseにも設定 |

DB schema適用を含む初期化commandは人が明示的に実行する。ホスト準備やアプリ起動からは実行しない。既存migrationは変更せず `alembic upgrade head` で適用する。DB準備の順序は **人手初期化（schema・role・policy・account・開催・選択問題）→ ID pool・sequenceと権限の人手準備 → preflight・両API起動 → ログイン**。開催・回答・参加者・session等が残っているDBは拒否するため、先に運営ログインしない。

現行の単一設定JSONは、開始待機3秒、採点version `scoring-2026-10-15-v2`、正解400点＋速度最大100点、0.1秒ごとに速度点が1点減る設定である。正式時間は`client_monotonic_v1`、到着猶予4,000ms、端末先行許容2,500ms、上限側加算許容10,000ms（設定上の時間差上限14,000ms）とし、通常回答時間10秒、誤答0点は維持する。新規DBには人手初期化commandでJSONと同じversion・係数・計測設定を登録する。同じversionで係数の異なるpolicyがある既存DBは自動更新しない。runtimeの `DEMO_OPERATOR_USERNAME=managed-in-database` / `DEMO_OPERATOR_PASSWORD=unused` はログイン情報ではなく固定の非秘密値である。

## 人が1回行う新規DB初期化

hostにreleaseと同じcommitをcheckoutし、backend依存をlockfileから用意する。CLIはHEAD一致と準備コード・依存定義に未commit変更がないことも確認する。非公開問題集は外部fileを使える。releaseに記録したUID/GIDの配置accountで、次の入力を確認する。

初期化用JSONの形式は以下。これは構造例であり、置換前の値では実行できない。実値はshell履歴、SSM command本文、標準出力へ貼らず、担当者が確認できる外部の `0600` fileとして配置する。

```json
{
  "admin_database_url": "postgresql+psycopg://<管理role>:<URLエンコードしたpassword>@<RDS hostname>:5432/<DB名>?sslmode=verify-full",
  "app_database_url": "postgresql+psycopg://rehearsal_app:<URLエンコードしたpassword>@<同じRDS hostname>:5432/<同じDB名>?sslmode=verify-full",
  "operator_username": "rehearsal-operator",
  "operator_password": "<新しく生成した32〜128文字の強いASCII password>"
}
```

`app_database_url` はruntimeの `DATABASE_URL` と完全一致させる。管理roleはapp roleと分ける。両URLは同一host・port・DBを指定し、TLSの `verify-full` を維持する。CLIはreleaseのhost CAとchecksum、DNS解決先、秘密fileの所有者・権限、開催UUID、問題集SHA-256を検証する。

人が次のcommandを1回実行する。`--confirm-current-policies` は正式採点と表示policyの採用を明示する引数である。

```sh
cd /opt/gs-app-tomidaisai && \
uv run --project backend --frozen --no-dev python -m validation.rehearsal_initialize \
  --repository-root /opt/gs-app-tomidaisai \
  --release-file /etc/gs-app-tomidaisai/aws-demo-rehearsal-release.env \
  --secrets-file /etc/gs-app-tomidaisai/rehearsal-initialization-secrets.json \
  --event-file /etc/gs-app-tomidaisai/rehearsal-event.json \
  --content /etc/gs-app-tomidaisai/event-questions.json \
  --confirm-event-id '<event-fileとreleaseに記録したUUID>' \
  --expected-content-sha256 '<checkで確認したSHA-256>' \
  --confirm-current-policies
```

空の新規専用DBであることを確認し、[schema revision](../backend/src/quiz_app/schema_revision.py) が示すmigration（現行 `20261015_0007`）、DDL権限を持たない `rehearsal_app`、単一設定JSONの `scoring.version` と係数・計測設定、表示policy `demo-display-name-v1`、Argon2idでhash化した運営accountを作成する。続いて選択問題の登録処理を呼ぶ。選択数をNとすると、正常終了時はEvent 1件、開催設定1件、Question N件、選択肢4N件、初期Round 1件が登録される。開催設定にはJSONの内容と初期表示状態を保存する。先にAlembicを単独実行すると既存table検出で停止するため、このcommandへ全revisionの適用を任せる。ID pool・sequenceの準備とアプリ起動・公開はこのcommandに含まれない。

schema適用とデータ登録は別transactionである。既存tableまたはapp roleがある場合は初期化を拒否する。途中失敗や接続解放失敗のときは、担当者がDBのschema・role・policy・開催UUID・件数を確認してから対応を決める。成功後の再実行、失敗直後の再試行、DB reset、既存recordの上書きは行わない。実行結果は配置記録へ残す。

## 準備済みDBへの選択問題登録だけを行う場合

現行schema・承認済みpolicy・現行Argon2idの専用運営accountを担当者が別途準備した空の開催DBには、`prepare` を単独で使える。開催入力の採点versionとDB上の全係数は、releaseに含まれる単一設定JSONと完全一致させる。旧採点policyを指定して進行する互換経路はない。上記の初期化が成功したDBには実行しない。

```sh
cd /opt/gs-app-tomidaisai && \
uv run --project backend --frozen --no-dev python -m validation.rehearsal prepare \
  --repository-root /opt/gs-app-tomidaisai \
  --release-file /etc/gs-app-tomidaisai/aws-demo-rehearsal-release.env \
  --event-file /etc/gs-app-tomidaisai/rehearsal-event.json \
  --content /etc/gs-app-tomidaisai/event-questions.json \
  --confirm-event-id '<event-fileとreleaseに記録したUUID>' \
  --expected-content-sha256 '<checkで確認したSHA-256>'
```

全条件が一致したときだけ、1transactionでEvent 1件、開催設定1件、選択IDに対応するQuestionと各4選択肢、初期Round 1件をINSERTする。releaseのCAをhost接続にも明示し、有限timeoutと排他lockを使う。既存recordの更新・削除、schema修復、自動migration、account/policy作成は行わない。

成功後の再実行は拒否する。通信失敗や `rehearsal_database_cleanup_failed_after_commit` など結果が不明な場合は、まず担当者がUUID・件数をSELECTで確認する。後者はtransaction成功後の接続解放失敗を表すため、投入し直さない。失敗をDB resetで解消しない。

## 開催登録後の参加者ID準備

上記initializerと`prepare`はいずれもID poolを作らない。新しいEventの登録後、[参加者IDの人手準備手順](handover/README.md#参加者idの準備)に従い、同じ開催UUIDで既存のSQL生成器を一度実行し、出力を確認した担当者が対象DBへ適用する。生成器自体はDBへ接続しない。既に適用済みのmigrationを繰り返さず、poolやsequenceの再作成・巻戻しも行わない。

`rehearsal_app`には`participant_id_pools`と`participant_id_slots`の`SELECT`、新しい開催専用sequenceの`USAGE`を付与する。initializerのsequence権限付与は作成時点のsequenceだけが対象なので、後から作る開催専用sequenceには別途付与が必要である。poolへの書込み権限とsequenceの`UPDATE`/`setval`権限、DDL権限は与えない。固定slotの完全性・採番余地・API roleの権限を確認してから、read-only preflightへ進む。この人手経路に、委員向けworkflow専用の自動準備を流用しない。

## 初回配置

現行の2 API構成に対応した同一commitのAPI/gateway imageを使う。gateway imageには`/etc/caddy/Caddyfile.multi-api`が必要であり、同fileを含まない旧imageへこのComposeを組み合わせない。`rehearsal-six-round` はcandidateだけでpreflightを実行でき、初回rollback baselineは不要。既存のsingle/load scopeの条件は変更しない。

選択問題登録とID pool・sequence権限の準備後、次のread-only preflightを行う。

```sh
cd /opt/gs-app-tomidaisai && \
./scripts/aws-demo-preflight.sh \
  --release-file /etc/gs-app-tomidaisai/aws-demo-rehearsal-release.env
```

source/image label、runtime、CA、backup、同一Event、登録問題3件以上、1〜登録問題数のRound、運営account、ID pool・sequenceとAPI roleの権限、DB/Redisの準備状態を確認する。preflightはimage取得、API/gateway起動、公開切替、DB変更を行わない。

配置条件確定・preflight成功後に、人が承認済みhost/contextで実行する起動例:

```sh
rehearsal_compose() {
  env -i HOME="${HOME:?}" PATH="${PATH:?}" \
    docker --context '<releaseのDocker context>' compose \
      --env-file /etc/gs-app-tomidaisai/aws-demo-rehearsal-release.env \
      --file deploy/aws-demo/compose.yaml \
      --file deploy/aws-demo/compose.rehearsal.yaml \
      --project-directory deploy/aws-demo \
      --project-name gs-app-tomidaisai-aws-demo "$@"
}
rehearsal_compose up --detach --wait --wait-timeout 90 --no-build --pull never \
  --remove-orphans api api-secondary gateway || exit 1
for api_service in api api-secondary; do
  for probe in live ready; do
    rehearsal_compose exec -T gateway curl --fail --silent --show-error --max-time 5 \
      "http://${api_service}:8000/api/health/${probe}" || exit 1
  done
done
```

両APIのlive応答の`version`がreleaseのsource commitと一致し、ready応答が`status=ready, database=ok, migration=current, redis=ok`であることを確認する。HTTP 200でも`degraded`なら公開しない。gatewayを経由した一回の確認だけで、両APIの準備完了と判断しない。

この起動例はloopback確認用。CloudFrontへの接続には [公開用overrideを加えた起動手順](AWS_REHEARSAL_CLI.md) を使う。root `.env` やambient `AWS_DEMO_*` / `COMPOSE_*` を継承しない。外部HTTPS入口の疎通を確認してから利用者を案内する。失敗時は公開を止めて終了・撤去へ進み、自動復旧や切戻しは行わない。次問・開催完了に必要な暗号化backupの整合性確認は維持する。

## デモ配置の切替と復帰

手動の `single-round` デモを同じCompose projectで切り替える場合、対象host・Event・schema revisionと、配置先にあるAPI・gatewayのimage digestを確認する。稼働中のRoundでは切り替えない。candidateと直前の成功版のpreflightを通し、戻す場合は `scripts/aws-demo-preflight.sh --rollback-operation` に成功版をrelease、失敗版をrollbackとして渡す。DBの復元やmigrationが必要な変更には使えない。

切替・復帰とも、`single-round` は `deploy/aws-demo/compose.yaml` 単体、`load-test-ten-round` は同ファイルの後に `deploy/aws-demo/compose.load-test.yaml` を重ねる。対象releaseのenvを指定して `up -d --no-build --pull never --remove-orphans --wait` で起動する。単一APIへ戻したら、`api` と `gateway` が起動し、`api-secondary`が不在であることを確認する。named volumeやhost backup directoryは削除しない。両経路ともliveの版とDB・Redisのreadinessを照合する。

## デモ当日の通し確認

1. 運営は `/operator`、参加者は `/` を同じHTTPS Originで開く。参加者IDはサーバーが開催専用の固定リストとsequenceから割り当てる半角英字大文字3文字。ID入力と候補取得は行わない。
2. 運営の登録問題・全選択肢・正答を確認する。準備開始前なら登録済みの問題の中で問題を切り替えられる。
3. 参加者を待機させ、問題を確定して配信した後、各端末の問題受信・時刻同期を確認する。予約前のマージンを取り、単一設定JSONの `start_delay_seconds` 後の開始を予約する（現行3秒）。運営と参加者の開始案内が同じ秒数であることを確認する。
4. 正答・誤答をそれぞれ送信し、受付済み表示、締切後の自動集計、正解・回答分布・許可された順位が参加者・運営の2画面で一致することを確認する。本人順位とTOP3は参加者向けの公開設定に従う。
5. 公開前の正答や次問previewが参加者画面へ漏れないことを確認する。次問確定時のbackup成功を確認し、同じ流れで練習1問から本番へ進み、総合0点・「第1問」表示と本番からの加点を確認する。別runでタブを非表示から戻し、最新状態と時刻同期を確認するまで回答操作が無効であること、保存結果が不明な回答の照会・回復が継続することも確認する。
6. 任意の問の結果確認後に、未使用問題を残した状態でも開催を完了できることと、本番5問後に次問へ続行できることを確認し、終了画面を確認する。失敗時は失敗codeと発生操作を記録し、未確認のまま次問へ進めない。

開始案内時刻・終了後アンケート・ホームURLを変更する場合だけ、既存の `VITE_EVENT_START_AT` / `VITE_SURVEY_URL` / `VITE_HOME_URL` を設定してgatewayをbuildする。未確定リンクは設定しない。

実AWS通し、実PostgreSQLへの人手投入、TLS入口、backupの実保存・復号、スマートフォン表示は配置時の確認事項。単体・回帰テストの成功と分けて実施結果を残す。

## 終了

開催完了はアプリ内の操作であり、AWS環境の削除ではない。通し確認の終了後、失敗・中断時、利用期限到達時は [終了・全削除手順](AWS_REHEARSAL_CLI.md#終了と全削除) を実行する。共有資源は対象から除外し、専用のDB、暗号化backup、secret、image、ログ、ネットワークを含めて残存を確認する。

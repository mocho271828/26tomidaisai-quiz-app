# 100独立ブラウザと実端末のローカル再試験

これはP0回帰確認用の6問試験であり、正式600VU/10問のrunnerではない。

50〜650VUを同一PCでコマンド操作する手動運営モードは[専用手順](LOCAL_BROWSER_CONTROL.md)を使う。現在の参加者IDはサーバー割当であり、最新revisionと開催別poolの人手準備が必要。

## 準備と観測

1. [TLS起動手順](LOCAL_TLS_RETEST.md)に従い、schema適用済みの専用DBと架空6問を準備する。各runは未使用の専用開催を使い、前runのDB・証跡を保持する。
2. サーバーと負荷PCを分ける。同居試験ならその事実、ホスト/VMのCPU数・メモリ割当・swap、Docker/image ID、source commitと未commit差分のhashを記録する。FD上限、DB pool、CA fingerprint、通常CA bundle条件を固定する。
3. 公開はgatewayの8443だけ。負荷PCの`baseUrl`は証明書SANに入れたサーバーのprivate IPv4。`Origin`はそのHTTPS originと完全一致させる。運営はサーバーPCのloopback URLで操作し、別PCへ運営資格情報を送らない。
4. 負荷PC・実端末の所有者が試験CAを信頼する。ブラウザ警告が出るなら開始せず、接続名/SAN/有効期間/CAを直す。証明書無視optionを加えない。
5. 負荷前からサーバー観測を開始する。FD使用/上限・loop・DB pool取得時間/thread待ち・DB lock集約・TLS・route別HTTP/WSをrun別に保存する。

```sh
mkdir -p .local/retest-evidence
./scripts/observe-local-tls.sh .local/tls/runtime.env .local/retest-evidence/server-normal-005-a 360
```

収集時間を各runより長くし、接続前30秒・終了後30秒以上も記録する。CPU/FDが戻らない場合は次の段階へ進まない。DB観測は既存権限のSELECTだけで、権限不足はunavailableとして残す。観測起因の負荷も記録する。

## runnerのインストールと入力

```sh
cd load-test/browser
pnpm install --frozen-lockfile
pnpm exec playwright install chromium
pnpm lint
pnpm typecheck
pnpm test
pnpm build
```

Playwright 1.63.0をlockfileで固定する。各参加者は`browser.newContext()`と1ページを持ち、Cookie/localStorage/sessionStorage/登録/WSが分離する。100タブやHTTPのみのVUへ置き換えない。[Playwrightの独立Context](https://playwright.dev/docs/browser-contexts)に沿う。既存Vitest/jsdomとk6ではReact＋実ブラウザを検証できないため、新しい試験専用packageに限定して依存を追加した。

`example.json`をGit外へコピーし、次を実環境に合わせる。出力先の親directoryだけを先に作成する。run directoryはrunnerが非上書き・0700で作る。

- `sourceCommit`: 試験対象の40桁commit。image ID、build時のcommit、dirty diff hashも試験台帳で照合する。入力だけで正しい版と証明されたとは扱わない。
- `runId`: runごとに新しい値。同じrunの欠損を再実行で埋めない。
- `count`: 5→20→50→100。`ordinalStart`は1からで、証跡の参加者序数に使う。参加者IDは参加操作時にサーバーが割り当て、runnerから指定せず、ID値を証跡へ出さない。
- `startAt`: 全PC共通の未来の日時（UTC offset付き）。100contextの生成時間も考え2分以上余裕を取る。PC時刻の同期状態・差を台帳へ記録する。
- `rampStages` / `rampHoldMs`: 同じrunで接続を段階的に増やす場合は `[5,20,50,100]` / `5000`。各段階の全登録・実WS OPENを確認してから次へ進む。別runの5/20/50人全6問試験を代行したとは扱わない。
- `joinSpreadMs`: rampStages未指定の段階試験は10000など。一斉接続runは0。
- `operatorStartDelaySeconds`: 共通開始日時から運営の初回配信までの時間。例45秒。未準備を検知したら運営が止め、遅い生成機をサーバー障害と混同しない。
- `preScheduleMarginMs`: 配信から予約前の待機。最初の比較条件は5000ms。これは仮の試験条件であり最適な値・最大許容待機を決めたものではない。
- `resultHoldMs`: 各問の結果表示を維持する時間。例5000ms。実機併用時は手動運営を使い結果確認を待つ。
- `answerDelayMs`: 回答可能DOMの確認から選択まで。例500ms。一斉回答runは0として予定/実際の操作時刻を照合する。
- `scenario`: `normal` / `reconnect` / `clock-delay`。混ぜず別runへ分離する。
- `durationSeconds`: 有限期限。通常300秒、実機併用は余裕を持たせる。タイムアウト/欠落は失敗として残す。

```sh
node dist/src/main.js participants /absolute/path/to/run.json
```

runnerは自動生成IDによる参加ボタンを操作し、各問の実Reactで問題番号・回答ボタン・受付済み・本人結果を確認する。回答POSTの直接代行はしない。6問完了まで全contextを保持し、Secure/HttpOnly Cookieの非重複をメモリ内だけで照合する。run中の同時WS/登録済み数とserial別の受付/結果問数を保存する。失敗時の例外本文、HTML、Cookie、参加者ID、問題/回答本文は保存しない。

全sessionは`events.jsonl`にallowlistの軽量イベントを記録する。代表の先頭serialとHTTP5xxはDNS/TCP/TLS/TTFBの詳細timingも残す。UIは`quiz:observation`の型付き数値・固定状態だけを保存し、ブラウザtrace/HAR/動画/スクリーンショットを無条件収集しない。計測v2の資源採取は既定3秒周期で、ホスト全体CPU/使用メモリ/swapとNode RSS/CPU/loopを区別する。イベントは有界非同期batchで保存し、終了時の `evidence-health.json` も確認する。ホスト全体の値なので同居アプリも含む。

```sh
node dist/src/analyze.js /absolute/path/to/run-participants-1/events.jsonl > /absolute/path/to/new-analysis.json
```

所要時間は各hostのmonotonic値で計算する。PC間wall clock差をAPI遅延へ足さない。100人を複数PCへ分けた場合、各summaryのpeakを足して同時100と断定しない。共通日時の接続時系列とサーバーのWS activeを重ね、全員の稼働区間が重なった実績を確認する。

## 計測v2

runnerはContext生成までをsetup、登録開始から全問観測終了をmeasurement、その後の取消・回収をshutdownとして記録する。`participant_measurement_started` / `participant_run_finished`（運営は`operator_`）が境界になる。`analyze`の全指標はこの窓に合わせる。旧証跡も読めるが、準備と測定を分離できない場合はlegacyと表示する。終了後に遅れて届く観測はshutdownへ分離し、欠落を推測で埋めない。

HTTP中断はページ内sequenceとrouteを対応させ、一意な終了観測に固定取消理由がある場合だけ分類する。正常な差替え・非表示・unmount等と、deadline・transport・不明を分ける。認証失効は正常取消へ免責しない。`summary.status`とprocess終了codeは旧strict条件を保持し、分類後の`measurementStatus`とは別に残す。分類後の通信エラー0件だけで試験全体を合格にしない。

受付表示・本人結果の観測は次問回答と独立して待つ。前問結果が欠けても次問を試し、欠落はその問の失敗として保持する。中止時は全actorと障害注入を回収し、未試行の後続問を連鎖的な`actor_failed`にしない。`join_stage_started/finished`でnavigation・join操作・応答・Secure Cookieの段階を確認する。

任意の`coordination`には`directory`と全参加cohortの`ordinalStart/count`を指定する。同じ共有directoryを使える場合、run・source・開始日時・cohortを照合したheartbeatで運営開始と中止を連携する。異なるPCの時計を差し引かず、heartbeat進行を観測側の単調時計で監視する。共有filesystemを用意しない場合は`coordination_unconfigured`が記録されるため、監督側で全参加者の接続と生成機停止を確認して運営を制御する。

匿名DB照合にはrunごとに32 byteの乱数鍵を作り、hex64文字の通常fileを所有者限定0600で保存して`identityKeyFile`へ絶対pathを指定する。同じrunの生成機とDB照合でだけ共有する。参加者の生ラベル・Cookie・DB UUIDは証跡へ保存せず、run別HMACを使う。鍵なしは対応不明として残り、DB照合成功にはならない。

2 APIのサーバー観測は次の引数で開始する。

```sh
scripts/observe-local-tls.sh .local/tls/runtime.env \
  .local/retest-evidence/server-normal-v2 420 gs-app-tomidaisai-tls dual
```

APIごとに常駐collectorを1回起動し、metricsは1秒目標、DBとcontainer資源は15秒目標で採る。`api/samples.jsonl`、`api-secondary/samples.jsonl`、`containers.jsonl`とmanifest/completionを保存する。DB・container採取の待ちをmetrics周期に加えず、実際の採取間隔・所要時間・欠測も残す。API再起動は採取の正常継続とみなさない。旧1標本1fileの観測形式は使わない。

サーバー集計は`quiz_app.observability.local_observation_analysis`へ観測directoryとブラウザ測定開始・終了のUTCを渡す。窓内の先頭標本をbaselineにし、setup分の累積counterを含めない。境界の未測定時間を表示する。2 APIのcounterとhistogram bucket差分を合算し、p95を再計算する。process別のp95平均やcounter resetのゼロ補完はしない。結果子workerの内部metricsは対象外である。

`local-server-analysis-v3`はprometheus-clientの遅延生成系列を区別する。同一processの連続有効標本で未見の系列に対し、name/全labelが一致する`_created`が前scrape開始〜現scrape完了かつ測定窓内にある場合だけ、生成時0から初値を計上する。生成metadata変更は品質不合格にする。先頭標本の既存系列はbaselineを維持し、欠測・不正・restart/reset・系列消失/再出現・生成根拠なしは補完しない。これにより正常な初出を欠測と誤認せず、回答POSTの初回bucketを捨てない。

```sh
uv run --project backend python -m quiz_app.observability.local_observation_analysis OBSERVATION_DIR \
  --start-at MEASUREMENT_START_UTC --end-at MEASUREMENT_END_UTC > NEW_SERVER_SUMMARY.json
```

`measurement_usable`は指定窓のうち実際に標本で挟めた区間の品質を表し、アプリ合格や全要求の完全計数ではない。収集processの正常終了は`collector_lifecycle_complete`で別に示す。PC間の時刻差も別途確認する。

全問終了または中止後のDB照合は、TLS設定済みAPI環境で`browser_membership snapshot`を実行し、生成した匿名snapshotをホストの`analyze`へ渡す。鍵と出力directoryだけを専用mountし、運営資格情報を生成機へ渡さない。次は同moduleの引数例であり、DB接続環境を自動作成しない。

```sh
python -m quiz_app.observability.browser_membership snapshot \
  --run-id RUN_ID --source-commit SOURCE_COMMIT \
  --identity-key-file PRIVATE_KEY_FILE --output NEW_SNAPSHOT.json
uv run --project backend python -m quiz_app.observability.browser_membership analyze \
  --snapshot NEW_SNAPSHOT.json --conditions PARTICIPANT_RUN/conditions.json \
  --events PARTICIPANT_RUN/events.jsonl --output NEW_MEMBERSHIP_REPORT.json
```

複数cohortなら`--conditions`と`--events`に同じ順序で各fileを列挙する。全予定100人・600組を固定し、未接続者、DB対応不明、保存・受付・結果・クリック・送信の不足/余剰/重複を報告する。DB照合はsnapshot取得時の保存存在であり、測定窓内のcommit完了時刻を保証しない。入力fileのSHA-256を結果に含め、既存snapshot/reportは上書きしない。VOID履歴や複数開催で番号対応を推定しない。

## 運営と全6問

手動運営が基本。loopback `/operator`へログインし、問題確認→「問題を確定して配信」→予約前マージン→「3秒後に開始予約」を順に操作する。readiness数や98%ゲートはない。開始予約は3秒後、選択は10秒未満、DB到着終端は開始14秒とする。`ReadyToReveal`で運営だけが統計を確認し、参加者の発表待ちを確認後に「正解・結果を公開」を押す。`Reveal`と結果表示を確認して次問へ進む。先頭1問は練習として本人結果を残し、総合0点を確認する。第6問も結果を記録してから人がEventを完了する。

少数/機械だけの試験にはサーバーPCでUI自動運営も利用できる。資格情報JSONは`{"username":"…","password":"…"}`をGit外0600へ人が作る。引数やログに値を書かない。operator用configの`baseUrl`はloopback、`runId/startAt/条件`を参加者側と揃える。

```sh
node dist/src/main.js operator /absolute/path/to/operator-run.json /absolute/path/to/operator-credentials.json
```

最終Event完了は自動化しない。旧HTTP acceptanceは1問の補助チェックで、100ブラウザの代わりにはならない。

## 複数PCと実端末

負荷PC Aは`count:50, ordinalStart:1`、Bは`count:50, ordinalStart:51`とし、同じ`runId/startAt/sourceCommit`で起動する。各PCで独立した出力directoryを使う。証跡のserial範囲を重複させず、別runは新しい専用開催を用意する。参加者IDの重複は登録時のDB一意制約で拒否され、画面が別IDで再試行する。CPU飽和・swap増加・操作遅延がある場合は分割し、条件変更を別runに記録する。

100機械＋約5実端末は機械100の成功後に実施する。実端末はユーザーが操作し、次の表を記録する。ID・画面内容・Cookieを記録しない。

| 端末序数 | OS/browser/version | CA信頼確認 | 接続/登録 | 各問受信/待機 | 各問選択/受付 | 各問結果 | 復帰時間/異常分類 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| D1〜D5 | iPhone Safari / Android Chrome等 | 日時 | 時刻 | 1〜6問 | 1〜6問 | 1〜6問 | monotonic計測可なら所要時間 |

1. 所有者がCA fingerprintをサーバー記録と照合し信頼設定する。接続先はSANと一致するHTTPS URL。Wi-Fi/SSID、電波状況、低電力設定を記録する。
2. 画面が自動生成した3文字IDで参加する。IDを手入力せず、記録表へ転記しない。
3. 運営が配信後、端末に開始待機が出たか確認する。未受信・時刻未同期の表示を完了とみなさない。
4. 運営の予約後、各自回答を1回選び受付表示を確認する。結果、練習総合除外、次問待機を6問分確認する。
5. 別runで1台をバックグラウンドへ3秒移動して復帰、1台はWi-Fiを2秒切断して復帰、1台は途中登録とする。非表示中に通常の状態GETが止まり、復帰直後に最新状態と時刻同期を確認するまで回答操作が無効であることを確認する。保存結果が不明な回答の照会・回復は非表示中も継続する。通常runへ意図した欠損を混ぜない。
6. 問題本文や個人情報が写る画面保存を共有証跡に使わない。必要な異常調査は序数・問番号・時刻・固定分類を使う。終了後は所有者が試験CAを解除する。

実端末未準備を5つの自動contextで代替して完了扱いにしない。運営画面は参加者数とは別の接続数として記録する。

## run分割と判定

| run | 操作/条件 | 調べる点 |
| --- | --- | --- |
| 少数→段階増加 | normal、5→20→50→100、6問 | 全sessionが実受信・受付・結果へ到達。生成機負荷も確認 |
| 一斉接続 | joinSpreadMs=0 | TLS/FD/loop/登録集中。開始前に全員の稼働を確認 |
| 接続済み配信 | 登録済み100、手動配信 | 受信→DOM→同期→開始。未受信を完了表示しない |
| 一斉回答 | answerDelayMs=0 | 予定と実際の操作差、DB受付/lock待ち、欠落 |
| 再接続 | reconnect、faultAfterSeconds/faultDurationMs固定 | 全contextをoffline→online。WS再確立時間とGET増幅 |
| 一時遅延 | clock-delay | 初回時刻応答3件だけ600ms遅延後に回復。30秒待ちを再発しない |
| 実機併用 | 機械100＋約5端末、手動運営 | 6問全工程とbackground復帰、端末差 |

最初の暫定評価目安は通常runで予定600回答/600結果を欠落なく確認、予期しない5xx/timeout/画面停止0、state GET同時1件、clock遅延解除から5秒以内の再同期、終了後30秒のFD平常値復帰。HTTP/state p95 1秒超や生成機CPU継続90%/swap増加は原因調査の目安とする。正式版SLOや容量上限を追加決定したものではなく、未達理由を保存して修正前後を同条件で再測定する。

Redis比較だけでブラウザ/DB/無線/AWS経路の改善を断定しない。単一APIで残る問題を特定してから、必要に応じ同じ改善版・同じ負荷で2APIを別runとして比較する。その際DB接続予算、process別metrics、自動結果公開の重複とWS回収も確認する。

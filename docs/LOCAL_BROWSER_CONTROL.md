# 手動運営に追従する50〜650 VUブラウザrunner

対象は `load-test/browser` のブラウザ操作である。参加はPlaywrightクリック、手動runnerの回答はページ内の `HTMLButtonElement.click()` を使う。1 VUにつき独立BrowserContext・ページ・Cookie/storageを保持する。総VU数は起動時に50〜650の整数で指定する。既定100人ずつ、650人は100×6 worker＋50へ分割する。同一PCのNode IPCで起動・監督・停止し、自動operatorは起動しない。

回答方式は利用者の指定により送信負荷の生成を優先した。DOMクリックは通常のReactハンドラーとHTTP送信を通るが、マウス操作・クリック位置判定・Playwrightのactionability待機を再現せず、`isTrusted` はfalseになる。回答の可視性・disabled/inert・指定問・同期済み締切はrunnerが検証する。従来方式と比較する場合はこの試験条件の差を記録する。

**650は設定上限であり、この版で650人の実走に成功したという意味ではない。** プロセスを分けてもPC全体のCPU・メモリ容量は増えない。旧 `load-test/k6` の正式600VU/10問契約は別系統であり、本runnerのブラウザ完了判定に転用しない。従来の `participants` / `operator` 自動6問回帰モードは維持する。

手動Runnerの予定問数は起動前の `EXPECTED_ROUNDS` で指定する。local手動の既定は6問で、1〜4095の整数を受け付ける。この上限は既存の4096操作/runから参加1操作を除いたRunner側の制約であり、アプリの問題数上限ではない。AWSの既定7問は[AWS専用手順](AWS_BROWSER_LOAD_TEST.md)を参照する。

## 初回準備

1. 同じ版のAPI・frontendを使う。接続先は既存のlocal HTTPS origin（loopbackまたはprivate IPv4）の参加者画面 `/`、手動運営は `/operator`。既存の[ブラウザ再試験](LOCAL_BROWSER_RETEST.md)・[TLS手順](LOCAL_TLS_RETEST.md)に従い、HTTPS/WSS、Secure Cookie、Origin、PostgreSQL verify-full、Redis TLSを準備する。証明書検証を無効にする設定はない。CAのOS信頼は所有者が管理する。
2. 試験専用DBと未使用の専用開催を人が準備する。最新の[参加者ID割当手順](handover/README.md#参加者idの準備)に従い、[現行schema revision](../backend/src/quiz_app/schema_revision.py)、開催別pool/slot/sequence、API roleの権限を確認する。revisionだけ一致してもpool未準備ならjoinは503になる。
3. 同じHEADのfrontendを既存手順でbuildする。手動runnerは画面の内部観測から同期済み回答期限を取得する。古いfrontendでこの情報がない場合、手動回答を拒否する。これは同期推定に基づくクライアント側guardであり、DB受理の保証はサーバーの期限検証と別である。アプリのHTTP/WS API、受付期限、採点は変えていない。
4. Node、既存pnpm、Playwright Chromiumを準備する。OSにChromeが導入済みなら `channel: "chrome"` も使える。

```sh
pnpm --dir load-test/browser install --frozen-lockfile
pnpm --dir load-test/browser exec playwright install chromium
pnpm --dir load-test/browser lint
pnpm --dir load-test/browser typecheck
pnpm --dir load-test/browser test
pnpm --dir load-test/browser build
```

runnerのbuildとアプリのbuildは別である。プロセスのFD上限・メモリ余裕・休眠設定も所有者が確認する。この手順の実装はOS設定や証明書信頼を変更しない。

## 毎回の入力と起動

### Makefileの作成・起動プリセット

repository rootで以下を実行する。依存とChromiumの準備はInternet接続中に一度行う。新規作成・起動時にもRunnerをbuildし直す。

```sh
make browser-runner-prepare

# 設定・鍵・予定VUの計画だけを作成する
make browser-runner-new VUS=100

# 新規作成したRunnerをそのまま起動する
make browser-runner VUS=100 DURATION_SECONDS=1200

# workerを50人ずつに分け、最小計測で300 VUを起動する
make browser-runner VUS=300 WORKER_SIZE=50 MEASUREMENT_LEVEL=minimal \
  BASE_URL=https://127.0.0.1:8443 MANUAL_PARTICIPANTS=1

# 固定LANの別PCで動くサーバーを使う場合
make browser-runner VUS=300 WORKER_SIZE=50 DURATION_SECONDS=1200 \
  MEASUREMENT_LEVEL=minimal BASE_URL=https://10.0.1.2:8443
```

毎回 `.local/browser-runs/<自動生成run名>/` を新設し、`identity.key`、`run.json`、`plan.json`を保存する。directoryは0700、fileは0600。作成のみの場合は、その設定とVU数を使う起動コマンドを表示する。実行時の正本manifestと証跡は、その配下の `<run名>-control/` に既存Runnerが生成する。中断・再試験には再度新規作成を使う。

| Make変数 | 既定値 | 指定範囲・用途 |
| --- | --- | --- |
| `VUS` | `50` | 総VU数、50〜650の整数 |
| `BASE_URL` | `https://127.0.0.1:8443` | localhost・loopback・private IPv4のHTTPS origin |
| `DURATION_SECONDS` | `900` | 準備完了後の操作時間、180〜3600秒 |
| `EXPECTED_ROUNDS` | `6` | 起動前に固定する予定問数、1〜4095の整数 |
| `WORKER_SIZE` | `100` | workerあたり1〜100人 |
| `MEASUREMENT_LEVEL` | `normal` | `normal` / `minimal` |
| `DETAILED_VU_COUNT` | `3` | normalで詳細診断する人数、0〜10 |
| `HOST_SAMPLE_INTERVAL_MS` | `3000` | 資源採取周期、2000〜30000ms |
| `SETUP_TIMEOUT_SECONDS` | `900` | 全workerの準備期限、10〜900秒 |
| `OPERATION_TIMEOUT_MS` | `30000` | 各操作の期限、100〜30000ms |
| `MANUAL_PARTICIPANTS` | `0` | 手動参加者の予定人数、0〜100 |
| `EXPECTED_OPERATOR_CONNECTIONS` | `1` | 運営の予定接続数、0〜10 |
| `CHANNEL` | `chromium` | `chromium` / 導入済みの`chrome` |
| `HEADLESS` | `true` | `true` / `false`（ブラウザ画面を表示） |

空値・小数・範囲外は作成前に拒否する。総VU数は実行中には変更できない。参加・回答の時間分散は起動後の `join --spread 30s`、`answer --round 1 --when-open --delay 500ms` で指定する。

sourceCommitにはRunner側のGit HEADを保存するため、未commit変更がある場合は作成前に停止する。アプリも同じcommitを配置し、上の初回準備に従ってHTTPS・CA信頼・専用開催・ID poolを確認しておく。この汎用presetは接続先アプリのcommitを自動照合しない。固定LAN構成のmetadata・CA照合には既存の `lan-client-*` を使う。prepare/newでアプリ・DB・CA信頼・負荷試験を起動または変更しない。

### JSONを直接指定する場合

`load-test/browser/control.example.json` をGit外のrun専用JSONへコピーして編集する。外側の `run` は既存runner設定を再利用し、手動専用の値は外側で分ける。

| 設定 | 用途 |
| --- | --- |
| `run.runId` | 未使用run名。再試験ごとに変更する |
| `run.sourceCommit` | 実際に配置したアプリの40桁commit。未commit版を既存commit実測と表現しない |
| `run.baseUrl` | 検証済み接続先。例 `https://127.0.0.1:8443` |
| `run.outputDirectory` | Git外の証跡親directory。下へ `RUN_ID-control` を排他作成する |
| `run.identityKeyFile` | run専用32 byte秘密鍵のhex64文字を保存した絶対path。DB匿名照合に必要 |
| `run.ordinalStart` | 機械VUの先頭序数。既定1。アプリが発行する生の参加者IDとは異なる |
| `run.durationSeconds` | 全worker準備完了後の手動操作期限。180〜3600秒、例900秒 |
| `expectedRounds` | 手動Runnerの予定問数、1〜4095の整数。省略時6問 |
| `workerSize` | 1〜100、既定100。全worker合計はCLIの `--vus` で確定する |
| `setupTimeoutSeconds` | 全workerの準備期限。既定900秒、10〜900秒 |
| `operationTimeoutMs` | navigation/join/回答予約の登録/受付観測などの有限待機。人の開始待ちはrun期限まで。既定30000ms |
| `measurementLevel` | `normal` または `minimal`。既定normal |
| `detailedVuCount` | normalで詳細診断する先頭VU数。全worker合計で既定3、0〜10 |
| `hostSampleIntervalMs` | host資源と各Node processの採取周期。既定3000ms、2000〜30000ms |
| `manualParticipantCount` | 自分が実端末で操作する参加者の予定人数。VU数には加えない |
| `expectedOperatorConnections` | 手動運営の予定接続数。VU数とは別、既定1 |

300VU時の生成機の遅延を考慮し、worker応答期限は60秒とする。その他の待機上限は下表に示す。新規設定の準備・操作の既定値は上表のとおり。既存のrun.jsonや明示したMake変数の値はそのまま使うため、再試験では新しいRunnerを作成する。

| 固定の待機上限 | 秒 |
| --- | --- |
| worker応答欠測 | 60（超過時に停止） |
| workerのChromium起動 / 手動VUの暗黙DOM待機 | 90 |
| 正常終了猶予 / 強制終了後の回収確認 | 45 / 9 |
| LAN client確認のHTTPS・Chromium・参加画面 / Git操作 | 30 |
| LAN server確認の接続 / 要求全体 | 9 / 30 |
| 回収用process一覧取得 / macOS swap取得 | 6 / 3 |

run実行時間・アプリの回答締切・報告や採取の周期・2.5秒の標本鮮度は維持する。待機延長だけでは、存在しないDOM要素や回答操作の失敗は解消しない。

`run.count` は既存schemaのため入力に残るが、手動起動では各workerの人数で置き換わる。`run.startAt` も起動時刻へ置き換わり、自動参加の起点には使わない。`run.joinSpreadMs` / `answerDelayMs` / operator用待機値も対話コマンドの操作には使わない。`scenario: "normal"`、空 `rampStages` とし、共有filesystemの `coordination` は指定しない。総数650のためにcohortごとの100上限を取り払う必要はない。

鍵は値をterminalへ表示せずに生成できる。`PRIVATE_RUN_DIRECTORY` を未使用の絶対pathへ設定し、親directoryの権限も所有者限定にする。次はファイル作成だけであり、DBへ接続しない。

```sh
mkdir -m 700 "$PRIVATE_RUN_DIRECTORY"
node --input-type=module -e 'import {randomBytes} from "node:crypto"; import {writeFileSync} from "node:fs"; writeFileSync(process.argv[1], randomBytes(32).toString("hex")+"\n", {mode:0o600,flag:"wx"});' "$PRIVATE_RUN_DIRECTORY/identity.key"
cp load-test/browser/control.example.json "$PRIVATE_RUN_DIRECTORY/run.json"
```

JSONのplaceholderを実値へ編集し、秘密鍵pathを設定する。例はそれぞれ別runで実行する。同じrun名・出力先の再利用は拒否する。

```sh
pnpm --dir load-test/browser control --vus 50 --config /absolute/private/run-050.json
pnpm --dir load-test/browser control --vus 100 --config /absolute/private/run-100.json
pnpm --dir load-test/browser control --vus 300 --config /absolute/private/run-300.json
pnpm --dir load-test/browser control --vus 650 --config /absolute/private/run-650.json
```

対話terminalを開いたまま操作する。stdinのEOFも停止として扱う。実行中の人数変更は提供しない。内部unit/smokeには1〜数人を使えるが、公開CLIは50未満を拒否する。

## 毎回の操作

1. 起動直後はページ・参加ボタン・フォントの準備だけを行う。「準備完了」を待つ。各workerは1画面ずつ、フォント読込の完了まで待って次の独立Contextを起動する。300 VU / worker 50なら最大6画面を並行準備する。自動参加はしない。
2. 自分の運営画面と、予定した人数だけの手動参加者画面を別ブラウザ/端末で開く。運営接続・手動参加者をrunnerのVU数へ合算しない。
3. runnerのterminalで参加を指示する。各workerをまたいだ全対象の序数で分散する。

```text
status
join --spread 30s
```

一斉参加は `join` または `join --spread 0ms`。同じVUへ別command IDでjoinを再発行しても二重参加しない。失敗した登録を別IDの再参加で隠さず、そのrunを記録する。

4. 登録数・WS数を `status` で確認する。運営画面で第1問を「問題を確定して配信」し、terminalでその問だけを予約する。

```text
answer --round 1 --when-open --delay 500ms
```

5. 運営画面で手動の開始予約を行う。各VUはブラウザ内で指定した第1問の回答可能状態を観測してから500ms後に、序数に応じた4択ボタンをDOMクリックする。Nodeへの表示通知の到着を待たない。結果は通常の運営操作・アプリの進行に従って表示する。
6. `status` の第1問クリック・受付・結果件数を確認する。運営画面で次問へ進め、配信→その問の回答予約→手動開始→結果確認を予定した最終問（既定第6問）まで繰り返す。過去問命令を次問へ流用しない。
7. 終了時に `stop`。全worker・browserを回収し、書込完了後にJSONとCSVを生成する。Ctrl+Cも同じ停止経路へ入る。

回答可能になっている問を直ちに押す操作と、時間分散する操作も利用できる。

```text
answer --round 2 --now
answer --round 3 --when-open --delay 500ms --spread 2s
pause
status
resume
stop
```

`--now` は未参加VUには即時に拒否し、参加済みVUでは予約がブラウザへ届いた時点で指定問が回答可能な場合だけクリックする。未開始問は `round_not_answerable` としてskipする。この理由で未クリックが確定した場合だけ、同じ問へ別命令を出せる。`--when-open` は指定問だけを予約し、開始前に入力しておく。`--spread` は先頭から末尾までの0〜120秒、`--delay` は0〜120秒。when-openはブラウザ内の回答可能状態の観測、nowは命令発行時刻を予定の起点とする。通常回答期限は10秒なので、期限に収まらない予約はクリックされず、skip/未回答として残る。4択の配分は `(ordinal - 1) % 4`。

`pause` は新規参加・回答の実行を保留し、WS・状態受信・結果観測を継続する。**アプリの回答期限は止まらない。** resume時に古い問や締切済の予約を実行せず、期限内の保留だけを再開する。pause前にアプリへ届いたクリック/送信を取り消す機能ではない。

人の配信待ち・開始待ちには実操作の30秒timeoutを適用せず、全体の有限実行期限を使う。クリック後の受付待ちなどは別の有限timeoutを使う。現在問、予約問、run、command、VU序数を検証し、同じ問の操作を別commandで再実行しない。

## statusと成果物の読み方

`status` はworkerごとに予定人数、最後の報告からの経過ms、準備数、登録数、現在のWS接続数、失敗人数、操作失敗/timeout/切断/復帰件数、問別のクリック・受付・結果件数を表示する。命令配布の表示は「命令を送った」という意味で、`commandReceipts` はVUでの命令受付、`clicked` は回答ボタンのDOMクリック、`accepted` は可視受付表示である。Nodeへの観測通知が遅れれば件数への反映も遅れる。DB保存は別の匿名照合で確認する。

手動参加者・運営接続は `planned` と `observed: null` で表示する。このrunnerから別ブラウザの実接続数は観測できない。手動参加者の登録数は終了後の明示allowlist付きDB照合で確認する。統計のための追加HTTP問い合わせは行わない。

起動前の `manifest.json` が予定人数・`expectedRounds`・worker範囲の正本である。届いたログだけから分母を作らず、予定問数を実績に合わせて縮めない。

| 出力 | 内容 |
| --- | --- |
| `report.json` | 固定分母の未登録・未接続・未回答・未完了、エラー、timeout、切断/復帰、問別件数、受付時間、記録完全性 |
| `rounds.csv` | 各問の予定・クリック・送信・受付・結果・回答済結果・未回答・未完了とlatency標本数/中央値/p95/最大 |
| `latencies.csv` | 全VUの同一ブラウザ単調時計で測った送信→可視受付DOMの標本 |
| `operations.csv` | command/VU/問、when-open予定・クリックのbrowser単調時計とずれ、参加/now予定・完了観測のNode時刻とずれ |
| `connections.csv` | 予定人数・準備・登録・WSの時系列、worker標本と欠測 |
| `resources.csv` | host全体のCPU/使用メモリ/swap、Node process別CPU/RSS/loop、採取遅れ、保存待ち |
| 各workerの `events.jsonl` / `evidence-health.json` | 非秘密の状態変化、連続sequence、書込件数/時間/待ち/最大buffer/欠落/保存失敗/終了状態 |
| `termination.json` | 親の終了理由、予定worker数、各workerのexit/clean状態 |

`results` は未回答の結果表示も含み、`answeredResults` と区別する。reportの `expectedRounds` が予定問数で、`measurementComplete` と `allRoundsBrowserComplete` は別である。手動の途中終了・意図した未回答を予定全問成功とせず、10問予定で7問終了も未完了とする。`databaseMembershipVerified` はこの集計だけではfalseのまま。終了コード0は要求に応じて記録を閉じたことを示し、全問成功を保証しない。

`make browser-runner`の終了表示には停止理由、準備/登録人数、失敗人数、timeout、計測記録と予定全問の完了状態が出る。`worker_failed`などの異常終了時は、各workerの`events.jsonl`にある`manual_operation_failed`の`stage`を確認する。準備段階は`navigation`、`join_button`、`fonts`に分かれる。

ページの`load`完了後もフォント取得は続くため、各workerは参加ボタンに加えて`document.fonts.status === "loaded"`を有限期限で待つ。HTTP protocolの強制変更、フォント取得の省略、アプリ側timeoutの引き延ばしは行わない。`lan-client-check`の単一画面成功は多画面準備の成功を保証しない。

接続数は親の1秒標本時点で最新のworker報告を集約する。worker報告は1秒周期、2.5秒超の古い報告は欠測とし、0や保持値で正常補完しない。完全同時のサーバー接続実数ではなく最大2.5秒の標本ずれを含む生成側推定である。別時刻のworker peakを加算しない。

受付時間はbrowser内の同じ `performance.now()` で計算し、全workerの生標本から中央値/p95/最大を再計算する。worker p95の平均は使わない。可視DOM観測でありpaint完了・サーバー内部時間ではない。`browserAnswerDriftMs` はwhen-openの同一browserの予定→DOMクリックの差だけを集計する。CSVの `planned_browser_monotonic_ms`、`clicked_browser_monotonic_ms`、`browser_lag_ms` が対応する。`operationDriftMs` と従来のNode時刻列は参加操作・now・旧証跡の予定→完了観測用で、Playwright返却待ちを含む。when-openもNode完了観測時刻は残すが、Node予定・ずれは空欄とする。nowは命令発行時刻をブラウザ時計へ換算して実行するため、ブラウザ予定・ずれの標本には含めない。異なるbrowser間やNode/browser間の未校正時計差をlatencyへ混ぜない。

## 記録の負荷と比較方法

全VUは必要な状態変化・送信・受付・結果・エラーを記録する。通常計測でもResourceTiming、MutationObserver、longtask、詳細HTTP診断は代表VUだけ。HAR・動画・全VU screenshotは常時採取しない。生の参加者ID、Cookie、認証情報、問題・回答本文は保存しない。

ホスト全体のCPU/メモリ/swapは親だけで既定3秒ごとに取得する。macOSのswap取得の子process起動をworker数倍にしない。2〜5秒を初期候補とし、既定3秒は短い変動をある程度追いながらOS呼出しを抑える暫定値である。`hostSampleIntervalMs` で変更し、比較時は同じ値に固定する。メモリ値はtotal−freeで、macOSの再利用可能cacheを含む。Node RSSはChromium込みのworkerメモリではない。資源の要約はsetupからshutdownまでのprocess生存期間を含み、CSVの時刻で準備/手動運用区間を分けて比較する。終了回収によるWS closeは `shutdownDisconnects` として通常の切断件数と分ける。

各processの保存queueは既定4MiBかつ10000件、64KiB目安で分割し、250ms周期で非同期flushを起動する。単一イベントはbatch目安を超える場合があり、I/OやNodeの停滞時に250ms以内の保存完了を保証しない。書込中のbatchも上限に含める。上限超過・保存失敗・sequence欠落・health未封印を隠さず、計測完全性をfalseにする。`writeMs` は非同期I/Oの経過時間でありCPU占有時間と同一ではない。serialization時間、queue待ち、event loop遅延、資源採取時間、採取欠測も併せて評価する。

最小計測との比較は、`measurementLevel: "minimal"` にして詳細ブラウザprobeと高頻度Node histogramを止める。必須状態、同じbrowser内の受付時間、低頻度timer遅延、host資源と記録healthは維持する。normalとminimalで次を固定し、別run・別専用開催へ保存する。

1. 同じアプリcommit・生成PC・ブラウザ・接続経路・worker数・VU数・採取周期。
2. 同じjoin/answerの分散、開始前マージン、予定問数（既定6問）、運営操作順と待機時間。
3. `operations.csv` の実操作ずれ、受付時間の標本数/中央値/p95/最大、host CPU/swap、各worker timer/loop遅延、保存待ち/欠測を比較する。
4. 順序効果を避けるため可能なら順番を入れ替えて反復する。手動操作差・端末負荷・時計・標本不足を記載する。

この変更による負荷削減率や計測影響ゼロは未実測であり主張しない。minimalで採らないhistogramを0と解釈しない。run中には重い集計・グラフ・圧縮を行わない。

## 中断・再試験・異常

- `stop` / Ctrl+C / stdin EOFは全workerへ停止を配布する。worker応答が60秒を超えて途絶えると異常停止、準備期限/実行期限も全体停止する。終了待ちは45秒後に対象子孫processを強制回収し、さらに9秒で収束を確認する。強制停止された区間はhealth未封印/不完全として扱う。
- 回答予約/クリック/受付観測の処理中に止めた場合は `manual_observation_interrupted` として計測不完全を残す。開始待ちだけの回答予約もこの保守的な判定に含む。結果を受け取る前の停止では、その後のアプリDB保存を推測しない。最後に保存されたsequence以降は不明。保存失敗がある場合、通常終了表示よりhealthとreportの不完全判定を優先する。
- worker起動失敗、範囲重複/欠損、IPC異常を無視して残りだけの成功にしない。manifestの予定母数を維持する。
- 再試験は新run ID、新出力directory、新専用開催を使う。旧DB・証跡を上書き/削除しない。新開催のID pool準備も人が行う。
- 親process自体の強制終了等でreportがないときは、既存証跡を読み、新しい出力先へ再集計できる。通常集計済みの証跡も同じ方法で保全する。

```sh
pnpm --dir load-test/browser control:report /absolute/evidence/RUN_ID-control /absolute/evidence/new-analysis
```

## 手動参加者を含む匿名DB照合

起動前manifestの `manualParticipantCount` と、終了後に明示した手動参加者のHMAC許可リストを使う。未知のDB参加者・回答は引き続きFAILであり、一律除外しない。機械VUとの重複指定もFAIL。50VUは300組、650VUは3900組で、未接続・停止workerの不足も残す。旧manifestなし分析は従来100人/600組専用として維持する。

同じrunの鍵を用い、手動端末に表示された3文字IDを所有者限定0600の `PRIVATE_LABELS.json` に `{"labels":["ABC"]}` の形で保存する。これは秘密入力でありGit・共有証跡へ入れず、生IDをコマンド引数へ直接渡さない。

```sh
uv run --project backend python -m quiz_app.observability.browser_membership manual-allowlist \
  --run-id RUN_ID --identity-key-file PRIVATE_KEY_FILE \
  --labels-file PRIVATE_LABELS.json --output NEW_ALLOWLIST.json
```

以下の既存Python DB verifierは6問照合用であり、今回変更していない。`EXPECTED_ROUNDS` の変更だけで7問・10問や複数PCのDB照合へ対応したとは扱わない。

DB snapshotは既存のREAD ONLY/REPEATABLE READの `snapshot` コマンドを、所有者が専用DB接続先を確認して実行する。詳細は[従来の照合手順](LOCAL_BROWSER_RETEST.md#計測v2)。

```sh
uv run --project backend python -m quiz_app.observability.browser_membership analyze \
  --snapshot NEW_SNAPSHOT.json --manifest RUN_DIRECTORY/manifest.json \
  --conditions WORKER_1/conditions.json WORKER_2/conditions.json \
  --events WORKER_1/events.jsonl WORKER_2/events.jsonl \
  --manual-allowlist NEW_ALLOWLIST.json --output NEW_MEMBERSHIP_REPORT.json
```

条件とeventsは届いたworker分だけ同じ順序で列挙する。残りはmanifestからmissing_workersとして報告し分母に残す。`--health` を省略すると各eventsと同じdirectoryの `evidence-health.json` を参照し、欠落もFAILにする。指定する場合はconditions/eventsと同順序。予定手動人数0ならallowlistを省略できる。DB snapshotは取得時点の保存存在を示し、計測窓内のcommit時刻・点数・順位・paintは別検証である。

## 隔離smokeの実行

実アプリへ接続しないfixtureを使う。

```sh
RUN_BROWSER_CONTEXT_SMOKE=1 BROWSER_CONTEXT_SMOKE_COUNT=3 \
RUN_BROWSER_DIAGNOSTICS_SMOKE=1 RUN_BROWSER_PROGRESS_SMOKE=1 RUN_BROWSER_MANUAL_SMOKE=1 \
pnpm --dir load-test/browser exec vitest run \
  tests/contextIsolation.smoke.test.ts tests/browserDiagnostics.smoke.test.ts \
  tests/participantProgress.smoke.test.ts tests/manualParticipant.smoke.test.ts \
  tests/manualAnswer.smoke.test.ts tests/browserWorkerCrash.smoke.test.ts
```

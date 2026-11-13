# 負荷試験 evidence handoff（現行v3）

既存v3コードの証跡回収と照合手順です。正式版受け入れは[600人同時接続の実測達成](LOAD_TEST_FORMAL_V3.md#正式版受け入れ)で判断します。以下のper-round照合や機械判定の手順を、正式版受け入れの追加条件にはしません。実行・撤去は[AWS手順](AWS_LOAD_TEST_AUTOMATION.md)に従います。

現行sourceは補助回答を廃止し、`prepare → schedule → publish → wait-results → advance`で進行する。`publish`は集計完了・公開可能を確認して運営mutationを送り、`wait-results`は読取だけを行う。集計system auditと公開operator auditを分離する。旧正式manifest・readiness・採点oracleの非互換とCLI停止線は維持し、現行sourceで実行可能という意味にはしない。

## 同時接続の証拠と回収の入口

- 600人同時接続の証拠は回収物`generator/generator-ramp-ready.json`です。`sessions=600`、`websocket_connected=600`、観測時刻とrun・manifest・試験対象の対応を確認します。累計参加人数ではありません。
- [証跡の回収](AWS_LOAD_TEST_AUTOMATION.md#証跡の回収)は`quiz_app.load_test_collect`を使います。既存コードの機械判定は元の値を保存し、正式版受け入れを別に記録します。
- [controller status](#controller-statusと検証request)と[per-round/final照合](#per-roundfinal照合)は、既存runnerの証跡を読み取るための技術仕様です。

manifestはv3、generator round evidence・verification request/receipt・controller journal/checkpointはv2です。DB verifier inputは `load-test-db-verifier-input-v1`、lock inputは `load-test-db-lock-observation-input-v1` を引き続き使います。これらのwrapperのv1は旧manifestの受入を意味しません。`ControllerStatus` 自体にformat fieldはなく、run/manifest等のidentityで束縛します。

## 正式runの証跡回収

[AWS手順の作成順序](AWS_LOAD_TEST_AUTOMATION.md#作成順序)でstartした後、repository rootから次のcollectorを起動します。これはAWSへ接続する次回run用の手順です。出力先はrepository外の**まだ存在しない**directoryを指定し、collectorが `0700` で新規作成します。既存directoryは上書きしません。

```sh
uv run --project backend --extra aws-login python -m quiz_app.load_test_collect \
  --manifest /secure/run/manifest.json \
  --preparation-evidence /secure/run/deployment/preparation.json \
  --output-directory /secure/run/collected
```

runnerはgeneratorを停止してから固定inventoryを検証し、`evidence/runner/archive.tar.gz` をSHA-256 metadata付きで保存します。回収対象はmanifest/preparation、generatorのramp・各Round・metrics、DB input/report/receiptとlock観測、controller request/checkpoint/journal、障害status/journal、metrics、最終result/acceptanceです。AWS runnerのtyped controller statusを手動status JSONとして別途採取する必要はありません。対象名とschemaの実装入口は [collection_inventory.py](../backend/src/quiz_app/load_test/collection_inventory.py) です。

collectorはarchiveと各artifactのhash・size・run/manifest・resultの参照先を照合し、`0600` で保存・再読込・fsyncした後だけ `evidence/runner/collected.json` へACKを記録します。認証情報、DB URL、runtime env、CA秘密鍵、自由形式のprocessログは回収しません。

- `status=collected`はローカル回収完了、`test_decision`は既存コードの機械判定です。機械PASSに必要な証跡の不足、artifactの改ざん、保存失敗ではACKしません。失敗・中断runは、残せた証跡と失敗判定を検証して回収します。
- `cleanup_status=not_verified` は撤去未確認です。撤去は [AWS status/SNS](AWS_LOAD_TEST_AUTOMATION.md#作成順序) と [独立した撤去処理](AWS_LOAD_TEST_AUTOMATION.md#独立した撤去処理) で別に確認します。controllerの完了や試験PASSを撤去完了と読み替えません。
- 回収待機は結果作成から最長10分、T0+5400秒、AWS期限30秒前のうち最も早い時点までです。未回収・PC停止・回収失敗でも期限撤去を優先し、AWS側の未回収証跡も削除します。runnerはACKとcleanup要求の成功前にPASSを出力しません。cleanup要求の成功もサービス別不存在の証明ではありません。

## controller statusと検証request

定義は [ControllerStatus](../backend/src/quiz_app/load_test/controller_contracts.py)、検証は [evidence_handoff](../backend/src/quiz_app/load_test/evidence_handoff.py) を参照します。AWS runnerはtyped statusを共通のmanifest/head検証へ直接渡します。手動CLIも同じ検証とcanonical inputの非上書きsealを使います。

| controllerの状態 | handoffでの扱い |
| --- | --- |
| `schedule` 成功: `status=waiting,next_action=publish` | 集計・公開待ち。DB照合入力には使いません。 |
| `publish` 集計待ち: `status=waiting,next_action=publish` | 集計完了・公開可能になるまで期限付きで再確認します。 |
| `publish` 成功: `status=waiting,next_action=wait-results` | 運営mutationで公開済み。次に結果とgenerator証跡を読取確認します。 |
| `wait-results` 未公開: `status=waiting,next_action=wait-results` | 待機。CLI自身はpollingしません。runnerが期限付きで再確認します。 |
| `wait-results` 公開確認済み: `status=waiting,next_action=advance` | `verification_requested_head_sha256` と `verification_request_artifact` が両方必要です。このstatusからper-round/final入力をsealします。 |
| Round 10の `advance` 成功: `status=completed,next_action=null` | Event完了。事前にsealしたfinal入力を使います。このstatusから入力を作り直しません。 |

statusの `run_id`、`event_id`、`source_commit`、`schema_revision`、`manifest_sha256`、`scenario_sha256` はmanifestと完全一致させます。requestのRound/question/時間枠、verification ID、head、artifactのname/hash/sizeとcanonical bytesも再検証します。`journal_head_sha256` を `verification_requested_head_sha256` の代わりに使いません。

### 手動CLIでのstatus保存例

以下は契約確認用の入口です。正式runの進行はAWS runnerに任せ、並行して手動操作を入れません。`REPOSITORY_ROOT` はrepositoryの絶対pathとし、各変数は対象runの検証済みartifactを指す値に設定します。controllerのpasswordはargvや環境変数へ置かず、CLIのTTY確認とpassword入力を使います。

manifest、専用CA、journal、checkpoint、request、generator evidence、receipt、status、DB input/reportはrepository外の用途別directoryへ分離します。手動handoffの出力directoryは事前に `0700` で用意し、各directoryを同一または包含関係にしません。通常file・single-link・所有者一致・exact `0600` とcanonical bytesを維持します。共有書込可能な `/tmp` は正式入力に使いません。

`wait-results` のstdoutは未使用のattempt名へ直接保存します。`umask 077` と `noclobber` は子shell内だけへ適用し、pipe、`tee`、editor、formatterを介しません。`schedule` のstatusを保存する場合も同じ方式です。

```bash
RESULTS_STATUS_FILE=/secure/controller-status/round-01-results-attempt-01.json
(
  umask 077
  set -o noclobber
  "$REPOSITORY_ROOT/backend/.venv/bin/python" -m quiz_app.controller_cli wait-results \
    --manifest /secure/manifest/load-test-manifest.json \
    --repository-root "$REPOSITORY_ROOT" \
    --journal "$CONTROLLER_JOURNAL_FILE" \
    --checkpoint-directory "$CONTROLLER_CHECKPOINT_DIRECTORY" \
    --verification-request-directory "$VERIFICATION_REQUEST_DIRECTORY" \
    --username "$OPERATOR_USERNAME" \
    --ca-file "$PRIVATE_CA_FILE" \
    --generator-evidence "$GENERATOR_EVIDENCE_FILE" \
    > "$RESULTS_STATUS_FILE"
)
```

公開確認済みstatusだけをhandoffへ渡します。待機statusは上書きせず、再確認ごとに未使用名を使います。失敗時の空・partial fileを削除・修正して再利用せず、そのrunを停止します。保存後は `load_controller_status` が末尾LFを含むcanonical statusを再検証します。

## per-round/final照合

現行DB verifierは、`aggregate_round_results`をaccount/keyなしのsystem actor、所定の自動集計理由・Round targetと照合します。`publish_round_results`はoperator accountと公開用の冪等keyを要求します。全10 Roundの最終期待は **60 audit・40冪等record** です。内訳は準備・予約・集計・公開・backup検証が各10件、次問進行9件、開催終了1件であり、集計とbackup検証は冪等recordを作りません。全回答の自動生成ID、得点ledger、選択肢集計、順位も検証します。入力をsealできたことだけでDB照合PASSにはなりません。この判定を正式版受け入れへ追加しません。

AWS側の入口は [RunDatabase](../backend/src/quiz_app/load_test/orchestrator_database.py) と [orchestrator](../backend/src/quiz_app/load_test/orchestrator.py) です。手動で同じ入力契約を確認する場合は [evidence_handoff_cli](../backend/src/quiz_app/evidence_handoff_cli.py) を使います。

```sh
"$REPOSITORY_ROOT/backend/.venv/bin/python" -m quiz_app.evidence_handoff_cli build-db-input \
  --manifest /secure/manifest/load-test-manifest.json \
  --repository-root "$REPOSITORY_ROOT" \
  --controller-status "$RESULTS_STATUS_FILE" \
  --verification-request-directory "$VERIFICATION_REQUEST_DIRECTORY" \
  --output-directory /secure/db-inputs \
  --mode per-round
```

1. Round 1–9は公開確認済みstatusから `per-round` 入力をsealし、SELECT-only verifierのPASS receiptを得てから `advance` します。
2. Round 10も `per-round` をseal・検証します。同じ公開確認済みstatus/requestから **Event完了前** に `final` 入力もsealします（上記CLIの `--mode final`）。生成済みartifactは上書き・自動再利用せず、同名衝突なら停止します。
3. Round 10のper-round PASS receiptをcontrollerへ渡して `advance`（Event complete）します。`status=completed` 後、pre-seal済みの同じfinal入力のbytes/identityを再検証してSELECT-onlyのfinal照合を行います。完了statusから入力を再生成しません。

DB実行の入口は [db_verifier_cli](../backend/src/quiz_app/db_verifier_cli.py) です。`DB_INPUT_FILE` はseal結果のnameから特定した入力、`VERIFIER_DATABASE_URL_FILE` と `POSTGRES_CA_FILE` は準備済みverifier専用設定、`POSTGRES_CA_SHA256` は確定済みchecksumを指定します。`GENERATOR_EVIDENCE_FILE` は対象Round（finalではRound 10）の証跡です。

```sh
"$REPOSITORY_ROOT/backend/.venv/bin/python" -m quiz_app.db_verifier_cli \
  --input "$DB_INPUT_FILE" \
  --manifest /secure/manifest/load-test-manifest.json \
  --repository-root "$REPOSITORY_ROOT" \
  --database-url-file "$VERIFIER_DATABASE_URL_FILE" \
  --ca-file "$POSTGRES_CA_FILE" \
  --ca-sha256 "$POSTGRES_CA_SHA256" \
  --output-directory /secure/db-reports \
  --generator-evidence "$GENERATOR_EVIDENCE_FILE"
```

verifierは専用read-only roleで `REPEATABLE READ READ ONLY` のSELECT-only照合を行います。DB URL本文やcredentialをargvへ展開しません。schema適用・role準備は [AWS手順](AWS_LOAD_TEST_AUTOMATION.md#oci-image-と-runtime-の配布手順) の人手工程であり、handoff/verifierはDDL・migration・DB補正を実行しません。

## lock observation input

AWS runnerは各RoundのOpenから15秒間、1秒ごとの16標本を観測します。手動入力を準備する場合は実行時刻を逃さないよう事前に10 Round分を `build-lock-input` でsealします。入力作成自体はDBへ接続せず、観測済みの証拠にもなりません。

```sh
"$REPOSITORY_ROOT/backend/.venv/bin/python" -m quiz_app.evidence_handoff_cli build-lock-input \
  --manifest /secure/manifest/load-test-manifest.json \
  --repository-root "$REPOSITORY_ROOT" \
  --output-directory /secure/db-lock-inputs \
  --round-number 1
```

観測は上記 `db_verifier_cli` へlock inputを渡し、`--generator-evidence` を省略します。最終標本のlock wait 0と中間peakを区別して記録します。handoff CLIのstdoutは作成したartifactの `name`、`sha256`、`size_bytes` だけで、拒否時は固定codeを返します。

## 既存コードが回収する診断記録

以下は既存コードが記録する診断内容です。実施状況・機械判定はrunごとのresultに残し、未実施を実施済みと扱いません。

- 600 VUの接続確立、80分・全10 Roundの回答・再送・保存結果照会・運営mutationによる結果公開。
- 実controllerのrequest/checkpoint/journalと、全10回のper-round PASS receipt、Event完了後のfinal照合。
- 各RoundのOpenから+15秒までのlock観測、障害注入・復旧、metricsを含む総合判定。
- 実targetのdirectory/UID分離、同UIDの承認済みprocess inventory、Docker/systemd/artifact writerの排他性と外部mutation不在の運用記録。
- collectorによる全artifactのローカル保存・再読込・ACKと、別途のサービス別撤去確認。

ローカルunit test・lint成功を600人同時接続や撤去完了の証拠にしません。

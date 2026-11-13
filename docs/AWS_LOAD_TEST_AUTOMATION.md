# AWS 負荷試験の作成・期限・自動撤去

本書の価格・期限制御は既存の正式負荷試験プログラムの仕様である。既存コード・登録済みAWS予約を自動変更するものではない。

> 正式版の受け入れは600人同時接続の実測で判断する。インスタンス起動時間は受け入れ条件に含めない。既存コードの開始・撤去期限や自動停止によって測定前に停止した回は、達成未確認と記録する。使用する版と記録方法は[正式負荷試験](LOAD_TEST_FORMAL_V3.md)を参照する。

この機能は `quiz_app.load_test_aws` と `infra/aws-load-test/{control,workload}.json` で、1 run 専用の AWS 資源を作成する。Cloudflare、R2、公開 DNS、問題画像は使用しない。AWS の VPC 内で独立した runner から app の固定 private IP `10.77.1.10` に HTTPS 接続する（runner は `10.77.1.11`）。秘密値を含む設定は repository 外の所有者専用 `0600` file として渡す。symlink と group/world-writable な祖先 directory は拒否するため、`/tmp` や `/private/tmp` は正式入力に使わない。

## 固定構成と期限

- 東京リージョン、app `m6a.2xlarge`、runner `m6a.xlarge`、Ubuntu 24.04 x86_64、RDS PostgreSQL `db.t4g.small`、Redis 7.1 `cache.t4g.small`。
- 600 接続、80 分、10 問、4 障害。既存 schema 適用だけは人間が行う。
- EC2 は SSM・配布物取得用 public IPv4 を持つ。Internet からの inbound rule はなく、app の 443 は runner security group だけを受け付ける。NAT Gateway と Load Balancer は作らない。
- RDS は 20 GiB gp3、Single AZ、backup retention 0、final snapshot 無効。Redis の snapshot retention は 0。app と exporter は別 ACL user / password。
- plan の `created_at` は最初の作成前に一度だけ確定する。`expires_at = created_at + 3 時間` に撤去を開始し、価格計算には 4 時間分を含める。残り 90 分未満での prepare/start は拒否する。
- 開始直前に AWS Price List `GetProducts` から再計算する。為替 200 円/USD、消費税 10%、RDS の 2 vCPU 全時間 surplus credit、IPv4 2 個、EBS 120 GiB、S3/ECR/制御 API 等の予備 0.50 USD を含む。1000 円超の見積は作成を拒否する。

AWS の削除完了時間や為替・実使用量は保証できないため、1000 円を強制する AWS の支払上限ではない。削除が失敗した資源の課金は続き得る。`done=true` は観測時点で対象の課金資源が残っていないことを示し、請求明細が即座に 0 円になる意味ではない。

## 作成順序

1. `plan` は AWS を呼ばず template と固定見積を表示する。`preflight` は必須の `--manifest` を既存 v3 契約で読み、API/gateway/generator の image digest と run/source/schema/固定 origin/hash を確定した後、STS account、時刻、Canonical の Ubuntu 24.04 AMI、最新価格を読み取る。異なる account や未確認認証のまま作成しない。
2. `prepare --execute` は省略された CFN role を run 専用に作る。既存 role を使う場合は `cloudformation_role_arn` を指定する。専用 role の権限は `cfn-execution-policy.json` を run の ARN/AMI/tag に束縛し、6144 文字以内の managed policy に分割する。`iam:PassRole` は run 専用 role と利用 service に限定し、全サービス全権の policy は使わない。
3. SchedulerのNameを`${Prefix}-deadline`、GroupNameを`${Prefix}`とし、別control stackの物理名競合を防ぐ。CFNの名前競合検証用default group参照は同じrun名へのGetScheduleだけに限定する。control stack を作成し、SNS email の購読確認、Scheduler の enabled/期限/入力/宛先を readback する。両方の成功後に初めて DLQ の CloudWatch Alarm を SDK で作成し、固定名・設定・run 所有 tag を再照会する。既存の同名 Alarm は上書きせず、設定・所有が異なれば拒否する。確認前に Alarm・worker・workload は作成しない。
4. Linux CPython 3.12 x86_64 向け worker zip を SHA-256 検証し、Lambda に直接 upload する。Lambda package は `quiz_app.load_test_aws`、Pydantic、logfire、boto3 と依存を含む。macOS の `.venv` は upload しない。
5. 追加承認により worker は未予約枠を使う。Active・code digest・role・handler・実 Timeout 120 秒・$LATEST、予約設定なし、GetAccountSettings の未予約枠が正であることを readback 後に workload stack を作る。start でも同じ検証を行い、予約設定を含むずれは上書きせず拒否する。未予約枠は現在の空き数や枠の専有を保証しない。他functionとの枠競合や複数cleanupの競合は失敗通知・有限再試行・新しい残存走査で処理する。Python 3.12・x86_64・Zip・追加 layer なしに固定し、SnapStart・Durable・Managed Instances 設定、別 version を含む provisioned concurrency、一覧の未取得ページを拒否する。CA、配布 bundle、Compose、実行設定は deployment 工程で app/runner 別の S3 prefix に配る。
6. 人間が既存 migration を適用し、application 非 owner role と read-only verifier role を用意する。`SchemaReceipt` と `AwsPreparationEvidence` が揃うまで start は拒否する。receipt は省略による自動 schema 適用を許可しない。自動 DB preflight が実 DB の role と event 条件を再検証する。
7. start は固定 SSM document で runner の orchestrator を `gsloadtest` UID 1001 として起動する。app の fault/export は別の固定 document の `faults|export` enum に限定する。

両 SSM document の実行 timeout は 10,800 秒とし、T0 より早く start した場合の待機も含める。例えば作成後30分で start、T0 が作成後85分なら、55分の待機に80分の試験と証跡回収が続く。実際の停止期限は `expires_at - 30秒` の RunClock と `expires_at` の独立 AWS 撤去を優先し、この timeout で期限を延長しない。SendCommand の `TimeoutSeconds` は未開始の command に対する配送期限であり、document 内の実行 timeout とは別である（[AWS の timeout 仕様](https://docs.aws.amazon.com/systems-manager/latest/userguide/monitor-commands.html)）。

`prepare` は段階的であり、`execution_role_created` / `control_creating` / `worker_creating` / `workload_creating` を返したら AWS の状態を確認して再実行する。`awaiting_human_schema_and_deployment` まで進んでも、自動 schema 操作は行わない。作成に失敗した stack は `OnFailure=DELETE` で撤去する。`FORCE_DELETE_STACK` や `RetainResources` で失敗資源を見えなくしない。

課金資源作成前に制御定義を補正した場合は、control stack の `UPDATE_COMPLETE` を準備済みとして受理し、通知・期限・worker の実設定を再確認する。workload は `CREATE_COMPLETE` のみを受理し、再構築した試験環境を正式runの継続として扱わない。

CloudFormation service role の `states:UpdateStateMachine` は、その run の `${Prefix}-cleanup` という完全一致 ARN にだけ許可する。既存 managed policy の差異は自動上書きせず拒否するため、既に作成した role にこの権限を補う場合は、変更内容を確認してから別途適用する。

```sh
uv run --project backend python -m quiz_app.load_test_aws plan --plan /secure/run/plan.json
uv run --project backend python -m quiz_app.load_test_aws preflight --plan /secure/run/plan.json --manifest /secure/run/manifest.json
uv run --project backend python -m quiz_app.load_test_aws prepare --plan /secure/run/plan.json --manifest /secure/run/manifest.json --worker-package /secure/run/cleanup-linux.zip --execute
uv run --project backend python -m quiz_app.load_test_aws start --plan /secure/run/plan.json --manifest /secure/run/manifest.json --preparation-evidence /secure/run/deployment/preparation.json --schema-receipt /secure/run/deployment/schema.json --execute
uv run --project backend python -m quiz_app.load_test_aws status --plan /secure/run/plan.json
uv run --project backend python -m quiz_app.load_test_aws cleanup --plan /secure/run/plan.json --execute
```

preflight / prepare の JSON 出力には時刻・SKU・単価を含む pricing evidence が含まれるため、run のローカル証跡として保存する。SNS 宛先は plan の実行時入力であり、コードに埋め込まない。CLI の `--execute` は意図しない書込みを避ける明示 flag である。

## ローカル配布物の作成

変更を commit してから、既存 lockfile の Lambda 用依存 group を使って Linux 向け zip を生成する。追加のアプリ依存は導入しない。

```sh
bash infra/aws-load-test/build-worker.sh /secure/run/new-worker-build
```

出力の `cleanup-worker.zip` と `cleanup-worker.sha256` を plan へ対応付ける。ビルドは Python 3.12 / x86_64 manylinux wheel を hash 検証し、固定順・固定 timestamp の zip を作る。Mac のネイティブライブラリを配布しない。`aws login` を使うローカル CLI は `uv run --project backend --extra aws-login ...` で公式 CRT extra を有効にする。EC2/Lambda role 認証にはこの extra を含めない。

## OCI image と runtime の配布手順

OCI image は AWS 資源の作成前に build する。repository を commit 済み・変更なしにし、repository 外に所有者専用 `0700` の空ディレクトリを作る。次の CLI は build 前後に同じ commit と変更なしを再確認する。

```sh
mkdir -m 700 /secure/run/oci
uv run --project backend python -m quiz_app.load_test_aws.deployment_build_cli --source-commit <40桁の確定commit> --output-directory /secure/run/oci
```

`app.oci.tar`、`gateway.oci.tar`、`images.json` と build log が `0600` で生成される。`images.json` の digest は OCI image **manifest 本文**の SHA-256 であり、Docker ImageID や config digest を転用しない。export は linux/amd64、gzip layer、provenance 無効に固定する。すべての blob の hash・size、source label、`aws-demo` label、architecture を検証する。gateway の静的 assets は build host の CPU で作り、最終 Caddy image は amd64 にする。

run UUID が決まったら、manifest の API/gateway image reference を `<account>.dkr.ecr.ap-northeast-1.amazonaws.com/gs-lt-<run UUIDからハイフン除去>/app@sha256:...` と同じ prefix の `/gateway@sha256:...` にする。generator は正式契約の k6 image digest に固定する。`public_origin=https://10.77.1.10` として v3 manifest を確定し、その canonical hash を plan に入れる。manifest と plan の確定後に `preflight` / `prepare` を行い、workload が準備できたら次へ進む。

```sh
mkdir -m 700 /secure/run/deployment
uv run --project backend --extra aws-login python -m quiz_app.load_test_aws.deployment_cli publish --plan /secure/run/plan.json --manifest /secure/run/manifest.json --output-directory /secure/run/deployment --app-archive /secure/run/oci/app.oci.tar --gateway-archive /secure/run/oci/gateway.oci.tar --execute
```

publish は事前確定した archive 内の同じ blob と manifest bytes を ECR に送り、ECR の digest と manifest 本文を readback する。別 run の repository、manifest にない image、期限・通知を確認できない環境は拒否する。ECR push のための再buildは行わない。

runtime の `dependencies.json` は `RuntimeDependencies` 契約で、公式 repository の監視 image 5 個を digest 固定し、`postgres_ca_sha256`、`node_version`（22系）、`node_archive_sha256`、`uv_archive_sha256`、`docker_package_version`（28/29系）、`docker_compose_package_version`（2.30以降の2系）を持つ。Node/uv の Linux x86_64 archive は公式 checksum と照合し、RDS 東京リージョンの CA bundle も hash 固定する。app hostname、Redis private IPv4、RDS/Redis endpoint は CFN 出力と app 内の読み取り専用 DNS 確認から自動取得する。

```sh
uv run --project backend --extra aws-login python -m quiz_app.load_test_aws.deployment_cli build --plan /secure/run/plan.json --manifest /secure/run/manifest.json --output-directory /secure/run/deployment --settings /secure/tools/dependencies.json --postgres-ca /secure/tools/postgres-ca.pem --node-archive /secure/tools/node.tar.xz --uv-archive /secure/tools/uv.tar.gz
uv run --project backend --extra aws-login python -m quiz_app.load_test_aws.deployment_cli install --plan /secure/run/plan.json --output-directory /secure/run/deployment --execute
```

build は短期 CA と app private IP の SAN を持つ証明書、run 専用 credential、Fernet key、release/runtime/exporter 設定を生成する。CA の秘密鍵は配布しない。app と runner は別 S3 prefix・別 bundle とし、app の DB password/TLS秘密鍵を runner へ配らない。candidate と recovery release は同じ image digest を使い、release ID だけ分ける。これは障害 timer による同じ artifact の復旧用であり、通常アプリの過去リリースへの rollback と区別する。

install は UID/GID 1001 の `gsloadtest`、user systemd の linger、固定 Docker CLI 設定、hash 固定 tool、lockfile 由来の Python 依存、Compose とソースを用意する。schema 適用・role 作成・grant は実行しない。人間が既存 migration を `quizadmin` として適用した後、生成された `deployment/human-database.sql` を実行する。この SQL は app 非 owner role、verifier の列単位 SELECT と `pg_read_all_stats`、監視用 role を作る。verifier の read-only は接続設定で固定するため、role-level setting を追加しない。

人手作業後、外部 `0600` の `human-confirmation.json` に次の契約を記録する。値は対象 plan の run/account/schema と実際の UTC 確認時刻に合わせる。

```json
{
  "format": "load-test-human-schema-confirmation-v1",
  "run_id": "<planと同一のUUID>",
  "account_id": "<12桁account>",
  "schema_revision": "20260804_0002",
  "schema_and_roles_applied_by_human": true,
  "confirmed_at": "<UTCのISO8601時刻>"
}
```

```sh
uv run --project backend --extra aws-login python -m quiz_app.load_test_aws.deployment_cli activate --plan /secure/run/plan.json --output-directory /secure/run/deployment --human-confirmation /secure/run/human-confirmation.json --execute
uv run --project backend --extra aws-login python -m quiz_app.load_test_aws start --plan /secure/run/plan.json --manifest /secure/run/manifest.json --preparation-evidence /secure/run/deployment/preparation.json --schema-receipt /secure/run/deployment/schema.json --execute
```

activate は空DBへ run 専用の Event/設問/運営アカウントを一度 INSERT し、Compose と private TLS を起動する。runner から TLS、health、現行 schema、verifier の実効権限、対象 Event が単一であることを読み取り確認する。成功した `SchemaReceipt` と `AwsPreparationEvidence` を両 host へ seal してから、ローカルの `preparation.json` / `schema.json` を出す。片方の保存が失敗したときも start 用証拠は出さない。publish/install/activate の失敗・SIGINT・SIGTERM は共通 cleanup に合流する。確認できない準備状態を PASS にしない。

## 証跡の回収

start 後、ローカルで次の collector を起動する。出力先はまだ存在しない repository 外の directory とし、CLI が `0700` で新規作成する。既存 directory は上書きしない。

```sh
uv run --project backend --extra aws-login python -m quiz_app.load_test_collect --manifest /secure/run/manifest.json --preparation-evidence /secure/run/deployment/preparation.json --output-directory /secure/run/collected
```

runner は generator を停止してから、manifest・preparation・DB 検証・generator・障害 journal・metrics・controller request/checkpoint/journal・最終 result の固定 inventory を schema 検証する。認証情報、DB URL、runtime env、CA private key、process の自由形式ログは含めない。S3 の `evidence/runner/archive.tar.gz` に SHA-256 metadata 付きで保存し、ローカル回収の ACK を待つ。

collector は archive と全 artifact の hash・size・run/manifest・最終 result が参照する証跡を照合し、`0600` で保存・再読込・fsync した後だけ `evidence/runner/collected.json` へ ACK を記録する。PASS の証跡不足や改ざん、保存失敗は ACK せず失敗する。ローカルには archive・inventory・各 artifact・checksum・ACK が残る。`status=collected` と `test_decision` は回収と試験判定を表す。`cleanup_status=not_verified` の通り、撤去完了は別に `status` / SNS の結果で確認する。

回収待機の判定期限は結果作成から最長10分、かつ T0+5400秒と AWS 期限30秒前のうち最も早い時点。I/O 前後で期限を確認するため、進行中の SDK 呼出しは有限の timeout/retry 分だけ判定を越える可能性がある。独立した AWS 側期限はこの待機に依存しない。再度の SIGINT/SIGTERM は回収待機を中断する。未回収・PC停止・回収失敗でも finally で同じ cleanup を要求する。runner は ACK と cleanup 要求の成功前に PASS を出力しない。期限時は未回収の S3 証跡も削除する。

app は app-worker 証跡だけ、runner は archive だけを書ける。ACK の作成はローカル認証に限定する。runner の ListBucket は ACK の exact prefix に限定し、ACK が存在してから内容を取得する。存在しない key への GET が ListBucket 権限なしでは 403 になる仕様を踏まえ、AccessDenied を待機へ変換しない。[AWS GetObject 仕様](https://docs.aws.amazon.com/AmazonS3/latest/API/API_GetObject.html)

## 独立した撤去処理

正常・失敗・signal の終了経路と Scheduler は、同じ入力・名前 `cleanup` の Standard Step Functions execution に合流する。PC、runner、app のいずれを失っても、workload 作成前に設定済みの AWS 側期限は残る。Scheduler API の `ActionAfterCompletion=DELETE` を設定して readback する。CloudFormation の Schedule resource がこの property を公開していないため、SDK 設定を省略しない。

cleanup Lambda は 120 秒の一巡のみを担当し、Step Functions が一時エラーへ backoff/jitter 付き最大 2 回の task retry を行い、開始から最初の 1 時間は 60 秒間隔、それ以降は 300 秒間隔で再確認する。1 時間時点で残存を再通知する。上限は全体で 24 時間とし、API 呼出しごとに実経過を確認して最後の 10 分を終了処理・失敗通知へ確保する。task 自体の失敗も外側の巡回で再試行する。 制御資源の不存在を確認した段階を実行内へ保存し、再試行は未完了の段階から行う。完了済みの削除を毎回再実行して Standard の[25,000 event 履歴上限](https://docs.aws.amazon.com/step-functions/latest/dg/service-quotas.html)へ達することを避ける。AccessDenied を含む worker の拒否は待機前に SNS へ通知する。途中の SNS publish 自体が失敗しても撤去は続け、`notification_failed` と履歴に残す。途中を含む通知失敗があれば、資源撤去の結果と区別して `CleanupNotificationFailed` として終了し、独立 EventBridge rule も通知を試みる。失敗・timeout・abort は SNS と独立 EventBridge rule が通知する。通知先購読は workload 作成前の必須条件である。

削除済み stack も含む CFN の物理 ID 台帳と run 所有 tag（間接作成 volume には AWS 予約 stack tag）を照合して通常の DeleteStack を要求し、stack が消えた後に直接 API scan で漏れを確認する。cleanup 開始時には台帳に一致する app/runner EC2 を先に terminate し、fault timer と試験 process を停止する。対象は VPC/subnet/ENI/security group/route table/Internet Gateway、EC2、EBS、EBS snapshot、AMI、Elastic IP、RDS instance/manual snapshot/retained automated backup、Redis group/manual snapshot、ECR image/repository、S3 の current/old version/delete marker/multipart upload、Secrets Manager、workload log group。S3 は旧 version まで空にしてから bucket を削除する。RDS は final snapshot を作らず automated backup も削除する。削除 API の受付だけで完了にせず、次の scan で不存在を確認する。所有証明が不一致・不足の場合は削除を拒否し、完了とも判定しない。

workload の不存在後、Step Functions が deadline を削除し、worker function と全 version/alias を削除する。不存在を確認したら600秒待機し、再度不存在を確認してから制御 alarm / DLQ / Lambda log group を直接 SDK integration で消す。最後に alarm と log group の不存在を確認して成功を通知する。 AlarmTypes を省略すると Metric のみが返るため、作成前・撤去後とも Metric / Composite / Log の全 3 種を明示して照会し、Composite を返すための DescribeAlarms 読取権限だけは [AWS の仕様](https://docs.aws.amazon.com/AmazonCloudWatch/latest/APIReference/API_DescribeAlarms.html)に従い Resource=* とする（削除権限は run の固定名に限定）。worker に `logs:CreateLogGroup` 権限はなく、Lambda の最後のログ出力で group が再作成されることを防ぐ。worker upload 前に PC を失った場合も、ページ末尾まで stack 履歴を照合して当該 workload が一度も作成されていないことを確認し、control の有料部分を撤去する。既存 stack があるのに worker が失われた場合は成功にせず通知・再確認する。

600 秒は標準 on-demand worker の既存実行を待つ保守的な猶予である。[Lambda lifecycle の公式仕様](https://docs.aws.amazon.com/lambda/latest/dg/lambda-runtime-environment.html)は通常 INIT を10秒、初回 invocation での INIT 再試行を設定 timeout、INVOKE 全体を設定 timeout、SHUTDOWN を最大2秒としている。実 Timeout 120 秒を前提とする各フェーズの和は `10 + 120 + 120 + 2 = 252秒` であり、600秒はこれを上回る。[DeleteFunction](https://docs.aws.amazon.com/lambda/latest/api/API_DeleteFunction.html)は全version/aliasを削除するが、既存実行の即時停止を保証する記述はないため、600秒だけで排出を保証したとは扱わない。削除後の不存在、600秒待機、再度の不存在、CreateLogGroup 不許可を組み合わせる。削除確認・待機終了・待機後確認を別phaseに保存し、再試行で待機を飛ばしたり、待機後の一時的な照会失敗で600秒を繰り返したりしない。待機後の再出現は成功にせず通知・再照会し、外部による再作成か不明な同名functionを無条件に削除しない。$LATEST は [provisioned concurrency 非対応](https://docs.aws.amazon.com/lambda/latest/dg/provisioned-concurrency.html)で、追加の INIT モードも開始前に拒否する。600秒の待機へ入る期限は実行開始23時間40分後までとし、待機後にも最終失敗通知用の10分を残す。

無料の IAM role/policy、CloudFormation control stack metadata、SNS topic/subscription、EventBridge rule、空の Scheduler group、実行停止した Standard state machine と AWS が保持する履歴は残る。継続して保存課金される Lambda code、CloudWatch logs/alarm、S3/ECR、volume/snapshot/backup は残さない。証跡を回収できない場合も期限撤去を優先する。障害時は秘密値を含まない Step Functions 履歴と SNS の run 情報から復旧する。

## 検証と制約

`tests/test_load_test_aws.py` は AWS I/O を mock し、別 account/所有 tag の拒否、S3 部分削除失敗、非同期削除中の扱い、期限、独立 private host/CA 証拠、有限権限、self log 再作成防止を確認する。CFN は `cfn-lint` で静的検証する。これらは AWS での実行確認を代替しない。AWS 統合試験の前に IAM policy と State Machine definition のサービス側 validation、SNS 購読、schema receipt、配布 bundle と pricing evidence が必要である。

参照: [CFN service role](https://docs.aws.amazon.com/AWSCloudFormation/latest/UserGuide/using-iam-servicerole.html)、[DeletionPolicy](https://docs.aws.amazon.com/AWSCloudFormation/latest/TemplateReference/aws-attribute-deletionpolicy.html)、[RDS 削除](https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/USER_DeleteInstance.html)、[Scheduler universal target](https://docs.aws.amazon.com/scheduler/latest/UserGuide/managing-targets-universal.html)、[Standard StartExecution の冪等性](https://docs.aws.amazon.com/step-functions/latest/apireference/API_StartExecution.html)、[Canonical AMI](https://ubuntu.com/aws/docs/aws-how-to/instances/find-ubuntu-images/)。CFN execution policy の action は cfn-lint に同梱された AWS resource provider schema の create/read/delete handler permissions を基に、この template が使う resource variant へ限定している。

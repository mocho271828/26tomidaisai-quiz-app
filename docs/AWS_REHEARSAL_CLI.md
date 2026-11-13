# AWS CLIによるリハーサルの公開・終了

本書は配置条件を決めた後に担当者が実行する手順である。今回の作業ではAWSを操作していない。公開にはAWS発行の `https://<distribution>.cloudfront.net` を使い、終了時は専用環境とデモデータを全削除する。復旧用image、rollback baseline、復元手順は要求しない。

AWSの設定操作はAWS CLIで行える。MacにはAWS CLI v2とjq、SSM接続にはSession Manager plugin、EC2には既存のDocker Compose・uv実行環境が必要。アプリのbuildとDB準備は [AWSリハーサル](AWS_REHEARSAL.md) に従う。

新規開催の開始待機・採点・結果表示は、[単一設定JSON](../backend/src/quiz_app/quiz_configuration/quiz-settings.json)を正本にする。現行値は待機3秒、正解400点＋速度最大100点、0.1秒ごとに1点減である。設定の変更と開催への固定方法は[クイズ設定](QUIZ_CONFIGURATION.md)を参照する。

## 構成と事前条件

```text
ブラウザー（参加者 / 運営）
  └ HTTPS / WSS :443 → CloudFrontのAWS発行URL
      └ VPC origin → 非公開EC2のprivate IPv4 :18080
          └ Caddy :8080 → 既存FastAPI
              ├ PostgreSQL（証明書検証付きTLS）
              └ Redis（認証付きTLS）
```

[公開入口template](../infra/aws-rehearsal/public-edge.yaml) はCloudFront distribution、VPC origin、専用EC2 SGへの受信ruleだけを管理する。EC2、PostgreSQL、Redis、VPC、SSM接続経路を作るtemplateではない。これらの新設・既存専用環境の選択とサイズを配置条件として確定する。正式負荷試験用templateは流用しない。

公開入口には次の条件を満たす専用EC2を渡す。

- 東京リージョンの対応AZのprivate subnetで起動済み。CloudFront VPC originsが非対応のAZ ID `apne1-az3` を除外する（accountごとに対応が異なるAZ名の末尾文字では判定しない）。public IPv4なし。VPCにはInternet Gatewayをattachするが、private subnetにInternet Gatewayへの直接routeを作らない。
- CloudFrontのENIを置ける空きIPv4と、同じVPCの専用EC2 Security Groupがある。SGのgateway port受信はCloudFrontのorigin-facing managed prefix listからだけ許可する。既存の全開放ruleやSSH公開がないことを確認する。
- SSMがオンラインで、image取得・必要な依存先へ接続できる。SSM等のVPC endpointまたは管理用の外向き経路は、配置条件に含めて準備する。
- API、DB、Redis、Docker socket、Caddy adminはinternetへpublishしない。gatewayのloopback bindを残し、公開用overrideでprivate IPv4だけを追加する。
- 同じデモIDの専用workload stackにEC2/EBS、DB、Redis、secret、ログ、networkを集約する。共有資源は参照だけとし、そのstackに取り込まない。

CloudFrontは無効状態で作成する。全経路を `CachingDisabled` / `AllViewer` とし、Cookie、Origin、CSRF header、query、WebSocket handshakeを転送する。エラーもTTL 0にし、APIの404をindex.htmlへ変換しない。URLの発行だけで参加受付を開始しない。[VPC originsの条件](https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/private-content-vpc-origins.html)、[WebSocket転送](https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/distribution-working-with.websockets.html)

利用者の「内部TLSで工程が増える場合は不要」という判断に従い、このリハーサルではCloudFrontからgateway、同一host内のAPIへの経路だけHTTPを許可する。内部TLSにはorigin用の証明書発行・名前検証・配置が追加で必要になるためである。ブラウザー側HTTPS/WSS、Secure Cookie、DB/RedisのTLSは維持する。[CloudFrontのorigin証明書条件](https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/cnames-and-https-requirements.html)

## 1. 実行対象を一度設定する

以下はMacのrepository rootで、同じbash session内で実行する例。`<...>` は確定した非秘密値へ置き換える。secretをこの設定へ追加しない。

```bash
bash
set -euo pipefail
umask 077
rehearsal_profile='<AWS profile>'
rehearsal_region='ap-northeast-1'
rehearsal_account='<12桁のAWS account ID>'
rehearsal_id='<32文字以内の小文字英数字とハイフンのデモ識別子>'
rehearsal_instance='<専用EC2 instance ID>'
rehearsal_security_group='<そのEC2にattachした専用SG ID>'
rehearsal_workload_stack='<専用workload stack名>'
rehearsal_edge_stack="gs-rehearsal-${rehearsal_id}-edge"
rehearsal_records='<repository外の未使用の台帳directory絶対path>'
mkdir -m 700 "$rehearsal_records"
cp infra/aws-rehearsal/public-edge.yaml "$rehearsal_records/public-edge.yaml"

rehearsal_aws() {
  aws --profile "$rehearsal_profile" --region "$rehearsal_region" \
    --no-cli-pager "$@"
}
test "$(rehearsal_aws sts get-caller-identity --query Account --output text)" = "$rehearsal_account"
rehearsal_aws ec2 describe-instances --instance-ids "$rehearsal_instance" \
  --output json > "$rehearsal_records/instance.json"
jq -e '
  [.Reservations[].Instances[]] | length == 1' "$rehearsal_records/instance.json"
jq -e --arg sg "$rehearsal_security_group" --arg demo "$rehearsal_id" '
  .Reservations[0].Instances[0] |
  .State.Name == "running" and .PublicIpAddress == null and
  any(.SecurityGroups[]; .GroupId == $sg) and
  any(.Tags[]; .Key == "DemoId" and .Value == $demo)
' "$rehearsal_records/instance.json"
rehearsal_private_ip="$(jq -r '.Reservations[0].Instances[0].PrivateIpAddress' "$rehearsal_records/instance.json")"
rehearsal_private_dns="$(jq -r '.Reservations[0].Instances[0].PrivateDnsName' "$rehearsal_records/instance.json")"
rehearsal_vpc="$(jq -r '.Reservations[0].Instances[0].VpcId' "$rehearsal_records/instance.json")"
rehearsal_subnet="$(jq -r '.Reservations[0].Instances[0].SubnetId' "$rehearsal_records/instance.json")"
rehearsal_aws ec2 describe-subnets --subnet-ids "$rehearsal_subnet" \
  --output json > "$rehearsal_records/subnet.json"
jq -e '.Subnets | length == 1 and .[0].AvailabilityZoneId != "apne1-az3" and
  .[0].AvailableIpAddressCount > 0' "$rehearsal_records/subnet.json"
rehearsal_prefix_list="$(rehearsal_aws ec2 describe-managed-prefix-lists \
  --filters Name=prefix-list-name,Values=com.amazonaws.global.cloudfront.origin-facing \
  --query 'PrefixLists[0].PrefixListId' --output text)"
```

EC2以外も `DemoId` tagと作成時台帳で専用資源か確認する。subnetのroute、SGの全rule、SSM Onlineを読み戻す。NACLのoutboundは戻り通信のTCP 1024–65535をCloudFrontのorigin-facing IP range（または `0.0.0.0/0`）へ許可する。CloudFront prefix listのrule weightは55なのでSG quotaにも収まることを確認する。アクセス拒否や取得失敗を「存在しない」と解釈しない。

```bash
rehearsal_aws ec2 describe-security-groups --group-ids "$rehearsal_security_group"
rehearsal_aws ec2 describe-route-tables --filters "Name=vpc-id,Values=$rehearsal_vpc"
rehearsal_aws ec2 describe-network-acls --filters "Name=association.subnet-id,Values=$rehearsal_subnet"
rehearsal_aws ssm describe-instance-information \
  --filters "Key=InstanceIds,Values=$rehearsal_instance"
```

## 2. 無効状態でURLを発行する

配置前にtemplateの検証とchange setの内容確認を行う。CloudFrontはglobal serviceだが、ここではworkloadと同じリージョンのCloudFormation stackで管理する。既存の同名stackへ上書きしない。

```bash
rehearsal_aws cloudformation validate-template \
  --template-body "file://$rehearsal_records/public-edge.yaml"
rehearsal_aws cloudformation deploy \
  --stack-name "$rehearsal_edge_stack" \
  --template-file "$rehearsal_records/public-edge.yaml" \
  --parameter-overrides \
    "DemoId=$rehearsal_id" \
    "OriginInstanceArn=arn:aws:ec2:$rehearsal_region:$rehearsal_account:instance/$rehearsal_instance" \
    "OriginPrivateDnsName=$rehearsal_private_dns" \
    "OriginSecurityGroupId=$rehearsal_security_group" \
    "CloudFrontPrefixListId=$rehearsal_prefix_list" \
    GatewayPort=18080 PublicEnabled=false \
  --tags "DemoId=$rehearsal_id" Purpose=quiz-rehearsal \
  --no-execute-changeset
```

表示されたchange set ARNを指定し、専用公開入口以外を変更しないこと、AWSのpre-deployment validationに失敗していないことを確認してから実行する。失敗原因の確認には `aws cloudformation describe-events --change-set-name '<ARN>'` を使う。CloudFront用service-linked roleの初回作成権限が必要になる場合もある。

```bash
rehearsal_changeset='<上で作成されたchange set ARN>'
rehearsal_aws cloudformation describe-change-set --change-set-name "$rehearsal_changeset"
rehearsal_aws cloudformation execute-change-set --change-set-name "$rehearsal_changeset"
rehearsal_aws cloudformation wait stack-create-complete --stack-name "$rehearsal_edge_stack"
rehearsal_aws cloudformation describe-stacks --stack-name "$rehearsal_edge_stack" \
  --output json > "$rehearsal_records/edge-stack.json"
rehearsal_url="$(jq -r '.Stacks[0].Outputs[] | select(.OutputKey == "PublicUrl") | .OutputValue' "$rehearsal_records/edge-stack.json")"
rehearsal_distribution="$(jq -r '.Stacks[0].Outputs[] | select(.OutputKey == "DistributionId") | .OutputValue' "$rehearsal_records/edge-stack.json")"
printf '%s\n' "$rehearsal_url"
```

## 3. 許可Originを設定し、アプリを起動する

SSMで専用EC2へ接続し、releaseのUID/GIDに一致する配置accountへ切り替える。実runtime fileに `REALTIME_ALLOWED_ORIGINS=["https://実distribution.cloudfront.net"]` の形式で **取得したURLだけのJSON配列** を設定する。末尾 `/` は付けない。`SESSION_COOKIE_SECURE=true` を維持する。問題集やsecretをSSM command本文、標準出力、CloudFormation parameterへ貼らない。

```bash
rehearsal_aws ssm start-session --target "$rehearsal_instance"
```

EC2の配置accountへ切り替えたら、まず `cd /opt/gs-app-tomidaisai` でrepository rootへ移動する。新規専用DBには、担当者が [人が1回行う新規DB初期化](AWS_REHEARSAL.md#人が1回行う新規db初期化) のcommandを明示的に実行する。外部の `0600` JSONから管理DB URL・app DB URL・運営資格情報を読み、[schema revision](../backend/src/quiz_app/schema_revision.py) に対応するmigration（現行 `20261015_0007`）、app role、単一設定JSONの `scoring.version` と係数・計測設定（現行version `scoring-2026-10-15-v2`）、表示policy `demo-display-name-v1`、運営account、開催設定、選択問題の登録までを順に行う。登録件数は使用する問題JSONで確認する。練習1問＋本番5問＋予備2問の構成なら8問・32選択肢になる。host準備スクリプトやアプリ起動ではDB初期化を行わない。現行schema・JSONと一致する採点policy・accountが準備済みの空の開催DBに限り、同書の `prepare` 単独手順を使う。

登録成功後、[開催登録後の参加者ID準備](AWS_REHEARSAL.md#開催登録後の参加者id準備)に従って固定ID pool・sequenceを人手で準備し、`rehearsal_app`のpool/slot `SELECT`と開催専用sequence `USAGE`を確認してから [candidate preflight](AWS_REHEARSAL.md#初回配置) を完了する。pool更新・sequence `UPDATE`/`setval`・DDL権限は付与しない。初期化や登録が途中で失敗した場合は、再実行せず担当者がDB状態を確認する。

同一commitの現行API/gateway imageを使用し、gatewayに`/etc/caddy/Caddyfile.multi-api`が含まれることを確認する。旧gateway imageへ新Composeだけを組み合わせない。AWSから読み戻したprivate IPv4を指定し、同一digestの両APIとgatewayを起動する。`REHEARSAL_PRIVATE_IPV4` は公開用bindの非秘密入力だけであり、releaseの既存keyを上書きする `AWS_DEMO_*` 環境変数はexportしない。releaseの `AWS_DEMO_GATEWAY_PORT` は公開入口の `GatewayPort` と同じ18080にする。

```bash
cd /opt/gs-app-tomidaisai
rehearsal_private_ipv4='<上で読み戻したEC2のprivate IPv4>'
rehearsal_compose() {
  env -i HOME="${HOME:?}" PATH="${PATH:?}" REHEARSAL_PRIVATE_IPV4="$rehearsal_private_ipv4" \
    docker --context '<releaseのDocker context>' compose \
      --env-file /etc/gs-app-tomidaisai/aws-demo-rehearsal-release.env \
      --file deploy/aws-demo/compose.yaml \
      --file deploy/aws-demo/compose.rehearsal.yaml \
      --file deploy/aws-demo/compose.rehearsal-public.yaml \
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
curl --fail --silent --show-error http://127.0.0.1:18080/api/health/ready
```

公開前に両APIのlive `version`がreleaseのsource commitと一致し、readyが`status=ready, database=ok, migration=current, redis=ok`であることを確認する。HTTP 200の`degraded`や片側だけの成功では公開へ進まない。

private IPv4がEC2自身のinterfaceと一致し、gatewayだけがloopbackとそのprivate IPv4へbindしていることを `ip -4 addr` とDockerのport一覧で確認する。`0.0.0.0`、public IPv4、他hostのIPへ置き換えない。API・DB・Redisをhostへpublishしない。

## 4. 公開して確認する

Macの元のbash sessionで実行する。既存stackのparameterは維持し、`PublicEnabled` だけ変更する。作成時に台帳directoryへ固定した同じtemplateを使い、稼働中にそのコピーを編集しない。

```bash
rehearsal_aws cloudformation deploy \
  --stack-name "$rehearsal_edge_stack" \
  --template-file "$rehearsal_records/public-edge.yaml" \
  --parameter-overrides PublicEnabled=true \
  --tags "DemoId=$rehearsal_id" Purpose=quiz-rehearsal
rehearsal_aws cloudfront wait distribution-deployed --id "$rehearsal_distribution"
rehearsal_aws cloudfront get-distribution --id "$rehearsal_distribution" \
  --query 'Distribution.{Status:Status,Enabled:DistributionConfig.Enabled,Domain:DomainName}'
curl --fail --silent --show-error "$rehearsal_url/api/health/ready"
```

- `/`、`/operator` がHTTPSで表示され、廃止した `/result` は404になる。HTTPからHTTPSへ移り、証明書警告がない。
- 運営ログイン、サーバーの固定リストとsequenceによる3文字ID割当、参加、回答保存が動く。参加者IDの手入力や候補取得APIを使わない。CookieはSecureで、未認証の運営API操作は拒否される。
- 認証済み参加者・運営のWebSocketが101で接続し、練習1問・本番5問の進行と集計が2画面に反映される。予備2問への続行と、未使用問題を残した手動終了も確認する。許可していないOriginからのWebSocketは拒否される。
- 別browser session間で回答・sessionが共有されない。非表示タブでは通常の状態GETが止まり、復帰時に最新状態と時刻同期を確認して回答操作が戻る。応答の `Age` やcache HITがなく、APIの404/503がSPA成功応答へ変わらない。
- `/internal` と `/internal/metrics` は404。EC2への直接internet接続はできない。問題集・backup・runtime fileは取得できない。

この確認は実AWSで行う。CloudFront設定だけで600人性能や実ブラウザー疎通を合格扱いしない。

既存frontendは末尾slashなしの正規API pathを使う。内部HTTPのため、手動でAPI pathの末尾に `/` を追加した場合のFastAPI自動redirectはHTTPのLocationを返すことがある。通常画面のAPI・Cookie・WebSocketにはこのredirectを使わない。任意URLの補正までHTTPSに統一するproxy設定は今回追加しない。

## 終了と全削除

正常終了・失敗・中断・利用期限で同じ手順を使う。これは手動撤去手順であり、期限時刻に自動実行する機能ではない。撤去担当者はCFN削除の待ち時間も確保する。本番・本番前リハーサル・本番前テスト用の専用環境では、開始前の終了時刻確認を要求しない。課金停止の時刻をAPI受付時刻で代用しない。

### 作成時に削除可能な形へそろえる

専用workload stackの作成担当者は、以下をtemplateと台帳で確認する。公開入口templateだけではDB等は削除されない。

| 専用資源 | 作成時の条件 / 終了時の扱い |
| --- | --- |
| EC2・EBS | 全専用volumeで `DeleteOnTermination: true`。別途snapshot/AMIを作らない |
| RDS | `DeletionPolicy: Delete`、`UpdateReplacePolicy: Delete`、`DeletionProtection: false`、`DeleteAutomatedBackups: true`。終了時のfinal snapshotを残さない |
| Redis | 専用replication group・user/group・subnet groupをstack所有にし、終了snapshotを作らない |
| Secrets Manager | 専用secretをstack所有、`DeletionPolicy: Delete`。共有secretとAWS管理keyを削除対象にしない |
| ECR | 専用repositoryを作る場合はstack所有、`EmptyOnDelete: true`、`DeletionPolicy: Delete`。共有repositoryは残す |
| S3 | 不要なら作らない。作る場合は専用bucketだけを台帳化し、全version・delete marker・未完了multipartを消してからstack削除 |
| CloudWatch / SSM | 専用log group・alarm・出力先を台帳化。SSMからのアプリ出力へsecretや回答本文を含めない |
| Network / IAM | 専用endpoint、ENI、IP、SG、route、IGW、VPC、instance profile/roleをstack所有にする。共有資源とservice-linked roleは対象外 |

RDSは指定なしだと削除時にsnapshotが残り得るため `DeletionPolicy: Delete` を明記する。CloudFormationのsecret削除は既定で復旧期間なしとなる。S3はstack削除前に空にする必要がある。[DeletionPolicy仕様](https://docs.aws.amazon.com/AWSCloudFormation/latest/TemplateReference/aws-attribute-deletionpolicy.html)

### 1. 公開を閉じる

正常終了なら任意の問の結果発表後に運営画面から開催を完了し、その後に公開を閉じる。予備問題を含む全8問の配信は終了条件ではない。アプリ障害時は開催完了を待たず閉じる。

```bash
rehearsal_aws cloudformation deploy \
  --stack-name "$rehearsal_edge_stack" \
  --template-file "$rehearsal_records/public-edge.yaml" \
  --parameter-overrides PublicEnabled=false \
  --tags "DemoId=$rehearsal_id" Purpose=quiz-rehearsal
rehearsal_aws cloudfront wait distribution-deployed --id "$rehearsal_distribution"
rehearsal_aws cloudfront get-distribution --id "$rehearsal_distribution" \
  --query 'Distribution.{Status:Status,Enabled:DistributionConfig.Enabled}'
```

`Deployed` / `Enabled=false` を確認する。CloudFrontの反映には時間がかかり、既存WebSocketの切断とは別なので、台帳・DemoIdを照合した専用EC2も停止する。緊急時はこのEC2停止を先に行う。停止だけで終了とせず、引き続き全削除する。

```bash
rehearsal_aws ec2 stop-instances --instance-ids "$rehearsal_instance"
rehearsal_aws ec2 wait instance-stopped --instance-ids "$rehearsal_instance"
```

公開入口の作成前や作成失敗の場合は、有効化・無効化をやり直さない。`describe-stacks` でedgeが存在する場合はその状態を記録して削除へ進む。対象stackの不存在が確認できた場合だけedge操作を省略し、専用workloadは撤去する。権限拒否はこの省略条件に含めない。手順中のcommand失敗でshellが終了した場合も、担当者は台帳から未完了の削除段階を再確認して続行する。

### 2. 削除前に台帳を確定する

次の処理は2つのstackの `DemoId` を照合し、stack ARNと全physical IDを保存する。名前のprefixだけで削除対象を選ばない。nested stackやstack外の専用資源があれば、その台帳も作成する。作成されていないstackは上記の不存在確認後に限りloopから除く。

```bash
for rehearsal_stack in "$rehearsal_edge_stack" "$rehearsal_workload_stack"; do
  rehearsal_aws cloudformation describe-stacks --stack-name "$rehearsal_stack" \
    --output json > "$rehearsal_records/$rehearsal_stack-stack.json"
  jq -e --arg demo "$rehearsal_id" '
    .Stacks | length == 1 and
    any(.[0].Tags[]; .Key == "DemoId" and .Value == $demo)
  ' "$rehearsal_records/$rehearsal_stack-stack.json"
  rehearsal_aws cloudformation list-stack-resources --stack-name "$rehearsal_stack" \
    --output json > "$rehearsal_records/$rehearsal_stack-resources.json"
done
```

台帳を作成時の記録と照合し、共有資源を含まず、上の削除条件を満たすことを確認する。台帳にはsecret本文、回答、DB dumpを含めない。

### 3. 公開入口、workloadの順で削除する

CloudFrontのdistributionとVPC originを先に削除し、CloudFront ENIが専用SG/subnetの削除を妨げない状態にする。次にworkloadを削除する。stack ARNを使い、同名で作り直された別stackへ操作しない。

```bash
rehearsal_edge_arn="$(jq -r '.Stacks[0].StackId' "$rehearsal_records/$rehearsal_edge_stack-stack.json")"
rehearsal_workload_arn="$(jq -r '.Stacks[0].StackId' "$rehearsal_records/$rehearsal_workload_stack-stack.json")"
rehearsal_aws cloudformation delete-stack --stack-name "$rehearsal_edge_arn"
rehearsal_aws cloudformation wait stack-delete-complete --stack-name "$rehearsal_edge_arn"
rehearsal_aws cloudformation delete-stack --stack-name "$rehearsal_workload_arn"
rehearsal_aws cloudformation wait stack-delete-complete --stack-name "$rehearsal_workload_arn"
```

waiterのtimeoutや `DELETE_FAILED` は完了ではない。`aws cloudformation describe-events --stack-name '<stack ARN>' --filters FailedEvents=true` で原因を確認する。削除に失敗した専用資源だけを同じ台帳で処理し、`--retain-resources` や `FORCE_DELETE_STACK` で残存を隠さない。削除処理の再試行は行えても、アプリの再起動・復旧は行わない。

### 4. サービスごとの残存を確認する

stackの削除完了だけで終了にしない。保存したphysical IDを各サービスのread APIへ渡し、次を確認して担当者と完了時刻を台帳へ記録する。タグ検索は取り漏れがあるため、補助確認として使う。

| 対象 | read API / 完了条件 |
| --- | --- |
| 公開入口 | `cloudfront get-distribution` / `get-vpc-origin` が対象IDの不存在を返す |
| EC2・EBS・IP | `ec2 describe-instances` はterminated、`describe-volumes` / `describe-addresses` で台帳のvolume/IPが不在 |
| DB・backup | `rds describe-db-instances` が対象不在、`describe-db-snapshots` / `describe-db-instance-automated-backups` に対象DBの残存なし |
| Redis | `elasticache describe-replication-groups` と `describe-snapshots` に対象group・snapshotの残存なし |
| secret・image | `secretsmanager describe-secret` で対象不在（削除は非同期）、専用ECR repositoryが不在 |
| S3・ログ | 専用bucketが不在、専用log group/alarmが不在。versionやmultipartを残さない |
| network | 台帳のVPC/subnet/endpoint/SG/IPが不在。共有VPCの場合は専用ENI・SG・endpointだけを確認 |

権限拒否、接続失敗、取得timeoutを不存在として扱わない。RDSの手動snapshot、残ったEBS、secret、S3 objectなどが見つかった場合は作成時台帳と所有tagを再照合し、そのexact IDだけを削除して再確認する。対象の分からない資源を一括削除しない。

EC2上のruntime、問題コピー、Fernet backupは、その専用EBSが削除されたことをもって回収完了とする。Macのrepository内問題集はユーザーの原本として保持する。追加で作ったDB dump・回答・secretのローカルコピーは残さず、非秘密の検証結果と削除台帳だけを保持する。費用画面の反映は遅れるため、サービス別の不存在確認と後日の請求確認を区別する。

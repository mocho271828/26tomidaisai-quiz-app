# GitHub ActionsからAWS確認環境を更新する

このworkflowで扱う本番・本番前リハーサル・本番前テスト用の専用環境では、予算・撤去時間の事前確認・再確認は不要とする。

委員に公開URLを共有し、問題や設定を確認してもらうための専用環境である。手動配置の`public_entrypoint`で`custom-domain`（`AWS_REVIEW_DOMAIN_NAME`に登録したホスト）または`cloudfront`（今回発行される標準URL）を選ぶ。入口はどちらもCloudFrontのまま、private EC2へVPC originで接続する。JSON編集をmainへ反映した後、所有者がGitHub Actionsを手動実行する。`deployment_mode=update`（既定）は既存AWS環境を更新し、`create`は新しい専用環境を作る。**どちらも専用DBをresetし、現在の問題・設定から新Eventを初期化する。** AWS構成・URL・運営パスワードを保ち、同じEvent UUID・問題・設定・ID sequenceのまま進行dataだけを戻す場合に限り、独立したAWS Review Reset Eventを使う。

実際のACM検証tokenとCloudFront標準domainは、作成後に取得する。

同じEC2上の`api`・`api-secondary`へ同じdigestのAPI imageを配置し、CaddyでHTTP/WebSocketを分散する。DB/Redis・session secret・backup鍵/保存先・UID/GIDを共有し、instance IDだけを分ける。EC2はCPU credit制限のないc7i.xlargeを既定とし、1台で稼働する。

| 操作 | Workflow | 実施内容 |
| --- | --- | --- |
| push / pull request | [AWS Review Deploy](../.github/workflows/aws-review-deploy.yml) | 問題・設定JSONのオフライン検証だけ |
| 手動配置 | AWS Review Deploy | JSON検証後、新規createまたは既存updateでDBを全初期化し、アプリを配置 |
| 手動Event確認 | [AWS Review Event Status](../.github/workflows/aws-review-event-status.yml) | 現在のEvent ID・version・状態を読み取り、Summaryへ表示 |
| 手動Event初期化 | [AWS Review Reset Event](../.github/workflows/aws-review-reset-event.yml) | 稼働sourceと期待Event UUID/版を照合し、暗号化archiveを検証後、参加者・回答・得点・進行を初期化 |
| 手動撤去 | [AWS Review Destroy](../.github/workflows/aws-review-destroy.yml) | 専用環境、データ、バックアップを全削除 |

自動実行の入口は`validation.github_json_check`だけとする。lint・テスト・AWS操作・DB操作をpushやpull requestに接続しない。定期実行、自動配置、自動初期化、自動撤去のscheduleは設けない。

## 初回設定

以下はAWS管理者とリポジトリ所有者が初回に設定する。例示の `quiz.example.org` は、今回使う独自ドメインに読み替える。workflowを追加するだけでは、IAM roleやGitHub Environmentは作成されない。

1. 対象AWS accountにGitHubのOIDC providerを準備する。provider URLは`https://token.actions.githubusercontent.com`、audienceは`sts.amazonaws.com`とする。既存providerを利用する場合はそのARNを確認する。
2. [OIDC/IAM template](../infra/aws-review/github-oidc.yaml)を適用し、`GitHubOidcProviderArn`にprovider ARN、`GitHubSubject`にこのリポジトリの`aws-review` Environmentへ発行される正確なsubject、`DemoId`に以後使う`AWS_REVIEW_ID`を指定する。`custom-domain`を使う場合は後述の[証明書とDNSの準備](#証明書とdnsの準備)で同じaccountの`us-east-1`に対象domainのACM証明書を発行し、`ReviewCertificateArn`へ指定する。`cloudfront`だけを使う場合、この外部証明書の準備は不要。通常のDeploy workflowはbootstrap IAMを更新しない。
3. GitHubのSettings → Environmentsで`aws-review`を作成し、Deployment branches and tagsを**main branchだけ**に制限する。tagや他branchは許可しない。
4. 下表のEnvironment secretsと必須Environment variableを登録する。templateのoutput `DeployRoleArn`を`AWS_DEPLOY_ROLE_ARN`、`DestroyRoleArn`を`AWS_DESTROY_ROLE_ARN`へ設定する。配置roleと撤去roleは分離し、それぞれの`MaxSessionDuration`を10,800秒以上にする。
5. workflow・CLI・infraをmainへ反映する。`workflow_dispatch`の登録にはworkflowファイルがdefault branch上に存在する必要がある。実行時もmainを選ぶ。

OIDCの標準subjectがEnvironment形式の場合、値は`repo:<owner>/<repository>:environment:aws-review`となる。実際に発行される形式はGitHub側のsubject設定に合わせ、immutableな識別子を含む形式やcustom subjectを使用している場合はその完全一致値を渡す。subjectをワイルドカードへ緩めて接続しない。Environment形式のsubjectにはbranchが含まれないため、main限定のEnvironment設定が必要である。[GitHubのAWS OIDC設定](https://docs.github.com/en/actions/how-tos/secure-your-work/security-harden-deployments/oidc-in-aws)、[手動実行の登録条件](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax#onworkflow_dispatch)

| Environment secret | 値 |
| --- | --- |
| `AWS_ACCOUNT_ID` | 配置先の12桁AWS account ID |
| `AWS_REVIEW_ID` | 環境識別子。最大21文字、形式`[a-z0-9]+(-[a-z0-9]+)*`。IAMの`DemoId`と一致させる |
| `AWS_DEPLOY_ROLE_ARN` | OIDCで引き受ける配置roleのARN |
| `AWS_DESTROY_ROLE_ARN` | OIDCで引き受ける撤去roleのARN |
| `AWS_APP_AZ` | `ap-northeast-1a`形式の東京リージョンAZ名。CloudFront VPC origin対応AZを選ぶ。AZ IDではない |
| `AWS_SECONDARY_AZ` | `AWS_APP_AZ`とは異なる、`ap-northeast-1c`形式の東京リージョンAZ名 |
| `AWS_UBUNTU_AMI_ID` | `ami-`＋小文字16進数17桁。配置対象として確認した東京リージョンのUbuntu AMI ID |
| `AWS_DATABASE_ENGINE_VERSION` | `16.6`など`16.<数字>`形式。対象accountで利用可能なRDS PostgreSQL 16のversion |
| `AWS_REVIEW_CERTIFICATE_ARN` | `custom-domain`で必須。同じaccountの`us-east-1`にある発行済み対象domain証明書のARN。bootstrapの`ReviewCertificateArn`と一致させる |

既存AWS設定8項目と、`custom-domain`で使用する証明書ARNはSettings → Environments → `aws-review` → **Environment secrets**に登録する。workflowは`${{ secrets.AWS_ACCOUNT_ID }}`のように参照し、Variablesへのfallbackは設けない。Secretsに登録済みなら再登録は不要。`cloudfront`では外部証明書を使用せず、登録済み証明書Secretの削除も不要。

| Environment variable | 値 |
| --- | --- |
| `AWS_REVIEW_DOMAIN_NAME` | `custom-domain`で必須。管理するサブドメイン（例 `quiz.example.org`）（scheme・path・末尾dotなし） |

`custom-domain`のdomainは`${{ vars.AWS_REVIEW_DOMAIN_NAME }}`から参照する。`cloudfront`ではこの独自domainをAliasやruntime originに使用しない。参加者画面の任意設定`VITE_*`も、後述のEnvironment variablesを使う。

`AWS_DEPLOY_ROLE_ARN`には`DeployRoleArn`出力の値全体、`AWS_DESTROY_ROLE_ARN`には`DestroyRoleArn`出力の値全体をコピーする。形式は`arn:aws:iam::<12桁のaccount ID>:role/<role名>`で、roleにpathがあればそれも含める。IAMロール詳細画面のARNを使う場合も、同じCloudFormationで作成した配置用・撤去用roleを選ぶ。`AWS_ACCOUNT_ID`には同じaccountの半角数字12桁だけを登録する。`user/`を含むIAMユーザーのARNは、AdministratorAccessが付いていても利用できない。実在する専用roleのARNを取得する。

workflowはAWS認証前にaccount IDとIAM role ARNの形式・account一致を検証する。値の自動補正やARNの組み立ては行わず、不正な場合はSecret名と確認方法だけを表示する。この検証はroleの実在やOIDC trustの確認を代替しない。[IAM ARNの形式](https://docs.aws.amazon.com/IAM/latest/UserGuide/reference_identifiers.html)、[role名とpathの制約](https://docs.aws.amazon.com/IAM/latest/APIReference/API_CreateRole.html)

EC2・RDS・Redisなどの配置リージョンは`ap-northeast-1`固定であり、CloudFront用ACM証明書の照会だけ`us-east-1`を使う。AMI・AZ・DB versionは実際のaccountで利用できる組合せを設定する。CloudFront VPC originsに非対応のAZ ID `apne1-az3`はアプリ配置先に使わない。AZ名の末尾文字だけで判定しない。[CloudFront VPC originsの要件](https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/private-content-vpc-origins.html)

`AWS_REVIEW_ID`はこのworkflow専用として初回に決め、以後の配置・撤去でも同じ値を使う。過去の負荷試験・期限付きリハーサルや共有環境のIDを流用しない。既存環境にはupdate、撤去済み・未作成環境にはcreateを明示する。逆の組み合わせはAWS変更前に拒否する。

AWSの長期access keyをGitHub Secretsへ保存しない。通常jobの権限は`contents: read`のみで、OIDC発行権限`id-token: write`は配置・Event確認・Event初期化・撤去jobだけに付与する。Event確認とEvent初期化は既存の`AWS_DEPLOY_ROLE_ARN`を使い、IAM権限を追加しない。配置・撤去のsessionは10,800秒、job timeoutは180分である。IAM roleの最大session時間が短い場合は認証時に失敗する。[IAM roleの最大session時間](https://docs.aws.amazon.com/IAM/latest/UserGuide/id_roles_update-role-settings.html#id_roles_update-session-duration)

Settings → Secrets and variables → Actions の **Repository variables** に、対象リポジトリ（`owner/repository`）を `AWS_REVIEW_REPOSITORY`、手動実行を担当するGitHubユーザー名を `AWS_REVIEW_OPERATOR` として登録する。AWS認証前にリポジトリ・mainブランチ・初回と再実行の両方の実行者を照合する。値が空、または一致しない場合は先へ進まない。

### 参加者画面の任意設定

同じ`aws-review`のEnvironment variablesへ、必要な項目だけ登録する。Secretsへの登録やローカル`.env`のアップロードは不要。

| Environment variable | 値 | 未登録・空欄時 |
| --- | --- | --- |
| `VITE_EVENT_START_AT` | offset付きISO日時（例: `2026-09-30T18:00:00+09:00`） | 開始日時を使った残り時間案内を表示しない |
| `VITE_SURVEY_URL` | 来場者アンケートの外部HTTPS URL | アンケートリンクを表示しない |
| `VITE_HOME_URL` | 終了後に戻す外部サイトのHTTPS URL | ホームリンクを表示しない |

アンケートを案内する年は、その年度のURLをEnvironment variableに設定してから配布用にビルドする。

終了後のリンクにはユーザー名・パスワードを含まない外部HTTPS URLを使う。日時・URLはAWS資源の操作前にPydanticで検証し、gatewayのDocker build引数へ渡す。API imageとbackend runtime envへは渡さない。

これらの値は公開されるフロントのJavaScriptに埋め込まれるため、秘密情報を設定しない。値の変更後はmainからAWS Review Deployを手動実行して再build・再配置する。**updateでもDB全体を初期化する。** Variablesの編集やコンテナ再起動だけでは反映されない。

## 証明書とDNSの準備

この節は`public_entrypoint=custom-domain`を使う場合だけ必要である。`cloudfront`はCloudFront既定証明書と標準URLを使い、XserverやACM検証CNAME・配信用CNAMEを操作しない。

AWS管理者とDNS管理者が一度準備する。証明書は短命なreview stackの外で保持し、Deploy/Destroy roleには発行・更新・削除権限を追加しない。CloudFrontで使用するACM証明書は、配置先と同じaccountの`us-east-1`に必要である。[CloudFrontの証明書要件](https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/cnames-and-https-requirements.html)

1. AWS管理者がログイン先accountを確認し、未発行なら次を実行する。発行要求を重複作成せず、返されたARNを控える。

   ```sh
   aws sts get-caller-identity --query Account --output text
   aws acm request-certificate \
     --region us-east-1 \
     --domain-name quiz.example.org \
     --validation-method DNS \
     --key-algorithm RSA_2048 \
     --options Export=DISABLED \
     --query CertificateArn --output text
   ```

   通常の公開証明書をDNSで検証し、秘密鍵はexportしない。[発行要求のCLI仕様](https://docs.aws.amazon.com/cli/latest/reference/acm/request-certificate.html)

2. 返された実際のARNを手元の`AWS_REVIEW_CERTIFICATE_ARN`へ設定し、次で検証用DNS値と発行状態を取得する。作成直後にDNS値が未反映なら少し待って再確認する。ARNやtokenを推測で作らない。

   ```sh
   aws acm describe-certificate \
     --region us-east-1 \
     --certificate-arn "${AWS_REVIEW_CERTIFICATE_ARN:?取得した証明書ARNを設定してください}" \
     --query 'Certificate.{Status:Status,Names:SubjectAlternativeNames,NotBefore:NotBefore,NotAfter:NotAfter,DNS:DomainValidationOptions[].ResourceRecord}' \
     --output json
   ```

3. Xserverへ**証明書検証用**CNAMEを登録する。名前と値は上の`DNS`に返った`Name`・`Value`をそのまま使う。Xserver画面がzone名を自動付加する入力形式なら重複させず、保存後の完全なレコード名を確認する。このCNAMEは証明書発行後も残す。再度`describe-certificate`で`ISSUED`、対象domain、有効期間を確認する。[ACMのDNS検証](https://docs.aws.amazon.com/acm/latest/userguide/dns-validation.html)
4. IAM bootstrapの`ReviewCertificateArn`とGitHub Environment secret `AWS_REVIEW_CERTIFICATE_ARN`へ同じARN、Environment variable `AWS_REVIEW_DOMAIN_NAME`へ`quiz.example.org`を設定する。bootstrap更新でDeployRoleへ当該ARNの`acm:DescribeCertificate`だけを付与する。DestroyRoleには追加しない。
5. mainからDeployを実行する。新規はcreate、既存はupdateを選び、どちらも回答受付前に行う。完了summaryまたはedge output `CloudFrontDomainName`から、実際のCloudFront標準domainを取得する。
6. Xserverの`quiz.example.org A <旧IPv4>`を**配信用**CNAMEへ置き換える。同名にAとCNAMEを併存させない。値は手順5のdomainだけとし、`https://`やpathを付けない。

| 用途 | DNS名の形式 | CNAME値の取得元 | 保持・更新 |
| --- | --- | --- | --- |
| 証明書検証 | `_<name-token>.quiz.example.org` | ACMの`ResourceRecord.Value`。形式は`_<value-token>.acm-validations.aws` | 証明書を使う間は保持 |
| サイト配信 | `quiz.example.org` | 配置summaryの`cloudfront_domain_name`。形式は`d….cloudfront.net` | CloudFront再作成時に更新 |

上表のtokenと`d…`は形式の説明であり、実際に登録する値ではない。証明書の検証CNAMEとサイト配信CNAMEを取り違えない。CloudFront標準domainは公開入口へDNSを向けるための値で、予約EIPへ紐付ける値ではない。[CloudFrontの独自domain設定](https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/CreatingCNAME.html)

DNS反映後に次を確認し、TLS検証を無効化しない。

```sh
dig +short CNAME quiz.example.org
curl --fail --silent --show-error https://quiz.example.org/api/health/ready
```

CNAMEが今回のCloudFront標準domainと一致し、HTTPSが正常であることを確認する。続いて独自URLから参加・運営login・問題表示・回答・本人結果・WSS接続を実機確認する。以前のCloudFront標準URLのCookieは独自domainへ引き継がれないため、利用するURLをそろえて参加/loginし直す。DNSの旧Aレコードのキャッシュが残る場合は、設定値だけで切替完了と判断しない。

## アプリを配置・更新する

AWSにログインし、CloudWatch Dashboard `gs-rehearsal-<環境ID>-health` を開く。EC2・RDS・Redis・CloudFrontの標準メトリクスと、`GSQuiz/SystemHealth` のAPI別live/ready・HTTPリクエスト率を確認できる。collectorはhostから両APIを観測する。初回や片API欠測、counterリセットなどでリクエスト率が欠測した区間は0件と扱わない。監視終了後もAWS資源・Dashboardは残るため、利用終了時は撤去手順に従う。

手動workflowの`public_entrypoint`を次の2値から選ぶ。失敗時に自動で別の入口へ切り替えない。

| `public_entrypoint` | edge設定・公開URL | 準備と確認 |
| --- | --- | --- |
| `custom-domain`（既定） | Aliasと外部ACM証明書を関連付け、`https://quiz.example.org`を使用 | 資源作成前に証明書ARNのaccount・`us-east-1`・`ISSUED`・SAN・有効期間を検証。DNSとHTTPS/WSSを確認する |
| `cloudfront` | 既存templateへdomain/certificate両方空欄を渡し、Aliasなし・CloudFront既定証明書で発行された標準URLを使用 | 外部ACM証明書と独自DNSの操作は不要。発行された標準URLのHTTPS/WSSを確認する |

どちらもedgeの`PublicUrl`をruntimeの公開origin・WebSocket許可originへ使う。参加者・運営・両PCのRunnerも同じURLへ統一する。domainと外部証明書の片側だけを設定することや、任意の独自domainは許可しない。証明書の自動発行・秘密鍵取得・DNS変更はworkflowに含めない。

workflowは選択値を`AWS_REVIEW_PUBLIC_ENTRYPOINT`としてCLIへ渡す。CLIの既定も`custom-domain`であり、不明な値を拒否する。`cloudfront`では登録済みのdomain・証明書値も空欄へ正規化し、Alias・外部ACM設定へ流用しない。入口を切り替えるためにEnvironmentの既存設定を削除する必要はない。

配置中はCloudFrontを無効にし、EC2上の所有確認・DB初期化・既存preflight・コンテナ起動が成功した後に有効にする。`ssm_wait` で失敗した場合に、コンソールからCloudFrontだけを有効化して回避しない。修正をmainへ反映した後、**Run workflow → main**で新しく実行する。旧実行の **Re-run jobs** は修正前のcommitを使う。

SSMの終端失敗時は、EC2側が出した`review_host_failed`の安全な診断をActionsにも表示する。既知の例外型・source位置・許可済みAWSエラーだけを再検証して表示し、生の子process出力や秘密値を転送しない。診断が欠測・破損している場合も配置失敗として扱う。`CalledProcessError`だけの場合は、所有確認済みEC2で該当する読み取り専用チェックを再実行して原因を絞る。

create・updateの初期化は両API停止 → DB reset → 既存migration → 現在JSONから新開催作成 → 参加者ID pool/slot/sequence準備・権限付与 → read-only preflight → 両API起動の順で行う。ID準備は既存SQL生成器を使い、管理roleだけが書き込む。preflightやAPI起動時には作成・修復しない。起動後はgatewayから両serviceへ個別にlive/readyを照会し、source commit・DB・migration・Redisが一致してから公開する。片方だけ正常でも成功にしない。

本番・リハーサルの登録対象は、配置する `content/event-questions.json` で確認する。練習1問・本番5問・予備2問の構成では、予備を含む全8問を登録し、Runnerでは通常6問なら`EXPECTED_ROUNDS=6`、予備もすべて実施するなら`EXPECTED_ROUNDS=8`を起動前に明示する。専用の切替機能は追加しない。ファイル保存だけでは配置済みDBは変わらないが、次のDeploy create/updateでは現在の問題・設定から新開催を作る。手動Event初期化は保存済み問題・設定を維持する。

1. [問題JSON](../content/event-questions.json)で問題文・選択肢・正解・出題順を編集する。書式は[問題設定手順](QUESTION_CONFIGURATION.md)に従う。
2. [クイズ設定JSON](../backend/src/quiz_app/quiz_configuration/quiz-settings.json)で対応する進行・採点・表示項目を編集する。設定項目とversion変更の条件は[クイズ設定手順](QUIZ_CONFIGURATION.md)に従う。
3. 変更をmainへ反映し、自動JSON検証が成功したことを確認する。
4. GitHubのActions → **AWS Review Deploy** → **Run workflow**でmain、`deployment_mode`、`app_instance_type=c7i.xlarge`、`public_entrypoint`を選ぶ。予算・撤去時間・監視終了日時の入力や確認は不要。create・updateとも配置CLI開始から12時間後を監視終了日時として自動設定し、updateでも今回の期限へ更新する。監視収集の停止だけを制御し、AWS資源の自動撤去は行わない。updateも既存開催dataを削除して現在の問題・設定を新開催へ投入するため、必要な記録を配置前に別途保存する。
5. 完了後のjob summaryの公開URLと`cloudfront_domain_name`を確認する。`custom-domain`は配信用CNAMEを設定・確認し、`cloudfront`は発行された標準URLをそのまま使う。summaryの成功はAWS配置完了であり、公開URLのHTTPS/WSS・参加・回答を確認してから委員へ共有する。参加者画面はURLのルート、運営画面は`/operator`を使う。公開画面は廃止済みで、`/result`は404になる。

回答の選択期限は10秒、現行設定の到着猶予は4秒（開始14秒が終端）であり、開始前待機秒とは異なる。補助回答は廃止し、集計後は運営の公開操作を待つ。現行revisionは `20261015_0007`。時間設定は[クイズ設定](QUIZ_CONFIGURATION.md#設定項目)を参照する。

手動配置は次のCLIを実行する。GitHub runnerはUbuntu 24.04、Python 3.12、uv 0.11.28を使い、`uv sync --frozen --no-dev --project backend`でlock済み依存を取得する。

```sh
uv run --no-sync --project backend python -m validation.aws_review deploy
```

**updateは既存開催・参加者・回答・得点・進行を削除し、現在の問題JSON・採点設定から新Eventを作る。** 委員の回答受付中に配置を開始せず、必要な記録は事前に保存する。実行対象は`AWS_REVIEW_ID`で識別した専用環境に限定し、共有資源・本番環境を対象にしない。

create・updateの双方が従来のDB初期化・既存migration適用・初期データとID割当準備を行う。所有確認後にSecrets Managerの既存値から同じ資格情報を導出し、サービス停止後に専用RDSのpublic schemaとapplication roleだけをresetする。AWS構成・公開URL・資格情報・既存Round backupは保持する。廃止したDB保持updateが作成したcandidate/previous runtime・releaseの固定4ファイルは削除し、旧資格情報付きrollback設定は残さない。配置先は引き続き既存のaccount・環境ID・所有証明で限定し、任意の本番DBや他環境への操作へ拡張しない。

運営ユーザー名は`rehearsal-operator`。パスワードはEC2の`/etc/gs-app-tomidaisai/review-operator-password`にpermission `0600`で保存する。担当者がSession Managerの対話セッションで取得し、Run Commandの出力、GitHubのログ・summary・artifactやrepositoryへ転記しない。共有する参加者URLへ運営credentialを含めない。

## GitHub ActionsでEvent ID・versionを確認する

**AWS Review Event Status**は現在のEventを読み取り、Resetへ入力する値をGitHubのSummaryに表示する。運営画面へのログイン、Event ID、versionの事前入力は不要である。

1. workflowをmainへ反映した後、GitHubのActions → **AWS Review Event Status** → **Run workflow**でmainを選んで実行する。
2. 成功した実行の**Summary**を開き、`expected_event_id`と`expected_event_version`をコピーする。`event_state`（Eventの状態）、`observed_at`（hostのUTC観測時刻）、`source_commit`（配置済みcommit）も表示する。
3. [AWS Review Reset Event](#aws構成を維持してeventを最初からやり直す)へ同名の2項目を入力し、確認文`RESET EVENT`を指定する。確認後に進行操作などでversionが変わった場合は、Event Statusを新規実行して値を取り直す。古い値を推測で書き換えない。

確認だけならアプリの再Deployは不要であり、初期化対応を配置済みの既存hostを使える。SSMで固定の読み取り処理を実行し、既存のrelease・runtime・配置sourceとAWS所有対象を照合してから、application role・TLSのREAD ONLY transactionでEventを取得する。読み取りに運営パスワードを使わず、資格情報・接続先・参加者・問題・回答はSummaryへ出さない。DB更新、サービス停止、source/image更新、初期化、監視期限の延長は行わない。

Event Statusが確認するのは観測時点の値であり、実行後の進行をロックしない。確認workflowのmainと配置済みcommitが異なっていても確認できるが、**Resetは実行mainと配置済みcommitの一致が必要**である。不一致の場合、Deploy update自体がDBを全初期化して新Eventを作るため、古いEvent ID・versionでResetへ進まない。現在のmainへupdateした後に、さらに同じEventをDML初期化する必要がある場合だけEvent Statusを取り直す。確認に成功しただけでReset可能・初期化済み・公開疎通正常とは扱わない。

## AWS構成を維持してEventを最初からやり直す

この手動workflowは、既存の専用`aws-review`に限って実行する。所有者が対象と削除範囲を確認して実行する。

| 対象 | 初期化後 |
| --- | --- |
| AWS構成・公開URL・稼働image | 維持する。CloudFront・EC2・RDS・Redisを作り直さない |
| 運営account・パスワード・session secret・backup鍵 | 維持する。運営sessionは削除するため再ログインが必要 |
| Event UUID・問題・選択肢・開催設定snapshotの静的設定・採点policy | 維持する。現在JSONから問題や設定を投入し直さない |
| 参加者・参加者session・回答・得点・集計・順位 | 削除する。参加者は同じURLから参加し直す |
| Round履歴・冪等性記録・旧監査 | 削除する。Event.versionを増やし、最初の既存問題を新UUIDのRoundとして準備する |
| 進行・結果表示 | `LobbyOpen`・`PreparingAssets`へ戻し、3項目の表示を保存済み設定の初期値へ戻す。削除するRoundへの最終問題指定を解除する |
| ID pool・slot・sequence | 維持する。再参加は残りのID枠から割り当て、旧IDは再利用しない |
| 既存Round backup | 保持する。削除前archiveは別形式・別保存先へ追加する |
| 監視期限・機種・課金設定 | 維持する。初期化で期限を延長せず、課金停止も行わない |

1. Resetは稼働sourceと実行mainのcommitが一致する場合だけ実行でき、source更新やアプリの再buildを代行しない。不一致ならDeploy updateがDBを全初期化するため、旧開催を保持する目的でupdateしてはいけない。同一commitの配置済み環境で、Event UUID・問題・設定・ID sequenceを維持したDML初期化が必要な場合だけ次へ進む。
2. 各PCのBrowser Runnerを停止し、運営操作と参加・回答を終える。[AWS Review Event Status](#github-actionsでevent-idversionを確認する)を実行し、Summaryの`expected_event_id`と`expected_event_version`を控える。取得後に進行が変わった場合は確認workflowを新規実行する。既存の認証済み`/api/v1/operator/state`でも直下の`event_id`と`event_version`を確認できるが、Cookie・CSRF・パスワードや応答全文は共有しない。
3. GitHubのActions → **AWS Review Reset Event** → **Run workflow**でmainを選び、次の3項目を入力する。

| 入力 | 値 |
| --- | --- |
| `expected_event_id` | 手順2で確認した既存Event UUID |
| `expected_event_version` | 同じSummaryの`expected_event_version`。Roundのversionではない |
| `confirmation` | `RESET EVENT`（半角大文字・半角空白、完全一致） |

workflowは所有確認とhost lockの下でgateway・両APIを停止し、application DB接続が残っていないことを確認する。削除直前の全アプリtableを暗号化archiveへ保存し、復号した内容・件数を照合してから、期待UUID/版を再検証する1transactionで対象データを初期化する。schema、migration、権限、ID sequenceは変更しない。read-only preflightと両APIのlive/ready確認まで成功した場合に再開成功となる。

成功後は同じ公開URLを開き直す。運営は同じパスワードでログインし、参加者数0・最初の問題の準備状態を確認する。参加者は再参加して新しいIDを受け取る。ID sequenceは消費位置を維持するため、初期化の繰返しで残り枠は減り、枯渇した場合は自動修復しない。HTTPS/WSS・再ログイン・再参加の実機確認はworkflow成功とは別に行う。

**初期化後に元の開催へ戻すボタンや自動復元機能はない。** 削除前archiveは`quiz-event-reset-archive-v1`形式で、全23アプリtableと参加者ID sequenceの`last_value`を保全する。既存backup鍵で暗号化し、EC2の`/var/lib/quiz-rehearsal-backups/event-reset-<reset UUID hex>/archive.fernet`へ非上書き保存する。directoryは0700、fileは0600とし、平文は永続化しない。既存Round backupのbind先`/var/lib/quiz-rehearsal-backups/data`から分離し、既存artifactを保持する。

SSMの成功結果にはreset UUID・archive directory等の非秘密情報だけを出し、archive本体と鍵をGitHub artifactやログへ転記しない。復号した全内容とchecksumの一致は、別DBへの復元検証成功ではない。既存Round backupを全DB復元用として流用せず、手動復旧の設計・承認・検証は別作業とする。archiveは同じEC2のEBS上にあり、AWS Review DestroyでEBSとともに削除される。

停止後にarchive・DB・preflight・起動確認が失敗した場合は、サービスを停止したままにする。自動再reset・自動rollback・失敗時の自動再起動は行わない。job失敗やキャンセルだけではDBが変更前か変更後かを断定できないため、所有者が最新DB状態とarchive、SSM/Actionsの固定診断を照合する。古い期待版の**Re-run jobs**や確認値の推測で進めず、状態確認と必要な修復後に操作を判断する。初期化未実施・結果不明・初期化済みだが再開失敗を区別する。

## 確認が終わったら撤去する

GitHubのActions → **AWS Review Destroy** → **Run workflow**でmainを選んで実行する。最新JSONの検証結果に依存せず撤去できる。

```sh
uv run --no-sync --project backend python -m validation.aws_review destroy
```

撤去は、同じ`AWS_REVIEW_ID`の専用AWS資源に加え、確認用データとバックアップを削除する。復元用データは残さない。各serviceの削除確認後にCLIが`status=deleted`を返し、job summaryへ結果を記録する。削除APIの受付だけで撤去成功とは扱わない。

外部管理のACM証明書、ACM検証用CNAME、OIDC/IAM bootstrap、予約EIP `<旧IPv4>`は、この撤去の対象に含めない。Destroyはdomain・証明書設定や証明書の有効性へ依存せず実行できる。`custom-domain`で今回のCloudFrontへ向けた配信用CNAMEは削除後にXserverで解除し、再作成時には新しい`cloudfront_domain_name`へ設定し直す。`cloudfront`では独自DNSを操作しない。ACM検証用CNAMEは保持する。証明書の自動更新にはAWSサービスで使用中であることも必要なため、長期間未使用なら次回配置前に期限と状態を再確認する。[ACMのDNS検証と更新](https://docs.aws.amazon.com/acm/latest/userguide/dns-renewal-validation.html)

予約EIPの解放は別途、所有者・AllocationId・関連付け・他用途がないことを確認して判断する。この環境のNAT Gateway用EIPとは区別し、DNS切替前や旧Aレコードがキャッシュに残る間に解放しない。Destroy成功をアカウント全体の課金停止と解釈しない。

配置・Event確認・Event初期化・撤去は共通concurrency group `aws-review`で直列化し、進行中jobは新規要求で中断しない。GitHubのconcurrencyでは待機中の実行が新しい要求に置き換わる場合があるため、連打せずActions上の実行結果を確認する。[GitHubのconcurrency仕様](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/control-workflow-concurrency)

確認環境ではcontrol templateへ`EnableDeadline=false`を渡し、既定の時刻による撤去scheduleを作成しない。従来リハーサル用template自体の既定値`true`は維持する。workflowの失敗・手動キャンセル・180分timeoutは自動撤去を意味しない。資源が残っている場合はAWS Review Destroyを実行し、削除結果を確認する。確認環境は撤去完了まで稼働・課金が続く。

## Job詳細画面の進捗

配置jobの「専用環境を作成またはDBを初期化して更新」を開くと、従来の`phase`に加え、対象stack・工程・主要リソースの論理名と状態を表示する。たとえば`AppInstance`はアプリ実行用EC2、`Database`はPostgreSQL、`Redis`はRedis、`Distribution`はCloudFront配信を表す。EC2内の2 APIは同じ`AppInstance`で動く。表示例は次のとおり（実行記録ではない）。

```json
{"status":"running","phase":"stack_wait","stack":"workload","step":"stack","resource":"Database","state":"CREATE_IN_PROGRESS","resource_type":"AWS::RDS::DBInstance","message":"PostgreSQLデータベース (Database): AWSリソースの作成・更新 / CREATE_IN_PROGRESS"}
```

変更計画の準備、stack作成、EC2のSSM接続待ち、EC2での配置実行を区別する。状態が変わった項目だけ出力するため、表示が止まっている時間も待機は継続している。物理ID・ARN・認証情報・問題本文・AWSの生のエラー理由は表示しない。

詳細取得に失敗した場合は「詳細進捗取得不可（通常の完了判定を継続）」を一度表示し、そのstack操作の追加詳細取得を停止する。通常のstack状態・所有確認・SSM結果による成功/失敗判定は継続し、表示用APIの失敗だけで配置や撤去を中断しない。

stack/SSMの既存応答を再利用し、リソース詳細だけ`ListStackResources`で最大60秒に1巡回取得する。現行templateは各1ページで、配置1回の4つのstack操作をすべて45分上限まで待った場合でも追加は最大180巡回（SDKの有限retryを除く）。実際には完了時点で止まる。SSMの追加呼出し・監視用AWS資源・IAM拡張・定期実行はない。AWS標準リソースのCloudFormation利用に追加料金はなく、表示処理は既存jobの待機中に行うため、表示目的で大きくAPI利用量や計算量を増やさない。アカウント全体の無料枠残量はこの変更では照会しない。[CloudFormation料金](https://aws.amazon.com/cloudformation/pricing/)、[ListStackResources仕様](https://docs.aws.amazon.com/boto3/latest/reference/services/cloudformation/client/list_stack_resources.html)

## 初回・失敗時の確認

- Run workflowが表示されない場合は、workflowがdefault branchへ反映され、Actionsが有効になっているか確認する。
- Event Statusが失敗した場合はSummaryと固定診断を確認する。所有対象・release/runtime・Event件数の不一致を自動修復しない。mainと配置済みsourceが異なるだけなら確認は可能だが、Deploy updateはDBを全初期化するため、旧EventのReset準備として実行しない。
- 実行者確認で失敗した場合は、Repository variablesに設定した担当者が対象リポジトリのmainを選んで実行する。他人の実行の再実行でも、元の実行者の条件は省略しない。
- 必須設定の確認で失敗した場合は、`aws-review`のEnvironment secretsに該当名があるか確認する。SecretsとVariablesは別の保存先であり、`${{ vars.AWS_ACCOUNT_ID }}`では同名のSecretを取得できない。旧workflowがVariablesを参照している場合は、修正版をmainへ反映した後、**Run workflow**で新規実行する。旧実行の**Re-run jobs**では旧commitのworkflowを使う。[GitHubのSecrets参照](https://docs.github.com/en/actions/how-tos/write-workflows/choose-what-workflows-do/use-secrets)、[再実行の仕様](https://docs.github.com/en/actions/how-tos/manage-workflow-runs/re-run-workflows-and-jobs)
- Node.js 20の非推奨警告はAction自体のruntimeに関するもので、`AWS_ACCOUNT_ID`の未設定とは別の問題である。Node.js 24対応版のActionへ更新し、完全commit SHAで固定する。`setup-node`の追加ではActionのruntimeは変わらない。[GitHubのNode.js 20廃止案内](https://github.blog/changelog/2025-09-19-deprecation-of-node-20-on-github-actions-runners/)
- `Request ARN is invalid`の場合は、Environment secretsの`AWS_DEPLOY_ROLE_ARN`（撤去では`AWS_DESTROY_ROLE_ARN`）を、上記のrole ARN全体で更新する。role名だけ、IAM user・OIDC provider・STS assumed-roleのARN、コンソールのURLは使わない。引用符、`AWS_DEPLOY_ROLE_ARN=`などの代入文、埋め込まれた空白や改行がないか確認する。Secretsの値はGitHubから読み戻せないため、元の出力から再コピーする。ARNの形式を直しても失敗する場合に、次項のOIDC設定を確認する。
- OIDC接続に失敗した場合は、role ARN、provider、audience、subjectの完全一致、Environmentのmain限定、roleのsession上限を確認する。AWS key追加やtrust条件のワイルドカード化で回避しない。
- 証明書preflightに失敗した場合は、`us-east-1`、account、`ISSUED`、SAN、有効期間、bootstrapの`ReviewCertificateArn`とDeployRoleの読取権限を確認する。通常Deployの再実行だけでbootstrap IAMは更新されない。証明書検証のためにDB初期化や証明書再発行を繰り返さない。
- Distribution作成でCNAMEが別distributionを指す409が出た場合は、CloudFormationの失敗イベントと現在の配信用CNAME、所有確認済みstackの状態を照合する。古いCloudFront名へのCNAMEが残ったまま同じcreateを再実行しない。独自domainを継続する復旧では、対象DNSと残存環境の処置を明示的に決め、ACM検証CNAMEを保持して古い配信用CNAMEの解消を確認する。workloadが残存しedgeがROLLBACK_COMPLETEになった失敗は、create/updateの単純な再実行で復旧できず、専用DB・backupを削除するDestroy→createには対象を限定した許可が必要。新しい配信用CNAMEの登録・HTTPS/WSS確認までを配置成功と分けて確認する。
- 配置成功後も独自URLへ接続できない場合は、summaryの`cloudfront_domain_name`と配信用CNAME、同名の旧Aレコード、TLS・DNS反映を確認する。summaryのAWS配置成功は独自URLの公開確認成功を意味しない。
- CLIの設定検証で`ValidationError`になった場合は、同じJSONログの`configuration_errors`を確認する。`variables`に不正な設定名、`requirement`に必要な形式と登録先を表示する。入力値・例外本文は出力しない。Secrets/Variablesには引用符・代入文・余分な空白や改行を付けず、上表の値だけを登録する。同一AZは両AZ名の案内になる。AWS資源の操作前に停止するため、この検証エラーが発生した実行では配置処理は始まっていない。
- 旧版のログに`error_type=ValidationError`と`__main__.py`の位置しかない場合、不正な項目は特定できない。診断対応版をmainへ反映し、**Run workflow**で新規実行する。旧実行の**Re-run jobs**では診断対応版に切り替わらない。
- 配置・撤去に失敗した場合は固定failure codeとAWS側の状態を確認する。確認用環境が不要ならDestroyを実行し、削除完了を確認する。

この文書とworkflowの追加は、AWS配置やGitHub上のEnvironment設定を実行した記録ではない。実配置・Event確認・初期化・撤去の結果は、それぞれの手動実行で確認する。

RDSの保持自動バックアップには、IAMで親DBの`DbiResourceId`を条件にする仕組みがない。タグを持たない残存も回収するため、配置・撤去roleの`rds:DeleteDBInstanceAutomatedBackup`だけは、対象account・東京リージョンの`auto-backup:*`へ許可している。このIAM権限単体の範囲は同account・同リージョンの全保持自動バックアップであり、確認環境だけに限定されない。[AWSのRDS権限仕様](https://docs.aws.amazon.com/service-authorization/latest/reference/list_rds.html)

実行コードでは、CFN台帳にある専用DBの識別子、削除前に保存した変更不能な`DbiResourceId`、`retained`状態がすべて一致した場合だけ、そのIDを指定して削除する。タグだけでは削除しない。非秘密の台帳は専用bucketの`review-destroy-ledger.json`へ保存し、撤去の再実行にも使用する。所有関係を復元できない場合は失敗とし、対象を広げない。main・workflowの変更権限とOIDCのEnvironment制限を維持する。[DbiResourceIdの定義](https://docs.aws.amazon.com/AmazonRDS/latest/APIReference/API_DBInstance.html)

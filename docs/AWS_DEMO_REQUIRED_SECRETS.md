# AWS demo runtimeで外部注入する秘密値

次のkeyは公開templateへ値もplaceholderも置かず、権限を持つ担当者がrepository外のruntime env fileへ追加する。runtime env fileはsingle-linkの通常file・exact `0600`とし、lexical pathの全componentでsymlinkを使わない。全ancestor directoryは`root`またはpreflight実行account所有かつgroup/other非書込み、直接親は同account所有とする。値をterminal、ticket、Markdown、Composeの展開結果へ貼り付けない。

| key | 条件 |
| --- | --- |
| `DATABASE_URL` | `postgresql+psycopg://`、16文字以上の専用random password、非local・非予約host、`sslmode=verify-full`を使う |
| `REDIS_URL` | `rediss://`、16文字以上の専用random credential、非local・非予約hostを使う |
| `SESSION_SECRET` | secret managerで生成した43〜128文字のURL-safe random ASCII値を使い、大文字・小文字・数字を各1文字以上含める |
| `ROUND_BACKUP_ENCRYPTION_KEY` | `rehearsal-six-round`と`load-test-ten-round`で追加する。secret managerまたは承認済みoffline環境で生成した32 byteのFernet鍵を使い、repository公開済み値・見本値・短周期の単純反復値を使わない。validatorは形式と既知・自明な弱値を拒否するがentropyを証明しない |

`single-round`の実fileには先頭3 keyと[`runtime.nonsecret.env.template`](../deploy/aws-demo/runtime.nonsecret.env.template)の全keyを、`load-test-ten-round`の実fileには4 keyすべてと[`runtime.load-test.nonsecret.env.template`](../deploy/aws-demo/runtime.load-test.nonsecret.env.template)の全keyを重複なしで含める。空白、値全体の引用符、`export`、`$`展開、backslash、`#` comment、非ASCII文字は使わず、URL内の予約文字はpercent encodeする。host validatorはComposeとの解釈差が生じる表記と、repositoryで公開済みの固定見本値を拒否する。

`rehearsal-six-round`は4 keyすべてと[`runtime.rehearsal.nonsecret.env.template`](../deploy/aws-demo/runtime.rehearsal.nonsecret.env.template)を使う。candidateで下記の負荷試験と同じ所有者・permission・path・artifact inventory条件を満たす。同一digestの2 APIは同じruntime file・session secret・Fernet鍵・backup directory・writer UID/GIDを共有し、`API_INSTANCE_ID`だけを分ける。gatewayは`Caddyfile.multi-api`を含む現行imageを使う。rollbackは不要だが、指定する場合も現行2 API構成に対応するimageと同じFernet鍵・backup directoryが必要である。監視・Toxiproxy・負荷試験manifest・exporter資格情報は指定しない。

正式負荷試験はCloudflare/R2を使用しない。通知先、専用CA、run固有の認証情報は実行時設定で渡す。

PostgreSQLのroot CA bundleは秘密値ではないが、承認したsingle-link fileをrepository外へ配置し、release記録の`AWS_DEMO_POSTGRES_CA_FILE`と`AWS_DEMO_POSTGRES_CA_SHA256`で固定する。runtime fileの`PGSSLROOTCERT=/run/secrets/aws-demo-postgres-ca.pem`は変更しない。CA bundleとそのancestor directoryへprivate keyや共有書込み権限を混在させない。release記録も同じsymlink・hard link・ancestor owner/mode条件を満たす。

負荷試験のPostgreSQL exporter passwordはrepository外の専用single-link通常fileへ改行なしの16〜512 byte ASCIIとして保存し、exact `0600`にする。`AWS_DEMO_POSTGRES_EXPORTER_USER`はapplication userと異なる専用userとし、対象DBへの接続と監視viewの`SELECT`だけを許可する。Redis exporter password fileもrepository外・single-link・exact `0600`とし、exact `{"rediss://<専用user>@<承認host>:<port>/<db>":"<password>"}`の1要素JSON mappingにする。Redis専用userは監視に必要なread-only ACL commandだけを許可する。両passwordをruntime env、release file、URI、CLI引数、log、Compose展開結果へ複製しない。

`AWS_DEMO_LOAD_TEST_MANIFEST_FILE`はbootstrap、generator、controller、照合、metrics exportが共有するrun正本である。repository外のsingle-link通常file・exact `0600`へ保存し、candidate/rollback releaseは同じpath、run ID、Event identityを参照する。配置時はcandidate releaseのsource commit・API/gateway digest・公開Originとmanifestを一致させる。rollback時はEvent/run identityだけを維持し、失敗candidate用manifestを直前成功commitへ偽装しない。

`AWS_DEMO_TOXIPROXY_CONFIG_FILE`は秘密値を含めないが、障害対象を決める制御fileとしてrepository外のsingle-link通常file・exact `0600`へ保存する。内容はexact 1 proxyの`name`、固定data listener、同一portのliteral remote IPv4 upstream、`enabled: true`だけとし、credential、接続URL、初期toxic、hostname upstreamを含めない。`AWS_DEMO_TOXIPROXY_CONFIG_SHA256`でcandidate/rollbackへ同一bytesを固定し、`AWS_DEMO_REDIS_HOSTNAME`はruntime `REDIS_URL`とRedis exporter URLの元FQDNへ完全一致させる。正式run前にこのFQDNがconfigのliteral upstream 1件だけへ解決され、DBの全解決先が`172.30.250.0/29`外であることをpreflightで確認する。既存load manifestの`fault_schedule`を障害計画として使い、別planへ時刻を重複させない。

`AWS_DEMO_METRICS_ARTIFACT_HOST_DIRECTORY`はrepository・backup・runtime・CA・release・credential・manifestと交差しない専用empty directoryとし、ownerをruntime UID/GID、modeをexact `0700`にする。exporterは`metrics-<run UUID>.json`、`.csv`、`.sha256`をsingle-link `0600`・非上書きで作る。回収後はchecksumを検証して外部archiveへ移し、次のpreflightには新しいempty directoryを用意する。

`AWS_DEMO_PROMETHEUS_CONFIG_SHA256`は承認対象の[`infra/prometheus.load-test.yml`](../infra/prometheus.load-test.yml)から `sha256sum infra/prometheus.load-test.yml`（macOSは`shasum -a 256`）で再計算する。設定変更時はcandidate/rollback templateと実release記録を同じ実digestへ更新し、validatorのchecksum一致を確認する。見本digestで代用しない。

監視imageは次のfully-qualified repositoryだけを許可し、各release記録にはregistryが返した実`sha256` digest、取得元、確認日を残す。Prometheusは`docker.io/prom/prometheus`または`quay.io/prometheus/prometheus`、PostgreSQL exporterは`quay.io/prometheuscommunity/postgres-exporter`、Redis exporterは`docker.io/oliver006/redis_exporter`、`ghcr.io/oliver006/redis_exporter`、`quay.io/oliver006/redis_exporter`のいずれか、node exporterは`docker.io/prom/node-exporter`または`quay.io/prometheus/node-exporter`とする。Toxiproxyは`ghcr.io/shopify/toxiproxy`だけを許可し、v2.12.0のversion/revision labelと`amd64`または`arm64` architectureもpreflightで照合する。private mirrorや短縮名は許可しない。preflightはlocal imageを照合するだけで、pullしない。

`DEMO_OPERATOR_USERNAME=managed-in-database`と`DEMO_OPERATOR_PASSWORD=unused`は秘密値ではない。AWS demoではseedが無効であり、runtime loginはDB内の`is_demo` accountとArgon2id hashを検証するため、未使用のseed用環境変数へcredentialを置かないための固定sentinelである。実operator accountの発行、password強度、local accountが残っていないことの確認はDB担当者の人手作業とする。`single-round`の`ROUND_BACKUP_ENCRYPTION_KEY=disabled`はfilesystem backup無効を示す非秘密sentinelである。負荷試験用Fernet鍵はrelease file、Compose変数、image label、logへ複製せず、candidate/rollback双方のruntime fileへ同じ値を注入する。runtime fileのmodeはexact `0600`とする。

負荷試験のhost backup directoryはrelease fileの`AWS_DEMO_ROUND_BACKUP_HOST_DIRECTORY`へ絶対pathを記録する。repositoryとそのancestorの外にある通常directory・非symlink・exact `0700`とし、directoryのUID/GID、preflight実行account、release fileの`AWS_DEMO_RUNTIME_UID` / `AWS_DEMO_RUNTIME_GID`を完全一致させる。全ancestorは前述のowner/mode条件を満たす。directoryは空、またはappが生成したexact `rb-<32 lowercase hex>.(advance_round|complete_event).fernet`名のsingle-link通常fileだけを含めてよい。各artifactは同じUID/GID・exact `0600`とし、無関係なsecret、temporary file、subdirectory、symlink、hard linkを1件でも含む場合はpreflightを失敗させる。Composeは明示UID/GIDで両APIを起動し、shellの`UID`やimage既定rootへ依存しない。

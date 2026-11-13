# 正式負荷試験

> この試験の旧controllerと採点oracleは600 VU・10問・readiness/targetを前提とする。P0 APIはreadiness/heartbeat/gateを廃止し、現controllerのlegacy応答検証では拒否する。正式指定ブランチとの対応が決まるまで修正版へ実行しない。ローカル100 BrowserContext/6問の結果は本書の合格に転用しない。

## 正式版受け入れ

試験対象の公開用アプリで**600人分の同時接続（600 VU）を実測で達成することだけ**を受け入れ条件とする。公開用アプリと試験コードのcommit、image digest、設定versionを実測の証拠とともに記録する。未実施・未達・証拠未回収は達成未確認と記録する。

## 既存コードの実行と記録

試験コードの指定ブランチは`codex/figma-design01-05-loadtest-controller`。試験のために公開用アプリを変更せず、指定ブランチの試験コードが対象版のAPI・採点仕様に対応しているか確認する。実行・AWS撤去の手順は[AWS自動化](AWS_LOAD_TEST_AUTOMATION.md)、証跡の回収は[証跡handoff](LOAD_TEST_EVIDENCE_HANDOFF.md)を参照する。

既存コードは`load-test-manifest-v3`による80分・10問のシナリオと機械判定を持つ。これらはコードの現在の動作であり、正式版受け入れの追加条件にはしない。`acceptance.json`などの出力結果と失敗理由はそのまま保存し、正式版受け入れ結果とは別に記録する。文書整理によって、manifest、機械判定、開始・停止制御が変更されたとは扱わない。

## データ・実行条件

DB schemaは人が既存migrationで適用する。自動処理はschema revisionと空のapplication tableを確認した後、manifestに束縛した試験データだけを作成する。既存schema/migration/backup形式とnullable画像列を変更しない。

開始前の疎通はread-onlyで確認する。運営操作から結果回収まではAWS runnerで進行する。中断したrunを再開・自動追試しない。

app/fault timerとrunnerを別EC2へ置く。private IPのSANを持つ証明書と専用CAをPython/Node/k6へ明示する。TLS検証、Secure Cookie、Origin、CSRFを維持する。internet inbound、SSH、Cloudflare、外部DNS、NAT Gateway、ALBは使用しない。

## 費用と撤去

以下は既存の正式負荷試験プログラムの仕様である。既存コードの価格・期限制御は変更していない。

作成直前の公式単価で4時間の準備・試験・撤去と制御/保存/転送予備費を見積もり、200円/USD・税10%で総額税込1,000円を超える場合は作成しない。最初の有料作成＋90分までに開始し、＋3時間で撤去を開始、1時間の削除時間を確保する。間に合わなければ試験を開始せず撤去する。削除遅延があるため円額の強制上限保証ではない。

費用・期限は[AWS試験の構成](AWS_LOAD_TEST_AUTOMATION.md)で確認し、正式版受け入れ結果と撤去結果を別々に記録する。削除APIの受付だけを課金停止の証拠にせず、サービス別不存在を確認する。証跡未回収でも期限でAWS artifactを削除する。

ローカルunit test、型検査、lint、build成功を600人同時接続の実測へ読み替えない。実測の実施状況はrunごとの結果を正本とする。

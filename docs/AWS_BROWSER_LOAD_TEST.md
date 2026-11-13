# 2台のPCからAWSへ送るブラウザ負荷試験

使用する問題JSONと開催に登録した問題を照合し、試験の問数を起動前に確定する。通常の構成は練習1問・本番5問で、予備2問を含める場合は計8問。

AWSのCloudFront公開URLへ、手元の2台のPCから既存Browser Runnerで負荷を送る。各PCは独立したrunとして起動し、参加・回答・停止をそれぞれのterminalで操作する。運営画面は人が操作する。片方を`RECORD=true`、もう片方を`RECORD=false`にすると、片方の結果だけを保存できる。両PCで共通planや鍵を共有する操作は不要である。

AWS用の入口は`aws-browser-*`とし、既存の`browser-runner-*`、`lan-*`、`local-*`はそのまま使う。RunnerのAWS予定問数の既定7は維持するが、現行の実問題では`EXPECTED_ROUNDS=6`（練習1問＋本番5問）を起動前に明示する。予備2問まで実施するなら8を指定する。旧k6の600VU/10問シナリオの合否や、既存local自動6問回帰とは別の試験である。

## 現行の回答時刻条件

現行sourceは選択期限10秒、到着猶予4秒、DB到着終端14秒、端末申告時間とDB受付経過時間の差を設定上−2,500ms〜＋14,000msとする。DB終端は排他のため実際に受付可能な正方向差は最大13,999msである。試験前に対象開催snapshotの実値と、配置アプリ・各PCのRunner source commitを記録する。回答時間の設定は[クイズ設定](QUIZ_CONFIGURATION.md)を参照する。

## 実行前に確認すること

独自ドメインを使う場合は、各Runner端末のシェルで `export AWS_REVIEW_DOMAIN_NAME=quiz.example.org` の例を今回のホスト名に置き換えて設定する。CloudFront標準URLは今回の配置出力から取得する。

1. 試験対象は、所有者が確認した専用AWS環境の公開URLとする。Deployの`public_entrypoint=custom-domain`なら`https://quiz.example.org`、`cloudfront`ならその配置が発行した`https://d….cloudfront.net`を使う。runtime originと参加者・運営・両PCのURLをそろえる。参加者画面は`/`、手動運営は`/operator`。TLS検証を無効にせず、HTTPS/WSS・Secure Cookie・Originの契約を維持する。任意の独自domain、port、path、queryは許可しない。
2. 各PCではGit checkoutから実行する。RunnerはローカルのHEADと未commit変更の有無（`sourceDirty`）を自動記録する。`SOURCE_COMMIT`の指定、cleanな作業ツリー、AWS配置版や両PCのcommit一致は起動条件にしない。この記録はAWS配置版の証明ではないため、配置版を特定して結果を残す場合は配置記録のcommit・image digestを別に確認する。
3. 未使用の試験専用開催、予定問数以上の問題、専用運営アカウント、機械VUと手動参加者を収容できるID poolを確認する。専用reviewの手動Deployは既存のID準備まで行う。それ以外の人手準備は[参加者ID割当のDB準備](handover/README.md#参加者idの準備)に従う。schema revision・slot・sequence・API roleの権限を確認し、revision一致だけではpool準備完了にしない。
4. AWSの配置・撤去は[既存のAWS確認環境手順](GITHUB_ACTIONS_AWS.md)で別途行う。create・updateの双方がDBを初期化し、EC2再起動・アプリ停止を伴い得るため、試験中に実行しない。Runnerの準備・起動から、AWS操作、migration、DB reset、seed、ID pool準備を自動実行しない。
5. 両PCのCPU・メモリ・FD・ネットワークと保存先の空き容量を確認し、試験中にPCを休眠させない。250/300 VUは計画人数であり、そのPCで生成に成功する保証ではない。準備・操作の失敗や生成側の遅れを、AWS側の性能だけに帰属させない。
6. 選択した公開URLのHTTPS/WSS・参加・回答を事前確認する。`custom-domain`では[証明書とDNSの準備](GITHUB_ACTIONS_AWS.md#証明書とdnsの準備)も必要であり、Deployの成功summaryだけではDNS確認済みにならない。`cloudfront`では独自DNSを操作しない。reviewの参加者・運営sessionは各120分のため、参加/loginからの経過時間にも注意する。

## 両PCの依存を準備する

macOSまたはLinuxの同じrepositoryのrootで、両PCとも一度実行する。既存Browser Runnerの依存準備・Chromium取得・buildを再利用する。保存先と親directoryは所有者またはrootの所有とし、共有書込みやsymlink経由の配置を避ける。

```sh
make aws-browser-prepare
```

この操作ではアプリ、VU、AWS資源を起動しない。以後のコマンドは選択した公開URLでの確認完了後に実行する。運営password、参加token、鍵の内容をコマンド引数へ記載しない。

## 各PCを起動する

次の項目を指定する。両PCで接続先と予定問数をそろえ、人数と記録の有無を選ぶ。その他の設定は必要な場合だけ後述の変数で変更する。

| 変数 | 指定内容 |
| --- | --- |
| `BASE_URL` | Deployが返す公開URL。`custom-domain`は`https://quiz.example.org`、`cloudfront`は今回発行された標準URL |
| `VUS` | そのPCから参加するVU数。50〜650、既定300。例250または300 |
| `RECORD` | 既定`true`。`true`は結果保持、`false`は終了結果表示後にそのrunの記録を削除 |
| `EXPECTED_ROUNDS` | 予定問数。AWS既定7、1〜4095。現行の実問題では通常6、予備を含める場合8を明示する。起動後の実績から予定を縮めない |

以下は`custom-domain`で合計600 VUを予定する例。`cloudfront`で配置した場合は、すべての`BASE_URL`を今回発行された標準URLへ置き換える。PC1は300 VU分の記録を保存する。

```sh
make aws-browser-runner \
  BASE_URL="https://${AWS_REVIEW_DOMAIN_NAME:?独自ドメイン名を設定してください}" \
  VUS=300 RECORD=true EXPECTED_ROUNDS=6
```

PC2は同じURLへ300 VUを送る。終了後の記録は保持しない。

```sh
make aws-browser-runner \
  BASE_URL="https://${AWS_REVIEW_DOMAIN_NAME:?独自ドメイン名を設定してください}" \
  VUS=300 RECORD=false EXPECTED_ROUNDS=6
```

合計500 VUなら、両コマンドの`VUS`を250にする。PCの能力に合わせて250＋350など別々の値も指定できる。片側の記録だけを残すことは、片側の人数を全体人数として扱うことを意味しない。

各起動で一意なrun IDとそのrun専用の32 byte鍵を生成し、`.local/aws-browser-runs/<自動run ID>/`を作る。directoryは0700、fileは0600。両PCの鍵やVU序数をそろえる必要はなく、SCPによるplan転送も不要である。

### 必要な設定をすべてコマンドで指定する

JSONの手編集は不要。次は共通設定をすべて明示した例であり、同じ設定をPC2へ使う場合は`RECORD`と必要な`VUS`を変える。

```sh
make aws-browser-runner \
  BASE_URL="https://${AWS_REVIEW_DOMAIN_NAME:?独自ドメイン名を設定してください}" \
  VUS=300 RECORD=true EXPECTED_ROUNDS=6 \
  WORKER_SIZE=50 MEASUREMENT_LEVEL=minimal DETAILED_VU_COUNT=3 \
  HOST_SAMPLE_INTERVAL_MS=3000 SETUP_TIMEOUT_SECONDS=900 \
  OPERATION_TIMEOUT_MS=30000 MANUAL_PARTICIPANTS=0 \
  EXPECTED_OPERATOR_CONNECTIONS=1 CHANNEL=chromium HEADLESS=true
```

| 変数 | 用途 |
| --- | --- |
| `WORKER_SIZE` | 1 workerあたりのVU数。1〜100 |
| `MEASUREMENT_LEVEL` | `normal` / `minimal`。minimalでも接続数・回答・記録欠測を残す |
| `DETAILED_VU_COUNT` | normal時の詳細診断対象数。0〜10 |
| `HOST_SAMPLE_INTERVAL_MS` | そのPCの資源採取周期。2000〜30000ms |
| `SETUP_TIMEOUT_SECONDS` | 全worker準備期限。10〜900秒 |
| `OPERATION_TIMEOUT_MS` | navigation・join・回答操作などの有限timeout。100〜30000ms |
| `MANUAL_PARTICIPANTS` | そのrunで別枠として記録する手動参加者の予定人数。機械VUへ加えない |
| `EXPECTED_OPERATOR_CONNECTIONS` | そのrunで別枠として記録する運営接続の予定人数。機械VUへ加えない |
| `CHANNEL` | `chromium` / OSへ導入済みの`chrome` |
| `HEADLESS` | `true` / `false`。falseはブラウザ画面を表示する |

AWS用Runnerの操作時間は`stop`までとし、`DURATION_SECONDS`は指定しない。準備・各操作・worker応答・終了回収の有限timeoutは維持する。数時間の試験は、開始前の待機や予定問終了後の接続保持を含めて行う。問題や回答を無限に繰り返すモードではなく、アプリのsession期限も延長しない。

## 両terminalで参加・回答を操作する

起動直後は画面・参加ボタン・フォントを準備する。**両PCの「準備完了」を確認してから参加する。** 自動参加や自動運営は行わない。対話terminalを開いたままにする。

1. 各PCのterminalへ次を入力する。120秒の分散はそのPCのVU範囲内に適用される。入力時刻が違えば、参加開始時刻も異なる。

   ```text
   status
   join --spread 120s
   ```

2. 両PCで登録数・WS数・失敗・timeoutを確認する。参加命令の配布完了だけで全員登録済みとは判断しない。人はブラウザで同じCloudFront URLの`/operator`を開き、運営アカウントでログインする。
3. 運営画面で第1問を確定・配信し、**両PCのterminalで**同じ問の回答を予約する。

   ```text
   answer --round 1 --when-open --delay 500ms
   ```

4. 両PCへ予約した後に、運営画面で開始を予約する。各VUはその問が回答可能になってから500ms後に回答する。表示された問の指定、可視性、disabled/inert、同期済み回答期限を既存Runnerが確認する。
5. 両PCのクリック・受付・結果件数を確認してから人が次問へ進む。問番号を2〜予定問数（通常6）へ変更し、配信→両PCで予約→運営開始→結果確認を繰り返す。過去問の命令を次問へ流用しない。本人結果の観測は表示中の得点画面に付いた実出題番号で確認し、UI更新後の見出し文言だけに依存しない。

途中操作も各PC独立で行う:

```text
pause
status
resume
stop
```

`pause`は新しい参加・回答を保留し、WS・状態受信・結果観測は継続する。アプリの回答期限は止まらず、送信済み回答を取り消さない。回答・操作の詳しい意味は[共通Runnerの操作説明](LOCAL_BROWSER_CONTROL.md#毎回の操作)を参照する。

PC1の命令はPC2へ配信されない。片側の停止・異常終了でも、もう片側は自動停止しない。試験を中断するときは両PCへ`stop`を入力する。Ctrl+Cまたはterminal入力のEOFでも、そのPCの停止処理へ入る。

## RECORDの意味と終了時の扱い

`RECORD`は**終了後に証跡を保持するか**を指定する。`false`でも起動中のVU操作・監督・計測は同じで、一時証跡を使って終了結果を作る。軽量化や記録処理の無効化を意味しない。

| 設定 | 終了時の扱い |
| --- | --- |
| `RECORD=true` | 終了結果をterminalへ表示し、run directory内の設定・鍵・report・CSV・worker証跡を保持 |
| `RECORD=false` | 終了結果をterminalへ表示後、今回作成したrun directoryだけを削除 |

`RECORD=false`は、Runnerが異常終了を検出した場合も、終了処理を完了できれば今回runの記録を削除する。後から原因を調査したい場合は、そのPCも起動前に`RECORD=true`を選ぶ。削除後の証跡を再作成できるとは扱わない。

電源断、OSやプロセスの強制終了、削除失敗では一時directoryが残る場合がある。再起動後は起動時に表示されたrun IDと`.local/aws-browser-runs/<run ID>/`の残存を確認する。該当Runnerと子ブラウザが終了していることを確認してから、所有者が**そのrunだけ**を片付ける。他runを含む`.local/aws-browser-runs`全体の削除で代用しない。削除失敗の表示や終了コードを無視して「記録なし」と報告しない。

## 保存した結果の読み方

独自ドメインの試験結果を後日 `control:report` で再集計する場合も、試験時と同じ `AWS_REVIEW_DOMAIN_NAME` をシェルに設定する。

`RECORD=true`側の`report.json`、`rounds.csv`、`connections.csv`、`resources.csv`、`termination.json`、worker証跡を確認する。内容は[共通Runnerの成果物](LOCAL_BROWSER_CONTROL.md#statusと成果物の読み方)に従う。

**300 VUを記録したreportは300 VU分の結果であり、もう一方が動いていても600 VUのreportにはならない。** 両PCの`status`は各PCの稼働確認に使い、AWS全体の600同時接続はサーバー側の指標などで別に確認・記録する。

既存APIの接続指標は`quiz_websocket_active{audience="participant"}`。複数APIの場合は各APIの同時刻の値を合計し、運営用接続と区別する。取得は対象環境の内部監視経路を使い、metricsをインターネットへ公開しない。このRunnerはAWS側の監視設定や採取を追加しないため、未採取なら全体同時接続は未確認として残す。

- `VUS`の合計は予定人数。実接続の達成人数ではない。
- 各PCの接続peakは、そのPC内のworker報告を集約した標本最大。別時刻に生じた両PCのpeakを足して同時接続数にしない。
- 手動参加者・運営の予定人数は実数未観測と区別し、機械VUへ加えない。
- `measurementComplete`は記録の完全性、`expectedRounds`は起動前に指定した予定問数、`allRoundsBrowserComplete`はそのPCの予定全問のブラウザ完了を示す。DB保存検証とは別である。過去reportの`sixRoundBrowserComplete`は当時の6問契約であり、別の予定問数の成功へ読み替えない。
- 終了コード0は停止・回収・記録処理の結果であり、600同時接続や全問成功の合格を保証しない。

本手順では、両PCを一つのrunとして匿名DB照合したり、reportを自動合算したりしない。保存側の`databaseMembershipVerified`は未検証として扱う。同じ開催には非記録側の参加者もいるため、保存側の単体manifestをDB全体へ照合した結果を、全体の合否として使わない。

過去のlocal/LANの成功や、unit test・lint・buildの成功を、新しいAWS配置版の実測へ転用しない。試験日時、配置commit/digest、各PCの予定人数とstatus、保存側のrunと結果、AWS側の観測、確認できなかった項目を区別して残す。

## 両PCの停止とAWS撤去

1. 両PCへ`stop`を入力し、それぞれの終了表示を確認する。片側だけの正常停止で全体の停止完了にしない。
2. 記録側の証跡を回収し、非記録側は終了結果と今回runの削除結果を確認する。鍵、運営password、token、Cookie、AWS credentialを公開ログや報告書へ含めない。
3. AWSが不要になったら、[AWS Review Destroyの既存手順](GITHUB_ACTIONS_AWS.md#確認が終わったら撤去する)を所有者が実行し、サービス別の削除完了を確認する。この撤去は確認用データとバックアップも削除するため、必要な証跡回収を先に完了する。

Runnerの`stop`、PCの終了、CLIの失敗はAWS撤去ではない。AWS確認環境は削除確認まで稼働・課金が続く。旧正式k6の期限付き撤去が、このCloudFront確認環境にも設定されているとは扱わない。

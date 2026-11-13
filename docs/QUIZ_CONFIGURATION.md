# クイズ進行・結果表示・採点の設定

進行・採点・結果表示を変更するには、[quiz-settings.json](../backend/src/quiz_app/quiz_configuration/quiz-settings.json)を編集する。
このファイルが新規開催・local demo・リハーサルの共通設定である。秘密情報は入れない。

問題文・選択肢・正解は[問題JSONの編集手順](QUESTION_CONFIGURATION.md)を参照する。進行設定と問題データは別ファイルで管理する。

## 設定項目

| 項目 | 単位・意味 | 初期値 |
| --- | --- | --- |
| `schema_version` | JSON形式のversion。現在は整数3のみ | `3` |
| `version` | 開催設定のversion。原則は内容変更時に新しい名前にする | `quiz-2026-10-23-v1` |
| `start_delay_seconds` | 予約操作から問題開始までの秒数。整数1〜300 | `3` |
| `scoring.version` | 保存する採点policyのversion。原則は方式・係数変更時に新しい名前にする | `scoring-2026-10-15-v2` |
| `scoring.correct_points` | 1問の正答点。整数0〜1,000,000 | `400` |
| `scoring.maximum_speed_bonus` | 開始直後の速度加点。整数0〜1,000,000 | `100` |
| `scoring.seconds_per_point` | 速度加点が1点減る秒数。0より大きく10以下、整数msへ変換できる数値 | `0.1` |
| `timing.basis` | 正式回答時間の計測方式 | `client_monotonic_v1` |
| `timing.arrival_grace_ms` | 選択締切後のサーバー到着猶予ms | `4000` |
| `timing.clock_tolerance_ms` | 到着猶予へ加算する時間差の上限許容ms | `10000` |
| `timing.client_ahead_tolerance_ms` | 端末申告時間がサーバー受付経過時間より先行できるms | `2500` |
| `visibility.distribution` | 参加者画面の回答分布、正答率、有効回答件数 | 初期表示、手動 |
| `visibility.personal_ranking` | 参加者本人の総合順位と回答速度順位 | 初期非表示、手動 |
| `visibility.overall_ranking` | 参加者画面のTOP3。本人順位も公開されている場合に表示 | 初期非表示、手動 |

不足・未知項目、型の不一致、負数、不正な小数、重複・逆順の切替予定は拒否する。
`"3"`、`"false"`等の文字列を数値・真偽値へ自動変換しない。
参加者画面は選択前に開始からの経過時間を表示し、選択時に端末の単調時計で測った整数ミリ秒を固定する。APIへ送る `client_elapsed_ms` は採点にも使い、通信が途切れて再送する場合は同じ回答ID・選択肢・計測値を使う。

全参加者の選択時間は10秒未満（整数0〜9,999ms）。サーバーは開始以上・開始14,000ms未満のDB到着を検証し、到着猶予を選択時間の延長には使わない。端末申告とDB受付の差は設定上−2,500〜＋14,000msを検証するが、DB終端が排他で申告時間は0ms以上のため、実際に受付可能な正方向差は最大13,999msとなる。補助回答の区分・時間枠は設けない。
開始時刻を明示指定する場合の許容範囲は、DB時刻から`max(1, 待機秒-1)`〜`待機秒+1`秒。初期値なら2〜4秒である。

## 正解・結果の公開

開始10秒で選択を締め切り、到着猶予4秒後に全回答を集計する。集計が成功すると `ReadyToReveal + Withheld` になり、運営は正解・正答率・分布・ランキング・回答速度を確認できる。司会者も運営認証を使い、司会者画面で正解と集計を確認できる。運営画面の「正解・結果を公開」はサーバーの `can_publish_results` がtrueのときに使用できる。司会の発表に合わせて押すと `Reveal + Published` へ進む。集計完了だけでは自動公開しないため、参加者への正解発表時刻に最大値はない。

参加者は公開まで「正解発表をお待ちください」と表示する。公開前の新snapshotを参加者順位・本人結果に使わず、公開後は以下の3項目の設定を適用する。司会と運営の操作は[当日の手順](handover/OPERATIONS.md#各問の進め方)を参照する。

## 手動・自動の切替

各項目に`mode`、`initially_visible`、`changes`を指定する。

- `mode: "manual"`は運営画面で切り替える。`changes`は空配列にする。
- `mode: "automatic"`は指定問の結果公開時に切り替える。`changes`を1件以上、`after_round`が増える順に並べる。
- `initially_visible`は新規開催の初期状態であり、次問ごとにリセットしない。
- 自動モードでも運営画面から変更できる。その値は次の明示的な自動切替まで維持する。最後の自動切替後の手動変更は以後維持する。
- 正解・結果の手動公開前は、今回の正解・正誤・得点・分布・順位を参加者APIへ返さない。公開後は3項目の非表示設定にかかわらず正解・本人の正誤・得点を表示し、分布・順位の非公開データはAPIからも除外する。

例えば全体順位を「最初は非表示、練習第1問後に表示、本番第1問後に非表示、本番第3問後に再表示」にするには、同じJSONの`visibility.overall_ranking`を次に変える。

```json
{
  "mode": "automatic",
  "initially_visible": false,
  "changes": [
    { "after_round": 1, "visible": true },
    { "after_round": 2, "visible": false },
    { "after_round": 4, "visible": true }
  ]
}
```

`after_round`は練習1問を含む実出題番号であり、その問の結果公開時から新しい表示状態になる。1は練習、2は本番第1問を指す。画面の本番番号へ設定値を置き換えない。
次問の準備だけでは切り替えず、内部の状態更新versionや問題集のIDを使わない。
開始前のVOIDは番号に数えず、出題後のVOIDは数える。導出は[出題番号の契約](ROUND_NUMBERING.md)と共通である。
集計失敗・未公開状態のまま自動切替を進めない。
既に通過した切替予定を毎問再適用しないため、非表示後の手動再表示も維持できる。

## 採点と保存

正答は`正答点 + max(0, 最大速度加点 - floor(経過ms / 刻みms))`で採点する。
初期値では0〜99msが500点、100msが499点、9,900〜9,999msが401点。
10,000msちょうどは選択期限外であり、採点対象に入らない。DB到着時刻はこれと別に猶予終端を検証する。
誤答・未回答・VOIDは0点。全参加者を同じ回答・採点区分で扱う。
時間刻みは速度順位の同着幅にも使う。初期値は100ms単位の同着になる。

採点の正本はEventが参照するDBの`ScoringPolicy`、過去得点は`ScoreLedger`である。
新設定は`base_points=400`、`initial_speed_bonus=100`、`speed_bucket_ms=100`、`speed_decrement_per_bucket=1`、`standard_duration_ms=10000`へ保存する。
既存の採点policyと得点履歴は保持する。

JSONはAPI起動時に一度読み込み、起動中のファイル編集を反映しない。
設定はbackend artifactに同梱するため、編集後は同じsourceからbackendを再buildする。
開催には設定の全文snapshotと表示状態を保存し、実行時にはJSON・Eventの採点version・DB policyの全係数を照合する。
不一致や設定欠落は進行・projectionを拒否する。起動・GETを理由にDBを補正しない。
開始後のJSON差替え、採点version切替、過去得点の再計算を通常手順として提供しない。

## 練習問題と総合ランキング

実際に配信した先頭1問を練習とする。問題集のIDではなく、永続履歴から導出した`round_number`の1で判定する。運営が開始前に問題を選び直した場合も最初の1問が練習になる。参加者には練習第1問、その後は本番第1問から表示する。

練習の正答発表では、その問の獲得点を通常どおり表示する。練習の回答・得点は保存するが、総合得点・正解数・同点判定用の回答時間には含めない。本番1問目の配信後は練習の得点表示から待機画面へ切り替わり、総合0点から本番の得点を加算する。開始前VOIDは練習枠を消費せず、開始後VOIDはその枠を消費する。

本番5問で進行を打ち切らない。事前登録した未使用問題があれば続行でき、運営は各問の結果発表後に「イベント終了」を選べる。問題の残数による自動終了はなく、終了操作は既存の暗号化backup検証を通す。

途中参加者も参加後の回答で総合・速度順位に入る。未回答の問題による加点はない。本人・全体順位の公開設定は維持する。練習だけ回答した参加者は総合0点として残る。

手動Event初期化は保存済みの問題・設定を保持する。新しい問題・設定は新規開催へ反映する。

## DB変更と人手適用

期待するDB revisionは [`20261015_0007`](../backend/src/quiz_app/schema_revision.py)。
`event_quiz_configurations`はEventとの1対1で、次を保持する。

| 列 | 内容 |
| --- | --- |
| `event_id` | Event FK兼primary key |
| `settings_json` | 検証済み設定全体のsnapshot |
| `distribution_visible` | 現在の回答分布の表示状態 |
| `personal_ranking_visible` | 現在の本人順位の表示状態 |
| `overall_ranking_visible` | 現在の全体順位の表示状態 |
| `last_applied_round` | 自動切替の適用済み終了問数 |

既存の`events.ranking_visibility`列は過去schema・backupとの関係から残すが、新しいAPI・表示判断には使用しない。
自動切替は運営の正解・結果の公開と同じtransaction、手動切替は既存の運営認証・CSRF・version・冪等性・監査transactionで保存する。
API processの再起動や複数process間で表示状態を失わない。

実DBへの適用は、対象と権限を確認した担当者が行う。同じ採点versionでも古いDBには異なる係数が保存されている場合があるため、全係数と開催snapshotを照合する。

1. DBと開催の識別子、現在のrevision、問題・設定・採点versionを確認し、DBとbackup原本を保管する。切替時は対象APIとworkerを停止する。
2. 現行版へ移行するDBでは、[revision 0007の適用条件](../backend/migrations/versions/20261015_0007_manual_results.py)を確認する。旧補助参加者や `Assisted` 除外台帳が残っている場合、開催が進行中・中断中・障害中の場合、または開始予約・受付・集計中のRoundがある場合は適用が拒否される。既存データや状態を書き換えて回避せず、旧DBを保持して新開催用の専用DBを用意する。
3. 新規local demoは人手のseed、新規専用リハーサルDBはinitializer/prepareを使う。どちらも同じJSONからpolicyと開催設定を作成する。初期化にmigrationを含む経路では二重適用を避ける。[参加者IDの準備](handover/README.md#参加者idの準備)でpool・slot・sequenceとAPI権限を確認する。
4. 既存の未開始Eventへ設定を反映する必要がある場合は、担当者が別途対象と影響を確認する。transaction内でEventをlockし、開始時刻を持つRound・Answer・ScoreLedger・RankSnapshotがないことを確認する。新しい採点policyは別versionで登録し、開催の参照と設定snapshotを同じtransactionで更新する。
5. revision、schema、実行roleの権限を確認し、同じsourceのAPI・worker・frontendを配置する。設定不一致時は起動時の自動更新やDBリセットで補正しない。

既存Eventの設定欠落をseedで補うことはしない。すでに設定のある開催へ同じseedを再実行しても、運営が変更した表示状態を初期値へ戻さない。
新tableを消す自動downgradeも提供しない。切戻しは保存済み設定・表示状態を含め人がレビューする。
Round backupは回答・得点・集計の退避であり、開催全体の復元物ではない。完全復元にはEvent、採点policy、開催設定tableと表示状態も必要になる。

## 既存ツールとの境界

APIの表示切替は`POST /api/v1/operator/result-visibility`で、`target`と`visible`を既存のmutation envelopeへ加える。
参加者結果は公開範囲に応じて `distribution` がnullになる。運営認証用の順位APIと参加者用の投影は分かれている。

旧正式負荷試験のrunnerは開始待ち5秒、第1問の総合得点算入、途中参加の順位除外を前提としており、現行アプリの検証には使えない。負荷試験では使用するアプリとrunnerの設定を照合する。

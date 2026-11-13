# 問題文・選択肢のJSON設定

問題文、4つの選択肢、正解はJSONだけで編集できる。用途に合わせて次のファイルを開く。

| 用途 | 編集するJSON | 出題の構成 |
| --- | --- | --- |
| 本番・リハーサル用のゲストクイズ | [event-questions.json](../content/event-questions.json) | 通常は練習1問・本番5問、予備2問 |
| ローカル動作確認用の架空問題 | [demo-questions.json](../backend/src/quiz_app/bootstrap/demo-questions.json) | 10問（先頭1問が練習、以降9問が本番扱い） |

待機時間・採点・結果表示は別の[クイズ設定](QUIZ_CONFIGURATION.md)で管理する。実際の配信順の先頭1問は練習としてその問の得点を表示し、2問目以降を本番第1問から数えて総合集計する。途中参加者も総合ランキングへ含める。運営画面で先頭の問題を変更すると、練習に使う問題も変わる。

通常の出題順は練習1問、本番5問、予備2問。時間があれば予備へ進める。運営は各問の結果発表後に終了を選ぶ。

## 実問題を編集する

`questions`内の該当する問題を編集する。例えば次は1問分の形式であり、ファイル全体の置換には使わない。

```json
{
  "id": 1,
  "respondent": "ゲスト",
  "prompt": "問題文をここに入力",
  "options": [
    { "label": "A", "content": "選択肢1" },
    { "label": "B", "content": "選択肢2" },
    { "label": "C", "content": "選択肢3" },
    { "label": "D", "content": "選択肢4" }
  ],
  "correct_label": "B",
  "answer_note": "確認用メモ。参加者には表示されません。"
}
```

- `prompt`が問題文、`options[].content`が各選択肢、`correct_label`が正解のA〜D。
- `respondent`は空でない出題対象の表示名。問題文の前へ表示する。
- ファイル先頭の`selected_question_ids`が出題順。
- IDは重複しない正整数。問題集・出題するIDはそれぞれ3件以上とし、出題するIDは問題集に存在するものを重複なしで指定する。先頭1件に練習用を配置する。予備を用意する場合も`selected_question_ids`へ含めて開催に登録する。全問に確定したA〜Dの正解を設定し、`null`や仮答えのままにしない。
- 問題文・選択肢・回答者名は空欄不可、各500文字以内。`answer_note`は省略可能で、指定する場合は空欄不可・500文字以内。同じ問題の選択肢は重複不可。解説は正解の選択肢から自動生成する。

編集後、リポジトリのルートで検証する。DBやAWSには接続しない。

```sh
uv run --project backend python -m validation.rehearsal check --content content/event-questions.json
```

成功時は`status: "ready"`と出題順、内容のSHA-256が表示される。新開催に登録するときは、担当者が対象DBと消去範囲を確認する。手動で準備する場合は[リハーサル手順](AWS_REHEARSAL.md)、専用のAWS環境では[手動workflowの手順](GITHUB_ACTIONS_AWS.md)を参照する。

## ローカル用の架空問題を編集する

`demo-questions.json`の`questions`配列を編集する。初期値は10問で、3問以上の範囲で追加できる。次は1問分の形式。

```json
{
  "prompt": "[DEMO] 架空のマスコットの帽子は何色？",
  "explanation": "[DEMO] 正解は「あお」です。",
  "options": ["あか", "あお", "みどり", "きいろ"],
  "correct_label": "B"
}
```

`options`の順番はA、B、C、Dに対応する。UUIDや各選択肢の正誤フラグを編集する必要はない。
`questions`の配列順が問題一覧の順番になる。実際の次問選択は既存の運営画面から行う。
問題文は500文字、解説は1,000文字、各選択肢は500文字以内。前後の空白は除去し、空欄や選択肢の重複、不明な項目・型を拒否する。
`schema_version`は整数`1`のままにする。ローカル用は架空問題だけを扱い、問題文・解説の`[DEMO]`を残す。

```sh
uv run --project backend python -m quiz_app.bootstrap.demo_content_loading
```

別ファイルを下書きとして確認する場合は`--content /path/to/demo-questions.json`を指定できる。
これは検証対象の指定であり、seedの入力先を切り替える設定ではない。
現在の10問では、成功時に`status: "ready"`、`question_count: 10`、`option_count: 40`、`database_accessed: false`が表示される。
不正なJSONや読込失敗は終了コード2で拒否する。

## アプリへの反映

JSONは初期データの入力であり、表示の正本はDBに登録した問題である。ファイル保存、API再起動、手動Event初期化では登録済みの問題は変わらない。問題を差し替えるときは、新しい開催に問題JSONと[開催設定](QUIZ_CONFIGURATION.md)を登録する。

ローカル用JSONはbackend imageへ同梱する。新しい専用ローカルDBへ登録するときは、JSON検証後に同じソースからimageを再buildし、[ローカルセットアップ](../README.md)で投入する。
既存DBと内容が異なる場合、seedは不一致を検出して停止し、既存問題・回答・進行状態を上書きしない。
稼働中の開催への差替えやDBリセットはこの編集手順に含まない。既存DBの変更が必要な場合は対象と影響を別途確認する。

問題集・正答はfrontendや公開ディレクトリへコピーせず、公開済み結果だけを既存APIから表示する。

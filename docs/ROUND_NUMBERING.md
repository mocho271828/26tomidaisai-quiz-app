# 出題番号の契約

- 参加者APIの`current_round`と`latest_result`は、それぞれ必須の正整数`round_number`として実出題の連番を返す。1は「第1問（練習）」、2以降は1を引いて本番「第1問」以降として表示する。待機・回答・結果・本人順位・運営の問題別回答速度で同じ表示規則を使う。
- 総合点からの練習除外はbackendで先頭1問に適用する。frontendの表示変換でAPIの番号を書き換えず、ブラウザ観測と公開設定の`after_round`は練習を含む実出題番号のまま扱う。問題バンクの番号は「登録問題」として実出題番号と区別する。
- `version`は内部revision、`state_version`は状態の新旧判定のための値として維持する。回答の`round_version`、競合検出、古いresponseの拒否へ出題番号を流用しない。
- 問題の選び直しは同じRoundなので番号を増やさない。問題集の`display_order`や問題versionは番号の根拠にしない。次Roundの準備中には次の番号を表示し、直前結果には直前の番号を保持する。
- 番号は「同じEventで対象より前にある、開始前VOID以外のRound数 + 1」。過去Roundを確定してから次Roundを`max(version) + 1`で作る既存の進行契約を利用する。対象自身を数えるqueryは使わず、対象の内部versionが読取中に増えても番号を変えない。
- 導出の共通入口は`quiz_app.domain.round_number.quiz_round_number`。audience projectionと、出題番号を条件にするdomain処理はこの入口を利用し、公開済み結果の件数など別の数え方を追加しない。
- 開始前のVOIDは数えず、開始時刻以降のVOIDは数える。未予約（`open_at`なし）のVOIDは開始前として扱う。予約済みVOIDは`open_at`と成功したVOID確定監査の`occurred_at`を比較し、時刻が一致する場合は出題済みとする。現在時刻との比較では判定しない。
- VOID確定監査は同一Event/Round、`target_type=quiz_round`、Round UUIDと一致する`target_id`、`after_state=Voided`、`outcome=succeeded`を満たすもの。監査時刻はVOID確定の判断に使ったDB時刻と同じ値とする。backup後もこの監査を保持する。
- 予約済みVOIDに確定監査がない、または異なる確定時刻が複数ある場合は`round_number_void_timing_unavailable`で拒否する。出題有無を推測して番号を返さない。
- 現行実装にVOID確定mutationはない。

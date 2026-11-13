# 2027年担当者への引き継ぎ

この資料は、富大祭クイズアプリを翌年の担当者が理解し、開催準備を始めるための入口です。
2026年の富大祭で本番使用しました。
本番日時、使用commit、参加人数、600人同時接続の実測、終了後の撤去状況は未確認です。
現在のmainと本番使用版が一致するとは限らないため、今年の実績と来年使う版を分けて確認してください。

## 読む順番

1. 本書で構成、開発の始め方、年度変更箇所を確認する。
2. [当日の運用と障害対応](OPERATIONS.md)で、進行とデータを消す操作を確認する。
3. [公開リポジトリにする前の確認](PUBLIC_RELEASE.md)で、公開範囲を決める。

## アプリの構成

| 部分 | 役割・主な場所 |
| --- | --- |
| 参加者 `/` | 登録、回答、本人結果、終了後案内。`frontend/src/quiz/participant/` |
| 運営 `/operator` | 問題配信、結果公開、次問・終了、表彰。`frontend/src/quiz/operator/` |
| 司会者 `/moderator` | 参加者画面の埋込と確定済みTOP5。`frontend/src/quiz/moderator/` |
| Frontend | React・Vite・TypeScript。画面の入口は[App.tsx](../../frontend/src/App.tsx) |
| API | FastAPI・Pydantic・SQLModel。`backend/src/quiz_app/` |
| 保存・通知 | PostgreSQLが状態の正本。Redisは変更通知に使用 |
| ローカル環境 | Caddyが画面・API・WebSocketを同じ入口へ集約。通常はAPI 2系統 |
| AWS配置 | 手動workflowは`.github/workflows/`、実行処理は`validation/aws_review/` |

単一開催のアプリであり、画面から開催を切り替える機能はありません。
司会者画面の認証と参加者登録には[運用上の注意](OPERATIONS.md#司会者画面の扱い)があります。

## 手元で始める

Docker EngineとDocker Composeを用意します。
hostで品質確認する場合はNode.js 22以上、pnpm 11、uvも必要です。
以下はリポジトリのルートで実行するコマンドです。
既存の`.env`がある場合は上書きせず、管理者と設定を確認してください。

```sh
make local-env
```

このコマンドは環境専用のラウンドバックアップ鍵を生成し、`.env` を所有者だけが読み書きできる権限で新規作成します。既存の `.env` は保持します。認証情報とバックアップ鍵の扱いは[セットアップ手順](../../README.md)を確認してください。
実際の値はGitへ登録しません。
運営パスワードは初期投入時にDBへ保存されるため、後から`.env`だけを変えても既存アカウントは更新されません。

### 初回DB作成は人が対象を確認して行う

`make local-db-reset`は初回作成にも使えますが、既存の通常ローカルDBを**確認入力なしで全削除**します。
参加者、回答、得点、進行、セッションを失い、架空10問と運営アカウントを再投入して起動します。
既存データを破棄できることを確認した担当者が、[セットアップ手順](../../README.md)に従って初期化します。
既存データを引き継ぐ場合は、担当者が対象DBと配備する版を照合し、[開催設定の適用手順](../QUIZ_CONFIGURATION.md#db変更と人手適用)を確認します。

### 参加者IDの準備

新規参加には、開催ごとの3文字IDの候補リストと採番用のsequenceが必要です。`make local-db-reset` は専用ローカルDBの初期化時にこれらも作成します。既存データを保持する場合や、手動で新しい開催を登録した場合は、DB担当者が次を確認します。

1. 対象DB・開催UUID・配備版のschema revisionを確認し、既存データをバックアップします。準備済みのリストは再作成しません。
2. 未準備の場合は新規参加を止め、`QUIZ_EVENT_ID` に対象の開催UUIDを設定します。次のコマンドはDBに接続せず、未使用の出力先にSQLを生成します。

   ```sh
   uv run --project backend python -m quiz_app.bootstrap.participant_id_pool_sql \
     --event-id "$QUIZ_EVENT_ID" --output /absolute/private/path/participant-id-pool.sql
   ```

3. DB担当者がSQLと接続先を確認して適用します。SQLは既存参加者のIDを除き、revision不一致や準備済みの場合は停止します。失敗時は対象DBの状態を確認してください。
4. poolの容量とslot件数が一致し、IDが既存参加者と重複していないことを確認します。API用roleにはpool・slotの `SELECT` と開催専用sequenceの `USAGE` が必要です。容量が0の場合はslotも0件で、sequenceは作成されません。
5. 参加受付を再開するときは、既存参加者の復帰と新規登録を確認します。復元時は参加者・session、リスト、sequence、session secretの対応を保ちます。採番の欠番はそのまま扱います。

### 初期化後の通常操作

| コマンド | 用途 |
| --- | --- |
| `make local-restart` | DBを保持して全体を再ビルド・再起動。migration・seedは実行しない |
| `make local-frontend` | 起動済み環境の画面変更を反映 |
| `make local-status` / `make local-logs` | 状態・ログを確認 |
| `make local-stop` | DB volumeを保持して停止 |
| `make local-frontend-check` | Frontendのlint・型チェック・test・build |
| `make local-test` | Backend・Frontend・host scriptの品質確認一式 |

画面は`http://localhost:8080/`、`/operator`、`/moderator`です。
health確認は`/api/health/live`と`/api/health/ready`を使います。
通常の`local-test`に含まれるAlembic確認はoffline SQL生成であり、DBへ適用しません。
複数worktreeからも同じローカルCompose projectを操作するため、同時に起動・更新しないでください。
実行内容は[Makefile](../../Makefile)と[test-local.sh](../../scripts/test-local.sh)で確認できます。

## 年度が変わるときに確認する場所

| 項目 | 編集先・注意点 |
| --- | --- |
| 実問題・正解・配信候補 | [問題集JSON](../../content/event-questions.json)。[編集手順](../QUESTION_CONFIGURATION.md)に従う |
| ローカルの架空問題 | [demo-questions.json](../../backend/src/quiz_app/bootstrap/demo-questions.json) |
| 開始待ち・結果表示・採点・時間許容差 | [quiz-settings.json](../../backend/src/quiz_app/quiz_configuration/quiz-settings.json)と[設定手順](../QUIZ_CONFIGURATION.md) |
| 開催日時・終了後リンク | `VITE_EVENT_START_AT`、`VITE_SURVEY_URL`、`VITE_HOME_URL`。変更後はgatewayを再ビルド |
| 年度表記・画像・案内文 | 参加者・運営・司会者の各画面。素材の公開条件は[公開準備](PUBLIC_RELEASE.md)を確認 |
| 公開URL・証明書・GitHub/AWS管理者 | [AWS手順](../GITHUB_ACTIONS_AWS.md)。現担当者の設定を次年度へそのまま流用しない |

アンケートを表示する年は、ローカルの `VITE_SURVEY_URL` とAWS配置用のGitHub Environment variableにその年度のURLを設定します。未設定なら終了画面のアンケートリンクは表示されません。

通常の出題構成は練習1問＋本番5問の計6問で、予備を含めると計8問です。実際に登録する問題数と出題順は、使用する問題JSONで確認してください。
実際に配信した先頭1問だけが練習となり、総合得点から除外されます。
問題JSONと設定JSONは初期投入の入力であり、編集・再起動だけでは既存開催のDBを更新しません。
過去の得点を再計算せず、来年の問題・設定は新しい開催の準備として扱います。
問題数、採点、時間の値は[設定手順](../QUIZ_CONFIGURATION.md)と実際に配置する版を照合してください。

## 非公開で引き継ぐ情報と未確認の実績

次の情報は、公開本文へ実値を記載せず、管理者が利用できる非公開の保管先で引き継ぎます。

- GitHub、AWS、DNS・証明書の管理主体、権限移管先、緊急時の連絡先。
- 運営認証情報、session secret、backup鍵の保管先と取得・更新方法。
- 実参加者データ、回答・得点、運用ログ、backupの保管場所・保存期限・削除責任者。
- 本番の日時、使用commitとimage digest、実参加人数、問題・設定snapshot、障害と対応の記録。
- 現在残っているAWS資源と費用、撤去の実行結果、DNSと証明書の管理状況。

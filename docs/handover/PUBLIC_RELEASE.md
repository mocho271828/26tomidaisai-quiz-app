# 公開する資料と、委員会内で引き継ぐもの

このリポジトリには、開催準備と当日の運営手順を残す。AWSの接続先や運営パスワードを次年度の担当者に渡すときは、委員会が管理する非公開の経路を使う。

## 次年度に渡す設定

- GitHubでAWS配置を実行できるリポジトリと担当者、AWSアカウント、IAM role、独自ドメインとDNSの管理先は、担当交代時に管理者が確認する。[設定項目](../GITHUB_ACTIONS_AWS.md)
- 運営パスワード、DB接続情報、バックアップ復号鍵、実参加者の回答と得点は個別に引き継ぐ。バックアップを残す場合は、鍵の保管先と削除担当者も合わせて確認する。[当日の運用と終了後の片付け](OPERATIONS.md)
- アンケートは開催ごとにURLを設定する。画面構成は[デザイン資料](../design/README.md)、設営時の動きは[準備中ページの運用](../XSERVER_MAINTENANCE.md)を参照する。

## 公開範囲の確認状況

過去のGit履歴には当時のサンプル鍵に対応する開催バックアップと個人のメールアドレスがあり、デプロイのActionsログには実際の接続先が含まれる。履歴を含めて公開する場合は、ほかのブランチやタグ、Issue・PRの本文や添付、Actionsのログや成果物も確認する。[GitHubの可視性変更の説明](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/managing-repository-settings/setting-repository-visibility)

過去のコミットには F66Macaron の Regular と Bold が残っている。第三者への再配布許諾は未確認のため、これらのコミットを含めて公開する場合は管理者が許諾を確認する。

実問題と正解、トミー、ロゴの再配布許諾とコードの利用条件は未確認。公開前に管理者が確認する。[フォントと素材の利用条件](../design/README.md)・[Noto Sans JPのライセンス](../../frontend/public/licenses/NotoSansJP-OFL.txt)・[Phosphorのライセンス](../design/PHOSPHOR-LICENSE.txt)

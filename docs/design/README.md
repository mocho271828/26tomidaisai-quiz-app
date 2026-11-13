# 画面デザインと素材

参加者・運営・司会者画面の色、文字サイズ、余白は、委員会の共通デザインルールを反映した [tokens.css](../../frontend/src/styles/tokens.css) で管理する。

- 参加者の回答画面は、設問と選択肢を同じ画面に収め、選択中にページがスクロールしないようにする。参加者向けの小さい文字は原則14px以上にする。[参加者画面のCSS](../../frontend/src/styles/participant.css)
- 参加者の正誤・受付状態は、色に加えて文字やアイコンでも示す。運営のダッシュボードは進行操作と回答状況をまとめ、司会者画面は横向きの表示を前提にする。
- 富大祭2026のロゴは、委員会指定の画像（756×228）を各画面で共用する。縦横比を維持し、切り抜きや色変更をしない。[ロゴ画像](../../frontend/src/assets/tomidaisai-2026-logo.png) · [表示コンポーネント](../../frontend/src/quiz/_components/FestivalLogo.tsx)

## 素材の出典と利用条件

| 素材 | 出典・取り扱い |
| --- | --- |
| 案にゃい猫トミー | 利用者から本アプリ用に提供されたPNGを、透過を保ったWebPに変換した。紙吹雪のSVGは指定Figma「ゲスト企画」のノード `2:383` から書き出した。[素材](../../frontend/src/assets/characters/) · [ポーズと代替テキストの指定](../../frontend/src/quiz/_components/TommyMascot.tsx) |
| Noto Sans JP | Google Fonts配布の可変フォントをWOFF2に変換し、本文用に同梱している。配布時はSIL Open Font License 1.1の通知を添える。[配布元](https://github.com/google/fonts/tree/main/ofl/notosansjp) · [ライセンス全文](../../frontend/public/licenses/NotoSansJP-OFL.txt) |
| 見出し・数字の書体 | 委員会のAdobe Fontsキット `bqk8omc` を読み込む。利用ドメインのキット登録状況は未確認。[読み込み設定](../../frontend/index.html) |
| アイコン | UIには既存依存の `@phosphor-icons/react` を使用する。[Phosphor IconsのMITライセンス](PHOSPHOR-LICENSE.txt) |

トミーとロゴの利用確認は富大祭の本アプリを対象としている。第三者への再配布許諾の確認状況は引き継ぎ資料に記載している。[公開範囲の確認状況](../handover/PUBLIC_RELEASE.md)

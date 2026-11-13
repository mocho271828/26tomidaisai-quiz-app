# Xserverの準備中ページ

対象は次回の企画用サブドメイン。以下の `quiz.example.org` は説明用の例。AWS設営前に「サイトは只今準備中です。」を表示する。

アップロードするのは、[配信用フォルダ](../deploy/xserver-maintenance/)の **`index.html`と`assets/`フォルダ**。既存参加画面と同じデザイントークン、ロゴ、トミー、本文フォントを同梱している。ビルド・JavaScript・外部配信サービス・アプリサーバーは不要。この手順書やリポジトリ全体を公開フォルダへ置かない。スマートフォン向けの可変幅、日本語の見出し、検索結果への掲載を抑える`noindex`を設定済み。自動更新は行わない。

`assets/`は`frontend/src/styles/tokens.css`、`frontend/src/assets/tomidaisai-2026-logo.png`、`frontend/src/assets/characters/tommy-greeting.webp`、`frontend/src/assets/fonts/NotoSansJP-Variable.woff2`、`frontend/public/licenses/NotoSansJP-OFL.txt`の無加工コピー。共通デザインを更新する際は元ファイルから再コピーする。本文と見出しは既存のNoto Sans JPを利用し、見出し用Adobe Fontsが未読込の場合と同じフォールバックにする。

## 1. XserverのIPアドレスと公開先を確認

1. Xserverのサーバーパネル →「サーバー情報」で、契約サーバーの**IPアドレス**を控える。AWSのIPや、親サイトのDNSから推測したIPを使わない。
2. 「サブドメイン設定」で`example.org`を選び、`quiz`を追加する。すでに存在する場合は再作成せず、設定を確認する。
3. 画面に表示されるドキュメントルート（公開フォルダ）を控える。例えば`/example.org/public_html/quiz.example.org/`や`/example.org/public_html/quiz/`になるが、実際に選択したパスを優先する。
4. DNS切替前に無料SSLの自動設定が失敗した場合は、手順3で改めて設定する。HTTPS転送は証明書の有効性を確認してから有効にする。
5. ファイルマネージャまたはSFTPで、その公開フォルダ直下に`index.html`と`assets/`をアップロードする。既存ファイルは保存してから置き換え、親サイトの`public_html/index.html`を上書きしない。既存の`index.php`や`.htaccess`がある場合は、別サイトが入っていないか確認し、無断で削除しない。

公式: [サブドメイン設定](https://www.xserver.ne.jp/manual/man_domain_subdomain_setting.php)

## 2. このサブドメインのDNSをXserverへ向ける

切替前に対象サブドメインの配信用レコード、用途、TTLを確認し、切戻し用に値を控える。

サーバーパネル →「DNSレコード設定」→ `example.org`。対象レコードを次の形へ変更する。

| ホスト名欄 | 種別 | 内容 | TTL |
| --- | --- | --- | --- |
| `quiz` | A | 手順1で確認したXserverのIPアドレス | 3600 |
| `www.quiz` | A | 同じXserverのIPアドレス | 3600 |

- `quiz`に配信用CNAMEがある場合は、Aレコードに置き換える。同じ名前のAとCNAMEは併存させない。画面で種別を変更できない場合は、対象CNAMEだけを削除してAを追加する。
- サブドメイン追加時に正しいAが自動作成されていれば、重複追加せず確認する。入力欄が`.example.org`を補う場合、ドメイン部分を二重に入力しない。
- `www.quiz`は無料SSLのwwwあり・なし双方の確認に備える設定。既存レコードがあれば用途を確認する。同名に別サーバー向けのAAAAがある場合も、IPv6だけ別サイトへ到達しないよう設定を確認してから進める。
- **親ドメイン、`www.example.org`、他サブドメイン、MX・TXT・NS、`_…quiz.example.org`のACM検証用CNAMEは変更しない。** ネームサーバーの変更も不要。

公式: [DNSレコードの編集](https://www.xserver.ne.jp/manual/man_domain_dns_setting.php)

## 3. 無料独自SSLを有効にする

1. DNS反映後、サーバーパネル →「SSL設定」で**`quiz.example.org`**を選び、無料独自SSLをONにする。すでにONなら証明書と表示を確認する。
2. Xserver公式では、DNSをカスタマイズしている場合、対象のwwwあり・なし両方のAが契約サーバーを参照する必要がある。エラー時は両レコードと設定反映を確認し、DNS認証が案内された場合は画面指定の名前・値を使う。認証値を推測しない。
3. SSL設定の反映を待ち、`https://quiz.example.org/`で証明書警告がなく準備中ページが表示されることを確認する。
4. HTTPS確認後、対象サブドメインの設定に「HTTPS転送」があればONにする。見当たらなければ、**対象サブドメインの公開フォルダ内**の`.htaccess`へ以下を追記する。既存設定はバックアップし、消さずに先頭へ追記する。親サイトの`.htaccess`は編集しない。

```apache
RewriteEngine On
RewriteCond %{HTTPS} !on
RewriteRule ^ https://quiz.example.org%{REQUEST_URI} [R=301,L]
```

準備中HTMLをブラウザに長期保存させたくない場合は、同じ`.htaccess`へ次も追加する。Xserver側のキャッシュ設定でHTMLをキャッシュしている場合は、対象の設定と実際のレスポンスヘッダーも確認する。

```apache
<IfModule mod_headers.c>
  <Files "index.html">
    Header set Cache-Control "no-store"
  </Files>
</IfModule>
```

SSL反映は公式案内で最大1時間程度。サブドメイン設定は半日程度かかる場合もある。DNSキャッシュ（変更前のレコードのTTL）とは別の待ち時間なので、変更後1時間で必ず完了すると判断しない。

公式: [無料独自SSL設定](https://www.xserver.ne.jp/manual/man_server_ssl.php)・[常時SSL化](https://www.xserver.ne.jp/manual/man_server_fullssl.php)

## 4. 公開確認とAWSへの切替

MacのターミナルでDNSとHTTP応答を確認できる。

```sh
dig +short A quiz.example.org
dig +short CNAME quiz.example.org
dig +short A www.quiz.example.org
curl -I https://quiz.example.org/
curl -I http://quiz.example.org/
```

Xserver配信中はAが契約IP、CNAMEは空、HTTPSは200、HTTPはHTTPSへの転送が期待値。ヘッダーだけで完了とせず、PCとスマートフォンで本文も確認する。サブドメイン直下以外の任意URLやAPIを案内ページへ転送する設定は含めていない。

本番切替前に、今回のCloudFrontに独自ドメインのAliasと対応する`us-east-1`のACM証明書が設定され、アプリが稼働していることを確認する。その後、**`quiz`のAを、今回発行されたCloudFrontドメインへのCNAMEに置き換える**。`https://`やパスは値に含めず、過去のCloudFront名を流用しない。ACM検証用CNAMEは残す。[既存AWS公開手順](GITHUB_ACTIONS_AWS.md#証明書とdnsの準備)も参照する。

古いDNSを保持する端末に備えてXserverのHTMLを切替直後に消さない。`www.quiz`は別ホストなので自動では切り替わらない。今回追加したwww用Aを本番後に残すか解除するかも確認し、CloudFrontのAlias・証明書にwwwを追加せずwwwまでCloudFrontへ向けない。正式案内URLはwwwなしを使う。切替後は独自URLでHTTPS、参加・運営ログイン・回答・WebSocketを確認する。

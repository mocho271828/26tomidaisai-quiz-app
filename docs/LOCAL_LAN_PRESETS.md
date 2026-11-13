# WANなしルーターで行うLANブラウザ試験

固定LANプリセットではDHCP予約に合わせ、アプリPCを `10.0.1.2`、負荷生成PCを `10.0.1.3`、接続先を `https://10.0.1.2:8443` に固定する。両PCをルーターのLANポートにつなぐ。WANとポート転送は不要。

Codexの実行メニューにある `LAN このPC` はアプリ側、`LAN 別PC` は負荷生成側で実行する。同じcommitのリポジトリを別PCにも用意すると同じプリセットを利用できる。ターミナルではrepository rootで以下の `make` を使う。

## LANを接続する前に行う準備

Internetを使える状態で、依存・Chromium・Docker imageを取得する。両PCにNode/pnpm、このPCにuvと起動済みのDockerが必要。プリセットはネットワーク設定・DB schema・参加者ID poolを変更しない。

### このPC（10.0.1.2）

```sh
make lan-server-prepare
```

通常HTTP project `gs-app-tomidaisai` のCompose解決済み設定から、現在のDB資格情報・session・backup設定を維持した `.local/lan-server/runtime.env` を作る。既存volume `gs-app-tomidaisai_postgres-data` が必要で、別の過去TLS DBを選ばない。CAとLAN IPを含む証明書を生成し、TLS用のAPI/gatewayをbuild、PostgreSQL/Redis imageを取得する。この段階では通常HTTPを停止せず、TLSも起動しない。

作業treeはcleanにする。buildしたcommitとimage IDを記録し、準備後に変わった場合は起動を拒否する。既存のLAN envと証明書は再生成しない。通常 `.env` の資格情報を後で変更してもLAN envへ自動追従しないので、変更した場合は保存済みLAN設定を所有者が見直す。

次を実行すると、今回のCAだけをこのMacのlogin keychainへSSL用に信頼登録する。macOSの認証ダイアログが出る場合がある。プリセットを選ぶ操作が明示的な登録操作であり、prepareや起動に混ぜない。

```sh
make lan-server-ca-trust
```

ブラウザを再起動する。登録時のSHA-256指紋を保管する。CA30日・server証明書14日の有効期間があるため、後日の試験では期限も確認する。

### 別PC（10.0.1.3）

このPCと同じcommitをcheckoutし、次を実行する。秘密値を含む `.env` や `.local/lan-server`、`node_modules` を丸ごとコピーしない。

```sh
make lan-client-prepare
```

このPCの `.local/lan-client/` にある **`ca.crt` と `server.json` の2ファイルだけ**を、USB/AirDropなどで別PCの同じrepository内の `.local/lan-client/` へコピーする。コピー先の親directoryは必要なら `mkdir -p .local/lan-client` で作る。`server.json` は接続先、build時のcommit、CA指紋だけで、資格情報は含まない。署名付き配置証明ではないため、この2ファイルは同じ信頼できる経路で渡す。

このPCで表示されたCA指紋と、別PCで次のコマンドが表示する指紋を照合する。

```sh
openssl x509 -in .local/lan-client/ca.crt -noout -fingerprint -sha256
make lan-client-ca-trust
```

ブラウザを再起動する。準備後にアプリを再buildして転送ファイルが更新された場合は、2ファイルとcheckoutを揃え直す。

## LAN接続でInternetにつながらなくなる場合

WANなし有線LANがデフォルト経路として優先された可能性がある。次はIPと経路・macOSのサービス順を表示するだけで、接続設定を変更しない。LAN切断中にも実行できる。

```sh
make lan-network
```

Wi-FiでInternetを維持する場合は、macOSの「システム設定 → ネットワーク → アクションメニュー → サービスの順序を設定」で、InternetにつながるWi-Fiを有線LANより上にする。VPNや両方のネットワークで重複するサブネットも診断対象になる。設定変更は所有者が行い、プリセットでWi-Fiの停止やdefault route/DNSの変更をしない。[Appleのサービス順の説明](https://support.apple.com/ja-jp/guide/mac-help/mchlp2711/mac)

有線LANはDHCPを使い、予約どおりこのPC=`10.0.1.2`、別PC=`10.0.1.3`になったことを確認する。ルーターのサブネットマスクは実際のDHCP設定に従い、IPだけから `/24` と決めつけない。`make lan-network` の `10.0.1.2` / `10.0.1.3` への経路が有線interfaceを向くことを確認する。試験中はInternetがなくてもよいが、LANへの疎通は必要。

## LAN接続後の起動と確認

このPCで順番に実行する。

```sh
make lan-server-start
make lan-server-status
make lan-server-check
```

`start` はlocal Docker、設定、buildした版、現在のIP、volume所有情報、既存TLS projectのDBを確認してから、通常HTTPを停止し、**同じDB volume**をTLS project `gs-app-tomidaisai-tls` で起動する。DBは削除・初期化しない。DBを使っている別projectがあれば拒否し、同じDBを2つのPostgreSQLから同時起動しない。LAN起動はbuild/pullをせず、未準備imageはエラーになる。

途中失敗時は通常HTTPが停止済みの場合がある。原因を確認し、必要なら下記の停止・通常復帰を行う。自動rollbackやDB修復は行わない。readiness成功と、未使用開催・6問・[現行schema revision](../backend/src/quiz_app/schema_revision.py)・参加者ID poolの準備完了は別である。[人手DB準備](handover/README.md#参加者idの準備)に従い、使用済み開催を新規試験として扱わない。

このPCで運営画面 `https://10.0.1.2:8443/operator` を開く。運営資格情報は既存DBのものを使い、別PCへ渡さない。別PCでは以下を実行する。

```sh
make lan-client-check
```

転送したcommitとrunner側HEAD、CA指紋、HTTPS readiness、実際にrunnerが使うChromiumのTLS接続と参加ボタンを確認する。**参加登録はしない**。CA明示のNode HTTPSが成功してもChromiumの信頼が未設定なら失敗する。証明書検証を無効にして通さない。

## 600VUを起動して手動操作する

このPCの別terminalでサーバー計測を開始する（最大60分。試験終了時はCtrl+C）。観測はread-onlyで、runごとの新しい `.local/lan-observations/` に保存する。長い試験では `LAN_OBSERVE_SECONDS=7200 make lan-server-observe` のように、runnerの準備時間と実行時間を覆う長さへ変更する（上限7200秒）。

```sh
make lan-server-observe
```

別PCの対話terminalで起動する。

```sh
make lan-client-600
```

毎回 `.local/lan-runs/` に新run・0600の匿名照合鍵・設定・証跡を作る。100VU×6 worker、既定15分、手動参加者0人、運営接続予定1。手動参加者も加える場合は `LAN_MANUAL_PARTICIPANTS=1 make lan-client-600`、実行時間は `LAN_DURATION_SECONDS=1800 make lan-client-600` のように明示する。TLSの差替えを避けるため、非空の `NODE_OPTIONS` と証明書検証を無効にする環境変数を指定した起動は拒否する。

準備完了後は同じterminalへ入力する。

```text
status
join --spread 30s
answer --round 1 --when-open --delay 500ms
```

運営画面で問題を配信し、該当問の回答予約を入れてから手動で開始する。2〜6問も問番号を変えて命令する。最後は `stop` またはCtrl+Cでworker/browserを回収し、証跡を閉じる。別のプリセットからstdin命令を送る機能はない。[詳細操作・レポート・DB照合](LOCAL_BROWSER_CONTROL.md)

`make lan-client-50` は別試験用。**同じ開催で50VU→600VUを続けると先行参加者がDBに残る**。run名を新しくしてもDBは空にならない。600人の分母や全6問試験に影響するため、未使用開催の準備は試験前に人が確認する。今回のプリセットにresetを含めない。600VUの設定対応を実測成功と扱わない。

## 終了・通常HTTPへ戻す

先に別PCのrunnerを `stop` で終了し、サーバー観測も終了する。このPCでTLSを停止する。

```sh
make lan-server-stop
```

この操作もDBを保持する。通常HTTPへ戻す場合だけ、その後で `make local-restart` を実行する。通常再起動にはbuildが含まれるため、必要ならInternetへ戻してから行う。**TLS稼働中に `local-restart` や `local-db-reset` を実行しない。**

各Macで今回のCAを解除する。

```sh
# このPC
make lan-server-ca-remove
# 別PC
make lan-client-ca-remove
```

解除は対応するCAのSHA-256でlogin keychainを指定し、他CAやSystem keychainを対象にしない。再試験では有効期間内の同じCAを再登録できる。証明書を更新する場合は旧CAの解除と証跡保管後に古い `.local/lan-server` / `.local/lan-client` を別名へ退避してからprepareし、既存DB volumeを削除しない。

## Linux / Windowsで将来実行する場合

Linux nativeではNode/pnpmに加えてPlaywrightのOS依存が必要で、`lan-client-prepare` は `install --with-deps chromium` を使う。CA登録/解除プリセットはmacOS専用なので、LinuxではOSとChromiumのNSS DBへ所有者が登録する。[Chromiumの証明書管理](https://chromium.googlesource.com/chromium/src/+/master/docs/linux/cert_management.md)

Windows nativeは鍵所有者検査・swap計測・子process回収が未対応。WSL2のLinux Node/Chromiumを使う案は未実測である。WSL内では物理LANの `10.0.1.3` が自身のinterfaceに現れない場合があり、現在のMakeプリセットのIP guardを通らない。その場合はWindows側のIPとWSLからの経路を確認したうえで、WSLのrepo rootから `pnpm --dir load-test/browser lan:check` / `lan:600` を使う。repo・鍵・証跡は `/home` 配下に置き、CAはWSL側にも設定する。資源計測はLinux VMの値でありWindows PC全体ではない。[MicrosoftのWSL説明](https://learn.microsoft.com/ja-jp/windows/wsl/networking)

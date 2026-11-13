# 手元4台で800 VUを生成するセットアップ手順

本書は設定・操作手順であり、WSL2での動作確認や4台800 VU・全問の実測成功を示す記録ではない。

負荷生成用のクラウドVMを借りず、手元のMac 2台とWindows 2台から、起動済みの専用AWS公開アプリへ各200 VUを接続する。試験対象アプリをローカルへ移す手順ではない。
既存Runnerの仕様は[AWSブラウザ負荷試験](AWS_BROWSER_LOAD_TEST.md)、操作・計測の詳細は[共通Runner手順](LOCAL_BROWSER_CONTROL.md)を参照する。

## 前提と構成

独自ドメインを使う場合は、各Runner端末のシェルで `export AWS_REVIEW_DOMAIN_NAME=quiz.example.org` の例を今回のホスト名に置き換えて設定する。CloudFront標準URLは今回の配置出力から取得する。

| PC | Runnerの実行場所 | 予定人数 |
| --- | --- | ---: |
| MacBook Air M2 / RAM 16GB | macOSのターミナル | 200 VU |
| MacBook Air M4 / RAM 16GB | macOSのターミナル | 200 VU |
| Windows Core i5 / RAM 16GB | WSL2のUbuntu | 200 VU |
| Windows Ryzen 7 / RAM 16GB | WSL2のUbuntu | 200 VU |
| 合計 | 独立した4つのRunner | 800 VU |

- 各台200 VUは目標値であり、CPUの世代・メモリ・発熱・通信条件によっては処理しきれない。準備・同時回答時のCPU、メモリ、swap、遅延、欠測を確認する。
- macOSは14以降、WindowsはWSL2を利用できるWindows 10 version 2004 / build 19041以降またはWindows 11を前提とする。Windows nativeでRunnerを実行しない。
- 各PCでGitHubのmainを取得できること。WindowsではGit・Node.js・pnpmをすべてUbuntu内で使う。
- 試験対象は、所有者が確認した現在の公開URLで稼働し、未使用の試験専用開催、練習1問＋本番5問＋予備2問の計8問、現行開催設定`quiz-2026-10-23-v1`、800人分以上の未使用参加者ID枠、運営アカウントが準備済みであること。手動参加者がいる場合はその分の枠も必要。
- 本書のRunner準備にDocker・AWS CLIは不要。AWS配置・DB初期化・migration・ID pool準備は行わない。必要な人手作業は[参加者IDの準備手順](handover/README.md#参加者idの準備)と[AWS確認環境手順](GITHUB_ACTIONS_AWS.md)で別に確認する。
- コマンドは上から順に実行し、エラーが出た段階で原因を解消する。初回準備はインターネット接続が必要。

## 1. Mac 2台の初回準備

それぞれのMacのターミナルで実行する。

```sh
xcode-select --install
```

インストール画面が出た場合は完了まで待つ。「すでにインストール済み」なら続ける。

```sh
git --version
make --version
uname -m
touch "$HOME/.zshrc"
```

M2・M4では`uname -m`が`arm64`になるターミナルを使う。手順3へ進む。

## 2. Windows 2台の初回準備

### PowerShell: WSL2とUbuntuを導入する

管理者として開いたPowerShellで実行する。Ubuntu 24.04を導入済みならinstallは省略する。

```powershell
wsl --install -d Ubuntu-24.04
```

再起動を要求された場合はWindowsを再起動し、PowerShellを開き直す。Ubuntuが自動で開いた場合はユーザー名・パスワードの初期設定を済ませ、Ubuntuで次を実行してPowerShellへ戻る。

```sh
exit
```

以下はPowerShellで実行する。

```powershell
wsl --update
wsl --set-default-version 2
wsl --list --verbose
```

Ubuntu-24.04の`VERSION`が`1`の場合だけ実行する。

```powershell
wsl --set-version Ubuntu-24.04 2
```

### PowerShell: WSLのメモリ上限を設定する

16GBのPCではまず12GBを上限とし、Windows側にも余力を残す。これは200 VUを保証する値ではない。Windows側のメモリ逼迫もタスクマネージャーで確認する。

```powershell
notepad "$env:USERPROFILE\.wslconfig"
```

新規ファイルなら次の内容で保存する。既存設定がある場合は他の設定を残し、既存の`[wsl2]`内の`memory`を変更する。CPU数は固定せず既存設定を確認する。

```ini
[wsl2]
memory=12GB
```

WSL上の作業を終了してから設定を反映する。`wsl --shutdown`は他のWSL環境も停止するため、Runner起動後には実行しない。

```powershell
wsl --shutdown
wsl -d Ubuntu-24.04
```

Linux用のユーザー名・パスワードをまだ作成していない場合は、初回起動時に作成する。

### Ubuntu: 基本ツールを導入する

ここからは開いたUbuntuのターミナル内で実行する。

```sh
sudo apt-get update
sudo apt-get install -y git make curl ca-certificates libatomic1

git --version
make --version
uname -s
free -h
```

`uname -s`が`Linux`になることを確認する。以後、Windowsの2台では手順3以降をUbuntu内で実行する。WSL内の資源値はWindows全体の使用量を示すものではない。

## 3. 全4台: Node.jsとpnpmを導入する

macOS・Ubuntuで共通。Node.js 24系と、確認時のリポジトリ指定であるpnpm 11.15.1を使う。
nvmが未導入の場合に実行する。

```sh
curl -fsSL https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.8/install.sh | bash
```

続けて全4台で実行する。既存nvmを別の場所へ導入済みの場合は、その導入先で読み込む。

```sh
export NVM_DIR="$HOME/.nvm"
. "$NVM_DIR/nvm.sh"

nvm install 24
nvm use 24
npm install --global pnpm@11.15.1

node --version
pnpm --version
node -p 'process.platform + " " + process.arch'
```

Node.jsは`v24...`、pnpmは`11.15.1`、Macは`darwin arm64`、WindowsのUbuntuは`linux x64`を確認する。ここでのnpmはpnpm自体の導入用であり、プロジェクト依存はpnpmで導入する。

## 4. 全4台: mainを取得する

VU用のチェックアウトを`$HOME/vu/gs-app-tomidaisai`に置く。WindowsではLinux側ホーム配下を使い、`/mnt/c`・共有フォルダ・symlink配下を避ける。Runnerは保存先と親の所有者・書込み権限も検査する。

### 初めて取得する場合

`read` で入力待ちになったら委員会が使うリポジトリのclone URLを貼り付ける。

```sh
umask 077
mkdir -p "$HOME/vu"
chmod 700 "$HOME/vu"

read -r REPOSITORY_URL
git clone --branch main \
  "$REPOSITORY_URL" \
  "$HOME/vu/gs-app-tomidaisai"

cd "$HOME/vu/gs-app-tomidaisai"
git status --short
git rev-parse HEAD
```

### 取得済みの場合

```sh
cd "$HOME/vu/gs-app-tomidaisai"
git status --short
```

未コミット変更がないことを確認してから更新する。変更がある場合は、削除や強制resetで進めない。

```sh
git switch main
git pull --ff-only origin main
git status --short
git rev-parse HEAD
```

本手順では比較条件をそろえるため、4台のHEADを一致させる。これはRunner自身の起動条件ではなく、試験対象アプリの配置版を証明するものでもない。配置commit・image digestは配置記録で別途確認する。

## 5. 全4台: RunnerとChromiumを準備する

```sh
cd "$HOME/vu/gs-app-tomidaisai"
export NVM_DIR="$HOME/.nvm"
. "$NVM_DIR/nvm.sh"
nvm use 24

unset NODE_OPTIONS NODE_TLS_REJECT_UNAUTHORIZED DURATION_SECONDS
make aws-browser-prepare
```

依存パッケージ・Chromiumの取得とRunnerのビルドを行う。確認時のPlaywrightは1.63.0。mainで依存指定が変わった場合は`load-test/browser/package.json`とlockfileを正本とし、独自に最新版へ更新しない。

WindowsのUbuntu 2台だけ、続けてOS側のライブラリを導入する。sudoのパスワードはUbuntuのパスワードを使う。

```sh
pnpm --dir load-test/browser exec playwright install-deps chromium
```

全4台でChromium単体の起動を確認する。この確認では試験対象へ接続しない。

```sh
pnpm --dir load-test/browser exec node --input-type=module -e '
import { chromium } from "playwright";
const browser = await chromium.launch({ headless: true });
console.log("Chromium起動成功:", browser.version());
await browser.close();
'
```

`Chromium起動成功:`と表示されればブラウザ起動の準備は完了。200 VUや公開URLのHTTPS/WSS疎通を確認したことにはならない。

## 6. 試験前: 電源・スリープを設定する

全4台を電源につなぎ、ふたを開けたまま使う。生成機には試験対象のアプリ・DBを同居させず、不要なアプリを終了して余力を確保する。

Macでは別のターミナルを1つ開き、次を実行したままにする。

```sh
caffeinate -is
```

WindowsではPowerShellで電源設定を開き、電源接続時のスリープを一時的に「なし」にする。元の設定を控えておく。

```powershell
Start-Process "ms-settings:powersleep"
```

Runnerの操作用ターミナルは別に開いて維持する。`nohup`やバックグラウンド化を使わず、対話入力を保持する。

## 7. 全4台: 接続先を指定して200 VUを起動する

Macは通常のターミナル、WindowsはUbuntu内で実行する。

```sh
cd "$HOME/vu/gs-app-tomidaisai"
export NVM_DIR="$HOME/.nvm"
. "$NVM_DIR/nvm.sh"
nvm use 24

unset NODE_OPTIONS NODE_TLS_REJECT_UNAUTHORIZED DURATION_SECONDS
ulimit -n 4096
```

FD上限の変更に失敗した場合は`ulimit -Sn`と`ulimit -Hn`で上限を確認し、解消してから起動する。`NODE_OPTIONS`へheap拡張オプションを加えたり、TLS検証を無効にしたりしない。

次を実行すると入力待ちになる。現在使う参加者画面のURLを入力してEnterを押す。4台とも同じURLにする。

```sh
printf '参加者画面のURLを入力してください（末尾の / なし）: '
read -r VU_BASE_URL
export VU_BASE_URL
```

受理される形式は現在の配置が発行した`https://d……cloudfront.net`、または`https://quiz.example.org`。`/operator`・末尾の`/`・queryを付けない。過去配置のURLを現在の稼働先とみなさない。

```sh
curl --fail --silent --show-error \
  --connect-timeout 10 --max-time 30 \
  --output /dev/null \
  --write-out 'HTTP %{http_code}\n' \
  "$VU_BASE_URL/"
```

`HTTP 200`を確認する。これはページ取得の確認だけであり、HTTPS/WSS・参加・回答は事前の実端末確認で別に確かめる。試験専用開催の準備が済んだら、全4台で同じコマンドを実行する。以下は予備を含む全8問の試験例。通常の練習1問＋本番5問までの試験なら、起動前に全台で`EXPECTED_ROUNDS=6`へ変更する。実施後の問数に合わせて予定を縮めない。

```sh
make aws-browser-runner \
  BASE_URL="$VU_BASE_URL" \
  VUS=200 \
  EXPECTED_ROUNDS=8 \
  RECORD=true \
  WORKER_SIZE=50 \
  MEASUREMENT_LEVEL=minimal \
  DETAILED_VU_COUNT=3 \
  HOST_SAMPLE_INTERVAL_MS=3000 \
  SETUP_TIMEOUT_SECONDS=900 \
  OPERATION_TIMEOUT_MS=30000 \
  MANUAL_PARTICIPANTS=0 \
  EXPECTED_OPERATOR_CONNECTIONS=1 \
  CHANNEL=chromium \
  HEADLESS=true
```

各台で`準備完了 200 VU / 4 workers。joinで参加します。`の表示を待つ。起動直後はページ・参加ボタン・フォントの準備だけを行い、自動参加しない。4台とも準備完了になってから次へ進む。

`VUS=800`を1台へ指定しない。現行の1 runの設定範囲は50〜650であり、800は4台の予定人数の合計である。手動参加者がいる場合は`MANUAL_PARTICIPANTS`を実施前に指定するが、機械VUの人数には加算しない。

## 8. 全4台: 参加させる

ここからはシェルではなく、起動中のRunnerへ入力するコマンド。4台それぞれで入力する。

```text
status
join --spread 120s
```

約120秒に分散して各200人が参加する。参加処理が落ち着いてから、各台で再確認する。

```text
status
```

各PCの50人ずつの4 workerで`joined: 50`・`connected: 50`・`failed: 0`・`stale: false`を確認し、操作失敗・timeout・切断も確認する。命令配布の完了だけで全員登録済みと判断しない。

全体800同時接続は同じ時間帯のサーバー側参加者WS数でも確認する。異なる時刻のPC別peakを足さず、運営や手動参加者を機械VUへ加算しない。内部metricsの取得経路は[AWS手順の結果の読み方](AWS_BROWSER_LOAD_TEST.md#保存した結果の読み方)を参照し、metricsを公開しない。

## 9. 各問: 4台へ回答を予約する

運営者がブラウザで同じ公開URLの`/operator`を開き、運営アカウントでログインする。passwordやtokenをコマンド引数へ記載しない。

1. 運営画面で「問題を確定して配信」する。
2. 4台すべてのRunnerへ、その問の回答予約を入力する。
3. 運営画面で開始を予約する。
4. 回答締切・集計後、運営画面で「正解・結果を公開」を押す。
5. 4台の`status`で、その問のクリック・受付・結果件数を確認する。
6. 次の問へ進む。

第1問の回答予約:

```text
answer --round 1 --when-open --delay 500ms
```

結果公開後の確認:

```text
status
```

残りの予約コマンドは以下。その問を配信したタイミングで1行だけ入力し、全行をまとめて貼り付けない。

```text
answer --round 2 --when-open --delay 500ms
answer --round 3 --when-open --delay 500ms
answer --round 4 --when-open --delay 500ms
answer --round 5 --when-open --delay 500ms
answer --round 6 --when-open --delay 500ms
answer --round 7 --when-open --delay 500ms
answer --round 8 --when-open --delay 500ms
```

| 画面上の問題 | --round |
| --- | ---: |
| 練習第1問 | 1 |
| 本番第1問 / 第2問 / 第3問 | 2 / 3 / 4 |
| 本番第4問 / 第5問 | 5 / 6 |
| 予備の本番第6問 / 第7問 | 7 / 8 |

1台への入力は他の3台へ伝わらない。各workerの該当問で`clicked`・`accepted`・`results`を確認し、未回答の結果表示を回答成功としない。終了後の`report.json`では各問の`answeredResults`と全問完了の`allRoundsBrowserComplete`も確認する。ブラウザ内DOMクリックで通常のReact送信経路を通す試験であり、マウス移動・クリック位置判定を再現するものではない。DB保存照合とは別の観測である。

## 10. 終了・記録確認

全問の結果を確認後、4台それぞれへ入力する。

```text
status
stop
```

全台で終了表示が出るまで待つ。`RECORD=true`なので、終了時に表示されるディレクトリへ結果を保存する。終了コード0だけで800同時接続・全問成功としない。

シェルに戻った後、reportの場所を確認する。

```sh
cd "$HOME/vu/gs-app-tomidaisai"
find .local/aws-browser-runs -type f -name report.json -print
```

保存先は`.local/aws-browser-runs/<run-id>/<run-id>-control/`。
`report.json`、`rounds.csv`、`connections.csv`、`resources.csv`、`termination.json`を各台で確認する。run親の`identity.key`を共有・表示しない。
4台の自動合算reportや複数PCのDB照合機能はない。予定人数・同時接続・記録完全性・全問完了・DB確認の有無を分けて残す。

Macでは`caffeinate -is`を動かしていたターミナルでCtrl+Cを押す。Windowsは電源設定を元に戻す。VUのstopやPCの終了はAWSアプリの停止・撤去ではない。環境所有者が必要に応じて[AWS撤去手順](GITHUB_ACTIONS_AWS.md#確認が終わったら撤去する)を別途扱う。

## 中断・再試験と2回目以降

- 中断時は全4台へ`stop`を入力する。Ctrl+CやstdinのEOFも当該PCの停止処理へ入るが、他PCは自動停止しない。
- `pause`は新しい参加・回答を保留し、`resume`で再開する。WS・結果観測は継続し、アプリの回答締切は止まらない。
- 同じRunnerの人数を途中変更できない。再起動は新run・新Cookieとなるため、同じ開催へ再joinすると追加参加者になる。50→100→200の予備試験は別の試験開催とID枠を人が準備し、既存記録を削除・自動resetして合わせ込まない。
- 現行review環境のsession期限は参加/loginから120分。準備完了後に参加し、試験計画上の余裕として参加開始から90分以内の全問終了を目安にする。90分はRunnerの制限ではなく、stopまで継続する設定も認証期限を延長しない。
- 2回目以降は、試験中のRunnerが終了していることを確認し、手順4のmain更新、手順5の依存・build確認、手順6〜10を実施する。main取得だけで稼働アプリは更新されない。

## 根拠

- [Makefile](../Makefile)、[Browser Runner依存](../load-test/browser/package.json)、[OS条件](../load-test/browser/src/aws.ts)、[URL条件](../load-test/browser/src/config.ts)、[保存先権限](../load-test/browser/src/awsPreset.ts)
- [Microsoft: WSL導入](https://learn.microsoft.com/en-us/windows/wsl/install)、[WSL設定](https://learn.microsoft.com/en-us/windows/wsl/wsl-config)、[Linux側へのファイル配置](https://learn.microsoft.com/en-us/windows/wsl/interop)
- [nvm公式導入手順](https://github.com/nvm-sh/nvm#installing-and-updating)、[pnpm対応環境](https://pnpm.io/installation#compatibility)
- [Playwright対応環境](https://playwright.dev/docs/intro#system-requirements)、[Linux依存導入](https://playwright.dev/docs/browsers#install-system-dependencies)

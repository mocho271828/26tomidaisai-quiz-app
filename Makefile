.DEFAULT_GOAL := help
.PHONY: local-env
local-env:
	python3 ./scripts/init-local-env.py

.PHONY: help local-restart local-repair local-db-reset local-frontend local-frontend-build local-frontend-check local-test local-status local-logs local-stop

help:
	@printf '%s\n' \
	  'make local-env            初回の .env と専用バックアップ鍵を作成' \
	  'make local-restart        全体を再ビルド・再起動（DBを保持、2 API）' \
	  'make local-repair         Colima接続を修復して全体を再起動' \
	  'make local-db-reset       ローカルDBを全削除してDemo・ID割当を再作成' \
	  'make local-frontend       画面だけを再ビルドしてlocalhostへ反映' \
	  'make local-frontend-build 画面の型チェック・dist生成のみ' \
	  'make local-frontend-check Frontendのlint・型・test・build' \
	  'make local-test           Backend/Frontendの品質確認一式' \
	  'make local-status         ローカルcontainerの状態' \
	  'make local-logs           ログを追跡（Ctrl+Cで終了）' \
	  'make local-stop           ローカルcontainerを停止・削除（DBを保持）'
	@printf '\nLAN試験: このPC=10.0.1.2 / 別PC=10.0.1.3（docs/LOCAL_LAN_PRESETS.md）\n'
	@printf '%s\n' \
	  'make lan-network  有線IP・通信経路を確認' \
	  'make lan-server-prepare  Internet接続中に準備（DB保持、起動なし）' \
	  'make lan-server-ca-trust  macOS login keychainへ今回CAを登録' \
	  'make lan-server-start  10.0.1.2で通常HTTP停止→HTTPS起動' \
	  'make lan-server-check  HTTPSとreadinessを確認' \
	  'make lan-server-status  TLS projectのcontainer状態' \
	  'make lan-server-observe  別terminalで試験中の資源・APIを観測' \
	  'make lan-server-stop  TLS停止。通常へ戻すにはlocal-restart' \
	  'make lan-server-ca-remove  今回CAだけをlogin keychainから削除' \
	  'make lan-client-prepare  Internet接続中にrunner依存・build' \
	  'make lan-client-ca-trust  macOS login keychainへ今回CAを登録' \
	  'make lan-client-check  10.0.1.3から証明書・版・実画面を確認' \
	  'make lan-client-50  新run/鍵/証跡で手動runner50VU' \
	  'make lan-client-600  新run/鍵/証跡で手動runner600VU' \
	  'make lan-client-ca-remove  今回CAだけをlogin keychainから削除'
	@printf '\n可変VU Browser Runner（docs/LOCAL_BROWSER_CONTROL.md）\n'
	@printf '%s\n' \
	  'make browser-runner-prepare  初回の依存・Chromium準備とbuild' \
	  'make browser-runner-new VUS=100  新run・鍵・設定・VU計画の作成のみ' \
	  'make browser-runner VUS=100 DURATION_SECONDS=1200  作成して手動Runner起動' \
	  '設定: BASE_URL EXPECTED_ROUNDS WORKER_SIZE MEASUREMENT_LEVEL DETAILED_VU_COUNT' \
	  '      HOST_SAMPLE_INTERVAL_MS SETUP_TIMEOUT_SECONDS OPERATION_TIMEOUT_MS' \
	  '      MANUAL_PARTICIPANTS EXPECTED_OPERATOR_CONNECTIONS CHANNEL HEADLESS'
	@printf '\nAWS向け2PC Browser Runner（docs/AWS_BROWSER_LOAD_TEST.md）\n'
	@printf '%s\n' \
	  'make aws-browser-prepare  各PCの依存・Chromium準備' \
	  'make aws-browser-runner VUS=300 BASE_URL=... RECORD=true  stopまで運転' \
	  '通常はEXPECTED_ROUNDS=6（練習1問＋本番5問）、予備込み全問は8を明示。省略時は旧既定7。' \
	  '別PCは同じコマンドのRECORD=falseで結果保存なし。各PCで独立操作。'

local-restart:
	./scripts/restart-local.sh

local-repair:
	./scripts/restart-local.sh --repair-colima

local-db-reset:
	./scripts/reset-local-db.sh

local-frontend:
	./scripts/restart-local.sh --frontend-only

local-frontend-build:
	pnpm --dir frontend install --frozen-lockfile
	pnpm --dir frontend build

local-frontend-check:
	pnpm --dir frontend install --frozen-lockfile
	pnpm --dir frontend lint
	pnpm --dir frontend typecheck
	pnpm --dir frontend test
	pnpm --dir frontend build

local-test:
	./scripts/test-local.sh

local-status local-logs local-stop:
	./scripts/local-compose.sh $(@:local-%=%)

LAN_PRESETS := network server-prepare server-ca-trust server-start server-check server-status server-observe server-stop server-ca-remove client-prepare client-ca-trust client-check client-50 client-600 client-ca-remove
.PHONY: $(addprefix lan-,$(LAN_PRESETS))
$(addprefix lan-,$(LAN_PRESETS)):
	./scripts/lan-test.sh $(@:lan-%=%)

.PHONY: browser-runner-prepare browser-runner-build browser-runner-new browser-runner
browser-runner-prepare:
	pnpm --dir load-test/browser install --frozen-lockfile
	pnpm --dir load-test/browser exec playwright install chromium
	pnpm --dir load-test/browser build

browser-runner-build:
	pnpm --dir load-test/browser build

# 値をshellコードへ展開せず、環境変数として渡して既存Zod schemaで検証する。
browser-runner-new browser-runner: export VUS ?= 50
browser-runner-new browser-runner: export EXPECTED_ROUNDS ?= 6
browser-runner-new browser-runner: export BASE_URL ?= https://127.0.0.1:8443
browser-runner-new browser-runner: export DURATION_SECONDS ?= 900
browser-runner-new browser-runner: export WORKER_SIZE ?= 100
browser-runner-new browser-runner: export MEASUREMENT_LEVEL ?= normal
browser-runner-new browser-runner: export DETAILED_VU_COUNT ?= 3
browser-runner-new browser-runner: export HOST_SAMPLE_INTERVAL_MS ?= 3000
browser-runner-new browser-runner: export SETUP_TIMEOUT_SECONDS ?= 900
browser-runner-new browser-runner: export OPERATION_TIMEOUT_MS ?= 30000
browser-runner-new browser-runner: export MANUAL_PARTICIPANTS ?= 0
browser-runner-new browser-runner: export EXPECTED_OPERATOR_CONNECTIONS ?= 1
browser-runner-new browser-runner: export CHANNEL ?= chromium
browser-runner-new browser-runner: export HEADLESS ?= true

browser-runner-new: browser-runner-build
	pnpm --dir load-test/browser runner:new

browser-runner: browser-runner-build
	pnpm --dir load-test/browser runner:new --start

.PHONY: aws-browser-prepare aws-browser-runner
aws-browser-prepare: browser-runner-prepare

# AWS用だけを独立設定とし、既存local用targetの既定値を変更しない。
aws-browser-runner: export AWS_REVIEW_DOMAIN_NAME ?=
aws-browser-runner: export BASE_URL ?=
aws-browser-runner: export VUS ?= 300
aws-browser-runner: export EXPECTED_ROUNDS ?= 7
aws-browser-runner: export RECORD ?= true
aws-browser-runner: export WORKER_SIZE ?= 50
aws-browser-runner: export MEASUREMENT_LEVEL ?= minimal
aws-browser-runner: export DETAILED_VU_COUNT ?= 3
aws-browser-runner: export HOST_SAMPLE_INTERVAL_MS ?= 3000
aws-browser-runner: export SETUP_TIMEOUT_SECONDS ?= 900
aws-browser-runner: export OPERATION_TIMEOUT_MS ?= 30000
aws-browser-runner: export MANUAL_PARTICIPANTS ?= 0
aws-browser-runner: export EXPECTED_OPERATOR_CONNECTIONS ?= 1
aws-browser-runner: export CHANNEL ?= chromium
aws-browser-runner: export HEADLESS ?= true
aws-browser-runner: browser-runner-build
	pnpm --dir load-test/browser aws:run

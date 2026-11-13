import { executeLan, lanFailureCode, parseLanAction } from "./lanClient.js";

async function main(): Promise<void> {
  const result = await executeLan(parseLanAction(process.argv.slice(2)), process.platform, process.env);
  if (result.kind === "checked") process.stdout.write("LAN確認成功: 転送metadataとlocal HEADの一致・CA・HTTPS readiness・Chromium参加画面。参加操作はしていません。\n");
  else process.stdout.write(`LAN試験を終了しました。証跡: ${result.directory}\n`);
}

main().catch((error: unknown) => {
  const code = lanFailureCode(error);
  process.stderr.write(`${code}; ${code === "lan_platform_unsupported" ? "Windows nativeは非対応です。MacまたはLinuxを使用してください。" :
    ".local/lan-client入力、同じcommit、CA信頼、接続先、設定と依存を確認してください。"}\n`);
  process.exitCode = 1;
});

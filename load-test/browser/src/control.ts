import { parseArgs } from "node:util";
import { cliVusSchema } from "./controlCommands.js";
import { readControlSettings } from "./controlPlan.js";
import { superviseControl } from "./controlSupervisor.js";

async function main(): Promise<void> {
  const {values} = parseArgs({options: {vus: {type: "string"}, config: {type: "string"}}});
  if (!values.config || !values.vus) throw new Error("usage_control_vus_config");
  const count = cliVusSchema.parse(values.vus);
  const directory = await superviseControl(await readControlSettings(values.config), count);
  process.stdout.write(`終了しました。report.json / rounds.csv: ${directory}\n`);
}
main().catch(() => { process.stderr.write("control_failed; 設定・依存・非秘密の証跡を確認してください\n"); process.exitCode = 1; });

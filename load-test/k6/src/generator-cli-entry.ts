import { realpathSync } from "node:fs";
import { fileURLToPath } from "node:url";

import { main } from "./generator-cli.js";

if (
  process.argv[1] !== undefined &&
  realpathSync(process.argv[1]) === realpathSync(fileURLToPath(import.meta.url))
) {
  process.exitCode = await main();
}

export { main };

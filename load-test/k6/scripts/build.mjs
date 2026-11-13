import { build } from "esbuild";
import { realpathSync, rmSync } from "node:fs";
import { URL, fileURLToPath } from "node:url";

// 廃止済みentrypointの生成物を配布対象へ残さない。
rmSync(new URL("../dist/", import.meta.url), { recursive: true, force: true });

await build({
  bundle: true,
  entryPoints: {
    contracts: "src/contracts.ts",
    "generator-cli": "src/generator-cli-entry.ts",
    "inspect-fixture": "tests/fixtures/inspect-runtime.ts",
    launcher: "src/runtime.ts",
    "metrics-summary": "src/generator/metrics-summary.ts",
  },
  format: "esm",
  logLevel: "info",
  outdir: "dist",
  platform: "node",
  sourcemap: false,
  target: "node22",
});

await build({
  bundle: true,
  entryPoints: {
    generator: "src/generator/k6-entry.ts",
    "summary-smoke": "tests/fixtures/summary-smoke.ts",
  },
  external: ["k6", "k6/*"],
  format: "esm",
  logLevel: "info",
  outdir: "dist",
  platform: "neutral",
  sourcemap: false,
  target: "es2022",
});

const bundle = await import(new URL("../dist/launcher.js", import.meta.url));
if (typeof bundle.runK6GeneratorRuntime !== "function") {
  throw new Error("runtime_bundle_entrypoint_missing");
}
const cliBundle = await import(new URL("../dist/generator-cli.js", import.meta.url));
if (typeof cliBundle.main !== "function") {
  throw new Error("generator_cli_bundle_entrypoint_missing");
}
const expectedRoot = realpathSync(
  fileURLToPath(new URL("../../..", import.meta.url)),
);
if (bundle.runtimeRepositoryRoot() !== expectedRoot) {
  throw new Error("runtime_bundle_root_invalid");
}

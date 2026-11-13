import { canonicalJson } from "./canonical-json.js";
import { FormalDockerRefusal } from "./formal-docker.js";
import { FormalRuntimeRefusal } from "./formal-runtime.js";
import type {
  K6GeneratorRuntimeOptions,
  K6GeneratorRuntimeResult,
} from "./formal-runtime.js";
import { runK6GeneratorRuntime } from "./formal-runtime.js";
import { GeneratorArmRefusal } from "./generator-arm.js";
import { LauncherRefusal } from "./launcher.js";
import { PreparationRefusal } from "./preparation.js";

type GeneratorRuntime = (
  options: K6GeneratorRuntimeOptions,
) => Promise<K6GeneratorRuntimeResult>;

export interface GeneratorCliOutput {
  write(value: string): void;
}

export class GeneratorCliArgumentError extends Error {
  constructor() {
    super("generator_cli_arguments_invalid");
    this.name = "GeneratorCliArgumentError";
  }
}

/** argvの値や例外詳細を出さないstrict 4-flag CLI。 */
export async function runGeneratorCli(
  argv: readonly string[],
  standardOutput: GeneratorCliOutput,
  standardError: GeneratorCliOutput,
  runtime: GeneratorRuntime = runK6GeneratorRuntime,
): Promise<number> {
  try {
    const options = parseGeneratorCliArguments(argv);
    const result = await runtime(options);
    standardOutput.write(`${canonicalJson(successPayload(result))}\n`);
    return 0;
  } catch (error: unknown) {
    const isArgumentError = error instanceof GeneratorCliArgumentError;
    standardError.write(
      `${canonicalJson({
        code: safeErrorCode(error),
        format: "load-test-generator-cli-error-v1",
        status: "failed",
      })}\n`,
    );
    return isArgumentError ? 2 : 1;
  }
}

export function parseGeneratorCliArguments(
  argv: readonly string[],
): K6GeneratorRuntimeOptions {
  if (argv.length !== 8) throw new GeneratorCliArgumentError();
  const values = new Map<string, string>();
  const flags = ["--manifest", "--output-directory", "--preparation-evidence", "--application-ca-file"];
  for (let index = 0; index < argv.length; index += 2) {
    const flag = argv[index];
    const value = argv[index + 1];
    if (flag === undefined || !flags.includes(flag) || values.has(flag) ||
        value === undefined || value.length === 0 || value.startsWith("--")) {
      throw new GeneratorCliArgumentError();
    }
    values.set(flag, value);
  }
  const manifestPath = values.get("--manifest");
  const outputDirectory = values.get("--output-directory");
  const preparationEvidencePath = values.get("--preparation-evidence");
  const applicationCaFile = values.get("--application-ca-file");
  if (manifestPath === undefined || outputDirectory === undefined ||
      preparationEvidencePath === undefined || applicationCaFile === undefined) {
    throw new GeneratorCliArgumentError();
  }
  return { manifestPath, outputDirectory, preparationEvidencePath, applicationCaFile };
}

export function main(): Promise<number> {
  return runGeneratorCli(process.argv.slice(2), process.stdout, process.stderr);
}

function successPayload(result: K6GeneratorRuntimeResult): object {
  return {
    actual_spawn_at: result.actualSpawnAt,
    arm_marker: result.armMarker,
    artifacts: result.artifacts,
    container_name: result.containerName,
    event_id: result.eventId,
    format: result.format,
    generator_bundle: result.generatorBundle,
    generator_image: result.generatorImage,
    manifest_sha256: result.manifestSha256,
    metrics_artifact: result.metricsArtifact,
    run_id: result.runId,
    scenario_sha256: result.scenarioSha256,
    scheduled_start_at: result.scheduledStartAt,
    status: "completed",
  };
}

function safeErrorCode(error: unknown): string {
  if (error instanceof GeneratorCliArgumentError) {
    return "generator_cli_arguments_invalid";
  }
  if (
    error instanceof PreparationRefusal ||
    error instanceof FormalDockerRefusal ||
    error instanceof FormalRuntimeRefusal ||
    error instanceof GeneratorArmRefusal ||
    error instanceof LauncherRefusal
  ) {
    return error.code;
  }
  return "generator_cli_failed";
}

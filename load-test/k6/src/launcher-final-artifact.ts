import { sealArtifact } from "./artifacts.js";
import type { ArtifactIdentity } from "./artifacts.js";
import {
  GENERATOR_METRICS_ARTIFACT_NAME,
  MAX_GENERATOR_METRICS_ARTIFACT_BYTES,
} from "./generator/metrics-summary-contract.js";
import type { GeneratorStreamProcessor } from "./launcher.js";

export type LauncherFinalArtifactRefusalCode =
  | "final_artifact_metrics_incomplete"
  | "final_artifact_stream_invalid";

export class LauncherFinalArtifactRefusal extends Error {
  readonly code: LauncherFinalArtifactRefusalCode;

  constructor(code: LauncherFinalArtifactRefusalCode) {
    super(code);
    this.name = "LauncherFinalArtifactRefusal";
    this.code = code;
  }
}

/** 正常終了後にだけprocessorから最終summaryを取り出し、狭い上限でsealする。 */
export function sealFinalMetricsArtifact(
  processor: GeneratorStreamProcessor,
  outputDirectory: string,
  isRequired: boolean,
): ArtifactIdentity | null {
  if (!isRequired) {
    return null;
  }
  const completed = processor.takeFinalArtifact?.() ?? null;
  if (completed === null) {
    throw new LauncherFinalArtifactRefusal("final_artifact_metrics_incomplete");
  }
  if (
    completed.name !== GENERATOR_METRICS_ARTIFACT_NAME ||
    !(completed.body instanceof Uint8Array)
  ) {
    throw new LauncherFinalArtifactRefusal("final_artifact_stream_invalid");
  }
  return sealArtifact({
    outputDirectory,
    name: completed.name,
    body: completed.body,
    maximumBytes: MAX_GENERATOR_METRICS_ARTIFACT_BYTES,
  });
}

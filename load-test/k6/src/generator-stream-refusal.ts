import type { CompletedRoundArtifact } from "./launcher.js";

/**
 * 同一stdout chunkの後半を拒否しても、前半で完結したRoundを失わずlauncherへ渡す。
 */
export class GeneratorStreamProcessingRefusal extends Error {
  readonly completedArtifacts: readonly CompletedRoundArtifact[];

  constructor(
    message: string,
    completedArtifacts: readonly CompletedRoundArtifact[] = [],
  ) {
    super(message);
    this.name = "GeneratorStreamProcessingRefusal";
    this.completedArtifacts = Object.freeze([...completedArtifacts]);
  }
}

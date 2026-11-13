import { canonicalJson } from "./canonical-json.js";
import { RAMP_READY_ARTIFACT_NAME, RAMP_READY_DEADLINE_MILLISECONDS, SessionConnectionSchema } from "./generator/ramp-readiness.js";
import type { CompletedFinalArtifact } from "./launcher.js";
import type { LoadedManifest } from "./manifest.js";

/** 接続の累積回数ではなくVUごとの最新open/closeを追跡する。 */
export class GeneratorRampReadiness {
  readonly #loaded: LoadedManifest;
  readonly #states = new Map<number, { connected: boolean; observedAt: number }>();
  #wasSealed = false;

  constructor(loaded: LoadedManifest) { this.#loaded = loaded; }

  accept(payload: unknown): boolean {
    const parsed = SessionConnectionSchema.safeParse(payload);
    if (!parsed.success) return false;
    const event = parsed.data;
    const start = Date.parse(this.#loaded.manifest.scheduled_start_at);
    const previous = this.#states.get(event.vu_id);
    if (event.run_id !== this.#loaded.manifest.run_id ||
        event.event_id !== this.#loaded.manifest.event.id ||
        event.manifest_sha256 !== this.#loaded.hashes.manifestSha256 ||
        event.observed_at_unix_ms < start ||
        event.observed_at_unix_ms > start + 4_800_000 ||
        (previous !== undefined && (event.observed_at_unix_ms < previous.observedAt ||
          event.websocket_connected === previous.connected))) return false;
    this.#states.set(event.vu_id, { connected: event.websocket_connected, observedAt: event.observed_at_unix_ms });
    return true;
  }

  takeArtifact(observedAt = Date.now()): CompletedFinalArtifact | null {
    const start = Date.parse(this.#loaded.manifest.scheduled_start_at);
    const rampEnd = start + this.#loaded.manifest.scenario.lifecycle.ramp_end_seconds * 1_000;
    if (this.#wasSealed || !Number.isSafeInteger(observedAt) ||
        observedAt < rampEnd || observedAt > start + RAMP_READY_DEADLINE_MILLISECONDS ||
        this.#states.size !== 600 ||
        [...this.#states.values()].some((state) => !state.connected || state.observedAt > observedAt)) {
      return null;
    }
    // ramp終了後の取出時に再評価し、同じchunk内の後続closeを取りこぼさない。
    this.#wasSealed = true;
    return {
      name: RAMP_READY_ARTIFACT_NAME,
      body: new TextEncoder().encode(`${canonicalJson({
        format: "load-test-generator-ramp-ready-v1", run_id: this.#loaded.manifest.run_id,
        event_id: this.#loaded.manifest.event.id, manifest_sha256: this.#loaded.hashes.manifestSha256,
        sessions: 600, websocket_connected: 600, observed_at_unix_ms: observedAt,
      })}\n`),
    };
  }
}

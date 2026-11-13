import { describe, expect, it } from "vitest";
import { GeneratorRampReadiness } from "../src/ramp-readiness.js";
import { loadedManifest } from "./generator-event-fixtures.js";

const loaded = loadedManifest();
const start = Date.parse(loaded.manifest.scheduled_start_at);
function connection(vuId: number, connected = true, offset = 600_000) {
  return {
    format: "load-test-generator-session-connection-v1", run_id: loaded.manifest.run_id,
    event_id: loaded.manifest.event.id, manifest_sha256: loaded.hashes.manifestSha256,
    vu_id: vuId, session_checked: true, websocket_connected: connected,
    observed_at_unix_ms: start + offset,
  };
}

describe("ramp connection evidence", () => {
  it("累積600接続ではsealせず、切断VUの再接続で同時600をsealする", () => {
    const tracker = new GeneratorRampReadiness(loaded);
    for (let vuId = 1; vuId <= 599; vuId += 1) expect(tracker.accept(connection(vuId))).toBe(true);
    expect(tracker.accept(connection(1, false, 600_001))).toBe(true);
    expect(tracker.accept(connection(600, true, 600_002))).toBe(true);
    expect(tracker.takeArtifact(start + 600_002)).toBeNull();
    expect(tracker.accept(connection(1, true, 601_000))).toBe(true);
    const artifact = tracker.takeArtifact(start + 601_000);
    expect(artifact?.name).toBe("generator-ramp-ready.json");
    expect(JSON.parse(new TextDecoder().decode(artifact?.body))).toEqual({
      format: "load-test-generator-ramp-ready-v1", run_id: loaded.manifest.run_id,
      event_id: loaded.manifest.event.id, manifest_sha256: loaded.hashes.manifestSha256,
      sessions: 600, websocket_connected: 600, observed_at_unix_ms: start + 601_000,
    });
    expect(tracker.takeArtifact(start + 601_000)).toBeNull();
  });
  it("VU重複・未確認session・別run・timestamp逆行を拒否する", () => {
    const tracker = new GeneratorRampReadiness(loaded);
    expect(tracker.accept(connection(1))).toBe(true);
    expect(tracker.accept(connection(1))).toBe(false);
    expect(tracker.accept(connection(1, false, 599_999))).toBe(false);
    expect(tracker.accept({ ...connection(2), session_checked: false })).toBe(false);
    expect(tracker.accept({ ...connection(2), run_id: "33333333-3333-4333-8333-333333333333" })).toBe(false);
  });
  it("T0+660秒を超えた600到達では証拠を発行しない", () => {
    const tracker = new GeneratorRampReadiness(loaded);
    for (let vuId = 1; vuId <= 600; vuId += 1) tracker.accept(connection(vuId, true, 660_001));
    expect(tracker.takeArtifact(start + 660_001)).toBeNull();
  });
  it("ramp終了前の600接続を保持しても、終了前または後続close後はsealしない", () => {
    const tracker = new GeneratorRampReadiness(loaded);
    for (let vuId = 1; vuId <= 600; vuId += 1) tracker.accept(connection(vuId, true, 599_000));
    expect(tracker.takeArtifact(start + 599_999)).toBeNull();
    expect(tracker.accept(connection(1, false, 600_000))).toBe(true);
    expect(tracker.takeArtifact(start + 600_001)).toBeNull();
    expect(tracker.accept(connection(1, true, 601_000))).toBe(true);
    const artifact = tracker.takeArtifact(start + 601_100);
    expect(JSON.parse(new TextDecoder().decode(artifact?.body)).observed_at_unix_ms).toBe(start + 601_100);
  });
  it("T0+600以前の最後の接続後に追加eventがなくても、hostの再観測でsealする", () => {
    const tracker = new GeneratorRampReadiness(loaded);
    for (let vuId = 1; vuId <= 600; vuId += 1) tracker.accept(connection(vuId, true, 599_000));
    const artifact = tracker.takeArtifact(start + 600_500);
    expect(JSON.parse(new TextDecoder().decode(artifact?.body)).observed_at_unix_ms).toBe(start + 600_500);
  });
});

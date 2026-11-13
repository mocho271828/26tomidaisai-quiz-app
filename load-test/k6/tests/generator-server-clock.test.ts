import { describe, expect, it } from "vitest";

import type { TimeResponse } from "../src/generator/api-contracts.js";
import { GeneratorServerClock } from "../src/generator/server-clock.js";
import type { GeneratorHttpResult } from "../src/generator/session-contracts.js";
import { GeneratorProtocolRefusal } from "../src/generator/session-contracts.js";

function success(
  serverTimeMs: number,
  startedAtLocalMs: number,
  endedAtLocalMs: number,
): GeneratorHttpResult<TimeResponse> {
  return {
    kind: "success",
    value: { server_time: new Date(serverTimeMs).toISOString() },
    startedAtLocalMs,
    endedAtLocalMs,
  };
}

function unknown(
  startedAtLocalMs: number,
  endedAtLocalMs: number,
): GeneratorHttpResult<TimeResponse> {
  return { kind: "unknown", startedAtLocalMs, endedAtLocalMs };
}

describe("generator server clock", () => {
  it("同期前は時刻を返さず、3標本の最短RTTをmidpoint補正して採用する", () => {
    const clock = new GeneratorServerClock();

    expect(clock.isSynchronized).toBe(false);
    expect(clock.now(0)).toBeNull();
    expect(
      clock.apply(
        [
          success(10_000, 0, 120),
          success(20_000, 120, 130),
          success(30_000, 130, 190),
        ],
        190,
        500,
      ),
    ).toBe(true);

    // 最短標本は130ms時点でserver 20,005ms、採用時190msまで60ms進む。
    expect(clock.isSynchronized).toBe(true);
    expect(clock.now(190)).toBe(20_065);
    expect(clock.now(290)).toBe(20_165);
  });

  it("失敗標本を除外し、RTT 500msは受理して500ms超では同期を解除する", () => {
    const clock = new GeneratorServerClock();

    expect(
      clock.apply(
        [unknown(0, 10), success(40_000, 100, 600), unknown(600, 610)],
        610,
        500,
      ),
    ).toBe(true);
    expect(clock.now(610)).toBe(40_260);

    expect(
      clock.apply(
        [success(50_000, 1_000, 1_501), unknown(1_501, 1_510)],
        1_510,
        500,
      ),
    ).toBe(false);
    expect(clock.isSynchronized).toBe(false);
    expect(clock.now(1_510)).toBeNull();
  });

  it("再同期のserver時刻が古くても利用中の時計を後退させない", () => {
    const clock = new GeneratorServerClock();
    expect(clock.apply([success(40_000, 100, 100)], 100, 500)).toBe(true);
    expect(clock.now(200)).toBe(40_100);

    expect(clock.apply([success(10_000, 200, 210)], 210, 500)).toBe(true);
    expect(clock.now(210)).toBe(40_110);
    expect(clock.now(250)).toBe(40_150);
    expect(clock.now(200)).toBe(40_110);
  });

  it("有限でないlocal clockと不正RTT境界を固定codeで拒否する", () => {
    const clock = new GeneratorServerClock();

    expect(() => clock.now(Number.NaN)).toThrowError(
      new GeneratorProtocolRefusal("generator_clock_invalid"),
    );
    expect(() => clock.apply([], 0, -1)).toThrowError(
      new GeneratorProtocolRefusal("generator_clock_invalid"),
    );
    expect(() => clock.apply([], Number.POSITIVE_INFINITY, 500)).toThrowError(
      new GeneratorProtocolRefusal("generator_clock_invalid"),
    );
  });
});

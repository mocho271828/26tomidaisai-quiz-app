import { describe, expect, it } from "vitest";

import {
  advanceNondecreasingMilliseconds,
  isScenarioStartWithinTolerance,
} from "../src/generator/k6-runtime-policy.js";

describe("k6 VU clock policy", () => {
  it("wall clockの後退を直前値へ丸める", () => {
    expect(advanceNondecreasingMilliseconds(2_000, 1_999)).toBe(2_000);
    expect(advanceNondecreasingMilliseconds(2_000, 2_001)).toBe(2_001);
  });

  it.each([
    [-1, 0],
    [0, -1],
    [0.5, 1],
    [0, Number.NaN],
    [0, Number.POSITIVE_INFINITY],
  ])("不正なclock座標 (%s, %s) を拒否する", (previous, observed) => {
    expect(advanceNondecreasingMilliseconds(previous, observed)).toBeNull();
  });
});

describe("k6 scenario start policy", () => {
  const scheduledStart = 2_000_000;

  it.each([-1_000, -999, 0, 999, 1_000])(
    "許容境界内の差分 %i ms を受け入れる",
    (difference) => {
      expect(
        isScenarioStartWithinTolerance(
          scheduledStart,
          scheduledStart + difference,
          1_000,
        ),
      ).toBe(true);
    },
  );

  it.each([-1_001, 1_001])("境界外の差分 %i ms を拒否する", (difference) => {
    expect(
      isScenarioStartWithinTolerance(
        scheduledStart,
        scheduledStart + difference,
        1_000,
      ),
    ).toBe(false);
  });

  it.each([Number.NaN, Number.POSITIVE_INFINITY, 2_000_000.5])(
    "不正なscenario開始時刻 %s を拒否する",
    (scenarioStart) => {
      expect(
        isScenarioStartWithinTolerance(scheduledStart, scenarioStart, 1_000),
      ).toBe(false);
    },
  );
});

import { describe, expect, it } from "vitest";

import {
  PARTICIPANT_PUBLIC_LABEL_CAPACITY,
  PARTICIPANT_PUBLIC_LABEL_PATTERN,
  participantPublicLabelForOrdinal,
} from "../src/participant-public-label.js";

describe("participant public label", () => {
  it.each([
    [1, "AAA"],
    [26, "AAZ"],
    [27, "ABA"],
    [600, "AXB"],
    [PARTICIPANT_PUBLIC_LABEL_CAPACITY, "ZZZ"],
  ])("1始まりの順序 %i を3文字base-26へ写像する", (ordinal, expected) => {
    expect(participantPublicLabelForOrdinal(ordinal)).toBe(expected);
  });

  it("600 VUへ一意な大文字3文字を決定論的に割り当てる", () => {
    const labels = Array.from({ length: 600 }, (_, index) =>
      participantPublicLabelForOrdinal(index + 1),
    );

    expect(new Set(labels).size).toBe(600);
    expect(labels.every((label) => PARTICIPANT_PUBLIC_LABEL_PATTERN.test(label))).toBe(true);
  });

  it.each([0, PARTICIPANT_PUBLIC_LABEL_CAPACITY + 1, 1.5, Number.NaN])(
    "範囲外の順序 %s を通信前に拒否する",
    (ordinal) => {
      expect(() => participantPublicLabelForOrdinal(ordinal)).toThrow(
        "participant_public_label_ordinal_invalid",
      );
    },
  );
});

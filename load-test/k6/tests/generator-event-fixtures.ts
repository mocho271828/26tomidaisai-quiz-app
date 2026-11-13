import {
  LoadTestManifestSchema,
  type LoadTestManifest,
} from "../src/contracts.js";
import {
  GeneratorEventSchema,
  type GeneratorEvent,
} from "../src/events.js";
import type { LoadedManifest } from "../src/manifest.js";
import {
  PYTHON_MANIFEST_SHA256,
  PYTHON_SCENARIO_SHA256,
  UUID5_GOLDEN,
  validManifestFixture,
} from "./fixtures/manifest.js";

export const MANIFEST_SHA256 = PYTHON_MANIFEST_SHA256;
export const SCENARIO_SHA256 = PYTHON_SCENARIO_SHA256;

export function roundId(roundNumber: number): string {
  return roundNumber === 1
    ? UUID5_GOLDEN.round1
    : `33333333-3333-4333-8333-${roundNumber.toString(16).padStart(12, "0")}`;
}

export function buildManifest(): LoadTestManifest {
  return LoadTestManifestSchema.parse(validManifestFixture());
}

export function loadedManifest(): LoadedManifest {
  return {
    manifest: buildManifest(),
    hashes: {
      manifestSha256: MANIFEST_SHA256,
      scenarioSha256: SCENARIO_SHA256,
    },
  };
}

export function roundStartMilliseconds(
  manifest: LoadTestManifest,
  roundNumber: number,
): number {
  const scenario = manifest.scenario.rounds.find(
    (candidate) => candidate.number === roundNumber,
  );
  if (scenario === undefined) {
    throw new Error("fixture_round_missing");
  }
  return Date.parse(manifest.scheduled_start_at) + scenario.start_offset_seconds * 1_000;
}

export function sealTime(manifest: LoadTestManifest, roundNumber: number): string {
  return canonicalTimestamp(roundStartMilliseconds(manifest, roundNumber) + 3_000);
}

export function canonicalTimestamp(milliseconds: number): string {
  return new Date(milliseconds)
    .toISOString()
    .replace(/\.(\d{3})Z$/, (_match, fraction: string) =>
      fraction === "000" ? "Z" : `.${fraction}000Z`,
    );
}

function selectedOption(vuId: number): "A" | "B" | "C" | "D" {
  if (vuId <= 496) {
    return (["A", "B", "C", "D"] as const)[(vuId - 1) % 4] ?? "A";
  }
  return ({ 497: "A", 498: "A", 499: "B", 500: "C", 599: "B", 600: "C" } as const)[
    vuId as 497 | 498 | 499 | 500 | 599 | 600
  ];
}

export function buildEvent(
  manifest: LoadTestManifest,
  roundNumber: number,
  vuId: number,
): GeneratorEvent {
  const scenario = manifest.scenario.rounds.find(
    (candidate) => candidate.number === roundNumber,
  );
  const question = manifest.questions.find(
    (candidate) => candidate.display_order === roundNumber,
  );
  if (scenario === undefined || question === undefined) {
    throw new Error("fixture_round_missing");
  }
  const shouldAnswer = scenario.answer_population === "mixed"
    ? vuId <= 498 || vuId === 599 || vuId === 600
    : vuId <= 500;
  const startMilliseconds = roundStartMilliseconds(manifest, roundNumber);
  const shouldReplay = shouldAnswer && scenario.replay === "all";
  const shouldRecoverStatus =
    shouldAnswer &&
    scenario.condition === "receipt-discard" &&
    vuId >= (scenario.receipt_discard_vu_start ?? Number.POSITIVE_INFINITY) &&
    vuId <= (scenario.receipt_discard_vu_end ?? Number.NEGATIVE_INFINITY);

  return GeneratorEventSchema.parse({
    format: "load-test-generator-vu-round-event-v1",
    run_id: manifest.run_id,
    event_id: manifest.event.id,
    source_commit: manifest.artifacts.source_commit,
    schema_revision: manifest.schema_revision,
    manifest_sha256: MANIFEST_SHA256,
    scenario_sha256: SCENARIO_SHA256,
    round_number: roundNumber,
    round_id: roundId(roundNumber),
    question_id: question.id,
    scenario_condition: scenario.condition,
    answer_population: scenario.answer_population,
    vu_id: vuId,
    session_established: true,
    answer: shouldAnswer
      ? {
          expected: true,
          selected_option: selectedOption(vuId),
          request_started_at_unix_ms: startMilliseconds + 100 + (vuId > 598 ? vuId - 100 : vuId) - 1,
          resolved: true,
          duplicate: false,
          misleading_success: false,
        }
      : {
          expected: false,
          selected_option: null,
          request_started_at_unix_ms: null,
          resolved: false,
          duplicate: false,
          misleading_success: false,
        },
    replay: {
      attempted: shouldReplay,
      same_answer_id: shouldReplay,
      row_changed: false,
    },
    status_recovery: {
      receipt_discarded: shouldRecoverStatus,
      status_queried: shouldRecoverStatus,
      recovered: shouldRecoverStatus,
    },
    observed_at: canonicalTimestamp(startMilliseconds + 2_000),
  });
}

export function buildRoundEvents(
  manifest: LoadTestManifest,
  roundNumber: number,
): GeneratorEvent[] {
  return Array.from({ length: 600 }, (_, index) =>
    buildEvent(manifest, roundNumber, index + 1),
  );
}

export function encodeEventLine(event: GeneratorEvent): Uint8Array {
  return new TextEncoder().encode(`@@LOAD_TEST_EVENT@@${JSON.stringify(event)}\n`);
}

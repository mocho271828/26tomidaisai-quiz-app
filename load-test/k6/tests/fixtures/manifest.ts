import { createHash } from "node:crypto";
import { existsSync, readFileSync } from "node:fs";

import { z } from "zod";

const uuid4Schema = z.string().regex(
  /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/,
);
const sha256Schema = z.string().regex(/^[0-9a-f]{64}$/);
const contractGoldenSchema = z.strictObject({
  inputs: z.strictObject({
    run_id: uuid4Schema,
    event_id: uuid4Schema,
    event_slug: z.string(),
    scheduled_start_at: z.string(),
    public_origin: z.string(),
    source_commit: z.string(),
    api_image: z.string(),
    gateway_image: z.string(),
    generator_image: z.string(),
    scoring_policy_version: z.string(),
    scoring_base_points: z.int(),
    scoring_initial_speed_bonus: z.int(),
    scoring_speed_bucket_ms: z.int(),
    scoring_speed_decrement_per_bucket: z.int(),
    scoring_standard_duration_ms: z.int(),
    scoring_is_approved: z.literal(true),
    display_name_policy_version: z.string(),
    display_notice_version: z.string(),
    display_max_code_points: z.int(),
    display_reject_patterns_json: z.string(),
    created_at: z.string(),
  }),
  expected: z.strictObject({
    manifest_sha256: sha256Schema,
    scenario_sha256: sha256Schema,
    feff_title_manifest_sha256: sha256Schema,
    information_separator_title_manifest_sha256: sha256Schema,
    uuid5: z.strictObject({
      question1: z.string().uuid(),
      question1_option_a: z.string().uuid(),
      round1: z.string().uuid(),
    }),
  }),
});
const contractGoldenLocation = [
  new URL("../../../fixtures/python-contract-golden-v3.json", import.meta.url),
  new URL("../../fixtures/python-contract-golden-v3.json", import.meta.url),
].find((candidate) => existsSync(candidate));
if (contractGoldenLocation === undefined) {
  throw new Error("test_contract_golden_missing");
}
const CONTRACT_GOLDEN = contractGoldenSchema.parse(
  JSON.parse(readFileSync(contractGoldenLocation, "utf8")),
);
const RUN_ID = CONTRACT_GOLDEN.inputs.run_id;
const OPTION_LABELS = ["A", "B", "C", "D"] as const;

function uuid5(name: string): string {
  const namespace = Buffer.from(RUN_ID.replaceAll("-", ""), "hex");
  const digest = createHash("sha1")
    .update(namespace)
    .update(name, "utf8")
    .digest()
    .subarray(0, 16);
  digest[6] = ((digest[6] ?? 0) & 0x0f) | 0x50;
  digest[8] = ((digest[8] ?? 0) & 0x3f) | 0x80;
  const hex = digest.toString("hex");
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}

interface RoundSpec {
  readonly number: number;
  readonly start: number;
  readonly population: "mixed" | "concentrated";
  readonly virtualAnswers: number;
  readonly replay: "none" | "all";
  readonly condition:
    | "none"
    | "receipt-discard"
    | "redis-unavailable"
    | "redis-recovered"
    | "api-secondary-unavailable"
    | "api-secondary-recovered"
    | "accumulated";
}

const ROUND_SPECS: readonly RoundSpec[] = [
  { number: 1, start: 900, population: "mixed", virtualAnswers: 500, replay: "none", condition: "none" },
  { number: 2, start: 1260, population: "mixed", virtualAnswers: 500, replay: "none", condition: "none" },
  { number: 3, start: 1620, population: "concentrated", virtualAnswers: 500, replay: "all", condition: "none" },
  { number: 4, start: 1980, population: "concentrated", virtualAnswers: 500, replay: "none", condition: "receipt-discard" },
  { number: 5, start: 2340, population: "mixed", virtualAnswers: 500, replay: "none", condition: "redis-unavailable" },
  { number: 6, start: 2700, population: "mixed", virtualAnswers: 500, replay: "none", condition: "redis-recovered" },
  { number: 7, start: 3060, population: "mixed", virtualAnswers: 500, replay: "none", condition: "api-secondary-unavailable" },
  { number: 8, start: 3420, population: "concentrated", virtualAnswers: 500, replay: "none", condition: "api-secondary-recovered" },
  { number: 9, start: 3780, population: "mixed", virtualAnswers: 500, replay: "none", condition: "accumulated" },
  { number: 10, start: 4140, population: "concentrated", virtualAnswers: 500, replay: "all", condition: "none" },
];

export function validManifestFixture() {
  const questions = Array.from({ length: 10 }, (_, index) => {
    const number = index + 1;
    return {
      id: uuid5(`question:${number}`),
      version: number,
      prompt: `[LOAD-TEST] 通信確認用の架空問題${String(number).padStart(2, "0")}です。`,
      explanation: `[LOAD-TEST] 通信確認用の架空問題${String(number).padStart(2, "0")}です。`,
      display_order: number,

      options: OPTION_LABELS.map((label, displayOrder) => ({
        id: uuid5(`question:${number}:option:${label}`),
        label,
        content: `[LOAD-TEST] 架空問題${String(number).padStart(2, "0")}の選択肢${label}`,
        display_order: displayOrder,
      })),
    };
  });
  const rounds = ROUND_SPECS.map((round) => ({
    number: round.number,
    start_offset_seconds: round.start,
    answer_population: round.population,
    virtual_answer_count: round.virtualAnswers,
    expected_option_counts: { a: 126, b: 125, c: 125, d: 124 },
    replay: round.replay,
    condition: round.condition,
    receipt_discard_vu_start: round.number === 4 ? 1 : null,
    receipt_discard_vu_end: round.number === 4 ? 50 : null,
    receipt_discard_only_when_status: round.number === 4 ? "Stored" : null,
    answer_status_delay_ms: round.number === 4 ? 2500 : null,
    retry_only_when_status: round.number === 4 ? "NotStored" : null,
  }));

  return {
    format: "load-test-manifest-v3",
    run_id: RUN_ID,
    created_at: CONTRACT_GOLDEN.inputs.created_at,
    scheduled_start_at: CONTRACT_GOLDEN.inputs.scheduled_start_at,
    public_origin: CONTRACT_GOLDEN.inputs.public_origin,
    schema_revision: "20260804_0002",
    threshold_mode: "calibration",
    artifacts: {
      source_commit: CONTRACT_GOLDEN.inputs.source_commit,
      api_image: CONTRACT_GOLDEN.inputs.api_image,
      gateway_image: CONTRACT_GOLDEN.inputs.gateway_image,
      generator_image: CONTRACT_GOLDEN.inputs.generator_image,
    },
    event: {
      id: CONTRACT_GOLDEN.inputs.event_id,
      slug: CONTRACT_GOLDEN.inputs.event_slug,
      title: `[LOAD-TEST] ${CONTRACT_GOLDEN.inputs.event_slug}`,
      state: "LobbyOpen",
      ranking_visibility: "HiddenAll",
      scoring_policy: {
        version: CONTRACT_GOLDEN.inputs.scoring_policy_version,
        base_points: CONTRACT_GOLDEN.inputs.scoring_base_points,
        initial_speed_bonus: CONTRACT_GOLDEN.inputs.scoring_initial_speed_bonus,
        speed_bucket_ms: CONTRACT_GOLDEN.inputs.scoring_speed_bucket_ms,
        speed_decrement_per_bucket:
          CONTRACT_GOLDEN.inputs.scoring_speed_decrement_per_bucket,
        standard_duration_ms: CONTRACT_GOLDEN.inputs.scoring_standard_duration_ms,
        is_approved: CONTRACT_GOLDEN.inputs.scoring_is_approved,
      },
      display_name_policy: {
        version: CONTRACT_GOLDEN.inputs.display_name_policy_version,
        notice_version: CONTRACT_GOLDEN.inputs.display_notice_version,
        max_code_points: CONTRACT_GOLDEN.inputs.display_max_code_points,
        reject_patterns_json: CONTRACT_GOLDEN.inputs.display_reject_patterns_json,
      },
    },
    questions,
    initial_round: {
      id: uuid5("round:1"),
      question_id: questions[0]?.id ?? "",
      version: 1,
      state: "PreparingAssets",
      result_publication: "Withheld",
    },
    scenario: {
      lifecycle: {
        virtual_sessions: 600,
        target_total_sessions: 600,
        ramp_start_seconds: 0,
        ramp_end_seconds: 600,
        ramp_targets: [100, 300, 500, 600],
        ramp_target_offsets_seconds: [150, 300, 450, 600],
        measurement_start_seconds: 900,
        measurement_end_seconds: 4500,
        ramp_down_start_seconds: 4500,
        ramp_down_end_seconds: 4800,
        exclusive_window_confirmation_required: true,
      },
      answer_oracle: {
        modulo_vu_start: 1,
        modulo_vu_end: 496,
        modulo_option_cycle: ["A", "B", "C", "D"],
        fixed_virtual_answers: [
          { vu_id: 497, option: "A" },
          { vu_id: 498, option: "A" },
          { vu_id: 499, option: "B" },
          { vu_id: 500, option: "C" },
          { vu_id: 599, option: "B" },
          { vu_id: 600, option: "C" },
        ],
        mixed_virtual_vu_end: 498,
        mixed_additional_vu_ids: [599, 600],
        concentrated_virtual_vu_end: 500,
        non_answering_vu_start: 501,
        non_answering_vu_end: 598,
        expected_option_counts: { a: 126, b: 125, c: 125, d: 124 },
      },
      protocol: {
        join_token_characters: 43,
        independent_cookie_jar_per_session: true,
        independent_websocket_per_session: true,
        heartbeat_interval_seconds: 3,
        clock_samples_per_sync: 3,
        clock_sync_interval_seconds: 30,
        clock_sample_timeout_ms: 1000,
        clock_sample_selection: "shortest-rtt",
        clock_sample_max_rtt_ms: 500,
        polling_interval_seconds: 1,
        websocket_reconnect_interval_seconds: 5,
        static_asset_fetches_per_session: 1,
        refetch_projection_after_invalidation: true,
        send_readiness: true,
        query_status_after_unknown_response: true,
        client_answer_id_scheme: "uuid5(run_id,question_id:vu_id)",
      },
      thresholds: {
        sustained_total_sessions: 600,
        sustained_duration_seconds: 3600,
        normal_participant_websockets: 600,
        target_count: 600,
        normal_expected_ready_count: 600,
        ready_count_minimum: 588,
        asset_failure_maximum: 11,
        expected_answer_count: 500,
        round_start_width_max_ms: 1000,
        notification_refresh_over_one_second_maximum: 5,
        http_5xx_maximum: 0,
        recovery_seconds_maximum: 120,
        missed_rounds_maximum: 0,
        duplicate_answers_maximum: 0,
        misleading_successes_maximum: 0,
        unresolved_answers_maximum: 0,
        replay_same_answer_id_required: true,
        replay_row_change_maximum: 0,
        latency_mode: "first-run-calibration-only",
        latency_report_statistics: ["p50", "p95", "p99", "max"],
        hard_latency_threshold_ms: null,
      },
      resource_abort: {
        generator_cpu_percent_over: 80,
        generator_memory_percent_over: 80,
        scheduler_lag_ms_over: 50,
        consecutive_seconds: 30,
      },
      rounds,
      fault_schedule: [
        { target: "redis", action: "disable", offset_seconds: 2280 },
        { target: "redis", action: "enable", offset_seconds: 2580 },
        { target: "api-secondary", action: "disable", offset_seconds: 3000 },
        { target: "api-secondary", action: "enable", offset_seconds: 3300 },
      ],
    },
  };
}

export const UUID5_GOLDEN = {
  question1: CONTRACT_GOLDEN.expected.uuid5.question1,
  question1OptionA: CONTRACT_GOLDEN.expected.uuid5.question1_option_a,
  round1: CONTRACT_GOLDEN.expected.uuid5.round1,
} as const;

export const PYTHON_MANIFEST_SHA256 =
  CONTRACT_GOLDEN.expected.manifest_sha256;
export const PYTHON_SCENARIO_SHA256 =
  CONTRACT_GOLDEN.expected.scenario_sha256;
export const PYTHON_FEFF_TITLE_MANIFEST_SHA256 =
  CONTRACT_GOLDEN.expected.feff_title_manifest_sha256;
export const PYTHON_INFORMATION_SEPARATOR_TITLE_MANIFEST_SHA256 =
  CONTRACT_GOLDEN.expected.information_separator_title_manifest_sha256;

import { createHash } from "node:crypto";
import { isIP } from "node:net";

import { z } from "zod";


const LOAD_TEST_PREFIX = "[LOAD-TEST]";
const IMAGE_REFERENCE_PATTERN =
  /^[A-Za-z0-9][A-Za-z0-9._:/-]*@sha256:[0-9a-f]{64}(?![\s\S])/;
const UUID_PATTERN =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}(?![\s\S])/;
const UUID4_PATTERN =
  /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}(?![\s\S])/;
const EVENT_SLUG_PATTERN = /^[a-z0-9][a-z0-9-]{2,63}(?![\s\S])/;
const AWARE_DATETIME_PATTERN =
  /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.(\d{6}))?(Z|[+-]\d{2}:\d{2})(?![\s\S])/;

function isPythonWhitespace(codeUnit: number): boolean {
  return (
    (codeUnit >= 0x0009 && codeUnit <= 0x000d) ||
    codeUnit === 0x0020 ||
    codeUnit === 0x0085 ||
    codeUnit === 0x00a0 ||
    codeUnit === 0x1680 ||
    (codeUnit >= 0x2000 && codeUnit <= 0x200a) ||
    codeUnit === 0x2028 ||
    codeUnit === 0x2029 ||
    codeUnit === 0x202f ||
    codeUnit === 0x205f ||
    codeUnit === 0x3000
  );
}

function pythonStrip(value: string): string {
  let start = 0;
  let end = value.length;
  while (start < end && isPythonWhitespace(value.charCodeAt(start))) {
    start += 1;
  }
  while (end > start && isPythonWhitespace(value.charCodeAt(end - 1))) {
    end -= 1;
  }
  return value.slice(start, end);
}

export function stringHasLoneSurrogate(value: string): boolean {
  for (let index = 0; index < value.length; index += 1) {
    const codeUnit = value.charCodeAt(index);
    if (codeUnit >= 0xd800 && codeUnit <= 0xdbff) {
      const following = value.charCodeAt(index + 1);
      if (
        index + 1 >= value.length ||
        following < 0xdc00 ||
        following > 0xdfff
      ) {
        return true;
      }
      index += 1;
    } else if (codeUnit >= 0xdc00 && codeUnit <= 0xdfff) {
      return true;
    }
  }
  return false;
}

const safeInteger = z.number().refine(Number.isSafeInteger, "safe integer required");
const nonNegativeInteger = safeInteger.refine((value) => value >= 0, "non-negative required");
const positiveInteger = safeInteger.refine((value) => value > 0, "positive required");
const policyVersion = z.string().min(1).max(64);
const syntheticText = z
  .string()
  .transform(pythonStrip)
  .pipe(z.string().min(12).max(500).startsWith(LOAD_TEST_PREFIX));
const uuid = z.string().regex(UUID_PATTERN);
const uuid4 = z.string().regex(UUID4_PATTERN);

function parseIpv4(host: string): readonly number[] | null {
  if (isIP(host) !== 4) {
    return null;
  }
  const octets = host.split(".").map(Number);
  return octets.length === 4 && octets.every((octet) => Number.isInteger(octet))
    ? octets
    : null;
}

function isAllowedIpv4(host: string): boolean {
  const octets = parseIpv4(host);
  if (octets === null) {
    return false;
  }
  const first = octets[0] ?? -1;
  const second = octets[1] ?? -1;
  const third = octets[2] ?? -1;
  const isAwsPrivate =
    first === 10 ||
    (first === 172 && second >= 16 && second <= 31) ||
    (first === 192 && second === 168);
  if (isAwsPrivate) {
    return true;
  }
  const isPythonGlobalException =
    first === 192 &&
    second === 0 &&
    third === 0 &&
    ((octets[3] ?? -1) === 9 || (octets[3] ?? -1) === 10);
  const isNonGlobal =
    first === 0 ||
    first === 127 ||
    first >= 224 ||
    (first === 100 && second >= 64 && second <= 127) ||
    (first === 169 && second === 254) ||
    (first === 192 && second === 0 && third === 0) ||
    (first === 192 && second === 0 && third === 2) ||
    (first === 198 && (second === 18 || second === 19)) ||
    (first === 198 && second === 51 && third === 100) ||
    (first === 203 && second === 0 && third === 113);
  return isPythonGlobalException || !isNonGlobal;
}

export function canonicalAwareDatetimeMicroseconds(value: string): bigint | null {
  const match = AWARE_DATETIME_PATTERN.exec(value);
  if (match === null || !Number.isFinite(Date.parse(value))) {
    return null;
  }
  const year = Number(match[1]);
  const month = Number(match[2]);
  const day = Number(match[3]);
  const hour = Number(match[4]);
  const minute = Number(match[5]);
  const second = Number(match[6]);
  const fraction = match[7];
  const timezone = match[8] ?? "";
  const daysInMonth = new Date(Date.UTC(year, month, 0)).getUTCDate();
  const offsetIsZero = timezone === "+00:00" || timezone === "-00:00";
  const isCanonical =
    year >= 1 &&
    month >= 1 &&
    month <= 12 &&
    day >= 1 &&
    day <= daysInMonth &&
    hour <= 23 &&
    minute <= 59 &&
    second <= 59 &&
    (fraction === undefined || !/^0{6}$/.test(fraction)) &&
    !offsetIsZero;
  if (!isCanonical) {
    return null;
  }
  const milliseconds = Date.parse(value);
  const subMillisecondDigits = (fraction ?? "000000").slice(3);
  return BigInt(milliseconds) * 1_000n + BigInt(subMillisecondDigits);
}

export const AwareDatetimeSchema = z.string().refine(
  (value) => canonicalAwareDatetimeMicroseconds(value) !== null,
  "canonical aware datetime required",
);

function isCanonicalRemoteHttpsOrigin(value: string): boolean {
  try {
    const parsed = new URL(value);
    const host = parsed.hostname.toLowerCase().replace(/^\[|\]$/g, "").replace(/\.+$/, "");
    const addressKind = isIP(host);
    const isAllowedAddress =
      addressKind === 0 ||
      (addressKind === 4 && isAllowedIpv4(host));
    const isLocalHost =
      host === "localhost" ||
      host.endsWith(".localhost") ||
      host.endsWith(".local") ||
      host === "gateway.docker.internal" ||
      host === "host.docker.internal" ||
      host === "localstack" ||
      host === "postgres" ||
      host === "redis" ||
      host === "example.com" ||
      host === "example.net" ||
      host === "example.org" ||
      host.endsWith(".example.com") ||
      host.endsWith(".example.net") ||
      host.endsWith(".example.org") ||
      host.endsWith(".example") ||
      host.endsWith(".invalid") ||
      host.endsWith(".test") ||
      (addressKind === 0 && !host.includes("."));
    return (
      parsed.protocol === "https:" &&
      parsed.username === "" &&
      parsed.password === "" &&
      parsed.pathname === "/" &&
      parsed.search === "" &&
      parsed.hash === "" &&
      value === parsed.origin &&
      !isLocalHost &&
      isAllowedAddress
    );
  } catch {
    return false;
  }
}

/** manifest内の決定論的identityとgeneratorのclient answer IDで共有するUUIDv5。 */
export function uuid5(namespace: string, name: string): string {
  const namespaceBytes = Buffer.from(namespace.replaceAll("-", ""), "hex");
  const digest = createHash("sha1")
    .update(namespaceBytes)
    .update(name, "utf8")
    .digest()
    .subarray(0, 16);
  digest[6] = ((digest[6] ?? 0) & 0x0f) | 0x50;
  digest[8] = ((digest[8] ?? 0) & 0x3f) | 0x80;
  const hex = digest.toString("hex");
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}

function isStringArrayJson(value: string): boolean {
  try {
    const parsed: unknown = JSON.parse(value);
    return (
      Array.isArray(parsed) &&
      parsed.every(
        (item) => typeof item === "string" && !stringHasLoneSurrogate(item),
      )
    );
  } catch {
    return false;
  }
}

export const OptionLabelSchema = z.enum(["A", "B", "C", "D"]);
export type OptionLabel = z.infer<typeof OptionLabelSchema>;

export const AnswerPopulationSchema = z.enum(["mixed", "concentrated"]);
export type AnswerPopulation = z.infer<typeof AnswerPopulationSchema>;

export const ScenarioConditionSchema = z.enum([
  "none",
  "receipt-discard",
  "redis-unavailable",
  "redis-recovered",
  "api-secondary-unavailable",
  "api-secondary-recovered",
  "accumulated",
]);
export type ScenarioCondition = z.infer<typeof ScenarioConditionSchema>;

export const LoadTestOptionCountsSchema = z.strictObject({
  a: z.literal(126),
  b: z.literal(125),
  c: z.literal(125),
  d: z.literal(124),
});
export type LoadTestOptionCounts = z.infer<typeof LoadTestOptionCountsSchema>;

const artifactManifestSchema = z.strictObject({
  source_commit: z.string().regex(/^[0-9a-f]{40}(?![\s\S])/),
  api_image: z.string().max(2048).regex(IMAGE_REFERENCE_PATTERN),
  gateway_image: z.string().max(2048).regex(IMAGE_REFERENCE_PATTERN),
  generator_image: z.string().max(2048).regex(IMAGE_REFERENCE_PATTERN),
});

const scoringPolicySchema = z.strictObject({
  version: policyVersion,
  base_points: nonNegativeInteger,
  initial_speed_bonus: nonNegativeInteger,
  speed_bucket_ms: positiveInteger,
  speed_decrement_per_bucket: nonNegativeInteger,
  standard_duration_ms: z.literal(10_000),
  is_approved: z.literal(true),
});

const displayNamePolicySchema = z.strictObject({
  version: policyVersion,
  notice_version: policyVersion,
  max_code_points: safeInteger.refine(
    (value) => value >= 1 && value <= 100,
    "max_code_points out of range",
  ),
  reject_patterns_json: z.string().min(2).max(10_000).refine(isStringArrayJson),
});

const eventManifestSchema = z.strictObject({
  id: uuid4,
  slug: z.string().regex(EVENT_SLUG_PATTERN),
  title: syntheticText,
  state: z.literal("LobbyOpen"),
  ranking_visibility: z.literal("HiddenAll"),
  scoring_policy: scoringPolicySchema,
  display_name_policy: displayNamePolicySchema,
});

const optionManifestSchema = z.strictObject({
  id: uuid,
  label: OptionLabelSchema,
  content: syntheticText,
  display_order: safeInteger.refine(
    (value) => value >= 0 && value <= 3,
    "display_order out of range",
  ),
});

const questionManifestSchema = z
  .strictObject({
    id: uuid,
    version: safeInteger.refine(
      (value) => value >= 1 && value <= 10,
      "version out of range",
    ),
    prompt: syntheticText,
    explanation: syntheticText,
    display_order: safeInteger.refine(
      (value) => value >= 1 && value <= 10,
      "display_order out of range",
    ),
    options: z.array(optionManifestSchema).length(4),
  })
  .superRefine((question, context) => {
    const labels = question.options.map((option) => option.label).join("");
    const orders = question.options.map((option) => option.display_order).join(",");
    const optionIds = new Set(question.options.map((option) => option.id));
    if (labels !== "ABCD" || orders !== "0,1,2,3" || optionIds.size !== 4) {
      context.addIssue({ code: "custom", message: "question options invalid" });
    }
  });

const initialRoundSchema = z.strictObject({
  id: uuid,
  question_id: uuid,
  version: z.literal(1),
  state: z.literal("PreparingAssets"),
  result_publication: z.literal("Withheld"),
});

const lifecycleSchema = z.strictObject({
  virtual_sessions: z.literal(600),
  target_total_sessions: z.literal(600),
  ramp_start_seconds: z.literal(0),
  ramp_end_seconds: z.literal(600),
  ramp_targets: z.tuple([
    z.literal(100),
    z.literal(300),
    z.literal(500),
    z.literal(600),
  ]),
  ramp_target_offsets_seconds: z.tuple([
    z.literal(150),
    z.literal(300),
    z.literal(450),
    z.literal(600),
  ]),
  measurement_start_seconds: z.literal(900),
  measurement_end_seconds: z.literal(4500),
  ramp_down_start_seconds: z.literal(4500),
  ramp_down_end_seconds: z.literal(4800),
  exclusive_window_confirmation_required: z.literal(true),
});

const answerOracleSchema = z.strictObject({
  modulo_vu_start: z.literal(1),
  modulo_vu_end: z.literal(496),
  modulo_option_cycle: z.tuple([
    z.literal("A"),
    z.literal("B"),
    z.literal("C"),
    z.literal("D"),
  ]),
  fixed_virtual_answers: z.tuple([
    z.strictObject({ vu_id: z.literal(497), option: z.literal("A") }),
    z.strictObject({ vu_id: z.literal(498), option: z.literal("A") }),
    z.strictObject({ vu_id: z.literal(499), option: z.literal("B") }),
    z.strictObject({ vu_id: z.literal(500), option: z.literal("C") }),
    z.strictObject({ vu_id: z.literal(599), option: z.literal("B") }),
    z.strictObject({ vu_id: z.literal(600), option: z.literal("C") }),
  ]),
  mixed_virtual_vu_end: z.literal(498),
  mixed_additional_vu_ids: z.tuple([z.literal(599), z.literal(600)]),
  concentrated_virtual_vu_end: z.literal(500),
  non_answering_vu_start: z.literal(501),
  non_answering_vu_end: z.literal(598),
  expected_option_counts: LoadTestOptionCountsSchema,
});

const protocolSchema = z.strictObject({
  join_token_characters: z.literal(43),
  independent_cookie_jar_per_session: z.literal(true),
  independent_websocket_per_session: z.literal(true),
  heartbeat_interval_seconds: z.literal(3),
  clock_samples_per_sync: z.literal(3),
  clock_sync_interval_seconds: z.literal(30),
  clock_sample_timeout_ms: z.literal(1000),
  clock_sample_selection: z.literal("shortest-rtt"),
  clock_sample_max_rtt_ms: z.literal(500),
  polling_interval_seconds: z.literal(1),
  websocket_reconnect_interval_seconds: z.literal(5),
  static_asset_fetches_per_session: z.literal(1),
  refetch_projection_after_invalidation: z.literal(true),
  send_readiness: z.literal(true),
  query_status_after_unknown_response: z.literal(true),
  client_answer_id_scheme: z.literal("uuid5(run_id,question_id:vu_id)"),
});

const thresholdsSchema = z.strictObject({
  sustained_total_sessions: z.literal(600),
  sustained_duration_seconds: z.literal(3600),
  normal_participant_websockets: z.literal(600),
  target_count: z.literal(600),
  normal_expected_ready_count: z.literal(600),
  ready_count_minimum: z.literal(588),
  asset_failure_maximum: z.literal(11),
  expected_answer_count: z.literal(500),
  round_start_width_max_ms: z.literal(1000),
  notification_refresh_over_one_second_maximum: z.literal(5),
  http_5xx_maximum: z.literal(0),
  recovery_seconds_maximum: z.literal(120),
  missed_rounds_maximum: z.literal(0),
  duplicate_answers_maximum: z.literal(0),
  misleading_successes_maximum: z.literal(0),
  unresolved_answers_maximum: z.literal(0),
  replay_same_answer_id_required: z.literal(true),
  replay_row_change_maximum: z.literal(0),
  latency_mode: z.literal("first-run-calibration-only"),
  latency_report_statistics: z.tuple([
    z.literal("p50"),
    z.literal("p95"),
    z.literal("p99"),
    z.literal("max"),
  ]),
  hard_latency_threshold_ms: z.null(),
});

const resourceAbortSchema = z.strictObject({
  generator_cpu_percent_over: z.literal(80),
  generator_memory_percent_over: z.literal(80),
  scheduler_lag_ms_over: z.literal(50),
  consecutive_seconds: z.literal(30),
});

export const LoadTestRoundScenarioSchema = z.strictObject({
  number: safeInteger.refine(
    (value) => value >= 1 && value <= 10,
    "round number out of range",
  ),
  start_offset_seconds: nonNegativeInteger,
  answer_population: AnswerPopulationSchema,
  virtual_answer_count: safeInteger.refine(
    (value) => value >= 0 && value <= 500,
    "virtual answer count out of range",
  ),
  expected_option_counts: LoadTestOptionCountsSchema,
  replay: z.enum(["none", "all"]),
  condition: ScenarioConditionSchema,
  receipt_discard_vu_start: z.union([safeInteger, z.null()]),
  receipt_discard_vu_end: z.union([safeInteger, z.null()]),
  receipt_discard_only_when_status: z.union([z.literal("Stored"), z.null()]),
  answer_status_delay_ms: z.union([nonNegativeInteger, z.null()]),
  retry_only_when_status: z.union([z.literal("NotStored"), z.null()]),
});
export type LoadTestRoundScenario = z.infer<typeof LoadTestRoundScenarioSchema>;

const EXPECTED_ROUNDS = [
  [1, 900, "mixed", 500, "none", "none", null, null, null, null, null],
  [2, 1260, "mixed", 500, "none", "none", null, null, null, null, null],
  [3, 1620, "concentrated", 500, "all", "none", null, null, null, null, null],
  [4, 1980, "concentrated", 500, "none", "receipt-discard", 1, 50, "Stored", 2500, "NotStored"],
  [5, 2340, "mixed", 500, "none", "redis-unavailable", null, null, null, null, null],
  [6, 2700, "mixed", 500, "none", "redis-recovered", null, null, null, null, null],
  [7, 3060, "mixed", 500, "none", "api-secondary-unavailable", null, null, null, null, null],
  [8, 3420, "concentrated", 500, "none", "api-secondary-recovered", null, null, null, null, null],
  [9, 3780, "mixed", 500, "none", "accumulated", null, null, null, null, null],
  [10, 4140, "concentrated", 500, "all", "none", null, null, null, null, null],
] as const;

const faultActionSchema = z.strictObject({
  target: z.enum(["redis", "api-secondary"]),
  action: z.enum(["disable", "enable"]),
  offset_seconds: nonNegativeInteger,
});

export const LoadTestScenarioSchema = z
  .strictObject({
    lifecycle: lifecycleSchema,
    answer_oracle: answerOracleSchema,
    protocol: protocolSchema,
    thresholds: thresholdsSchema,
    resource_abort: resourceAbortSchema,
    rounds: z.array(LoadTestRoundScenarioSchema).length(10),
    fault_schedule: z.array(faultActionSchema).length(4),
  })
  .superRefine((scenario, context) => {
    scenario.rounds.forEach((round, index) => {
      const expected = EXPECTED_ROUNDS[index];
      const actual = [
        round.number,
        round.start_offset_seconds,
        round.answer_population,
        round.virtual_answer_count,
        round.replay,
        round.condition,
        round.receipt_discard_vu_start,
        round.receipt_discard_vu_end,
        round.receipt_discard_only_when_status,
        round.answer_status_delay_ms,
        round.retry_only_when_status,
      ];
      if (expected === undefined || actual.some((value, itemIndex) => value !== expected[itemIndex])) {
        context.addIssue({
          code: "custom",
          message: "round scenario does not match the approved contract",
          path: ["rounds", index],
        });
      }
    });
    const faults = scenario.fault_schedule.map(
      (fault) => `${fault.target}:${fault.action}:${fault.offset_seconds}`,
    );
    const expectedFaults = [
      "redis:disable:2280",
      "redis:enable:2580",
      "api-secondary:disable:3000",
      "api-secondary:enable:3300",
    ];
    if (faults.some((fault, index) => fault !== expectedFaults[index])) {
      context.addIssue({ code: "custom", message: "fault schedule invalid", path: ["fault_schedule"] });
    }
  });
export type LoadTestScenario = z.infer<typeof LoadTestScenarioSchema>;

export const LoadTestManifestSchema = z
  .strictObject({
    format: z.literal("load-test-manifest-v3"),
    run_id: uuid4,
    created_at: AwareDatetimeSchema,
    scheduled_start_at: AwareDatetimeSchema,
    public_origin: z.string().max(2_083).refine(isCanonicalRemoteHttpsOrigin),
    schema_revision: z.literal("20260804_0002"),
    threshold_mode: z.literal("calibration"),
    artifacts: artifactManifestSchema,
    event: eventManifestSchema,
    questions: z.array(questionManifestSchema).length(10),
    initial_round: initialRoundSchema,
    scenario: LoadTestScenarioSchema,
  })
  .superRefine((manifest, context) => {
    const createdAt = canonicalAwareDatetimeMicroseconds(manifest.created_at);
    const scheduledStartAt = canonicalAwareDatetimeMicroseconds(manifest.scheduled_start_at);
    if (
      createdAt === null ||
      scheduledStartAt === null ||
      scheduledStartAt <= createdAt
    ) {
      context.addIssue({ code: "custom", message: "scheduled start must be later", path: ["scheduled_start_at"] });
    }
    const questionIds = new Set<string>();
    const allIds = new Set<string>([
      manifest.run_id,
      manifest.event.id,
      manifest.initial_round.id,
    ]);
    manifest.questions.forEach((question, index) => {
      if (question.version !== index + 1 || question.display_order !== index + 1) {
        context.addIssue({ code: "custom", message: "question order invalid", path: ["questions", index] });
      }
      questionIds.add(question.id);
      allIds.add(question.id);
      const roundNumber = index + 1;
      if (question.id !== uuid5(manifest.run_id, `question:${roundNumber}`)) {
        context.addIssue({
          code: "custom",
          message: "question identity invalid",
          path: ["questions", index, "id"],
        });
      }
      question.options.forEach((option, optionIndex) => {
        allIds.add(option.id);
        if (
          option.id !==
          uuid5(manifest.run_id, `question:${roundNumber}:option:${option.label}`)
        ) {
          context.addIssue({
            code: "custom",
            message: "option identity invalid",
            path: ["questions", index, "options", optionIndex, "id"],
          });
        }
      });
    });
    if (
      questionIds.size !== 10 ||
      allIds.size !== 53 ||
      manifest.initial_round.question_id !== manifest.questions[0]?.id
    ) {
      context.addIssue({ code: "custom", message: "manifest identity relations invalid" });
    }
    if (manifest.initial_round.id !== uuid5(manifest.run_id, "round:1")) {
      context.addIssue({ code: "custom", message: "initial round identity invalid", path: ["initial_round", "id"] });
    }
  });
export type LoadTestManifest = z.infer<typeof LoadTestManifestSchema>;

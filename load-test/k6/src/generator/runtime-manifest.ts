import { z } from "zod";

import { canonicalJson } from "../canonical-json-core.js";
import type { LoadTestManifest } from "../contracts.js";
import type { GeneratorPlanningManifest } from "./planner-core.js";

const SHA256_PATTERN = /^[0-9a-f]{64}(?![\s\S])/;
const SOURCE_COMMIT_PATTERN = /^[0-9a-f]{40}(?![\s\S])/;
const EVENT_SLUG_PATTERN = /^[a-z0-9][a-z0-9-]{2,63}(?![\s\S])/;
export const MAX_K6_RUNTIME_MANIFEST_BYTES = 64 * 1_024;

const optionLabelSchema = z.enum(["A", "B", "C", "D"]);
const optionSchema = z.strictObject({
  id: z.uuid(),
  label: optionLabelSchema,
  display_order: z.number().int().min(0).max(3),
});

const questionSchema = z.strictObject({
  id: z.uuid(),
  version: z.number().int().min(1).max(10),
  display_order: z.number().int().min(1).max(10),
  options: z.array(optionSchema).length(4),
});

const lifecycleSchema = z.strictObject({
  virtual_sessions: z.literal(600),
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
  ramp_down_start_seconds: z.literal(4_500),
  ramp_down_end_seconds: z.literal(4_800),
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
});

const protocolSchema = z.strictObject({
  join_token_characters: z.literal(43),
  independent_cookie_jar_per_session: z.literal(true),
  independent_websocket_per_session: z.literal(true),
  heartbeat_interval_seconds: z.literal(3),
  clock_samples_per_sync: z.literal(3),
  clock_sync_interval_seconds: z.literal(30),
  clock_sample_timeout_ms: z.literal(1_000),
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

const roundSchema = z.strictObject({
  number: z.number().int().min(1).max(10),
  start_offset_seconds: z.number().int().nonnegative(),
  answer_population: z.enum(["mixed", "concentrated"]),
  virtual_answer_count: z.number().int().min(0).max(500),
  replay: z.enum(["none", "all"]),
  condition: z.enum([
    "none",
    "receipt-discard",
    "redis-unavailable",
    "redis-recovered",
    "api-secondary-unavailable",
    "api-secondary-recovered",
    "accumulated",
  ]),
  receipt_discard_vu_start: z.number().int().nullable(),
  receipt_discard_vu_end: z.number().int().nullable(),
  receipt_discard_only_when_status: z.literal("Stored").nullable(),
  answer_status_delay_ms: z.number().int().nonnegative().nullable(),
  retry_only_when_status: z.literal("NotStored").nullable(),
});

const runtimeManifestSchema = z
  .strictObject({
    format: z.literal("load-test-manifest-v3"),
    run_id: z.uuid(),
    scheduled_start_at: z.iso.datetime({ offset: true }),
    public_origin: z.string().max(2_083).refine(isRuntimeHttpsOriginShape),
    schema_revision: z.literal("20260804_0002"),
    artifacts: z.strictObject({
      source_commit: z.string().regex(SOURCE_COMMIT_PATTERN),
    }),
    event: z.strictObject({
      id: z.uuid(),
      slug: z.string().min(1).max(80).regex(EVENT_SLUG_PATTERN),
    }),
    questions: z.array(questionSchema).length(10),
    initial_round: z.strictObject({
      id: z.uuid(),
      question_id: z.uuid(),
    }),
    scenario: z.strictObject({
      lifecycle: lifecycleSchema,
      answer_oracle: answerOracleSchema,
      protocol: protocolSchema,
      rounds: z.array(roundSchema).length(10),
    }),
  })
  .superRefine((manifest, context) => {
    if (
      !Number.isSafeInteger(Date.parse(manifest.scheduled_start_at)) ||
      manifest.initial_round.question_id !== manifest.questions[0]?.id
    ) {
      context.addIssue({ code: "custom", message: "manifest relation invalid" });
    }
    manifest.questions.forEach((question, index) => {
      const expectedNumber = index + 1;
      if (
        question.version !== expectedNumber ||
        question.display_order !== expectedNumber ||
        question.options.map((option) => option.label).join("") !== "ABCD" ||
        question.options.some((option, optionIndex) => option.display_order !== optionIndex)
      ) {
        context.addIssue({
          code: "custom",
          message: "question order invalid",
          path: ["questions", index],
        });
      }
    });
    manifest.scenario.rounds.forEach((round, index) => {
      if (round.number !== index + 1) {
        context.addIssue({
          code: "custom",
          message: "round order invalid",
          path: ["scenario", "rounds", index],
        });
      }
    });
  });

const runtimeEnvelopeSchema = z.strictObject({
  format: z.literal("load-test-k6-runtime-envelope-v3"),
  manifest_sha256: z.string().regex(SHA256_PATTERN),
  scenario_sha256: z.string().regex(SHA256_PATTERN),
  projection: runtimeManifestSchema,
});

export type K6RuntimeManifest = z.output<typeof runtimeManifestSchema> &
  GeneratorPlanningManifest;

export interface K6RuntimeManifestBinding {
  readonly manifest: K6RuntimeManifest;
  readonly manifestSha256: string;
  readonly runtimeManifestSha256: string;
  readonly scenarioSha256: string;
}

export type RuntimeManifestRefusalCode =
  | "generator_environment_invalid"
  | "generator_manifest_binding_mismatch"
  | "generator_manifest_invalid";

/** k6 stdoutへmanifest内容を漏らさない固定codeだけの失敗。 */
export class RuntimeManifestRefusal extends Error {
  readonly code: RuntimeManifestRefusalCode;

  constructor(code: RuntimeManifestRefusalCode) {
    super(code);
    this.name = "RuntimeManifestRefusal";
    this.code = code;
  }
}

/** launcherが固定したcanonical projection envelopeだけをk6 init contextへ取り込む。 */
export function loadK6RuntimeManifest(
  rawManifest: string | undefined,
  environment: Readonly<Record<string, string | undefined>>,
  hashCanonicalText: (value: string) => string,
): K6RuntimeManifestBinding {
  const runtimeManifestSha256 =
    environment["LOAD_TEST_RUNTIME_MANIFEST_SHA256"];
  if (
    rawManifest === undefined ||
    runtimeManifestSha256 === undefined ||
    !SHA256_PATTERN.test(runtimeManifestSha256)
  ) {
    throw new RuntimeManifestRefusal("generator_environment_invalid");
  }
  requireBoundedRuntimeManifestText(rawManifest);

  let decoded: unknown;
  try {
    decoded = JSON.parse(rawManifest);
  } catch {
    throw new RuntimeManifestRefusal("generator_manifest_invalid");
  }
  const parsed = runtimeEnvelopeSchema.safeParse(decoded);
  if (!parsed.success) {
    throw new RuntimeManifestRefusal("generator_manifest_invalid");
  }
  try {
    const canonicalManifest = canonicalJson(parsed.data);
    const actualRuntimeManifestSha256 = hashCanonicalText(canonicalManifest);
    if (
      rawManifest !== canonicalManifest ||
      !SHA256_PATTERN.test(actualRuntimeManifestSha256) ||
      actualRuntimeManifestSha256 !== runtimeManifestSha256
    ) {
      throw new RuntimeManifestRefusal("generator_manifest_binding_mismatch");
    }
  } catch (error: unknown) {
    if (error instanceof RuntimeManifestRefusal) {
      throw error;
    }
    throw new RuntimeManifestRefusal("generator_manifest_invalid");
  }
  return {
    manifest: parsed.data.projection as K6RuntimeManifest,
    manifestSha256: parsed.data.manifest_sha256,
    runtimeManifestSha256,
    scenarioSha256: parsed.data.scenario_sha256,
  };
}

/** hostで検証済みのOriginを再解釈せず、k6で安全に連結できる形だけ確認する。 */
export function isRuntimeHttpsOriginShape(value: string): boolean {
  if (!/^https:\/\/[^/\\?#@\s]+$/u.test(value)) {
    return false;
  }
  for (let index = 0; index < value.length; index += 1) {
    const codeUnit = value.charCodeAt(index);
    if (codeUnit <= 0x1f || codeUnit === 0x7f) {
      return false;
    }
  }
  return true;
}

/** 単一env値へ渡すprojectionをLinuxの保守的な64 KiB未満へ固定する。 */
export function requireBoundedRuntimeManifestText(value: string): string {
  if (new TextEncoder().encode(value).byteLength >= MAX_K6_RUNTIME_MANIFEST_BYTES) {
    throw new RuntimeManifestRefusal("generator_manifest_invalid");
  }
  return value;
}

/** full manifestとそのhashを、k6が実際に使う最小projectionへ一体束縛する。 */
export function projectK6RuntimeManifest(
  manifest: LoadTestManifest,
  hashes: {
    readonly manifestSha256: string;
    readonly scenarioSha256: string;
  },
): z.output<typeof runtimeEnvelopeSchema> {
  const candidate = {
    format: "load-test-k6-runtime-envelope-v3",
    manifest_sha256: hashes.manifestSha256,
    scenario_sha256: hashes.scenarioSha256,
    projection: {
      format: manifest.format,
      run_id: manifest.run_id,
      scheduled_start_at: manifest.scheduled_start_at,
      public_origin: manifest.public_origin,
      schema_revision: manifest.schema_revision,
      artifacts: { source_commit: manifest.artifacts.source_commit },
      event: { id: manifest.event.id, slug: manifest.event.slug },
      questions: manifest.questions.map((question) => ({
        id: question.id,
        version: question.version,
        display_order: question.display_order,
        options: question.options.map((option) => ({
          id: option.id,
          label: option.label,
          display_order: option.display_order,
        })),
      })),
      initial_round: {
        id: manifest.initial_round.id,
        question_id: manifest.initial_round.question_id,
      },
      scenario: {
        lifecycle: selectLifecycle(manifest),
        answer_oracle: selectAnswerOracle(manifest),
        protocol: selectProtocol(manifest),
        rounds: manifest.scenario.rounds.map((round) => ({
          number: round.number,
          start_offset_seconds: round.start_offset_seconds,
          answer_population: round.answer_population,
          virtual_answer_count: round.virtual_answer_count,
          replay: round.replay,
          condition: round.condition,
          receipt_discard_vu_start: round.receipt_discard_vu_start,
          receipt_discard_vu_end: round.receipt_discard_vu_end,
          receipt_discard_only_when_status:
            round.receipt_discard_only_when_status,
          answer_status_delay_ms: round.answer_status_delay_ms,
          retry_only_when_status: round.retry_only_when_status,
        })),
      },
    },
  };
  const parsed = runtimeEnvelopeSchema.safeParse(candidate);
  if (!parsed.success) {
    throw new RuntimeManifestRefusal("generator_manifest_invalid");
  }
  return parsed.data;
}

function selectLifecycle(manifest: LoadTestManifest): unknown {
  const lifecycle = manifest.scenario.lifecycle;
  return {
    virtual_sessions: lifecycle.virtual_sessions,
    ramp_start_seconds: lifecycle.ramp_start_seconds,
    ramp_end_seconds: lifecycle.ramp_end_seconds,
    ramp_targets: lifecycle.ramp_targets,
    ramp_target_offsets_seconds: lifecycle.ramp_target_offsets_seconds,
    ramp_down_start_seconds: lifecycle.ramp_down_start_seconds,
    ramp_down_end_seconds: lifecycle.ramp_down_end_seconds,
  };
}

function selectAnswerOracle(manifest: LoadTestManifest): unknown {
  const oracle = manifest.scenario.answer_oracle;
  return {
    modulo_vu_start: oracle.modulo_vu_start,
    modulo_vu_end: oracle.modulo_vu_end,
    modulo_option_cycle: oracle.modulo_option_cycle,
    fixed_virtual_answers: oracle.fixed_virtual_answers,
  };
}

function selectProtocol(manifest: LoadTestManifest): unknown {
  const protocol = manifest.scenario.protocol;
  return {
    join_token_characters: protocol.join_token_characters,
    independent_cookie_jar_per_session:
      protocol.independent_cookie_jar_per_session,
    independent_websocket_per_session:
      protocol.independent_websocket_per_session,
    heartbeat_interval_seconds: protocol.heartbeat_interval_seconds,
    clock_samples_per_sync: protocol.clock_samples_per_sync,
    clock_sync_interval_seconds: protocol.clock_sync_interval_seconds,
    clock_sample_timeout_ms: protocol.clock_sample_timeout_ms,
    clock_sample_selection: protocol.clock_sample_selection,
    clock_sample_max_rtt_ms: protocol.clock_sample_max_rtt_ms,
    polling_interval_seconds: protocol.polling_interval_seconds,
    websocket_reconnect_interval_seconds:
      protocol.websocket_reconnect_interval_seconds,
    static_asset_fetches_per_session:
      protocol.static_asset_fetches_per_session,
    refetch_projection_after_invalidation:
      protocol.refetch_projection_after_invalidation,
    send_readiness: protocol.send_readiness,
    query_status_after_unknown_response:
      protocol.query_status_after_unknown_response,
    client_answer_id_scheme: protocol.client_answer_id_scheme,
  };
}

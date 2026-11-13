import { describe, expect, it } from "vitest";

import { canonicalSha256 } from "../src/canonical-json.js";
import { LoadTestManifestSchema } from "../src/contracts.js";
import {
  PYTHON_MANIFEST_SHA256,
  PYTHON_SCENARIO_SHA256,
  validManifestFixture,
} from "./fixtures/manifest.js";

type ManifestFixture = ReturnType<typeof validManifestFixture>;
type ManifestMutation = (manifest: ManifestFixture) => void;

function expectRejected(mutation: ManifestMutation): void {
  const manifest = validManifestFixture();
  mutation(manifest);
  expect(LoadTestManifestSchema.safeParse(manifest).success).toBe(false);
}

describe("LoadTestManifestSchema", () => {

  it("full manifestとscenarioのhashがPython canonical JSONに一致する", () => {
    const manifest = LoadTestManifestSchema.parse(validManifestFixture());

    expect(canonicalSha256(manifest)).toBe(PYTHON_MANIFEST_SHA256);
    expect(canonicalSha256(manifest.scenario)).toBe(PYTHON_SCENARIO_SHA256);
  });

  it.each<[string, ManifestMutation]>([
    ["lifecycle", (manifest) => { manifest.scenario.lifecycle.ramp_targets[3] = 597; }],
    ["oracle", (manifest) => { manifest.scenario.answer_oracle.fixed_virtual_answers[0] = { vu_id: 497, option: "B" }; }],
    ["protocol", (manifest) => { manifest.scenario.protocol.heartbeat_interval_seconds = 4; }],
    ["threshold", (manifest) => { manifest.scenario.thresholds.ready_count_minimum = 587; }],
    ["resource abort", (manifest) => { manifest.scenario.resource_abort.generator_cpu_percent_over = 81; }],
    ["round", (manifest) => { manifest.scenario.rounds[1]!.virtual_answer_count = 498; }],
    ["fault", (manifest) => { manifest.scenario.fault_schedule[0]!.offset_seconds = 2281; }],
  ])("固定scenario driftを拒否する: %s", (_label, mutation) => {
    expectRejected(mutation);
  });

  it.each<[string, ManifestMutation]>([
    ["root", (manifest) => { Object.assign(manifest, { token: "forbidden" }); }],
    ["artifact", (manifest) => { Object.assign(manifest.artifacts, { cookie: "forbidden" }); }],
    ["scenario", (manifest) => { Object.assign(manifest.scenario, { password: "forbidden" }); }],
    ["protocol", (manifest) => { Object.assign(manifest.scenario.protocol, { csrf: "forbidden" }); }],
    ["round", (manifest) => { Object.assign(manifest.scenario.rounds[0]!, { client_answer_id: "forbidden" }); }],
    ["question", (manifest) => { Object.assign(manifest.questions[0]!, { answer: "forbidden" }); }],
    ["option", (manifest) => { Object.assign(manifest.questions[0]!.options[0]!, { is_correct: true }); }],
  ])("nested extraとsecret fieldを拒否する: %s", (_label, mutation) => {
    expectRejected(mutation);
  });

  it("UUIDv5 relationとschema revision driftを拒否する", () => {
    expectRejected((manifest) => {
      manifest.questions[0]!.id = manifest.questions[1]!.id;
    });
    expectRejected((manifest) => {
      manifest.questions[0]!.options[0]!.id = manifest.questions[0]!.options[1]!.id;
    });
    expectRejected((manifest) => {
      manifest.initial_round.id = "33333333-3333-5333-8333-333333333333";
    });
    expectRejected((manifest) => {
      manifest.schema_revision = "20260804_0001";
    });
  });

  it("旧v2と画像fieldを拒否する", () => {
    expectRejected((manifest) => { Object.assign(manifest, { format: "load-test-manifest-v2" }); });
    expectRejected((manifest) => { Object.assign(manifest.questions[0]!, { image_url: null }); });
    expectRejected((manifest) => { Object.assign(manifest.questions[0]!, { image_alt: null }); });
  });

  it("小数とunsafe integerを拒否する", () => {
    expectRejected((manifest) => {
      manifest.event.scoring_policy.base_points = 1.5;
    });
    expectRejected((manifest) => {
      manifest.event.scoring_policy.base_points = Number.MAX_SAFE_INTEGER + 1;
    });
  });

  it("通常回答時間が10秒ではないmanifestを拒否する", () => {
    expectRejected((manifest) => {
      manifest.event.scoring_policy.standard_duration_ms = 5_000;
    });
  });

  it.each([
    "2026-09-03T00:00:00+00:00",
    "2026-09-03T00:00:00.000000Z",
    "2026-09-03T00:00:00.1Z",
    "2026-02-30T00:00:00Z",
  ])("Pydanticがmodel_dump時に書き換える非canonical datetimeを拒否する: %s", (value) => {
    expectRejected((manifest) => {
      manifest.created_at = value;
    });
  });

  it("Pydantic canonical microsecondsと非UTC offsetを受理する", () => {
    const manifest = validManifestFixture();
    manifest.created_at = "2026-09-03T00:00:00.100000Z";
    manifest.scheduled_start_at = "2026-09-03T12:34:56.123400+09:00";
    expect(LoadTestManifestSchema.safeParse(manifest).success).toBe(true);
  });

  it("日時順序をPythonと同じmicrosecond精度で比較する", () => {
    const manifest = validManifestFixture();
    manifest.created_at = "2026-09-03T00:00:00.000001Z";
    manifest.scheduled_start_at = "2026-09-03T00:00:00.000002Z";
    expect(LoadTestManifestSchema.safeParse(manifest).success).toBe(true);

    manifest.scheduled_start_at = manifest.created_at;
    expect(LoadTestManifestSchema.safeParse(manifest).success).toBe(false);
  });

  it("二重JSON内のescaped lone surrogateを拒否する", () => {
    expectRejected((manifest) => {
      manifest.event.display_name_policy.reject_patterns_json = '["\\ud800"]';
    });
  });

  it("Pythonと同じevent slug境界を使う", () => {
    const trailingHyphen = validManifestFixture();
    trailingHyphen.event.slug = "ab-";
    expect(LoadTestManifestSchema.safeParse(trailingHyphen).success).toBe(true);
    expectRejected((manifest) => {
      manifest.event.slug = "ab";
    });
    expectRejected((manifest) => {
      manifest.event.slug = `a${"b".repeat(64)}`;
    });
  });

  it.each([
    "http://quiz.aws.internal",
    "https://quiz",
    "https://localhost",
    "https://quiz.local",
    "https://quiz.local..",
    "https://localhost..",
    "https://quiz.example",
    "https://example.com",
    "https://example.com..",
    "https://127.0.0.1",
    "https://169.254.1.1",
    "https://192.0.2.1",
    "https://224.0.0.1",
    "https://[::1]",
    "https://[fe80::1]",
    "https://[2001:db8::1]",
    "https://[fc00::1]",
    "https://[64:ff9b::1]",
    "https://[64:ff9b:1::1]",
    "https://[2001:1::1]",
    "https://[2001:3::1]",
    "https://[2001:2::1]",
    "https://[2002::1]",
    "https://[3fff::1]",
    "https://[::ffff:127.0.0.1]",
  ])("local・予約・非unicast originを拒否する: %s", (origin) => {
    expectRejected((manifest) => {
      manifest.public_origin = origin;
    });
  });

  it.each(["https://10.0.0.1", "https://172.16.0.1", "https://192.168.0.1"])(
    "Pythonが許可するAWS private originを受理する: %s",
    (origin) => {
      const manifest = validManifestFixture();
      manifest.public_origin = origin;
      expect(LoadTestManifestSchema.safeParse(manifest).success).toBe(true);
    },
  );

  it.each([
    "https://192.0.0.9",
    "https://192.0.0.10",
    "https://192.88.99.1",
  ])("Pythonがglobalと判定するoriginを受理する: %s", (origin) => {
    const manifest = validManifestFixture();
    manifest.public_origin = origin;
    expect(LoadTestManifestSchema.safeParse(manifest).success).toBe(true);
  });

  it.each<[string, ManifestMutation]>([
    ["run UUID", (manifest) => { manifest.run_id += "\n"; }],
    ["event slug", (manifest) => { manifest.event.slug += "\n"; }],
    ["source commit", (manifest) => { manifest.artifacts.source_commit += "\n"; }],
    ["image digest", (manifest) => { manifest.artifacts.api_image += "\n"; }],
    ["datetime", (manifest) => { manifest.created_at += "\n"; }],
  ])("末尾改行を正規表現の終端として受理しない: %s", (_label, mutation) => {
    expectRejected(mutation);
  });
});

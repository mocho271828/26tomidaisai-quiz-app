import { createHash } from "node:crypto";
import {
  chmodSync,
  mkdtempSync,
  realpathSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { afterEach, describe, expect, it, vi } from "vitest";

import {
  APPROVED_FORMAL_K6_IMAGE,
  CONTAINER_GENERATOR_PATH,
  DOCKER_CONFIG_DIRECTORY,
  DOCKER_ENDPOINT,
  FormalContainerCustodian,
  FormalDockerRefusal,
  buildFormalDockerArguments,
  requireApprovedImage,
  requireNonRootIdentity,
  validateGeneratorBundleSnapshot,
} from "../src/formal-docker.js";
import type {
  DockerCommandResult,
  FormalContainerBinding,
} from "../src/formal-docker.js";

const RUN_ID = "11111111-1111-4111-8111-111111111111";
const EVENT_ID = "22222222-2222-4222-8222-222222222222";
const MANIFEST_SHA256 = "a".repeat(64);
const CONTAINER_ID = "b".repeat(64);
const roots: string[] = [];

function containerBinding(): FormalContainerBinding {
  return {
    name: `gs-loadtest-k6-${RUN_ID}`,
    runId: RUN_ID,
    eventId: EVENT_ID,
    manifestSha256: MANIFEST_SHA256,
    image: APPROVED_FORMAL_K6_IMAGE,
  };
}

afterEach(() => {
  for (const root of roots.splice(0)) {
    rmSync(root, { force: true, recursive: true });
  }
});

describe("formal Docker invocation", () => {
  it("uses only the approved digest and exact least-privilege arguments", () => {
    const snapshot = `/run/user/1000/gs-loadtest-k6/${RUN_ID}.generator.js`;
    const arguments_ = buildFormalDockerArguments({
      binding: containerBinding(),
      bundlePath: snapshot,
      effectiveUserId: 1000,
      effectiveGroupId: 1000,
    });

    expect(arguments_).toEqual([
      "--config",
      DOCKER_CONFIG_DIRECTORY,
      "--host",
      DOCKER_ENDPOINT,
      "run",
      "--rm",
      "--pull=never",
      "--init",
      `--name=gs-loadtest-k6-${RUN_ID}`,
      "--network=bridge",
      "--read-only",
      "--cap-drop=ALL",
      "--security-opt=no-new-privileges=true",
      "--user=1000:1000",
      `--label=org.tomidaisai.load-test.run-id=${RUN_ID}`,
      `--label=org.tomidaisai.load-test.event-id=${EVENT_ID}`,
      `--label=org.tomidaisai.load-test.manifest-sha256=${MANIFEST_SHA256}`,
      "--mount",
      `type=bind,src=${snapshot},dst=${CONTAINER_GENERATOR_PATH},readonly`,
      "--env",
      "LOAD_TEST_RUNTIME_MANIFEST_JSON",
      "--env",
      "LOAD_TEST_RUNTIME_MANIFEST_SHA256",
      APPROVED_FORMAL_K6_IMAGE,
      "run",
      "--quiet",
      "--summary-mode=full",
      "--no-color",
      "--no-usage-report",
      "--log-output=stdout",
      "--log-format=raw",
      CONTAINER_GENERATOR_PATH,
    ]);
    expect(arguments_.filter((value) => value === "--mount")).toHaveLength(1);
    expect(arguments_.join(" ")).not.toMatch(/--publish|--privileged|--device/u);
    expect(arguments_[arguments_.indexOf("--mount") + 1]).not.toContain(
      "docker.sock",
    );
  });

  it("rejects every other digest-pinned image and root identity", () => {
    expect(() =>
      requireApprovedImage(`registry.aws.internal/k6@sha256:${"c".repeat(64)}`),
    ).toThrowError(
      expect.objectContaining<Partial<FormalDockerRefusal>>({
        code: "generator_image_invalid",
      }),
    );
    expect(() => requireNonRootIdentity(0, 1000)).toThrowError(
      expect.objectContaining<Partial<FormalDockerRefusal>>({
        code: "generator_user_invalid",
      }),
    );
  });

  it("revalidates the content-addressed 0400 bundle snapshot", () => {
    const root = realpathSync(mkdtempSync(join(tmpdir(), "k6-bundle-")));
    roots.push(root);
    const path = join(root, "generator.js");
    const body = Buffer.from("export default function () {}\n");
    writeFileSync(path, body, { mode: 0o400 });
    chmodSync(path, 0o400);
    const expected = {
      sha256: createHash("sha256").update(body).digest("hex"),
      sizeBytes: body.byteLength,
    };

    expect(() =>
      validateGeneratorBundleSnapshot(
        path,
        expected,
        process.geteuid!(),
        process.getegid!(),
      ),
    ).not.toThrow();
    chmodSync(path, 0o600);
    expect(() =>
      validateGeneratorBundleSnapshot(
        path,
        expected,
        process.geteuid!(),
        process.getegid!(),
      ),
    ).toThrowError(
      expect.objectContaining<Partial<FormalDockerRefusal>>({
        code: "generator_bundle_invalid",
      }),
    );
  });
});

describe("formal container custodian", () => {
  it("treats --rm disappearance immediately after stop as successful cleanup", () => {
    const calls: readonly string[][] = [];
    const mutableCalls = calls as string[][];
    let listCount = 0;
    const execute = vi.fn((arguments_: readonly string[]): DockerCommandResult => {
      mutableCalls.push([...arguments_]);
      if (arguments_[1] === "ls") {
        listCount += 1;
        return {
          status: 0,
          stdout: listCount === 1 ? `${CONTAINER_ID}\n` : "",
        };
      }
      if (arguments_[1] === "inspect") {
        return { status: 0, stdout: ownedInspect(true) };
      }
      if (arguments_[1] === "stop") {
        return { status: 0, stdout: `${CONTAINER_ID}\n` };
      }
      throw new Error("unexpected test command");
    });
    const custodian = new FormalContainerCustodian(containerBinding(), execute);

    expect(() => custodian.cleanupAndAssertAbsent()).not.toThrow();
    expect(mutableCalls.map((call) => call.slice(0, 2))).toEqual([
      ["container", "ls"],
      ["container", "inspect"],
      ["container", "stop"],
      ["container", "ls"],
    ]);
  });

  it("never removes a same-name container whose ownership labels differ", () => {
    const execute = vi.fn((arguments_: readonly string[]): DockerCommandResult => {
      if (arguments_[1] === "ls") {
        return { status: 0, stdout: `${CONTAINER_ID}\n` };
      }
      if (arguments_[1] === "inspect") {
        return {
          status: 0,
          stdout: ownedInspect(true).replace(RUN_ID, "33333333-3333-4333-8333-333333333333"),
        };
      }
      return { status: 0, stdout: "" };
    });
    const custodian = new FormalContainerCustodian(containerBinding(), execute);

    expect(() => custodian.cleanupAndAssertAbsent()).toThrowError(
      expect.objectContaining<Partial<FormalDockerRefusal>>({
        code: "generator_container_cleanup_failed",
      }),
    );
    expect(
      execute.mock.calls.some(([arguments_]) =>
        ["stop", "kill", "rm"].includes(arguments_[1] ?? ""),
      ),
    ).toBe(false);
  });

  it("refuses a stale deterministic name before launch without deleting it", () => {
    const execute = vi.fn(() => ({
      status: 0,
      stdout: `${CONTAINER_ID}\n`,
    }));
    const custodian = new FormalContainerCustodian(containerBinding(), execute);

    expect(() => custodian.assertAbsent()).toThrowError(
      expect.objectContaining<Partial<FormalDockerRefusal>>({
        code: "generator_container_conflict",
      }),
    );
    expect(execute).toHaveBeenCalledTimes(1);
  });
});

function ownedInspect(isRunning: boolean): string {
  return `${JSON.stringify({
    EventId: EVENT_ID,
    Id: CONTAINER_ID,
    Image: APPROVED_FORMAL_K6_IMAGE,
    ManifestSha256: MANIFEST_SHA256,
    Name: `/gs-loadtest-k6-${RUN_ID}`,
    RunId: RUN_ID,
    Running: isRunning,
  })}\n`;
}

describe("private HTTPS CA mount", () => {
  it("runに束縛したCAをreadonly mountしTLS検証を有効のまま使う", () => {
    const caPath = `/run/user/1000/gs-loadtest-k6/${RUN_ID}.ca.pem`;
    const args = buildFormalDockerArguments({ binding: containerBinding(),
      bundlePath: `/run/user/1000/gs-loadtest-k6/${RUN_ID}.generator.js`,
      applicationCaPath: caPath, effectiveUserId: 1000, effectiveGroupId: 1000 });
    expect(args).toContain(`type=bind,src=${caPath},dst=/load-test/ca.pem,readonly`);
    expect(args).toContain("--env=SSL_CERT_FILE=/load-test/ca.pem");
    expect(args.join(" ")).not.toContain("insecure");
    expect(() => buildFormalDockerArguments({ binding: containerBinding(),
      bundlePath: `/run/user/1000/gs-loadtest-k6/${RUN_ID}.generator.js`,
      applicationCaPath: "/tmp/other.pem", effectiveUserId: 1000, effectiveGroupId: 1000,
    })).toThrow(FormalDockerRefusal);
  });
});

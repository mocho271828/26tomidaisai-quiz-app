import { describe, expect, it, vi } from "vitest";

import {
  GeneratorCliArgumentError,
  parseGeneratorCliArguments,
  runGeneratorCli,
} from "../src/generator-cli.js";
import type { K6GeneratorRuntimeResult } from "../src/formal-runtime.js";
import { LauncherRefusal } from "../src/launcher.js";

function identity(name: string, digit: string) {
  return { name, sha256: digit.repeat(64), size_bytes: 123 };
}

function result(): K6GeneratorRuntimeResult {
  return {
    format: "load-test-generator-result-v1",
    runId: "11111111-1111-4111-8111-111111111111",
    eventId: "22222222-2222-4222-8222-222222222222",
    scheduledStartAt: "2030-01-01T00:00:10Z",
    actualSpawnAt: "2030-01-01T00:00:09Z",
    generatorImage: `ghcr.io/grafana/k6:2.2.0@sha256:${"9".repeat(64)}`,
    generatorBundle: identity("generator.js", "1"),
    armMarker: identity("run.arm.json", "2"),
    containerName:
      "gs-loadtest-k6-11111111-1111-4111-8111-111111111111",
    artifacts: Array.from({ length: 10 }, (_, index) =>
      identity(
        `generator-round-${String(index + 1).padStart(2, "0")}.json`,
        ((index + 3) % 10).toString(),
      ),
    ),
    metricsArtifact: identity("generator-metrics-summary.json", "4"),
    manifestSha256: "5".repeat(64),
    scenarioSha256: "6".repeat(64),
  };
}

describe("generator CLI arguments", () => {
  it("accepts only the four required flags in either order", () => {
    expect(
      parseGeneratorCliArguments([
        "--output-directory",
        "/artifacts",
        "--manifest",
        "/run/manifest.json",
        "--preparation-evidence", "/run/preparation.json",
        "--application-ca-file", "/run/ca.pem",
      ]),
    ).toEqual({
      manifestPath: "/run/manifest.json",
      preparationEvidencePath: "/run/preparation.json",
      applicationCaFile: "/run/ca.pem",
      outputDirectory: "/artifacts",
    });
  });

  it.each(
    [
      [],
      ["--manifest", "/secret/manifest.json"],
      ["--manifest", "/manifest", "--manifest", "/other"],
      ["--manifest=/manifest", "x", "--output-directory", "/out"],
      ["--unknown", "argv-secret", "--output-directory", "/out"],
      ["--manifest", "--output-directory", "x", "/out"],
    ].map((argv) => [argv] as const),
  )("rejects missing, duplicate, joined, and unknown argv: %j", (argv) => {
    expect(() => parseGeneratorCliArguments(argv)).toThrowError(
      GeneratorCliArgumentError,
    );
  });
});

describe("generator CLI output", () => {
  it("prints one secret-free bound result without filesystem paths", async () => {
    const stdout = { write: vi.fn() };
    const stderr = { write: vi.fn() };
    const runtime = vi.fn(async () => result());

    expect(
      await runGeneratorCli(
        ["--manifest", "/manifest", "--output-directory", "/output", "--preparation-evidence", "/preparation.json", "--application-ca-file", "/ca.pem"],
        stdout,
        stderr,
        runtime,
      ),
    ).toBe(0);
    expect(stderr.write).not.toHaveBeenCalled();
    expect(stdout.write).toHaveBeenCalledTimes(1);
    const written = stdout.write.mock.calls[0]?.[0] as string;
    const payload = JSON.parse(written) as Record<string, unknown>;
    expect(payload).toMatchObject({
      format: "load-test-generator-result-v1",
      status: "completed",
      actual_spawn_at: "2030-01-01T00:00:09Z",
      manifest_sha256: "5".repeat(64),
    });
    expect(written).not.toContain("/manifest");
    expect(written).not.toContain("/output");
  });

  it("redacts invalid argv and runtime exception details", async () => {
    const secret = "argv-and-error-secret";
    const stdout = { write: vi.fn() };
    const stderr = { write: vi.fn() };

    expect(
      await runGeneratorCli(
        ["--unknown", secret, "--output-directory", "/out"],
        stdout,
        stderr,
      ),
    ).toBe(2);
    expect(stderr.write.mock.calls[0]?.[0]).toBe(
      '{"code":"generator_cli_arguments_invalid","format":"load-test-generator-cli-error-v1","status":"failed"}\n',
    );
    stderr.write.mockClear();
    expect(
      await runGeneratorCli(
        ["--manifest", "/manifest", "--output-directory", "/out", "--preparation-evidence", "/preparation.json", "--application-ca-file", "/ca.pem"],
        stdout,
        stderr,
        async () => {
          throw new Error(secret);
        },
      ),
    ).toBe(1);
    expect(stderr.write.mock.calls[0]?.[0]).toBe(
      '{"code":"generator_cli_failed","format":"load-test-generator-cli-error-v1","status":"failed"}\n',
    );
    expect(stderr.write.mock.calls[0]?.[0]).not.toContain(secret);
    expect(stdout.write).not.toHaveBeenCalled();
  });

  it("prints an allowlisted fixed runtime failure code only", async () => {
    const stdout = { write: vi.fn() };
    const stderr = { write: vi.fn() };

    expect(
      await runGeneratorCli(
        ["--manifest", "/manifest", "--output-directory", "/out", "--preparation-evidence", "/preparation.json", "--application-ca-file", "/ca.pem"],
        stdout,
        stderr,
        async () => {
          throw new LauncherRefusal("launcher_start_late");
        },
      ),
    ).toBe(1);
    expect(stderr.write.mock.calls[0]?.[0]).toContain("launcher_start_late");
    expect(stdout.write).not.toHaveBeenCalled();
  });
});

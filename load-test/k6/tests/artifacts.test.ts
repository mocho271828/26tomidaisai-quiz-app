import { createHash } from "node:crypto";
import {
  chmodSync,
  linkSync,
  lstatSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  realpathSync,
  rmSync,
  symlinkSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { afterEach, describe, expect, it } from "vitest";

import {
  ArtifactRefusal,
  readSecureExternalArtifact,
  sealArtifact,
  validateSecureExternalDirectory,
} from "../src/artifacts.js";
import { runtimeRepositoryRoot } from "../src/manifest.js";

const temporaryRoots: string[] = [];

function temporaryDirectory(): string {
  const directory = realpathSync(mkdtempSync(join(tmpdir(), "k6-artifacts-")));
  chmodSync(directory, 0o700);
  temporaryRoots.push(directory);
  return directory;
}

afterEach(() => {
  for (const directory of temporaryRoots.splice(0)) {
    rmSync(directory, { force: true, recursive: true });
  }
});

describe("secure external artifact boundary", () => {
  it("reads only an owner 0600 single-link file and returns its exact identity", () => {
    const inputDirectory = temporaryDirectory();
    const body = Buffer.from('{"日本語":"😀"}\n');
    const input = join(inputDirectory, "manifest.json");
    writeFileSync(input, body, { mode: 0o600 });
    chmodSync(input, 0o600);

    const loaded = readSecureExternalArtifact(input, {});

    expect(Buffer.from(loaded.body)).toEqual(body);
    expect(loaded.artifact).toEqual({
      name: "manifest.json",
      sha256: createHash("sha256").update(body).digest("hex"),
      size_bytes: body.byteLength,
    });
  });

  it.each([0o750, 0o707, 0o1700])("rejects a non-exact output directory mode %o", (mode) => {
    const output = temporaryDirectory();
    chmodSync(output, mode);

    expect(() => validateSecureExternalDirectory(output)).toThrowError(
      expect.objectContaining<Partial<ArtifactRefusal>>({
        code: "artifact_directory_invalid",
      }),
    );
  });

  it("rejects a lexical ancestor symlink even when its target is owner-only", () => {
    const parent = temporaryDirectory();
    const target = join(parent, "target");
    const alias = join(parent, "alias");
    mkdirSync(target, { mode: 0o700 });
    chmodSync(target, 0o700);
    symlinkSync(target, alias, "dir");
    const input = join(target, "input.json");
    writeFileSync(input, "{}\n", { mode: 0o600 });
    chmodSync(input, 0o600);

    expect(() =>
      readSecureExternalArtifact(join(alias, "input.json"), {}),
    ).toThrowError(
      expect.objectContaining<Partial<ArtifactRefusal>>({
        code: "artifact_directory_invalid",
      }),
    );
  });

  it("rejects an input hard link", () => {
    const inputDirectory = temporaryDirectory();
    const input = join(inputDirectory, "input.json");
    writeFileSync(input, "{}\n", { mode: 0o600 });
    chmodSync(input, 0o600);
    linkSync(input, join(inputDirectory, "second-link.json"));

    expect(() =>
      readSecureExternalArtifact(input, {}),
    ).toThrowError(
      expect.objectContaining<Partial<ArtifactRefusal>>({ code: "artifact_input_invalid" }),
    );
  });

  it("rejects final symlink, special mode bits, and an oversized caller boundary", () => {
    const inputDirectory = temporaryDirectory();
    const input = join(inputDirectory, "input.json");
    writeFileSync(input, "{}\n", { mode: 0o600 });
    chmodSync(input, 0o600);

    const alias = join(inputDirectory, "alias.json");
    symlinkSync(input, alias);
    expect(() => readSecureExternalArtifact(alias, {})).toThrowError(
      expect.objectContaining<Partial<ArtifactRefusal>>({ code: "artifact_read_failed" }),
    );

    chmodSync(input, 0o1600);
    expect(lstatSync(input).mode & 0o7777).toBe(0o1600);
    expect(() => readSecureExternalArtifact(input, {})).toThrowError(
      expect.objectContaining<Partial<ArtifactRefusal>>({
        code: "artifact_input_invalid",
      }),
    );

    expect(() =>
      readSecureExternalArtifact(input, { maximumBytes: 64 * 1024 * 1024 + 1 }),
    ).toThrowError(
      expect.objectContaining<Partial<ArtifactRefusal>>({ code: "artifact_size_invalid" }),
    );
  });

  it("rejects a directory inside the repository boundary", () => {
    expect(() => validateSecureExternalDirectory(runtimeRepositoryRoot())).toThrowError(
      expect.objectContaining<Partial<ArtifactRefusal>>({
        code: "artifact_directory_must_be_external",
      }),
    );
  });

  it("seals with 0600 and refuses collision without changing existing bytes", () => {
    const output = temporaryDirectory();
    const original = Buffer.from('{"decision":"PASS"}\n');
    const identity = sealArtifact({
      outputDirectory: output,
      name: "round-01.json",
      body: original,
    });
    const target = join(output, identity.name);

    expect(lstatSync(target).mode & 0o7777).toBe(0o600);
    expect(identity.sha256).toBe(createHash("sha256").update(original).digest("hex"));
    expect(() =>
      sealArtifact({
        outputDirectory: output,
        name: "round-01.json",
        body: Buffer.from('{"decision":"FAIL"}\n'),
      }),
    ).toThrowError(
      expect.objectContaining<Partial<ArtifactRefusal>>({ code: "artifact_collision" }),
    );
    expect(readFileSync(target)).toEqual(original);
  });

  it("rejects newline artifact names, non-byte bodies, and oversized caller limits", () => {
    const output = temporaryDirectory();
    expect(() =>
      sealArtifact({
        outputDirectory: output,
        name: "round.json\n",
        body: Buffer.from("{}\n"),
      }),
    ).toThrowError(
      expect.objectContaining<Partial<ArtifactRefusal>>({ code: "artifact_name_invalid" }),
    );
    expect(() =>
      sealArtifact({
        outputDirectory: output,
        name: "round.json",
        body: "not-bytes" as unknown as Uint8Array,
      }),
    ).toThrowError(
      expect.objectContaining<Partial<ArtifactRefusal>>({ code: "artifact_size_invalid" }),
    );
    expect(() =>
      sealArtifact({
        outputDirectory: output,
        name: "round.json",
        body: Buffer.from("{}\n"),
        maximumBytes: 16 * 1024 * 1024 + 1,
      }),
    ).toThrowError(
      expect.objectContaining<Partial<ArtifactRefusal>>({ code: "artifact_size_invalid" }),
    );
  });
});

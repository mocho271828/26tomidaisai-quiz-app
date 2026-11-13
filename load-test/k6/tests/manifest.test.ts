import {
  chmodSync,
  linkSync,
  lstatSync,
  mkdtempSync,
  realpathSync,
  rmSync,
  symlinkSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { afterEach, describe, expect, it } from "vitest";

import { canonicalSha256 } from "../src/canonical-json.js";
import {
  ManifestRefusal,
  loadManifest,
  runtimeRepositoryRoot,
} from "../src/manifest.js";
import {
  PYTHON_FEFF_TITLE_MANIFEST_SHA256,
  PYTHON_INFORMATION_SEPARATOR_TITLE_MANIFEST_SHA256,
  PYTHON_MANIFEST_SHA256,
  validManifestFixture,
} from "./fixtures/manifest.js";

const temporaryDirectories: string[] = [];
const REPOSITORY_ROOT = realpathSync(fileURLToPath(new URL("../../..", import.meta.url)));

function externalDirectory(): string {
  const directory = realpathSync(mkdtempSync(join(realpathSync(tmpdir()), "quiz-k6-manifest-")));
  chmodSync(directory, 0o700);
  temporaryDirectories.push(directory);
  return directory;
}

function writeManifest(directory: string, body: Uint8Array | string): string {
  const path = join(directory, "manifest.json");
  writeFileSync(path, body, { mode: 0o600 });
  chmodSync(path, 0o600);
  return path;
}

function expectRefusal(action: () => unknown, code: string): void {
  try {
    action();
  } catch (error: unknown) {
    expect(error).toBeInstanceOf(ManifestRefusal);
    expect((error as ManifestRefusal).code).toBe(code);
    expect((error as Error).message).toBe(code);
    return;
  }
  throw new Error("expected manifest refusal");
}

afterEach(() => {
  for (const directory of temporaryDirectories.splice(0)) {
    rmSync(directory, { force: true, recursive: true });
  }
});

describe("loadManifest", () => {
  it("caller入力なしでsource checkoutのrepository rootを導出する", () => {
    expect(runtimeRepositoryRoot()).toBe(REPOSITORY_ROOT);
  });

  it("repository外の0600 manifestをstrict parseしてPython互換hashを返す", () => {
    const value = validManifestFixture();
    const path = writeManifest(externalDirectory(), `${JSON.stringify(value, null, 2)}\n`);

    const loaded = loadManifest(path);

    expect(loaded.manifest).toEqual(value);
    expect(loaded.hashes.manifestSha256).toBe(canonicalSha256(value));
    expect(loaded.hashes.scenarioSha256).toBe(canonicalSha256(value.scenario));
  });

  it("relative pathとrepository内parentを拒否する", () => {
    expectRefusal(() => loadManifest("manifest.json"), "manifest_path_invalid");
    const repositoryFixture = fileURLToPath(
      new URL("./fixtures/canonical-python-golden.json", import.meta.url),
    );
    expectRefusal(
      () => loadManifest(repositoryFixture),
      "manifest_parent_invalid",
    );
  });

  it("shared-write parentとsymlink ancestorを拒否する", () => {
    const shared = externalDirectory();
    chmodSync(shared, 0o720);
    const sharedPath = writeManifest(shared, JSON.stringify(validManifestFixture()));
    expectRefusal(() => loadManifest(sharedPath), "manifest_parent_invalid");

    const root = externalDirectory();
    const real = join(root, "real");
    const alias = join(root, "alias");
    const manifest = validManifestFixture();
    writeFileSync(real, JSON.stringify(manifest), { mode: 0o600 });
    chmodSync(real, 0o600);
    symlinkSync(dirname(real), alias);
    expectRefusal(
      () => loadManifest(join(alias, "real")),
      "manifest_path_invalid",
    );

    const finalLinkDirectory = externalDirectory();
    const finalLinkTarget = writeManifest(
      finalLinkDirectory,
      JSON.stringify(validManifestFixture()),
    );
    const finalLink = join(finalLinkDirectory, "manifest-link.json");
    symlinkSync(finalLinkTarget, finalLink);
    expectRefusal(() => loadManifest(finalLink), "manifest_path_invalid");
  });

  it("0600・single-link・size境界外のfileを拒否する", () => {
    const permissionDirectory = externalDirectory();
    const permissionPath = writeManifest(permissionDirectory, JSON.stringify(validManifestFixture()));
    chmodSync(permissionPath, 0o640);
    expectRefusal(() => loadManifest(permissionPath), "manifest_file_invalid");

    const hardLinkDirectory = externalDirectory();
    const hardLinkPath = writeManifest(hardLinkDirectory, JSON.stringify(validManifestFixture()));
    linkSync(hardLinkPath, join(hardLinkDirectory, "second-link.json"));
    expectRefusal(() => loadManifest(hardLinkPath), "manifest_file_invalid");

    const specialBitsDirectory = externalDirectory();
    const specialBitsPath = writeManifest(
      specialBitsDirectory,
      JSON.stringify(validManifestFixture()),
    );
    chmodSync(specialBitsPath, 0o1600);
    expect(lstatSync(specialBitsPath).mode & 0o7777).toBe(0o1600);
    expectRefusal(() => loadManifest(specialBitsPath), "manifest_file_invalid");

    const oversizedPath = writeManifest(externalDirectory(), Buffer.alloc(1_000_001, 0x20));
    expectRefusal(() => loadManifest(oversizedPath), "manifest_file_invalid");
  });

  it("1 byte未満を拒否しexact 1MBを受理する", () => {
    const emptyPath = writeManifest(externalDirectory(), Buffer.alloc(0));
    expectRefusal(() => loadManifest(emptyPath), "manifest_file_invalid");

    const serialized = Buffer.from(JSON.stringify(validManifestFixture()), "utf8");
    const exactLimit = Buffer.concat([
      serialized,
      Buffer.alloc(1_000_000 - serialized.length, 0x20),
    ]);
    const limitPath = writeManifest(externalDirectory(), exactLimit);

    expect(loadManifest(limitPath).hashes.manifestSha256).toBe(PYTHON_MANIFEST_SHA256);
  });

  it.each([
    ["invalid UTF-8", Buffer.from([0xff])],
    ["BOM", Buffer.from("\ufeff{}", "utf8")],
    ["NUL", Buffer.from("{}\0", "utf8")],
    ["invalid JSON", Buffer.from("{", "utf8")],
  ])("不正なJSON bytesを固定codeで拒否する: %s", (_label, body) => {
    const path = writeManifest(externalDirectory(), body);
    expectRefusal(() => loadManifest(path), "manifest_json_invalid");
  });

  it("nested extraと固定contract driftを詳細非公開で拒否する", () => {
    const extra = validManifestFixture();
    Object.assign(extra.scenario.protocol, { session_cookie: "must-not-leak" });
    const path = writeManifest(externalDirectory(), JSON.stringify(extra));
    expectRefusal(() => loadManifest(path), "manifest_contract_invalid");
  });

  it.each([
    ['"base_points":10000', '"base_points":10000.0'],
    ['"virtual_sessions":600', '"virtual_sessions":5.98e2'],
    ['"display_order":1', '"display_order":1e0'],
  ])("Python StrictIntが拒否する数値字句をJSON.parse前に拒否する", (from, to) => {
    const serialized = JSON.stringify(validManifestFixture());
    const mutated = serialized.replace(from, to);
    expect(mutated).not.toBe(serialized);
    const path = writeManifest(externalDirectory(), mutated);

    expectRefusal(() => loadManifest(path), "manifest_contract_invalid");
  });

  it("文字列内の小数・指数表記は数値tokenとして誤検出しない", () => {
    const manifest = validManifestFixture();
    manifest.event.title = "[LOAD-TEST] literal 1.0 and 5.98e2";
    const path = writeManifest(externalDirectory(), JSON.stringify(manifest));

    expect(loadManifest(path).manifest.event.title).toBe(manifest.event.title);
  });

  it("Python strip_whitespaceと同じUnicode集合だけを除去する", () => {
    const manifest = validManifestFixture();
    const baseTitle = manifest.event.title;
    manifest.event.title = `${baseTitle}\u0085`;
    const strippedPath = writeManifest(externalDirectory(), JSON.stringify(manifest));
    const stripped = loadManifest(strippedPath);
    expect(stripped.manifest.event.title).toBe(baseTitle);
    expect(stripped.hashes.manifestSha256).toBe(PYTHON_MANIFEST_SHA256);

    manifest.event.title = `${baseTitle}\ufeff`;
    const preservedPath = writeManifest(externalDirectory(), JSON.stringify(manifest));
    const preserved = loadManifest(preservedPath);
    expect(preserved.manifest.event.title).toBe(`${baseTitle}\ufeff`);
    expect(preserved.hashes.manifestSha256).toBe(PYTHON_FEFF_TITLE_MANIFEST_SHA256);

    manifest.event.title = `${baseTitle}\u001c`;
    const controlPath = writeManifest(externalDirectory(), JSON.stringify(manifest));
    const control = loadManifest(controlPath);
    expect(control.manifest.event.title).toBe(`${baseTitle}\u001c`);
    expect(control.hashes.manifestSha256).toBe(
      PYTHON_INFORMATION_SEPARATOR_TITLE_MANIFEST_SHA256,
    );
  });

  it("Pydantic JSON parserと同じくescaped lone surrogateを拒否する", () => {
    const serialized = JSON.stringify(validManifestFixture());
    const title = '"title":"[LOAD-TEST] load-test-2026-09-03"';
    const loneSurrogate = serialized.replace(
      title,
      '"title":"[LOAD-TEST] load-test-2026-09-03\\ud800"',
    );
    expect(loneSurrogate).not.toBe(serialized);
    const path = writeManifest(externalDirectory(), loneSurrogate);

    expectRefusal(() => loadManifest(path), "manifest_json_invalid");
  });

  it("manifest全階層とhash bindingをimmutableに返す", () => {
    const path = writeManifest(
      externalDirectory(),
      JSON.stringify(validManifestFixture()),
    );
    const loaded = loadManifest(path);
    const manifestHash = loaded.hashes.manifestSha256;

    expect(Object.isFrozen(loaded)).toBe(true);
    expect(Object.isFrozen(loaded.hashes)).toBe(true);
    expect(Object.isFrozen(loaded.manifest)).toBe(true);
    expect(Object.isFrozen(loaded.manifest.questions)).toBe(true);
    expect(Object.isFrozen(loaded.manifest.scenario.lifecycle)).toBe(true);
    expect(() => loaded.manifest.questions.reverse()).toThrow(TypeError);
    expect(loaded.hashes.manifestSha256).toBe(manifestHash);
  });
});

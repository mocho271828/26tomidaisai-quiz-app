import type { Mode, OpenMode, PathLike } from "node:fs";

import { vi } from "vitest";

const filesystemControl = vi.hoisted(() => ({
  beforeOpen: null as ((path: string) => void) | null,
}));

vi.mock("node:fs", async (importOriginal) => {
  const original = await importOriginal<typeof import("node:fs")>();
  return {
    ...original,
    openSync(path: PathLike, flags: OpenMode, mode?: Mode): number {
      filesystemControl.beforeOpen?.(String(path));
      return mode === undefined
        ? original.openSync(path, flags)
        : original.openSync(path, flags, mode);
    },
  };
});

import {
  chmodSync,
  lstatSync,
  mkdirSync,
  mkdtempSync,
  realpathSync,
  renameSync,
  rmSync,
  symlinkSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { afterEach, describe, expect, it } from "vitest";

import {
  ArtifactRefusal,
  validateSecureExternalDirectory,
} from "../src/artifacts.js";

const temporaryRoots: string[] = [];

function ownerOnlyDirectory(path: string): void {
  mkdirSync(path, { mode: 0o700 });
  chmodSync(path, 0o700);
}

afterEach(() => {
  filesystemControl.beforeOpen = null;
  for (const root of temporaryRoots.splice(0)) {
    rmSync(root, { force: true, recursive: true });
  }
});

describe("secure artifact directory race boundary", () => {
  it("rejects an ancestor replacement between lstat and open", () => {
    const root = realpathSync(mkdtempSync(join(tmpdir(), "k6-artifact-race-")));
    chmodSync(root, 0o700);
    temporaryRoots.push(root);

    const originalParent = join(root, "original-parent");
    const originalDirectory = join(originalParent, "artifacts");
    const replacementParent = join(root, "replacement-parent");
    const replacementDirectory = join(replacementParent, "artifacts");
    const movedParent = join(root, "moved-original-parent");
    ownerOnlyDirectory(originalParent);
    ownerOnlyDirectory(originalDirectory);
    ownerOnlyDirectory(replacementParent);
    ownerOnlyDirectory(replacementDirectory);

    filesystemControl.beforeOpen = (path) => {
      if (path !== originalDirectory) {
        return;
      }
      filesystemControl.beforeOpen = null;
      renameSync(originalParent, movedParent);
      symlinkSync(replacementParent, originalParent, "dir");
    };

    expect(() => validateSecureExternalDirectory(originalDirectory)).toThrowError(
      expect.objectContaining<Partial<ArtifactRefusal>>({
        code: "artifact_directory_invalid",
      }),
    );
    expect(lstatSync(originalParent).isSymbolicLink()).toBe(true);
  });
});

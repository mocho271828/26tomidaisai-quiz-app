import { join } from "node:path";

import { describe, expect, it } from "vitest";

import { LauncherRefusal } from "../src/launcher.js";
import {
  formalGeneratorScriptPath,
  runK6GeneratorRuntime,
  runtimeRepositoryRoot,
} from "../src/runtime.js";

describe("formal k6 generator runtime", () => {
  it.runIf(process.platform !== "linux")(
    "refuses a non-Linux host before reading caller paths",
    () => {
      expect(() =>
        runK6GeneratorRuntime({
          preparationEvidencePath: "/secure/preparation.json",
          applicationCaFile: "/secure/ca.pem",
          manifestPath: "/must/not/be/read/manifest.json",
          outputDirectory: "/must/not/be/read/output",
        }),
      ).toThrowError(
        expect.objectContaining<Partial<LauncherRefusal>>({
          code: "launcher_platform_unsupported",
        }),
      );
    },
  );

  it("self-derives the only script accepted by the formal evidence runtime", () => {
    expect(formalGeneratorScriptPath()).toBe(
      join(runtimeRepositoryRoot(), "load-test/k6/dist/generator.js"),
    );
  });
});

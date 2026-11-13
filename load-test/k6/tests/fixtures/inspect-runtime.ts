import { validRuntimeManifestFixture } from "./runtime-manifest.js";

const fixture = validRuntimeManifestFixture();
process.stdout.write(
  `${JSON.stringify({
    LOAD_TEST_RUNTIME_MANIFEST_JSON: fixture.raw,
    LOAD_TEST_RUNTIME_MANIFEST_SHA256:
      fixture.environment.LOAD_TEST_RUNTIME_MANIFEST_SHA256,
  })}\n`,
);

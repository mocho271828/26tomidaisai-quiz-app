import { canonicalJson, canonicalSha256 } from "./canonical-json.js";
import type { LoadedManifest } from "./manifest.js";
import {
  projectK6RuntimeManifest,
  requireBoundedRuntimeManifestText,
} from "./generator/runtime-manifest.js";

const FIXED_CHILD_PATH = "/usr/local/bin:/usr/bin:/bin";

/** ambient値を一切継承せず、manifest bindingだけをchildへ渡す。 */
export function buildK6ChildEnvironment(
  loadedManifest: LoadedManifest,
): NodeJS.ProcessEnv {
  const runtimeManifest = projectK6RuntimeManifest(
    loadedManifest.manifest,
    loadedManifest.hashes,
  );
  const canonicalRuntimeManifest = requireBoundedRuntimeManifestText(
    canonicalJson(runtimeManifest),
  );
  return {
    PATH: FIXED_CHILD_PATH,
    LANG: "C.UTF-8",
    LC_ALL: "C.UTF-8",
    TZ: "UTC",
    LOAD_TEST_RUNTIME_MANIFEST_JSON: canonicalRuntimeManifest,
    LOAD_TEST_RUNTIME_MANIFEST_SHA256: canonicalSha256(runtimeManifest),
  };
}

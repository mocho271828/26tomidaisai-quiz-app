import { createHash } from "node:crypto";
import { canonicalJsonBytes } from "./canonical-json-core.js";

export {
  canonicalJson,
  canonicalJsonBytes,
  CanonicalJsonError,
} from "./canonical-json-core.js";

export function canonicalSha256(value: unknown): string {
  return createHash("sha256").update(canonicalJsonBytes(value)).digest("hex");
}

const ESCAPES: Readonly<Record<number, string>> = {
  0x08: "\\b",
  0x09: "\\t",
  0x0a: "\\n",
  0x0c: "\\f",
  0x0d: "\\r",
};

export class CanonicalJsonError extends Error {
  public constructor() {
    super("canonical_json_value_invalid");
    this.name = "CanonicalJsonError";
  }
}

function escapeCodeUnit(codeUnit: number): string {
  return `\\u${codeUnit.toString(16).padStart(4, "0")}`;
}

function quoteString(value: string): string {
  let result = '"';
  for (const character of value) {
    const codePoint = character.codePointAt(0);
    if (codePoint === undefined) {
      throw new CanonicalJsonError();
    }
    if (character === '"') {
      result += '\\"';
    } else if (character === "\\") {
      result += "\\\\";
    } else if (codePoint in ESCAPES) {
      result += ESCAPES[codePoint];
    } else if (codePoint < 0x20 || codePoint >= 0x7f) {
      if (codePoint <= 0xffff) {
        result += escapeCodeUnit(codePoint);
      } else {
        const offset = codePoint - 0x10000;
        result += escapeCodeUnit(0xd800 + (offset >> 10));
        result += escapeCodeUnit(0xdc00 + (offset & 0x3ff));
      }
    } else {
      result += character;
    }
  }
  return `${result}"`;
}

function comparePythonStrings(left: string, right: string): number {
  const leftPoints = Array.from(left, (character) => character.codePointAt(0) ?? -1);
  const rightPoints = Array.from(right, (character) => character.codePointAt(0) ?? -1);
  const sharedLength = Math.min(leftPoints.length, rightPoints.length);
  for (let index = 0; index < sharedLength; index += 1) {
    const difference = (leftPoints[index] ?? -1) - (rightPoints[index] ?? -1);
    if (difference !== 0) {
      return difference;
    }
  }
  return leftPoints.length - rightPoints.length;
}

function serialize(value: unknown, ancestors: ReadonlySet<object>): string {
  if (value === null) {
    return "null";
  }
  if (typeof value === "boolean") {
    return value ? "true" : "false";
  }
  if (typeof value === "string") {
    return quoteString(value);
  }
  if (typeof value === "number") {
    if (!Number.isSafeInteger(value)) {
      throw new CanonicalJsonError();
    }
    return Object.is(value, -0) ? "0" : String(value);
  }
  if (typeof value !== "object") {
    throw new CanonicalJsonError();
  }
  if (ancestors.has(value)) {
    throw new CanonicalJsonError();
  }

  const nextAncestors = new Set(ancestors);
  nextAncestors.add(value);
  if (Array.isArray(value)) {
    if (
      Object.keys(value).length !== value.length ||
      Reflect.ownKeys(value).some(
        (key) =>
          key !== "length" &&
          (typeof key !== "string" || !/^(?:0|[1-9]\d*)$/.test(key)),
      )
    ) {
      throw new CanonicalJsonError();
    }
    const items: string[] = [];
    for (let index = 0; index < value.length; index += 1) {
      if (!Object.hasOwn(value, index)) {
        throw new CanonicalJsonError();
      }
      items.push(serialize(value[index], nextAncestors));
    }
    return `[${items.join(",")}]`;
  }

  const prototype = Object.getPrototypeOf(value);
  if (prototype !== Object.prototype && prototype !== null) {
    throw new CanonicalJsonError();
  }
  const record = value as Record<string, unknown>;
  if (Reflect.ownKeys(record).some((key) => typeof key !== "string")) {
    throw new CanonicalJsonError();
  }
  const keys = Object.keys(record).sort(comparePythonStrings);
  const fields = keys.map(
    (key) => `${quoteString(key)}:${serialize(record[key], nextAncestors)}`,
  );
  return `{${fields.join(",")}}`;
}

/** Python json.dumpsのcontroller契約と同一のASCII canonical JSONを返す。 */
export function canonicalJson(value: unknown): string {
  return serialize(value, new Set());
}

export function canonicalJsonBytes(value: unknown): Uint8Array {
  return new TextEncoder().encode(canonicalJson(value));
}

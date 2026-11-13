const MAX_INDEX_HTML_LENGTH = 64 * 1_024;
const MAX_ASSET_PATH_LENGTH = 512;

const HTML_TAG_WHITESPACE = "[\\t\\n\\f\\r ]";
const SAFE_ENTRY_JAVASCRIPT_PATH = createSafeAssetPathPattern("js");
const SAFE_ENTRY_STYLESHEET_PATH = createSafeAssetPathPattern("css");

export type StaticAssetRefusalCode =
  | "generator_static_asset_entry_count_invalid"
  | "generator_static_asset_entry_reference_invalid"
  | "generator_static_asset_input_invalid"
  | "generator_static_asset_input_too_large";

/** 外部入力をmessageへ転記せず、監視可能な固定codeだけで拒否理由を通知する。 */
export class StaticAssetRefusal extends Error {
  readonly code: StaticAssetRefusalCode;

  constructor(code: StaticAssetRefusalCode) {
    super(code);
    this.name = "StaticAssetRefusal";
    this.code = code;
  }
}

export type ViteEntryAssetPaths = Readonly<{
  javascriptPath: string;
  stylesheetPath: string;
}>;

/** Viteのindex HTMLからcanonicalな同一origin entryを1件ずつ抽出する。 */
export function extractViteEntryAssetPaths(indexHtmlValue: unknown): ViteEntryAssetPaths {
  const indexHtml = requireBoundedText(indexHtmlValue, MAX_INDEX_HTML_LENGTH);
  const javascriptPaths: string[] = [];
  const stylesheetPaths: string[] = [];

  for (const tag of indexHtml.match(/<script\b[^>]*>/giu) ?? []) {
    const srcAttributes = readQuotedAttributes(tag, "src");
    if (srcAttributes === null) {
      continue;
    }
    const typeAttributes = readQuotedAttributes(tag, "type");
    if (
      srcAttributes.length !== 1 ||
      typeAttributes === null ||
      typeAttributes.length !== 1 ||
      typeAttributes[0]?.toLowerCase() !== "module"
    ) {
      refuse("generator_static_asset_entry_reference_invalid");
    }
    javascriptPaths.push(
      requireSafePath(
        srcAttributes[0],
        SAFE_ENTRY_JAVASCRIPT_PATH,
        "generator_static_asset_entry_reference_invalid",
      ),
    );
  }

  for (const tag of indexHtml.match(/<link\b[^>]*>/giu) ?? []) {
    const relAttributes = readQuotedAttributes(tag, "rel");
    if (relAttributes === null) {
      continue;
    }
    if (relAttributes.length !== 1) {
      refuse("generator_static_asset_entry_reference_invalid");
    }
    const relTokens = relAttributes[0]?.toLowerCase().split(/[\t\n\f\r ]+/u) ?? [];
    if (!relTokens.includes("stylesheet")) {
      continue;
    }

    const hrefAttributes = readQuotedAttributes(tag, "href");
    if (hrefAttributes === null || hrefAttributes.length !== 1) {
      refuse("generator_static_asset_entry_reference_invalid");
    }
    stylesheetPaths.push(
      requireSafePath(
        hrefAttributes[0],
        SAFE_ENTRY_STYLESHEET_PATH,
        "generator_static_asset_entry_reference_invalid",
      ),
    );
  }

  if (javascriptPaths.length !== 1 || stylesheetPaths.length !== 1) {
    refuse("generator_static_asset_entry_count_invalid");
  }

  return {
    javascriptPath: requirePresent(javascriptPaths[0]),
    stylesheetPath: requirePresent(stylesheetPaths[0]),
  };
}

function createSafeAssetPathPattern(extensionPattern: string): RegExp {
  return new RegExp(
    `^/assets/(?:[A-Za-z0-9_-]+/)*[A-Za-z0-9][A-Za-z0-9._-]*\\.${extensionPattern}$`,
    "u",
  );
}

function requireBoundedText(value: unknown, maximumLength: number): string {
  if (typeof value !== "string") {
    refuse("generator_static_asset_input_invalid");
  }
  if (value.length > maximumLength) {
    refuse("generator_static_asset_input_too_large");
  }
  return value;
}

/** nullは属性自体が無い状態、空配列は未quotedまたは欠損した属性を表す。 */
function readQuotedAttributes(tag: string, attributeName: "href" | "rel" | "src" | "type"):
  string[] | null {
  const namePattern = new RegExp(
    `${HTML_TAG_WHITESPACE}+${attributeName}(?=${HTML_TAG_WHITESPACE}|=|/?>)`,
    "giu",
  );
  const nameCount = [...tag.matchAll(namePattern)].length;
  if (nameCount === 0) {
    return null;
  }

  const valuePattern = new RegExp(
    `${HTML_TAG_WHITESPACE}+${attributeName}${HTML_TAG_WHITESPACE}*=${HTML_TAG_WHITESPACE}*(?:"([^"]*)"|'([^']*)')`,
    "giu",
  );
  const values = [...tag.matchAll(valuePattern)].map((match) => match[1] ?? match[2] ?? "");
  return values.length === nameCount ? values : [];
}

function requireSafePath(
  path: string | undefined,
  safePattern: RegExp,
  refusalCode: "generator_static_asset_entry_reference_invalid",
): string {
  if (
    path === undefined ||
    path.length === 0 ||
    path.length > MAX_ASSET_PATH_LENGTH ||
    !safePattern.test(path)
  ) {
    refuse(refusalCode);
  }
  return path;
}

function requirePresent(value: string | undefined): string {
  if (value === undefined) {
    refuse("generator_static_asset_entry_count_invalid");
  }
  return value;
}

function refuse(code: StaticAssetRefusalCode): never {
  throw new StaticAssetRefusal(code);
}

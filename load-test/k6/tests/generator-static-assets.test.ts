import { describe, expect, it } from "vitest";

import {
  extractViteEntryAssetPaths,
  StaticAssetRefusal,
  type StaticAssetRefusalCode,
} from "../src/generator/static-assets.js";

const VITE_INDEX_HTML = `<!doctype html>
<html lang="ja">
  <head>
    <script type="module" crossorigin src="/assets/index-BF5hG0DP.js"></script>
    <link rel="stylesheet" crossorigin href="/assets/index-B0FM2Pxn.css">
  </head>
  <body><div id="root"></div></body>
</html>`;

function expectRefusal(action: () => unknown, code: StaticAssetRefusalCode): void {
  expect(action).toThrowError(new StaticAssetRefusal(code));
}

describe("generator static asset discovery", () => {
  it("属性順・single quote・rel token順に依存せずentryを抽出する", () => {
    const html = `
      <link href='/assets/app.css' rel='preload stylesheet'>
      <script crossorigin src='/assets/app.js' type='MODULE'></script>
    `;

    expect(extractViteEntryAssetPaths(html)).toEqual({
      javascriptPath: "/assets/app.js",
      stylesheetPath: "/assets/app.css",
    });
  });

  it.each([
    "https://evil.example/assets/app.js",
    "//evil.example/assets/app.js",
    "/assets/app.js?v=1",
    "/assets/app.js#fragment",
    "/assets/../app.js",
    "/assets/%2e%2e/app.js",
  ])("危険なJS entry参照 %s を固定codeで拒否する", (javascriptPath) => {
    const html = VITE_INDEX_HTML.replace("/assets/index-BF5hG0DP.js", javascriptPath);

    expectRefusal(
      () => extractViteEntryAssetPaths(html),
      "generator_static_asset_entry_reference_invalid",
    );
  });

  it.each([
    "https://evil.example/assets/app.css",
    "//evil.example/assets/app.css",
    "/assets/app.css?v=1",
    "/assets/app.css#fragment",
    "/assets/../app.css",
    "/assets/%2e%2e/app.css",
  ])("危険なCSS entry参照 %s を固定codeで拒否する", (stylesheetPath) => {
    const html = VITE_INDEX_HTML.replace("/assets/index-B0FM2Pxn.css", stylesheetPath);

    expectRefusal(
      () => extractViteEntryAssetPaths(html),
      "generator_static_asset_entry_reference_invalid",
    );
  });

  it("JS/CSS entryの重複と欠落を同じ固定count codeで拒否する", () => {
    const duplicateScript = VITE_INDEX_HTML.replace(
      "</head>",
      '<script type="module" src="/assets/second.js"></script></head>',
    );
    const duplicateStylesheet = VITE_INDEX_HTML.replace(
      "</head>",
      '<link rel="stylesheet" href="/assets/second.css"></head>',
    );
    const missingScript = VITE_INDEX_HTML.replace(
      /<script[^>]*><\/script>/u,
      "",
    );
    const missingStylesheet = VITE_INDEX_HTML.replace(/<link[^>]*>/u, "");

    for (const html of [
      duplicateScript,
      duplicateStylesheet,
      missingScript,
      missingStylesheet,
    ]) {
      expectRefusal(
        () => extractViteEntryAssetPaths(html),
        "generator_static_asset_entry_count_invalid",
      );
    }
  });

  it("unquoted・重複属性・classic scriptを曖昧なentryとして拒否する", () => {
    const unquoted = VITE_INDEX_HTML.replace(
      'src="/assets/index-BF5hG0DP.js"',
      "src=/assets/index-BF5hG0DP.js",
    );
    const duplicateAttribute = VITE_INDEX_HTML.replace(
      'src="/assets/index-BF5hG0DP.js"',
      'src="/assets/index-BF5hG0DP.js" src="/assets/other.js"',
    );
    const classicScript = VITE_INDEX_HTML.replace('type="module" ', "");

    for (const html of [unquoted, duplicateAttribute, classicScript]) {
      expectRefusal(
        () => extractViteEntryAssetPaths(html),
        "generator_static_asset_entry_reference_invalid",
      );
    }
  });

  it("非stringと上限超過HTMLを入力値を漏らさない固定codeで拒否する", () => {
    expectRefusal(
      () => extractViteEntryAssetPaths(null),
      "generator_static_asset_input_invalid",
    );

    const secret = "do-not-leak";
    let refusal: unknown;
    try {
      extractViteEntryAssetPaths(`${secret}${"x".repeat(64 * 1_024)}`);
    } catch (error: unknown) {
      refusal = error;
    }

    expect(refusal).toEqual(
      new StaticAssetRefusal("generator_static_asset_input_too_large"),
    );
    expect(String(refusal)).not.toContain(secret);
    expect((refusal as Error).stack).not.toContain(secret);
  });

});

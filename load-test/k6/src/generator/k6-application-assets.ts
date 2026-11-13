import http, {
  type CookieJar,
  type RefinedParams,
  type RefinedResponse,
} from "k6/http";

import {
  classifyHttpStatus,
  type GeneratorHttpStatusClass,
} from "./k6-classification.js";
import {
  type GeneratorHttpOutcome,
  type GeneratorStaticAssetFailure,
  type K6GeneratorMetrics,
} from "./k6-metrics.js";
import {
  extractViteEntryAssetPaths,
} from "./static-assets.js";

const APPLICATION_ASSET_TIMEOUT_MS = 5_000;
const EXPECTED_OK = http.expectedStatuses(200);

type ApplicationAssetRoute =
  | "app_index"
  | "app_javascript"
  | "app_stylesheet";

export const APPLICATION_ASSET_HTTP_NAMES = {
  app_index: "GET /",
  app_javascript: "GET /assets/{javascript}",
  app_stylesheet: "GET /assets/{stylesheet}",
} as const satisfies Record<ApplicationAssetRoute, string>;

interface K6ApplicationAssetRequestOptions {
  readonly origin: string;
  readonly cacheMode: "cold" | "warm";
  readonly jar: CookieJar;
  readonly metrics: K6GeneratorMetrics;
  readonly clock: () => number;
}

/** participantと同じorigin・Cookie jarで公開application bundle一式を取得する。 */
export function fetchK6ApplicationAssets(
  options: K6ApplicationAssetRequestOptions,
): GeneratorStaticAssetFailure | null {
  const index = getStaticText(options, "app_index", "/");
  if (index === null) {
    return "index_request_failed";
  }

  let entryPaths: ReturnType<typeof extractViteEntryAssetPaths>;
  try {
    entryPaths = extractViteEntryAssetPaths(index);
  } catch {
    return "bundle_contract_invalid";
  }
  const javascript = getStaticText(
    options,
    "app_javascript",
    entryPaths.javascriptPath,
  );
  if (javascript === null) {
    return "javascript_request_failed";
  }
  const stylesheet = getStaticText(
    options,
    "app_stylesheet",
    entryPaths.stylesheetPath,
  );
  if (stylesheet === null) {
    return "stylesheet_request_failed";
  }

  return null;
}

function getStaticText(
  options: K6ApplicationAssetRequestOptions,
  route: "app_index" | "app_javascript" | "app_stylesheet",
  path: string,
): string | null {
  const startedAt = options.clock();
  let response: RefinedResponse<"text">;
  try {
    response = http.get(
      `${options.origin}${path}`,
      assetParams(options, route, "text"),
    );
  } catch {
    options.metrics.recordHttp(
      route,
      "transport_failure",
      options.clock() - startedAt,
    );
    return null;
  }
  const endedAt = options.clock();
  const statusClass = classifyHttpStatus(response.status);
  options.metrics.recordHttp(route, httpMetricOutcome(statusClass), endedAt - startedAt);
  return statusClass === "success" ? response.body : null;
}

function assetParams<ResponseKind extends "binary" | "text">(
  options: K6ApplicationAssetRequestOptions,
  route: ApplicationAssetRoute,
  responseType: ResponseKind,
): RefinedParams<ResponseKind> {
  return {
    headers: {
      Accept: route === "app_index" ? "text/html" : "*/*",
      Origin: options.origin,
    },
    jar: options.jar,
    redirects: 0,
    responseCallback: EXPECTED_OK,
    responseType,
    tags: {
      cache_mode: options.cacheMode,
      name: APPLICATION_ASSET_HTTP_NAMES[route],
    },
    timeout: APPLICATION_ASSET_TIMEOUT_MS,
  };
}

function httpMetricOutcome(statusClass: GeneratorHttpStatusClass): GeneratorHttpOutcome {
  if (statusClass === "success") {
    return "success";
  }
  if (statusClass === "client") {
    return "client_failure";
  }
  return statusClass === "server" ? "server_failure" : "transport_failure";
}

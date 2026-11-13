import { get } from "node:https";
import { chromium, type Browser } from "playwright";

const readinessUrl = "https://10.0.1.2:8443/api/health/ready";
const participantUrl = "https://10.0.1.2:8443";
const timeoutMs = 30000;

export async function checkLanReadiness(ca: string): Promise<void> {
  await new Promise<void>((resolve, reject) => {
    let settled = false;
    const finish = (reason?: string): void => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      if (reason) reject(new Error(reason)); else resolve();
    };
    // redirectを追わず、転送済みCAとhostnameを通常のTLS経路で検証する。
    const request = get(readinessUrl, {ca, rejectUnauthorized: true, agent: false}, response => {
      if (response.statusCode !== 200) {
        finish("lan_readiness_unavailable"); response.destroy(); return;
      }
      response.on("error", () => finish("lan_readiness_failed"));
      response.on("end", () => finish());
      response.resume();
    });
    const timer = setTimeout(() => {
      finish("lan_readiness_timeout"); request.destroy();
    }, timeoutMs);
    request.on("error", () => finish("lan_readiness_tls_or_network_failed"));
  });
}

export async function checkLanBrowser(): Promise<void> {
  let browser: Browser | undefined;
  try {
    browser = await chromium.launch({headless: true, timeout: timeoutMs,
      args: ["--disable-background-timer-throttling", "--disable-renderer-backgrounding"]});
    const context = await browser.newContext({viewport: {width: 390, height: 844}, locale: "ja-JP"});
    const page = await context.newPage();
    const deadline = Date.now() + timeoutMs;
    const remaining = (): number => Math.max(1, deadline - Date.now());
    await page.goto(participantUrl, {waitUntil: "domcontentloaded", timeout: remaining()});
    await page.getByRole("button", {name: "参加する", exact: true}).waitFor({state: "visible", timeout: remaining()});
  } catch { throw new Error("lan_browser_tls_or_page_failed"); }
  finally { await browser?.close(); }
}

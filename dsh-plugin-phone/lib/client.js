/**
 * Thin HTTP client for the local phone service.
 *
 * No DSH imports here so this module can be tested with plain Node.
 */
import { readFileSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";

const DEFAULT_BASE_URL = "http://127.0.0.1:8765";
const DEFAULT_TOKEN_FILE = join(homedir(), ".dsh", "phone-service", "token");
// A still screen makes the service fall back to single adb frames, which can
// take several seconds; 15s used to turn that into a spurious UNREACHABLE.
const DEFAULT_TIMEOUT_MS = 30000;
// One retry for idempotent calls only: a capture that raced the scrcpy warmup
// usually succeeds on the second try instead of costing the agent a step.
const RETRY_DELAY_MS = 300;
// The service serialises captures behind a lock (it waits up to 1s for it) and
// answers 409 BUSY; wait slightly longer than that lock budget before retrying.
const BUSY_RETRY_DELAY_MS = 800;

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

export class PhoneServiceError extends Error {
  constructor(code, message = "", status = 0) {
    super(message ? `${code}: ${message}` : code);
    this.name = "PhoneServiceError";
    this.code = code;
    this.status = status;
  }
}

function readTokenFile(path) {
  try {
    return readFileSync(path, "utf8").trim();
  } catch {
    return "";
  }
}

function resolveToken(token, tokenFile) {
  if (token) return token;
  if (process.env.PHONE_SERVICE_TOKEN) return process.env.PHONE_SERVICE_TOKEN;
  return readTokenFile(tokenFile || process.env.PHONE_SERVICE_TOKEN_FILE || DEFAULT_TOKEN_FILE);
}

export function createPhoneClient({ baseUrl, token, tokenFile, timeoutMs = DEFAULT_TIMEOUT_MS } = {}) {
  const root = String(baseUrl || process.env.PHONE_SERVICE_URL || DEFAULT_BASE_URL).replace(/\/+$/, "");

  /**
   * @param {string} path
   * @param {object} [options]
   * @param {number} [options.retries] Retry an UNREACHABLE failure this many
   *   times. Only pass this for idempotent, side-effect-free calls.
   * @param {boolean} [options.retryOnBusy] Retry once when the service reports
   *   its capture lock as busy (409 BUSY).
   */
  async function request(path, { method = "GET", body, raw = false, retries = 0, retryOnBusy = false } = {}) {
    const authToken = resolveToken(token, tokenFile);
    const headers = {};
    if (body !== undefined) headers["Content-Type"] = "application/json";
    if (authToken) headers.Authorization = `Bearer ${authToken}`;

    async function attempt() {
      let response;
      try {
        response = await fetch(`${root}${path}`, {
          method,
          headers,
          body: body === undefined ? undefined : JSON.stringify(body),
          signal: AbortSignal.timeout(timeoutMs),
        });
      } catch (error) {
        throw new PhoneServiceError(
          "UNREACHABLE",
          `phone service is not reachable at ${root}: ${error.message}`,
        );
      }
      if (raw) {
        if (!response.ok) {
          throw new PhoneServiceError(`HTTP_${response.status}`, await response.text(), response.status);
        }
        return Buffer.from(await response.arrayBuffer());
      }
      const text = await response.text();
      let payload;
      try {
        payload = text ? JSON.parse(text) : {};
      } catch {
        throw new PhoneServiceError("BAD_RESPONSE", `non-JSON response: ${text.slice(0, 200)}`, response.status);
      }
      if (!response.ok || payload?.ok === false) {
        const error = payload?.error || {};
        throw new PhoneServiceError(
          error.code || `HTTP_${response.status}`,
          error.message || text.slice(0, 300),
          response.status,
        );
      }
      return payload;
    }

    let retriedUnreachable = false;
    let retriedBusy = false;
    for (;;) {
      try {
        return await attempt();
      } catch (error) {
        if (!(error instanceof PhoneServiceError)) throw error;
        if (error.code === "UNREACHABLE" && retries > 0 && !retriedUnreachable) {
          retriedUnreachable = true;
          await sleep(RETRY_DELAY_MS);
          continue;
        }
        // Only capture calls opt in: the lock is held by another capture that
        // will usually be done well before the second attempt.
        if (error.code === "BUSY" && retryOnBusy && !retriedBusy) {
          retriedBusy = true;
          await sleep(BUSY_RETRY_DELAY_MS);
          continue;
        }
        throw error;
      }
    }
  }

  return {
    baseUrl: root,
    get hasToken() {
      return Boolean(resolveToken(token, tokenFile));
    },
    health: () => request("/health", { retries: 1 }),
    status: () => request("/status", { retries: 1 }),
    /** Ask the service to stop (the supervisor uses this to restart it). */
    shutdown: () => request("/shutdown", { method: "POST", body: {} }),
    screenshot: ({ includeBase64 = false, maxWidth } = {}) =>
      request("/screenshot", {
        method: "POST",
        retries: 1,
        retryOnBusy: true,
        body: {
          include_base64: Boolean(includeBase64),
          ...(maxWidth ? { max_width: maxWidth } : {}),
        },
      }),
    ocr: ({ minScore = 0.4, roi } = {}) =>
      request(
        `/ocr?min_score=${encodeURIComponent(minScore)}` + (roi ? `&roi=${encodeURIComponent(roi)}` : ""),
        { method: "GET", retries: 1, retryOnBusy: true },
      ),
    find: ({ keyword, minScore = 0.6, mode = "contains", roi, index = 0 } = {}) =>
      request("/find", {
        method: "POST",
        retries: 1,
        retryOnBusy: true,
        body: {
          text: keyword,
          min_score: minScore,
          mode,
          index,
          ...(roi ? { roi } : {}),
        },
      }),
    click: (x, y, holdMs) =>
      request("/tap", {
        method: "POST",
        body: { x, y, ...(holdMs ? { hold_ms: holdMs } : {}) },
      }),
    swipe: ({ x1, y1, x2, y2, ms = 400 }) =>
      request("/swipe", { method: "POST", body: { x1, y1, x2, y2, ms } }),
    provisionDevices: ({ timeout = 8 } = {}) =>
      request(`/provision/devices?timeout=${encodeURIComponent(timeout)}`),
    startProvision: ({ ssid, password, device, pop, timeout = 180 } = {}) =>
      request("/provision", {
        method: "POST",
        body: {
          ssid,
          password,
          ...(device ? { device } : {}),
          ...(pop ? { pop } : {}),
          timeout,
        },
      }),
    armProvisioning: () => request("/provision/arm", { method: "POST", body: {} }),
    provisionStatus: () => request("/provision/status"),
  };
}

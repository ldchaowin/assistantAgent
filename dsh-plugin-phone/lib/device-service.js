/**
 * Aggregated device status for the UI.
 *
 * The browser cannot call the local phone service directly (the token lives on
 * the host and the service is cross-origin), so the host probes it and returns
 * a small, stable shape. A slow `/status` is expected when the hand is offline;
 * the timeout is short and the error is folded into `device.reachable=false`
 * so the UI can tell "service down" from "device offline".
 */

const DEFAULT_TIMEOUT_MS = 2500;
const DEFAULT_TTL_MS = 5000;

function asRecord(value) {
  return value && typeof value === "object" ? value : null;
}

function asScreen(value) {
  if (!Array.isArray(value) || value.length < 2) return null;
  const width = Number(value[0]);
  const height = Number(value[1]);
  return Number.isFinite(width) && Number.isFinite(height) && width > 0 && height > 0
    ? [width, height]
    : null;
}

function asWifi(value) {
  const wifi = asRecord(value);
  if (!wifi) return null;
  return {
    state: typeof wifi.state === "string" ? wifi.state : null,
    ssid: typeof wifi.ssid === "string" ? wifi.ssid : "",
    ip: typeof wifi.ip === "string" ? wifi.ip : "",
    rssi: Number.isFinite(Number(wifi.rssi)) ? Number(wifi.rssi) : null,
    port: Number.isFinite(Number(wifi.port)) ? Number(wifi.port) : null,
  };
}

function asCapture(value) {
  const capture = asRecord(value);
  if (!capture) return null;
  return {
    name: typeof capture.name === "string" ? capture.name : null,
    stats: asRecord(capture.stats),
    ocrCachedFrames: Number.isFinite(Number(capture.ocr_cached_frames))
      ? Number(capture.ocr_cached_frames)
      : null,
  };
}

function serviceError(error, baseUrl, command = "") {
  const result = new Error(
    `本地 phone 服务没有运行（${baseUrl}）`,
  );
  result.code = "SERVICE_DOWN";
  result.details = {
    baseUrl,
    command,
    cause: error?.message ?? String(error),
  };
  return result;
}

export function createDeviceService({
  clientFactory,
  config = {},
  logger = console,
  now = () => Date.now(),
} = {}) {
  if (typeof clientFactory !== "function") {
    throw new Error("clientFactory is required");
  }
  const timeoutMs = Math.max(500, Number(config.deviceStatusTimeoutMs || DEFAULT_TIMEOUT_MS));
  // The first screenshot after adb reconnects can take seconds (scrcpy warmup),
  // so the probe gets its own budget instead of the 2.5s status timeout.
  const probeTimeoutMs = Math.max(
    timeoutMs,
    Number(config.captureProbeTimeoutMs) || 8000,
  );
  const ttlMs = Math.max(0, Number(config.deviceStatusTtlMs ?? DEFAULT_TTL_MS));
  const thumbnailTtlMs = Math.max(0, Number(config.thumbnailTtlMs ?? 1200));
  const startCommand = Array.isArray(config.serviceCommand)
    ? config.serviceCommand.map(String).join(" ")
    : "";
  const cache = new Map();
  const thumbCache = new Map();
  const thumbInflight = new Map();

  function clientFor(device, timeoutOverride) {
    const name = device || config.defaultDevice || "default";
    const entry = config.devices?.[name];
    const base = entry || config;
    return clientFactory({
      device: name,
      baseUrl: base.baseUrl,
      token: base.token,
      tokenFile: base.tokenFile,
      timeoutMs: timeoutOverride ?? timeoutMs,
    });
  }

  async function probe(device) {
    const client = clientFor(device);
    const checkedAt = now();
    try {
      await client.health();
    } catch (error) {
      throw serviceError(error, client.baseUrl, startCommand);
    }

    let payload;
    try {
      payload = await client.status();
    } catch (error) {
      // A healthy service with an unreachable hand is a normal UI state, not
      // a server error: return it in-band so the panel can offer provision/retry.
      logger?.warn?.(`dsh-plugin-phone: device status unavailable: ${error?.message ?? error}`);
      return {
        checkedAt,
        service: { ok: true, baseUrl: client.baseUrl, version: null },
        device: {
          reachable: false,
          label: config.deviceLabel || null,
          error: {
            code: error?.code || "DEVICE_UNREACHABLE",
            message: error?.message ?? String(error),
          },
        },
        capture: null,
        captureProbe: null,
      };
    }

    // `capture.stats` describes the scrcpy stream, which can be stale (the
    // stream dies while adb screencap still works). A tiny screenshot probe is
    // the only honest "can the Eye see right now" signal.
    let captureProbe = null;
    if (config.probeCapture !== false && typeof client.screenshot === "function") {
      const probeClient = clientFor(device, probeTimeoutMs);
      try {
        await probeClient.screenshot({ maxWidth: 64 });
        captureProbe = { ok: true, at: Date.now() };
      } catch (error) {
        captureProbe = {
          ok: false,
          at: Date.now(),
          error: error?.message ?? String(error),
        };
      }
    }

    const status = asRecord(payload?.status) || {};
    const hand = asRecord(payload?.hand);
    return {
      checkedAt,
      service: {
        ok: true,
        baseUrl: client.baseUrl,
        version: typeof payload?.version === "string" ? payload.version : null,
      },
      device: {
        reachable: true,
        platform: typeof payload?.platform === "string" ? payload.platform : null,
        label: config.deviceLabel || null,
        state: typeof status.state === "string" ? status.state : null,
        uptime: Number.isFinite(Number(status.uptime)) ? Number(status.uptime) : null,
        queue: Number.isFinite(Number(status.queue)) ? Number(status.queue) : null,
        usb: typeof status.usb === "boolean" ? status.usb : null,
        wifi: asWifi(status.wifi),
        lastError: typeof status.last_err === "string" ? status.last_err : "",
        screen: asScreen(payload?.screen),
        hand: hand
          ? {
              ok: hand.ok !== false,
              error: typeof hand.error === "string" ? hand.error : "",
            }
          : null,
      },
      capture: asCapture(payload?.capture),
      captureProbe,
    };
  }

  return {
    async status(payload = {}) {
      const device = String(payload.device || config.defaultDevice || "default");
      const force = Boolean(payload.force);
      const cached = cache.get(device);
      if (!force && cached && now() - cached.at < ttlMs) {
        return cached.value;
      }
      const value = await probe(device);
      cache.set(device, { at: value.checkedAt, value });
      return value;
    },

    async thumbnail(payload = {}) {
      const device = String(payload.device || config.defaultDevice || "default");
      const force = Boolean(payload.force);
      const maxWidth = Math.max(80, Math.min(480, Number(payload.maxWidth) || 240));
      const key = `${device}:${maxWidth}`;
      const cached = thumbCache.get(key);
      if (!force && cached && now() - cached.at < thumbnailTtlMs) {
        return cached.value;
      }
      if (thumbInflight.has(key)) return thumbInflight.get(key);
      const request = (async () => {
        const client = clientFor(device, Math.max(timeoutMs, 6000));
        let payloadResult;
        try {
          payloadResult = await client.screenshot({ includeBase64: true, maxWidth });
        } catch (error) {
          if (error?.code === "UNREACHABLE") {
            throw serviceError(error, client.baseUrl, startCommand);
          }
          throw error;
        }
        const base64 = typeof payloadResult?.base64 === "string" ? payloadResult.base64 : "";
        if (!base64) {
          const empty = new Error("手机画面没有返回图片数据");
          empty.code = "SCREENSHOT_EMPTY";
          throw empty;
        }
        const value = {
          mime: "image/png",
          base64,
          width: Number(payloadResult?.width) || null,
          height: Number(payloadResult?.height) || null,
          at: now(),
        };
        thumbCache.set(key, { at: value.at, value });
        return value;
      })();
      thumbInflight.set(key, request);
      try {
        return await request;
      } finally {
        thumbInflight.delete(key);
      }
    },

    clear(device) {
      if (device) {
        cache.delete(String(device));
        for (const key of [...thumbCache.keys()]) {
          if (key.startsWith(`${device}:`)) thumbCache.delete(key);
        }
      } else {
        cache.clear();
        thumbCache.clear();
      }
    },
  };
}

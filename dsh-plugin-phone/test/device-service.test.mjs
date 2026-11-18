import assert from "node:assert/strict";
import test from "node:test";
import { createDeviceService } from "../lib/device-service.js";

function makeClient(overrides = {}) {
  return {
    baseUrl: "http://127.0.0.1:8765",
    health: async () => ({ ok: true }),
    status: async () => ({
      ok: true,
      screen: [1264, 2800],
      status: {
        state: "READY",
        uptime: 1234,
        queue: 0,
        usb: true,
        last_err: "",
        wifi: { state: "connected", ssid: "YOUR_WIFI", ip: "192.168.1.20", rssi: -52, port: 9000 },
      },
      capture: { name: "scrcpy", stats: { fps: 30 }, ocr_cached_frames: 2 },
      version: "0.1.0",
    }),
    ...overrides,
  };
}

test("aggregates health and device status into a stable shape", async () => {
  const calls = [];
  const service = createDeviceService({
    clientFactory: (options) => {
      calls.push(options);
      return makeClient();
    },
    config: { deviceLabel: "hand-A1B2" },
    logger: { warn() {} },
  });
  const value = await service.status({});
  assert.equal(calls[0].timeoutMs, 2500);
  assert.equal(value.service.ok, true);
  assert.equal(value.service.version, "0.1.0");
  assert.equal(value.device.reachable, true);
  assert.equal(value.device.label, "hand-A1B2");
  assert.equal(value.device.state, "READY");
  assert.equal(value.device.usb, true);
  assert.deepEqual(value.device.screen, [1264, 2800]);
  assert.equal(value.device.wifi.ssid, "YOUR_WIFI");
  assert.equal(value.device.wifi.rssi, -52);
  assert.equal(value.capture.name, "scrcpy");
  assert.equal(value.capture.stats.fps, 30);
});

test("iOS eye-only payload keeps platform and hand state", async () => {
  const service = createDeviceService({
    clientFactory: () =>
      makeClient({
        status: async () => ({
          ok: true,
          platform: "ios",
          screen: [1179, 2556],
          status: {},
          hand: { ok: false, error: "DISCONNECT: cannot connect to hand" },
          capture: { name: "ios:avfoundation:iPhone", stats: { fps: 30 } },
          version: "0.1.0",
        }),
      }),
    logger: { warn() {} },
  });
  const value = await service.status({});
  assert.equal(value.device.reachable, true);
  assert.equal(value.device.platform, "ios");
  assert.equal(value.device.hand.ok, false);
  assert.match(value.device.hand.error, /DISCONNECT/);
  assert.deepEqual(value.device.screen, [1179, 2556]);
  assert.equal(value.capture.name, "ios:avfoundation:iPhone");
});

test("device status timeout folds into reachable=false", async () => {
  const service = createDeviceService({
    clientFactory: () =>
      makeClient({
        status: async () => {
          const error = new Error("no response to status");
          error.code = "TIMEOUT";
          throw error;
        },
      }),
    logger: { warn() {} },
  });
  const value = await service.status({});
  assert.equal(value.service.ok, true);
  assert.equal(value.device.reachable, false);
  assert.equal(value.device.error.code, "TIMEOUT");
  assert.match(value.device.error.message, /no response/);
});

test("service down is thrown with baseUrl details", async () => {
  const service = createDeviceService({
    clientFactory: () =>
      makeClient({
        health: async () => {
          throw new Error("connection refused");
        },
      }),
    config: { serviceCommand: [".venv/bin/phone", "--tcp", "hand.local", "serve"] },
    logger: { warn() {} },
  });
  await assert.rejects(
    () => service.status({}),
    (error) =>
      error.code === "SERVICE_DOWN" &&
      error.details.baseUrl === "http://127.0.0.1:8765" &&
      error.details.command === ".venv/bin/phone --tcp hand.local serve"
  );
});

test("status cache is reused and force bypasses it", async () => {
  let clock = 1000;
  let statusCalls = 0;
  const service = createDeviceService({
    clientFactory: () =>
      makeClient({
        status: async () => {
          statusCalls += 1;
          return makeClient().status();
        },
      }),
    config: { deviceStatusTtlMs: 5000 },
    now: () => clock,
    logger: { warn() {} },
  });
  await service.status({});
  clock = 3000;
  await service.status({});
  assert.equal(statusCalls, 1);
  await service.status({ force: true });
  assert.equal(statusCalls, 2);
  clock = 9000;
  await service.status({});
  assert.equal(statusCalls, 3);
});

test("per-device entries resolve their own baseUrl and timeout", async () => {
  const calls = [];
  const service = createDeviceService({
    clientFactory: (options) => {
      calls.push(options);
      return makeClient({ baseUrl: options.baseUrl });
    },
    config: {
      baseUrl: "http://127.0.0.1:8765",
      devices: { lab: { baseUrl: "http://127.0.0.1:9000", token: "lab-token" } },
      deviceStatusTimeoutMs: 1800,
    },
    logger: { warn() {} },
  });
  const value = await service.status({ device: "lab" });
  assert.equal(calls[0].baseUrl, "http://127.0.0.1:9000");
  assert.equal(calls[0].token, "lab-token");
  assert.equal(calls[0].timeoutMs, 1800);
  assert.equal(value.service.baseUrl, "http://127.0.0.1:9000");
});

test("thumbnail returns base64 and caps the requested width", async () => {
  let seen = null;
  const service = createDeviceService({
    clientFactory: () =>
      makeClient({
        screenshot: async (options) => {
          seen = options;
          return { base64: "AAAA", width: 240, height: 532 };
        },
      }),
    logger: { warn() {} },
  });
  const value = await service.thumbnail({ maxWidth: 999 });
  assert.deepEqual(seen, { includeBase64: true, maxWidth: 480 });
  assert.equal(value.mime, "image/png");
  assert.equal(value.base64, "AAAA");
  assert.equal(value.width, 240);
  assert.equal(value.height, 532);
  assert.ok(value.at > 0);
});

test("thumbnail rejects an empty screenshot", async () => {
  const service = createDeviceService({
    clientFactory: () => makeClient({ screenshot: async () => ({ width: 0, height: 0 }) }),
    logger: { warn() {} },
  });
  await assert.rejects(
    () => service.thumbnail({ maxWidth: 240 }),
    (error) => error.code === "SCREENSHOT_EMPTY"
  );
});

test("screenshot probe reports the real Eye state", async () => {
  const okService = createDeviceService({
    clientFactory: () => makeClient({ screenshot: async () => ({ ok: true }) }),
    config: {},
    logger: { warn() {} }
  });
  const ok = await okService.status({ force: true });
  assert.equal(ok.captureProbe.ok, true);
  assert.equal(ok.device.reachable, true);

  const downService = createDeviceService({
    clientFactory: () =>
      makeClient({
        screenshot: async () => {
          throw Object.assign(new Error("no ADB device"), { code: "INTERNAL" });
        }
      }),
    config: {},
    logger: { warn() {} }
  });
  const down = await downService.status({ force: true });
  assert.equal(down.captureProbe.ok, false);
  assert.match(down.captureProbe.error, /no ADB device/);
  assert.equal(down.device.reachable, true, "the hand is still fine");
});

test("probeCapture:false skips the screenshot probe", async () => {
  let shots = 0;
  const service = createDeviceService({
    clientFactory: () =>
      makeClient({
        screenshot: async () => {
          shots += 1;
          return { ok: true };
        }
      }),
    config: { probeCapture: false },
    logger: { warn() {} }
  });
  const value = await service.status({ force: true });
  assert.equal(shots, 0);
  assert.equal(value.captureProbe, null);
});

test("capture probe gets a longer timeout than the status call", async () => {
  const options = [];
  const service = createDeviceService({
    clientFactory: (opts) => {
      options.push(opts);
      return makeClient({ screenshot: async () => ({ ok: true }) });
    },
    config: { deviceStatusTimeoutMs: 2500 },
    logger: { warn() {} }
  });
  await service.status({ force: true });
  const timeouts = options.map((item) => item.timeoutMs);
  assert.ok(timeouts.some((value) => value >= 8000), `probe timeout should be >= 8000, got ${timeouts}`);
  assert.ok(timeouts.includes(2500), "the status call keeps the configured timeout");
});

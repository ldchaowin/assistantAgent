import assert from "node:assert/strict";
import test from "node:test";
import {
  PHONE_TASK_CHANNEL,
  PHONE_TASK_ENDPOINTS,
  createTaskRpcHandler,
  installTaskRpc,
} from "../lib/task-rpc.js";

function makeService() {
  return {
    async preflight(payload) {
      return { ok: true, device: payload.device || "default" };
    },
    async start(payload) {
      if (payload.fail) {
        const error = new Error("service down");
        error.code = "PHONE_SERVICE_DOWN";
        error.details = { baseUrl: "http://127.0.0.1:8765" };
        throw error;
      }
      return { id: "task-1", status: "starting" };
    },
    status(id) {
      return id ? { id, status: "running", steps: 2 } : { active: [], history: [] };
    },
    async stop(id) {
      return { id, status: "stopped" };
    },
    history: () => [{ id: "task-1", status: "done" }],
    async deleteHistory(id) {
      return { deleted: id === "task-1" };
    },
    async clearHistory() {
      return { deleted: 2 };
    },
    templates: () => [{ id: "t1", name: "T", objective: "O" }],
    async saveTemplate(payload) {
      return { id: "t2", ...payload };
    },
    async deleteTemplate() {
      return { deleted: true };
    },
    async schedule(payload) {
      return { id: "s1", at: Date.now() + 60000, ...payload };
    },
    async updateSchedule(payload) {
      return { id: payload.id, at: payload.at, objective: payload.objective };
    },
    schedules: () => [],
    async cancelSchedule() {
      return { cancelled: true };
    },
    async replay() {
      return { id: "task-2" };
    },
    devices: () => [{ name: "default", baseUrl: "http://127.0.0.1:8765" }],
  };
}

test("rpc dispatches task endpoints", async () => {
  const handler = createTaskRpcHandler(makeService());
  const started = await handler(PHONE_TASK_ENDPOINTS.start, { objective: "打开设置" });
  assert.deepEqual(started, { ok: true, value: { id: "task-1", status: "starting" } });
  const status = await handler(PHONE_TASK_ENDPOINTS.status, { taskId: "task-1" });
  assert.equal(status.value.steps, 2);
  const history = await handler(PHONE_TASK_ENDPOINTS.history, {});
  assert.equal(history.value.length, 1);
});

test("rpc deletes and clears task history", async () => {
  const handler = createTaskRpcHandler(makeService());
  const deleted = await handler(PHONE_TASK_ENDPOINTS.deleteHistory, { id: "task-1" });
  assert.deepEqual(deleted, { ok: true, value: { deleted: true } });
  const missing = await handler(PHONE_TASK_ENDPOINTS.deleteHistory, { id: "missing" });
  assert.deepEqual(missing, { ok: true, value: { deleted: false } });
  const cleared = await handler(PHONE_TASK_ENDPOINTS.clearHistory, {});
  assert.deepEqual(cleared, { ok: true, value: { deleted: 2 } });
});

test("rpc maps errors to actionable codes", async () => {
  const handler = createTaskRpcHandler(makeService());
  const result = await handler(PHONE_TASK_ENDPOINTS.start, { objective: "x", fail: true });
  assert.equal(result.ok, false);
  assert.equal(result.error.code, "PHONE_SERVICE_DOWN");
  assert.match(result.error.message, /service down/);
  assert.equal(result.error.details.baseUrl, "http://127.0.0.1:8765");
});

test("rpc rejects unknown endpoints", async () => {
  const handler = createTaskRpcHandler(makeService());
  const result = await handler("phone.task.unknown", {});
  assert.equal(result.ok, false);
  assert.equal(result.error.code, "NOT_FOUND");
});

test("installTaskRpc registers the phone-task channel", () => {
  let registered = null;
  const ctx = {
    connection: {
      rpc: {
        handle(channel, handler, options) {
          registered = { channel, handler, options };
          return () => {};
        },
      },
    },
  };
  const dispose = installTaskRpc(ctx, makeService(), { warn() {} });
  assert.equal(registered.channel, PHONE_TASK_CHANNEL);
  assert.equal(registered.options.authority, "loopback");
  assert.equal(typeof dispose, "function");
});

test("installTaskRpc mounts a /dsh-phone-task web route", async () => {
  let route = null;
  const ctx = {
    webServer: {
      register(value) {
        route = value;
        return () => {};
      },
    },
    connection: {
      requestRejection: () => undefined,
    },
  };
  const dispose = installTaskRpc(ctx, makeService(), { info() {}, warn() {} });
  assert.equal(route.path, PHONE_TASK_CHANNEL);
  assert.equal(route.kind, "prefix");
  const chunks = [];
  const req = {
    method: "POST",
    url: `${PHONE_TASK_CHANNEL}/phone.task.status`,
    headers: { host: "127.0.0.1:52113" },
    async *[Symbol.asyncIterator]() {
      yield Buffer.from(JSON.stringify({ taskId: "task-1" }));
    },
  };
  const res = {
    status: 0,
    headers: null,
    body: "",
    writeHead(status, headers) {
      this.status = status;
      this.headers = headers;
    },
    end(body) {
      this.body += body || "";
    },
  };
  await route.handler(req, res);
  assert.equal(res.status, 200);
  assert.equal(JSON.parse(res.body).value.steps, 2);
  assert.equal(typeof dispose, "function");
  dispose();
});


function makeProvisionService() {
  return {
    async devices() {
      return { devices: [{ name: "PROV_A1B2", rssi: -30 }] };
    },
    async start(payload) {
      return { ok: true, state: "running", ssid: payload.ssid };
    },
    async arm() {
      return { ok: true };
    },
    async status() {
      return { state: "done", wifi: { state: "connected", ip: "192.168.1.20" } };
    },
  };
}

test("provision endpoints are routed to the provision service", async () => {
  const handler = createTaskRpcHandler(makeService(), makeProvisionService());
  const devices = await handler(PHONE_TASK_ENDPOINTS.provisionDevices, {});
  assert.equal(devices.ok, true);
  assert.equal(devices.value.devices[0].name, "PROV_A1B2");

  const started = await handler(PHONE_TASK_ENDPOINTS.provisionStart, { ssid: "Office" });
  assert.equal(started.ok, true);
  assert.equal(started.value.state, "running");

  const armed = await handler(PHONE_TASK_ENDPOINTS.provisionArm, {});
  assert.equal(armed.value.ok, true);

  const status = await handler(PHONE_TASK_ENDPOINTS.provisionStatus, {});
  assert.equal(status.value.wifi.ip, "192.168.1.20");
});

test("provision endpoints report unavailable without a provision service", async () => {
  const handler = createTaskRpcHandler(makeService());
  const result = await handler(PHONE_TASK_ENDPOINTS.provisionDevices, {});
  assert.equal(result.ok, false);
  assert.equal(result.error.code, "UNAVAILABLE");
});

function makeDeviceService() {
  return {
    async status() {
      return {
        device: { reachable: true, screen: [1264, 2800] },
        service: { ok: true },
      };
    },
    async thumbnail() {
      return { mime: "image/png", base64: "AAAA", width: 240, height: 532 };
    },
  };
}

test("preflight and device status endpoints are routed", async () => {
  const handler = createTaskRpcHandler(makeService(), null, makeDeviceService());
  const preflight = await handler(PHONE_TASK_ENDPOINTS.preflight, { device: "default" });
  assert.equal(preflight.ok, true);
  assert.equal(preflight.value.device, "default");

  const status = await handler(PHONE_TASK_ENDPOINTS.deviceStatus, { force: true });
  assert.equal(status.ok, true);
  assert.equal(status.value.device.reachable, true);
  assert.deepEqual(status.value.device.screen, [1264, 2800]);
});

test("device status reports unavailable without a device service", async () => {
  const handler = createTaskRpcHandler(makeService());
  const result = await handler(PHONE_TASK_ENDPOINTS.deviceStatus, {});
  assert.equal(result.ok, false);
  assert.equal(result.error.code, "UNAVAILABLE");
});

test("schedule update and thumbnail endpoints are routed", async () => {
  const handler = createTaskRpcHandler(makeService(), null, makeDeviceService());
  const updated = await handler(PHONE_TASK_ENDPOINTS.updateSchedule, {
    id: "s1",
    at: Date.now() + 90000,
    objective: "新任务",
  });
  assert.equal(updated.ok, true);
  assert.equal(updated.value.objective, "新任务");

  const thumb = await handler(PHONE_TASK_ENDPOINTS.deviceThumbnail, { maxWidth: 240 });
  assert.equal(thumb.ok, true);
  assert.equal(thumb.value.base64, "AAAA");
  assert.equal(thumb.value.width, 240);
});

test("info reports the host plugin version", async () => {
  const service = makeService();
  const handler = createTaskRpcHandler(service);
  const result = await handler(PHONE_TASK_ENDPOINTS.info, {});
  assert.equal(result.ok, true);
  assert.match(result.value.version, /^\d+\.\d+\.\d+/);
  assert.equal(result.value.channel, PHONE_TASK_CHANNEL);
});

test("an unknown endpoint says the host may be outdated", async () => {
  const handler = createTaskRpcHandler(makeService());
  const result = await handler("phone.task.someFutureEndpoint", {});
  assert.equal(result.ok, false);
  assert.equal(result.error.code, "NOT_FOUND");
  assert.match(result.error.message, /restart DSH Desktop/);
});

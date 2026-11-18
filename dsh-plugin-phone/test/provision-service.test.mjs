import assert from "node:assert/strict";
import test from "node:test";

import { createProvisionService } from "../lib/provision-service.js";

function fakeClient(overrides = {}) {
  const calls = [];
  const client = {
    provisionDevices: async (payload) => {
      calls.push(["devices", payload]);
      return { devices: [{ name: "PROV_A1B2", address: "AA:BB", rssi: -30 }] };
    },
    startProvision: async (payload) => {
      calls.push(["start", payload]);
      return { ok: true, state: "running" };
    },
    provisionStatus: async () => ({ state: "running", ssid: "Office" }),
    status: async () => ({ status: { wifi: { state: "connected", ip: "192.168.1.20" } } }),
    ...overrides,
  };
  return { calls, factory: () => client };
}

test("devices forwards timeout and returns the list", async () => {
  const fake = fakeClient();
  const service = createProvisionService({ clientFactory: fake.factory });
  const result = await service.devices({ timeout: 3 });
  assert.deepEqual(result.devices[0].name, "PROV_A1B2");
  assert.deepEqual(fake.calls[0], ["devices", { timeout: 3 }]);
});

test("start validates ssid and password", async () => {
  const fake = fakeClient();
  const service = createProvisionService({ clientFactory: fake.factory });
  await assert.rejects(() => service.start({ ssid: "", password: "x" }), (error) => error.code === "BAD_REQUEST");
  await assert.rejects(() => service.start({ ssid: "Office", password: "" }), (error) => error.code === "BAD_REQUEST");
  assert.equal(fake.calls.length, 0);
});

test("start trims ssid and forwards device/pop", async () => {
  const fake = fakeClient();
  const service = createProvisionService({ clientFactory: fake.factory });
  const result = await service.start({
    ssid: "  Office  ",
    password: "secret123",
    device: "PROV_A1B2",
    pop: "hand-A1B2",
  });
  assert.equal(result.ok, true);
  assert.deepEqual(fake.calls[0], [
    "start",
    { ssid: "Office", password: "secret123", device: "PROV_A1B2", pop: "hand-A1B2", timeout: 180 },
  ]);
});

test("status enriches the device wifi after success", async () => {
  const fake = fakeClient({ provisionStatus: async () => ({ state: "done", ssid: "Office" }) });
  const service = createProvisionService({ clientFactory: fake.factory });
  const result = await service.status();
  assert.equal(result.state, "done");
  assert.equal(result.wifi.ip, "192.168.1.20");
});

test("client errors keep their code", async () => {
  const fake = fakeClient({
    provisionDevices: async () => {
      const error = new Error("no bluetooth");
      error.code = "PROVISION_UNAVAILABLE";
      throw error;
    },
  });
  const service = createProvisionService({ clientFactory: fake.factory });
  await assert.rejects(() => service.devices({}), (error) => {
    assert.equal(error.code, "PROVISION_UNAVAILABLE");
    assert.match(error.message, /no bluetooth/);
    return true;
  });
});


test("arm asks the hand to start provisioning", async () => {
  const fake = fakeClient({ armProvisioning: async () => ({ ok: true, t: 1 }) });
  const service = createProvisionService({ clientFactory: fake.factory });
  const result = await service.arm();
  assert.equal(result.ok, true);
});

test("unreachable service becomes an actionable hint", async () => {
  const fake = fakeClient({
    provisionStatus: async () => {
      const error = new Error("fetch failed");
      error.code = "UNREACHABLE";
      throw error;
    },
  });
  const service = createProvisionService({ clientFactory: fake.factory });
  await assert.rejects(() => service.status(), (error) => {
    assert.equal(error.code, "SERVICE_DOWN");
    assert.match(error.message, /serve --scrcpy/);
    return true;
  });
});

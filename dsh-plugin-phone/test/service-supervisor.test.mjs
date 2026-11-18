import assert from "node:assert/strict";
import test from "node:test";

import { createServiceSupervisor } from "../lib/service-supervisor.js";

function makeClient(healthyAfter = 0) {
  let calls = 0;
  return {
    async health() {
      calls += 1;
      if (calls <= healthyAfter) throw new Error("down");
      return { ok: true };
    },
    get calls() {
      return calls;
    },
  };
}

test("does not spawn when the service is healthy", async () => {
  const spawns = [];
  const supervisor = createServiceSupervisor({
    client: makeClient(0),
    config: { autoStartService: true, serviceCommand: ["phone", "serve"] },
    spawnImpl: (...args) => {
      spawns.push(args);
      return { on() {}, unref() {} };
    },
    logger: {},
    autoStart: false,
  });
  assert.equal(await supervisor.ensure(), true);
  assert.equal(spawns.length, 0);
  supervisor.dispose();
});

test("spawns detached when the service is down and waits for health", async () => {
  const spawns = [];
  const client = makeClient(2);
  const supervisor = createServiceSupervisor({
    client,
    config: {
      autoStartService: true,
      serviceCommand: ["/bin/phone", "--tcp", "hand.local", "serve"],
      serviceCwd: "/tmp",
    },
    spawnImpl: (command, args, options) => {
      spawns.push({ command, args, options });
      return { on() {}, unref() {} };
    },
    sleep: async () => {},
    logger: {},
    autoStart: false,
  });
  assert.equal(await supervisor.ensure(), true);
  assert.equal(spawns.length, 1);
  assert.equal(spawns[0].command, "/bin/phone");
  assert.deepEqual(spawns[0].args, ["--tcp", "hand.local", "serve"]);
  assert.equal(spawns[0].options.detached, true);
  supervisor.dispose();
});

test("stays disabled without a command", async () => {
  const spawns = [];
  const supervisor = createServiceSupervisor({
    client: makeClient(5),
    config: { autoStartService: true },
    spawnImpl: (...args) => {
      spawns.push(args);
      return { on() {}, unref() {} };
    },
    logger: {},
    autoStart: false,
  });
  assert.equal(supervisor.enabled, false);
  assert.equal(await supervisor.ensure(), false);
  assert.equal(spawns.length, 0);
  supervisor.dispose();
});

test("reports a timeout when the spawned service never becomes healthy", async () => {
  const supervisor = createServiceSupervisor({
    client: makeClient(999),
    config: {
      autoStartService: true,
      serviceCommand: ["/bin/phone"],
      serviceStartupTimeoutMs: 1000,
    },
    spawnImpl: () => ({ on() {}, unref() {} }),
    sleep: async () => {},
    now: (() => {
      let t = 0;
      return () => (t += 600);
    })(),
    logger: {},
    autoStart: false,
  });
  assert.equal(await supervisor.ensure(), false);
  assert.match(supervisor.lastError, /did not become healthy/);
  supervisor.dispose();
});

function degradedClient(reason = "last frame is 600.0s old") {
  let alive = true;
  let degraded = true;
  let shutdowns = 0;
  return {
    async health() {
      if (!alive) throw new Error("down");
      return { ok: true, capture: { degraded, reason: degraded ? reason : null } };
    },
    async shutdown() {
      shutdowns += 1;
      alive = false;
    },
    revive() {
      alive = true;
      degraded = false;
    },
    degrade() {
      degraded = true;
    },
    get shutdowns() {
      return shutdowns;
    },
    get alive() {
      return alive;
    },
  };
}

test("restarts a service whose capture went stale", async () => {
  const client = degradedClient();
  const spawns = [];
  const supervisor = createServiceSupervisor({
    client,
    config: {
      autoStartService: true,
      serviceCommand: ["/bin/phone", "serve"],
      serviceShutdownTimeoutMs: 1000,
      serviceDegradedBackoffMs: 1000,
    },
    spawnImpl: () => {
      spawns.push(1);
      client.revive();
      return { on() {}, unref() {} };
    },
    sleep: async () => {},
    logger: {},
    autoStart: false,
  });
  assert.equal(await supervisor.ensure(), true);
  assert.equal(client.shutdowns, 1);
  assert.equal(spawns.length, 1);
  supervisor.dispose();
});

test("backs off before restarting a degraded service again", async () => {
  const client = degradedClient();
  let clock = 0;
  const spawns = [];
  const supervisor = createServiceSupervisor({
    client,
    config: {
      autoStartService: true,
      serviceCommand: ["/bin/phone"],
      serviceShutdownTimeoutMs: 1000,
      serviceDegradedBackoffMs: 60000,
      serviceDegradedBackoffMaxMs: 60000,
    },
    spawnImpl: () => {
      spawns.push(1);
      client.revive();
      // The restarted service is stale again immediately.
      client.degrade();
      return { on() {}, unref() {} };
    },
    sleep: async (ms) => {
      clock += ms;
    },
    now: () => clock,
    logger: {},
    autoStart: false,
  });
  clock = 1000;
  assert.equal(await supervisor.ensure(), true);
  assert.equal(spawns.length, 1);
  const before = clock;
  assert.equal(await supervisor.ensure(), false);
  assert.equal(spawns.length, 1);
  clock = before + 60000;
  assert.equal(await supervisor.ensure(), true);
  assert.equal(spawns.length, 2);
  supervisor.dispose();
});

test("does not restart a degraded service when restarts are disabled", async () => {
  const client = degradedClient();
  const supervisor = createServiceSupervisor({
    client,
    config: {
      autoStartService: true,
      serviceCommand: ["/bin/phone"],
      serviceRestartOnDegraded: false,
    },
    spawnImpl: () => {
      throw new Error("should not spawn");
    },
    logger: {},
    autoStart: false,
  });
  assert.equal(await supervisor.ensure(), false);
  assert.equal(client.shutdowns, 0);
  supervisor.dispose();
});

test("does not spawn a second service when the old one will not stop", async () => {
  const client = {
    async health() {
      return { ok: true, capture: { degraded: true, reason: "stale" } };
    },
    async shutdown() {
      // Ignored: the old process stays up and keeps the port.
    },
  };
  const spawns = [];
  const supervisor = createServiceSupervisor({
    client,
    config: {
      autoStartService: true,
      serviceCommand: ["/bin/phone"],
      serviceShutdownTimeoutMs: 1,
      serviceDegradedBackoffMs: 1000,
    },
    spawnImpl: () => {
      spawns.push(1);
      return { on() {}, unref() {} };
    },
    sleep: async () => {},
    logger: {},
    autoStart: false,
  });
  assert.equal(await supervisor.ensure(), false);
  assert.equal(spawns.length, 0);
  assert.match(supervisor.lastError, /did not stop/);
  supervisor.dispose();
});

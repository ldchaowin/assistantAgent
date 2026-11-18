import assert from "node:assert/strict";
import fs from "node:fs";
import test from "node:test";
import vm from "node:vm";
import { createRequire } from "node:module";

const source = fs.readFileSync(new URL("../client/client.js", import.meta.url), "utf8");
const requireProfile = createRequire(new URL("../package.json", import.meta.url));
const profileModules = process.env.HOME + "/.dsh/profiles/node_modules/";
const React = requireProfile(profileModules + "react");
const { renderToStaticMarkup } = requireProfile(profileModules + "react-dom/server");

function loadClient(reactModule, overrides = {}) {
  let definition;
  const storage = new Map();
  const window = {
    __ModuleLoader__: {
      load(value) {
        definition = value;
      },
    },
  };
  const document = {
    querySelector() {
      return null;
    },
    createElement() {
      return { dataset: {}, textContent: "" };
    },
    head: { appendChild() {} },
    ...(overrides.document || {}),
  };
  const localStorage = {
    getItem(key) {
      return storage.has(key) ? storage.get(key) : null;
    },
    setItem(key, value) {
      storage.set(key, String(value));
    },
    removeItem(key) {
      storage.delete(key);
    },
  };
  const sandbox = {
    window,
    document,
    localStorage,
    crypto: globalThis.crypto,
    setTimeout,
    clearTimeout,
    setInterval,
    clearInterval,
    console,
    ...(overrides.globals || {}),
  };
  vm.runInNewContext(source, sandbox, { filename: "client.js" });
  assert.ok(definition, "client module should register");
  const fakeReact = {
    createElement() {
      return {};
    },
    useState(value) {
      return [value, () => {}];
    },
    useSyncExternalStore(subscribe, getSnapshot) {
      return getSnapshot();
    },
  };
  return definition.factory((name) => {
    if (name === "react") return reactModule || fakeReact;
    throw new Error(`unexpected require: ${name}`);
  });
}

function flushMicrotasks() {
  return new Promise((resolve) => setImmediate(resolve));
}

// Minimal React dispatcher for exercising one non-visual hook in isolation.
// State updates schedule a re-render on the microtask queue, like React does.
function createHookHarness() {
  const hookStates = [];
  const hookRefs = [];
  const effectRuns = [];
  let effectDefs = [];
  let hook = null;
  let lastArgs = null;
  let current;
  let cursor = 0;
  let scheduled = false;

  function scheduleRender() {
    if (scheduled) return;
    scheduled = true;
    queueMicrotask(() => {
      scheduled = false;
      if (lastArgs) render(...lastArgs);
    });
  }

  const react = {
    useState(initial) {
      const slot = cursor++;
      if (!(slot in hookStates)) hookStates[slot] = initial;
      return [
        hookStates[slot],
        (next) => {
          hookStates[slot] = typeof next === "function" ? next(hookStates[slot]) : next;
          scheduleRender();
        },
      ];
    },
    useRef(initial) {
      const slot = cursor++;
      if (!(slot in hookRefs)) hookRefs[slot] = { current: initial };
      return hookRefs[slot];
    },
    useEffect(effect, deps) {
      effectDefs.push({ slot: cursor++, effect, deps });
    },
  };

  function sameDeps(a, b) {
    if (!a || !b || a.length !== b.length) return false;
    return a.every((value, index) => Object.is(value, b[index]));
  }

  function runEffects() {
    const defs = effectDefs;
    effectDefs = [];
    for (const def of defs) {
      const previous = effectRuns[def.slot];
      if (previous && sameDeps(previous.deps, def.deps)) continue;
      if (previous && typeof previous.cleanup === "function") previous.cleanup();
      const cleanup = def.effect();
      effectRuns[def.slot] = {
        deps: def.deps,
        cleanup: typeof cleanup === "function" ? cleanup : undefined,
      };
    }
  }

  function render(...args) {
    lastArgs = args;
    cursor = 0;
    effectDefs = [];
    current = hook(...args);
    runEffects();
    return current;
  }

  return {
    react,
    mount(hookFn, ...args) {
      hook = hookFn;
      return render(...args);
    },
    render,
    state() {
      return current;
    },
    unmount() {
      for (const run of effectRuns) {
        if (run && typeof run.cleanup === "function") run.cleanup();
      }
    },
  };
}

// Deterministic timers injected into the client sandbox so a 5s poll costs no
// real wall-clock time.
function createFakeClock() {
  let now = 0;
  let sequence = 0;
  const timers = new Map();
  function setTimeout(fn, ms = 0, ...args) {
    const id = ++sequence;
    timers.set(id, { at: now + Math.max(0, Number(ms) || 0), run: () => fn(...args) });
    return id;
  }
  function clearTimeout(id) {
    timers.delete(id);
  }
  async function advance(ms) {
    const target = now + ms;
    for (;;) {
      let nextId = null;
      let next = null;
      for (const [id, timer] of timers) {
        if (timer.at <= target && (!next || timer.at < next.at)) {
          nextId = id;
          next = timer;
        }
      }
      if (!next) break;
      timers.delete(nextId);
      now = next.at;
      next.run();
      // Let async continuations run before the next due timer fires.
      await flushMicrotasks();
      await flushMicrotasks();
    }
    now = target;
  }
  return {
    setTimeout,
    clearTimeout,
    advance,
    pending: () => timers.size,
    now: () => now,
  };
}

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

function makeCtx() {
  const registered = [];
  const promptCalls = [];
  let cancelCalls = 0;
  let opened = null;
  let currentSessionId = "current-session";
  const session = {
    prompt: async (parts, mode) => {
      promptCalls.push({ parts, mode });
      return { ok: true, value: { accepted: true } };
    },
    cancel: async () => {
      cancelCalls += 1;
      return { ok: true };
    },
    rename: async (title) => ({ ok: true, value: { title, seq: 1 } }),
    subscribe: () => () => {},
    getSnapshot: () => ({
      running: true,
      blank: false,
      turnEnds: new Map(),
      lastAgentError: null,
    }),
  };
  const ctx = {
    slots: {
      inject(_key, factory) {
        factory();
      },
      register(definition, component) {
        registered.push({ definition, component });
        return { dispose() {} };
      },
    },
    sessions: {
      create: async () => "task-session",
      open: (id) => {
        opened = id;
      },
      binding: (id) => ({ session }),
    },
    effect: (fn) => fn(),
  };
  return {
    ctx,
    registered,
    promptCalls,
    get cancelCalls() {
      return cancelCalls;
    },
    get opened() {
      return opened;
    },
    setCurrentSessionId(id) {
      currentSessionId = id;
    },
    getCurrentSessionId() {
      return currentSessionId;
    },
  };
}

test("client registers sidebar plugin entry and main page", () => {
  const client = loadClient();
  assert.equal(client.name, "dsh-plugin-phone");
  assert.deepEqual(Array.from(client.inject), [
    "slots",
    "sessions",
    "connection",
    "layout",
    "uiWorkspace",
  ]);
  const { ctx, registered } = makeCtx();
  client.apply(ctx);
  assert.equal(registered.length, 2);
  assert.equal(registered[0].definition.name, "sidebar.panellist");
  assert.equal(registered[0].definition.id, "phone-task");
  assert.equal(registered[1].definition.name, "main");
  assert.equal(registered[1].definition.key, "phone-task");
  assert.equal(typeof client.__test.taskCallbacks.startTask, "function");
  client.__test.clearTimers();
});

test("startTask creates an independent session and sends a safe task prompt", async () => {
  const client = loadClient();
  const harness = makeCtx();
  const { ctx, registered, promptCalls } = harness;
  client.apply(ctx);
  const props = client.__test.taskCallbacks;
  const sessionId = await props.startTask("打开设置，连接公司 Wi-Fi");
  assert.equal(sessionId, "task-session");
  assert.equal(harness.opened, null);
  assert.equal(promptCalls.length, 1);
  assert.equal(promptCalls[0].mode, "queue");
  const text = promptCalls[0].parts[0].text;
  assert.match(text, /\[PHONE TASK\]/);
  assert.match(text, /打开设置，连接公司 Wi-Fi/);
  assert.match(text, /phone_screenshot/);
  assert.match(text, /验证码/);
  assert.equal(client.__test.getState().active.status, "running");
  client.__test.clearTimers();
});

test("stopTask cancels the task session", async () => {
  const client = loadClient();
  const harness = makeCtx();
  const { ctx, registered } = harness;
  client.apply(ctx);
  const props = client.__test.taskCallbacks;
  await props.startTask("截图并描述页面");
  await props.stopTask();
  assert.equal(client.__test.getState().active.status, "stopped");
  assert.equal(harness.cancelCalls, 1);
  client.__test.clearTimers();
});

test("scheduleTask stores a future task", async () => {
  const client = loadClient();
  const { ctx, registered } = makeCtx();
  client.apply(ctx);
  const props = client.__test.taskCallbacks;
  const entry = await props.scheduleTask("打开设置", 5);
  assert.ok(entry.at > Date.now());
  assert.equal(client.__test.getState().scheduled.length, 1);
  client.__test.clearTimers();
});

test("task composer and running card render the new UI", () => {
  const client = loadClient(React);
  const composer = renderToStaticMarkup(React.createElement(client.__test.TaskComposer, {
    templates: [{ label: "京东价保", text: "打开京东，尝试一键价保" }],
    text: "打开京东",
    busy: false,
    error: "",
    disabledReason: "",
    disabled: false,
    onTextChange: () => {},
    onStart: () => {},
    onSchedule: async () => {},
    onClearError: () => {},
  }));
  assert.match(composer, /要它做什么/);
  assert.match(composer, /开始执行/);
  assert.match(composer, /定时/);
  assert.match(composer, /京东价保/);

  const running = renderToStaticMarkup(React.createElement(client.__test.RunningTask, {
    task: {
      id: "task-render",
      objective: "打开设置",
      status: "running",
      steps: 3,
      startedAt: Date.now() - 42000,
      sessionId: "session-render",
      lastAction: { name: "phone_find_element", arguments: '{"keyword":"价格保护"}' },
    },
    onOpen: () => {},
    onStop: async () => {},
  }));
  assert.match(running, /执行中/);
  assert.match(running, /正在查找「价格保护」/);
  assert.match(running, /停止/);
  assert.ok(!running.includes("[object Object]"), "action arguments must be humanized");
  client.__test.clearTimers();
});

test("startTask falls back to manager select/get when open/binding are absent", async () => {
  const client = loadClient();
  let opened = null;
  const promptCalls = [];
  const session = {
    prompt: async (parts) => {
      promptCalls.push(parts);
      return { ok: true };
    },
    rename: async () => ({ ok: true }),
    cancel: async () => ({ ok: true }),
    subscribe: () => () => {},
    getSnapshot: () => ({ running: true, blank: false, turnEnds: new Map() }),
  };
  const ctx = {
    slots: { inject() {}, register() { return {}; } },
    sessions: {
      create: async () => ({ ok: true, value: { sessionId: "task-manager" } }),
      select: (id) => {
        opened = id;
      },
      get: (id) => (id === "task-manager" ? { session } : undefined),
    },
    effect: () => {},
  };
  const id = await client.__test.startTask(ctx, null, "打开设置", { focus: true });
  assert.equal(id, "task-manager");
  assert.equal(opened, "task-manager");
  assert.equal(promptCalls.length, 1);
  client.__test.clearTimers();
});

test("prefers ctx.get('sessions') full service over the narrow property", async () => {
  const client = loadClient();
  let opened = null;
  const session = {
    prompt: async () => ({ ok: true }),
    rename: async () => ({ ok: true }),
    cancel: async () => ({ ok: true }),
    subscribe: () => () => {},
    getSnapshot: () => ({ running: true, blank: false, turnEnds: new Map() }),
  };
  const full = {
    create: async () => "task-full",
    select: (id) => {
      opened = id;
    },
    get: () => ({ session }),
  };
  const ctx = {
    slots: { inject() {}, register() { return {}; } },
    sessions: { create: async () => ({ ok: true, value: { sessionId: "narrow" } }) },
    get: (name) => (name === "sessions" ? full : undefined),
    effect: () => {},
  };
  const id = await client.__test.startTask(ctx, null, "打开设置", { focus: true });
  assert.equal(id, "task-full");
  assert.equal(opened, "task-full");
  client.__test.clearTimers();
});

test("startTask still runs when the client has no navigation API", async () => {
  const client = loadClient();
  let prompted = 0;
  const session = {
    prompt: async () => {
      prompted += 1;
      return { ok: true };
    },
    rename: async () => ({ ok: true }),
    cancel: async () => ({ ok: true }),
    subscribe: () => () => {},
    getSnapshot: () => ({ running: true, blank: false, turnEnds: new Map() }),
  };
  const ctx = {
    slots: { inject() {}, register() { return {}; } },
    sessions: {
      create: async () => "task-no-nav",
      binding: () => ({ session }),
      list: { getSnapshot: () => ({}) },
    },
    effect: () => {},
  };
  const id = await client.__test.startTask(ctx, null, "打开设置", { focus: true });
  assert.equal(id, "task-no-nav");
  assert.equal(prompted, 1);
  assert.match(client.__test.getState().active.warning, /后台运行/);
  client.__test.clearTimers();
});

test("startTask falls back to the sessions.list current field", async () => {
  const client = loadClient();
  let current = null;
  const session = {
    prompt: async () => ({ ok: true }),
    rename: async () => ({ ok: true }),
    cancel: async () => ({ ok: true }),
    subscribe: () => () => {},
    getSnapshot: () => ({ running: true, blank: false, turnEnds: new Map() }),
  };
  const ctx = {
    slots: { inject() {}, register() { return {}; } },
    sessions: {
      create: async () => "task-list",
      binding: () => ({ session }),
      list: {
        update(mutator) {
          const draft = { current: null };
          mutator(draft);
          current = draft.current;
        },
      },
    },
    effect: () => {},
  };
  const id = await client.__test.startTask(ctx, null, "打开设置", { focus: true });
  assert.equal(id, "task-list");
  assert.equal(current, "task-list");
  assert.equal(client.__test.getState().active.warning, null);
  client.__test.clearTimers();
});

test("sessionFaceOf falls back to sessions.manager.get", async () => {
  const client = loadClient();
  let prompted = 0;
  const session = {
    prompt: async () => {
      prompted += 1;
      return { ok: true };
    },
    rename: async () => ({ ok: true }),
    cancel: async () => ({ ok: true }),
    subscribe: () => () => {},
    getSnapshot: () => ({ running: true, blank: false, turnEnds: new Map() }),
  };
  const ctx = {
    slots: { inject() {}, register() { return {}; } },
    sessions: {
      create: async () => "task-manager-get",
      manager: { get: (id) => (id === "task-manager-get" ? session : undefined) },
      list: { update() {} },
    },
    effect: () => {},
  };
  const id = await client.__test.startTask(ctx, null, "打开设置");
  assert.equal(id, "task-manager-get");
  assert.equal(prompted, 1);
  client.__test.clearTimers();
});

test("startTask uses the host task RPC when available", async () => {
  const client = loadClient();
  const harness = makeCtx();
  const calls = [];
  harness.ctx.connection = {
    rpc: {
      call: async (channel, endpoint, payload) => {
        calls.push({ channel, endpoint, payload });
        if (endpoint === "phone.task.start") {
          return { ok: true, value: { id: "host-task", status: "starting", steps: 0 } };
        }
        if (endpoint === "phone.task.status") {
          return {
            ok: true,
            value: { id: "host-task", status: "done", steps: 3, lastAction: { name: "phone_click" } },
          };
        }
        return { ok: true, value: [] };
      },
    },
  };
  client.apply(harness.ctx);
  const props = client.__test.taskCallbacks;
  await props.startTask("打开设置");
  const startCall = calls.find((call) => call.endpoint === "phone.task.start");
  assert.ok(startCall);
  assert.equal(startCall.channel, "/dsh-phone-task-v3");
  assert.equal(startCall.payload.sessionId, "task-session");
  assert.equal(client.__test.getState().active.hostTaskId, "host-task");
  await new Promise((resolve) => setTimeout(resolve, 20));
  assert.equal(client.__test.getState().active.steps, 3);
  client.__test.clearTimers();
});

test("host service-down error stops the task before prompting", async () => {
  const client = loadClient();
  const harness = makeCtx();
  harness.ctx.connection = {
    rpc: {
      call: async (_channel, endpoint) => {
        if (endpoint === "phone.task.start") {
          return {
            ok: false,
            error: { code: "PHONE_SERVICE_DOWN", message: "start phone serve" },
          };
        }
        return { ok: true, value: [] };
      },
    },
  };
  client.apply(harness.ctx);
  const props = client.__test.taskCallbacks;
  await assert.rejects(() => props.startTask("打开设置"), /start phone serve/);
  assert.equal(harness.promptCalls.length, 0);
  assert.equal(client.__test.getState().active.status, "error");
  client.__test.clearTimers();
});

test("main task page renders the task UI", () => {
  const client = loadClient(React);
  const props = {
    startTask: async () => {},
    stopTask: async () => {},
    openTask: () => {},
    scheduleTask: async () => {},
    saveTemplate: async () => {},
    replayTask: async () => {},
    setDevice: () => {},
    closePanel: () => {},
  };
  const html = renderToStaticMarkup(React.createElement(client.__test.PhoneTaskPage, props));
  assert.match(html, /手机任务/);
  assert.match(html, /管理模板/);
  assert.ok(!html.includes("返回对话"), "the back button is intentionally removed");
  client.__test.clearTimers();
});

test("setup wizard renders the two provisioning steps", () => {
  const client = loadClient(React);
  const props = {
    mode: "first",
    currentSsid: "",
    deviceStatus: null,
    provisionDevices: async () => ({ devices: [] }),
    startProvision: async () => ({ ok: true }),
    provisionStatus: async () => ({ state: "idle" }),
    armProvisioning: async () => ({ ok: true }),
    refreshDevice: async () => null,
    onReady: () => {},
    onClose: null,
    onProvisionUpdate: () => {},
    onToast: () => {},
    initialError: "",
  };
  const html = renderToStaticMarkup(React.createElement(client.__test.SetupWizard, props));
  assert.match(html, /给设备配网/);
  assert.match(html, /选择设备/);
  assert.match(html, /连接 Wi-Fi/);
  assert.match(html, /开始配网/);
});

test("watchHostTask stays free of provisioning code", () => {
  const start = source.indexOf("function watchHostTask");
  const end = source.indexOf("function watchTask");
  assert.ok(start > 0 && end > start);
  const body = source.slice(start, end);
  assert.ok(!body.includes("armProvisioning"), "provisioning code leaked into watchHostTask");
  assert.ok(!body.includes("busy"), "watchHostTask must not reference ProvisionCard state");
});

test("preflight failure stops before creating a session", async () => {
  const client = loadClient();
  let createCount = 0;
  const calls = [];
  const ctx = {
    slots: { inject() {}, register() { return {}; } },
    sessions: {
      create: async () => {
        createCount += 1;
        return "task-never";
      },
      binding: () => ({ session: {} }),
    },
    connection: {
      rpc: {
        call: async (_channel, endpoint) => {
          calls.push(endpoint);
          if (endpoint === "phone.task.preflight") {
            return {
              ok: false,
              error: { code: "DEVICE_NOT_READY", message: "设备没有响应" },
            };
          }
          return { ok: true, value: [] };
        },
      },
    },
    effect: () => {},
  };
  client.apply(ctx);
  await assert.rejects(
    () => client.__test.taskCallbacks.startTask("打开设置"),
    (error) => error.code === "DEVICE_NOT_READY"
  );
  assert.equal(createCount, 0);
  assert.equal(client.__test.getState().active, null);
  assert.equal(client.__test.getState().history.length, 0);
  assert.ok(calls.includes("phone.task.preflight"));
  client.__test.clearTimers();
});

test("deviceStatus callback uses the phone.device.status endpoint", async () => {
  const client = loadClient();
  const calls = [];
  const ctx = {
    slots: { inject() {}, register() { return {}; } },
    sessions: { create: async () => "s" },
    connection: {
      rpc: {
        call: async (_channel, endpoint, payload) => {
          calls.push({ endpoint, payload });
          return { ok: true, value: { device: { reachable: true } } };
        },
      },
    },
    effect: () => {},
  };
  client.apply(ctx);
  const value = await client.__test.taskCallbacks.deviceStatus({
    force: true,
    device: "default",
  });
  assert.equal(value.device.reachable, true);
  const call = calls.find((item) => item.endpoint === "phone.device.status");
  assert.ok(call);
  assert.deepEqual(call.payload, { force: true, device: "default" });
  client.__test.clearTimers();
});

test("status bar and result card render semantic state", () => {
  const client = loadClient(React);
  const status = renderToStaticMarkup(
    React.createElement(client.__test.StatusBar, {
      info: { tone: "warn", title: "设备未连接", meta: ["本地服务运行中"] },
      phase: "offline",
      devices: [],
      device: "default",
      onRefresh: () => {},
      refreshing: false,
      onOpenSetup: null,
      running: false,
      provisioning: false,
    })
  );
  assert.match(status, /设备未连接/);
  assert.match(status, /重新检测/);

  const result = renderToStaticMarkup(
    React.createElement(client.__test.ResultCard, {
      task: {
        id: "task-result",
        objective: "京东价保",
        status: "error",
        startedAt: Date.now() - 60000,
        finishedAt: Date.now(),
        sessionId: "session-result",
        error: { code: "DEVICE_NOT_READY", message: "设备没有响应" },
      },
      busy: false,
      onOpen: () => {},
      onRetry: () => {},
      onSaveTemplate: () => {},
      onDismiss: () => {},
    })
  );
  assert.match(result, /没有完成/);
  assert.match(result, /设备没有响应/);
  assert.match(result, /重试/);
  assert.match(result, /查看会话/);
});

test("mergeHistory dedupes host and local records", () => {
  const client = loadClient(React);
  const items = client.__test.mergeHistory({
    hostHistory: [
      {
        id: "h1",
        objective: "京东价保",
        status: "error",
        error: { code: "X", message: "找不到按钮" },
        finishedAt: 2,
      },
    ],
    history: [
      { id: "h1", objective: "京东价保", status: "done", finishedAt: 2 },
      { id: "l1", objective: "截图", status: "done", finishedAt: 1 },
    ],
  });
  assert.equal(items.length, 2);
  assert.equal(items[0].source, "host");
  assert.equal(items[0].error.message, "找不到按钮");
  const html = renderToStaticMarkup(
    React.createElement(client.__test.RecentTasks, {
      items,
      onOpen: () => {},
      onReplay: () => {},
    })
  );
  assert.match(html, /最近任务/);
  assert.match(html, /找不到按钮/);
});

test("mergeHistory shows one row per task session", () => {
  const client = loadClient();
  const items = client.__test.mergeHistory({
    hostHistory: [
      { id: "h1", objective: "打开设置", status: "done", sessionId: "s1", finishedAt: 2 },
    ],
    history: [
      { id: "l1", objective: "打开设置", status: "done", sessionId: "s1", finishedAt: 2 },
      { id: "l2", objective: "截图", status: "done", sessionId: "s2", finishedAt: 1 },
    ],
  });
  assert.equal(items.length, 2);
  assert.equal(items[0].id, "h1");
  assert.equal(items[0].source, "host");
  assert.equal(items[1].id, "l2");
});

test("finished recent tasks expose a delete affordance", () => {
  const client = loadClient(React);
  const options = {
    onOpen: () => {},
    onReplay: () => {},
    onDelete: async () => {},
  };
  const done = renderToStaticMarkup(
    React.createElement(client.__test.RecentTasks, {
      items: [{ id: "h1", objective: "打开设置", status: "done", source: "host", finishedAt: Date.now() }],
      ...options,
    })
  );
  assert.match(done, /删除/);

  const running = renderToStaticMarkup(
    React.createElement(client.__test.RecentTasks, {
      items: [{ id: "l2", objective: "打开设置", status: "running", source: "local" }],
      ...options,
    })
  );
  assert.doesNotMatch(running, /删除/);
});

test("deleteHistory removes the host record and its local copy", async () => {
  const client = loadClient();
  const calls = [];
  const ctx = {
    connection: {
      rpc: {
        call: async (channel, endpoint, payload) => {
          calls.push({ channel, endpoint, payload });
          if (endpoint === "phone.task.history") {
            return {
              ok: true,
              value: [
                { id: "h1", objective: "打开设置", status: "done", sessionId: "s1", finishedAt: 2 },
              ],
            };
          }
          return { ok: true, value: { deleted: true } };
        },
      },
    },
  };
  client.__test.setState({
    hostHistory: [
      { id: "h1", objective: "打开设置", status: "done", sessionId: "s1", finishedAt: 2 },
    ],
    history: [
      { id: "l1", objective: "打开设置", status: "done", sessionId: "s1", finishedAt: 2 },
      { id: "l2", objective: "截图", status: "done", sessionId: "s2", finishedAt: 1 },
    ],
  });
  const target = client.__test.mergeHistory(client.__test.getState()).find((item) => item.id === "h1");
  assert.equal(await client.__test.deleteHistoryRecord(ctx, target), true);
  assert.equal(calls.length, 2);
  assert.equal(calls[0].endpoint, "phone.task.history");
  assert.equal(calls[1].channel, "/dsh-phone-task-v3");
  assert.equal(calls[1].endpoint, "phone.task.deleteHistory");
  assert.equal(calls[1].payload.id, "h1");
  const state = client.__test.getState();
  assert.equal(state.hostHistory.length, 0);
  assert.equal(state.history.length, 1);
  assert.equal(state.history[0].id, "l2");
});

test("deleteHistory removes local-only records without calling the host", async () => {
  const client = loadClient();
  const calls = [];
  const ctx = {
    connection: {
      rpc: {
        call: async (_channel, endpoint, payload) => {
          calls.push({ endpoint, payload });
          return { ok: true, value: [] };
        },
      },
    },
  };
  client.__test.setState({
    hostHistory: [],
    history: [{ id: "l1", objective: "截图", status: "done", finishedAt: 1 }],
  });
  const item = client.__test.mergeHistory(client.__test.getState())[0];
  await client.__test.deleteHistoryRecord(ctx, item);
  assert.equal(calls.length, 1);
  assert.equal(calls[0].endpoint, "phone.task.history");
  assert.equal(client.__test.getState().history.length, 0);
});

test("deleteHistory keeps a host record when the host cannot delete it", async () => {
  const client = loadClient();
  const ctx = {
    connection: {
      rpc: {
        call: async () => {
          throw new Error("offline");
        },
      },
    },
  };
  client.__test.setState({
    hostHistory: [{ id: "h1", objective: "打开设置", status: "done", finishedAt: 2 }],
    history: [],
  });
  const item = client.__test.mergeHistory(client.__test.getState())[0];
  await assert.rejects(
    () => client.__test.deleteHistoryRecord(ctx, item),
    (error) => error.code === "HOST_RPC_UNAVAILABLE"
  );
  assert.equal(client.__test.getState().hostHistory.length, 1);
});

test("clearHistory clears finished records and keeps the running task", async () => {
  const client = loadClient();
  const calls = [];
  const ctx = {
    connection: {
      rpc: {
        call: async (channel, endpoint) => {
          calls.push(endpoint);
          if (endpoint === "phone.task.history") {
            return {
              ok: true,
              value: [{ id: "h1", objective: "打开设置", status: "done", finishedAt: 2 }],
            };
          }
          return { ok: true, value: { deleted: 1 } };
        },
      },
    },
  };
  client.__test.setState({
    active: { id: "active-1", objective: "运行中", status: "running" },
    history: [
      { id: "active-1", objective: "运行中", status: "running" },
      { id: "l1", objective: "截图", status: "done", finishedAt: 1 },
    ],
    hostHistory: [{ id: "h1", objective: "打开设置", status: "done", finishedAt: 2 }],
  });
  await client.__test.clearHistoryRecords(ctx);
  assert.deepEqual(calls, ["phone.task.history", "phone.task.clearHistory"]);
  const state = client.__test.getState();
  assert.equal(state.hostHistory.length, 0);
  assert.equal(state.history.length, 1);
  assert.equal(state.history[0].id, "active-1");
});

test("deleteHistory removes the host row behind a local finished task", async () => {
  const client = loadClient();
  const calls = [];
  const ctx = {
    connection: {
      rpc: {
        call: async (channel, endpoint, payload) => {
          calls.push({ endpoint, payload });
          if (endpoint === "phone.task.history") {
            return {
              ok: true,
              value: [
                { id: "h1", objective: "打开设置", status: "done", sessionId: "s1", finishedAt: 2 },
              ],
            };
          }
          return { ok: true, value: { deleted: true } };
        },
      },
    },
  };
  client.__test.setState({
    hostHistory: [],
    history: [
      {
        id: "l1",
        objective: "打开设置",
        status: "done",
        sessionId: "s1",
        hostTaskId: "h1",
        finishedAt: 2,
      },
    ],
  });
  const item = client.__test.mergeHistory(client.__test.getState())[0];
  assert.equal(item.source, "local");
  assert.equal(await client.__test.deleteHistoryRecord(ctx, item), true);
  assert.deepEqual(calls.map((call) => call.endpoint), [
    "phone.task.history",
    "phone.task.deleteHistory",
  ]);
  assert.equal(calls[1].payload.id, "h1");
  const state = client.__test.getState();
  assert.equal(state.hostHistory.length, 0);
  assert.equal(state.history.length, 0);
});

test("deleteHistory refuses a running task but allows a finished one", async () => {
  const client = loadClient();
  const ctx = {
    connection: {
      rpc: {
        call: async () => ({ ok: true, value: [] }),
      },
    },
  };
  const record = { id: "t1", objective: "打开设置", sessionId: "s1", finishedAt: 2 };
  client.__test.setState({
    active: { ...record, status: "running" },
    history: [{ ...record, status: "done" }],
  });
  const item = client.__test.mergeHistory(client.__test.getState())[0];
  await assert.rejects(
    () => client.__test.deleteHistoryRecord(ctx, item),
    /任务还在执行/
  );

  client.__test.setState({ active: { ...record, status: "done" } });
  assert.equal(await client.__test.deleteHistoryRecord(ctx, item), true);
  assert.equal(client.__test.getState().history.length, 0);
});

test("clearHistory keeps records when host history cannot be read", async () => {
  const client = loadClient();
  const ctx = {
    connection: {
      rpc: {
        call: async () => {
          throw new Error("offline");
        },
      },
    },
  };
  client.__test.setState({
    history: [{ id: "l1", objective: "截图", status: "done", finishedAt: 1 }],
    hostHistory: [{ id: "h1", objective: "打开设置", status: "done", finishedAt: 2 }],
  });
  await assert.rejects(
    () => client.__test.clearHistoryRecords(ctx),
    (error) => error.code === "HOST_RPC_UNAVAILABLE"
  );
  assert.equal(client.__test.getState().history.length, 1);
  assert.equal(client.__test.getState().hostHistory.length, 1);
});

test("action formatter and device summary are human readable", () => {
  const client = loadClient();
  assert.equal(
    client.__test.formatAction({ name: "phone_click", arguments: '{"x":10,"y":20}' }),
    "正在点击 (10, 20)"
  );
  assert.equal(
    client.__test.formatAction({ name: "phone_find_element", arguments: { keyword: "价格保护" } }),
    "正在查找「价格保护」"
  );
  assert.equal(client.__test.formatAction({ name: "phone_screenshot" }), "正在查看屏幕");
  assert.equal(
    client.__test.formatAction({ name: "phone_unknown", arguments: { a: 1 } }),
    "正在执行 phone_unknown"
  );

  const ready = client.__test.deviceSummary(
    "ready",
    {
      device: {
        reachable: true,
        usb: true,
        wifi: { state: "connected", ssid: "YOUR_WIFI", rssi: -52 },
        screen: [1264, 2800],
      },
    },
    null,
    null
  );
  assert.equal(ready.title, "设备已就绪");
  assert.match(ready.meta.join(" "), /Wi-Fi YOUR_WIFI/);
  assert.match(ready.meta.join(" "), /1264×2800/);

  const offline = client.__test.deviceSummary("offline", null, null, null);
  assert.equal(offline.title, "设备未连接");

  const iosEyeOnly = client.__test.deviceSummary(
    "ready",
    {
      platform: "ios",
      device: {
        reachable: true,
        platform: "ios",
        hand: { ok: false, error: "DISCONNECT" },
        screen: [1179, 2556],
      },
    },
    null,
    null
  );
  assert.equal(iosEyeOnly.title, "iPhone 屏幕在线 · 控制手未连接");
  assert.match(iosEyeOnly.meta.join(" "), /1179×2556/);

  const iosReady = client.__test.deviceSummary(
    "ready",
    {
      platform: "ios",
      device: {
        reachable: true,
        platform: "ios",
        hand: { ok: true },
        usb: true,
        state: "READY",
        screen: [1179, 2556],
      },
    },
    null,
    null
  );
  assert.equal(iosReady.title, "iPhone 已就绪");
});

test("clock time scheduling resolves today or tomorrow", () => {
  const client = loadClient();
  const morning = new Date(2026, 10, 12, 9, 0, 0).getTime();
  const evening = new Date(2026, 10, 12, 21, 0, 0).getTime();
  const todayAt10 = client.__test.atFromClock("10:30", morning);
  const tomorrowAt10 = client.__test.atFromClock("10:30", evening);
  assert.equal(new Date(todayAt10).getDate(), 12);
  assert.equal(new Date(tomorrowAt10).getDate(), 13);
  assert.equal(client.__test.timeInputValue(todayAt10), "10:30");
  assert.equal(client.__test.atFromClock("25:00", morning), null);
});

test("running card shows the phone thumbnail and preview affordance", () => {
  const client = loadClient(React);
  const html = renderToStaticMarkup(
    React.createElement(client.__test.RunningTask, {
      task: {
        id: "task-thumb",
        objective: "打开设置",
        status: "running",
        steps: 1,
        startedAt: Date.now(),
        sessionId: "session-thumb",
      },
      thumbnail: { src: "data:image/png;base64,AAAA" },
      onPreview: () => {},
      onOpen: () => {},
      onStop: async () => {},
    })
  );
  assert.match(html, /data:image\/png;base64,AAAA/);
  assert.match(html, /查看手机画面/);
});

test("template manager, history panel and schedule editor render", () => {
  const client = loadClient(React);
  const templates = [
    { id: "t1", label: "京东价保", text: "打开京东，尝试一键价保" },
    { id: "t2", label: "游戏日常", text: "打开游戏，完成每日任务" },
  ];
  const manager = renderToStaticMarkup(
    React.createElement(client.__test.TemplateManager, {
      templates,
      currentText: "打开设置",
      onClose: () => {},
      onSave: async () => {},
      onDelete: async () => {},
    })
  );
  assert.match(manager, /新增模板/);
  assert.match(manager, /现有模板（2）/);
  assert.match(manager, /京东价保/);
  assert.match(manager, /删除/);

  const emptyManager = renderToStaticMarkup(
    React.createElement(client.__test.TemplateManager, {
      templates: [],
      currentText: "",
      onClose: () => {},
      onSave: async () => {},
      onDelete: async () => {},
    })
  );
  assert.match(emptyManager, /保存模板/);

  const history = renderToStaticMarkup(
    React.createElement(client.__test.HistoryPanel, {
      items: client.__test.mergeHistory({
        hostHistory: [
          { id: "h1", objective: "京东价保", status: "done", finishedAt: Date.now(), sessionId: "s1" },
          { id: "h2", objective: "游戏日常", status: "error", error: { message: "中断" }, finishedAt: Date.now() },
        ],
      }),
      onClose: () => {},
      onOpen: () => {},
      onReplay: () => {},
      onDelete: async () => {},
      onClear: async () => {},
    })
  );
  assert.match(history, /任务记录/);
  assert.match(history, /游戏日常/);
  assert.match(history, /中断/);
  assert.match(history, /清空记录/);
  assert.match(history, /删除/);

  const editor = renderToStaticMarkup(
    React.createElement(client.__test.ScheduleEditor, {
      item: { id: "s1", objective: "打开设置", at: Date.now() + 60000 },
      onClose: () => {},
      onSave: async () => {},
    })
  );
  assert.match(editor, /编辑排期/);
  assert.match(editor, /执行时间/);
  assert.match(editor, /保存排期/);
});

test("P1 callbacks call thumbnail, updateSchedule and deleteTemplate", async () => {
  const client = loadClient();
  const calls = [];
  const ctx = {
    slots: { inject() {}, register() { return {}; } },
    sessions: { create: async () => "s" },
    connection: {
      rpc: {
        call: async (_channel, endpoint, payload) => {
          calls.push({ endpoint, payload });
          if (endpoint === "phone.device.thumbnail") {
            return { ok: true, value: { mime: "image/png", base64: "AAAA", at: 1 } };
          }
          if (endpoint === "phone.task.updateSchedule") {
            return { ok: true, value: { id: "s1", objective: "新任务", at: 2000 } };
          }
          if (endpoint === "phone.task.deleteTemplate") {
            return { ok: true, value: { deleted: true } };
          }
          return { ok: true, value: [] };
        },
      },
    },
    effect: () => {},
  };
  client.apply(ctx);
  client.__test.setState({
    scheduled: [{ id: "s1", objective: "旧任务", at: 1000 }],
  });
  const props = client.__test.taskCallbacks;

  const thumb = await props.thumbnail({ maxWidth: 240 });
  assert.equal(thumb.base64, "AAAA");
  assert.ok(calls.some((call) => call.endpoint === "phone.device.thumbnail" && call.payload.maxWidth === 240));

  const updated = await props.updateSchedule("s1", { objective: "新任务", at: 2000 });
  assert.equal(updated.objective, "新任务");
  assert.equal(client.__test.getState().scheduled[0].at, 2000);

  const deleted = await props.deleteTemplate("t1");
  assert.deepEqual(deleted, { deleted: true });
  assert.ok(calls.some((call) => call.endpoint === "phone.task.deleteTemplate"));
  client.__test.clearTimers();
});

test("openTask navigates through uiWorkspace.openSession", () => {
  const client = loadClient();
  let opened = null;
  const ctx = {
    slots: { inject() {}, register() { return {}; } },
    sessions: {},
    uiWorkspace: {
      openSession(sessionId) {
        opened = sessionId;
      },
    },
    get(name) {
      if (name === "uiWorkspace") return this.uiWorkspace;
      if (name === "sessions") return this.sessions;
      return undefined;
    },
    effect: () => {},
  };
  client.apply(ctx);
  const result = client.__test.taskCallbacks.openTask("session-from-workspace");
  assert.equal(result, true);
  assert.equal(opened, "session-from-workspace");
  client.__test.clearTimers();
});

test("openTask reports false when no navigation API exists", () => {
  const client = loadClient();
  const ctx = {
    slots: { inject() {}, register() { return {}; } },
    sessions: {},
    get() {
      return undefined;
    },
    effect: () => {},
  };
  client.apply(ctx);
  const result = client.__test.taskCallbacks.openTask("session-nowhere");
  assert.equal(result, false);
  client.__test.clearTimers();
});

test("openTask reports false when uiWorkspace navigation throws", () => {
  const client = loadClient();
  const ctx = {
    slots: { inject() {}, register() { return {}; } },
    sessions: {},
    uiWorkspace: {
      openSession() {
        throw new Error("session is archived");
      },
    },
    get(name) {
      if (name === "uiWorkspace") return this.uiWorkspace;
      if (name === "sessions") return this.sessions;
      return undefined;
    },
    effect: () => {},
  };
  client.apply(ctx);
  assert.equal(client.__test.taskCallbacks.openTask("session-archived"), false);
  client.__test.clearTimers();
});

test("host templates keep their ids so the manager can delete them", async () => {
  const client = loadClient();
  const harness = makeCtx();
  harness.ctx.connection = {
    rpc: {
      call: async (_channel, endpoint) => {
        if (endpoint === "phone.task.templates") {
          return {
            ok: true,
            value: [
              { id: "t1", name: "模板一", objective: "打开设置" },
              { id: "t2", name: "模板二", objective: "打开京东" },
            ],
          };
        }
        return { ok: true, value: [] };
      },
    },
  };
  client.apply(harness.ctx);
  await new Promise((resolve) => setTimeout(resolve, 30));
  assert.deepEqual(
    client.__test.templates().map((template) => template.id),
    ["t1", "t2"],
    "syncHost must keep the host id; dropping it made delete send 'undefined'",
  );
  assert.equal(client.__test.templates()[0].label, "模板一");
  client.__test.clearTimers();
});

test("deleteTemplate sends the real id and refreshes the chip list", async () => {
  const client = loadClient();
  const harness = makeCtx();
  const hostTemplates = [
    { id: "t1", name: "模板一", objective: "打开设置" },
    { id: "t2", name: "模板二", objective: "打开京东" },
  ];
  harness.ctx.connection = {
    rpc: {
      call: async (_channel, endpoint, payload) => {
        if (endpoint === "phone.task.templates") return { ok: true, value: hostTemplates };
        if (endpoint === "phone.task.deleteTemplate") {
          const index = hostTemplates.findIndex((item) => item.id === payload.id);
          if (index < 0) return { ok: true, value: { deleted: false, id: payload.id } };
          hostTemplates.splice(index, 1);
          return { ok: true, value: { deleted: true, id: payload.id } };
        }
        return { ok: true, value: [] };
      },
    },
  };
  client.apply(harness.ctx);
  await new Promise((resolve) => setTimeout(resolve, 30));
  const result = await client.__test.taskCallbacks.deleteTemplate("t1");
  assert.deepEqual(result, { deleted: true, id: "t1" });
  assert.deepEqual(client.__test.templates().map((template) => template.id), ["t2"]);
  client.__test.clearTimers();
});

test("deleteTemplate refuses a missing id instead of deleting nothing quietly", async () => {
  const client = loadClient();
  const harness = makeCtx();
  const calls = [];
  harness.ctx.connection = {
    rpc: {
      call: async (_channel, endpoint, payload) => {
        calls.push({ endpoint, payload });
        return { ok: true, value: [] };
      },
    },
  };
  client.apply(harness.ctx);
  const result = await client.__test.taskCallbacks.deleteTemplate(undefined);
  assert.equal(result.deleted, false);
  assert.equal(result.reason, "missing_id");
  assert.equal(calls.some((call) => call.endpoint === "phone.task.deleteTemplate"), false);
  client.__test.clearTimers();
});

test("task composer shows every template chip, not just four", () => {
  const client = loadClient(React);
  const templates = [1, 2, 3, 4, 5].map((index) => ({
    id: `t${index}`,
    label: `模板${index}`,
    text: `任务${index}`,
  }));
  const html = renderToStaticMarkup(
    React.createElement(client.__test.TaskComposer, {
      templates,
      text: "",
      busy: false,
      error: "",
      disabledReason: "",
      disabled: false,
      onTextChange: () => {},
      onStart: () => {},
      onSchedule: async () => {},
      onClearError: () => {},
    }),
  );
  assert.match(html, /模板5/, "the fifth template must be visible above the input");
});

test("template manager offers rename and md editing", () => {
  const client = loadClient(React);
  const html = renderToStaticMarkup(
    React.createElement(client.__test.TemplateManager, {
      templates: [
        { id: "t1", label: "带文件的模板", text: "打开设置", file: "/tmp/templates/t1.md" },
        { id: "t2", label: "纯 JSON 模板", text: "打开京东" },
      ],
      currentText: "",
      onClose: () => {},
      onSave: async () => {},
      onDelete: async () => {},
      onRename: async () => {},
      onReadFile: async () => ({ id: "t1", path: "/tmp/templates/t1.md", content: "---\n" }),
      onWriteFile: async () => {}
    }),
  );
  assert.match(html, /重命名/);
  assert.match(html, /编辑模板/);
  assert.match(html, /t1\.md/, "the md file name is shown so the row looks editable");
  assert.equal((html.match(/编辑模板/g) || []).length, 1, "only templates with a file get the editor");
});

test("template editor renders markdown and saves through the callback", () => {
  const client = loadClient(React);
  let saved = null;
  const html = renderToStaticMarkup(
    React.createElement(client.__test.TemplateEditor, {
      file: {
        id: "t1",
        label: "京东价保",
        path: "/tmp/templates/jd.md",
        content: "---\nid: jd\n---\n\n## 步骤\n\n```yaml\nsteps: []\n```\n"
      },
      onClose: () => {},
      onSave: async (content) => {
        saved = content;
      }
    }),
  );
  assert.match(html, /编辑模板：京东价保/);
  assert.match(html, /steps: \[\]/);
  assert.match(html, /保存 md/);
  assert.equal(saved, null);
});

test("rename / md read / md save callbacks hit the host RPC", async () => {
  const client = loadClient();
  const harness = makeCtx();
  const calls = [];
  harness.ctx.connection = {
    rpc: {
      call: async (_channel, endpoint, payload) => {
        calls.push({ endpoint, payload });
        if (endpoint === "phone.task.renameTemplate") {
          return { ok: true, value: { id: payload.id, name: payload.name, file: "/tmp/a.md" } };
        }
        if (endpoint === "phone.task.templateFile") {
          return { ok: true, value: { id: payload.id, path: "/tmp/a.md", content: "---\n" } };
        }
        if (endpoint === "phone.task.saveTemplateFile") {
          return { ok: true, value: { id: payload.id, bytes: payload.content.length } };
        }
        return { ok: true, value: [] };
      }
    }
  };
  client.apply(harness.ctx);
  const props = client.__test.taskCallbacks;

  const renamed = await props.renameTemplate("t1", "新名字");
  assert.equal(renamed.name, "新名字");
  const renameCall = calls.find((call) => call.endpoint === "phone.task.renameTemplate");
  // payload values cross the vm realm boundary, compare fields instead of prototypes
  assert.equal(renameCall.payload.id, "t1");
  assert.equal(renameCall.payload.name, "新名字");

  const file = await props.readTemplateFile("t1");
  assert.equal(file.path, "/tmp/a.md");

  const saved = await props.saveTemplateFile("t1", "---\nid: t1\n---\n");
  assert.equal(saved.id, "t1");
  const saveCall = calls.find((call) => call.endpoint === "phone.task.saveTemplateFile");
  assert.equal(saveCall.payload.id, "t1");
  assert.match(saveCall.payload.content, /id: t1/);
  client.__test.clearTimers();
});

test("offline notice keeps setup and retry reachable", () => {
  const client = loadClient(React);
  const html = renderToStaticMarkup(
    React.createElement(client.__test.OfflineNotice, {
      refreshing: false,
      onSetup: () => {},
      onRetry: () => {}
    }),
  );
  assert.match(html, /设备未连接/);
  assert.match(html, /去配网/);
  assert.match(html, /重新检测/);
  assert.match(html, /模板照常可以查看/);
});

test("rename errors reach the UI instead of a generic failure", async () => {
  const client = loadClient();
  const harness = makeCtx();
  harness.ctx.connection = {
    rpc: {
      call: async (_channel, endpoint) => {
        if (endpoint === "phone.task.templates") return { ok: true, value: [] };
        return {
          ok: false,
          error: { code: "NOT_FOUND", message: "unknown phone task endpoint: phone.task.renameTemplate" }
        };
      }
    }
  };
  client.apply(harness.ctx);
  await assert.rejects(
    () => client.__test.taskCallbacks.renameTemplate("t1", "新名字"),
    (error) => /unknown phone task endpoint/.test(error.message)
  );
  client.__test.clearTimers();
});

test("staleHostHint explains a host that did not restart", () => {
  const client = loadClient();
  const stale = client.__test.staleHostHint("");
  assert.match(stale, /旧版本/);
  assert.match(stale, /完全退出 DSH Desktop/);
  const mismatch = client.__test.staleHostHint("0.7.1");
  assert.match(mismatch, /0\.7\.1/);
  assert.match(mismatch, new RegExp(client.__test.CLIENT_VERSION));
  assert.equal(client.__test.staleHostHint(client.__test.CLIENT_VERSION), "");
});

test("package, host and client versions agree", async () => {
  const fs = await import("node:fs/promises");
  const pkg = JSON.parse(
    await fs.readFile(new URL("../package.json", import.meta.url), "utf8")
  );
  const host = await fs.readFile(new URL("../lib/version.js", import.meta.url), "utf8");
  const client = loadClient();
  assert.match(host, new RegExp(`"${pkg.version.replace(/\./g, "\\.")}"`));
  assert.equal(client.__test.CLIENT_VERSION, pkg.version);
});

test("review panel lists proposals with apply controls", () => {
  const client = loadClient(React);
  const html = renderToStaticMarkup(
    React.createElement(client.__test.TemplateReviewPanel, {
      template: { id: "t1", label: "蔚蓝星球日常" },
      review: {
        name: "蔚蓝星球日常",
        samples: { runs: 3, ok_runs: 3, enough: true },
        proposals: [
          {
            uid: "s1:promote_coords",
            step_id: "s1",
            kind: "promote_coords",
            reason: "连续命中同一位置，可升级为坐标点击"
          },
          {
            uid: "s3:timeout_raise",
            step_id: "s3",
            kind: "timeout_raise",
            reason: "p95 接近 3s 预算"
          }
        ]
      },
      onClose: () => {},
      onApply: async () => ({}),
      onRollback: async () => ({}),
      onApplied: async () => {}
    })
  );
  assert.match(html, /复盘建议：蔚蓝星球日常/);
  assert.match(html, /s1 · 升级为坐标点击/);
  assert.match(html, /s3 · 调大 timeout/);
  assert.match(html, /应用选中（2）/);
  assert.match(html, /预览 diff/);
  assert.match(html, /回滚上一版/);
  assert.match(html, /样本 3 次/);
});

test("review panel shows an empty state", () => {
  const client = loadClient(React);
  const html = renderToStaticMarkup(
    React.createElement(client.__test.TemplateReviewPanel, {
      template: { id: "t1", label: "空模板" },
      review: { samples: { runs: 0, ok_runs: 0, enough: false }, proposals: [] },
      onClose: () => {},
      onApply: async () => ({}),
      onRollback: async () => ({}),
      onApplied: async () => {}
    })
  );
  assert.match(html, /没有可应用的机械改动/);
  assert.match(html, /样本不足/);
});

test("review callbacks hit the host RPC with ids and uids", async () => {
  const client = loadClient();
  const harness = makeCtx();
  const calls = [];
  harness.ctx.connection = {
    rpc: {
      call: async (_channel, endpoint, payload) => {
        calls.push({ endpoint, payload });
        if (endpoint === "phone.task.templateReview") {
          return { ok: true, value: { id: payload.id, proposals: [] } };
        }
        if (endpoint === "phone.task.applyTemplateReview") {
          return { ok: true, value: { id: payload.id, applied: [], new_version: 3 } };
        }
        if (endpoint === "phone.task.rollbackTemplate") {
          return { ok: true, value: { id: payload.id, to_version: 2 } };
        }
        return { ok: true, value: [] };
      }
    }
  };
  client.apply(harness.ctx);
  const props = client.__test.taskCallbacks;

  const review = await props.reviewTemplate("t1");
  const reviewCall = calls.find((call) => call.endpoint === "phone.task.templateReview");
  assert.equal(reviewCall.payload.id, "t1");
  assert.equal(review.proposals.length, 0);

  await props.applyTemplateReview("t1", ["s1:promote_coords"], true);
  const applyCall = calls.find((call) => call.endpoint === "phone.task.applyTemplateReview");
  assert.equal(applyCall.payload.id, "t1");
  assert.equal(applyCall.payload.dryRun, true);
  assert.equal(applyCall.payload.uids[0], "s1:promote_coords");

  const rolled = await props.rollbackTemplate("t1");
  assert.ok(calls.some((call) => call.endpoint === "phone.task.rollbackTemplate"));
  assert.equal(rolled.to_version, 2);
  client.__test.clearTimers();
});

test("template manager shows an execute button for templates with a file", () => {
  const client = loadClient(React);
  const html = renderToStaticMarkup(
    React.createElement(client.__test.TemplateManager, {
      templates: [
        { id: "t1", label: "带文件", text: "打开设置", file: "/tmp/t1.md" },
        { id: "t2", label: "纯 JSON", text: "打开京东" }
      ],
      currentText: "",
      onClose: () => {},
      onSave: async () => {},
      onDelete: async () => {},
      onRename: async () => {},
      onReadFile: async () => ({ id: "t1", path: "/tmp/t1.md", content: "" }),
      onWriteFile: async () => {},
      onRun: async () => {},
      onReview: async () => ({ proposals: [] }),
      onApplyReview: async () => ({}),
      onRollbackReview: async () => ({})
    })
  );
  assert.equal((html.match(/执行模板/g) || []).length, 1);
});

test("runTemplate starts a host template run and attaches the watcher", async () => {
  const client = loadClient();
  const harness = makeCtx();
  const calls = [];
  harness.ctx.connection = {
    rpc: {
      call: async (_channel, endpoint, payload) => {
        calls.push({ endpoint, payload });
        if (endpoint === "phone.task.runTemplate") {
          return {
            ok: true,
            value: {
              id: "host-template-1",
              status: "running",
              steps: 0,
              total: 12,
              objective: "执行模板：淘宝保价"
            }
          };
        }
        return { ok: true, value: [] };
      }
    }
  };
  client.apply(harness.ctx);
  const hostTaskId = await client.__test.taskCallbacks.runTemplate("t1");
  assert.equal(hostTaskId, "host-template-1");
  const call = calls.find((item) => item.endpoint === "phone.task.runTemplate");
  assert.equal(call.payload.id, "t1");
  const active = client.__test.getState().active;
  assert.equal(active.hostTaskId, "host-template-1");
  assert.equal(active.mode, "template");
  assert.equal(active.total, 12);
  assert.match(active.objective, /执行模板/);
  client.__test.clearTimers();
});

test("result card renders template runs without a save-template action", () => {
  const client = loadClient(React);
  const html = renderToStaticMarkup(
    React.createElement(client.__test.ResultCard, {
      task: {
        id: "task-1",
        mode: "template",
        templateId: "t1",
        objective: "执行模板：淘宝保价",
        status: "done",
        startedAt: 1000,
        finishedAt: 4000,
        sessionId: null,
        hostTaskId: "host-1",
        proposals: 3,
        templateUpdate: { version: 2, newVersion: 3, applied: [] }
      },
      busy: false,
      onOpen: () => {},
      onRetry: () => {},
      onRetryTemplate: () => {},
      onSaveTemplate: () => {},
      onDismiss: () => {}
    })
  );
  assert.match(html, /模板执行完成/);
  assert.match(html, /再执行一次/);
  assert.match(html, /v2 → v3/);
  assert.match(html, /复盘：3 条建议/);
  assert.ok(!html.includes("存为模板"), "template runs must not offer save-as-template");
});

test("history rows from the host offer save-as-template", () => {
  const client = loadClient(React);
  const item = {
    id: "host-task-1",
    source: "host",
    objective: "打开淘宝，进入价保中心",
    status: "done",
    startedAt: 1000,
    finishedAt: 5000,
    sessionId: "session-1",
    tracePath: "/tmp/trace.jsonl"
  };
  const html = renderToStaticMarkup(
    React.createElement(client.__test.HistoryPanel, {
      items: [item],
      onClose: () => {},
      onOpen: () => {},
      onReplay: () => {},
      onDelete: () => {},
      onClear: () => {},
      onSaveTemplate: () => {}
    })
  );
  assert.match(html, /存为模板/);
});

test("failed history rows do not offer save-as-template", () => {
  const client = loadClient(React);
  const html = renderToStaticMarkup(
    React.createElement(client.__test.HistoryPanel, {
      items: [
        {
          id: "host-task-2",
          source: "host",
          objective: "失败的任务",
          status: "error",
          startedAt: 1000,
          finishedAt: 5000,
          tracePath: "/tmp/trace.jsonl"
        }
      ],
      onClose: () => {},
      onOpen: () => {},
      onReplay: () => {},
      onDelete: () => {},
      onClear: () => {},
      onSaveTemplate: () => {}
    })
  );
  assert.ok(!html.includes("存为模板"));
});

test("history rows use one replay label and keep save-as-template", () => {
  const client = loadClient(React);
  const html = renderToStaticMarkup(
    React.createElement(client.__test.HistoryPanel, {
      items: [
        {
          id: "host-task-9",
          source: "host",
          objective: "打开京东并价保",
          status: "done",
          startedAt: 1000,
          finishedAt: 9000,
          sessionId: "s-1"
        }
      ],
      onClose: () => {},
      onOpen: () => {},
      onReplay: () => {},
      onDelete: () => {},
      onClear: () => {},
      onSaveTemplate: () => {}
    })
  );
  assert.match(html, /再执行一次/);
  assert.match(html, /存为模板/);
  assert.ok(!html.includes("重放"), "重放 implied template replay but re-runs the agent");
});

test("local history rows that carry a host task id can become templates", () => {
  const client = loadClient(React);
  const html = renderToStaticMarkup(
    React.createElement(client.__test.HistoryPanel, {
      items: [
        {
          id: "local-1",
          hostTaskId: "host-task-10",
          objective: "本地发起的任务",
          status: "done",
          startedAt: 1000,
          finishedAt: 9000
        }
      ],
      onClose: () => {},
      onOpen: () => {},
      onReplay: () => {},
      onDelete: () => {},
      onClear: () => {},
      onSaveTemplate: () => {}
    })
  );
  assert.match(html, /存为模板/);
});

test("status bar shows the eye state", () => {
  const client = loadClient(React);
  const html = renderToStaticMarkup(
    React.createElement(client.__test.StatusBar, {
      info: { tone: "ok", title: "设备就绪", meta: [] },
      phase: "ready",
      devices: [],
      device: "default",
      refreshing: false,
      running: false,
      provisioning: false,
      eye: { state: "down", reason: "no ADB device" }
    })
  );
  assert.match(html, /画面 断开/);
  assert.match(html, /no ADB device/);
});

test("eye-down notice explains the cause and offers reconnect", () => {
  const client = loadClient(React);
  const html = renderToStaticMarkup(
    React.createElement(client.__test.EyeDownNotice, {
      reason: "scrcpy: no ADB device",
      reconnecting: false,
      refreshing: false,
      onReconnect: () => {},
      onRetry: () => {}
    })
  );
  assert.match(html, /读不到手机画面/);
  assert.match(html, /重连 ADB/);
  assert.match(html, /scrcpy: no ADB device/);
  assert.match(html, /重新配对无线调试/);
});

test("reconnectAdb callback hits the host RPC", async () => {
  const client = loadClient();
  const harness = makeCtx();
  const calls = [];
  harness.ctx.connection = {
    rpc: {
      call: async (_channel, endpoint, payload) => {
        calls.push({ endpoint, payload });
        if (endpoint === "phone.device.reconnectAdb") {
          return { ok: true, value: { ok: true, endpoint: "192.168.1.9:5555" } };
        }
        return { ok: true, value: [] };
      }
    }
  };
  client.apply(harness.ctx);
  const result = await client.__test.taskCallbacks.reconnectAdb({});
  assert.equal(result.ok, true);
  assert.equal(result.endpoint, "192.168.1.9:5555");
  assert.ok(calls.some((call) => call.endpoint === "phone.device.reconnectAdb"));
  client.__test.clearTimers();
});

test("thumbnail polling uses a 5s interval and never stacks pending captures", async () => {
  const clock = createFakeClock();
  const hookHarness = createHookHarness();
  const client = loadClient(hookHarness.react, {
    globals: { setTimeout: clock.setTimeout, clearTimeout: clock.clearTimeout },
  });
  assert.equal(client.__test.THUMBNAIL_POLL_MS, 5000);

  const first = deferred();
  const calls = [];
  const makeThumbnail = (name) => () => {
    calls.push(name);
    if (name === "first") return first.promise;
    return Promise.resolve({ mime: "image/png", base64: "BBBB", at: 2 });
  };
  hookHarness.mount(client.__test.useThumbnail, { thumbnail: makeThumbnail("first") }, true, "default");
  await flushMicrotasks();
  await flushMicrotasks();
  assert.equal(calls.length, 1, "mount starts the first capture immediately");
  assert.equal(hookHarness.state().loading, true);

  // Four poll periods elapse while the first capture is still pending and no
  // second request is issued.
  await clock.advance(20000);
  assert.equal(calls.length, 1, "a pending capture must not be stacked");

  // A re-render that swaps the host callback re-runs the effect; the pending
  // capture still must not be duplicated.
  hookHarness.render({ thumbnail: makeThumbnail("second") }, true, "default");
  await flushMicrotasks();
  assert.equal(calls.length, 1, "a re-run effect must wait for the pending capture");
  await clock.advance(5000);
  assert.equal(calls.length, 1, "still waiting on the pending capture");

  first.resolve({ mime: "image/png", base64: "AAAA", at: 1 });
  await flushMicrotasks();
  await flushMicrotasks();
  // The follow-up poll runs a full 5s after the capture settled.
  await clock.advance(5000);
  await flushMicrotasks();
  assert.deepEqual(calls, ["first", "second"]);
  assert.equal(hookHarness.state().src, "data:image/png;base64,BBBB");
  assert.equal(hookHarness.state().error, null);
  hookHarness.unmount();
});

test("a failed thumbnail poll keeps the last good frame", async () => {
  const clock = createFakeClock();
  const hookHarness = createHookHarness();
  const client = loadClient(hookHarness.react, {
    globals: { setTimeout: clock.setTimeout, clearTimeout: clock.clearTimeout },
  });

  let calls = 0;
  const thumbnail = async () => {
    calls += 1;
    if (calls === 1) return { mime: "image/png", base64: "AAAA", at: 1 };
    if (calls === 2) {
      const error = new Error("host rpc timed out");
      error.code = "HOST_TIMEOUT";
      throw error;
    }
    const busy = new Error("capture lock is busy");
    busy.code = "BUSY";
    throw busy;
  };
  hookHarness.mount(client.__test.useThumbnail, { thumbnail }, true, "default");
  await flushMicrotasks();
  await flushMicrotasks();
  assert.equal(hookHarness.state().src, "data:image/png;base64,AAAA");

  await clock.advance(5000);
  await flushMicrotasks();
  assert.equal(calls, 2);
  assert.equal(hookHarness.state().src, "data:image/png;base64,AAAA", "old frame must survive a failed poll");
  assert.equal(hookHarness.state().error && hookHarness.state().error.message, "host rpc timed out");
  assert.equal(hookHarness.state().loading, false);

  // BUSY stays an expected, silent condition: no error surfaced, frame kept.
  await clock.advance(5000);
  await flushMicrotasks();
  assert.equal(calls, 3);
  assert.equal(hookHarness.state().src, "data:image/png;base64,AAAA");
  assert.equal(hookHarness.state().error, null);
  hookHarness.unmount();
});

test("saveTemplate surfaces the host error instead of a generic failure", async () => {
  const client = loadClient();
  const harness = makeCtx();
  harness.ctx.connection = {
    rpc: {
      call: async (_channel, endpoint) => {
        if (endpoint === "phone.task.templates") return { ok: true, value: [] };
        return {
          ok: false,
          error: { code: "SUMMARY_FAILED", message: "没有生成模板：模型总结失败（模型没有返回总结内容）" }
        };
      }
    }
  };
  client.apply(harness.ctx);
  await assert.rejects(
    () => client.__test.taskCallbacks.saveTemplate({ name: "x", objective: "x", taskId: "t1" }),
    (error) => /模型总结失败/.test(error.message)
  );
  client.__test.clearTimers();
});

test("template result shows automatic navigation when it was needed", () => {
  const client = loadClient(React);
  const html = renderToStaticMarkup(
    React.createElement(client.__test.ResultCard, {
      task: {
        id: "t1",
        mode: "template",
        templateId: "app",
        objective: "执行模板：淘宝保价",
        status: "done",
        startedAt: 1000,
        finishedAt: 5000,
        guided: true,
        proposals: 0
      },
      busy: false,
      onOpen: () => {},
      onRetry: () => {},
      onRetryTemplate: () => {},
      onSaveTemplate: () => {},
      onDismiss: () => {}
    })
  );
  assert.match(html, /已自动导航到起始页/);
});

test("template result mentions the learned approach", () => {
  const client = loadClient(React);
  const html = renderToStaticMarkup(
    React.createElement(client.__test.ResultCard, {
      task: {
        id: "t1",
        mode: "template",
        templateId: "app",
        objective: "执行模板：淘宝保价",
        status: "done",
        startedAt: 1000,
        finishedAt: 5000,
        guided: true,
        learned: { steps: 3, version: 1, newVersion: 2 },
        proposals: 0
      },
      busy: false,
      onOpen: () => {},
      onRetry: () => {},
      onRetryTemplate: () => {},
      onSaveTemplate: () => {},
      onDismiss: () => {}
    })
  );
  assert.match(html, /已写回到达路径（3 步，v1 → v2）/);
});

test("template result notes when the start page was already open", () => {
  const client = loadClient(React);
  const html = renderToStaticMarkup(
    React.createElement(client.__test.ResultCard, {
      task: {
        id: "t1",
        mode: "template",
        templateId: "app",
        objective: "执行模板：淘宝保价",
        status: "done",
        startedAt: 1000,
        finishedAt: 5000,
        envAtEntry: true,
        proposals: 0
      },
      busy: false,
      onOpen: () => {},
      onRetry: () => {},
      onRetryTemplate: () => {},
      onSaveTemplate: () => {},
      onDismiss: () => {}
    })
  );
  assert.match(html, /已在起始页，跳过到达段/);
});

test("running template shows the environment-confirmation phase", () => {
  const client = loadClient(React);
  const html = renderToStaticMarkup(
    React.createElement(client.__test.RunningTask, {
      task: {
        id: "t1",
        mode: "template",
        templateId: "app",
        objective: "执行模板：淘宝保价",
        status: "running",
        phase: "env",
        steps: 0,
        total: 0,
        startedAt: Date.now()
      },
      busy: false,
      onStop: () => {}
    })
  );
  assert.match(html, /确认当前环境/);
});

test("result card says the whole task was model-run", () => {
  const client = loadClient(React);
  const html = renderToStaticMarkup(
    React.createElement(client.__test.ResultCard, {
      task: {
        id: "t1",
        mode: "template",
        templateId: "app",
        objective: "执行模板：淘宝保价",
        status: "done",
        startedAt: 1000,
        finishedAt: 5000,
        guided: true,
        guidedFull: true,
        proposals: 0
      },
      busy: false,
      onOpen: () => {},
      onRetry: () => {},
      onRetryTemplate: () => {},
      onSaveTemplate: () => {},
      onDismiss: () => {}
    })
  );
  assert.match(html, /已全程自动执行/);
});

test("running card shows the full auto-run phase", () => {
  const client = loadClient(React);
  const html = renderToStaticMarkup(
    React.createElement(client.__test.RunningTask, {
      task: {
        id: "t1",
        mode: "template",
        templateId: "app",
        objective: "执行模板：淘宝保价",
        status: "running",
        phase: "guided",
        guidedFull: true,
        steps: 2,
        total: 20,
        startedAt: Date.now()
      },
      busy: false,
      onStop: () => {}
    })
  );
  assert.match(html, /自动执行中/);
});

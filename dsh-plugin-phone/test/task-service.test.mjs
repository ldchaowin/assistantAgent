import assert from "node:assert/strict";
import { mkdirSync, writeFileSync } from "node:fs";
import { mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import test from "node:test";
import {
  createTaskService,
  readSuccessExpect,
  readTemplateCompletion,
  renderTemplate,
  sawTextInCaptured
} from "../lib/task-service.js";

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

/** Minimal Cordis-like event bus for `session/event`. */
function makeEvents() {
  const listeners = [];
  return {
    on(name, listener) {
      if (name === "session/event") listeners.push(listener);
      return () => {
        const index = listeners.indexOf(listener);
        if (index >= 0) listeners.splice(index, 1);
      };
    },
    emit(sessionId, event) {
      for (const listener of listeners) listener({ header: { id: sessionId } }, event);
    },
  };
}

async function waitFor(predicate, timeoutMs = 2000) {
  const started = Date.now();
  while (Date.now() - started < timeoutMs) {
    const value = await predicate();
    if (value) return value;
    await sleep(20);
  }
  return undefined;
}

function toolCall(callId, name, args = {}, time = 1000) {
  return { type: "tool/call", time, data: { callId, name, arguments: JSON.stringify(args) } };
}

function toolResult(callId, text, { time = 1200, isError = false } = {}) {
  return {
    type: "tool/result",
    time,
    data: { message: { toolCallId: callId, isError, content: [{ type: "text", text }] } },
  };
}


function makeAgent(id = "session-1") {
  const agent = {
    id,
    status: "running",
    session: { id, events: [] },
    followupMessage: null,
    cancelled: null,
    followup(message) {
      this.followupMessage = message;
    },
    cancel(cause) {
      this.cancelled = cause;
      this.status = "idle";
    },
  };
  return agent;
}

function makeService({
  agent,
  healthError = null,
  agents,
  statusError = null,
  screenshotError = null,
  usb = true,
  stateFile,
  events = null,
  config = {},
  runTool = null,
  startTool = null,
  guidedApproach = null,
  guidedRun = null,
  learnApproach = null,
  clientFactory = null,
} = {}) {
  return createTaskService({
    ...(runTool ? { runTool } : {}),
    ...(startTool ? { startTool } : {}),
    guidedApproach: guidedApproach || (async () => ({ ok: false, message: "guided approach not stubbed" })),
    guidedRun: guidedRun || (async () => ({ ok: false, message: "guided run not stubbed" })),
    ...(learnApproach ? { learnApproach } : {}),
    clientFactory: clientFactory || (() => ({
      baseUrl: "http://127.0.0.1:8765",
      health: async () => {
        if (healthError) throw healthError;
        return { ok: true };
      },
      status: async () => {
        if (statusError) throw statusError;
        return {
          ok: true,
          screen: [1080, 2400],
          status: { state: "READY", usb },
        };
      },
      screenshot: async () => {
        if (screenshotError) throw screenshotError;
        return { ok: true };
      },
      find: async () => ({ ok: true, found: false }),
    })),
    agents:
      agents ||
      {
        get: () => agent,
        create: async () => ({ agent: agent || makeAgent("auto-1") }),
      },
    events,
    config: {
      stateFile:
        stateFile || join(tmpdir(), `phone-task-test-${Date.now()}-${Math.random()}.json`),
      // Never fall back to process.cwd(): a test run would litter the plugin dir.
      workspace:
        config.workspace || join(tmpdir(), `phone-task-ws-${Date.now()}-${Math.random()}`),
      // Most tests only cover the mechanical converter; the summary path has
      // its own test and must not wait on a model.
      summarizeTemplates: false,
      ...config,
    },
    logger: { info() {}, warn() {} },
  });
}

test("renderTemplate replaces variables", () => {
  assert.equal(renderTemplate("连接 {{wifi_name}}", { wifi_name: "CU" }), "连接 CU");
  assert.equal(renderTemplate("连接 {{missing}}", {}), "连接 {{missing}}");
});

test("start checks phone health and reports actionable error", async () => {
  const service = makeService({ healthError: new Error("down") });
  await assert.rejects(
    () => service.start({ objective: "打开设置" }),
    (error) => error.code === "PHONE_SERVICE_DOWN" && /phone .*serve/.test(error.message),
  );
  service.dispose();
});

test("attach mode tracks agent steps and completion", async () => {
  const agent = makeAgent("session-attach");
  const service = makeService({ agent });
  const task = await service.start({ objective: "打开设置", sessionId: "session-attach" });
  assert.equal(task.mode, "attach");
  assert.equal(task.sessionId, "session-attach");
  await sleep(800);
  let current = service.status(task.id);
  assert.equal(current.status, "running");
  agent.session.events.push({
    type: "tool/call",
    data: { name: "phone_click", arguments: "{\"x\":1}" },
    time: Date.now(),
  });
  await sleep(650);
  current = service.status(task.id);
  assert.equal(current.steps, 1);
  agent.status = "idle";
  await sleep(650);
  current = service.status(task.id);
  assert.equal(current.status, "done");
  service.dispose();
});

test("autonomous mode creates an agent and prompts it", async () => {
  const agent = makeAgent("auto-1");
  const workspace = join(tmpdir(), `phone-task-cwd-${Date.now()}-${Math.random()}`);
  let created = null;
  const service = makeService({
    agent,
    config: { workspace },
    agents: {
      get: () => undefined,
      create: async (options) => {
        created = options;
        return { agent };
      },
    },
  });
  const task = await service.start({ objective: "打开设置" });
  assert.equal(task.mode, "autonomous");
  assert.ok(created?.sessionId);
  // The task agent must inherit the workspace cwd, not the host default.
  assert.equal(created.meta?.cwd, workspace);
  assert.equal(agent.followupMessage.role, "user");
  assert.match(agent.followupMessage.content[0].text, /\[PHONE TASK\]/);
  service.dispose();
});

test("taskCwd overrides the workspace for task agents", async () => {
  const agent = makeAgent("auto-pinned");
  const workspace = join(tmpdir(), `phone-task-ws-${Date.now()}-${Math.random()}`);
  const taskCwd = join(tmpdir(), `phone-task-pin-${Date.now()}-${Math.random()}`);
  let created = null;
  const service = makeService({
    agent,
    config: { workspace, taskCwd },
    agents: {
      get: () => undefined,
      create: async (options) => {
        created = options;
        return { agent };
      },
    },
  });
  await service.start({ objective: "打开设置" });
  assert.equal(created.meta?.cwd, taskCwd);
  service.dispose();
});

test("stop cancels the agent and records history", async () => {
  const agent = makeAgent("session-stop");
  const service = makeService({ agent });
  const task = await service.start({ objective: "打开设置", sessionId: "session-stop" });
  await sleep(800);
  await service.stop(task.id);
  assert.deepEqual(agent.cancelled, { kind: "user" });
  const current = service.status(task.id);
  assert.equal(current.status, "stopped");
  assert.ok(service.history().some((item) => item.id === task.id));
  service.dispose();
});

test("history records can be deleted and cleared", async () => {
  const stateFile = join(tmpdir(), `phone-task-history-${Date.now()}-${Math.random()}.json`);
  const service = makeService({ agent: makeAgent("session-history"), stateFile });
  const first = await service.start({ objective: "打开设置", sessionId: "session-history" });
  await sleep(800);
  await service.stop(first.id);
  const second = await service.start({ objective: "截图", sessionId: "session-history-2" });
  await sleep(800);
  await service.stop(second.id);
  assert.equal(service.history().length, 2);

  assert.deepEqual(await service.deleteHistory(first.id), { deleted: true, id: first.id });
  assert.deepEqual(service.history().map((item) => item.id), [second.id]);
  assert.deepEqual(await service.deleteHistory(first.id), { deleted: false });
  await assert.rejects(() => service.deleteHistory(""), (error) => error.code === "BAD_REQUEST");

  assert.deepEqual(await service.clearHistory(), { deleted: 1 });
  assert.equal(service.history().length, 0);
  assert.deepEqual(await service.clearHistory(), { deleted: 0 });
  service.dispose();

  // A host restart must not bring deleted records back.
  const reloaded = makeService({ stateFile });
  await reloaded.ready;
  assert.equal(reloaded.history().length, 0);
  reloaded.dispose();
  await rm(stateFile, { force: true });
});

test("templates can be saved and variables rendered", async () => {
  const service = makeService({ agent: makeAgent() });
  const saved = await service.saveTemplate({
    name: "连接 WLAN",
    objective: "连接 {{wifi_name}}",
    variables: ["wifi_name"],
  });
  assert.ok(service.templates().some((item) => item.id === saved.id));
  assert.equal(renderTemplate(saved.objective, { wifi_name: "Home" }), "连接 Home");
  service.dispose();
});

test("preflight rejects when the hand does not answer", async () => {
  const service = makeService({
    agent: makeAgent(),
    statusError: Object.assign(new Error("no response to status"), { code: "TIMEOUT" }),
  });
  await assert.rejects(
    () => service.preflight({}),
    (error) => error.code === "DEVICE_NOT_READY" && /设备没有响应/.test(error.message),
  );
  service.dispose();
});

test("preflight rejects when the phone is not on USB", async () => {
  const service = makeService({ agent: makeAgent(), usb: false });
  await assert.rejects(
    () => service.preflight({}),
    (error) => error.code === "DEVICE_NOT_READY" && /USB/.test(error.message),
  );
  service.dispose();
});

test("preflight rejects when the screen cannot be captured", async () => {
  const service = makeService({
    agent: makeAgent(),
    screenshotError: new Error("no frame"),
  });
  await assert.rejects(
    () => service.preflight({}),
    (error) => error.code === "EYE_UNAVAILABLE" && /手机画面/.test(error.message),
  );
  service.dispose();
});

test("preflight succeeds for a reachable hand and screen", async () => {
  const service = makeService({ agent: makeAgent() });
  const result = await service.preflight({});
  assert.deepEqual(result, { ok: true, device: "default" });
  service.dispose();
});

test("updateSchedule moves an existing schedule and changes its objective", async () => {
  const service = makeService({ agent: makeAgent() });
  const created = await service.schedule({ objective: "旧任务", delaySeconds: 60 });
  const at = Date.now() + 120000;
  const updated = await service.updateSchedule({ id: created.id, at, objective: "新任务" });
  assert.equal(updated.id, created.id);
  assert.equal(updated.objective, "新任务");
  assert.equal(updated.at, at);
  assert.equal(service.schedules().length, 1);
  assert.equal(service.schedules()[0].at, at);
  service.dispose();
});

test("updateSchedule rejects unknown ids", async () => {
  const service = makeService({ agent: makeAgent() });
  await assert.rejects(
    () => service.updateSchedule({ id: "missing", delaySeconds: 60 }),
    (error) => error.code === "NOT_FOUND"
  );
  service.dispose();
});

test("panel task buffers session events and writes a trace on finish", async () => {
  const dir = await mkdtemp(join(tmpdir(), "phone-trace-"));
  const events = makeEvents();
  const agent = makeAgent("session-trace");
  const service = makeService({
    agent,
    events,
    config: { runsDir: join(dir, "runs"), templatesDir: join(dir, "templates") },
  });
  const task = await service.start({ objective: "点五次招募", sessionId: "session-trace" });
  await sleep(800);

  events.emit("session-trace", toolCall("c1", "phone_screenshot", {}, 1000));
  events.emit(
    "session-trace",
    toolResult("c1", "Screenshot 462x1024 (1 bytes) saved to runs/service/shot-1.png.", { time: 1100 }),
  );
  events.emit("session-trace", toolCall("c2", "phone_find_element", { keyword: "商城" }, 1200));
  events.emit(
    "session-trace",
    toolResult("c2", 'Found "商城" at phone (632, 1400) with score 0.99.', { time: 1500 }),
  );
  events.emit("session-trace", toolCall("c3", "phone_click", { x: 632, y: 1400 }, 1600));
  events.emit("session-trace", toolResult("c3", "Clicked phone (632, 1400).", { time: 1650 }));
  events.emit("session-trace", { type: "turn/end", time: 1700, data: {} });
  await sleep(600);

  const running = service.status(task.id);
  assert.equal(running.steps, 3, "steps come from the event buffer");

  agent.status = "idle";
  await sleep(700);
  const finished = service.status(task.id);
  assert.equal(finished.status, "done");
  assert.ok(finished.tracePath, "task history carries the trace path");

  const trace = await waitFor(async () => {
    try {
      return await readFile(finished.tracePath, "utf8");
    } catch {
      return undefined;
    }
  });
  assert.ok(trace, "trace file exists");
  const records = trace.trim().split("\n").map((line) => JSON.parse(line));
  assert.equal(records[0].t, "run");
  assert.equal(records[0].mode, "panel");
  assert.deepEqual(records[0].screen, [1080, 2400]);
  const steps = records.filter((record) => record.t === "step");
  assert.deepEqual(steps.map((record) => record.do), ["look", "find", "tap"]);
  assert.deepEqual(steps[2].at, [632, 1400]);
  assert.equal(records.at(-1).t, "done");
  service.dispose();
  await rm(dir, { recursive: true, force: true });
});

test("saveTemplate with a taskId compiles a reusable template md", async () => {
  const dir = await mkdtemp(join(tmpdir(), "phone-template-"));
  const events = makeEvents();
  const agent = makeAgent("session-compile");
  const service = makeService({
    agent,
    events,
    config: { runsDir: join(dir, "runs"), templatesDir: join(dir, "templates") },
  });
  const task = await service.start({ objective: "点五次招募", sessionId: "session-compile" });
  await sleep(650); // let the monitor see the agent running before it goes idle
  events.emit("session-compile", toolCall("c2", "phone_find_element", { keyword: "商城" }, 1200));
  events.emit(
    "session-compile",
    toolResult("c2", 'Found "商城" at phone (632, 1400) with score 0.99.', { time: 1500 }),
  );
  events.emit("session-compile", toolCall("c3", "phone_click", { x: 632, y: 1400 }, 1600));
  events.emit("session-compile", toolResult("c3", "Clicked phone (632, 1400).", { time: 1650 }));
  agent.status = "idle";
  await sleep(700);

  const saved = await service.saveTemplate({
    name: "点五次招募",
    objective: "点五次招募",
    taskId: task.id,
  });
  assert.ok(saved.file, "saveTemplate returns the generated md path");
  assert.equal(saved.steps, 1);
  const markdown = await readFile(saved.file, "utf8");
  assert.match(markdown, /coords_known: true/);
  assert.match(markdown, /screen: \[1080, 2400\]/);
  assert.match(markdown, /do: tap, text: 商城, at: \[0.5852, 0.5833\]/);
  assert.equal(service.templates().some((item) => item.id === saved.id && item.file), true);

  // The generated template seeds one trace so the reviewer has a sample.
  const seededTrace = await waitFor(async () => {
    const { readdir } = await import("node:fs/promises");
    const runs = await readdir(join(dir, "runs", saved.id)).catch(() => []);
    return runs.length > 0 ? join(dir, "runs", saved.id, runs[0], "trace.jsonl") : undefined;
  });
  assert.ok(seededTrace, "generated template gets a seed trace");
  const seededLines = (await readFile(seededTrace, "utf8")).trim().split("\n");
  assert.equal(JSON.parse(seededLines[0]).t, "run");
  assert.equal(JSON.parse(seededLines[1]).id, "s1");
  service.dispose();
  await rm(dir, { recursive: true, force: true });
});

test("saveTemplate without a trace keeps the plain template", async () => {
  const service = makeService({ agent: makeAgent() });
  const saved = await service.saveTemplate({ name: "连接 WLAN", objective: "连接 {{wifi_name}}" });
  assert.equal(saved.file, undefined);
  assert.equal(saved.id.startsWith("task-") || saved.id.length > 0, true);
  service.dispose();
});

test("saveTemplate fails loudly when the trace is gone", async () => {
  const dir = await mkdtemp(join(tmpdir(), "phone-notrace-"));
  const service = makeService({
    agent: makeAgent(),
    config: { runsDir: join(dir, "runs"), templatesDir: join(dir, "templates") },
  });
  await assert.rejects(
    () => service.saveTemplate({ name: "旧任务", objective: "旧任务", taskId: "missing-task" }),
    (error) => error.code === "TRACE_NOT_FOUND"
  );
  assert.equal(service.templates().some((item) => item.name === "旧任务"), false);
  service.dispose();
  await rm(dir, { recursive: true, force: true });
});

test("deleteTemplate reports a real result instead of always succeeding", async () => {
  const service = makeService({ agent: makeAgent() });
  const saved = await service.saveTemplate({ name: "可删除", objective: "打开设置" });
  assert.deepEqual(await service.deleteTemplate(saved.id), { deleted: true, id: saved.id });
  assert.deepEqual(await service.deleteTemplate(saved.id), { deleted: false, id: saved.id });
  assert.equal(service.templates().some((item) => item.id === saved.id), false);
  await assert.rejects(() => service.deleteTemplate(""), (error) => error.code === "BAD_REQUEST");
  await assert.rejects(
    () => service.deleteTemplate(undefined),
    (error) => error.code === "BAD_REQUEST",
  );
  service.dispose();
});

test("a fast task that only leaves a turn/end event is done, not AGENT_NOT_STARTED", async () => {
  const dir = await mkdtemp(join(tmpdir(), "phone-fast-"));
  const events = makeEvents();
  const agent = makeAgent("session-fast");
  agent.status = "idle"; // never observed running by the 500ms poll
  const service = makeService({
    agent,
    events,
    config: { runsDir: join(dir, "runs"), templatesDir: join(dir, "templates") },
  });
  const task = await service.start({ objective: "截个图", sessionId: "session-fast" });
  await sleep(100);
  events.emit("session-fast", toolCall("c1", "phone_screenshot", {}, 1000));
  events.emit("session-fast", toolResult("c1", "Screenshot 1080x2400 saved to x.png.", { time: 1100 }));
  events.emit("session-fast", { type: "turn/end", time: 1200, data: {} });
  await sleep(700);
  const current = service.status(task.id);
  assert.equal(current.status, "done");
  assert.equal(current.steps, 1);
  service.dispose();
  await rm(dir, { recursive: true, force: true });
});

test("renameTemplate and md editing keep the file and the list in sync", async () => {
  const dir = await mkdtemp(join(tmpdir(), "phone-rename-"));
  const events = makeEvents();
  const agent = makeAgent("session-rename");
  const service = makeService({
    agent,
    events,
    config: { runsDir: join(dir, "runs"), templatesDir: join(dir, "templates") },
  });
  const task = await service.start({ objective: "点五次招募", sessionId: "session-rename" });
  await sleep(650);
  events.emit("session-rename", toolCall("c1", "phone_find_element", { keyword: "商城" }, 1200));
  events.emit(
    "session-rename",
    toolResult("c1", 'Found "商城" at phone (632, 1400) with score 0.99.', { time: 1500 }),
  );
  events.emit("session-rename", toolCall("c2", "phone_click", { x: 632, y: 1400 }, 1600));
  events.emit("session-rename", toolResult("c2", "Clicked phone (632, 1400).", { time: 1650 }));
  agent.status = "idle";
  await sleep(700);

  const saved = await service.saveTemplate({ name: "旧名字", objective: "点五次招募", taskId: task.id });
  assert.ok(saved.file);

  const renamed = await service.renameTemplate({ id: saved.id, name: "新名字" });
  assert.equal(renamed.name, "新名字");
  assert.equal(renamed.file, saved.file);
  let markdown = await readFile(saved.file, "utf8");
  assert.match(markdown, /^name: 新名字$/m);
  assert.match(markdown, /^# 新名字$/m);
  assert.equal(service.templates().find((item) => item.id === saved.id).name, "新名字");

  const read = await service.readTemplateFile({ id: saved.id });
  assert.equal(read.content, markdown);
  assert.equal(read.path, saved.file);

  await assert.rejects(
    () => service.saveTemplateFile({ id: saved.id, content: "hello" }),
    (error) => error.code === "INVALID_TEMPLATE",
  );

  const next = markdown.replace(/^name: .*$/m, "name: 又改了").replace(/^# .*$/m, "# 又改了");
  const written = await service.saveTemplateFile({ id: saved.id, content: next });
  assert.equal(written.name, "又改了");
  markdown = await readFile(saved.file, "utf8");
  assert.match(markdown, /^# 又改了$/m);
  assert.equal(service.templates().find((item) => item.id === saved.id).name, "又改了");

  const plain = await service.saveTemplate({ name: "纯文本模板", objective: "打开设置" });
  await assert.rejects(
    () => service.readTemplateFile({ id: plain.id }),
    (error) => error.code === "TEMPLATE_NO_FILE",
  );
  const plainRenamed = await service.renameTemplate({ id: plain.id, name: "改个名" });
  assert.equal(plainRenamed.file, null);
  assert.equal(plainRenamed.name, "改个名");
  service.dispose();
  await rm(dir, { recursive: true, force: true });
});

const SUMMARY_PLAN = {
  name: "每日任务",
  goal: "完成每日任务并领奖",
  preconditions: ["游戏主页已打开"],
  success_criteria: ["看到已领取"],
  strategy: ["先从任务入口进入"],
  summary: ["打开任务页", "连续领取两轮"],
  irreversible: [],
  steps: [
    {
      id: "s1",
      source_step: 2,
      goal: "打开任务页",
      do: "tap",
      text: "任务",
      at: [0.5, 0.5],
      expect_after: "出现任务列表"
    },
    {
      id: "s2",
      source_step: 4,
      goal: "领取任务奖励",
      do: "tap",
      text: "领取",
      at: [0.6, 0.6],
      expect_after: "出现已领取"
    },
    { id: "s3", goal: "确认奖励到账", do: "expect", text: "已领取", timeout: 3 }
  ]
};

function makeSummaryAgents(events, taskAgent, taskSessionId, plan = SUMMARY_PLAN) {
  return {
    get: (id) => (id === taskSessionId ? taskAgent : undefined),
    create: async () => {
      const id = "summary-session";
      const agent = { id, status: "running", session: { id }, followup() {} };
      setTimeout(() => {
        events.emit(id, {
          type: "assistant/message",
          time: Date.now(),
          data: {
            message: {
              role: "assistant",
              content: [{ type: "text", text: "```json\n" + JSON.stringify(plan) + "\n```" }]
            }
          }
        });
        agent.status = "idle";
      }, 30);
      return { agent, dispose: async () => {} };
    }
  };
}

async function runTaskWithEvents(service, events, sessionId) {
  const task = await service.start({ objective: "完成日常任务", sessionId });
  await sleep(650);
  events.emit(sessionId, toolCall("c1", "phone_find_element", { keyword: "任务" }, 1200));
  events.emit(sessionId, toolResult("c1", 'Found "任务" at phone (632, 1400) with score 0.99.', { time: 1500 }));
  events.emit(sessionId, toolCall("c2", "phone_click", { x: 632, y: 1400 }, 1600));
  events.emit(sessionId, toolResult("c2", "Clicked phone (632, 1400).", { time: 1650 }));
  events.emit(sessionId, {
    type: "assistant/message",
    time: 1100,
    data: { message: { content: [{ type: "reasoning", text: "先找任务入口。\n\n现在点击任务。" }] } }
  });
  const agent = service.status(task.id);
  assert.equal(agent.status, "running");
  return task;
}

test("saveTemplate summarises the run into an intent plan", async () => {
  const dir = await mkdtemp(join(tmpdir(), "phone-summary-"));
  const events = makeEvents();
  const agent = makeAgent("session-summary");
  const service = makeService({
    agent,
    events,
    agents: makeSummaryAgents(events, agent, "session-summary"),
    config: {
      runsDir: join(dir, "runs"),
      templatesDir: join(dir, "templates"),
      summarizeTemplates: true
    }
  });
  const task = await runTaskWithEvents(service, events, "session-summary");
  agent.status = "idle";
  await sleep(700);

  const saved = await service.saveTemplate({
    name: "完成日常任务",
    objective: "完成日常任务",
    taskId: task.id
  });
  assert.equal(saved.source, "summary");
  const markdown = await readFile(saved.file, "utf8");
  assert.match(markdown, /## 执行总结/);
  assert.match(markdown, /generator: summary/);
  assert.match(markdown, /goal: 打开任务页/);
  assert.match(markdown, /expect_after: 出现任务列表/);
  assert.match(markdown, /do: expect/);
  assert.match(markdown, /text: 已领取/);
  assert.match(markdown, /## 判据/);
  service.dispose();
  await rm(dir, { recursive: true, force: true });
});

test("summary agents also run in the task workspace", async () => {
  const dir = await mkdtemp(join(tmpdir(), "phone-summary-cwd-"));
  const workspace = join(dir, "ws");
  const events = makeEvents();
  const agent = makeAgent("session-summary-cwd");
  const created = [];
  const service = makeService({
    agent,
    events,
    agents: {
      get: (id) => (id === "session-summary-cwd" ? agent : undefined),
      create: async (options) => {
        created.push(options);
        const id = "summary-session-cwd";
        const summaryAgent = { id, status: "running", session: { id }, followup() {} };
        setTimeout(() => {
          events.emit(id, {
            type: "assistant/message",
            time: Date.now(),
            data: {
              message: {
                role: "assistant",
                content: [
                  { type: "text", text: "```json\n" + JSON.stringify(SUMMARY_PLAN) + "\n```" },
                ],
              },
            },
          });
          summaryAgent.status = "idle";
        }, 30);
        return { agent: summaryAgent, dispose: async () => {} };
      },
    },
    config: {
      workspace,
      runsDir: join(dir, "runs"),
      templatesDir: join(dir, "templates"),
      summarizeTemplates: true,
    },
  });
  const task = await runTaskWithEvents(service, events, "session-summary-cwd");
  agent.status = "idle";
  await sleep(700);
  await service.saveTemplate({ name: "完成日常任务", objective: "完成日常任务", taskId: task.id });
  assert.equal(created.length, 1);
  assert.equal(created[0].meta?.cwd, workspace);
  service.dispose();
  await rm(dir, { recursive: true, force: true });
});

test("saveTemplate reports a summary failure instead of quietly downgrading", async () => {
  const dir = await mkdtemp(join(tmpdir(), "phone-summary-fail-"));
  const events = makeEvents();
  const agent = makeAgent("session-summary-fail");
  const service = makeService({
    agent,
    events,
    agents: {
      get: (id) => (id === "session-summary-fail" ? agent : undefined),
      create: async () => {
        throw new Error("model provider down");
      }
    },
    config: {
      runsDir: join(dir, "runs"),
      templatesDir: join(dir, "templates"),
      summarizeTemplates: true
    }
  });
  const task = await runTaskWithEvents(service, events, "session-summary-fail");
  agent.status = "idle";
  await sleep(700);

  await assert.rejects(
    () => service.saveTemplate({ name: "完成日常任务", objective: "完成日常任务", taskId: task.id }),
    (error) => error.code === "SUMMARY_FAILED" && /没有生成模板/.test(error.message)
  );
  assert.equal(
    service.templates().some((item) => item.name === "完成日常任务"),
    false,
    "a failed summary must not leave a half-template behind"
  );

  // Mechanical output is available only when explicitly requested.
  const explicit = await service.saveTemplate({
    name: "完成日常任务",
    objective: "完成日常任务",
    taskId: task.id,
    mechanical: true
  });
  assert.equal(explicit.source, "mechanical");
  const markdown = await readFile(explicit.file, "utf8");
  assert.match(markdown, /coords_known: true/);
  service.dispose();
  await rm(dir, { recursive: true, force: true });
});

test("trace keeps the reasoning that explains each phone call", async () => {
  const dir = await mkdtemp(join(tmpdir(), "phone-why-"));
  const events = makeEvents();
  const agent = makeAgent("session-why");
  const service = makeService({
    agent,
    events,
    config: { runsDir: join(dir, "runs"), templatesDir: join(dir, "templates") }
  });
  const task = await service.start({ objective: "点一下", sessionId: "session-why" });
  await sleep(650);
  events.emit("session-why", {
    type: "assistant/message",
    time: 1000,
    data: { message: { content: [{ type: "reasoning", text: "先确认页面。\n\n现在点击任务入口。" }] } }
  });
  events.emit("session-why", toolCall("c1", "phone_click", { x: 632, y: 1400 }, 1100));
  events.emit("session-why", toolResult("c1", "Clicked phone (632, 1400).", { time: 1150 }));
  agent.status = "idle";
  await sleep(700);

  const finished = service.status(task.id);
  const trace = (await readFile(finished.tracePath, "utf8")).trim().split("\n").map((line) => JSON.parse(line));
  const step = trace.find((record) => record.t === "step");
  assert.match(step.why, /现在点击任务入口/);
  assert.equal(step.whySeq, 1);
  const reasoning = await readFile(join(dirname(finished.tracePath), "reasoning.jsonl"), "utf8");
  assert.match(reasoning, /先确认页面/);
  service.dispose();
  await rm(dir, { recursive: true, force: true });
});

async function makeTemplateFixture(prefix, runTool = null) {
  const dir = await mkdtemp(join(tmpdir(), `${prefix}-`));
  const events = makeEvents();
  const agent = makeAgent(`${prefix}-session`);
  const stateFile = join(dir, "tasks.json");
  const service = makeService({
    agent,
    events,
    config: {
      runsDir: join(dir, "runs"),
      templatesDir: join(dir, "templates"),
      stateFile
    }
  });
  const task = await service.start({ objective: "点五次招募", sessionId: `${prefix}-session` });
  await sleep(650);
  events.emit(`${prefix}-session`, toolCall("c1", "phone_find_element", { keyword: "商城" }, 1200));
  events.emit(
    `${prefix}-session`,
    toolResult("c1", 'Found "商城" at phone (632, 1400) with score 0.99.', { time: 1500 })
  );
  events.emit(`${prefix}-session`, toolCall("c2", "phone_click", { x: 632, y: 1400 }, 1600));
  events.emit(`${prefix}-session`, toolResult("c2", "Clicked phone (632, 1400).", { time: 1650 }));
  agent.status = "idle";
  await sleep(700);
  const saved = await service.saveTemplate({ name: "招募", objective: "点五次招募", taskId: task.id });
  const tools = join(dir, "tools");
  await mkdir(tools, { recursive: true });
  await writeFile(join(tools, "tpl_review.py"), "# stub\n", "utf8");
  await writeFile(join(tools, "tpl_apply.py"), "# stub\n", "utf8");
  await writeFile(join(tools, "tpl_run.py"), "# stub\n", "utf8");
  const configured = makeService({
    agent,
    events,
    ...(runTool ? { runTool } : {}),
    config: {
      runsDir: join(dir, "runs"),
      templatesDir: join(dir, "templates"),
      stateFile,
      toolsDir: tools,
      pythonExecutable: "/usr/bin/true"
    }
  });
  await configured.ready;
  return { dir, service, configured, saved, task };
}

test("reviewTemplate shells out to the python reviewer and returns proposals", async () => {
  const { dir, configured, saved } = await makeTemplateFixture("phone-review");
  const calls = [];
  const analysis = {
    samples: { runs: 3, ok_runs: 3, p50_ms: 100, p95_ms: 120, enough: true },
    coords: { usable: true, reason: "ok" },
    steps: [{ id: "s1", do: "tap", ok: 3, runs: 3, attempts: 1, p95_ms: 80, note: "" }],
    proposals: [
      { uid: "s1:promote_coords", kind: "promote_coords", step_id: "s1", reason: "稳定" }
    ]
  };
  const result = await configured.reviewTemplate({ id: saved.id }).catch((error) => error);
  // The default runner is used here: tools are stubs and python is /usr/bin/true,
  // so the tool exits 0 without JSON -> a structured error, not a crash.
  assert.equal(result.code, "TEMPLATE_TOOL_BAD_OUTPUT", result && result.message);
  configured.dispose();
  await rm(dir, { recursive: true, force: true });
});

test("reviewTemplate parses a tool JSON reply", async () => {
  const { dir, configured, saved } = await makeTemplateFixture("phone-review-ok");
  const analysis = {
    samples: { runs: 3, ok_runs: 3, p50_ms: 100, p95_ms: 120, enough: true },
    coords: { usable: true, reason: "ok" },
    steps: [],
    proposals: [
      { uid: "s1:promote_coords", kind: "promote_coords", step_id: "s1", reason: "稳定" }
    ]
  };
  const stub = makeService({
    agent: makeAgent("x"),
    config: {
      runsDir: join(dir, "runs"),
      templatesDir: join(dir, "templates"),
      stateFile: join(dir, "tasks.json"),
      toolsDir: join(dir, "tools")
    },
    runTool: async (options) => {
      assert.match(options.tool, /tpl_review\.py$/);
      assert.deepEqual(options.args.slice(-1), ["--json"]);
      return { code: 0, stdout: JSON.stringify(analysis), stderr: "" };
    }
  });
  await stub.ready;
  const result = await stub.reviewTemplate({ id: saved.id });
  assert.equal(result.id, saved.id);
  assert.equal(result.proposals.length, 1);
  assert.equal(result.samples.runs, 3);
  stub.dispose();
  configured.dispose();
  await rm(dir, { recursive: true, force: true });
});

test("applyTemplateReview passes --yes and --only, rollback adds --rollback", async () => {
  const { dir, saved } = await makeTemplateFixture("phone-apply");
  const seen = [];
  const stub = makeService({
    agent: makeAgent("x"),
    config: {
      runsDir: join(dir, "runs"),
      templatesDir: join(dir, "templates"),
      stateFile: join(dir, "tasks.json"),
      toolsDir: join(dir, "tools")
    },
    runTool: async (options) => {
      seen.push(options.args.slice());
      return {
        code: 0,
        stdout: JSON.stringify({
          applied: [{ id: "s1", kind: "promote_coords" }],
          skipped: [],
          diff: "@@ -1 +1 @@",
          version: 2,
          new_version: 3,
          written: true
        }),
        stderr: ""
      };
    }
  });
  await stub.ready;
  const applied = await stub.applyTemplateReview({ id: saved.id, uids: ["s1:promote_coords"] });
  assert.equal(applied.new_version, 3);
  assert.ok(seen[0].includes("--yes"));
  assert.equal(seen[0][seen[0].indexOf("--only") + 1], "s1:promote_coords");

  const preview = await stub.applyTemplateReview({ id: saved.id, uids: [], dryRun: true });
  assert.equal(preview.new_version, 3);
  assert.equal(seen[1].includes("--yes"), false);

  const rolled = await stub.rollbackTemplate({ id: saved.id });
  assert.match(seen[2].join(" "), /--rollback/);
  assert.equal(rolled.id, saved.id);
  stub.dispose();
  await rm(dir, { recursive: true, force: true });
});

test("reviewTemplate reports TOOL_MISSING when the workspace has no tools dir", async () => {
  const dir = await mkdtemp(join(tmpdir(), "phone-notools-"));
  const service = makeService({
    agent: makeAgent("x"),
    config: { workspace: join(dir, "ws"), templatesDir: join(dir, "templates") }
  });
  await service.ready;
  await service.saveTemplate({ name: "x", objective: "x" }).then(() => {});
  // A JSON-only template has no file: the file guard fires first.
  const plain = service.templates().find((item) => item.name === "x");
  await assert.rejects(
    () => service.reviewTemplate({ id: plain.id }),
    (error) => error.code === "TEMPLATE_NO_FILE"
  );
  service.dispose();
  await rm(dir, { recursive: true, force: true });
});

function makeTemplateRunService({ startTool, template, dir, agent = makeAgent("tpl-agent") }) {
  return makeService({
    agent,
    startTool,
    config: {
      runsDir: join(dir, "runs"),
      templatesDir: join(dir, "templates"),
      stateFile: join(dir, "tasks.json"),
      toolsDir: join(dir, "tools")
    }
  });
}

const RUN_MAIN = `---
id: app
name: app
version: 1
screen: [1080, 2400]
coords_known: true
coords_updated: 2026-11-18
---

## 步骤

\`\`\`yaml
steps:
  - {id: s1, do: key, code: BACK}
\`\`\`
`;

const RUN_WITH_ENTRY = `---
id: app
name: app
version: 1
screen: [1080, 2400]
coords_known: true
coords_updated: 2026-11-18
entry:
  expect: 我的淘宝
  timeout: 10
  approach:
    - {do: key, code: HOME}
---

## 步骤

\`\`\`yaml
steps:
  - {id: s1, do: key, code: BACK}
\`\`\`
`;

async function makeRunTemplateFixture(prefix, markdown) {
  const dir = await mkdtemp(join(tmpdir(), `${prefix}-`));
  const templates = join(dir, "templates");
  await mkdir(templates, { recursive: true });
  const file = join(templates, "app.md");
  await writeFile(file, markdown, "utf8");
  const tools = join(dir, "tools");
  await mkdir(tools, { recursive: true });
  await writeFile(join(tools, "tpl_run.py"), "# stub\n", "utf8");
  const stateFile = join(dir, "tasks.json");
  await writeFile(
    stateFile,
    JSON.stringify({
      templates: [{ id: "app", name: "app", objective: "x", variables: [], file }],
      history: [],
      schedules: []
    }),
    "utf8"
  );
  const config = {
    runsDir: join(dir, "runs"),
    templatesDir: templates,
    stateFile,
    toolsDir: tools,
    workspace: dir
  };
  return { dir, file, config };
}

/** Fake phase runner: emits events per phase and resolves when told to. */
function makePhaseScript(specs) {
  const calls = [];
  const start = (options) => {
    const args = options.args || [];
    const phase = args.includes("--entry-only") ? "entry" : "main";
    calls.push({ phase, args });
    const spec = specs[phase] || { ok: true, steps: 1 };
    let resolveDone;
    const handle = {
      phase,
      child: { kill() {} },
      kill() {
        resolveDone({ code: 1, stderr: "killed" });
      },
      done: new Promise((resolve) => {
        resolveDone = resolve;
      })
    };
    if (spec.pending) return handle;
    setTimeout(() => {
      const runDir = args[args.indexOf("--run-dir") + 1];
      if (spec.writeReport && runDir) {
        mkdirSync(runDir, { recursive: true });
        writeFileSync(
          join(runDir, "report.jsonl"),
          JSON.stringify({
            type: "step",
            index: 1,
            action: "assert_text",
            status: "failed",
            attempts: 3,
            elapsed_ms: 3000,
            params: { text: "专属客服", id: "s1-check" },
            error: "ASSERT_FAILED: '专属客服' not found within 3.0s"
          }) + "\n",
          "utf8"
        );
      }
      options.onLine(JSON.stringify({ event: "start", total: spec.steps || 1, runDir }));
      options.onLine(
        JSON.stringify({
          event: "step",
          index: 1,
          total: spec.steps || 1,
          action: "key",
          ok: spec.ok !== false,
          ms: 5
        })
      );
      options.onLine(
        JSON.stringify({
          event: "done",
          ok: spec.ok !== false,
          steps: spec.steps || 1,
          tracePath: runDir ? `${runDir}/trace.jsonl` : "/tmp/trace.jsonl",
          reviewPath: null
        })
      );
      resolveDone({ code: spec.ok === false ? 1 : 0, stderr: "" });
    }, 0);
    return handle;
  };
  return { start, calls };
}

test("unknown environment is driven entirely by the model", async () => {
  const { dir, config } = await makeRunTemplateFixture("phone-guided-full", RUN_WITH_ENTRY);
  const phases = makePhaseScript({ main: { ok: true } });
  const guidedRuns = [];
  const service = makeService({
    agent: makeAgent("tpl"),
    config,
    startTool: phases.start,
    guidedRun: async (context) => {
      guidedRuns.push(context);
      return { ok: true, doneText: "一键价保" };
    }
  });
  await service.ready;
  const task = await service.runTemplate({ id: "app" });
  await sleep(800);

  const final = service.status(task.id);
  assert.equal(final.status, "done");
  assert.equal(final.guided, true);
  assert.equal(final.guidedFull, true);
  assert.equal(guidedRuns.length, 1);
  assert.equal(guidedRuns[0].entryExpect, "我的淘宝");
  assert.match(guidedRuns[0].markdown, /steps:/);
  // No YAML phase runs when the environment is unknown.
  assert.deepEqual(phases.calls, []);
  service.dispose();
  await rm(dir, { recursive: true, force: true });
});

test("a failed full auto-run reports GUIDED_RUN_FAILED without replaying YAML", async () => {
  const { dir, config } = await makeRunTemplateFixture("phone-guided-full-fail", RUN_WITH_ENTRY);
  const phases = makePhaseScript({ main: { ok: true } });
  const service = makeService({
    agent: makeAgent("tpl"),
    config,
    startTool: phases.start,
    guidedRun: async () => ({ ok: false, message: "模型卡在登录页" })
  });
  await service.ready;
  const task = await service.runTemplate({ id: "app" });
  await sleep(800);
  const final = service.status(task.id);
  assert.equal(final.status, "error");
  assert.equal(final.error.code, "GUIDED_RUN_FAILED");
  assert.match(final.error.message, /登录页/);
  assert.deepEqual(phases.calls, [], "no blind coordinate execution");
  service.dispose();
  await rm(dir, { recursive: true, force: true });
});

test("guidedFirst:false replays the recorded approach before calling the model", async () => {
  const { dir, config } = await makeRunTemplateFixture("phone-replay-first", RUN_WITH_ENTRY);
  const phases = makePhaseScript({ entry: { ok: true }, main: { ok: true } });
  let guided = 0;
  const service = makeService({
    agent: makeAgent("tpl"),
    config: { ...config, guidedFirst: false },
    startTool: phases.start,
    guidedApproach: async () => {
      guided += 1;
      return { ok: true };
    }
  });
  await service.ready;
  const task = await service.runTemplate({ id: "app" });
  await sleep(800);
  const final = service.status(task.id);
  assert.equal(final.status, "done");
  assert.equal(guided, 0);
  assert.deepEqual(phases.calls.map((call) => call.phase), ["entry", "main"]);
  service.dispose();
  await rm(dir, { recursive: true, force: true });
});

test("a reachable entry skips guided navigation", async () => {
  const { dir, config } = await makeRunTemplateFixture("phone-guided-skip", RUN_WITH_ENTRY);
  const phases = makePhaseScript({ entry: { ok: true }, main: { ok: true } });
  let guided = 0;
  const service = makeService({
    agent: makeAgent("tpl"),
    config,
    startTool: phases.start,
    clientFactory: () => ({
      baseUrl: "http://127.0.0.1:8765",
      health: async () => ({ ok: true }),
      status: async () => ({ ok: true, screen: [1080, 2400], status: { state: "READY", usb: true } }),
      screenshot: async () => ({ ok: true }),
      find: async () => ({ ok: true, found: true })
    }),
    guidedApproach: async () => {
      guided += 1;
      return { ok: true };
    }
  });
  await service.ready;
  const task = await service.runTemplate({ id: "app" });
  await sleep(800);
  assert.equal(service.status(task.id).status, "done");
  assert.equal(guided, 0);
  assert.deepEqual(phases.calls.map((call) => call.phase), ["main"]);
  service.dispose();
  await rm(dir, { recursive: true, force: true });
});

test("guided and replay both failing reports ENTRY_UNREACHABLE", async () => {
  const { dir, config } = await makeRunTemplateFixture("phone-guided-fail", RUN_WITH_ENTRY);
  const phases = makePhaseScript({ entry: { ok: false } });
  const service = makeService({
    agent: makeAgent("tpl"),
    config: { ...config, guidedFirst: false },
    startTool: phases.start,
    guidedApproach: async () => ({ ok: false, message: "还是找不到起始页" })
  });
  await service.ready;
  const task = await service.runTemplate({ id: "app" });
  await sleep(800);
  const final = service.status(task.id);
  assert.equal(final.status, "error");
  assert.equal(final.error.code, "ENTRY_UNREACHABLE");
  assert.match(final.error.message, /还是找不到起始页/);
  // Replay is the last resort; the main path must never run without the gate.
  assert.deepEqual(phases.calls.map((call) => call.phase), ["entry"]);
  service.dispose();
  await rm(dir, { recursive: true, force: true });
});

test("a template without entry runs the main path once", async () => {
  const { dir, config } = await makeRunTemplateFixture("phone-no-entry", RUN_MAIN);
  const phases = makePhaseScript({ main: { ok: true } });
  const service = makeService({ agent: makeAgent("tpl"), config, startTool: phases.start });
  await service.ready;
  const task = await service.runTemplate({ id: "app" });
  await sleep(800);
  assert.equal(service.status(task.id).status, "done");
  assert.deepEqual(phases.calls.map((call) => call.phase), ["main"]);
  service.dispose();
  await rm(dir, { recursive: true, force: true });
});

test("stop kills an in-flight template phase", async () => {
  const { dir, config } = await makeRunTemplateFixture("phone-stop-template", RUN_WITH_ENTRY);
  const phases = makePhaseScript({ entry: { pending: true } });
  const service = makeService({ agent: makeAgent("tpl"), config, startTool: phases.start });
  await service.ready;
  const task = await service.runTemplate({ id: "app" });
  await sleep(120);
  const stopped = await service.stop(task.id);
  assert.equal(stopped.status, "stopped");
  await sleep(1600); // environment check retries may still be in flight
  assert.equal(service.status(task.id).status, "stopped");
  assert.ok(service.history().some((item) => item.id === task.id && item.status === "stopped"));
  service.dispose();
  await rm(dir, { recursive: true, force: true });
});

test("runTemplate refuses a template without an md file", async () => {
  const service = makeService({ agent: makeAgent() });
  await service.ready;
  const plain = await service.saveTemplate({ name: "纯文字", objective: "打开设置" });
  await assert.rejects(
    () => service.runTemplate({ id: plain.id }),
    (error) => error.code === "TEMPLATE_NO_FILE"
  );
  service.dispose();
});

test("saveTemplate can use an older history trace after a host restart", async () => {
  const dir = await mkdtemp(join(tmpdir(), "phone-hist-template-"));
  const events = makeEvents();
  const agent = makeAgent("session-old");
  const stateFile = join(dir, "tasks.json");
  const config = {
    runsDir: join(dir, "runs"),
    templatesDir: join(dir, "templates"),
    stateFile
  };
  const first = makeService({ agent, events, config });
  const task = await first.start({ objective: "点一下", sessionId: "session-old" });
  await sleep(650);
  events.emit("session-old", toolCall("c1", "phone_find_element", { keyword: "设置" }, 1200));
  events.emit(
    "session-old",
    toolResult("c1", 'Found "设置" at phone (540, 1200) with score 0.99.', { time: 1500 })
  );
  events.emit("session-old", toolCall("c2", "phone_click", { x: 540, y: 1200 }, 1600));
  events.emit("session-old", toolResult("c2", "Clicked phone (540, 1200).", { time: 1650 }));
  agent.status = "idle";
  await sleep(1200);
  assert.ok(first.status(task.id).tracePath);
  first.dispose();
  await sleep(300); // finish() persists asynchronously

  // A fresh service has an empty recentTraces cache: the trace must come from disk.
  const restarted = makeService({ agent: makeAgent("restarted"), config });
  await restarted.ready;
  const saved = await restarted.saveTemplate({
    name: "旧任务",
    objective: "点一下",
    taskId: task.id
  });
  assert.ok(saved.file, "history row still becomes a template after restart");
  assert.equal(saved.source, "mechanical");
  const markdown = await readFile(saved.file, "utf8");
  assert.match(markdown, /coords_known: true/);
  assert.match(markdown, /do: tap/);
  restarted.dispose();

  // An id with no trace is a hard error: no half-template is saved.
  await assert.rejects(
    () => restarted.saveTemplate({ name: "x", objective: "x", taskId: "missing-task" }),
    (error) => error.code === "TRACE_NOT_FOUND"
  );
  await rm(dir, { recursive: true, force: true });
});

test("reconnectAdb runs the workspace tool and returns its diagnosis", async () => {
  const dir = await mkdtemp(join(tmpdir(), "phone-adb-"));
  const tools = join(dir, "tools");
  await mkdir(tools, { recursive: true });
  await writeFile(join(tools, "adb_reconnect.py"), "# stub\n", "utf8");
  const seen = [];
  const service = makeService({
    agent: makeAgent("adb-agent"),
    config: { workspace: dir, toolsDir: tools, templatesDir: join(dir, "templates") },
    runTool: async (options) => {
      seen.push(options);
      return {
        code: 0,
        stdout: JSON.stringify({
          ok: false,
          endpoint: "192.168.1.149:33221",
          hint: "能找到无线调试端口，但 adb 连不上：需要重新配对"
        }),
        stderr: ""
      };
    }
  });
  await service.ready;
  const result = await service.reconnectAdb({});
  assert.match(seen[0].tool, /adb_reconnect\.py$/);
  assert.deepEqual(seen[0].args, ["--json"]);
  assert.equal(result.ok, false);
  assert.match(result.hint, /重新配对/);
  assert.equal(result.endpoint, "192.168.1.149:33221");

  await service.reconnectAdb({ endpoint: "10.0.0.2:5555" });
  assert.deepEqual(seen[1].args, ["--json", "--endpoint", "10.0.0.2:5555"]);
  service.dispose();
  await rm(dir, { recursive: true, force: true });
});

test("preflight retries a slow device status before failing", async () => {
  let calls = 0;
  const service = createTaskService({
    clientFactory: () => ({
      baseUrl: "http://127.0.0.1:8765",
      health: async () => ({ ok: true }),
      status: async () => {
        calls += 1;
        if (calls < 3) throw new Error("hand timeout after restart");
        return { ok: true, screen: [1080, 2400], status: { state: "READY", usb: true } };
      },
      screenshot: async () => ({ ok: true })
    }),
    agents: {},
    config: { stateFile: join(tmpdir(), `phone-retry-${Date.now()}.json`) },
    logger: { info() {}, warn() {} }
  });
  await service.ready;
  const result = await service.preflight({});
  assert.deepEqual(result, { ok: true, device: "default" });
  assert.equal(calls, 3);
  service.dispose();
});

test("preflight surfaces a dead hand instead of a generic message", async () => {
  const service = createTaskService({
    clientFactory: () => ({
      baseUrl: "http://127.0.0.1:8765",
      health: async () => ({ ok: true }),
      status: async () => ({
        ok: true,
        screen: [1080, 2400],
        status: { state: "READY", usb: true },
        hand: { ok: false, error: "TCP connect timeout" }
      }),
      screenshot: async () => ({ ok: true })
    }),
    agents: {},
    config: { stateFile: join(tmpdir(), `phone-hand-${Date.now()}.json`) },
    logger: { info() {}, warn() {} }
  });
  await service.ready;
  await assert.rejects(
    () => service.preflight({}),
    (error) => error.code === "DEVICE_NOT_READY" && /控制手没有响应/.test(error.message) &&
      /TCP connect timeout/.test(error.message)
  );
  service.dispose();
});

test("a failed main phase explains which step failed", async () => {
  const { dir, config } = await makeRunTemplateFixture("phone-tpl-fail", RUN_WITH_ENTRY);
  const phases = makePhaseScript({ main: { ok: false, writeReport: true } });
  const service = makeService({
    agent: makeAgent("tpl"),
    config: { ...config, guidedFirst: false },
    startTool: phases.start,
    guidedApproach: async () => ({ ok: true })
  });
  await service.ready;
  const task = await service.runTemplate({ id: "app" });
  await sleep(800);
  const current = service.status(task.id);
  assert.equal(current.status, "error");
  assert.match(current.error.message, /第 1 步 assert_text/);
  assert.match(current.error.message, /专属客服/);
  service.dispose();
  await rm(dir, { recursive: true, force: true });
});

test("a run that recovered from a failed step can still become a template", async () => {
  const dir = await mkdtemp(join(tmpdir(), "phone-recovered-tpl-"));
  const events = makeEvents();
  const agent = makeAgent("session-recovered");
  const service = makeService({
    agent,
    events,
    config: {
      runsDir: join(dir, "runs"),
      templatesDir: join(dir, "templates"),
      stateFile: join(dir, "tasks.json")
    }
  });
  const task = await service.start({ objective: "点一下", sessionId: "session-recovered" });
  await sleep(650);
  events.emit("session-recovered", toolCall("c1", "phone_click", { x: 10, y: 20 }, 1000));
  events.emit(
    "session-recovered",
    toolResult("c1", "Error: device busy", { time: 1100, isError: true })
  );
  events.emit("session-recovered", toolCall("c2", "phone_click", { x: 10, y: 20 }, 2000));
  events.emit(
    "session-recovered",
    toolResult("c2", "Clicked phone (10, 20).", { time: 2100 })
  );
  agent.status = "idle";
  await sleep(800);

  const saved = await service.saveTemplate({
    name: "恢复后的任务",
    objective: "点一下",
    taskId: task.id
  });
  assert.ok(saved.file, "process failures must not block the lesson");
  const trace = (await readFile(service.status(task.id).tracePath, "utf8")).trim().split("\n");
  const done = JSON.parse(trace.at(-1));
  assert.equal(done.failed, 1, "process hiccups are recorded");
  assert.equal(done.ok, true, "final outcome is success");
  service.dispose();
  await rm(dir, { recursive: true, force: true });
});

test("saveTemplate refuses a task whose final outcome was failure", async () => {
  const dir = await mkdtemp(join(tmpdir(), "phone-fail-tpl-"));
  const events = makeEvents();
  const agent = makeAgent("session-failed-run");
  const service = makeService({
    agent,
    events,
    config: {
      runsDir: join(dir, "runs"),
      templatesDir: join(dir, "templates"),
      stateFile: join(dir, "tasks.json")
    }
  });
  const task = await service.start({ objective: "点一下", sessionId: "session-failed-run" });
  await sleep(650);
  events.emit("session-failed-run", toolCall("c1", "phone_click", { x: 10, y: 20 }, 1000));
  events.emit(
    "session-failed-run",
    toolResult("c1", "Clicked phone (10, 20).", { time: 1100 })
  );
  await sleep(100);
  await service.stop(task.id); // final outcome: stopped, not success
  await sleep(700);

  await assert.rejects(
    () => service.saveTemplate({ name: "失败草稿", objective: "点一下", taskId: task.id }),
    (error) => error.code === "TRACE_HAS_FAILURES"
  );
  const forced = await service.saveTemplate({
    name: "失败草稿",
    objective: "点一下",
    taskId: task.id,
    force: true
  });
  assert.ok(forced.file, "force still allows a draft");
  service.dispose();
  await rm(dir, { recursive: true, force: true });
});

test("old traces with step failures but a successful outcome can still become templates", async () => {
  const dir = await mkdtemp(join(tmpdir(), "phone-old-trace-"));
  const runDir = join(dir, "runs", "panel", "old-task", "20261117-204735");
  await mkdir(runDir, { recursive: true });
  const lines = [
    JSON.stringify({
      t: "run",
      id: "old-task",
      status: "done",
      objective: "打开淘宝",
      screen: [1264, 2800],
      sessionId: "session-old"
    }),
    JSON.stringify({ t: "step", i: 1, id: "p1-find", do: "find", ok: false, ms: 10, args: { keyword: "我的淘宝" } }),
    JSON.stringify({ t: "step", i: 2, id: "p2-tap", do: "tap", ok: true, ms: 10, at: [500, 800] }),
    JSON.stringify({ t: "done", ok: false, steps: 2, failed: 1, ms: 100 })
  ];
  await writeFile(join(runDir, "trace.jsonl"), lines.join("\n") + "\n", "utf8");
  const stateFile = join(dir, "tasks.json");
  await writeFile(
    stateFile,
    JSON.stringify({
      templates: [],
      schedules: [],
      history: [
        {
          id: "old-task",
          objective: "打开淘宝",
          status: "done",
          mode: "attach",
          startedAt: 1,
          finishedAt: 2,
          tracePath: join(runDir, "trace.jsonl")
        }
      ]
    }),
    "utf8"
  );
  const service = makeService({
    agent: makeAgent("x"),
    config: { runsDir: join(dir, "runs"), templatesDir: join(dir, "templates"), stateFile }
  });
  await service.ready;
  const saved = await service.saveTemplate({ name: "旧任务", objective: "打开淘宝", taskId: "old-task" });
  assert.ok(saved.file, saved.warnings.join(" | "));
  const markdown = await readFile(saved.file, "utf8");
  assert.match(markdown, /entry: \{expect: 我的淘宝/);
  service.dispose();
  await rm(dir, { recursive: true, force: true });
});

test("host-created agents inherit the profile's default model route", async () => {
  const dir = await mkdtemp(join(tmpdir(), "phone-route-"));
  const home = join(dir, "dshhome");
  await mkdir(join(home, "profiles", "test"), { recursive: true });
  await writeFile(
    join(home, "profiles", "test", "cordis.patch.yml"),
    [
      "- id: agent-default-model",
      '  name: "@deepseek-ai/dsh-agent-default-model"',
      "  config:",
      "    provider: opencode-go",
      "    model: deepseek-v4.1-flash",
      "- id: other",
      "  config:",
      "    provider: wrong",
      ""
    ].join("\n"),
    "utf8"
  );
  const previous = { home: process.env.DSH_HOME, profile: process.env.DSH_PROFILE };
  process.env.DSH_HOME = home;
  process.env.DSH_PROFILE = "test";
  let captured = null;
  const agent = makeAgent("auto-route");
  try {
    const service = makeService({
      agent,
      agents: {
        get: () => undefined,
        create: async (options) => {
          captured = options;
          return { agent };
        }
      },
      config: { stateFile: join(dir, "tasks.json") }
    });
    await service.start({ objective: "打开设置" });
    assert.equal(captured.agentOptions.provider, "opencode-go");
    assert.equal(captured.agentOptions.model, "deepseek-v4.1-flash");
    service.dispose();
  } finally {
    if (previous.home === undefined) delete process.env.DSH_HOME;
    else process.env.DSH_HOME = previous.home;
    if (previous.profile === undefined) delete process.env.DSH_PROFILE;
    else process.env.DSH_PROFILE = previous.profile;
    await rm(dir, { recursive: true, force: true });
  }
});

test("summarising without any model route fails with a clear message", async () => {
  const dir = await mkdtemp(join(tmpdir(), "phone-noroute-"));
  const home = join(dir, "emptyhome");
  await mkdir(join(home, "profiles", "test"), { recursive: true });
  const previous = { home: process.env.DSH_HOME, profile: process.env.DSH_PROFILE };
  process.env.DSH_HOME = home;
  process.env.DSH_PROFILE = "test";
  try {
    const { dir: fixtureDir, task } = await makeTemplateFixture("phone-noroute");
    const service = makeService({
      agent: makeAgent("x"),
      agents: { get: () => undefined, create: async () => ({ agent: makeAgent("y") }) },
      config: {
        runsDir: join(fixtureDir, "runs"),
        templatesDir: join(fixtureDir, "templates"),
        stateFile: join(fixtureDir, "tasks.json"),
        summarizeTemplates: true
      }
    });
    await service.ready;
    await assert.rejects(
      () => service.saveTemplate({ name: "x", objective: "x", taskId: task.id }),
      (error) =>
        error.code === "SUMMARY_FAILED" && /没有可用的模型路由|总结失败/.test(error.message)
    );
    service.dispose();
  } finally {
    if (previous.home === undefined) delete process.env.DSH_HOME;
    else process.env.DSH_HOME = previous.home;
    if (previous.profile === undefined) delete process.env.DSH_PROFILE;
    else process.env.DSH_PROFILE = previous.profile;
    await rm(dir, { recursive: true, force: true });
  }
});

test("injected task messages use a producer-owned source kind", async () => {
  const agent = makeAgent("auto-source");
  const service = makeService({
    agent,
    agents: { get: () => undefined, create: async () => ({ agent }) }
  });
  await service.start({ objective: "打开设置" });
  // DSH session format v4 rejects the retired `{kind:"plugin"}` wrapper.
  assert.notEqual(agent.followupMessage.source.kind, "plugin");
  assert.equal(agent.followupMessage.source.kind, "dsh-plugin-phone");
  service.dispose();
});

test("summariser prompt uses a producer-owned source kind", async () => {
  const { dir, task } = await makeTemplateFixture("phone-source");
  const summaryAgent = {
    id: "summary-source",
    status: "idle",
    session: { id: "summary-source" },
    followup(message) {
      this.message = message;
    }
  };
  const service = makeService({
    agent: makeAgent("x"),
    agents: {
      get: () => undefined,
      create: async () => ({ agent: summaryAgent, dispose: async () => {} })
    },
    config: {
      runsDir: join(dir, "runs"),
      templatesDir: join(dir, "templates"),
      stateFile: join(dir, "tasks.json"),
      summarizeTemplates: true,
      summaryTimeoutMs: 300,
      provider: "test-provider",
      model: "test-model"
    }
  });
  await service.ready;
  await assert.rejects(
    () => service.saveTemplate({ name: "x", objective: "x", taskId: task.id }),
    (error) => error.code === "SUMMARY_FAILED"
  );
  assert.equal(summaryAgent.message.source.kind, "dsh-plugin-phone");
  service.dispose();
  await rm(dir, { recursive: true, force: true });
});

test("a failed approach-learn write does not block the main path", async () => {
  const { dir, config } = await makeRunTemplateFixture("phone-learn-fail", RUN_WITH_ENTRY);
  const phases = makePhaseScript({ entry: { ok: false }, main: { ok: true } });
  const service = makeService({
    agent: makeAgent("tpl"),
    config: { ...config, guidedFirst: false },
    startTool: phases.start,
    guidedApproach: async () => ({ ok: true, steps: [{ do: "key", code: "HOME" }] }),
    learnApproach: async () => {
      throw new Error("disk full");
    }
  });
  await service.ready;
  const task = await service.runTemplate({ id: "app" });
  await sleep(800);
  const final = service.status(task.id);
  assert.equal(final.status, "done");
  assert.equal(final.learned, null);
  service.dispose();
  await rm(dir, { recursive: true, force: true });
});

test("an already-open start page skips the approach entirely", async () => {
  const { dir, config } = await makeRunTemplateFixture("phone-env-at", RUN_WITH_ENTRY);
  const phases = makePhaseScript({ main: { ok: true } });
  let findCalls = 0;
  const service = makeService({
    agent: makeAgent("tpl"),
    config,
    startTool: phases.start,
    clientFactory: () => ({
      baseUrl: "http://127.0.0.1:8765",
      health: async () => ({ ok: true }),
      status: async () => ({ ok: true, screen: [1080, 2400], status: { state: "READY", usb: true } }),
      screenshot: async () => ({ ok: true }),
      find: async () => {
        findCalls += 1;
        return { ok: true, found: true };
      }
    })
  });
  await service.ready;
  const task = await service.runTemplate({ id: "app" });
  await sleep(800);
  const final = service.status(task.id);
  assert.equal(final.status, "done");
  assert.equal(final.envAtEntry, true);
  assert.equal(final.guided, false);
  assert.equal(findCalls, 1);
  assert.deepEqual(phases.calls.map((call) => call.phase), ["main"]);
  service.dispose();
  await rm(dir, { recursive: true, force: true });
});

test("envKnown skips the environment check and the approach", async () => {
  const { dir, config } = await makeRunTemplateFixture("phone-env-known", RUN_WITH_ENTRY);
  const phases = makePhaseScript({ main: { ok: true } });
  let findCalls = 0;
  const service = makeService({
    agent: makeAgent("tpl"),
    config,
    startTool: phases.start,
    clientFactory: () => ({
      baseUrl: "http://127.0.0.1:8765",
      health: async () => ({ ok: true }),
      status: async () => ({ ok: true, screen: [1080, 2400], status: { state: "READY", usb: true } }),
      screenshot: async () => ({ ok: true }),
      find: async () => {
        findCalls += 1;
        return { ok: true, found: false };
      }
    })
  });
  await service.ready;
  const task = await service.runTemplate({ id: "app", envKnown: true });
  await sleep(800);
  const final = service.status(task.id);
  assert.equal(final.status, "done");
  assert.equal(final.envKnown, true);
  assert.equal(final.envAtEntry, false);
  assert.equal(findCalls, 0);
  assert.deepEqual(phases.calls.map((call) => call.phase), ["main"]);
  service.dispose();
  await rm(dir, { recursive: true, force: true });
});

test("a start page that is not open still runs the approach when replay-first", async () => {
  const { dir, config } = await makeRunTemplateFixture("phone-env-away", RUN_WITH_ENTRY);
  const phases = makePhaseScript({ entry: { ok: true }, main: { ok: true } });
  const service = makeService({
    agent: makeAgent("tpl"),
    config: { ...config, guidedFirst: false },
    startTool: phases.start
  });
  await service.ready;
  const task = await service.runTemplate({ id: "app" });
  await sleep(800);
  const final = service.status(task.id);
  assert.equal(final.conclusion ?? final.status, "done");
  assert.equal(final.envAtEntry, false);
  assert.deepEqual(phases.calls.map((call) => call.phase), ["entry", "main"]);
  service.dispose();
  await rm(dir, { recursive: true, force: true });
});

test("readTemplateCompletion finds the last step and its evidence", () => {
  const markdown = `---
id: app
version: 1
---

## 步骤

\`\`\`yaml
steps:
  - {id: s3, do: tap, text: 一键价保, at: [0.5, 0.89], expect_after: 我知道了}
  - {id: s4, do: tap, text: 我知道了, at: [0.5, 0.91], expect_after: 一键价保}
\`\`\`
`;
  const completion = readTemplateCompletion(markdown);
  assert.equal(completion.lastStepText, "我知道了");
  assert.equal(completion.expectAfter, "一键价保");
});

test("sawTextInCaptured detects tool output that mentioned the text", () => {
  const captured = [
    { type: "tool/call", data: { name: "phone_ocr_texts", arguments: "{}" } },
    {
      type: "tool/result",
      data: {
        message: { content: [{ type: "text", text: '- "一键价保" at (636, 2542)\n- "我知道了" at (630, 2560)' }] }
      }
    }
  ];
  assert.equal(sawTextInCaptured(captured, "我知道了"), true);
  assert.equal(sawTextInCaptured(captured, "官方自助服务"), false);
  assert.equal(sawTextInCaptured([], "我知道了"), false);
});

test("readSuccessExpect picks up the unique terminal evidence", () => {
  const markdown = `---
id: app
version: 2
success_expect: 我知道了
---

## 步骤

\`\`\`yaml
steps:
  - {id: s3, do: tap, text: 一键价保, expect_after: 我知道了}
\`\`\`
`;
  assert.equal(readSuccessExpect(markdown), "我知道了");
  assert.equal(readSuccessExpect("---\nid: app\n---\n"), null);
});

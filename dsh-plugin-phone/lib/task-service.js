/**
 * Host-owned phone task lifecycle.
 *
 * The client creates/prompts an independent task session (attach mode), or the
 * host creates the agent itself (autonomous mode, used by schedules). This
 * service owns health checks, status, step counting, stop, history, templates
 * and host timers.
 */
import { randomUUID } from "node:crypto";
import { existsSync } from "node:fs";
import { access, mkdir, readFile, writeFile } from "node:fs/promises";
import { homedir } from "node:os";
import { dirname, join, resolve, sep } from "node:path";

import {
  assistantMessageText,
  buildTrace,
  createTraceBuffer,
  parseArguments,
  serializeTrace,
} from "./trace.js";
import { buildTemplateMarkdown, templateId } from "./template-md.js";
import {
  parseToolJson,
  resolvePython,
  resolveToolsDir,
  runTool as defaultRunTool,
  startTool as defaultStartTool,
} from "./template-review.js";
import {
  buildDigest,
  buildSummaryPrompt,
  normalizeSummary,
  parseSummaryResponse,
  renderSummaryTemplate,
  summaryTrace,
} from "./summary.js";

const MAX_HISTORY = 100;
const MAX_STEPS = 40;
const MAX_RECENT_TRACES = 20;
const DEFAULT_STATE_FILE = join(homedir(), ".dsh", "phone-service", "tasks.json");

function stamp(date = new Date()) {
  const pad = (value) => String(value).padStart(2, "0");
  return (
    `${date.getFullYear()}${pad(date.getMonth() + 1)}${pad(date.getDate())}-` +
    `${pad(date.getHours())}${pad(date.getMinutes())}${pad(date.getSeconds())}`
  );
}

const YAML_PLAIN = /^[\w\u4e00-\u9fa5·./ -]+$/;

function yamlScalar(value) {
  const text = String(value ?? "");
  return YAML_PLAIN.test(text) ? text : JSON.stringify(text);
}

/** Replace the `name:` line in frontmatter and the first H1 heading. */
export function patchTemplateName(content, name) {
  const lines = String(content).split("\n");
  let inFront = false;
  let patchedFront = false;
  for (let index = 0; index < lines.length; index += 1) {
    const line = lines[index];
    if (index === 0 && line.trim() === "---") {
      inFront = true;
      continue;
    }
    if (inFront) {
      if (line.trim() === "---") {
        inFront = false;
        continue;
      }
      if (/^name:\s*/.test(line)) {
        lines[index] = `name: ${yamlScalar(name)}`;
        patchedFront = true;
      }
      continue;
    }
    if (/^#\s+/.test(line)) {
      lines[index] = `# ${name}`;
      break;
    }
  }
  return patchedFront ? lines.join("\n") : content;
}

/** Pull the frontmatter `name:` back out so editing the md renames the chip too. */
export function readTemplateName(content) {
  const match = String(content).match(/^---\s*\n([\s\S]*?)\n---/);
  if (!match) return "";
  const line = match[1].split("\n").find((item) => /^name:\s*/.test(item));
  if (!line) return "";
  const raw = line.replace(/^name:\s*/, "").trim();
  if (!raw) return "";
  if (raw.startsWith('"')) {
    try {
      return JSON.parse(raw);
    } catch {
      return raw.replace(/^"|"$/g, "");
    }
  }
  return raw.replace(/^'|'$/g, "");
}

/** Reject content that is not a task template before it reaches the disk. */
export function validateTemplateMarkdown(content) {
  const text = String(content ?? "");
  if (!text.trim()) return "内容为空";
  if (!/^---\s*\n/.test(text)) return "缺少 frontmatter（开头应是 ---）";
  if (!/\n---\s*\n/.test(text)) return "frontmatter 没有结束标记 ---";
  if (!/^##\s*步骤\s*$/m.test(text)) return "缺少 ## 步骤 小节";
  if (!/```yaml[\s\S]*?steps:/m.test(text)) return "步骤块里没有 steps:";
  return "";
}

async function exists(path) {
  try {
    await access(path);
    return true;
  } catch {
    return false;
  }
}

const DEFAULT_TEMPLATES = [
  {
    id: "settings-wlan",
    name: "打开设置并连接 WLAN",
    objective: "打开设置，找到 WLAN，连接 {{wifi_name}}",
    variables: ["wifi_name"],
  },
  {
    id: "jd-price-protection",
    name: "京东价保",
    objective: "打开京东，进入我的 → 客户服务 → 价格保护，尝试一键价保",
    variables: [],
  },
  {
    id: "game-daily",
    name: "游戏日常",
    objective: "打开游戏，完成每日任务并领取奖励",
    variables: [],
  },
  {
    id: "screenshot-describe",
    name: "截图并描述页面",
    objective: "截取当前手机屏幕，并描述当前页面是什么",
    variables: [],
  },
];

export class TaskError extends Error {
  constructor(code, message, details = undefined) {
    super(message || code);
    this.name = "TaskError";
    this.code = code;
    this.details = details;
  }
}

export function renderTemplate(text, variables = {}) {
  return String(text).replace(/\{\{\s*([A-Za-z0-9_.-]+)\s*\}\}/g, (match, key) => {
    const value = variables[key];
    return value === undefined || value === null ? match : String(value);
  });
}

export function buildTaskPrompt(objective) {
  return [
    "[PHONE TASK]",
    `目标：${objective}`,
    "",
    "你是一个手机自动化操作员，通过 phone_* 工具控制一台真实手机。",
    "",
    "工作循环：",
    "1. 先用 phone_screenshot 看当前屏幕；返回的是 PNG 路径，可用 read_image 查看。",
    "2. 用 phone_find_element 找目标文字；找不到就先 phone_swipe 滚动或返回上一页。",
    "3. 找到后用 phone_click 点击中心坐标；每次点击后重新截图验证。",
    "4. 不要盲点；不要假设页面已经变化；每一步都要看结果。",
    `5. 最多 ${MAX_STEPS} 步；超过或无法继续时停止并说明卡在哪一步。`,
    "",
    "安全规则（必须遵守）：",
    "- 不输入密码、验证码、支付密码、银行卡等敏感信息。",
    "- 遇到登录、验证码、支付、风险提示、权限弹窗，立即停止并报告，等待用户处理。",
    "- 不做删除、卸载、购买、转账等破坏性或不可逆操作，除非用户在当前任务里明确要求并确认。",
    "- 只操作当前连接的这台手机。",
    "",
    "完成后：",
    "- 用一句话给出结果；说明关键步骤、当前页面、失败原因（如有）。",
    "- 如果任务无法完成，不要假装成功；明确告诉用户卡在哪里。",
  ].join("\n");
}

function publicTask(task) {
  return {
    id: task.id,
    objective: task.objective,
    rendered: task.rendered,
    variables: task.variables,
    device: task.device,
    sessionId: task.sessionId,
    status: task.status,
    mode: task.mode,
    steps: task.steps,
    lastAction: task.lastAction,
    startedAt: task.startedAt,
    finishedAt: task.finishedAt ?? null,
    error: task.error ?? null,
    tracePath: task.tracePath ?? null,
    reviewPath: task.reviewPath ?? null,
    templateId: task.templateId ?? null,
    phase: task.phase ?? null,
    guided: task.guided === true,
    guidedFull: task.guidedFull === true,
    envAtEntry: task.envAtEntry === true,
    envKnown: task.envKnown === true,
    learned: task.learned ?? null,
    total: task.total ?? null,
    runDir: task.runDir ?? null,
    proposals: task.proposals ?? null,
    templateUpdate: task.templateUpdate ?? null,
  };
}

/**
 * Legacy fallback for environments without an event subscription: the agent
 * may still expose a synchronous event array. Real hosts use the trace buffer.
 */
function countPhoneSteps(agent) {
  const events = agent?.session?.events ?? [];
  const calls = events.filter(
    (event) => event.type === "tool/call" && String(event.data?.name || "").startsWith("phone_"),
  );
  const last = calls[calls.length - 1];
  return {
    steps: calls.length,
    lastAction: last
      ? {
          name: last.data.name,
          arguments: last.data.arguments,
          at: last.time,
        }
      : null,
  };
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

/** Last executable step of a template: its action target text and expected evidence. */
export function readTemplateCompletion(markdown) {
  const text = String(markdown || "");
  const stepsBlock = text.match(/```yaml\n([\s\S]*?)```/);
  const body = stepsBlock ? stepsBlock[1] : "";
  let last = null;
  for (const line of body.split("\n")) {
    if (!/^\s+-\s*\{/.test(line)) continue;
    const step = {};
    for (const pair of line.replace(/^\s+-\s*\{/, "").replace(/\}\s*$/, "").split(",")) {
      const match = pair.match(/^\s*([A-Za-z_]+)\s*:\s*(.+?)\s*$/);
      if (!match) continue;
      let value = match[2].replace(/^["']|["']$/g, "");
      if (match[1] === "at" || match[1] === "from" || match[1] === "to") {
        const nums = match[2].match(/-?\d+(?:\.\d+)?/g);
        if (nums) value = nums.map(Number);
      }
      step[match[1]] = value;
    }
    if (step.do) last = step;
  }
  if (!last) return { lastStepText: null, expectAfter: null };
  const expectAfter = typeof last.expect_after === "string" ? last.expect_after : null;
  const lastStepText = typeof last.text === "string" ? last.text : null;
  return { lastStepText, expectAfter };
}

/** Optional frontmatter `success_expect`: text that only appears when the task really succeeded. */
export function readSuccessExpect(markdown) {
  const match = String(markdown || "").match(/^success_expect:\s*(.+)$/m);
  if (!match) return null;
  return match[1].trim().replace(/^["']|["']$/g, "") || null;
}

/** Count real phone actions; observation calls (OCR/screenshot/find) are not steps. */
export function countGuidedActions(captured) {
  const ACTIONS = new Set(["phone_click", "phone_swipe", "phone_key", "phone_text"]);
  return (captured || []).filter(
    (event) => event && event.type === "tool/call" && ACTIONS.has(String(event.data?.name || "")),
  ).length;
}

/** Did any captured tool output mention `text`? (OCR lists/find results feed this) */
export function sawTextInCaptured(captured, text) {
  const needle = String(text || "").trim();
  if (!needle) return false;
  for (const event of captured || []) {
    if (!event || event.type !== "tool/result") continue;
    const parts = event.data?.message?.content || [];
    for (const part of parts) {
      if (typeof part?.text === "string" && part.text.includes(needle)) return true;
    }
  }
  return false;
}

export function createTaskService({
  clientFactory,
  agents,
  events = null,
  config = {},
  logger = console,
  now = () => Date.now(),
  randomId = randomUUID,
  runTool = defaultRunTool,
  startTool = defaultStartTool,
  guidedApproach = null,
  guidedRun = null,
  learnApproach = null,
} = {}) {
  const stateFile = config.stateFile || DEFAULT_STATE_FILE;
  const tasks = new Map();
  const history = [];
  const schedules = new Map();
  let templates = [...DEFAULT_TEMPLATES];
  let disposed = false;

  const workspace = String(config.workspace || config.serviceCwd || process.cwd());
  /**
   * Where task agents run. A task session's cwd decides how the runtime resolves
   * relative tool paths (screenshots, files); leaving it to the host default
   * sends the task to the DSH profile directory instead of the workspace.
   */
  const agentCwd = () => String(config.taskCwd || workspace);
  const runsRoot = String(config.runsDir || join(workspace, "runs"));
  const templatesRoot = String(config.templatesDir || join(workspace, "templates"));
  /** sessionId -> { buffer, startedAt } — filled from `session/event`. */
  const traceBuffers = new Map();
  /** taskId -> finalized trace, kept so "存为模板" works right after a run. */
  const recentTraces = new Map();
  /** taskId -> { task, handle } for template executions in flight. */
  const templateRuns = new Map();
  /** sessionId -> raw events of a one-shot agent call (the template summariser). */
  const captureBuffers = new Map();
  /** Inject point for tests; production uses the model-guided navigator below. */
  const guidedApproachFn =
    typeof guidedApproach === "function"
      ? guidedApproach
      : (context) => defaultGuidedApproach(context);
  const guidedRunFn =
    typeof guidedRun === "function" ? guidedRun : (context) => defaultGuidedRun(context);
  const learnApproachFn =
    typeof learnApproach === "function" ? learnApproach : (context) => defaultLearnApproach(context);

  const canSubscribeEvents = typeof events?.on === "function";
  const unsubscribeEvents = canSubscribeEvents
      ? events.on("session/event", (session, event) => {
          const sessionId = session?.header?.id ?? session?.id;
          if (!sessionId) return;
          traceBuffers.get(sessionId)?.buffer.push(event);
          captureBuffers.get(sessionId)?.push(event);
        })
      : null;

  const unsubscribeAgentError = canSubscribeEvents
    ? events.on("agent/error", (payload) => {
        const agent = payload?.agent;
        const sessionId = agent?.id ?? agent?.session?.id;
        if (!sessionId) return;
        captureBuffers.get(sessionId)?.push({
          type: "agent/error",
          data: {
            error: { message: String(payload?.error?.message ?? payload?.error ?? "agent error") },
          },
        });
      })
    : null;

  const ready = loadState();

  async function loadState() {
    try {
      const raw = await readFile(stateFile, "utf8");
      const parsed = JSON.parse(raw);
      if (Array.isArray(parsed.history)) {
        for (const item of parsed.history.slice(0, MAX_HISTORY)) {
          if (item && item.id) history.push({ ...item, status: item.status === "running" ? "interrupted" : item.status });
        }
      }
      if (Array.isArray(parsed.templates) && parsed.templates.length > 0) {
        templates = parsed.templates;
      }
      if (Array.isArray(parsed.schedules)) {
        for (const entry of parsed.schedules) {
          if (!entry || !entry.id || !entry.at) continue;
          schedules.set(entry.id, entry);
          if (entry.at > now()) armSchedule(entry);
          else {
            schedules.delete(entry.id);
            history.unshift({
              ...entry,
              status: "missed",
              finishedAt: now(),
              error: "DSH host was closed when the schedule was due",
            });
          }
        }
        await persist();
      }
    } catch (error) {
      if (error?.code !== "ENOENT") logger.warn?.(`phone task state load failed: ${error.message}`);
    }
  }

  async function persist() {
    try {
      await mkdir(dirname(stateFile), { recursive: true });
      await writeFile(
        stateFile,
        JSON.stringify(
          {
            history: history.slice(0, MAX_HISTORY),
            templates,
            schedules: [...schedules.values()].map(({ timer, ...rest }) => rest),
          },
          null,
          2,
        ),
        "utf8",
      );
    } catch (error) {
      logger.warn?.(`phone task state persist failed: ${error.message}`);
    }
  }

  /** Start buffering a session's events for this task. */
  function bindTrace(task) {
    if (!canSubscribeEvents) return;
    if (!task.sessionId || traceBuffers.has(task.sessionId)) return;
    const entry = { taskId: task.id, buffer: createTraceBuffer(), startedAt: task.startedAt };
    traceBuffers.set(task.sessionId, entry);
    task.buffer = entry.buffer;
  }

  /** Steps and last action for the dock; prefers the live event buffer. */
  function liveStats(task) {
    if (task.buffer) {
      return { steps: task.buffer.size, lastAction: task.buffer.lastAction };
    }
    return countPhoneSteps(task.agent);
  }

  /**
   * Write `runs/panel/<taskId>/<stamp>/trace.jsonl` once a task ends, then
   * remember it so a following "存为模板" can compile it without re-reading.
   */
  /** Keep the failed summariser prompt/reply around for debugging. */
  async function dumpSummaryFailure(prompt, raw, reason) {
    const dir = join(runsRoot, "panel", ".summary-failures");
    await mkdir(dir, { recursive: true });
    const file = join(dir, `${stamp(new Date(now()))}.txt`);
    await writeFile(
      file,
      [
        `# ${reason}`,
        "",
        "## prompt",
        String(prompt || ""),
        "",
        "## raw reply",
        String(raw || "(empty)"),
        "",
      ].join("\n"),
      "utf8",
    );
    logger.warn?.(`phone summary failure detail: ${file}`);
    return file;
  }

  function lastAssistantText(events) {
    let text = "";
    let reasoning = "";
    for (const event of events) {
      if (event?.type !== "assistant/message") continue;
      const parsed = assistantMessageText(event);
      if (parsed.reasoning) reasoning = parsed.reasoning;
      if (parsed.text) text = parsed.text;
    }
    return text || reasoning;
  }

  /**
   * One-shot model call used at save time: run a throwaway agent on a prompt
   * and return its final assistant text. Calls are capped by `timeoutMs`
   * because the caller is a synchronous RPC.
   */
  async function summarizeExecution(prompt, { timeoutMs = Number(config.summaryTimeoutMs) || 90000 } = {}) {
    if (!agents || typeof agents.create !== "function") {
      throw new TaskError("AGENT_UNAVAILABLE", "DSH host agent registry is unavailable");
    }
    let handle;
    try {
      const route = await resolveAgentRoute();
      if (!route.provider) {
        throw new TaskError(
          "SUMMARY_NO_MODEL",
          "没有可用的模型路由：在插件配置里设置 provider/model（可参考 profile 的 agent-default-model）",
        );
      }
      const options = { sessionId: randomId(), meta: { cwd: agentCwd() } };
      options.agentOptions = { provider: route.provider, ...(route.model ? { model: route.model } : {}) };
      handle = await agents.create(options);
    } catch (error) {
      if (error instanceof TaskError) throw error;
      throw new TaskError(
        "SUMMARY_AGENT_FAILED",
        `总结用的 agent 创建失败：${error?.message ?? error}`,
      );
    }
    const agent = handle.agent;
    const sessionId = agent.session.id;
    const captured = [];
    captureBuffers.set(sessionId, captured);
    try {
      agent.followup({
        id: randomUUID(),
        role: "user",
        content: [{ type: "text", text: prompt }],
        source: { kind: "dsh-plugin-phone" },
      });
      const started = now();
      let text = "";
      while (now() - started < timeoutMs) {
        text = lastAssistantText(captured);
        const turnEnded = captured.some((event) => event && event.type === "turn/end");
        // A freshly created agent reports "idle" until its first turn actually
        // starts, so idle alone must not end the wait — only a reply, or a turn
        // that finished without any text.
        if (text && (agent.status === "idle" || turnEnded)) break;
        if (turnEnded && !text && now() - started > 5000) break;
        await sleep(250);
      }
      if (!text) {
        // Event capture can miss a freshly created agent's turn; the session
        // itself is the source of truth, so try it before giving up.
        try {
          const events = agent?.session?.snapshotEvents?.() || [];
          text = lastAssistantText(events);
        } catch {
          text = "";
        }
      }
      if (!text) {
        const kinds = captured.reduce((acc, event) => {
          const type = String(event?.type || "?");
          acc[type] = (acc[type] || 0) + 1;
          return acc;
        }, {});
        const detail = Object.entries(kinds)
          .map(([type, count]) => `${type}×${count}`)
          .join(" ");
        const agentError = captured
          .filter((event) => event && event.type === "agent/error")
          .map((event) => String(event.data?.error?.message || ""))
          .filter(Boolean)
          .slice(-1)[0];
        const turnEnd = captured.find((event) => event && event.type === "turn/end");
        const reason = turnEnd?.data?.reason;
        const reasonText = reason
          ? `；turn 结束：${typeof reason === "string" ? reason : reason.kind || JSON.stringify(reason)}`
          : "";
        const errorText = agentError ? `；错误：${agentError}` : "";
        await dumpSummaryFailure(
          prompt,
          "",
          `模型没有返回任何文本（事件：${detail || "无"}${reasonText}${errorText}）`,
        ).catch(() => {});
        throw new TaskError(
          "SUMMARY_EMPTY",
          `模型没有返回文本（收到事件：${detail || "无"}${reasonText}${errorText}）`,
        );
      }
      return text;
    } finally {
      captureBuffers.delete(sessionId);
      try {
        await handle.dispose?.();
      } catch (error) {
        logger.warn?.(`phone summarizer dispose failed: ${error?.message ?? error}`);
      }
    }
  }

  async function finalizeTaskTrace(task) {
    try {
      const entry = task.sessionId ? traceBuffers.get(task.sessionId) : null;
      const finishedAt = task.finishedAt || now();
      // Final outcome comes from the task lifecycle (did the agent finish?), not
      // from "were there any failed tool calls" — intermediate failures are
      // recoverable and are exactly what the review should learn from.
      const finalOk = task.status === "done";
      const { steps, done, reasonings = [] } = entry?.buffer
        ? entry.buffer.records({ startedAt: task.startedAt, finishedAt, finalOk })
        : { steps: [], done: { t: "done", ok: false, steps: 0, failed: 0, ms: 0, turns: 0 } };
      const header = {
        id: task.id,
        v: 1,
        ts: task.traceStamp,
        mode: "panel",
        objective: task.rendered || task.objective,
        sessionId: task.sessionId,
        device: task.device,
        screen: task.screen || null,
        status: task.status,
      };
      await mkdir(task.traceDir, { recursive: true });
      await writeFile(task.tracePath, serializeTrace(buildTrace(header, { steps, done })), "utf8");
      await writeFile(
        join(task.traceDir, "task.json"),
        JSON.stringify({ ...publicTask(task), trace: "trace.jsonl" }, null, 2),
        "utf8",
      );
      if (reasonings.length > 0) {
        // Full reasoning next to the trace: the summary needs the whole thing,
        // while trace.jsonl stays one readable line per step.
        await writeFile(
          join(task.traceDir, "reasoning.jsonl"),
          reasonings.map((item) => JSON.stringify(item)).join("\n") + "\n",
          "utf8",
        );
      }
      recentTraces.set(task.id, {
        taskId: task.id,
        sessionId: task.sessionId,
        device: task.device,
        objective: task.rendered || task.objective,
        screen: task.screen || null,
        startedAt: task.startedAt,
        finishedAt,
        done,
        steps,
        reasonings,
        tracePath: task.tracePath,
      });
      while (recentTraces.size > MAX_RECENT_TRACES) {
        recentTraces.delete(recentTraces.keys().next().value);
      }
      await persist();
      logger.info?.(`phone task trace: ${task.tracePath} (${steps.length} steps)`);
      return { steps: steps.length, done };
    } finally {
      if (task.sessionId) traceBuffers.delete(task.sessionId);
    }
  }

  function clientFor(device) {
    const name = device || config.defaultDevice || "default";
    const entry = config.devices?.[name];
    if (entry) {
      return clientFactory({ device: name, baseUrl: entry.baseUrl, token: entry.token, tokenFile: entry.tokenFile });
    }
    return clientFactory({ device: name, baseUrl: config.baseUrl, token: config.token, tokenFile: config.tokenFile });
  }

  function serviceCommandHint() {
    const command = Array.isArray(config.serviceCommand) ? config.serviceCommand.map(String) : [];
    return command.length > 0 ? command.join(" ") : "";
  }

  async function checkHealth(device) {
    const client = clientFor(device);
    try {
      await client.health();
      return client;
    } catch (error) {
      throw new TaskError(
        "PHONE_SERVICE_DOWN",
        `phone service is not reachable at ${client.baseUrl}. Start it with: phone serve --http-port 8765`,
        {
          device,
          baseUrl: client.baseUrl,
          command: serviceCommandHint(),
          cause: error?.message ?? String(error),
        },
      );
    }
  }

  /**
   * The agent is blind without the eye and useless without the hand, so a task
   * must not create an agent that is guaranteed to fail. This is intentionally
   * stricter than `/health`: it proves the hand answers and the phone screen
   * can actually be captured.
   */
  async function preflightDevice(device) {
    const client = await checkHealth(device);
    let payload = null;
    if (typeof client.status === "function") {
      // Right after DSH / the service restarts, the hand link is still being
      // re-established: one slow status call is not proof the device is gone.
      let lastError = null;
      for (let attempt = 1; attempt <= 3; attempt += 1) {
        try {
          payload = await client.status();
          lastError = null;
          break;
        } catch (error) {
          lastError = error;
          if (attempt < 3) await sleep(500);
        }
      }
      if (lastError) {
        throw new TaskError(
          "DEVICE_NOT_READY",
          `设备没有响应：确认控制手已上电，并和电脑在同一网络（重试 3 次失败：${lastError?.message ?? lastError}）`,
          { device, cause: lastError?.message ?? String(lastError) },
        );
      }
    }
    const status = payload?.status && typeof payload.status === "object" ? payload.status : null;
    if (status?.usb === false) {
      throw new TaskError(
        "DEVICE_NOT_READY",
        "控制手已连接，但手机没有识别到 USB：检查控制手与手机之间的连接",
        { device, usb: false },
      );
    }
    if (payload?.hand && payload.hand.ok === false) {
      throw new TaskError(
        "DEVICE_NOT_READY",
        `控制手没有响应：${payload.hand.error || "检查控制手的电源和 WiFi"}`,
        { device, hand: payload.hand },
      );
    }
    if (status?.state === "FAULT") {
      throw new TaskError(
        "DEVICE_NOT_READY",
        "控制手处于异常状态：重新插拔或重启设备后再试",
        { device, state: status.state, lastError: status.last_err || "" },
      );
    }
    if (config.preflightScreen !== false && typeof client.screenshot === "function") {
      try {
        await client.screenshot({ maxWidth: 180 });
      } catch (error) {
        throw new TaskError(
          "EYE_UNAVAILABLE",
          "读不到手机画面：解锁手机，确认无线调试（adb）已连接",
          { device, cause: error?.message ?? String(error) },
        );
      }
    }
    return { client, payload };
  }

  async function preflight(payload = {}) {
    await ready;
    if (disposed) throw new TaskError("DISPOSED", "task service is disposed");
    const device = String(payload.device || config.defaultDevice || "default");
    await preflightDevice(device);
    return { ok: true, device };
  }

  function waitForAgent(sessionId, timeoutMs = 15000) {
    const started = now();
    return (async () => {
      while (!disposed && now() - started < timeoutMs) {
        const agent = agents?.get?.(sessionId);
        if (agent) return agent;
        await sleep(100);
      }
      return undefined;
    })();
  }

  let cachedRoute = null;

  function profilePatchPath() {
    const home = process.env.DSH_HOME || join(homedir(), ".dsh");
    const profile = process.env.DSH_PROFILE || "desktop";
    return join(home, "profiles", profile, "cordis.patch.yml");
  }

  /**
   * Host-created agents have no UI to pick a model. Without an explicit route
   * their turn ends immediately without calling the model — which is exactly
   * what an empty summariser reply looks like. Take the route from plugin
   * config first, then the profile's `agent-default-model` entry.
   */
  async function resolveAgentRoute() {
    if (cachedRoute) return cachedRoute;
    const route = {};
    if (config.summaryProvider) route.provider = String(config.summaryProvider);
    else if (config.provider) route.provider = String(config.provider);
    if (config.summaryModel) route.model = String(config.summaryModel);
    else if (config.model) route.model = String(config.model);
    if (!route.provider) {
      try {
        const text = await readFile(profilePatchPath(), "utf8");
        let inEntry = false;
        for (const line of text.split("\n")) {
          if (/^\s*-\s*id:\s*agent-default-model\s*$/.test(line)) {
            inEntry = true;
            continue;
          }
          if (inEntry && /^\s*-\s*id:/.test(line)) break;
          if (!inEntry) continue;
          const provider = line.match(/^\s*provider:\s*(\S+)/);
          if (provider) route.provider = provider[1].replace(/^["']|["']$/g, "");
          const model = line.match(/^\s*model:\s*(\S+)/);
          if (model) route.model = model[1].replace(/^["']|["']$/g, "");
        }
      } catch (error) {
        logger.warn?.(
          `phone agent route: cannot read ${profilePatchPath()}: ${error?.message ?? error}`,
        );
      }
    }
    cachedRoute = route;
    if (route.provider) {
      logger.info?.(`phone agent route: ${route.provider}/${route.model || "(default)"}`);
    }
    return route;
  }

  async function createAgent(task) {
    if (!agents || typeof agents.create !== "function") {
      throw new TaskError("AGENT_UNAVAILABLE", "DSH host agent registry is unavailable");
    }
    const options = { sessionId: randomId(), meta: { cwd: agentCwd() } };
    const route = await resolveAgentRoute();
    if (route.provider) {
      options.agentOptions = { provider: route.provider, ...(route.model ? { model: route.model } : {}) };
    }
    let handle;
    try {
      handle = await agents.create(options);
    } catch (error) {
      throw new TaskError("AGENT_CREATE_FAILED", `failed to create task agent: ${error?.message ?? error}`);
    }
    return handle.agent;
  }

  function promptAgent(agent, objective) {
    const message = Object.freeze({
      id: randomUUID(),
      role: "user",
      content: Object.freeze([{ type: "text", text: buildTaskPrompt(objective) }]),
      source: Object.freeze({ kind: "dsh-plugin-phone" }),
    });
    agent.followup(message);
  }

  function monitor(task) {
    const started = now();
    let sawRunning = false;
    task.timer = setInterval(() => {
      if (disposed || task.status !== "running") return;
      const agent = task.agent;
      if (!agent) return;
      // session.events is gone in this DSH version, so the live buffer is the
      // only proof the agent actually worked. Without it a fast task (start and
      // end between two 500ms polls) was reported as AGENT_NOT_STARTED.
      const legacyEvents = agent.session?.events ?? [];
      const hasActivity = task.buffer
        ? task.buffer.size > 0 || task.buffer.turns > 0
        : legacyEvents.some((event) => event.type === "turn/end");
      if (agent.status === "running" || hasActivity) sawRunning = true;
      const { steps, lastAction } = liveStats(task);
      task.steps = steps;
      task.lastAction = lastAction;
      if (sawRunning && agent.status === "idle") {
        finish(task, "done");
      } else if (!sawRunning && now() - started > 30000) {
        finish(task, "error", "AGENT_NOT_STARTED", "agent did not start the task within 30s");
      }
    }, 500);
    task.timer.unref?.();
    const { steps, lastAction } = liveStats(task);
    task.steps = steps;
    task.lastAction = lastAction;
  }

  async function attach(task) {
    const agent = await waitForAgent(task.sessionId);
    if (!agent) {
      finish(task, "error", "AGENT_NOT_READY", "task session was created but no live agent appeared");
      return;
    }
    if (["stopped", "done", "error"].includes(task.status)) return;
    task.agent = agent;
    task.status = "running";
    monitor(task);
  }

  function finish(task, status, code = null, message = null) {
    if (task.timer) clearInterval(task.timer);
    task.timer = null;
    task.status = status;
    task.finishedAt = now();
    task.error = code ? { code, message } : null;
    if (task.buffer) {
      task.traceStamp = stamp(new Date(task.finishedAt));
      task.traceDir = join(runsRoot, "panel", task.id, task.traceStamp);
      task.tracePath = join(task.traceDir, "trace.jsonl");
      const { steps, lastAction } = liveStats(task);
      task.steps = steps;
      task.lastAction = lastAction;
    }
    tasks.delete(task.id);
    history.unshift(publicTask(task));
    history.splice(MAX_HISTORY);
    persist();
    if (task.buffer) {
      finalizeTaskTrace(task).catch((error) =>
        logger.warn?.(`phone task trace write failed: ${error?.message ?? error}`),
      );
    }
  }

  async function start(payload = {}) {
    await ready;
    if (disposed) throw new TaskError("DISPOSED", "task service is disposed");
    const objective = String(payload.objective || "").trim();
    if (!objective) throw new TaskError("BAD_REQUEST", "objective is required");
    const variables = payload.variables && typeof payload.variables === "object" ? payload.variables : {};
    const rendered = renderTemplate(objective, variables);
    const device = String(payload.device || config.defaultDevice || "default");
    const preflightResult = await preflightDevice(device);
    const screen = Array.isArray(preflightResult?.payload?.screen)
      ? preflightResult.payload.screen.map(Number).slice(0, 2)
      : null;

    const sessionId = payload.sessionId ? String(payload.sessionId) : null;
    const task = {
      id: randomId(),
      objective,
      rendered,
      variables,
      device,
      sessionId,
      status: sessionId ? "starting" : "running",
      mode: sessionId ? "attach" : "autonomous",
      steps: 0,
      lastAction: null,
      startedAt: now(),
      finishedAt: null,
      error: null,
      agent: null,
      timer: null,
      screen,
      buffer: null,
      traceDir: null,
      tracePath: null,
      traceStamp: null,
    };
    tasks.set(task.id, task);
    // Attach mode already knows the session id: buffer events before the
    // client sends the first prompt so step 1 is not lost.
    if (sessionId) bindTrace(task);

    if (sessionId) {
      attach(task).catch((error) => finish(task, "error", "ATTACH_FAILED", error?.message ?? String(error)));
    } else {
      try {
        const agent = await createAgent(task);
        task.agent = agent;
        task.sessionId = agent.session.id;
        bindTrace(task);
        task.status = "running";
        promptAgent(agent, task.rendered);
        monitor(task);
      } catch (error) {
        finish(task, "error", error?.code || "START_FAILED", error?.message ?? String(error));
        throw error;
      }
    }
    return publicTask(task);
  }

  function status(taskId) {
    if (taskId) {
      const templateRun = templateRuns.get(String(taskId));
      if (templateRun) return publicTask(templateRun.task);
      const active = tasks.get(String(taskId));
      if (active) return publicTask(active);
      return history.find((item) => item.id === String(taskId)) ?? null;
    }
    return {
      active: [
        ...[...templateRuns.values()].map((entry) => publicTask(entry.task)),
        ...[...tasks.values()].map(publicTask),
      ],
      history: history.slice(0, 20),
    };
  }

  async function stop(taskId) {
    const templateRun = templateRuns.get(String(taskId || ""));
    if (templateRun) {
      templateRun.cancelled = true;
      try {
        templateRun.handle?.kill?.();
      } catch (error) {
        logger.warn?.(`phone template kill failed: ${error?.message ?? error}`);
      }
      templateRun.task.status = "stopped";
      // The phase chain finalises (history/persist) once it notices the cancel.
      return publicTask(templateRun.task);
    }
    const task = tasks.get(String(taskId || ""));
    if (!task) {
      const existing = history.find((item) => item.id === String(taskId || ""));
      return existing ?? null;
    }
    try {
      task.agent?.cancel?.({ kind: "user" });
    } catch (error) {
      logger.warn?.(`phone task cancel failed: ${error.message}`);
    }
    finish(task, "stopped");
    return publicTask(task);
  }

  function listTemplates() {
    return templates;
  }

  /**
   * Compile a finished task run into `templates/<id>.md` (normalized coordinates,
   * readable steps) and seed `runs/<id>/<stamp>/trace.jsonl` so the reviewer has
   * a first sample. The markdown is a draft: success criteria are not inferred.
   */
  /**
   * Rebuild a trace entry from disk so old history rows can still become
   * templates after a host restart (recentTraces is memory-only).
   */
  async function traceEntryFromDisk(taskId) {
    const record = history.find((item) => String(item.id) === String(taskId));
    if (!record || !record.tracePath) return null;
    let text;
    try {
      text = await readFile(record.tracePath, "utf8");
    } catch {
      return null;
    }
    const records = [];
    for (const line of text.split("\n")) {
      if (!line.trim()) continue;
      try {
        records.push(JSON.parse(line));
      } catch {
        // skip a torn line
      }
    }
    const header = records.find((item) => item.t === "run") || {};
    const steps = records.filter((item) => item.t === "step");
    if (steps.length === 0) return null;
    const done = { ...(records.find((item) => item.t === "done") || {}) };
    // Older traces wrote `done.ok = no failed steps`. The task status in the
    // header is the real final outcome, so migrate on read.
    if (typeof header.status === "string") {
      done.ok = header.status === "done" && steps.length > 0;
    }
    return {
      taskId: String(taskId),
      sessionId: header.sessionId || record.sessionId || null,
      device: header.device || record.device || null,
      objective: header.objective || record.objective || "",
      screen: Array.isArray(header.screen) ? header.screen : null,
      startedAt: Number(header.startedAt) || record.startedAt || 0,
      finishedAt: Number(header.finishedAt) || record.finishedAt || 0,
      done,
      steps,
      reasonings: [],
      tracePath: record.tracePath,
    };
  }

  async function exportTemplate({
    taskId,
    name,
    objective,
    id = null,
    force = false,
    mechanical = false,
  }) {
    const key = String(taskId || "");
    let entry = recentTraces.get(key);
    if (!entry) entry = await traceEntryFromDisk(key);
    if (!entry) {
      throw new TaskError(
        "TRACE_NOT_FOUND",
        "这次任务没有可用轨迹（找不到 trace.jsonl，或任务还没结束）",
      );
    }
    const screen =
      Array.isArray(entry.screen) && entry.screen.length >= 2 ? entry.screen.map(Number) : null;
    if (!screen) throw new TaskError("TRACE_NO_SCREEN", "轨迹里没有屏幕尺寸，坐标无法归一化");

    // Templates are lessons from a run that worked. A trace with failed steps is
    // not a lesson; it would bake broken coordinates into the library.
    const failedCount = Number(entry.done?.failed ?? 0);
    if (entry.done?.ok === false && !force) {
      throw new TaskError(
        "TRACE_HAS_FAILURES",
        `这次任务最终没有成功（${failedCount ? `${failedCount} 步过程失败，` : ""}共 ${entry.steps.length} 步），` +
          "先修好再存模板；确认要保留草稿可以对任务记录强制生成",
      );
    }

    const today = new Date(now()).toISOString().slice(0, 10);
    const baseId = id || templateId(name, key);
    let finalId = baseId;
    let counter = 2;
    while (await exists(join(templatesRoot, `${finalId}.md`))) {
      finalId = `${baseId}-${counter}`;
      counter += 1;
    }

    // Preferred path: let the model summarise the run (intent + evidence), and
    // render markdown from that plan. Anything that goes wrong falls back to the
    // mechanical coordinate converter below.
    let built = null;
    let source = "mechanical";
    let summaryError = null;
    const summaryWarnings = [];
    if (config.summarizeTemplates !== false) {
      try {
        const digest = buildDigest(entry);
        const raw = await summarizeExecution(buildSummaryPrompt(digest));
        const parsed = parseSummaryResponse(raw);
        if (!parsed) {
          await dumpSummaryFailure(
            buildSummaryPrompt(digest),
            raw,
            "模型输出不是合法 JSON（缺少 steps 数组）",
          ).catch(() => {});
          throw new TaskError(
            "SUMMARY_INVALID",
            `模型输出不是合法 JSON：${String(raw).slice(0, 160).replace(/\s+/g, " ")}`,
          );
        }
        const plan = normalizeSummary(parsed);
        const rendered = renderSummaryTemplate({
          id: finalId,
          name,
          objective,
          taskId: key,
          sessionId: entry.sessionId,
          device: entry.device,
          screen,
          plan,
          originalSteps: entry.steps,
          durationMs: entry.done?.ms || 0,
          today,
        });
        built = { ...rendered, trace: summaryTrace(plan, entry.steps) };
        source = "summary";
        summaryWarnings.push(...plan.warnings);
      } catch (error) {
        summaryError = error?.message ?? String(error);
        logger.warn?.(`phone template summary failed, falling back: ${summaryError}`);
      }
    }

    if (!built) {
      // A template's whole value is the summarised intent. Silently downgrading
      // to the mechanical converter hides failures and produces blind scripts,
      // so that path is opt-in only (config / explicit flag), never a fallback.
      const mechanicalAllowed =
        config.summarizeTemplates === false ||
        config.allowMechanicalTemplates === true ||
        mechanical === true;
      if (!mechanicalAllowed) {
        throw new TaskError(
          "SUMMARY_FAILED",
          `没有生成模板：模型总结失败（${summaryError || "未知原因"}）。` +
            "没有写入任何文件；可以直接重试，或把配置 summarizeTemplates 设为 false / 传 mechanical=true 显式生成机械草稿。",
        );
      }
      logger.warn?.("phone template: using mechanical converter (explicitly allowed)");
      built = buildTemplateMarkdown({
        id: finalId,
        name,
        objective,
        taskId: key,
        sessionId: entry.sessionId,
        device: entry.device,
        screen,
        traceSteps: entry.steps,
        durationMs: entry.done?.ms || 0,
        taskOk: entry.done?.ok !== false,
        today,
      });
    }

    await mkdir(templatesRoot, { recursive: true });
    const file = join(templatesRoot, `${finalId}.md`);
    await writeFile(file, built.markdown, "utf8");

    const traceStamp = stamp(new Date(now()));
    const traceDir = join(runsRoot, finalId, traceStamp);
    await mkdir(traceDir, { recursive: true });
    const header = {
      id: finalId,
      v: 1,
      ts: traceStamp,
      mode: "generated",
      sourceTask: key,
      screen,
    };
    const records = [
      { t: "run", ...header },
      ...built.trace.records,
      { ...built.trace.done, ms: entry.done?.ms || 0 },
    ];
    await writeFile(join(traceDir, "trace.jsonl"), serializeTrace(records), "utf8");

    const warnings = [...(built.warnings || []), ...summaryWarnings];
    return {
      id: finalId,
      file,
      steps: built.steps.length,
      source,
      warnings,
      tracePath: join(traceDir, "trace.jsonl"),
    };
  }

  function templateToolTarget(name) {
    const toolsDir = resolveToolsDir(workspace, config);
    const tool = join(toolsDir, name);
    if (!existsSync(tool)) {
      throw new TaskError(
        "TOOL_MISSING",
        `找不到模板工具 ${tool}；插件需要工作区里的 tools/ 目录`,
      );
    }
    return {
      python: resolvePython(workspace, config),
      tool,
      // A configured workspace may not exist yet; spawning with a bad cwd
      // reports ENOENT and looks like a missing interpreter.
      cwd: existsSync(workspace) ? workspace : process.cwd(),
    };
  }

  async function runTemplateTool(name, args, { allowFailure = false } = {}) {
    const target = templateToolTarget(name);
    let raw;
    try {
      raw = await runTool({ ...target, args, timeoutMs: Number(config.templateToolTimeoutMs) || 30000 });
    } catch (error) {
      throw new TaskError("TEMPLATE_TOOL_FAILED", `模板工具运行失败：${error?.message ?? error}`);
    }
    if (raw.code !== 0) {
      // Some tools exit non-zero but still print a useful JSON diagnosis
      // (adb_reconnect reports "could not reconnect" that way).
      if (allowFailure) {
        try {
          return parseToolJson(raw.stdout);
        } catch {
          // fall through to the error below
        }
      }
      const detail = String(raw.stderr || raw.stdout || "")
        .trim()
        .split("\n")
        .slice(-3)
        .join(" ");
      throw new TaskError("TEMPLATE_TOOL_FAILED", detail || `模板工具退出码 ${raw.code}`);
    }
    try {
      return parseToolJson(raw.stdout);
    } catch (error) {
      throw new TaskError("TEMPLATE_TOOL_BAD_OUTPUT", error?.message ?? String(error));
    }
  }

  /** baseUrl/token of the service that owns the device connection. */
  function deviceEndpoint(device) {
    const name = device || config.defaultDevice || "default";
    const entry = config.devices?.[name] || {};
    return {
      baseUrl: String(entry.baseUrl || config.baseUrl || "http://127.0.0.1:8765"),
      token: String(entry.token || config.token || ""),
    };
  }

  /** Device flags for `tpl_run.py`, inferred from the service command if needed. */
  function templateDeviceArgs(payload = {}) {
    if (payload.mock) return ["--mock"];
    if (payload.port) return ["--port", String(payload.port)];
    if (payload.tcp) return ["--tcp", String(payload.tcp)];
    if (Array.isArray(config.templateDeviceArgs)) {
      return config.templateDeviceArgs.map(String);
    }
    const command = Array.isArray(config.serviceCommand) ? config.serviceCommand.map(String) : [];
    const index = command.findIndex((item) => item === "--tcp" || item === "--port");
    if (index >= 0 && command[index + 1]) return [command[index], command[index + 1]];
    return [];
  }

  /** Turn the runner's report into a sentence a person can act on. */
  async function describeTemplateFailure(task) {
    if (!task.runDir) return null;
    let text;
    try {
      text = await readFile(join(task.runDir, "report.jsonl"), "utf8");
    } catch {
      return null;
    }
    const steps = [];
    for (const line of text.split("\n")) {
      if (!line.trim()) continue;
      try {
        const entry = JSON.parse(line);
        if (entry && entry.type === "step") steps.push(entry);
      } catch {
        // ignore a torn line
      }
    }
    const failed = steps.find((step) => step.status !== "ok");
    if (!failed) return null;
    const params = failed.params || {};
    const label =
      typeof params.text === "string"
        ? `「${params.text}」`
        : params.x !== undefined
          ? `(${params.x}, ${params.y})`
          : "";
    const stepId = typeof params.id === "string" ? params.id : "";
    if (stepId === "entry") {
      return (
        `未到达起始页面：当前屏幕上没有${label || "模板要求的锚点"}。` +
        "先把手机打开到该页面再执行模板（模板会先确认起始页，再跑坐标步骤）"
      );
    }
    if (stepId.startsWith("entry-")) {
      return (
        `到达起始页面失败：${failed.error || "导航步骤没有成功"}。` +
        "检查手机是否已解锁、目标 App 是否可用"
      );
    }
    const firstStep = failed.index === 1 && failed.action === "assert_text";
    const hint = firstStep
      ? "；起始页面可能不一致——先打开目标 App 到起始页再执行"
      : "";
    return `第 ${failed.index} 步 ${failed.action}${label ? ` ${label}` : ""} 失败：${
      failed.error || "未知错误"
    }${hint}`;
  }

  function readEntryExpect(markdown) {
    const block = markdown.match(/^entry:\s*\n((?:[ \t]+.*(?:\n|$))*)/m);
    if (block) {
      const match = block[1].match(/^\s+expect:\s*(.+)$/m);
      if (match) return match[1].trim().replace(/^["']|["']$/g, "");
    }
    const flow = markdown.match(/^entry:\s*\{(.*?)\}/m);
    if (flow) {
      const match = flow[1].match(/expect:\s*([^,}]+)/);
      if (match) return match[1].trim().replace(/^["']|["']$/g, "");
    }
    return null;
  }

  /** Parse `entry.approach` back out of the markdown (used as a guided hint). */
  function readEntryApproach(markdown) {
    const block = markdown.match(/^entry:\s*\n((?:[ \t]+.*(?:\n|$))*)/m);
    if (!block) return [];
    const steps = [];
    let inApproach = false;
    for (const line of block[1].split("\n")) {
      if (/^\s+approach:\s*$/.test(line)) {
        inApproach = true;
        continue;
      }
      if (!inApproach) continue;
      const item = line.match(/^\s+-\s*\{(.*)\}\s*$/);
      if (!item) {
        if (/^\s+\S/.test(line)) inApproach = false;
        continue;
      }
      const step = {};
      for (const pair of item[1].split(",")) {
        const match = pair.match(/^\s*([A-Za-z_]+)\s*:\s*(.+?)\s*$/);
        if (!match) continue;
        let value = match[2].replace(/^["']|["']$/g, "");
        if (match[1] === "timeout" || match[1] === "ms") value = Number(value);
        step[match[1]] = value;
      }
      if (step.do) steps.push(step);
    }
    return steps;
  }

  function readLastExpectAfter(markdown) {
    const matches = [...String(markdown || "").matchAll(/expect_after:\s*("([^"]+)"|([^,\}\n]+))/g)];
    if (matches.length === 0) return null;
    const last = matches[matches.length - 1];
    return String(last[2] ?? last[3] ?? "").trim().replace(/^["']|["']$/g, "") || null;
  }

  function templateBaseArgs(file, payload, device) {
    const args = [file, "--events-json"];
    // Execute through `phone serve`: the service owns the hand, so a second
    // CLI process would fight it for the same TCP/serial connection.
    if (payload.viaService !== false && !payload.port && !payload.tcp && !payload.mock) {
      const endpoint = deviceEndpoint(device);
      args.push("--via-service", endpoint.baseUrl);
      if (endpoint.token) args.push("--token", endpoint.token);
    } else {
      args.push(...templateDeviceArgs(payload));
    }
    return args;
  }

  function applyTemplateEvent(task, event) {
    switch (event.event) {
      case "start":
        task.status = "running";
        task.total = Number(event.total) || 0;
        task.runDir = event.runDir || null;
        break;
      case "step":
        task.steps = Number(event.index) || task.steps;
        task.total = Number(event.total) || task.total;
        task.lastAction = {
          name: event.action,
          ok: event.ok !== false,
          ms: event.ms,
          at: now(),
        };
        break;
      case "review":
        task.reviewPath = event.reviewPath || null;
        task.proposals = Number(event.proposals) || 0;
        break;
      case "applied":
        task.templateUpdate = {
          version: event.version,
          newVersion: event.newVersion,
          backup: event.backup || null,
          applied: Array.isArray(event.applied) ? event.applied : [],
        };
        break;
      case "done":
        task.runDir = event.runDir || task.runDir;
        task.tracePath = event.tracePath || null;
        task.reviewPath = event.reviewPath || task.reviewPath;
        if (Number.isFinite(Number(event.steps))) {
          task.steps = Number(event.steps);
          task.total = task.total || Number(event.steps);
        }
        break;
      default:
        break;
    }
  }

  /** Run one tpl_run phase (entry / main) and return its exit state. */
  async function runTemplatePhase(entry, flags, suffix) {
    const { task, context } = entry;
    const target = templateToolTarget("tpl_run.py");
    const runDir = join(runsRoot, task.templateId, `${stamp(new Date(now()))}-${suffix}`);
    const args = [
      ...templateBaseArgs(context.file, context.payload, context.device),
      ...flags,
      "--run-dir",
      runDir,
    ];
    if (context.payload.stopBefore) args.push("--stop-before", String(context.payload.stopBefore));
    if (config.templateRunNoReview) args.push("--no-review");
    task.phase = suffix;
    let doneEvent = null;
    const handle = startTool({
      ...target,
      args,
      timeoutMs: Number(config.templateRunTimeoutMs) || 900000,
      onLine: (line) => {
        let event;
        try {
          event = JSON.parse(line);
        } catch {
          return;
        }
        if (!event || typeof event !== "object") return;
        if (event.event === "done") doneEvent = event;
        applyTemplateEvent(task, event);
      },
    });
    entry.handle = handle;
    const result = await handle.done;
    entry.handle = null;
    return { code: result.code, stderr: result.stderr, done: doneEvent, runDir };
  }

  /** Turn a guided agent's tool calls into coordinate-free approach steps. */
  function guidedStepsFromEvents(events) {
    const steps = [];
    let pendingFind = null;
    for (const event of events) {
      if (!event || event.type !== "tool/call") continue;
      const name = String(event.data?.name || "");
      const args = parseArguments(event.data?.arguments);
      if (name === "phone_find_element") {
        pendingFind = String(args.keyword || "").trim() || null;
      } else if (name === "phone_click" && pendingFind) {
        steps.push({ do: "tap_text", text: pendingFind });
        pendingFind = null;
      } else if (name === "phone_key") {
        const code = String(args.code || "").trim().toUpperCase();
        if (code) steps.push({ do: "key", code });
      } else if (name === "phone_text") {
        const value = String(args.value || "").trim();
        if (value) steps.push({ do: "text", value });
      }
    }
    if (pendingFind) steps.push({ do: "expect", text: pendingFind, timeout: 3 });
    // Approach steps cannot carry coordinates; keep only semantic actions.
    return steps.filter((step) => step.do !== "expect");
  }

  /** Persist the guided path via the workspace tool (backup + version bump). */
  async function defaultLearnApproach({ file, steps, taskId }) {
    const dir = join(runsRoot, "panel", ".learned");
    await mkdir(dir, { recursive: true });
    const stepsFile = join(dir, `${taskId}.json`);
    await writeFile(stepsFile, JSON.stringify({ steps }, null, 2), "utf8");
    return runTemplateTool("tpl_learn_approach.py", [
      file,
      "--steps-file",
      stepsFile,
      "--json",
    ]);
  }

  /**
   * Model-guided navigation: when the recorded approach cannot reach the start
   * page, hand the current screen and the target anchor to an agent that drives
   * the phone tools itself, then verify the anchor is really there.
   */
  /** Spawn a host agent and wait for its turn to end (with step caps). */
  async function runGuidedSession({ device, prompt, maxSteps, timeoutMs, shouldStop, successEvidence }) {
    if (!agents || typeof agents.create !== "function") {
      return { ok: false, message: "没有可用的 agent，无法引导执行" };
    }
    const route = await resolveAgentRoute();
    if (!route.provider) {
      return { ok: false, message: "没有可用的模型路由，无法引导执行" };
    }
    let handle;
    try {
      handle = await agents.create({
        sessionId: randomId(),
        meta: { cwd: agentCwd() },
        agentOptions: { provider: route.provider, ...(route.model ? { model: route.model } : {}) },
      });
    } catch (error) {
      return { ok: false, message: `引导 agent 创建失败：${error?.message ?? error}` };
    }
    const agent = handle.agent;
    const sessionId = agent.session.id;
    const captured = [];
    captureBuffers.set(sessionId, captured);
    const limit = Number(maxSteps) || Number(config.guidedMaxSteps) || 15;
    try {
      agent.followup({
        id: randomUUID(),
        role: "user",
        content: [{ type: "text", text: prompt }],
        source: { kind: "dsh-plugin-phone" },
      });
      const deadline = now() + (Number(timeoutMs) || Number(config.guidedTimeoutMs) || 150000);
      const observationLimit = Number(config.guidedMaxObservations) || 120;
      while (now() < deadline) {
        if (typeof shouldStop === "function" && shouldStop()) {
          try {
            agent.cancel?.({ kind: "user", reason: "guided-cancelled" });
          } catch {
            // best effort
          }
          return { ok: false, message: "已停止", captured };
        }
        const ended = captured.some((event) => event && event.type === "turn/end");
        if (ended && agent.status === "idle") break;
        const phoneCalls = captured.filter(
          (event) =>
            event && event.type === "tool/call" && String(event.data?.name || "").startsWith("phone_"),
        );
        const actionCalls = countGuidedActions(captured);
        const successSeen = successEvidence && sawTextInCaptured(captured, successEvidence);
        if (actionCalls > limit) {
          // The task may already have succeeded — never kill a run that has the
          // success evidence; stop counting and verify instead.
          if (!successSeen) {
            try {
              agent.cancel?.({ kind: "user", reason: "guided-abort" });
            } catch {
              // best effort
            }
            return { ok: false, message: `自动执行超过 ${limit} 个操作步，已中止`, captured };
          }
          break;
        }
        if (phoneCalls.length > observationLimit) {
          try {
            agent.cancel?.({ kind: "user", reason: "guided-abort" });
          } catch {
            // best effort
          }
          return {
            ok: false,
            message: `自动执行产生了 ${phoneCalls.length} 次屏幕读取（上限 ${observationLimit}），已中止`,
            captured,
          };
        }
        const recent = phoneCalls.slice(-3).map((event) => String(event.data?.name || ""));
        if (recent.length === 3 && recent.every((name) => name === "phone_find_element")) {
          try {
            agent.cancel?.({ kind: "user", reason: "guided-abort" });
          } catch {
            // best effort
          }
          return {
            ok: false,
            message: "自动执行在反复查找文字，已中止；应先 phone_ocr_texts/截图看清页面",
            captured,
          };
        }
        await sleep(300);
      }
      return { ok: true, captured };
    } finally {
      captureBuffers.delete(sessionId);
      try {
        await handle.dispose?.();
      } catch (error) {
        logger.warn?.(`phone guided agent dispose failed: ${error?.message ?? error}`);
      }
    }
  }

  /** Quick environment probe: is the anchor already on screen? (fast retries) */
  async function checkAnchor(device, text, { attempts = 2, delayMs = 400 } = {}) {
    const client = clientFor(device);
    for (let attempt = 0; attempt < attempts; attempt += 1) {
      try {
        const found = await client.find({ keyword: text, minScore: 0.6, mode: "contains" });
        if (found && found.found !== false) return true;
      } catch {
        // retry below
      }
      if (attempt < attempts - 1) await sleep(delayMs);
    }
    return false;
  }

  /** Verify a text appears on screen, retrying through stale/mid-transition frames. */
  async function verifyScreenText(device, text) {
    const client = clientFor(device);
    for (let attempt = 0; attempt < 3; attempt += 1) {
      try {
        const found = await client.find({ keyword: text, minScore: 0.6, mode: "contains" });
        if (found && found.found !== false) return true;
      } catch {
        // retry below
      }
      if (attempt < 2) await sleep(1000);
    }
    return false;
  }

  async function takeGuidedScreenshot(device) {
    try {
      const shot = await clientFor(device).screenshot({ maxWidth: 720 });
      return shot?.path || shot?.saved_path || "";
    } catch {
      return "";
    }
  }

  async function defaultGuidedApproach({ device, entryExpect, file, hints = [], shouldStop }) {
    if (config.guidedApproach === false) {
      return { ok: false, message: "引导导航已关闭（guidedApproach: false）" };
    }
    const screenshot = await takeGuidedScreenshot(device);
    const prompt = [
      "[引导导航]",
      `目标：把手机操作到起始页面，使屏幕上出现文字「${entryExpect}」。`,
      screenshot ? `当前屏幕截图：${screenshot}（可用 read_image 查看）` : "",
      "",
      "规则：",
      "1. 先用 phone_ocr_texts 或 phone_screenshot + read_image 一次看清整个屏幕；不要用 phone_find_element 枚举文字。",
      "2. 你可以用 phone_click / phone_swipe / phone_key 操作手机；图标可能在文件夹、第二屏或需要搜索。",
      "3. 每次点击后重新看一次屏幕确认；不要连续盲点。",
      "4. 不做支付、删除、登录等不可逆操作；遇到验证码就停下。",
      `5. 当屏幕上出现「${entryExpect}」后立即停止，回复「已到达」。最多 12 步，超过会被强制中止。`,
      hints.length > 0
        ? `参考路径（recorded approach，可能过时，仅作提示）：${hints
            .map((step) =>
              step.do === "tap_text"
                ? `点「${step.text}」`
                : step.do === "key"
                  ? `按 ${step.code}`
                  : step.do === "text"
                    ? `输入「${step.value}」`
                    : "等待",
            )
            .join(" → ")}`
        : "",
      `模板：${file}`,
    ]
      .filter(Boolean)
      .join("\n");
    const session = await runGuidedSession({
      device,
      prompt,
      maxSteps: config.guidedMaxSteps || 12,
      timeoutMs: config.guidedTimeoutMs,
      shouldStop,
    });
    if (!session.ok) return session;
    if (await verifyScreenText(device, entryExpect)) {
      return { ok: true, steps: guidedStepsFromEvents(session.captured) };
    }
    return { ok: false, message: `自动导航结束，但屏幕上仍没有「${entryExpect}」` };
  }

  /**
   * Guided full run: when the environment is unknown, the model drives every
   * step (not just the approach) using the template as a plan. Only a confirmed
   * environment may be replayed as YAML.
   */
  async function defaultGuidedRun({ device, entryExpect, file, markdown, objective, shouldStop }) {
    if (config.guidedRun === false) {
      return { ok: false, message: "自动执行已关闭（guidedRun: false）" };
    }
    const screenshot = await takeGuidedScreenshot(device);
    const plan = String(markdown || "").replace(/^---[\s\S]*?---\n?/, "").slice(0, 4000);
    const completion = readTemplateCompletion(markdown);
    const successExpect = readSuccessExpect(markdown);
    const lastExpect = completion.expectAfter || readLastExpectAfter(markdown) || entryExpect || null;
    const finalAction = completion.lastStepText || null;
    const prompt = [
      "[自动执行任务]",
      `任务目标：${objective || entryExpect}`,
      `起始页面锚点：「${entryExpect}」；完成后屏幕应出现：「${lastExpect}」。`,
      successExpect
        ? `成功判据：本次执行过程中必须亲眼看到「${successExpect}」（它通常在结果弹窗里出现，可能一闪而过）。` +
          `没看到它之前绝对不能回复完成——其他文字（例如「${lastExpect || "页面按钮"}」）在流程中间就有，不代表任务完成。`
        : finalAction
          ? `完成条件：必须执行完最后一步（点击/操作「${finalAction}」）之后，屏幕上出现「${lastExpect}」。` +
            "注意「" +
            lastExpect +
            "」可能在流程中间就出现（例如价保中心页本来就有该按钮），看到它不代表完成，不要提前停止。"
          : "",
      screenshot ? `当前屏幕截图：${screenshot}（可用 read_image 查看）` : "",
      "",
      "工具：phone_ocr_texts（一次列出整屏文字+坐标）、phone_screenshot、phone_click、phone_swipe、phone_key。",
      "规则：",
      "1. 每一步都先看清当前屏幕（phone_ocr_texts 或截图），再决定点击/滑动；不要照抄模板里的坐标。",
      "2. 完成后用 phone_ocr_texts 确认屏幕上出现了完成标志，再回复「已完成」。",
      "3. 不做支付、删除、登录等不可逆操作；遇到验证码/登录墙就停下并说明。",
      `4. 最多 ${Number(config.guidedFullMaxSteps) || 30} 个操作步（看屏幕不计入），超过会被强制中止。`,
      "",
      "模板计划（参考用，页面实际情况优先）：",
      plan,
    ]
      .filter(Boolean)
      .join("\n");
    const session = await runGuidedSession({
      device,
      prompt,
      maxSteps: config.guidedFullMaxSteps || 30,
      timeoutMs: Number(config.guidedFullTimeoutMs) || Number(config.guidedTimeoutMs) || 240000,
      shouldStop,
      successEvidence: successExpect || lastExpect,
    });
    if (!session.ok) return session;
    if (successExpect) {
      if (
        sawTextInCaptured(session.captured, successExpect) ||
        (await verifyScreenText(device, successExpect))
      ) {
        return {
          ok: true,
          doneText: successExpect,
          steps: guidedStepsFromEvents(session.captured),
        };
      }
      return {
        ok: false,
        message: `自动执行结束，但整个过程中没有出现成功证据「${successExpect}」——任务很可能没有做到最后`,
      };
    }
    if (lastExpect && (await verifyScreenText(device, lastExpect))) {
      if (finalAction && !sawTextInCaptured(session.captured, finalAction)) {
        return {
          ok: false,
          message:
            `自动执行提前结束了：最后一步（「${finalAction}」）没有执行过。` +
            "「" +
            lastExpect +
            "」在流程中间就会出现，不能作为完成判据。",
        };
      }
      return { ok: true, doneText: lastExpect, steps: guidedStepsFromEvents(session.captured) };
    }
    return {
      ok: false,
      message: `自动执行结束，但屏幕上仍没有「${lastExpect || "完成标志"}」`,
    };
  }

  /** Run the recorded approach phase; true when the entry gate passed. */
  async function replayApproach(entry) {
    const { task } = entry;
    task.phase = "entry";
    const phase = await runTemplatePhase(entry, ["--entry-only"], "entry");
    if (entry.cancelled) return false;
    return phase.code === 0 && phase.done?.ok !== false;
  }

  /** Let the model navigate to the start page; persist the path when it works. */
  async function runGuided(entry, entryExpect, hints) {
    const { task, context } = entry;
    task.phase = "guided";
    task.guided = true;
    task.lastAction = { name: "自动导航到起始页", ok: true, at: now() };
    const guided = await guidedApproachFn({
      task,
      device: context.device,
      entryExpect,
      file: context.file,
      hints,
      shouldStop: () => entry.cancelled,
    });
    if (entry.cancelled) return guided;
    if (!guided.ok) return guided;
    task.lastAction = { name: "已到达起始页", ok: true, at: now() };
    const learnedSteps = Array.isArray(guided.steps) ? guided.steps : [];
    if (learnedSteps.length > 0 && config.learnApproach !== false) {
      try {
        const learned = await learnApproachFn({
          file: context.file,
          steps: learnedSteps,
          taskId: task.id,
        });
        task.learned = {
          steps: learnedSteps.length,
          version: learned.version,
          newVersion: learned.new_version ?? learned.newVersion,
          backup: learned.backup || null,
        };
      } catch (error) {
        logger.warn?.(`phone approach learn failed: ${error?.message ?? error}`);
      }
    }
    return guided;
  }

  async function continueTemplateRun(entry) {
    const { task, context } = entry;
    if (entry.cancelled) return;
    let markdown = "";
    try {
      markdown = await readFile(context.file, "utf8");
    } catch {
      markdown = "";
    }
    const entryExpect = readEntryExpect(markdown);
    const approachHint = entryExpect ? readEntryApproach(markdown) : [];

    if (entryExpect) {
      // Step 1 is always the environment: if the phone is already on the start
      // page, skip navigation entirely. A user-described environment is trusted.
      if (context.payload.envKnown === true) {
        task.envKnown = true;
      } else if (config.environmentCheck !== false) {
        task.phase = "env";
        task.lastAction = { name: "确认当前环境", ok: true, at: now() };
        task.envAtEntry = await checkAnchor(context.device, entryExpect);
        if (entry.cancelled) return;
      }
    }

    const needReach = Boolean(entryExpect) && !task.envAtEntry && !task.envKnown;
    if (needReach) {
      if (config.guidedFirst === false) {
        // Replay-first: cheap when the recorded path still holds.
        const replayed = await replayApproach(entry);
        if (entry.cancelled) return;
        if (!replayed) {
          const guided = await runGuided(entry, entryExpect, approachHint);
          if (entry.cancelled) return;
          if (!guided.ok) {
            throw new TaskError(
              "ENTRY_UNREACHABLE",
              guided.message || `自动导航也没能到达起始页（需要看到「${entryExpect}」）`,
            );
          }
        }
      } else {
        // Environment unknown: the model drives every step, not just the
        // approach. A recorded path is only trustworthy once the page is known.
        task.phase = "guided";
        task.guided = true;
        task.guidedFull = true;
        task.lastAction = { name: "自动执行任务", ok: true, at: now() };
        const run = await guidedRunFn({
          task,
          device: context.device,
          entryExpect,
          file: context.file,
          markdown,
          objective: task.rendered || task.objective,
          shouldStop: () => entry.cancelled,
        });
        if (entry.cancelled) return;
        if (!run.ok) {
          throw new TaskError(
            "GUIDED_RUN_FAILED",
            run.message || `自动执行失败（需要看到「${entryExpect}」）`,
          );
        }
        task.lastAction = { name: "已完成", ok: true, at: now() };
        task.phase = "done";
        task.status = "done";
        return;
      }
    }
    if (entry.cancelled) return;
    task.phase = "main";
    const main = await runTemplatePhase(entry, ["--skip-entry"], "main");
    if (entry.cancelled) return;
    if (main.code !== 0) {
      throw new TaskError(
        "TEMPLATE_RUN_FAILED",
        (await describeTemplateFailure(task)) || "模板执行失败",
      );
    }
    task.phase = "done";
    task.status = "done";
  }

  function finalizeTemplateTask(entry) {
    const { task } = entry;
    templateRuns.delete(task.id);
    task.finishedAt = task.finishedAt || now();
    if (entry.cancelled) {
      task.status = "stopped";
      task.error = null;
    } else if (task.status !== "error") {
      task.status = "done";
      task.error = null;
    }
    history.unshift(publicTask(task));
    history.splice(MAX_HISTORY);
    persist();
  }

  async function runTemplate(payload = {}) {
    await ready;
    if (disposed) throw new TaskError("DISPOSED", "task service is disposed");
    const template = templateById(payload.id);
    const file = templateFilePath(template);
    const device = String(payload.device || config.defaultDevice || "default");
    await preflightDevice(device);

    const task = {
      id: randomId(),
      objective: `执行模板：${template.name}`,
      templateId: template.id,
      rendered: template.name,
      variables: {},
      device,
      sessionId: null,
      status: "starting",
      mode: "template",
      phase: "entry",
      guided: false,
      steps: 0,
      total: 0,
      lastAction: null,
      startedAt: now(),
      finishedAt: null,
      error: null,
      tracePath: null,
      reviewPath: null,
      runDir: null,
      proposals: null,
      templateUpdate: null,
      agent: null,
      timer: null,
    };
    const entry = {
      task,
      handle: null,
      cancelled: false,
      context: { file, payload, device, template },
    };
    templateRuns.set(task.id, entry);
    (async () => {
      try {
        await continueTemplateRun(entry);
      } catch (error) {
        task.status = "error";
        task.error = {
          code: error?.code || "TEMPLATE_RUN_FAILED",
          message: error?.message ?? String(error),
        };
      } finally {
        finalizeTemplateTask(entry);
      }
    })();
    return publicTask(task);
  }

  /**
   * Reconnect wireless ADB (the Eye's scrcpy capture needs it). Runs the
   * workspace tool which tries the last endpoint, then mDNS discovery.
   */
  async function reconnectAdb(payload = {}) {
    const args = ["--json"];
    if (payload.endpoint) args.push("--endpoint", String(payload.endpoint));
    const result = await runTemplateTool("adb_reconnect.py", args, { allowFailure: true });
    return { ...result, checkedAt: now() };
  }

  /** Analyse the template's recent traces into machine-readable proposals. */
  async function reviewTemplate(payload = {}) {
    const template = templateById(payload.id);
    const file = templateFilePath(template);
    const analysis = await runTemplateTool("tpl_review.py", [file, "--json"]);
    const proposals = Array.isArray(analysis.proposals) ? analysis.proposals : [];
    return {
      id: template.id,
      file,
      samples: analysis.samples ?? null,
      coords: analysis.coords ?? null,
      steps: Array.isArray(analysis.steps) ? analysis.steps : [],
      proposals,
    };
  }

  /** Apply selected proposals (or preview the diff with dryRun). */
  async function applyTemplateReview(payload = {}) {
    const template = templateById(payload.id);
    const file = templateFilePath(template);
    const args = [file, "--json"];
    if (payload.dryRun !== true) args.push("--yes");
    const uids = Array.isArray(payload.uids) ? payload.uids.filter(Boolean).map(String) : [];
    if (uids.length > 0) args.push("--only", uids.join(","));
    const result = await runTemplateTool("tpl_apply.py", args);
    return { id: template.id, ...result };
  }

  /** Restore the previous template version from templates/.versions. */
  async function rollbackTemplate(payload = {}) {
    const template = templateById(payload.id);
    const file = templateFilePath(template);
    const args = [file, "--json", "--rollback"];
    if (payload.to !== undefined && payload.to !== null) args.push("--to", String(payload.to));
    const result = await runTemplateTool("tpl_apply.py", args);
    return { id: template.id, ...result };
  }

  async function saveTemplate(payload = {}) {
    const name = String(payload.name || "").trim();
    const objective = String(payload.objective || "").trim();
    if (!name || !objective) throw new TaskError("BAD_REQUEST", "template name and objective are required");

    let exported = null;
    if (payload.taskId) {
      // Generation failures are the user's business: surface them instead of
      // saving a half-template that silently lacks its markdown.
      exported = await exportTemplate({
        taskId: payload.taskId,
        name,
        objective,
        id: payload.id,
        force: payload.force === true,
        mechanical: payload.mechanical === true,
      });
    }

    const template = {
      id: exported?.id || String(payload.id || randomId()),
      name,
      objective,
      variables: Array.isArray(payload.variables) ? payload.variables.map(String) : [],
      ...(exported ? { file: exported.file, steps: exported.steps } : {}),
    };
    const index = templates.findIndex((item) => item.id === template.id);
    if (index >= 0) templates[index] = template;
    else templates.push(template);
    await persist();
    return {
      ...template,
      ...(exported
        ? { source: exported.source, tracePath: exported.tracePath, steps: exported.steps }
        : {}),
      warnings: [...(exported?.warnings || [])],
    };
  }

  function templateById(id) {
    const key = id === undefined || id === null ? "" : String(id).trim();
    const found = templates.find((item) => String(item.id) === key);
    if (!found) throw new TaskError("NOT_FOUND", "template not found");
    return found;
  }

  /** Only files under templatesRoot may be read or written. */
  function templateFilePath(template) {
    const file = template && template.file ? String(template.file) : "";
    if (!file) {
      throw new TaskError(
        "TEMPLATE_NO_FILE",
        "这个模板没有 md 文件（从「管理模板」手动新增的模板没有轨迹，先跑一次任务再从结果卡存为模板）",
      );
    }
    const root = resolve(templatesRoot);
    const target = resolve(file);
    if (target !== root && !target.startsWith(root + sep)) {
      throw new TaskError("BAD_PATH", "模板文件不在 templates 目录里");
    }
    return target;
  }

  async function renameTemplate(payload = {}) {
    const template = templateById(payload.id);
    const name = String(payload.name ?? "")
      .split(/\r?\n/)[0]
      .replace(/\s+/g, " ")
      .trim()
      .slice(0, 80);
    if (!name) throw new TaskError("BAD_REQUEST", "模板名称不能为空");

    template.name = name;
    let file = null;
    try {
      file = templateFilePath(template);
    } catch {
      file = null; // JSON-only template: rename the list entry only
    }
    if (file) {
      try {
        const current = await readFile(file, "utf8");
        await writeFile(file, patchTemplateName(current, name), "utf8");
      } catch (error) {
        if (error?.code !== "ENOENT") throw error;
        template.file = undefined;
        file = null;
      }
    }
    await persist();
    return { id: template.id, name, file: file || null };
  }

  async function readTemplateFile(payload = {}) {
    const template = templateById(payload.id);
    const file = templateFilePath(template);
    let content;
    try {
      content = await readFile(file, "utf8");
    } catch (error) {
      throw new TaskError("TEMPLATE_FILE_MISSING", `模板文件读不到：${error?.message ?? error}`);
    }
    return {
      id: template.id,
      name: template.name,
      path: file,
      content,
    };
  }

  async function saveTemplateFile(payload = {}) {
    const template = templateById(payload.id);
    const file = templateFilePath(template);
    const content = String(payload.content ?? "");
    const problem = validateTemplateMarkdown(content);
    if (problem) throw new TaskError("INVALID_TEMPLATE", problem);
    await writeFile(file, content, "utf8");

    // A renamed heading/frontmatter should update the chip label too.
    const name = readTemplateName(content);
    if (name && name !== template.name) {
      template.name = name;
      await persist();
    }
    return {
      id: template.id,
      path: file,
      bytes: Buffer.byteLength(content),
      name: template.name,
    };
  }

  async function deleteTemplate(id) {
    const key = id === undefined || id === null ? "" : String(id).trim();
    // "undefined"/"null" arrive when a client lost the template id; treat as bad input
    // instead of silently reporting success.
    if (!key || key === "undefined" || key === "null") {
      throw new TaskError("BAD_REQUEST", "template id is required");
    }
    const before = templates.length;
    templates = templates.filter((item) => String(item.id) !== key);
    if (templates.length === before) return { deleted: false, id: key };
    await persist();
    return { deleted: true, id: key };
  }

  async function deleteHistory(id) {
    const key = String(id || "").trim();
    if (!key) throw new TaskError("BAD_REQUEST", "history id is required");
    const index = history.findIndex((item) => item.id === key);
    if (index < 0) return { deleted: false };
    history.splice(index, 1);
    await persist();
    return { deleted: true, id: key };
  }

  async function clearHistory() {
    const count = history.length;
    if (count === 0) return { deleted: 0 };
    history.splice(0, history.length);
    await persist();
    return { deleted: count };
  }

  function scheduleStatus(entry) {
    return {
      id: entry.id,
      objective: entry.objective,
      variables: entry.variables || {},
      device: entry.device,
      at: entry.at,
      status: entry.status || "scheduled",
    };
  }

  function armSchedule(entry) {
    if (disposed) return;
    const delay = entry.at - now();
    if (delay <= 0) {
      schedules.delete(entry.id);
      runSchedule(entry).catch(() => {});
      return;
    }
    const timer = setTimeout(() => {
      schedules.delete(entry.id);
      runSchedule(entry).catch(() => {});
    }, Math.min(delay, 2147483647));
    timer.unref?.();
    schedules.set(entry.id, { ...entry, timer });
  }

  async function runSchedule(entry) {
    try {
      const task = await start({
        objective: entry.objective,
        variables: entry.variables || {},
        device: entry.device,
        scheduled: true,
      });
      await persist();
      return task;
    } catch (error) {
      history.unshift({
        ...scheduleStatus(entry),
        status: "error",
        finishedAt: now(),
        error: { code: error?.code || "SCHEDULE_FAILED", message: error?.message ?? String(error) },
      });
      await persist();
      throw error;
    }
  }

  async function schedule(payload = {}) {
    await ready;
    const objective = String(payload.objective || "").trim();
    if (!objective) throw new TaskError("BAD_REQUEST", "objective is required");
    let at = Number(payload.at);
    if (!Number.isFinite(at)) {
      const delaySeconds = Number(payload.delaySeconds);
      if (!Number.isFinite(delaySeconds) || delaySeconds <= 0) {
        throw new TaskError("BAD_REQUEST", "at or delaySeconds is required");
      }
      at = now() + delaySeconds * 1000;
    }
    if (at <= now()) throw new TaskError("BAD_REQUEST", "scheduled time must be in the future");
    const entry = {
      id: randomId(),
      objective,
      variables: payload.variables && typeof payload.variables === "object" ? payload.variables : {},
      device: String(payload.device || config.defaultDevice || "default"),
      at,
      status: "scheduled",
    };
    armSchedule(entry);
    await persist();
    return scheduleStatus(entry);
  }

  async function updateSchedule(payload = {}) {
    await ready;
    if (disposed) throw new TaskError("DISPOSED", "task service is disposed");
    const id = String(payload.id || "");
    const entry = schedules.get(id);
    if (!entry) throw new TaskError("NOT_FOUND", "schedule not found");
    let at = Number(payload.at);
    if (!Number.isFinite(at)) {
      const delaySeconds = Number(payload.delaySeconds);
      if (!Number.isFinite(delaySeconds) || delaySeconds <= 0) {
        throw new TaskError("BAD_REQUEST", "at or delaySeconds is required");
      }
      at = now() + delaySeconds * 1000;
    }
    if (at <= now()) throw new TaskError("BAD_REQUEST", "scheduled time must be in the future");
    if (entry.timer) clearTimeout(entry.timer);
    const objective = String(payload.objective || entry.objective).trim();
    if (!objective) throw new TaskError("BAD_REQUEST", "objective is required");
    const next = {
      ...entry,
      objective,
      variables:
        payload.variables && typeof payload.variables === "object"
          ? payload.variables
          : entry.variables || {},
      at,
      status: "scheduled",
    };
    armSchedule(next);
    await persist();
    return scheduleStatus(next);
  }

  async function cancelSchedule(id) {
    const entry = schedules.get(String(id || ""));
    if (!entry) return { cancelled: false };
    if (entry.timer) clearTimeout(entry.timer);
    schedules.delete(entry.id);
    await persist();
    return { cancelled: true };
  }

  function listSchedules() {
    return [...schedules.values()].map(scheduleStatus);
  }

  async function replay(historyId) {
    await ready;
    const entry = history.find((item) => item.id === String(historyId || ""));
    if (!entry) throw new TaskError("NOT_FOUND", "task history not found");
    return start({
      objective: entry.objective,
      variables: entry.variables || {},
      device: entry.device,
    });
  }

  function devices() {
    const names = new Set(["default", ...Object.keys(config.devices || {})]);
    if (config.defaultDevice) names.add(config.defaultDevice);
    return [...names].map((name) => ({
      name,
      baseUrl: config.devices?.[name]?.baseUrl || config.baseUrl || "http://127.0.0.1:8765",
    }));
  }

  function dispose() {
    disposed = true;
    if (typeof unsubscribeEvents === "function") unsubscribeEvents();
    if (typeof unsubscribeAgentError === "function") unsubscribeAgentError();
    for (const entry of templateRuns.values()) {
      entry.cancelled = true;
      try {
        entry.handle?.kill?.();
      } catch {
        // best effort
      }
    }
    templateRuns.clear();
    for (const task of tasks.values()) {
      if (task.timer) clearInterval(task.timer);
      task.timer = null;
    }
    for (const entry of schedules.values()) {
      if (entry.timer) clearTimeout(entry.timer);
    }
    schedules.clear();
    tasks.clear();
  }

  return {
    ready,
    preflight,
    start,
    status,
    stop,
    history: () => history.slice(),
    deleteHistory,
    clearHistory,
    templates: listTemplates,
    saveTemplate,
    exportTemplate,
    renameTemplate,
    readTemplateFile,
    saveTemplateFile,
    runTemplate,
    reconnectAdb,
    reviewTemplate,
    applyTemplateReview,
    rollbackTemplate,
    deleteTemplate,
    schedule,
    updateSchedule,
    schedules: listSchedules,
    cancelSchedule,
    replay,
    devices,
    dispose,
  };
}

/**
 * Summarise a finished panel task into a reusable template.
 *
 * The mechanical converter only copies coordinates. A task's real value is the
 * *reasoning* behind each step: what it tried to achieve and what it expected
 * to see afterwards. This module turns the trace plus the model's own
 * reasoning into a structured plan, asks the model to compress it, and renders
 * markdown that still passes `tools/tpl_compile.py`.
 *
 * Everything except `renderSummaryTemplate` is pure; the host supplies the
 * model call.
 */

const YAML_PLAIN = /^[\w\u4e00-\u9fa5·./ -]+$/;

export function yamlString(value) {
  const text = String(value ?? "");
  return YAML_PLAIN.test(text) ? text : JSON.stringify(text);
}

function fmt(value, digits = 4) {
  return Number(value).toFixed(digits);
}

function oneLine(value, limit = 200) {
  return String(value ?? "")
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter(Boolean)
    .join(" ")
    .slice(0, limit);
}

export function templateIdFrom(name, taskId) {
  const slug = String(name || "")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 40);
  if (slug) return slug;
  const short = String(taskId || "").replace(/^task-/, "").slice(0, 8);
  return short ? `task-${short}` : "untitled";
}

/** One numbered line per trace step, including the model's own reasoning. */
export function buildDigest(entry) {
  const steps = Array.isArray(entry.steps) ? entry.steps : [];
  const lines = [];
  steps.forEach((step, index) => {
    const number = index + 1;
    const ms = step.ms ? `${Math.round(step.ms)}ms` : "";
    let line = `[${number}] ${step.do}`;
    if (step.do === "find" && step.args?.keyword) line += ` "${step.args.keyword}"`;
    if (step.do === "look" && step.dim) line += ` ${step.dim[0]}x${step.dim[1]}`;
    if (step.at) line += ` → 点击 (${step.at[0]}, ${step.at[1]})`;
    if (step.center) line += ` → 命中 (${step.center[0]}, ${step.center[1]})`;
    if (step.do === "swipe") line += " 滑动";
    line += ` ${ms}${step.ok === false ? " 失败" : ""}`;
    if (step.why) line += `\n    思考：${oneLine(step.why, 300)}`;
    lines.push(line);
  });
  const failedSteps = steps
    .map((step, index) => ({ index: index + 1, step }))
    .filter((item) => item.step.ok === false)
    .map((item) => item.index);
  return {
    objective: entry.objective || "",
    screen: entry.screen || null,
    status: entry.done?.ok === false ? "失败" : "成功",
    failedSteps,
    durationMs: entry.done?.ms || 0,
    stepCount: steps.length,
    text: lines.join("\n"),
  };
}

/**
 * Ask the model for a structured plan. Output must be one JSON object — the
 * host parses it and renders markdown itself, so a chatty reply only costs one
 * retry, never a broken template.
 */
export function buildSummaryPrompt(digest) {
  const screen = Array.isArray(digest.screen) ? digest.screen : [1080, 2400];
  return [
    "你是手机自动化任务的分析师。下面是一次已经执行完的任务记录，",
    "包括每一步的工具调用和当时模型的思考。",
    "",
    "请把它整理成一份可复用的任务模板计划。不要调用任何工具，只输出 JSON。",
    "",
    "输出格式（单个 JSON 对象，不要 markdown 代码块，不要解释）：",
    "{",
    '  "name": "不超过 20 字的标题",',
    '  "goal": "整体目标，一句话",',
    '  "preconditions": ["执行前页面上必须成立的条件"],',
    '  "success_criteria": ["看到什么算成功"],',
    '  "strategy": ["可复用的策略要点，比如先点哪里、遇到弹窗怎么处理"],',
    '  "entry_expect": "approach 完成后、主步骤开始前，屏幕上应出现的短文字（≤12 字）",',
    '  "approach": [',
    '    {"do": "key", "code": "HOME"},',
    '    {"do": "tap_text", "text": "淘宝"},',
    '    {"do": "expect", "text": "我的淘宝", "timeout": 10}',
    "  ],",
    '  "summary": ["执行总结要点：走了什么路径、哪几步容易出错、耗时分布"],',
    '  "irreversible": ["可能有真实副作用的步骤 id"],',
    '  "steps": [',
    '    {"id": "s1", "source_step": 2, "goal": "这一步想达成什么",',
    '     "do": "tap", "text": "任务", "at": [0.9296, 0.2261],',
    '     "expect_after": "点击后屏幕上应出现什么", "check_timeout": 15}',
    "  ]",
    "}",
    "",
    "规则：",
    `1. do 只能是 tap / tap_text / expect / key / swipe / sleep / text。`,
    `2. 坐标用记录里的值，除以屏幕 ${screen[0]}x${screen[1]} 归一化到 0-1，保留 4 位小数。`,
    "3. tap 必须给 at，并尽量给 text（这次点了什么文字）；文本记录里没有就给 null。",
    "4. expect 必须给 text 和 timeout（秒，默认 3）。",
    "5. 连续重复的相同动作只保留一轮，在 goal 里写清「需要重复 N 次」。",
    "6. 每步的 expect_after 必须是屏幕上的可观察结果（文字/页面），不能写「成功」。",
    "7. source_step 填这条计划对应记录里的方括号编号；对不上就省略。",
    "8. 截图（look）步骤不要进计划。",
    "9. 不要编造记录里没发生的步骤。",
    "10. entry_expect 是 approach 完成后、主步骤开始前屏幕上的**短文字锚点**（例如「专属客服」），不是桌面上看到的东西；拿不准给 null。",
    "11. approach 是「从任意状态/桌面到起始页」的导航：只用 key/tap_text/expect/sleep，不要坐标；先按 HOME 回桌面，再按文字打开 App、关掉弹窗、进入目标页。多几步没关系，求稳。",
    "12. main steps 只写「到达起始页之后」的稳定路径；到达之前的那段不要放进 steps。",
    "13. expect_after 必须是屏幕上会出现的**短文字**（≤16 字、不要标点或整句），它会被编译成真正的校验；写不准就省略。",
    "15. 结果异步出现的步骤（例如点「一键价保」后先显示「正在核算」、再弹结果），在该步上加 check_timeout（8–30 秒，默认 15）；普通同步步骤不要加。",
    "14. 记录里标了「失败」的步骤是过程中的坑：写进 strategy 或 summary——发生了什么、怎么恢复的、下次如何避免；不要把它们当正常流程。",
    "",
    "=== 任务记录 ===",
    `目标：${digest.objective}`,
    `结果：${digest.status}，共 ${digest.stepCount} 步，耗时 ${Math.round(digest.durationMs / 1000)} 秒` +
      (digest.failedSteps && digest.failedSteps.length > 0
        ? `；过程中有 ${digest.failedSteps.length} 步失败（第 ${digest.failedSteps.join("、")} 步）`
        : ""),
    digest.text,
    "=== 记录结束 ===",
  ].join("\n");
}

/** Pull the first JSON object out of a model reply (fenced or bare). */
export function parseSummaryResponse(text) {
  const raw = String(text || "");
  const fenced = raw.match(/```(?:json)?\s*\n([\s\S]*?)```/i);
  const candidates = [];
  if (fenced) candidates.push(fenced[1]);
  const start = raw.indexOf("{");
  if (start >= 0) {
    let depth = 0;
    let inString = false;
    let escaped = false;
    for (let index = start; index < raw.length; index += 1) {
      const char = raw[index];
      if (inString) {
        if (escaped) escaped = false;
        else if (char === "\\") escaped = true;
        else if (char === '"') inString = false;
        continue;
      }
      if (char === '"') inString = true;
      else if (char === "{") depth += 1;
      else if (char === "}") {
        depth -= 1;
        if (depth === 0) {
          candidates.push(raw.slice(start, index + 1));
          break;
        }
      }
    }
  }
  for (const candidate of candidates) {
    try {
      const parsed = JSON.parse(candidate);
      if (parsed && typeof parsed === "object" && Array.isArray(parsed.steps)) return parsed;
    } catch {
      // try the next candidate
    }
  }
  return null;
}

const ALLOWED_DO = new Set(["tap", "tap_text", "expect", "key", "swipe", "sleep", "text"]);
/** Steps whose outcome arrives later (calculations, loading, uploads). */
const ASYNC_SEMANTICS = /核算|计算|结算|加载|处理中|提交|生成|上传|同步|排队|等待|查询中/;
const ASYNC_CHECK_TIMEOUT = 15;

function frac(value) {
  const number = Number(value);
  if (!Number.isFinite(number) || number < 0 || number > 1) return null;
  return number;
}

/** Coerce the model's plan into the step shape tpl_compile accepts. */
export function normalizeSummary(summary) {
  const warnings = [];
  const steps = [];
  const rawSteps = Array.isArray(summary?.steps) ? summary.steps : [];

  rawSteps.forEach((raw, index) => {
    if (!raw || ALLOWED_DO.has(raw.do) === false) {
      warnings.push(`计划第 ${index + 1} 步动作不合法（${raw?.do ?? "空"}），已丢弃`);
      return;
    }
    const step = {
      id: `s${steps.length + 1}`,
      goal: oneLine(raw.goal || "", 120),
      do: raw.do,
      sourceStep: Number.isFinite(Number(raw.source_step)) ? Number(raw.source_step) : null,
    };
    if (raw.do === "tap") {
      const pair = Array.isArray(raw.at) ? [frac(raw.at[0]), frac(raw.at[1])] : [null, null];
      if (!pair[0] && pair[0] !== 0) warnings.push(`计划第 ${index + 1} 步 tap 缺少有效坐标，已丢弃`);
      if (pair[0] === null || pair[1] === null) return;
      step.at = pair;
      if (raw.text) step.text = oneLine(raw.text, 40);
      else warnings.push(`计划第 ${index + 1} 步 tap 没有 text 兜底`);
    } else if (raw.do === "tap_text" || raw.do === "expect") {
      const text = oneLine(raw.text || "", 60);
      if (!text) {
        warnings.push(`计划第 ${index + 1} 步 ${raw.do} 缺少 text，已丢弃`);
        return;
      }
      step.text = text;
      if (raw.do === "expect") step.timeout = Number(raw.timeout) > 0 ? Number(raw.timeout) : 3;
    } else if (raw.do === "swipe") {
      const from = Array.isArray(raw.from) ? raw.from.map(frac) : [];
      const to = Array.isArray(raw.to) ? raw.to.map(frac) : [];
      if (from.length !== 2 || to.length !== 2 || from.includes(null) || to.includes(null)) {
        warnings.push(`计划第 ${index + 1} 步 swipe 坐标不合法，已丢弃`);
        return;
      }
      step.from = from;
      step.to = to;
      step.ms = Number(raw.ms) || 400;
    } else if (raw.do === "key") {
      const code = oneLine(raw.code || "", 20).toUpperCase();
      if (!code) {
        warnings.push(`计划第 ${index + 1} 步 key 缺少 code，已丢弃`);
        return;
      }
      step.code = code;
    } else if (raw.do === "sleep") {
      step.ms = Math.max(0, Math.min(60000, Number(raw.ms) || 0));
    } else {
      const value = String(raw.value ?? "");
      if (!value) {
        warnings.push(`计划第 ${index + 1} 步 text 缺少 value，已丢弃`);
        return;
      }
      step.value = value;
    }
    if (raw.expect_after && step.do !== "expect") step.expectAfter = oneLine(raw.expect_after, 120);
    if (step.expectAfter) {
      const explicit = Number(raw.check_timeout ?? raw.checkTimeout ?? raw.timeout);
      if (Number.isFinite(explicit) && explicit > 0) {
        step.checkTimeout = Math.min(120, Math.max(1, explicit));
      } else if (
        ASYNC_SEMANTICS.test(`${step.goal || ""} ${step.text || ""} ${step.expectAfter}`)
      ) {
        step.checkTimeout = ASYNC_CHECK_TIMEOUT;
      }
    }
    steps.push(step);
  });

  if (steps.length === 0) {
    throw new Error("模型没有产出可用步骤");
  }

  const approach = [];
  (Array.isArray(summary.approach) ? summary.approach : []).forEach((raw, index) => {
    if (!raw || typeof raw !== "object") return;
    const action = raw.do;
    if (!["tap_text", "expect", "key", "sleep"].includes(action)) {
      warnings.push(`approach 第 ${index + 1} 步动作不支持（${action ?? "空"}），已丢弃`);
      return;
    }
    if (action === "key") {
      const code = oneLine(raw.code || "", 20).toUpperCase();
      if (!code) {
        warnings.push(`approach 第 ${index + 1} 步 key 缺少 code，已丢弃`);
        return;
      }
      approach.push({ do: "key", code });
    } else if (action === "tap_text") {
      const text = oneLine(raw.text || "", 40);
      if (!text) {
        warnings.push(`approach 第 ${index + 1} 步 tap_text 缺少文字，已丢弃`);
        return;
      }
      approach.push({ do: "tap_text", text });
    } else if (action === "expect") {
      const text = oneLine(raw.text || "", 40);
      if (!text) {
        warnings.push(`approach 第 ${index + 1} 步 expect 缺少文字，已丢弃`);
        return;
      }
      approach.push({ do: "expect", text, timeout: Number(raw.timeout) > 0 ? Number(raw.timeout) : 3 });
    } else {
      approach.push({ do: "sleep", ms: Math.max(0, Math.min(60000, Number(raw.ms) || 0)) });
    }
  });

  return {
    entryExpect: oneLine(summary.entry_expect || "", 40) || null,
    approach,
    name: oneLine(summary.name || "", 30),
    goal: oneLine(summary.goal || "", 200),
    preconditions: (Array.isArray(summary.preconditions) ? summary.preconditions : [])
      .map((item) => oneLine(item, 160))
      .filter(Boolean),
    successCriteria: (Array.isArray(summary.success_criteria) ? summary.success_criteria : [])
      .map((item) => oneLine(item, 160))
      .filter(Boolean),
    strategy: (Array.isArray(summary.strategy) ? summary.strategy : [])
      .map((item) => oneLine(item, 160))
      .filter(Boolean),
    summaryLines: (Array.isArray(summary.summary) ? summary.summary : [])
      .map((item) => oneLine(item, 240))
      .filter(Boolean),
    irreversible: (Array.isArray(summary.irreversible) ? summary.irreversible : [])
      .map((item) => oneLine(item, 40))
      .filter(Boolean),
    steps,
    warnings,
  };
}

function entryYaml(plan) {
  if (!plan.entryExpect) return [];
  const lines = ["entry:", `  expect: ${yamlString(plan.entryExpect)}`, "  timeout: 20"];
  if (Array.isArray(plan.approach) && plan.approach.length > 0) {
    lines.push("  approach:");
    for (const step of plan.approach) {
      const parts = [`do: ${step.do}`];
      if (step.text !== undefined) parts.push(`text: ${yamlString(step.text)}`);
      if (step.code !== undefined) parts.push(`code: ${yamlString(step.code)}`);
      if (step.timeout !== undefined) parts.push(`timeout: ${step.timeout}`);
      if (step.ms !== undefined) parts.push(`ms: ${step.ms}`);
      lines.push(`    - {${parts.join(", ")}}`);
    }
  }
  return lines;
}

function stepYaml(step) {
  const parts = [`id: ${step.id}`, `do: ${step.do}`];
  if (step.goal) parts.push(`goal: ${yamlString(step.goal)}`);
  if (step.text !== undefined) parts.push(`text: ${yamlString(step.text)}`);
  if (step.at) parts.push(`at: [${fmt(step.at[0])}, ${fmt(step.at[1])}]`);
  if (step.from) parts.push(`from: [${fmt(step.from[0])}, ${fmt(step.from[1])}]`);
  if (step.to) parts.push(`to: [${fmt(step.to[0])}, ${fmt(step.to[1])}]`);
  if (step.timeout !== undefined) parts.push(`timeout: ${step.timeout}`);
  if (step.ms !== undefined) parts.push(`ms: ${step.ms}`);
  if (step.value !== undefined) parts.push(`value: ${yamlString(step.value)}`);
  if (step.code !== undefined) parts.push(`code: ${yamlString(step.code)}`);
  if (step.expectAfter) parts.push(`expect_after: ${yamlString(step.expectAfter)}`);
  if (step.checkTimeout !== undefined) parts.push(`check_timeout: ${step.checkTimeout}`);
  return `  - {${parts.join(", ")}}`;
}

/** Canonical trace for the reviewer: ids match the template steps. */
export function summaryTrace(plan, originalSteps) {
  const records = [];
  plan.steps.forEach((step, index) => {
    const sourceIndex = Number.isInteger(step.sourceStep) ? step.sourceStep - 1 : index;
    const source = originalSteps[sourceIndex] || originalSteps[index] || null;
    const record = {
      t: "step",
      i: records.length + 1,
      id: step.id,
      do: step.do,
      ok: source ? source.ok !== false : true,
      att: 1,
      ms: source ? Number(source.ms) || 0 : 0,
    };
    if (step.do === "tap" && step.at) record.at = step.at;
    if (step.do === "expect" && source?.center) record.center = source.center;
    records.push(record);
  });
  const failed = records.filter((record) => record.ok === false).length;
  return {
    records,
    done: { t: "done", ok: failed === 0 && records.length > 0, steps: records.length, failed, ms: 0 },
  };
}

function bulletList(lines, empty) {
  if (!lines || lines.length === 0) return [`- ${empty}`];
  return lines.map((line) => `- ${line}`);
}

/**
 * Render the model's plan as a template. Extra `goal` / `expect_after` fields
 * are metadata for humans and for a future guided runner; tpl_compile ignores
 * unknown keys, so the file still compiles with the current rules.
 */
export function renderSummaryTemplate({
  id,
  name,
  objective,
  taskId,
  sessionId,
  device,
  screen,
  plan,
  originalSteps,
  durationMs = 0,
  today = new Date().toISOString().slice(0, 10),
}) {
  const title = plan.name || oneLine(name, 30) || id;
  const lastTap = plan.steps.filter((step) => step.do === "tap").at(-1);
  const irreversible = plan.irreversible.length
    ? plan.irreversible.join("、")
    : lastTap
      ? `${lastTap.id}（最后一次点击后可能有真实副作用）`
      : "无";
  const lines = [
    "---",
    `id: ${id}`,
    `name: ${yamlString(title)}`,
    "version: 1",
    "status: draft",
    `screen: [${screen[0]}, ${screen[1]}]`,
    ...(device ? [`device: ${yamlString(device)}`] : []),
    "coords_known: true",
    `coords_updated: ${today}`,
    "coords_ttl_days: 30",
    `verified: {runs: 1, ok: 1, p50_ms: ${Math.round(durationMs)}, last: ${yamlString(today)}}`,
    ...entryYaml(plan),
    "source:",
    `  task: ${yamlString(taskId)}`,
    `  session: ${yamlString(sessionId)}`,
    `  objective: ${yamlString(objective)}`,
    "  generator: summary",
    "---",
    "",
    `# ${title}`,
    "",
    "## 执行总结",
    "",
    ...bulletList(plan.summaryLines, "模型没有给出执行总结"),
    "",
    "## 目标",
    "",
    plan.goal || objective,
    "",
    "## 判据",
    "",
    ...bulletList(
      plan.successCriteria.map((item) => `**成功**：${item}`),
      "**成功**：待补（模型没有推断出成功判据）",
    ),
    "- **失败**：任一步 expect 超时；页面与预期不符。",
    `- **不可逆点**：${irreversible}`,
    "",
    "## 前置",
    "",
    ...bulletList(
      plan.preconditions,
      "手机上的起始页面要和本次任务开始时一致。",
    ),
    "",
    "## 策略",
    "",
    ...bulletList(plan.strategy, "按步骤顺序执行，每步通过 expect 校验后再继续。"),
    "",
    "## 步骤",
    "",
    "```yaml",
    "# 坐标归一化 0–1；expect_after 是短标签，编译后会变成紧跟该步的 assert_text 校验。",
    "steps:",
    ...plan.steps.map(stepYaml),
    "```",
    "",
    "## 变更记录",
    "",
    "| 版本 | 日期 | 改了什么 | 依据 |",
    "|---|---|---|---|",
    `| 1 | ${today} | 由任务 ${taskId} 的执行记录总结生成 | ${plan.steps.length} 步，模型总结 |`,
    "",
    "## 记录",
    "",
    "```bash",
    `make tpl-check TPL=${id}`,
    `make run-tpl TPL=${id} ARGS="--port <serial> --adb"`,
    `make tpl-review TPL=${id}`,
    "```",
    "",
  ];

  return {
    id,
    markdown: lines.join("\n"),
    steps: plan.steps,
    warnings: plan.warnings,
  };
}

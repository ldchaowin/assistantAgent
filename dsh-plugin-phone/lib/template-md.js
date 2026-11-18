/**
 * Generate a task template `.md` from a recorded panel task.
 *
 * A panel task is exploratory: the agent screenshots, finds text, clicks. That
 * trace is the L0 run. This module compiles it into the deterministic L1 form —
 * normalized coordinates the runner can replay without a model — and writes
 * the same steps as a template file people can read and edit.
 */
import { traceId } from "./trace.js";

const MATCH_PX = 24;
const YAML_SAFE = /^[\w\u4e00-\u9fa5·.\-/ ]+$/;

export function slugify(value) {
  const slug = String(value || "")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 40);
  return slug;
}

export function templateId(name, taskId) {
  const slug = slugify(name);
  if (slug) return slug;
  const short = String(taskId || "").replace(/^task-/, "").slice(0, 8);
  return short ? `task-${short}` : "untitled";
}

function yamlString(value) {
  const text = String(value ?? "");
  return YAML_SAFE.test(text) ? text : JSON.stringify(text);
}

/** A template name must be one short line: frontmatter and the H1 both use it. */
export function cleanName(value, limit = 40) {
  const firstLine = String(value ?? "")
    .split(/\r?\n/)
    .map((line) => line.trim())
    .find((line) => line.length > 0) || "";
  const flat = firstLine.replace(/\s+/g, " ").trim();
  return flat.length > limit ? `${flat.slice(0, limit - 1)}…` : flat;
}

function fmt(value, digits = 4) {
  return Number(value).toFixed(digits);
}

function normalize(point, screen) {
  if (!Array.isArray(point) || point.length < 2) return null;
  const [width, height] = screen;
  if (!width || !height) return null;
  return [point[0] / width, point[1] / height];
}

function samePoint(a, b, tolerance = MATCH_PX) {
  if (!Array.isArray(a) || !Array.isArray(b)) return false;
  return Math.abs(a[0] - b[0]) <= tolerance && Math.abs(a[1] - b[1]) <= tolerance;
}

function nextActionable(steps, from) {
  for (let index = from; index < steps.length; index += 1) {
    if (steps[index].do !== "look") return index;
  }
  return -1;
}

/**
 * Compile trace steps into template steps.
 * Returns `{ steps, skipped, notes }` where `steps` already use `do/at/text`.
 */
/**
 * Best-effort start-page anchor for the mechanical converter: the first text
 * the agent searched for *before* touching the screen. Anything later happened
 * after navigation and is not a valid starting anchor.
 */
export function inferEntryAnchor(traceSteps) {
  for (const step of traceSteps) {
    if (step.do === "tap" || step.do === "swipe" || step.do === "key") return null;
    if (step.do === "find" && step.args && step.args.keyword) return String(step.args.keyword);
  }
  return null;
}

export function templateStepsFromTrace(traceSteps, screen) {
  const steps = [];
  const notes = [];
  let skippedLooks = 0;
  let untaggedTaps = 0;

  const push = (step) => {
    const id = `s${steps.length + 1}`;
    steps.push({ id, ...step });
    return id;
  };

  for (let index = 0; index < traceSteps.length; index += 1) {
    const record = traceSteps[index];
    if (record.do === "look") {
      skippedLooks += 1;
      continue;
    }

    if (record.do === "find") {
      const keyword = record.args?.keyword;
      const nextIndex = nextActionable(traceSteps, index + 1);
      const next = nextIndex >= 0 ? traceSteps[nextIndex] : null;
      if (next && next.do === "tap" && samePoint(record.center, next.at)) {
        const at = normalize(next.at, screen);
        if (at) {
          push({ do: "tap", text: keyword, at });
          index = nextIndex;
          continue;
        }
      }
      if (keyword) {
        push({ do: "expect", text: keyword, timeout: 3 });
        notes.push(`find "${keyword}" 没有跟随点击，生成 expect`);
      } else {
        notes.push(`第 ${record.i} 步 find 缺少关键词，已跳过`);
      }
      continue;
    }

    if (record.do === "tap") {
      const at = normalize(record.at, screen);
      const text = record.args?.keyword;
      if (!at) {
        notes.push(`第 ${record.i} 步点击缺少坐标，已跳过`);
        continue;
      }
      if (!text) untaggedTaps += 1;
      push({ do: "tap", ...(text ? { text } : {}), at });
      continue;
    }

    if (record.do === "swipe") {
      const from = normalize([record.args?.x1, record.args?.y1], screen);
      const to = normalize([record.args?.x2, record.args?.y2], screen);
      if (!from || !to) {
        notes.push(`第 ${record.i} 步滑动缺少坐标，已跳过`);
        continue;
      }
      push({ do: "swipe", from, to, ms: Number(record.args?.ms) || 400 });
      continue;
    }

    if (record.do === "text") {
      const value = record.args?.text || record.args?.value;
      if (value) push({ do: "text", value });
      continue;
    }

    if (record.do === "key") {
      const code = record.args?.code || record.args?.key;
      if (code) push({ do: "key", code });
      continue;
    }

    notes.push(`第 ${record.i} 步 ${record.tool || record.do} 不在模板动作里，已跳过`);
  }

  return { steps, skippedLooks, notes, untaggedTaps };
}

/** One-line yaml flow map; values render the way the compiler expects. */
export function stepYaml(step) {
  const parts = [`id: ${step.id}`, `do: ${step.do}`];
  if (step.text !== undefined) parts.push(`text: ${yamlString(step.text)}`);
  if (step.at) parts.push(`at: [${fmt(step.at[0])}, ${fmt(step.at[1])}]`);
  if (step.from) parts.push(`from: [${fmt(step.from[0])}, ${fmt(step.from[1])}]`);
  if (step.to) parts.push(`to: [${fmt(step.to[0])}, ${fmt(step.to[1])}]`);
  if (step.timeout !== undefined) parts.push(`timeout: ${step.timeout}`);
  if (step.ms !== undefined) parts.push(`ms: ${step.ms}`);
  if (step.value !== undefined) parts.push(`value: ${yamlString(step.value)}`);
  if (step.code !== undefined) parts.push(`code: ${yamlString(step.code)}`);
  return `  - {${parts.join(", ")}}`;
}

/**
 * Build the runnable trace for the generated template: same ids and same `do`
 * as the template steps, so the Python reviewer can read it directly.
 */
export function canonicalTrace(templateSteps, traceSteps, screen) {
  const byIndex = traceSteps.filter((step) => step.do !== "look");
  const records = [];
  let cursor = 0;

  for (const step of templateSteps) {
    // A find+tap pair collapses into one tap step: advance past both.
    const first = byIndex[cursor];
    const second = byIndex[cursor + 1];
    const source =
      step.do === "tap" && first?.do === "find"
        ? second
        : first;
    const consumed = step.do === "tap" && first?.do === "find" ? 2 : 1;
    cursor += consumed;

    const record = {
      t: "step",
      i: records.length + 1,
      id: step.id,
      do: step.do,
      ok: source ? source.ok !== false : true,
      att: 1,
      ms: source ? Number(source.ms) || 0 : 0,
    };
    if (step.do === "tap" && Array.isArray(source?.at)) record.at = source.at;
    if (step.do === "expect" && Array.isArray(source?.center)) record.center = source.center;
    records.push(record);
  }

  const failed = records.filter((record) => record.ok === false).length;
  return {
    records,
    done: { t: "done", ok: failed === 0 && records.length > 0, steps: records.length, failed, ms: 0 },
  };
}

export function buildTemplateMarkdown({
  id: providedId = null,
  name,
  objective,
  taskId,
  sessionId,
  device,
  screen,
  traceSteps,
  durationMs = 0,
  taskOk = true,
  today = new Date().toISOString().slice(0, 10),
}) {
  const id = providedId || templateId(name, taskId);
  const displayName = cleanName(name) || cleanName(objective) || id;
  const { steps, notes, skippedLooks, untaggedTaps } = templateStepsFromTrace(traceSteps, screen);
  if (steps.length === 0) {
    throw new Error("任务轨迹里没有可复用的点击/校验步骤");
  }
  const { records, done } = canonicalTrace(steps, traceSteps, screen);
  const entryAnchor = inferEntryAnchor(traceSteps);
  const last = steps[steps.length - 1];
  const irreversible = steps.filter((step) => step.do === "tap").length > 0
    ? steps.filter((step) => step.do === "tap").at(-1)
    : null;

  const lines = [
    "---",
    `id: ${id}`,
    `name: ${yamlString(displayName)}`,
    "version: 1",
    "status: draft",
    `screen: [${screen[0]}, ${screen[1]}]`,
    ...(device ? [`device: ${yamlString(device)}`] : []),
    "coords_known: true",
    ...(entryAnchor
      ? [`entry: {expect: ${yamlString(entryAnchor)}, timeout: 15}`]
      : ["entry: {expect: 起始页标记, timeout: 15}"]),
    `coords_updated: ${today}`,
    "coords_ttl_days: 30",
    `verified: {runs: 1, ok: ${taskOk ? 1 : 0}, p50_ms: ${Math.round(durationMs)}, last: ${yamlString(today)}}`,
    "source:",
    `  task: ${yamlString(taskId)}`,
    `  session: ${yamlString(sessionId)}`,
    `  objective: ${yamlString(objective)}`,
    "---",
    "",
    `# ${displayName}`,
    "",
    "## 目标",
    "",
    objective,
    "",
    "## 判据",
    "",
    "- **成功**：待补 —— 自动生成时只有轨迹，没有写成功判据。",
    "- **失败**：任一步 expect 超时；页面停在登录 / 验证码 / 弹窗。",
    `- **不可逆点**：${irreversible ? `\`${irreversible.id}\`（最后一次点击之后可能有真实副作用，首次重放加 --stop-before）` : "无点击，暂无"}`,
    "",
    "## 前置",
    "",
    `- 由面板任务 \`${taskId}\` 于 ${today} 自动生成，坐标取自真机实测 ${screen[0]}×${screen[1]}。`,
    "- 手机上的起始页面要和本次任务开始时一致（这条决定成败）。",
    "",
    "## 策略",
    "",
    "- 坐标来自实测，超过 `coords_ttl_days` 自动降级成 tap_text，不会盲点。",
    "- **判据是空的**：先跑 3 次，按 review.md 的提案补 expect 和 timeout，再标 verified。",
    "- expect 的 timeout 先统一给 3 秒，真机上按实测 p95 调。",
    "",
    "## 步骤",
    "",
    "```yaml",
    "# 坐标归一化 0–1，相对 screen；改分辨率不用动数字。",
    "steps:",
    ...steps.map(stepYaml),
    "```",
    "",
    "## 变更记录",
    "",
    "| 版本 | 日期 | 改了什么 | 依据 |",
    "|---|---|---|---|",
    `| 1 | ${today} | 由任务 ${taskId} 的实测轨迹生成 | ${steps.length} 步，${skippedLooks} 次截图未入模板 |`,
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

  const warnings = [...notes];
  if (!entryAnchor) {
    warnings.push(
      "没有推断出入口锚点：模板里先放了占位 entry（起始页标记），执行会停在这一步；" +
        "把 entry.expect 改成起始页上真实存在的文字即可",
    );
  }
  if (skippedLooks > 0) {
    warnings.push(`${skippedLooks} 次 phone_screenshot 没进模板（模型看画面用，回放不需要）`);
  }
  if (untaggedTaps > 0) {
    warnings.push(
      `${untaggedTaps} 个 tap 没有 text 兜底（坐标过期后无法降级），补上文字或 roi`,
    );
  }

  return {
    id,
    markdown: lines.join("\n"),
    steps,
    trace: {
      id,
      records,
      done,
    },
    warnings,
  };
}

export { traceId };

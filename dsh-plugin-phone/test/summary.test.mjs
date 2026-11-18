import assert from "node:assert/strict";
import test from "node:test";
import {
  buildDigest,
  buildSummaryPrompt,
  normalizeSummary,
  parseSummaryResponse,
  renderSummaryTemplate,
  summaryTrace,
  yamlString
} from "../lib/summary.js";

const ENTRY = {
  taskId: "task-1",
  sessionId: "session-1",
  device: "default",
  objective: "完成每日任务",
  screen: [1264, 2800],
  done: { ok: true, ms: 600000 },
  steps: [
    { t: "step", i: 1, id: "p1-look", do: "look", ok: true, ms: 120, dim: [462, 1024] },
    {
      t: "step",
      i: 2,
      id: "p2-find",
      do: "find",
      ok: true,
      ms: 300,
      args: { keyword: "任务" },
      center: [1175, 633],
      why: "先找任务入口。",
      whySeq: 1
    },
    { t: "step", i: 3, id: "p3-tap", do: "tap", ok: true, ms: 80, at: [1175, 633] }
  ]
};

const PLAN = {
  name: "每日任务",
  goal: "完成每日任务并领奖",
  preconditions: ["游戏主页已打开"],
  success_criteria: ["看到已领取"],
  strategy: ["先从任务入口进入"],
  summary: ["打开任务页", "连续领取两轮"],
  irreversible: ["s2"],
  steps: [
    {
      id: "s1",
      source_step: 2,
      goal: "打开任务页",
      do: "tap",
      text: "任务",
      at: [0.9296, 0.2261],
      expect_after: "出现任务列表"
    },
    { id: "s2", goal: "领取奖励", do: "tap", text: "领取", at: [0.6, 0.6] },
    { id: "s3", goal: "确认到账", do: "expect", text: "已领取", timeout: 3 }
  ]
};

test("buildDigest numbers steps and keeps the reasoning", () => {
  const digest = buildDigest(ENTRY);
  assert.equal(digest.stepCount, 3);
  assert.match(digest.text, /\[1\] look 462x1024/);
  assert.match(digest.text, /\[2\] find "任务" → 命中 \(1175, 633\)/);
  assert.match(digest.text, /思考：先找任务入口/);
  assert.equal(digest.status, "成功");
  assert.equal(digest.durationMs, 600000);
});

test("buildSummaryPrompt states the rules and embeds the digest", () => {
  const prompt = buildSummaryPrompt(buildDigest(ENTRY));
  assert.match(prompt, /只输出 JSON/);
  assert.match(prompt, /1264x2800/);
  assert.match(prompt, /source_step/);
  assert.match(prompt, /完成每日任务/);
  assert.match(prompt, /\[2\] find "任务"/);
});

test("parseSummaryResponse accepts fenced or bare JSON and rejects junk", () => {
  const fenced = parseSummaryResponse("```json\n" + JSON.stringify(PLAN) + "\n```");
  assert.equal(fenced.name, "每日任务");
  const bare = parseSummaryResponse("这是结果：" + JSON.stringify(PLAN) + " 完毕");
  assert.equal(bare.steps.length, 3);
  assert.equal(parseSummaryResponse("抱歉，我无法完成"), null);
  assert.equal(parseSummaryResponse(JSON.stringify({ name: "x" })), null);
});

test("normalizeSummary drops invalid steps and records why", () => {
  const plan = normalizeSummary({
    name: "测试",
    goal: "g",
    success_criteria: ["看到 X"],
    summary: ["a"],
    steps: [
      { do: "tap", at: [0.5, 0.5], text: "好", goal: "点好" },
      { do: "tap", at: [2, 0.5] },
      { do: "nonsense" },
      { do: "expect", text: "" },
      { do: "swipe", from: [0, 0.2], to: [0, 0.8], ms: 300 },
      { do: "key", code: "back" },
      { do: "wait", text: "x" }
    ]
  });
  assert.deepEqual(plan.steps.map((step) => step.do), ["tap", "swipe", "key"]);
  assert.equal(plan.steps[0].goal, "点好");
  assert.equal(plan.steps[1].ms, 300);
  assert.equal(plan.steps[2].code, "BACK");
  assert.ok(plan.warnings.some((warning) => /坐标/.test(warning)));
  assert.ok(plan.warnings.some((warning) => /不合法/.test(warning)));
});

test("normalizeSummary refuses an empty plan", () => {
  assert.throws(() => normalizeSummary({ steps: [] }), /没有产出可用步骤/);
});

test("renderSummaryTemplate writes goal and expect_after into the steps", () => {
  const plan = normalizeSummary(PLAN);
  const built = renderSummaryTemplate({
    id: "task-1",
    name: "每日任务",
    objective: "完成每日任务",
    taskId: "task-1",
    sessionId: "session-1",
    device: "default",
    screen: [1264, 2800],
    plan,
    originalSteps: ENTRY.steps,
    durationMs: 600000,
    today: "2026-11-15"
  });
  assert.match(built.markdown, /generator: summary/);
  assert.match(built.markdown, /## 执行总结/);
  assert.match(built.markdown, /goal: 打开任务页/);
  assert.match(built.markdown, /expect_after: 出现任务列表/);
  assert.match(built.markdown, /at: \[0\.9296, 0\.2261\]/);
  assert.match(built.markdown, /## 不可逆点|不可逆点/);
  assert.equal(built.steps.length, 3);
});

test("summaryTrace maps plan steps back to the recorded run", () => {
  const plan = normalizeSummary(PLAN);
  const { records, done } = summaryTrace(plan, ENTRY.steps);
  assert.deepEqual(records.map((record) => record.id), ["s1", "s2", "s3"]);
  assert.deepEqual(records[0].at, [0.9296, 0.2261]);
  assert.equal(done.ok, true);
});

test("yamlString quotes only when needed", () => {
  assert.equal(yamlString("领取"), "领取");
  assert.equal(yamlString("a: b"), '"a: b"');
});

test("entry_expect becomes a frontmatter entry gate", () => {
  const plan = normalizeSummary({ ...PLAN, entry_expect: "我的淘宝" });
  assert.equal(plan.entryExpect, "我的淘宝");
  const built = renderSummaryTemplate({
    id: "task-1",
    name: "每日任务",
    objective: "完成每日任务",
    taskId: "task-1",
    sessionId: "session-1",
    device: "default",
    screen: [1264, 2800],
    plan,
    originalSteps: ENTRY.steps,
    today: "2026-11-17"
  });
  assert.match(built.markdown, /entry:\n  expect: 我的淘宝\n  timeout: 20/);
});

test("no entry gate is written when the model omits it", () => {
  const plan = normalizeSummary(PLAN);
  assert.equal(plan.entryExpect, null);
  const built = renderSummaryTemplate({
    id: "task-1",
    name: "每日任务",
    objective: "x",
    taskId: "task-1",
    sessionId: "s",
    device: "default",
    screen: [1264, 2800],
    plan,
    originalSteps: ENTRY.steps,
    today: "2026-11-17"
  });
  assert.ok(!built.markdown.includes("entry: {"));
});


test("approach steps are rendered into the entry block", () => {
  const plan = normalizeSummary({
    ...PLAN,
    entry_expect: "专属客服",
    approach: [
      { do: "key", code: "HOME" },
      { do: "tap_text", text: "淘宝" },
      { do: "expect", text: "我的淘宝", timeout: 10 },
      { do: "tap", at: [1, 1] }
    ]
  });
  assert.deepEqual(plan.approach.map((step) => step.do), ["key", "tap_text", "expect"]);
  assert.ok(plan.warnings.some((warning) => /approach 第 4 步动作不支持/.test(warning)));
  const built = renderSummaryTemplate({
    id: "task-1",
    name: "每日任务",
    objective: "x",
    taskId: "task-1",
    sessionId: "s",
    device: "default",
    screen: [1264, 2800],
    plan,
    originalSteps: ENTRY.steps,
    today: "2026-11-18"
  });
  assert.match(built.markdown, /entry:\n  expect: 专属客服\n  timeout: 20\n  approach:/);
  assert.match(built.markdown, /- \{do: key, code: HOME\}/);
  assert.match(built.markdown, /- \{do: tap_text, text: 淘宝\}/);
  assert.match(built.markdown, /- \{do: expect, text: 我的淘宝, timeout: 10\}/);
});

test("async steps automatically get a longer check timeout", () => {
  const plan = normalizeSummary({
    ...PLAN,
    steps: [
      {
        id: "s1",
        goal: "点击一键价保发起核算",
        do: "tap",
        text: "一键价保",
        at: [0.5, 0.89],
        expect_after: "我知道了"
      },
      {
        id: "s2",
        goal: "打开设置",
        do: "tap",
        text: "设置",
        at: [0.5, 0.5],
        expect_after: "无线网络"
      },
      {
        id: "s3",
        goal: "等待生成",
        do: "tap",
        text: "生成",
        at: [0.2, 0.2],
        expect_after: "已生成",
        check_timeout: 30
      }
    ]
  });
  const byId = Object.fromEntries(plan.steps.map((step) => [step.id, step]));
  assert.equal(byId.s1.checkTimeout, 15, "核算类步骤默认等 15 秒");
  assert.equal(byId.s2.checkTimeout, undefined, "普通步骤不加等待");
  assert.equal(byId.s3.checkTimeout, 30, "模型显式给的值优先");

  const built = renderSummaryTemplate({
    id: "task-1",
    name: "每日任务",
    objective: "x",
    taskId: "task-1",
    sessionId: "s",
    device: "default",
    screen: [1264, 2800],
    plan,
    originalSteps: ENTRY.steps,
    today: "2026-11-18"
  });
  assert.match(built.markdown, /expect_after: 我知道了, check_timeout: 15/);
});

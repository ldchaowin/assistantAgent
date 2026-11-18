import assert from "node:assert/strict";
import test from "node:test";
import {
  buildTemplateMarkdown,
  canonicalTrace,
  cleanName,
  inferEntryAnchor,
  slugify,
  stepYaml,
  templateId,
  templateStepsFromTrace,
} from "../lib/template-md.js";

const SCREEN = [1264, 2800];

function step(overrides) {
  return { ok: true, att: 1, ms: 100, args: {}, ...overrides };
}

test("slugify keeps ascii and falls back for chinese", () => {
  assert.equal(slugify("Open Settings!"), "open-settings");
  assert.equal(slugify("打开设置"), "");
  assert.equal(templateId("打开设置", "1d167dc2-0d1d"), "task-1d167dc2");
});

test("find + click collapse into one coordinate tap", () => {
  const { steps } = templateStepsFromTrace(
    [
      step({ do: "look", i: 1 }),
      step({ do: "find", i: 2, args: { keyword: "商城" }, center: [632, 1400] }),
      step({ do: "tap", i: 3, at: [632, 1400] }),
    ],
    SCREEN,
  );
  assert.equal(steps.length, 1);
  assert.deepEqual(steps[0], { id: "s1", do: "tap", text: "商城", at: [0.5, 0.5] });
});

test("a find without a click becomes an expect", () => {
  const { steps, notes } = templateStepsFromTrace(
    [step({ do: "find", i: 1, args: { keyword: "申请成功" }, center: [100, 100] })],
    SCREEN,
  );
  assert.equal(steps[0].do, "expect");
  assert.equal(steps[0].text, "申请成功");
  assert.equal(notes.length, 1);
});

test("a click far from the found center stays two steps", () => {
  const { steps } = templateStepsFromTrace(
    [
      step({ do: "find", i: 1, args: { keyword: "我的" }, center: [100, 100] }),
      step({ do: "tap", i: 2, at: [900, 2000] }),
    ],
    SCREEN,
  );
  assert.deepEqual(steps.map((item) => item.do), ["expect", "tap"]);
});

test("screenshots are skipped and counted", () => {
  const { steps, skippedLooks } = templateStepsFromTrace(
    [step({ do: "look", i: 1 }), step({ do: "look", i: 2 }), step({ do: "tap", i: 3, at: [0, 0] })],
    SCREEN,
  );
  assert.equal(skippedLooks, 2);
  assert.equal(steps.length, 1);
});

test("swipe and key survive the mapping", () => {
  const { steps } = templateStepsFromTrace(
    [
      step({ do: "swipe", i: 1, args: { x1: 0, y1: 100, x2: 0, y2: 900, ms: 300 } }),
      step({ do: "key", i: 2, args: { code: "BACK" } }),
    ],
    SCREEN,
  );
  assert.equal(steps[0].do, "swipe");
  assert.equal(steps[0].ms, 300);
  assert.equal(steps[1].do, "key");
  assert.equal(steps[1].code, "BACK");
});

test("stepYaml renders a one-line flow map", () => {
  assert.equal(
    stepYaml({ id: "s1", do: "tap", text: "我的", at: [0.5, 0.9] }),
    "  - {id: s1, do: tap, text: 我的, at: [0.5000, 0.9000]}",
  );
  assert.equal(
    stepYaml({ id: "s2", do: "expect", text: "a: b", timeout: 3 }),
    '  - {id: s2, do: expect, text: "a: b", timeout: 3}',
  );
});

test("canonicalTrace keeps template step ids", () => {
  const traceSteps = [
    step({ do: "find", i: 1, args: { keyword: "商城" }, center: [632, 1400], ms: 300 }),
    step({ do: "tap", i: 2, at: [632, 1400], ms: 80 }),
    step({ do: "find", i: 3, args: { keyword: "招募" }, center: [100, 200], ms: 200 }),
  ];
  const { steps } = templateStepsFromTrace(traceSteps, SCREEN);
  const { records, done } = canonicalTrace(steps, traceSteps, SCREEN);
  assert.equal(records.length, 2);
  assert.deepEqual(records.map((record) => record.id), ["s1", "s2"]);
  assert.deepEqual(records[0].at, [632, 1400]);
  assert.equal(records[1].do, "expect");
  assert.deepEqual(records[1].center, [100, 200]);
  assert.equal(done.ok, true);
});

test("buildTemplateMarkdown writes runnable frontmatter and steps", () => {
  const built = buildTemplateMarkdown({
    name: "点击商城招募",
    objective: "在永远的蔚蓝星球里点 5 次擂鼓招募",
    taskId: "task-1",
    sessionId: "session-1",
    device: "default",
    screen: SCREEN,
    traceSteps: [
      step({ do: "find", i: 1, args: { keyword: "商城" }, center: [632, 1400] }),
      step({ do: "tap", i: 2, at: [632, 1400] }),
      step({ do: "find", i: 3, args: { keyword: "擂鼓招募" }, center: [706, 1241] }),
      step({ do: "tap", i: 4, at: [706, 1241] }),
    ],
    durationMs: 558000,
    today: "2026-11-14",
  });

  assert.match(built.markdown, /^---\nid: task-1\n/);
  assert.match(built.markdown, /screen: \[1264, 2800\]/);
  assert.match(built.markdown, /coords_known: true/);
  assert.match(built.markdown, /coords_updated: 2026-11-14/);
  assert.match(built.markdown, /p50_ms: 558000/);
  assert.match(built.markdown, /- \{id: s1, do: tap, text: 商城, at: \[0.5000, 0.5000\]\}/);
  assert.equal(built.steps.length, 2);
  assert.equal(built.trace.records.length, 2);
});

test("buildTemplateMarkdown refuses an empty trace", () => {
  assert.throws(
    () =>
      buildTemplateMarkdown({
        name: "x",
        objective: "",
        taskId: "t",
        screen: SCREEN,
        traceSteps: [],
      }),
    /没有可复用/,
  );
});


test("cleanName flattens a multi-line objective into one title line", () => {
  assert.equal(
    cleanName("打开微信小程序“永远的蔚蓝星球”；\n主页面里点击任务，根据任务指引"),
    "打开微信小程序“永远的蔚蓝星球”；",
  );
  assert.equal(cleanName("a  b\n\nc"), "a b");
  assert.equal(cleanName("x".repeat(60)).endsWith("…"), true);
  assert.equal(cleanName("  \n "), "");
});

test("buildTemplateMarkdown keeps the H1 and name on one line", () => {
  const built = buildTemplateMarkdown({
    name: "打开微信\n主页面里点击任务",
    objective: "完成日常",
    taskId: "task-x",
    screen: SCREEN,
    traceSteps: [step({ do: "tap", i: 1, at: [100, 200] })],
    today: "2026-11-14",
  });
  assert.match(built.markdown, /\nname: 打开微信\n/);
  assert.match(built.markdown, /\n# 打开微信\n/);
  assert.ok(!built.markdown.includes("打开微信\n主页面"));
});

test("mechanical converter infers an entry anchor before the first touch", () => {
  assert.equal(
    inferEntryAnchor([
      step({ do: "look", i: 1 }),
      step({ do: "find", i: 2, args: { keyword: "我的淘宝" } }),
      step({ do: "tap", i: 3, at: [10, 20] })
    ]),
    "我的淘宝"
  );
  assert.equal(
    inferEntryAnchor([
      step({ do: "tap", i: 1, at: [10, 20] }),
      step({ do: "find", i: 2, args: { keyword: "买买买" } })
    ]),
    null
  );
});

test("mechanical template writes the inferred entry gate", () => {
  const built = buildTemplateMarkdown({
    name: "淘宝",
    objective: "打开淘宝",
    taskId: "t1",
    sessionId: "s1",
    device: "default",
    screen: SCREEN,
    traceSteps: [
      step({ do: "find", i: 1, args: { keyword: "我的淘宝" }, center: [500, 800] }),
      step({ do: "tap", i: 2, at: [500, 800] })
    ],
    today: "2026-11-17"
  });
  assert.match(built.markdown, /entry: \{expect: 我的淘宝, timeout: 15\}/);
  assert.ok(!built.warnings.some((warning) => /没有推断出入口锚点/.test(warning)));
});

test("mechanical template warns when no anchor can be inferred", () => {
  const built = buildTemplateMarkdown({
    name: "淘宝",
    objective: "打开淘宝",
    taskId: "t1",
    sessionId: "s1",
    screen: SCREEN,
    traceSteps: [step({ do: "tap", i: 1, at: [500, 800] })],
    today: "2026-11-17"
  });
  assert.ok(built.warnings.some((warning) => /没有推断出入口锚点/.test(warning)));
  assert.match(built.markdown, /entry: \{expect: 起始页标记, timeout: 15\}/);
});

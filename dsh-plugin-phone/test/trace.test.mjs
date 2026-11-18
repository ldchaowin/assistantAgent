import assert from "node:assert/strict";
import test from "node:test";
import {
  buildStepRecords,
  buildTrace,
  createTraceBuffer,
  decisionExcerpt,
  parseArguments,
  parseFindResult,
  parseScreenshotResult,
  serializeTrace,
  traceId,
} from "../lib/trace.js";

function call(callId, name, args = {}, time = 1000) {
  return {
    type: "tool/call",
    time,
    data: { callId, name, arguments: JSON.stringify(args) },
  };
}

function result(callId, text, { isError = false, time = 1200 } = {}) {
  return {
    type: "tool/result",
    time,
    data: {
      message: {
        toolCallId: callId,
        isError,
        content: [{ type: "text", text }],
      },
    },
  };
}

test("parseArguments tolerates strings, objects and garbage", () => {
  assert.deepEqual(parseArguments('{"x":1}'), { x: 1 });
  assert.deepEqual(parseArguments({ x: 2 }), { x: 2 });
  assert.deepEqual(parseArguments("not json"), {});
  assert.deepEqual(parseArguments(null), {});
});

test("parseFindResult reads coordinates and score", () => {
  assert.deepEqual(parseFindResult('Found "商城" at phone (123, 2760) with score 0.9974.'), {
    center: [123, 2760],
    score: 0.9974,
  });
  assert.deepEqual(parseFindResult("nothing here"), {});
});

test("parseScreenshotResult reads path and downscaled size", () => {
  assert.deepEqual(
    parseScreenshotResult("Screenshot 462x1024 (542755 bytes) saved to runs/service/shot-1.png."),
    { dim: [462, 1024], shot: "runs/service/shot-1.png" },
  );
});

test("traceId keeps the tool short name", () => {
  assert.equal(traceId(1, "phone_click"), "p1-click");
  assert.equal(traceId(12, "phone_find_element"), "p12-find_element");
});

test("buffer turns phone calls into steps and skips other tools", () => {
  const buffer = createTraceBuffer();
  buffer.push(call("c1", "phone_screenshot", {}, 1000));
  buffer.push(result("c1", "Screenshot 462x1024 (1 bytes) saved to runs/service/shot-1.png.", { time: 1100 }));
  buffer.push(call("c2", "phone_find_element", { keyword: "商城" }, 1200));
  buffer.push(result("c2", 'Found "商城" at phone (123, 2760) with score 0.9974.', { time: 1500 }));
  buffer.push(call("c3", "phone_click", { x: 123, y: 2760 }, 1600));
  buffer.push(result("c3", "Clicked phone (123, 2760).", { time: 1650 }));
  buffer.push(call("c4", "bash", { command: "ls" }, 1700));
  buffer.push(result("c4", "ok", { time: 1750 }));

  assert.equal(buffer.size, 4);
  assert.equal(buffer.lastAction.name, "phone_click");

  const { steps, done } = buffer.records({ startedAt: 900, finishedAt: 2000 });
  assert.equal(steps.length, 3);
  assert.equal(steps[0].do, "look");
  assert.equal(steps[0].ms, 100);
  assert.equal(steps[1].do, "find");
  assert.deepEqual(steps[1].center, [123, 2760]);
  assert.equal(steps[1].args.keyword, "商城");
  assert.equal(steps[2].do, "tap");
  assert.deepEqual(steps[2].at, [123, 2760]);
  assert.equal(done.steps, 3);
  assert.equal(done.failed, 0);
  assert.equal(done.ok, true);
  assert.equal(done.ms, 1100);
});

test("a call without a result is marked failed", () => {
  const { steps, done } = buildStepRecords({
    calls: [{ callId: "c1", name: "phone_click", arguments: '{"x":1,"y":2}', time: 10 }],
    results: new Map(),
    startedAt: 0,
    finishedAt: 20,
  });
  assert.equal(steps[0].ok, false);
  assert.equal(done.failed, 1);
  assert.equal(done.ok, false);
});

test("an error result is marked failed", () => {
  const buffer = createTraceBuffer();
  buffer.push(call("c1", "phone_find_element", { keyword: "X" }));
  buffer.push(result("c1", "找不到", { isError: true }));
  const { done } = buffer.records({ startedAt: 0, finishedAt: 10 });
  assert.equal(done.failed, 1);
});

test("buildTrace prepends the run header and serializeTrace writes JSONL", () => {
  const records = buildTrace({ id: "t1", screen: [1264, 2800] }, {
    steps: [{ t: "step", i: 1, id: "p1-click", do: "tap", ok: true }],
    done: { t: "done", ok: true, steps: 1, failed: 0, ms: 10 },
  });
  assert.equal(records[0].t, "run");
  assert.equal(records.length, 3);
  const lines = serializeTrace(records).trim().split("\n");
  assert.equal(lines.length, 3);
  assert.equal(JSON.parse(lines[0]).id, "t1");
});

test("assistant reasoning is attached to the phone call that follows it", () => {
  const buffer = createTraceBuffer();
  buffer.push({
    type: "assistant/message",
    time: 900,
    data: {
      message: {
        content: [
          { type: "reasoning", text: "先看页面结构。\n\n现在点击任务入口，进入任务页。" }
        ]
      }
    }
  });
  buffer.push(call("c1", "phone_click", { x: 10, y: 20 }, 1000));
  buffer.push(result("c1", "Clicked phone (10, 20).", { time: 1050 }));

  const { steps, reasonings } = buffer.records({ startedAt: 800, finishedAt: 1100 });
  assert.equal(steps.length, 1);
  assert.match(steps[0].why, /现在点击任务入口/);
  assert.equal(steps[0].whySeq, 1);
  assert.equal(reasonings.length, 1);
  assert.match(reasonings[0].reasoning, /先看页面结构/);
  assert.deepEqual(reasonings[0].steps, [steps[0].id]);
});

test("decisionExcerpt keeps the tail of a long deliberation", () => {
  const long = "第一步探索。\n\n第二步再看。\n\n最后决定点击领取按钮。";
  const excerpt = decisionExcerpt(long);
  assert.match(excerpt, /最后决定点击领取按钮/);
});

test("done.ok reflects the final outcome, not intermediate step failures", () => {
  const buffer = createTraceBuffer();
  buffer.push(call("c1", "phone_click", { x: 1, y: 2 }, 1000));
  buffer.push(result("c1", "Error: device busy", { isError: true, time: 1100 }));
  buffer.push(call("c2", "phone_click", { x: 1, y: 2 }, 1200));
  buffer.push(result("c2", "Clicked phone (1, 2).", { time: 1300 }));

  const recovered = buffer.records({ startedAt: 900, finishedAt: 1400, finalOk: true });
  assert.equal(recovered.done.failed, 1, "process hiccup is kept for the review");
  assert.equal(recovered.done.ok, true, "the task still succeeded");

  const failedRun = buffer.records({ startedAt: 900, finishedAt: 1400, finalOk: false });
  assert.equal(failedRun.done.failed, 1);
  assert.equal(failedRun.done.ok, false);
});

import assert from "node:assert/strict";
import { resolve } from "node:path";
import test from "node:test";

import { createToolDefinitions } from "../lib/tools.js";

const fakeClient = {
  screenshot: async ({ includeBase64 }) => ({
    path: "/tmp/shot.png",
    width: 50,
    height: 100,
    frame_age_ms: 12.5,
    bytes: 123,
    ...(includeBase64 ? { base64: "abc" } : {}),
  }),
  find: async ({ keyword }) => ({
    ok: true,
    found: true,
    keyword,
    match: {
      text: "设置",
      score: 0.99,
      device_center: [10, 20],
      box: [[0, 0], [1, 0], [1, 1], [0, 1]],
    },
    frame: [100, 200],
    screen: [100, 200],
    screenshot_path: "/tmp/find.png",
    frame_age_ms: 8,
    elapsed_ms: 1.5,
    items: 1,
  }),
  ocr: async () => ({
    ok: true,
    frame: [100, 200],
    screen: [100, 200],
    screenshot_path: "/tmp/ocr.png",
    frame_age_ms: 12,
    items: [
      { text: "买买买", score: 0.99, device_center: [40, 50] },
      { text: "淘宝", score: 0.98, device_center: [41, 60] },
    ],
  }),
  click: async () => ({ ok: true, response: { id: 1, ok: true } }),
  swipe: async () => ({ ok: true, response: { id: 2, ok: true } }),
};

const defs = createToolDefinitions((definition) => definition, fakeClient);
const byName = Object.fromEntries(defs.map((definition) => [definition.name, definition]));

test("exposes exactly the five phone tools", () => {
  assert.deepEqual(Object.keys(byName).sort(), [
    "phone_click",
    "phone_find_element",
    "phone_ocr_texts",
    "phone_screenshot",
    "phone_swipe",
  ]);
});

test("screenshot maps nullable path and optional base64", async () => {
  const result = await byName.phone_screenshot.execute({ maxWidth: 50, includeBase64: true });
  assert.equal(result.path, "/tmp/shot.png");
  assert.equal(result.base64, "abc");
  assert.equal(result.ageMs, 12.5);
  assert.match(byName.phone_screenshot.output.render({}, result)[0].text, /50x100/);
  assert.match(byName.phone_screenshot.output.render({}, result)[0].text, /age 12.5 ms/);
});

test("find maps device center to x/y", async () => {
  const result = await byName.phone_find_element.execute({ keyword: "设置" });
  assert.equal(result.found, true);
  assert.equal(result.x, 10);
  assert.equal(result.y, 20);
  assert.equal(result.text, "设置");
  assert.equal(result.screenshotPath, "/tmp/find.png");
  assert.equal(result.ageMs, 8);
  assert.match(byName.phone_find_element.output.render({}, result)[0].text, /\(10, 20\)/);
  assert.match(byName.phone_find_element.output.render({}, result)[0].text, /age 8 ms/);
});

test("relative screenshot paths resolve against the workspace", async () => {
  const relativeClient = {
    ...fakeClient,
    screenshot: async () => ({
      path: "runs/service/shot-1.png",
      width: 50,
      height: 100,
      bytes: 1,
    }),
    find: async () => ({
      ...(await fakeClient.find({ keyword: "设置" })),
      screenshot_path: "runs/service/find-1.png",
    }),
  };
  const defs = createToolDefinitions((definition) => definition, relativeClient, {
    workspace: "/ws",
  });
  const tools = Object.fromEntries(defs.map((definition) => [definition.name, definition]));

  const shot = await tools.phone_screenshot.execute({});
  assert.equal(shot.path, resolve("/ws", "runs/service/shot-1.png"));
  assert.match(
    tools.phone_screenshot.output.render({}, shot)[0].text,
    new RegExp(`saved to ${resolve("/ws", "runs/service/shot-1.png")}`),
  );

  const found = await tools.phone_find_element.execute({ keyword: "设置" });
  assert.equal(found.screenshotPath, resolve("/ws", "runs/service/find-1.png"));
});

test("absolute screenshot paths are left untouched", async () => {
  const defs = createToolDefinitions((definition) => definition, fakeClient, {
    workspace: "/ws",
  });
  const tools = Object.fromEntries(defs.map((definition) => [definition.name, definition]));
  const shot = await tools.phone_screenshot.execute({});
  assert.equal(shot.path, "/tmp/shot.png");
  assert.equal(
    (await tools.phone_find_element.execute({ keyword: "设置" })).screenshotPath,
    "/tmp/find.png",
  );
});

test("click and swipe return canonical output", async () => {
  const click = await byName.phone_click.execute({ x: 1, y: 2 });
  assert.deepEqual(click, { ok: true, x: 1, y: 2, response: { id: 1, ok: true } });
  const swipe = await byName.phone_swipe.execute({ x1: 1, y1: 2, x2: 3, y2: 4, ms: 500 });
  assert.equal(swipe.ms, 500);
  assert.equal(swipe.ok, true);
});

test("ocr lists all texts with coordinates in one call", async () => {
  const result = await byName.phone_ocr_texts.execute({});
  assert.deepEqual(result.texts, [
    { text: "买买买", score: 0.99, x: 40, y: 50 },
    { text: "淘宝", score: 0.98, x: 41, y: 60 }
  ]);
  assert.equal(result.screenshotPath, "/tmp/ocr.png");
  const rendered = byName.phone_ocr_texts.output.render({}, result)[0].text;
  assert.match(rendered, /"买买买" at \(40, 50\)/);
  assert.match(rendered, /"淘宝" at \(41, 60\)/);
});

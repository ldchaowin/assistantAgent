/**
 * Tool definitions for the phone plugin.
 *
 * Kept separate from lib/index.js so tests can pass a fake defineTool and a
 * fake phone client without resolving @deepseek-ai/dsh-tools.
 */
import { isAbsolute, resolve } from "node:path";

const nullableString = { oneOf: [{ type: "string" }, { type: "null" }] };

/**
 * Screenshot paths come from the phone service process, so a relative path is
 * relative to the service cwd, not to the agent session's. Resolve it against
 * the workspace so `read_image` works no matter where the session runs.
 */
function resolveShotPath(path, workspace) {
  if (typeof path !== "string" || path === "") return null;
  return isAbsolute(path) ? path : resolve(workspace, path);
}

export function createToolDefinitions(defineTool, client, { workspace = process.cwd() } = {}) {
  return [
    defineTool({
      name: "phone_screenshot",
      description:
        "Capture the current phone screen. Returns a PNG file path (use the read_image tool to inspect it) plus dimensions and byte size. Set includeBase64=true only when inline PNG data is explicitly needed; it is large.",
      parameters: {
        maxWidth: {
          type: "integer",
          description: "Optional maximum width in pixels; the returned image is downscaled proportionally.",
        },
        includeBase64: {
          type: "boolean",
          description: "Include the PNG bytes as base64 in the result (large; default false).",
        },
      },
      output: {
        schema: {
          type: "object",
          additionalProperties: false,
          properties: {
            path: { ...nullableString, required: true },
            width: { type: "integer", required: true },
            height: { type: "integer", required: true },
            ageMs: { type: "number" },
            bytes: { type: "integer", required: true },
            base64: { type: "string" },
          },
        },
        render: (_args, value) => [
          {
            type: "text",
            text: `Screenshot ${value.width}x${value.height} (${value.bytes} bytes)${
              Number.isFinite(value.ageMs) ? `, frame age ${value.ageMs} ms` : ""
            }${
              value.path ? ` saved to ${value.path}` : " (not saved; no screenshot dir configured)"
            }.`,
          },
        ],
      },
      execute: async (args) => {
        const shot = await client.screenshot({
          includeBase64: args.includeBase64,
          maxWidth: args.maxWidth,
        });
        return {
          path: resolveShotPath(shot.path, workspace),
          width: shot.width,
          height: shot.height,
          ...(Number.isFinite(shot.frame_age_ms) ? { ageMs: shot.frame_age_ms } : {}),
          bytes: shot.bytes,
          ...(shot.base64 ? { base64: shot.base64 } : {}),
        };
      },
      presentCall: (args) => ({
        card: "generic",
        title: "Phone screenshot",
        kind: "other",
        rawInput: args,
      }),
    }),

    defineTool({
      name: "phone_find_element",
      description:
        "Find text on the current phone screen with OCR and return the element center in phone physical pixels. Use the returned x/y with phone_click. Coordinates are device pixels, not screenshot pixels.",
      parameters: {
        keyword: {
          type: "string",
          required: true,
          description: "Text to search for (Chinese or English).",
        },
        minScore: {
          type: "number",
          description: "Minimum OCR confidence, 0..1. Default 0.6.",
        },
        mode: {
          type: "string",
          enum: ["contains", "exact", "regex"],
          description: "Match mode. Default contains (whitespace/case-insensitive).",
        },
        roi: {
          type: "string",
          description: "Optional OCR region in screenshot pixels: 'x,y,w,h'.",
        },
        index: {
          type: "integer",
          description: "Pick the nth match in reading order (default 0).",
        },
      },
      output: {
        schema: {
          type: "object",
          additionalProperties: false,
          properties: {
            found: { type: "boolean", required: true },
            keyword: { type: "string", required: true },
            text: { type: "string" },
            score: { type: "number" },
            x: { type: "integer" },
            y: { type: "integer" },
            box: { type: "array", items: { type: "array", items: { type: "number" } } },
            frameWidth: { type: "integer" },
            frameHeight: { type: "integer" },
            screenWidth: { type: "integer" },
            screenHeight: { type: "integer" },
            screenshotPath: nullableString,
            ageMs: { type: "number" },
            elapsedMs: { type: "number" },
          },
        },
        render: (_args, value) =>
          value.found
            ? [
                {
                  type: "text",
                  text: `Found "${value.text}" at phone (${value.x}, ${value.y}) with score ${value.score}${
                    Number.isFinite(value.ageMs) ? ` (frame age ${value.ageMs} ms)` : ""
                  }.`,
                },
              ]
            : [{ type: "text", text: `Text "${value.keyword}" not found on screen.` }],
      },
      execute: async (args) => {
        const result = await client.find({
          keyword: args.keyword,
          minScore: args.minScore,
          mode: args.mode,
          roi: args.roi,
          index: args.index,
        });
        if (!result.found) {
          return { found: false, keyword: args.keyword };
        }
        const match = result.match;
        return {
          found: true,
          keyword: args.keyword,
          text: match.text,
          score: match.score,
          x: match.device_center[0],
          y: match.device_center[1],
          box: match.box,
          frameWidth: result.frame?.[0],
          frameHeight: result.frame?.[1],
          screenWidth: result.screen?.[0],
          screenHeight: result.screen?.[1],
          screenshotPath: resolveShotPath(result.screenshot_path, workspace),
          ...(Number.isFinite(result.frame_age_ms) ? { ageMs: result.frame_age_ms } : {}),
          elapsedMs: result.elapsed_ms,
        };
      },
      presentCall: (args) => ({
        card: "generic",
        title: `Find "${args.keyword}" on phone`,
        kind: "other",
        rawInput: args,
      }),
    }),

    defineTool({
      name: "phone_ocr_texts",
      description:
        "List every text currently on the phone screen with device coordinates. Use this once to understand the page instead of calling phone_find_element repeatedly; then click the returned coordinates.",
      parameters: {
        minScore: {
          type: "number",
          description: "Minimum OCR confidence, 0..1. Default 0.4.",
        },
        roi: {
          type: "string",
          description: "Optional OCR region in screenshot pixels: 'x,y,w,h'.",
        },
      },
      output: {
        schema: {
          type: "object",
          additionalProperties: false,
          properties: {
            ok: { type: "boolean", required: true },
            texts: {
              type: "array",
              required: true,
              items: {
                type: "object",
                additionalProperties: false,
                properties: {
                  text: { type: "string", required: true },
                  score: { type: "number" },
                  x: { type: "integer" },
                  y: { type: "integer" },
                },
              },
            },
            screenshotPath: { type: "string" },
            ageMs: { type: "number" },
          },
        },
        render: (_args, value) => [
          {
            type: "text",
            text:
              value.texts.length === 0
                ? "No text recognized on the current phone screen."
                : "Texts on phone screen (x, y are device pixels):\n" +
                  value.texts
                    .map((item) => `- "${item.text}" at (${item.x}, ${item.y}) score ${item.score}`)
                    .join("\n"),
          },
        ],
      },
      execute: async (args) => {
        const result = await client.ocr({ minScore: args.minScore, roi: args.roi });
        return {
          ok: true,
          texts: (result.items || []).map((item) => ({
            text: item.text,
            score: item.score,
            x: item.device_center?.[0],
            y: item.device_center?.[1],
          })),
          screenshotPath: resolveShotPath(result.screenshot_path, workspace),
          ageMs: result.frame_age_ms,
        };
      },
      presentCall: () => ({
        card: "generic",
        title: "Read all text on phone",
        kind: "other",
        rawInput: {},
      }),
    }),

    defineTool({
      name: "phone_click",
      description:
        "Click a phone physical pixel coordinate through the ESP32-S3 USB HID hand. Use phone_find_element first when the target position is unknown.",
      parameters: {
        x: { type: "integer", required: true, description: "Phone physical X pixel." },
        y: { type: "integer", required: true, description: "Phone physical Y pixel." },
        holdMs: { type: "integer", description: "Optional press duration in milliseconds." },
      },
      output: {
        schema: {
          type: "object",
          additionalProperties: false,
          properties: {
            ok: { type: "boolean", required: true },
            x: { type: "integer", required: true },
            y: { type: "integer", required: true },
            response: { type: "json" },
          },
        },
        render: (_args, value) => [
          { type: "text", text: `Clicked phone (${value.x}, ${value.y}).` },
        ],
      },
      execute: async (args) => {
        const result = await client.click(args.x, args.y, args.holdMs);
        return { ok: true, x: args.x, y: args.y, response: result.response ?? result };
      },
      presentCall: (args) => ({
        card: "generic",
        title: `Click (${args.x}, ${args.y})`,
        kind: "other",
        rawInput: args,
      }),
    }),

    defineTool({
      name: "phone_swipe",
      description:
        "Swipe on the phone through the ESP32-S3 USB HID hand, using phone physical pixel coordinates.",
      parameters: {
        x1: { type: "integer", required: true, description: "Start X pixel." },
        y1: { type: "integer", required: true, description: "Start Y pixel." },
        x2: { type: "integer", required: true, description: "End X pixel." },
        y2: { type: "integer", required: true, description: "End Y pixel." },
        ms: { type: "integer", description: "Swipe duration in milliseconds (default 400)." },
      },
      output: {
        schema: {
          type: "object",
          additionalProperties: false,
          properties: {
            ok: { type: "boolean", required: true },
            x1: { type: "integer", required: true },
            y1: { type: "integer", required: true },
            x2: { type: "integer", required: true },
            y2: { type: "integer", required: true },
            ms: { type: "integer", required: true },
            response: { type: "json" },
          },
        },
        render: (_args, value) => [
          {
            type: "text",
            text: `Swiped (${value.x1}, ${value.y1}) -> (${value.x2}, ${value.y2}) in ${value.ms} ms.`,
          },
        ],
      },
      execute: async (args) => {
        const result = await client.swipe({
          x1: args.x1,
          y1: args.y1,
          x2: args.x2,
          y2: args.y2,
          ms: args.ms ?? 400,
        });
        return {
          ok: true,
          x1: args.x1,
          y1: args.y1,
          x2: args.x2,
          y2: args.y2,
          ms: args.ms ?? 400,
          response: result.response ?? result,
        };
      },
      presentCall: (args) => ({
        card: "generic",
        title: `Swipe (${args.x1}, ${args.y1}) -> (${args.x2}, ${args.y2})`,
        kind: "other",
        rawInput: args,
      }),
    }),
  ];
}

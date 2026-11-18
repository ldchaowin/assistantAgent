/**
 * Turn a task session's event stream into a trace.
 *
 * The plugin cannot read `session.events` any more (synchronous reads are
 * deprecated in this DSH version), so the task service subscribes to
 * `session/event` on the Cordis context and buffers the few event types a
 * trace needs. Everything here is pure so it can be tested without a host.
 */

const PHONE_PREFIX = "phone_";

/** How a phone tool appears in the trace. Unknown tools keep their raw name. */
const DO_BY_TOOL = {
  phone_screenshot: "look",
  phone_find_element: "find",
  phone_click: "tap",
  phone_swipe: "swipe",
  phone_text: "text",
  phone_key: "key",
};

const KEEP_EVENT_TYPES = new Set(["tool/call", "tool/result", "turn/end", "assistant/message"]);

export function traceId(index, toolName) {
  return `p${index}-${String(toolName || "tool").replace(PHONE_PREFIX, "")}`;
}

export function isPhoneTool(name) {
  return typeof name === "string" && name.startsWith(PHONE_PREFIX);
}

/** Tool arguments arrive as a JSON string on the event; tolerate objects too. */
export function parseArguments(raw) {
  if (raw && typeof raw === "object") return raw;
  if (typeof raw !== "string" || raw.trim() === "") return {};
  try {
    const value = JSON.parse(raw);
    return value && typeof value === "object" ? value : {};
  } catch {
    return {};
  }
}

/** Concatenate the text parts of a tool result message. */
export function resultText(message) {
  const content = message?.content;
  if (!Array.isArray(content)) return "";
  return content
    .filter((part) => part && part.type === "text" && typeof part.text === "string")
    .map((part) => part.text)
    .join("\n");
}

/**
 * Assistant messages carry the model's reasoning and any plain text. A phone
 * call is explained by the message that preceded it, so the buffer stamps each
 * call with the index of its latest message.
 */
export function assistantMessageText(event) {
  const content = event?.data?.message?.content;
  if (!Array.isArray(content)) return { reasoning: "", text: "" };
  const reasoning = content
    .filter((part) => part && part.type === "reasoning" && typeof part.text === "string")
    .map((part) => part.text)
    .join("\n\n");
  const text = content
    .filter((part) => part && part.type === "text" && typeof part.text === "string")
    .map((part) => part.text)
    .join("\n\n");
  return { reasoning, text };
}

/** Keep the decision paragraphs, not the whole deliberation. */
export function decisionExcerpt(reasoning, text, limit = 400) {
  const source = String(reasoning || "").trim() || String(text || "").trim();
  if (!source) return "";
  const paragraphs = source
    .split(/\n{2,}/)
    .map((paragraph) => paragraph.trim())
    .filter(Boolean);
  const tail = paragraphs.slice(-2).join("\n\n") || source;
  return tail.length > limit ? `${tail.slice(0, limit)}…` : tail;
}

/** `Found "商城" at phone (123, 2760) with score 0.9974.` */
export function parseFindResult(text) {
  const out = {};
  if (typeof text !== "string") return out;
  const at = text.match(/at phone \(\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*\)/);
  if (at) out.center = [Math.round(Number(at[1])), Math.round(Number(at[2]))];
  const score = text.match(/score\s+(\d+(?:\.\d+)?)/);
  if (score) out.score = Number(score[1]);
  return out;
}

/** `Screenshot 462x1024 (542755 bytes) saved to runs/service/shot-...png.` */
export function parseScreenshotResult(text) {
  if (typeof text !== "string") return {};
  const out = {};
  const dim = text.match(/Screenshot\s+(\d+)x(\d+)/i);
  if (dim) out.dim = [Number(dim[1]), Number(dim[2])];
  const saved = text.match(/saved to (\S+?)\.?$/i);
  if (saved) out.shot = saved[1];
  return out;
}

/**
 * Buffer only what a trace needs. Call `push` for every `session/event`.
 * Returns `{ push, size, lastAction, calls }`.
 */
export function createTraceBuffer() {
  const calls = [];
  const results = new Map();
  const turnEnds = [];
  const messages = [];

  function push(event) {
    const type = event?.type;
    if (!KEEP_EVENT_TYPES.has(type)) return;
    const data = event.data || {};
    if (type === "assistant/message") {
      const { reasoning, text } = assistantMessageText(event);
      if (reasoning || text) messages.push({ reasoning, text, time: Number(event.time) || 0 });
      return;
    }
    if (type === "tool/call") {
      calls.push({
        callId: data.callId,
        name: data.name,
        arguments: data.arguments,
        time: Number(event.time) || 0,
        whyIndex: messages.length - 1,
      });
      return;
    }
    if (type === "tool/result") {
      const message = data.message || {};
      results.set(message.toolCallId || data.toolCallId, {
        ok: message.isError !== true,
        text: resultText(message),
        time: Number(event.time) || 0,
      });
      return;
    }
    turnEnds.push(Number(event.time) || 0);
  }

  return {
    push,
    get size() {
      return calls.length;
    },
    /** Completed turns seen by this buffer; the monitor uses it to prove the agent ran. */
    get turns() {
      return turnEnds.length;
    },
    get lastAction() {
      const last = findLastPhoneCall(calls);
      if (!last) return null;
      return { name: last.name, arguments: parseArguments(last.arguments), at: last.time };
    },
    /** Build the trace body from whatever arrived so far. */
    records({ startedAt = 0, finishedAt = null, finalOk = null } = {}) {
      return buildStepRecords({ calls, results, turnEnds, messages, startedAt, finishedAt, finalOk });
    },
  };
}

function findLastPhoneCall(calls) {
  for (let index = calls.length - 1; index >= 0; index -= 1) {
    if (isPhoneTool(calls[index].name)) return calls[index];
  }
  return null;
}

export function buildStepRecords({
  calls = [],
  results = new Map(),
  turnEnds = [],
  messages = [],
  startedAt = 0,
  finishedAt = null,
  finalOk = null,
} = {}) {
  const steps = [];
  const reasoningByMessage = new Map();

  for (const call of calls) {
    if (!isPhoneTool(call.name)) continue;
    const result = results.get(call.callId);
    const index = steps.length + 1;
    const record = {
      t: "step",
      i: index,
      id: traceId(index, call.name),
      do: DO_BY_TOOL[call.name] || call.name.replace(PHONE_PREFIX, ""),
      tool: call.name,
      args: parseArguments(call.arguments),
      ok: result ? result.ok === true : false,
      att: 1,
      ms: result ? Math.max(0, result.time - call.time) : 0,
    };
    if (result?.text) {
      const text = result.text.length > 600 ? `${result.text.slice(0, 600)}…` : result.text;
      record.text = text;
      Object.assign(record, parseFindResult(text), parseScreenshotResult(text));
    }
    if (record.do === "tap" && record.args && Number.isFinite(record.args.x)) {
      record.at = [Number(record.args.x), Number(record.args.y)];
      delete record.center;
    }
    if (record.do === "find" && record.args?.keyword && !record.text) {
      record.missing = true;
    }
    const message = Number.isInteger(call.whyIndex) && call.whyIndex >= 0
      ? messages[call.whyIndex]
      : undefined;
    if (message) {
      const why = decisionExcerpt(message.reasoning, message.text);
      if (why) {
        record.why = why;
        record.whySeq = call.whyIndex + 1;
        const entry = reasoningByMessage.get(call.whyIndex) || {
          seq: call.whyIndex + 1,
          reasoning: message.reasoning || message.text || "",
          steps: [],
        };
        entry.steps.push(record.id);
        reasoningByMessage.set(call.whyIndex, entry);
      }
    }
    steps.push(record);
  }

  const failed = steps.filter((step) => step.ok === false).length;
  // `failed` counts hiccups inside the run; `ok` is the final outcome. A task
  // can recover from a failed tool call, so the two must not be conflated: the
  // reviewer needs the hiccups, template generation needs the outcome.
  const ok =
    typeof finalOk === "boolean" ? finalOk && steps.length > 0 : failed === 0 && steps.length > 0;
  const done = {
    t: "done",
    ok,
    steps: steps.length,
    failed,
    ms:
      finishedAt && startedAt && finishedAt > startedAt
        ? finishedAt - startedAt
        : 0,
    turns: turnEnds.length,
  };
  const reasonings = [...reasoningByMessage.values()].sort((a, b) => a.seq - b.seq);
  return { steps, done, reasonings };
}

/**
 * Assemble the JSONL record list written to `trace.jsonl`.
 * `header` carries the task identity so a reader needs no side file.
 */
export function buildTrace(header, { steps, done }) {
  return [{ t: "run", ...header }, ...steps, done];
}

/** Serialize records as JSONL. */
export function serializeTrace(records) {
  return records.map((record) => JSON.stringify(record)).join("\n") + "\n";
}

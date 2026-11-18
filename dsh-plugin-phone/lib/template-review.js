/**
 * Bridge from the plugin host to the Python template tooling.
 *
 * Review and apply live in `phone_console` / `tools/tpl_*.py` because the
 * runner and the reviewer must agree on the template grammar. The host does
 * not re-implement those rules; it shells out to the workspace's tools and
 * parses their `--json` output.
 */
import { spawn } from "node:child_process";
import { existsSync } from "node:fs";
import { join } from "node:path";

/** Pick the interpreter that has `phone_console` installed. */
export function resolvePython(workspace, config = {}) {
  if (config.pythonExecutable) return String(config.pythonExecutable);
  if (process.env.DSH_PHONE_PYTHON) return process.env.DSH_PHONE_PYTHON;
  const venv = join(String(workspace || ""), ".venv", "bin", "python");
  return existsSync(venv) ? venv : "python3";
}

export function resolveToolsDir(workspace, config = {}) {
  return String(config.toolsDir || join(String(workspace || ""), "tools"));
}

/**
 * Run one tool and collect its output. Resolves with the exit code instead of
 * throwing so callers can report the tool's own error message.
 */
export function runTool({ python, cwd, tool, args = [], timeoutMs = 30000 }) {
  return new Promise((resolve, reject) => {
    let child;
    try {
      child = spawn(python, [tool, ...args], { cwd, stdio: ["ignore", "pipe", "pipe"] });
    } catch (error) {
      reject(error);
      return;
    }
    let stdout = "";
    let stderr = "";
    const timer = setTimeout(() => {
      try {
        child.kill("SIGKILL");
      } catch {
        // already gone
      }
      reject(new Error(`模板工具超时（${timeoutMs}ms）：${tool}`));
    }, timeoutMs);
    child.stdout.on("data", (chunk) => {
      stdout += chunk;
    });
    child.stderr.on("data", (chunk) => {
      stderr += chunk;
    });
    child.on("error", (error) => {
      clearTimeout(timer);
      reject(error);
    });
    child.on("close", (code) => {
      clearTimeout(timer);
      resolve({ code: Number.isFinite(code) ? code : 1, stdout, stderr });
    });
  });
}

/**
 * Start a long-running tool and stream its stdout lines. Returns a handle so
 * the task service can update progress live and kill it on "stop".
 */
export function startTool({ python, cwd, tool, args = [], timeoutMs = 900000, onLine }) {
  let child;
  try {
    child = spawn(python, [tool, ...args], { cwd, stdio: ["ignore", "pipe", "pipe"] });
  } catch (error) {
    return {
      child: null,
      kill() {},
      done: Promise.resolve({ code: 1, stderr: String(error?.message ?? error) }),
    };
  }
  let buffer = "";
  let stderr = "";
  let finished = false;
  let timer = null;
  if (timeoutMs > 0) {
    timer = setTimeout(() => {
      try {
        child.kill("SIGKILL");
      } catch {
        // already gone
      }
    }, timeoutMs);
    timer.unref?.();
  }

  const done = new Promise((resolve) => {
    const settle = (code, extra = {}) => {
      if (finished) return;
      finished = true;
      if (timer) clearTimeout(timer);
      resolve({ code: Number.isFinite(code) ? code : 1, stderr, ...extra });
    };
    child.stdout.setEncoding("utf8");
    child.stdout.on("data", (chunk) => {
      buffer += chunk;
      let index = buffer.indexOf("\n");
      while (index >= 0) {
        const line = buffer.slice(0, index).trim();
        buffer = buffer.slice(index + 1);
        if (line) {
          try {
            onLine?.(line);
          } catch {
            // a progress handler must never kill the run
          }
        }
        index = buffer.indexOf("\n");
      }
    });
    child.stderr.setEncoding("utf8");
    child.stderr.on("data", (chunk) => {
      stderr += chunk;
      if (stderr.length > 20000) stderr = stderr.slice(-20000);
    });
    child.on("error", (error) => settle(1, { stderr: stderr || String(error) }));
    child.on("close", (code, signal) => {
      if (buffer.trim()) {
        try {
          onLine?.(buffer.trim());
        } catch {
          // ignore
        }
      }
      settle(code === null ? 1 : code, { signal });
    });
  });

  return {
    child,
    done,
    kill() {
      try {
        child.kill("SIGKILL");
      } catch {
        // already gone
      }
    },
  };
}

/** Parse the last JSON object a tool printed (tools may log before it). */
export function parseToolJson(stdout) {
  const text = String(stdout || "").trim();
  if (!text) throw new Error("工具没有输出");
  try {
    return JSON.parse(text);
  } catch {
    // Fall back to the last balanced object in the stream.
  }
  let depth = 0;
  let start = -1;
  let last = null;
  let inString = false;
  let escaped = false;
  for (let index = 0; index < text.length; index += 1) {
    const char = text[index];
    if (inString) {
      if (escaped) escaped = false;
      else if (char === "\\") escaped = true;
      else if (char === '"') inString = false;
      continue;
    }
    if (char === '"') inString = true;
    else if (char === "{") {
      if (depth === 0) start = index;
      depth += 1;
    } else if (char === "}") {
      depth -= 1;
      if (depth === 0 && start >= 0) {
        last = text.slice(start, index + 1);
        start = -1;
      }
    }
  }
  if (!last) throw new Error(`工具输出不是 JSON：${text.slice(0, 200)}`);
  return JSON.parse(last);
}

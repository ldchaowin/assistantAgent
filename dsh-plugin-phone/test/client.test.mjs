import assert from "node:assert/strict";
import http from "node:http";
import test from "node:test";

import { createPhoneClient, PhoneServiceError } from "../lib/client.js";

function start(handler) {
  return new Promise((resolve) => {
    const server = http.createServer(handler);
    server.listen(0, "127.0.0.1", () => resolve(server));
  });
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

// Delayed/aborted responses can leave sockets open, and server.close() would
// then wait for them forever.
function stop(server) {
  if (typeof server.closeAllConnections === "function") server.closeAllConnections();
  return new Promise((resolve) => server.close(() => resolve()));
}

function later(res, body, delay) {
  const timer = setTimeout(() => {
    if (res.writableEnded || res.destroyed) return;
    res.setHeader("content-type", "application/json");
    res.end(JSON.stringify(body));
  }, delay);
  timer.unref?.();
  res.on("close", () => clearTimeout(timer));
  res.on("error", () => {});
}

test("client sends auth and parses find/click/screenshot", async () => {
  const seen = [];
  const server = await start((req, res) => {
    let body = "";
    req.on("data", (chunk) => (body += chunk));
    req.on("end", () => {
      seen.push({ url: req.url, auth: req.headers.authorization, body });
      res.setHeader("content-type", "application/json");
      if (req.url === "/find") {
        res.end(
          JSON.stringify({
            ok: true,
            found: true,
            keyword: "设置",
            match: {
              text: "设置",
              score: 0.99,
              device_center: [10, 20],
              box: [[0, 0], [1, 0], [1, 1], [0, 1]],
            },
            frame: [100, 200],
            screen: [100, 200],
            screenshot_path: null,
            elapsed_ms: 1.5,
            items: 1,
          }),
        );
      } else if (req.url === "/tap") {
        res.end(JSON.stringify({ ok: true, action: "tap", response: { id: 1, ok: true } }));
      } else if (req.url === "/screenshot") {
        res.end(
          JSON.stringify({
            ok: true,
            path: "/tmp/shot.png",
            width: 50,
            height: 100,
            bytes: 123,
          }),
        );
      } else {
        res.statusCode = 404;
        res.end(JSON.stringify({ ok: false, error: { code: "NOT_FOUND", message: req.url } }));
      }
    });
  });
  const { port } = server.address();
  const client = createPhoneClient({ baseUrl: `http://127.0.0.1:${port}`, token: "secret" });
  try {
    const found = await client.find({ keyword: "设置" });
    assert.equal(found.found, true);
    assert.equal(seen[0].auth, "Bearer secret");
    assert.match(seen[0].body, /"text":"设置"/);

    const click = await client.click(10, 20);
    assert.equal(click.response.ok, true);

    const shot = await client.screenshot({ maxWidth: 50, includeBase64: false });
    assert.equal(shot.path, "/tmp/shot.png");
    assert.equal(shot.width, 50);
  } finally {
    server.close();
  }
});

test("client maps HTTP errors to PhoneServiceError", async () => {
  const server = await start((req, res) => {
    res.statusCode = 401;
    res.setHeader("content-type", "application/json");
    res.end(JSON.stringify({ ok: false, error: { code: "UNAUTHORIZED", message: "bad token" } }));
  });
  const { port } = server.address();
  const client = createPhoneClient({ baseUrl: `http://127.0.0.1:${port}`, token: "wrong" });
  try {
    await assert.rejects(
      () => client.status(),
      (error) => error instanceof PhoneServiceError && error.code === "UNAUTHORIZED" && error.status === 401,
    );
  } finally {
    server.close();
  }
});

test("client posts /shutdown so the supervisor can restart the service", async () => {
  const seen = [];
  const server = await start((req, res) => {
    seen.push({ method: req.method, url: req.url });
    res.setHeader("content-type", "application/json");
    res.end(JSON.stringify({ ok: true, shutting_down: true }));
  });
  const { port } = server.address();
  const client = createPhoneClient({ baseUrl: `http://127.0.0.1:${port}` });
  try {
    const payload = await client.shutdown();
    assert.equal(payload.shutting_down, true);
    assert.equal(seen[0].method, "POST");
    assert.equal(seen[0].url, "/shutdown");
  } finally {
    server.close();
  }
});

test("client health exposes the capture probe", async () => {
  const server = await start((req, res) => {
    res.setHeader("content-type", "application/json");
    res.end(
      JSON.stringify({
        ok: true,
        service: "phone",
        capture: { name: "scrcpy:default", degraded: true, age_ms: 61000, reason: "last frame is 61.0s old" },
      }),
    );
  });
  const { port } = server.address();
  const client = createPhoneClient({ baseUrl: `http://127.0.0.1:${port}` });
  try {
    const payload = await client.health();
    assert.equal(payload.capture.degraded, true);
    assert.equal(payload.capture.age_ms, 61000);
  } finally {
    server.close();
  }
});

test("client default timeout is 30s", async () => {
  const original = AbortSignal.timeout;
  const seen = [];
  AbortSignal.timeout = (ms) => {
    seen.push(ms);
    return original.call(AbortSignal, ms);
  };
  const server = await start((req, res) => {
    res.setHeader("content-type", "application/json");
    res.end(JSON.stringify({ ok: true }));
  });
  const { port } = server.address();
  const client = createPhoneClient({ baseUrl: `http://127.0.0.1:${port}` });
  try {
    await client.health();
    assert.deepEqual(seen, [30000]);
  } finally {
    AbortSignal.timeout = original;
    await stop(server);
  }
});

test("client retries an idempotent request once after a timeout", async () => {
  let calls = 0;
  const server = await start((req, res) => {
    calls += 1;
    if (calls === 1) {
      // Stall past the client timeout, so the first attempt aborts.
      later(res, { ok: true, attempt: 1 }, 300);
      return;
    }
    res.setHeader("content-type", "application/json");
    res.end(JSON.stringify({ ok: true, attempt: calls }));
  });
  const { port } = server.address();
  const client = createPhoneClient({ baseUrl: `http://127.0.0.1:${port}`, timeoutMs: 60 });
  try {
    const payload = await client.status();
    assert.equal(payload.ok, true);
    assert.equal(payload.attempt, 2, "the retry's response must win");
    assert.equal(calls, 2);
  } finally {
    await stop(server);
  }
});

test("client throws UNREACHABLE after the single retry is exhausted", async () => {
  let calls = 0;
  const server = await start((req, res) => {
    calls += 1;
    later(res, { ok: true }, 300);
  });
  const { port } = server.address();
  const client = createPhoneClient({ baseUrl: `http://127.0.0.1:${port}`, timeoutMs: 50 });
  try {
    await assert.rejects(
      () => client.status(),
      (error) => {
        assert.ok(error instanceof PhoneServiceError);
        assert.equal(error.name, "PhoneServiceError");
        assert.equal(error.code, "UNREACHABLE");
        assert.equal(error.status, 0);
        assert.match(
          error.message,
          /^UNREACHABLE: phone service is not reachable at http:\/\/127\.0\.0\.1:\d+: /,
        );
        return true;
      },
    );
    assert.equal(calls, 2, "exactly one retry");
  } finally {
    await stop(server);
  }
});

test("client does not retry a mutating call", async () => {
  let calls = 0;
  const server = await start((req, res) => {
    calls += 1;
    later(res, { ok: true }, 400);
  });
  const { port } = server.address();
  const client = createPhoneClient({ baseUrl: `http://127.0.0.1:${port}`, timeoutMs: 50 });
  try {
    await assert.rejects(() => client.click(10, 20), (error) => error.code === "UNREACHABLE");
    assert.equal(calls, 1);
    // A retry would fire after ~300ms; wait past that to prove tap stays single-shot.
    await sleep(450);
    assert.equal(calls, 1, "tap must not be retried");
  } finally {
    await stop(server);
  }
});

test("client retries screenshot once when the service is busy", async () => {
  let calls = 0;
  const server = await start((req, res) => {
    calls += 1;
    res.setHeader("content-type", "application/json");
    if (calls === 1) {
      res.statusCode = 409;
      res.end(JSON.stringify({ ok: false, error: { code: "BUSY", message: "capture lock is busy" } }));
      return;
    }
    res.end(JSON.stringify({ ok: true, path: "/tmp/shot.png", width: 60, height: 120, bytes: 42 }));
  });
  const { port } = server.address();
  const client = createPhoneClient({ baseUrl: `http://127.0.0.1:${port}` });
  try {
    const payload = await client.screenshot({ includeBase64: true, maxWidth: 120 });
    assert.equal(payload.path, "/tmp/shot.png");
    assert.equal(calls, 2);
  } finally {
    await stop(server);
  }
});

test("client retries BUSY only once for screenshot", async () => {
  let calls = 0;
  const server = await start((req, res) => {
    calls += 1;
    res.statusCode = 409;
    res.setHeader("content-type", "application/json");
    res.end(JSON.stringify({ ok: false, error: { code: "BUSY", message: "capture lock is busy" } }));
  });
  const { port } = server.address();
  const client = createPhoneClient({ baseUrl: `http://127.0.0.1:${port}` });
  try {
    await assert.rejects(
      () => client.screenshot(),
      (error) => error instanceof PhoneServiceError && error.code === "BUSY" && error.status === 409,
    );
    assert.equal(calls, 2, "exactly one BUSY retry");
  } finally {
    await stop(server);
  }
});

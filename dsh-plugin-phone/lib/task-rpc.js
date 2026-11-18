/**
 * Host RPC channel for phone tasks. The client dock calls this channel through
 * ctx.connection.rpc.call("phone-task", endpoint, payload).
 */
import { PLUGIN_VERSION } from "./version.js";

export const PHONE_TASK_CHANNEL = "/dsh-phone-task-v3";

export const PHONE_TASK_ENDPOINTS = Object.freeze({
  info: "phone.task.info",
  preflight: "phone.task.preflight",
  start: "phone.task.start",
  status: "phone.task.status",
  stop: "phone.task.stop",
  history: "phone.task.history",
  deleteHistory: "phone.task.deleteHistory",
  clearHistory: "phone.task.clearHistory",
  templates: "phone.task.templates",
  saveTemplate: "phone.task.saveTemplate",
  deleteTemplate: "phone.task.deleteTemplate",
  renameTemplate: "phone.task.renameTemplate",
  templateFile: "phone.task.templateFile",
  saveTemplateFile: "phone.task.saveTemplateFile",
  runTemplate: "phone.task.runTemplate",
  templateReview: "phone.task.templateReview",
  applyTemplateReview: "phone.task.applyTemplateReview",
  rollbackTemplate: "phone.task.rollbackTemplate",
  schedule: "phone.task.schedule",
  updateSchedule: "phone.task.updateSchedule",
  schedules: "phone.task.schedules",
  cancelSchedule: "phone.task.cancelSchedule",
  replay: "phone.task.replay",
  devices: "phone.task.devices",
  provisionDevices: "phone.provision.devices",
  provisionStart: "phone.provision.start",
  provisionStatus: "phone.provision.status",
  provisionArm: "phone.provision.arm",
  deviceStatus: "phone.device.status",
  reconnectAdb: "phone.device.reconnectAdb",
  deviceThumbnail: "phone.device.thumbnail",
});

function ok(value) {
  return { ok: true, value };
}

function fail(error) {
  return {
    ok: false,
    error: {
      code: error?.code || "TASK_ERROR",
      message: error?.message || String(error),
      details: error?.details ?? {},
    },
  };
}

export function createTaskRpcHandler(service, provisionService, deviceService) {
  return async function handle(endpoint, payload = {}) {
    try {
      switch (endpoint) {
        case PHONE_TASK_ENDPOINTS.info:
          // Lets the client detect a host that is still running an older plugin.
          return ok({ version: PLUGIN_VERSION, channel: PHONE_TASK_CHANNEL });
        case PHONE_TASK_ENDPOINTS.preflight:
          return ok(await service.preflight(payload));
        case PHONE_TASK_ENDPOINTS.start:
          return ok(await service.start(payload));
        case PHONE_TASK_ENDPOINTS.status:
          return ok(service.status(payload?.taskId));
        case PHONE_TASK_ENDPOINTS.stop:
          return ok(await service.stop(payload?.taskId));
        case PHONE_TASK_ENDPOINTS.history:
          return ok(service.history());
        case PHONE_TASK_ENDPOINTS.deleteHistory:
          return ok(await service.deleteHistory(payload?.id));
        case PHONE_TASK_ENDPOINTS.clearHistory:
          return ok(await service.clearHistory());
        case PHONE_TASK_ENDPOINTS.templates:
          return ok(service.templates());
        case PHONE_TASK_ENDPOINTS.saveTemplate:
          return ok(await service.saveTemplate(payload));
        case PHONE_TASK_ENDPOINTS.deleteTemplate:
          return ok(await service.deleteTemplate(payload?.id));
        case PHONE_TASK_ENDPOINTS.renameTemplate:
          return ok(await service.renameTemplate(payload));
        case PHONE_TASK_ENDPOINTS.templateFile:
          return ok(await service.readTemplateFile(payload));
        case PHONE_TASK_ENDPOINTS.saveTemplateFile:
          return ok(await service.saveTemplateFile(payload));
        case PHONE_TASK_ENDPOINTS.runTemplate:
          return ok(await service.runTemplate(payload));
        case PHONE_TASK_ENDPOINTS.templateReview:
          return ok(await service.reviewTemplate(payload));
        case PHONE_TASK_ENDPOINTS.applyTemplateReview:
          return ok(await service.applyTemplateReview(payload));
        case PHONE_TASK_ENDPOINTS.rollbackTemplate:
          return ok(await service.rollbackTemplate(payload));
        case PHONE_TASK_ENDPOINTS.schedule:
          return ok(await service.schedule(payload));
        case PHONE_TASK_ENDPOINTS.updateSchedule:
          return ok(await service.updateSchedule(payload));
        case PHONE_TASK_ENDPOINTS.schedules:
          return ok(service.schedules());
        case PHONE_TASK_ENDPOINTS.cancelSchedule:
          return ok(await service.cancelSchedule(payload?.id));
        case PHONE_TASK_ENDPOINTS.replay:
          return ok(await service.replay(payload?.id));
        case PHONE_TASK_ENDPOINTS.devices:
          return ok(service.devices());
        case PHONE_TASK_ENDPOINTS.provisionDevices:
          if (!provisionService) throw { code: "UNAVAILABLE", message: "provisioning is not configured" };
          return ok(await provisionService.devices(payload));
        case PHONE_TASK_ENDPOINTS.provisionStart:
          if (!provisionService) throw { code: "UNAVAILABLE", message: "provisioning is not configured" };
          return ok(await provisionService.start(payload));
        case PHONE_TASK_ENDPOINTS.provisionArm:
          if (!provisionService) throw { code: "UNAVAILABLE", message: "provisioning is not configured" };
          return ok(await provisionService.arm(payload));
        case PHONE_TASK_ENDPOINTS.provisionStatus:
          if (!provisionService) throw { code: "UNAVAILABLE", message: "provisioning is not configured" };
          return ok(await provisionService.status(payload));
        case PHONE_TASK_ENDPOINTS.reconnectAdb:
          return ok(await service.reconnectAdb(payload));
        case PHONE_TASK_ENDPOINTS.deviceStatus:
          if (!deviceService) throw { code: "UNAVAILABLE", message: "device status is not configured" };
          return ok(await deviceService.status(payload));
        case PHONE_TASK_ENDPOINTS.deviceThumbnail:
          if (!deviceService) throw { code: "UNAVAILABLE", message: "device status is not configured" };
          return ok(await deviceService.thumbnail(payload));
        default:
          return fail({
            code: "NOT_FOUND",
            message:
              `unknown phone task endpoint: ${endpoint}. ` +
              `Host plugin is ${PLUGIN_VERSION}; if the client is newer, restart DSH Desktop.`,
          });
      }
    } catch (error) {
      return fail(error);
    }
  };
}

function mountWebRoute(ctx, handler, logger) {
  const webServer = ctx?.webServer;
  if (!webServer || typeof webServer.register !== "function") return null;
  const route = {
    kind: "prefix",
    path: PHONE_TASK_CHANNEL,
    handler: async (req, res) => {
      let rejection;
      if (typeof ctx.connection?.requestRejection === "function") {
        try {
          rejection = ctx.connection.requestRejection.call(ctx.connection, req);
        } catch {
          rejection = 403;
        }
      } else {
        const host = String(req.headers?.host || "");
        const loopback = /^(127\.0\.0\.1|localhost|\[::1\])(:\d+)?$/.test(host);
        if (!loopback || req.headers?.["sec-fetch-site"] === "cross-site") rejection = 403;
      }
      if (rejection !== undefined) {
        res.writeHead(rejection, { "content-type": "text/plain; charset=utf-8" });
        res.end(rejection === 401 ? "unauthorized" : "forbidden");
        return;
      }
      if (req.method !== "POST") {
        res.writeHead(405, { "content-type": "text/plain; charset=utf-8" });
        res.end("method not allowed");
        return;
      }
      const pathname = new URL(req.url || "/", "http://localhost").pathname;
      const prefix = `${PHONE_TASK_CHANNEL}/`;
      if (!pathname.startsWith(prefix)) {
        res.writeHead(404, { "content-type": "text/plain; charset=utf-8" });
        res.end("not found");
        return;
      }
      const endpoint = decodeURIComponent(pathname.slice(prefix.length));
      const chunks = [];
      let size = 0;
      for await (const chunk of req) {
        size += chunk.length;
        if (size > 1024 * 1024) {
          res.writeHead(413, { "content-type": "text/plain; charset=utf-8" });
          res.end("payload too large");
          return;
        }
        chunks.push(chunk);
      }
      let payload = {};
      try {
        const text = Buffer.concat(chunks).toString("utf8");
        payload = text ? JSON.parse(text) : {};
      } catch {
        res.writeHead(400, { "content-type": "text/plain; charset=utf-8" });
        res.end("body is not JSON");
        return;
      }
      const result = await handler(endpoint, payload);
      res.writeHead(200, { "content-type": "application/json" });
      res.end(JSON.stringify(result));
    },
  };
  const registered = webServer.register(route);
  logger.info?.("dsh-plugin-phone: task web route mounted at /phone-task");
  if (typeof registered === "function") return registered;
  if (registered && typeof registered.then === "function") {
    return () => {
      registered.then((dispose) => dispose?.()).catch(() => {});
    };
  }
  return () => {};
}

export function installTaskRpc(
  ctx,
  service,
  logger = console,
  provisionService = null,
  deviceService = null,
) {
  const handler = createTaskRpcHandler(service, provisionService, deviceService);
  const disposers = [];
  const webDispose = mountWebRoute(ctx, handler, logger);
  if (webDispose) {
    disposers.push(webDispose);
  } else if (ctx?.connection?.rpc?.handle) {
    // Older DSH builds: the connection RPC is the only transport. Newer builds
    // throw "cannot get property webServer without inject" from this path, so
    // never call it when the web route mounted.
    try {
      disposers.push(ctx.connection.rpc.handle(PHONE_TASK_CHANNEL, handler, { authority: "loopback" }));
    } catch (error) {
      logger.warn?.(`dsh-plugin-phone: connection RPC unavailable: ${error.message}`);
    }
  }
  if (disposers.length === 0) {
    logger.warn?.("dsh-plugin-phone: no task transport available; host task RPC disabled");
  }
  return () => {
    for (const dispose of disposers) {
      try {
        dispose?.();
      } catch (_) {}
    }
  };
}

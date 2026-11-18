/**
 * dsh-plugin-phone: expose the local phone service to the DSH model and host a
 * task lifecycle RPC for the composer dock.
 *
 * The service runs separately (`phone serve ...`) and owns the ESP32-S3 serial
 * connection. This plugin is a thin HTTP client; it never imports the Python
 * console or touches the device directly.
 */
import { defineTool } from "@deepseek-ai/dsh-tools";
import { createPhoneClient } from "./client.js";
import { createToolDefinitions } from "./tools.js";
import { createTaskService } from "./task-service.js";
import { installTaskRpc } from "./task-rpc.js";
import { createProvisionService } from "./provision-service.js";
import { createDeviceService } from "./device-service.js";
import { createServiceSupervisor } from "./service-supervisor.js";

export const name = "dsh-plugin-phone";
export const inject = ["tools", "connection", "agents", "webServer"];

export function apply(ctx, config = {}) {
  const clientFactory = (options = {}) =>
    createPhoneClient({
      baseUrl: options.baseUrl ?? config.baseUrl,
      token: options.token ?? config.token,
      tokenFile: options.tokenFile ?? config.tokenFile,
      timeoutMs: options.timeoutMs ?? config.timeoutMs,
    });

  const client = clientFactory({});
  const logger = ctx.logger?.(name) ?? console;
  const supervisor = createServiceSupervisor({ client, config, logger });
  // Relative paths reported by the service resolve against the service process
  // cwd (serviceCwd); fall back to the task workspace, then this process.
  const pathRoot = config.serviceCwd || config.workspace || process.cwd();
  for (const tool of createToolDefinitions(defineTool, client, { workspace: pathRoot })) {
    ctx.tools.register(tool);
  }

  const taskService = createTaskService({
    clientFactory,
    agents: ctx.agents,
    // Subscribe to session/event so panel task runs leave a trace
    // (synchronous session.events reads are deprecated in this DSH version).
    events: ctx,
    config,
    logger,
  });
  const provisionService = createProvisionService({ clientFactory, logger });
  const deviceService = createDeviceService({ clientFactory, config, logger });
  const disposeRpc = installTaskRpc(ctx, taskService, logger, provisionService, deviceService);
  ctx.effect?.(() => () => {
    supervisor.dispose();
    disposeRpc?.();
    taskService.dispose();
  });

  logger?.info?.(`phone tools registered against ${client.baseUrl}; task RPC on ${config.taskChannel || "phone-task"}`);
  return { taskService, provisionService, deviceService, supervisor };
}

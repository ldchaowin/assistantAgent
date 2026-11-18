/**
 * Keep the local phone service alive.
 *
 * The DSH host process is restarted often (updates, crashes, profile reloads),
 * and its children die with it. This supervisor pings /health and, when the
 * service is down, spawns the configured command detached so it survives the
 * plugin lifetime. It re-checks periodically and after plugin reloads.
 */
import fs from "node:fs";
import { spawn } from "node:child_process";

function quote(value) {
  return /^[A-Za-z0-9_./:=@+-]+$/.test(value) ? value : JSON.stringify(value);
}

export function createServiceSupervisor({
  client,
  config = {},
  logger = console,
  spawnImpl = spawn,
  sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms)),
  now = () => Date.now(),
  autoStart = true,
} = {}) {
  const command = Array.isArray(config.serviceCommand)
    ? config.serviceCommand.map(String)
    : null;
  const enabled = Boolean(
    config.autoStartService && command && command.length > 0
  );
  const intervalMs = Math.max(2000, Number(config.serviceHealthIntervalMs || 20000));
  const cwd = config.serviceCwd || process.cwd();
  const startupTimeoutMs = Math.max(1000, Number(config.serviceStartupTimeoutMs || 15000));
  let starting = false;
  let timer = null;
  let lastStartAt = 0;
  let lastRestartAt = 0;
  let lastError = null;
  let degradedRestarts = 0;

  async function probe() {
    try {
      const payload = await client.health();
      const capture = payload && typeof payload === "object" ? payload.capture : null;
      const degraded = Boolean(capture && capture.degraded);
      return {
        alive: true,
        degraded,
        reason: degraded ? String(capture.reason || "capture is stale") : null,
      };
    } catch (_) {
      return { alive: false, degraded: false, reason: null };
    }
  }

  function degradedCooldownMs() {
    const base = Math.max(1000, Number(config.serviceDegradedBackoffMs || 120000));
    const cap = Math.max(base, Number(config.serviceDegradedBackoffMaxMs || 900000));
    const step = Math.min(10, Math.max(0, degradedRestarts - 1));
    return Math.min(cap, base * 2 ** step);
  }

  async function stopService() {
    try {
      await client.shutdown?.();
    } catch (_) {
      // Best effort: the service may already be gone or expose no /shutdown.
    }
    const timeoutMs = Math.max(0, Number(config.serviceShutdownTimeoutMs || 5000));
    const deadline = now() + timeoutMs;
    while (now() < deadline) {
      await sleep(200);
      if (!(await probe()).alive) return true;
    }
    return false;
  }

  async function ensure() {
    if (!enabled) return false;
    const state = await probe();
    if (state.alive && !state.degraded) {
      degradedRestarts = 0;
      return true;
    }
    if (starting) return false;
    starting = true;
    try {
      if (state.alive && state.degraded) {
        // /health says the service process is fine but its eye is blind: the
        // capture stream stopped producing frames. A restart re-opens adb.
        if (config.serviceRestartOnDegraded === false) return false;
        if (lastRestartAt && now() - lastRestartAt < degradedCooldownMs()) {
          return false;
        }
        degradedRestarts += 1;
        lastRestartAt = now();
        logger.warn?.(
          `dsh-plugin-phone: ${state.reason}; restarting the local service`
        );
        const stopped = await stopService();
        if (!stopped) {
          lastError = "service did not stop for a restart";
          logger.warn?.(`dsh-plugin-phone: ${lastError}`);
          return false;
        }
      }
      lastStartAt = now();
      lastError = null;
      const line = command.map(quote).join(" ");
      logger.info?.(`dsh-plugin-phone: local service down, starting: ${line}`);
      let stdio = "ignore";
      let logFd = null;
      if (config.serviceLog) {
        try {
          logFd = fs.openSync(config.serviceLog, "a");
          stdio = ["ignore", logFd, logFd];
        } catch (error) {
          logger.warn?.(`dsh-plugin-phone: cannot open service log: ${error.message}`);
          stdio = "ignore";
        }
      }
      const child = spawnImpl(command[0], command.slice(1), {
        cwd,
        detached: true,
        stdio,
        env: { ...process.env, ...(config.serviceEnv || {}) },
      });
      child.on?.("error", (error) => {
        lastError = error.message;
        logger.warn?.(`dsh-plugin-phone: service spawn failed: ${error.message}`);
      });
      child.unref?.();

      const deadline = now() + startupTimeoutMs;
      while (now() < deadline) {
        await sleep(500);
        if ((await probe()).alive) {
          logger.info?.("dsh-plugin-phone: local service is healthy");
          return true;
        }
      }
      lastError = "service did not become healthy";
      logger.warn?.("dsh-plugin-phone: local service did not become healthy");
      return false;
    } catch (error) {
      lastError = error.message;
      logger.warn?.(`dsh-plugin-phone: service start failed: ${error.message}`);
      return false;
    } finally {
      starting = false;
    }
  }

  if (enabled && autoStart) {
    ensure().catch(() => {});
    timer = setInterval(() => ensure().catch(() => {}), intervalMs);
    timer.unref?.();
  }

  return {
    enabled,
    ensure,
    dispose() {
      if (timer) clearInterval(timer);
      timer = null;
    },
    get starting() {
      return starting;
    },
    get lastStartAt() {
      return lastStartAt;
    },
    get lastError() {
      return lastError;
    },
    get degradedRestarts() {
      return degradedRestarts;
    },
  };
}

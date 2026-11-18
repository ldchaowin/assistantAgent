/**
 * BLE provisioning proxy: the browser panel calls the host RPC, the host calls
 * the local phone service (`/provision/devices`, `/provision`, `/provision/status`).
 */
function provisionError(code, message) {
  const error = new Error(message);
  error.code = code;
  return error;
}

function friendlyError(error) {
  if (error && error.code === "UNREACHABLE") {
    return provisionError(
      "SERVICE_DOWN",
      "本地 phone 服务未启动（127.0.0.1:8765）。请在终端运行："
        + ".venv/bin/phone --tcp <ESP32_IP> serve --scrcpy",
    );
  }
  return error;
}

export function createProvisionService({ clientFactory, logger = console } = {}) {
  if (typeof clientFactory !== "function") {
    throw new Error("clientFactory is required");
  }
  const client = () => clientFactory({});

  return {
    async devices(payload = {}) {
      const timeout = Number(payload.timeout || 8);
      try {
        const result = await client().provisionDevices({ timeout });
        return { devices: result?.devices || [] };
      } catch (error) {
        logger.warn?.(`dsh-plugin-phone: BLE scan failed: ${error.message}`);
        const mapped = friendlyError(error);
        throw provisionError(mapped.code || "PROVISION_UNAVAILABLE", mapped.message);
      }
    },

    async start(payload = {}) {
      const ssid = String(payload.ssid || "").trim();
      const password = String(payload.password || "");
      if (!ssid) throw provisionError("BAD_REQUEST", "WiFi SSID is required");
      if (!password) throw provisionError("BAD_REQUEST", "WiFi password is required");
      try {
        return await client().startProvision({
          ssid,
          password,
          device: payload.device ? String(payload.device) : undefined,
          pop: payload.pop ? String(payload.pop) : undefined,
          timeout: Number(payload.timeout || 180),
        });
      } catch (error) {
        logger.warn?.(`dsh-plugin-phone: provisioning start failed: ${error.message}`);
        const mapped = friendlyError(error);
        throw provisionError(mapped.code || "PROVISION_FAILED", mapped.message);
      }
    },

    async arm() {
      try {
        return await client().armProvisioning();
      } catch (error) {
        throw provisionError(error.code || "PROVISION_FAILED", error.message);
      }
    },

    async status() {
      try {
        const status = await client().provisionStatus();
        if (status && status.state === "done") {
          try {
            const full = await client().status();
            status.wifi = full?.status?.wifi || full?.wifi || null;
          } catch (_) {
            status.wifi = null;
          }
        }
        return status;
      } catch (error) {
        const mapped = friendlyError(error);
        throw provisionError(mapped.code || "PROVISION_UNAVAILABLE", mapped.message);
      }
    },
  };
}

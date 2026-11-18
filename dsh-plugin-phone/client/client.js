/**
 * dsh-plugin-phone client UI: a natural-language phone task dock above the
 * composer. It creates an independent task session, sends a structured
 * observe-act prompt, and tracks start/stop/scheduled state.
 *
 * Plain JS on purpose: DSH's client module loader loads this file directly;
 * no build step is required.
 */
window.__ModuleLoader__.load({
  id: "dsh-plugin-phone",
  factory: (require) => {
    var module = { exports: {} };
    var exports = module.exports;
    const React = require("react");

    const STORAGE_KEY = "dsh-plugin-phone/tasks/v1";
    const MAX_HISTORY = 20;
    const MAX_STEPS = 40;
    const MAX_MINUTES = 24 * 60;

    const DEFAULT_TEMPLATES = [
      { label: "打开设置并连接 WLAN", text: "打开设置，找到 WLAN，连接可用的公司 Wi-Fi" },
      { label: "京东价保", text: "打开京东，进入我的 → 客户服务 → 价格保护，尝试一键价保" },
      { label: "游戏日常", text: "打开游戏，完成每日任务并领取奖励" },
      { label: "截图并描述页面", text: "截取当前手机屏幕，并描述当前页面是什么" },
      { label: "微信发测试消息", text: "打开微信，给文件传输助手发送一条测试消息" }
    ];
    // Keep in sync with package.json; the host reports its own version so a
    // stale host (common after a plugin edit without restart) is visible.
    const CLIENT_VERSION = "0.14.4";

    function staleHostHint(hostVersion) {
      if (!hostVersion) {
        return `插件后台没有响应版本探测，很可能仍在跑旧版本（client ${CLIENT_VERSION}）。完全退出 DSH Desktop 再打开。`;
      }
      if (hostVersion !== CLIENT_VERSION) {
        return `插件后台是 ${hostVersion}，页面是 ${CLIENT_VERSION}。完全退出 DSH Desktop 再打开。`;
      }
      return "";
    }

    let templates = [...DEFAULT_TEMPLATES];

    function newId() {
      try {
        if (globalThis.crypto && typeof globalThis.crypto.randomUUID === "function") {
          return globalThis.crypto.randomUUID();
        }
      } catch (_) {}
      return `task-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
    }

    function loadState() {
      try {
        const raw = globalThis.localStorage && globalThis.localStorage.getItem(STORAGE_KEY);
        if (raw) {
          const parsed = JSON.parse(raw);
          if (parsed && typeof parsed === "object") {
            return {
              active: parsed.active || null,
              history: Array.isArray(parsed.history) ? parsed.history.slice(0, MAX_HISTORY) : [],
              hostHistory: Array.isArray(parsed.hostHistory) ? parsed.hostHistory : [],
              devices: Array.isArray(parsed.devices) ? parsed.devices : [],
              device: parsed.device || "default",
              scheduled: Array.isArray(parsed.scheduled) ? parsed.scheduled : []
            };
          }
        }
      } catch (_) {}
      return { active: null, history: [], hostHistory: [], devices: [], device: "default", scheduled: [] };
    }

    let state = loadState();
    let scheduleRevision = 0;
    const listeners = new Set();
    const scheduleTimers = new Map();
    const watchers = new Map();
    const hostPollers = new Map();

    function getState() {
      return state;
    }

    function persist() {
      try {
        if (globalThis.localStorage) {
          globalThis.localStorage.setItem(STORAGE_KEY, JSON.stringify(state));
        }
      } catch (_) {}
    }

    function setState(patch) {
      state = Object.assign({}, state, patch);
      persist();
      listeners.forEach((fn) => fn());
    }

    function subscribe(fn) {
      listeners.add(fn);
      return () => listeners.delete(fn);
    }

    function upsertHistory(record) {
      const history = [record]
        .concat(state.history.filter((item) => item.id !== record.id))
        .slice(0, MAX_HISTORY);
      setState({ history });
    }

    function updateTask(taskId, patch) {
      const active =
        state.active && state.active.id === taskId
          ? Object.assign({}, state.active, patch)
          : state.active;
      const history = state.history.map((item) =>
        item.id === taskId ? Object.assign({}, item, patch) : item
      );
      setState({ active, history });
    }

    function finishTask(taskId, status, error) {
      updateTask(taskId, {
        status,
        error: error || null,
        finishedAt: Date.now()
      });
      const watcher = watchers.get(taskId);
      if (watcher) {
        watcher.cleanup();
        watchers.delete(taskId);
      }
      const hostPoller = hostPollers.get(taskId);
      if (hostPoller) {
        clearInterval(hostPoller);
        hostPollers.delete(taskId);
      }
    }

    function normalizeSessionId(result) {
      if (typeof result === "string") return result;
      if (result && result.value && result.value.sessionId) return result.value.sessionId;
      if (result && result.sessionId) return result.sessionId;
      return null;
    }

    function getSessions(ctx) {
      try {
        if (ctx && typeof ctx.get === "function") {
          const sessions = ctx.get("sessions");
          if (sessions) return sessions;
        }
      } catch (_) {}
      return ctx ? ctx.sessions : null;
    }

    function getConnection(ctx) {
      try {
        if (ctx && typeof ctx.get === "function") {
          const connection = ctx.get("connection");
          if (connection) return connection;
        }
      } catch (_) {}
      return ctx ? ctx.connection : null;
    }

    function hostTaskError(result) {
      const error = new Error(result?.error?.message || "host task RPC failed");
      error.code = result?.error?.code || "HOST_RPC_FAILED";
      error.details = result?.error?.details;
      return error;
    }

    async function callHostTask(ctx, endpoint, payload = {}, options = {}) {
      // Preferred transport: the host's /phone-task web route. It is same-origin
      // and carries the DSH browser session, so it also works on remote/LAN UIs.
      if (typeof fetch === "function" && typeof location !== "undefined") {
        try {
          const init = {
            method: "POST",
            credentials: "same-origin",
            headers: { "content-type": "application/json" },
            body: JSON.stringify(payload || {})
          };
          if (
            options &&
            options.timeoutMs &&
            typeof AbortSignal !== "undefined" &&
            typeof AbortSignal.timeout === "function"
          ) {
            init.signal = AbortSignal.timeout(options.timeoutMs);
          }
          const response = await fetch(`/dsh-phone-task-v3/${encodeURIComponent(endpoint)}`, init);
          const text = await response.text();
          let result = null;
          try {
            result = text ? JSON.parse(text) : null;
          } catch (_) {}
          if (result && typeof result.ok === "boolean") {
            if (!result.ok) throw hostTaskError(result);
            return result.value;
          }
          if (response.status !== 404 && response.status !== 401 && response.status !== 403) {
            throw new Error(`host task HTTP ${response.status}`);
          }
        } catch (error) {
          if (error.code && error.code !== "HOST_RPC_UNAVAILABLE") throw error;
        }
      }
      const connection = getConnection(ctx);
      if (!connection?.rpc?.call) {
        const error = new Error("host task RPC unavailable");
        error.code = "HOST_RPC_UNAVAILABLE";
        throw error;
      }
      let result;
      try {
        result = await connection.rpc.call("/dsh-phone-task-v3", endpoint, payload);
      } catch (error) {
        const unavailable = new Error("host task RPC unavailable");
        unavailable.code = "HOST_RPC_UNAVAILABLE";
        throw unavailable;
      }
      if (!result || result.ok === false) throw hostTaskError(result);
      return result.value;
    }

    function sessionFaceOf(ctx, sessionId) {
      if (!sessionId) return null;
      const sessions = getSessions(ctx);
      if (!sessions) return null;
      try {
        if (typeof sessions.binding === "function") {
          const binding = sessions.binding(sessionId);
          if (binding && binding.session) return binding.session;
        }
      } catch (_) {}
      try {
        if (typeof sessions.get === "function") {
          const record = sessions.get(sessionId);
          if (record && record.session) return record.session;
          if (record && typeof record.prompt === "function") return record;
        }
      } catch (_) {}
      try {
        if (sessions.manager && typeof sessions.manager.get === "function") {
          const record = sessions.manager.get(sessionId);
          if (record && record.session) return record.session;
          if (record && typeof record.prompt === "function") return record;
        }
      } catch (_) {}
      return null;
    }

    function describe(value) {
      if (!value) return null;
      return {
        type: typeof value,
        keys: Object.keys(value),
        own: Object.getOwnPropertyNames(value),
        proto: Object.getOwnPropertyNames(Object.getPrototypeOf(value))
      };
    }

    function sessionsDebug(ctx) {
      const sessions = getSessions(ctx);
      const info = {
        sessions: describe(sessions),
        manager: sessions ? describe(sessions.manager) : null,
        list: sessions ? describe(sessions.list) : null
      };
      try {
        globalThis.localStorage.setItem(
          "dsh-plugin-phone/debug/sessions",
          JSON.stringify(info)
        );
      } catch (_) {}
      return info;
    }

    function getUiWorkspace(ctx) {
      try {
        if (ctx && typeof ctx.get === "function") {
          const service = ctx.get("uiWorkspace");
          if (service) return service;
        }
      } catch (_) {}
      return ctx ? ctx.uiWorkspace : null;
    }

    function openSession(ctx, sessionId) {
      if (!sessionId) return false;
      // Current DSH: navigation belongs to the Workspace view owner. The
      // sessions service only exposes catalog/reference operations.
      const uiWorkspace = getUiWorkspace(ctx);
      if (uiWorkspace && typeof uiWorkspace.openSession === "function") {
        try {
          uiWorkspace.openSession(sessionId);
          return true;
        } catch (_) {
          // The workspace service is authoritative on current DSH; do not
          // pretend a legacy fallback navigated when it did not.
          return false;
        }
      }
      const sessions = getSessions(ctx);
      if (!sessions) return false;
      const candidates = [
        [sessions, "open"],
        [sessions, "select"],
        [sessions, "setCurrent"],
        [sessions, "navigate"],
        [sessions.manager, "select"],
        [sessions.manager, "open"],
        [sessions.manager, "setSelected"],
        [sessions.manager, "selectSession"],
        [sessions.manager, "navigate"],
        [sessions.list, "select"],
        [sessions.list, "setCurrent"]
      ];
      for (const [target, name] of candidates) {
        if (target && typeof target[name] === "function") {
          try {
            target[name](sessionId);
            return true;
          } catch (_) {
            // Try the next navigation shape.
          }
        }
      }
      // Some DSH builds expose no navigation verb at all; the list store's
      // current field is still the UI's selection source.
      try {
        if (sessions.list && typeof sessions.list.update === "function") {
          sessions.list.update((draft) => {
            draft.current = sessionId;
          });
          return true;
        }
      } catch (_) {}
      try {
        if (sessions.list && typeof sessions.list.set === "function") {
          sessions.list.set(
            Object.assign({}, sessions.list.getSnapshot(), { current: sessionId })
          );
          return true;
        }
      } catch (_) {}
      sessionsDebug(ctx);
      return false;
    }

    function buildTaskPrompt(objective) {
      return [
        "[PHONE TASK]",
        `目标：${objective}`,
        "",
        "你是一个手机自动化操作员，通过 phone_* 工具控制一台真实手机。",
        "",
        "工作循环：",
        "1. 先用 phone_screenshot 看当前屏幕；返回的是 PNG 路径，可用 read_image 查看。",
        "2. 用 phone_find_element 找目标文字；找不到就先 phone_swipe 滚动或返回上一页。",
        "3. 找到后用 phone_click 点击中心坐标；每次点击后重新截图验证。",
        "4. 不要盲点；不要假设页面已经变化；每一步都要看结果。",
        `5. 最多 ${MAX_STEPS} 步；超过或无法继续时停止并说明卡在哪一步。`,
        "",
        "安全规则（必须遵守）：",
        "- 不输入密码、验证码、支付密码、银行卡等敏感信息。",
        "- 遇到登录、验证码、支付、风险提示、权限弹窗，立即停止并报告，等待用户处理。",
        "- 不做删除、卸载、购买、转账等破坏性或不可逆操作，除非用户在当前任务里明确要求并确认。",
        "- 只操作当前连接的这台手机。",
        "",
        "完成后：",
        "- 用一句话给出结果；说明关键步骤、当前页面、失败原因（如有）。",
        "- 如果任务无法完成，不要假装成功；明确告诉用户卡在哪里。"
      ].join("\n");
    }

    function watchHostTask(ctx, taskId, hostTaskId) {
      const poll = async () => {
        try {
          const task = await callHostTask(ctx, "phone.task.status", { taskId: hostTaskId });
          if (!task) return;
          updateTask(taskId, {
            hostTaskId,
            steps: task.steps || 0,
            total: task.total || 0,
            lastAction: task.lastAction || null,
            status: task.status || "running",
            mode: task.mode || "agent",
            phase: task.phase || null,
            guided: task.guided === true,
            guidedFull: task.guidedFull === true,
            envAtEntry: task.envAtEntry === true,
            envKnown: task.envKnown === true,
            learned: task.learned || null,
            reviewPath: task.reviewPath || null,
            proposals: task.proposals ?? null,
            templateUpdate: task.templateUpdate || null,
            error: task.error ? task.error.message : null
          });
          if (["done", "error", "stopped"].includes(task.status)) {
            finishTask(taskId, task.status, task.error ? task.error.message : null);
          }
        } catch (_) {
          // Host status is best-effort; the task session still shows the agent log.
        }
      };
      hostPollers.set(taskId, setInterval(poll, 1000));
      poll();
    }

    function watchTask(ctx, taskId, sessionId) {
      const session = sessionFaceOf(ctx, sessionId);
      if (!session) return;

      let sawRunning = false;
      let stopped = false;
      let timer = null;
      let interval = null;

      const cleanup = () => {
        stopped = true;
        if (timer) clearTimeout(timer);
        if (interval) clearInterval(interval);
        try {
          unsub();
        } catch (_) {}
      };

      const check = () => {
        if (stopped) return;
        let snapshot;
        try {
          snapshot = session.getSnapshot();
        } catch (_) {
          return;
        }
        if (!snapshot) return;
        if (snapshot.running) sawRunning = true;
        const ended = snapshot.turnEnds && snapshot.turnEnds.size > 0;
        if (!snapshot.running && (sawRunning || ended)) {
          finishTask(
            taskId,
            snapshot.lastAgentError ? "error" : "done",
            snapshot.lastAgentError
          );
          cleanup();
        }
      };

      const unsub = session.subscribe(check);
      timer = setTimeout(check, 1500);
      interval = setInterval(check, 2500);
      watchers.set(taskId, { cleanup });
      check();
    }

    /**
     * Execute a template as a task. No model involved: the host runs the
     * compiled scenario and streams step progress back through phone.task.status.
     */
    async function startTemplateTask(ctx, templateId, options = {}) {
      const key = templateId === undefined || templateId === null ? "" : String(templateId).trim();
      if (!key) throw new Error("模板 id 为空");
      const taskId = newId();
      const record = {
        id: taskId,
        objective: "执行模板…",
        status: "starting",
        startedAt: Date.now(),
        finishedAt: null,
        sessionId: null,
        hostTaskId: null,
        steps: 0,
        total: 0,
        lastAction: null,
        error: null,
        warning: null,
        mode: "template",
        templateId: key
      };
      setState({ active: record });
      upsertHistory(record);
      try {
        const hostTask = await callHostTask(
          ctx,
          "phone.task.runTemplate",
          { id: key, device: options.device || state.device || "default" },
          { timeoutMs: 60000 }
        );
        updateTask(taskId, {
          hostTaskId: hostTask.id,
          objective: hostTask.objective || record.objective,
          status: hostTask.status || "running",
          steps: hostTask.steps || 0,
          total: hostTask.total || 0,
          lastAction: hostTask.lastAction || null
        });
        watchHostTask(ctx, taskId, hostTask.id);
        return hostTask.id;
      } catch (error) {
        finishTask(taskId, "error", friendlyError(error));
        throw error;
      }
    }

    async function startTask(ctx, currentSessionId, objective, options) {
      const text = String(objective || "").trim();
      if (!text) throw new Error("请先输入任务");
      const device = (options && options.device) || state.device || "default";
      // Prove the hand answers and the eye can capture before creating a task
      // session. Older hosts do not have this endpoint; fall back to the old
      // behaviour instead of blocking the task.
      try {
        await callHostTask(ctx, "phone.task.preflight", { device }, { timeoutMs: 20000 });
      } catch (error) {
        if (error.code !== "HOST_RPC_UNAVAILABLE" && error.code !== "NOT_FOUND") throw error;
      }
      const taskId = newId();
      const record = {
        id: taskId,
        objective: text,
        status: "starting",
        startedAt: Date.now(),
        finishedAt: null,
        sessionId: null,
        error: null,
        warning: null,
        scheduled: Boolean(options && options.scheduled)
      };
      setState({ active: record });
      upsertHistory(record);

      try {
        const sessions = getSessions(ctx);
        if (!sessions || typeof sessions.create !== "function") {
          throw new Error("任务会话创建失败：sessions.create 不可用");
        }
        const created = await sessions.create({});
        const sessionId = normalizeSessionId(created);
        if (!sessionId) throw new Error("任务会话创建失败");
        let warning = null;
        // Stay on the phone task page by default: the running card is the
        // control surface, and "查看会话" is the explicit way into the chat log.
        // Scheduled/background tasks never navigate.
        if (options && options.focus === true) {
          if (!openSession(ctx, sessionId)) {
            warning = "任务已在后台运行，请在侧边栏打开任务会话";
          }
        }
        let hostTask = null;
        try {
          hostTask = await callHostTask(ctx, "phone.task.start", {
            objective: text,
            sessionId,
            device: options?.device || state.device || "default"
          });
        } catch (error) {
          if (error.code !== "HOST_RPC_UNAVAILABLE") throw error;
        }
        updateTask(taskId, {
          sessionId,
          status: "running",
          warning,
          hostTaskId: hostTask?.id || null,
          steps: hostTask?.steps || 0,
          lastAction: hostTask?.lastAction || null
        });
        const session = sessionFaceOf(ctx, sessionId);
        if (!session) {
          sessionsDebug(ctx);
          throw new Error("任务会话创建失败：无法获取会话");
        }
        try {
          await session.rename(`手机任务：${text.slice(0, 30)}`);
        } catch (_) {}
        const result = await session.prompt(
          [{ type: "text", text: buildTaskPrompt(text) }],
          "queue"
        );
        if (!result || result.ok === false) {
          throw new Error(
            (result && result.error && result.error.message) || "任务提交失败"
          );
        }
        if (hostTask?.id) watchHostTask(ctx, taskId, hostTask.id);
        else watchTask(ctx, taskId, sessionId);
        return sessionId;
      } catch (error) {
        finishTask(taskId, "error", error && error.message ? error.message : String(error));
        throw error;
      }
    }

    async function stopTask(ctx) {
      const active = state.active;
      if (!active || !["starting", "running"].includes(active.status)) return;
      if (active.hostTaskId) {
        try {
          await callHostTask(ctx, "phone.task.stop", { taskId: active.hostTaskId });
        } catch (_) {}
      }
      if (active.sessionId) {
        const session = sessionFaceOf(ctx, active.sessionId);
        if (session) {
          try {
            await session.cancel();
          } catch (_) {}
        }
      }
      finishTask(active.id, "stopped");
    }

    function openTask(ctx, sessionId) {
      if (!sessionId) return false;
      const opened = openSession(ctx, sessionId);
      if (!opened) {
        const active = state.active;
        if (active) {
          updateTask(active.id, {
            warning: "无法自动切换，请在侧边栏打开任务会话"
          });
        }
      }
      return opened;
    }

    async function scheduleTask(ctx, objective, options) {
      const text = String(objective || "").trim();
      if (!text) throw new Error("请先输入任务");
      let at = null;
      let delaySeconds = null;
      if (options && typeof options === "object") {
        at = Number(options.at);
        if (!Number.isFinite(at)) {
          const value = Number(options.minutes);
          if (!Number.isFinite(value) || value <= 0 || value > MAX_MINUTES) {
            throw new Error(`定时分钟数需在 1–${MAX_MINUTES} 之间`);
          }
          delaySeconds = Math.round(value * 60);
        } else if (at <= Date.now()) {
          throw new Error("定时时间必须晚于现在");
        }
      } else {
        const value = Number(options);
        if (!Number.isFinite(value) || value <= 0 || value > MAX_MINUTES) {
          throw new Error(`定时分钟数需在 1–${MAX_MINUTES} 之间`);
        }
        delaySeconds = Math.round(value * 60);
      }
      try {
        const entry = await callHostTask(ctx, "phone.task.schedule", {
          objective: text,
          device: state.device || "default",
          ...(Number.isFinite(at) ? { at } : { delaySeconds })
        });
        scheduleRevision += 1;
        setState({ scheduled: state.scheduled.concat([entry]) });
        return entry;
      } catch (error) {
        if (error.code !== "HOST_RPC_UNAVAILABLE") throw error;
      }
      const entry = {
        id: newId(),
        objective: text,
        at: Number.isFinite(at) ? at : Date.now() + delaySeconds * 1000,
        status: "scheduled"
      };
      scheduleRevision += 1;
      setState({ scheduled: state.scheduled.concat([entry]) });
      armSchedule(ctx, entry);
      return entry;
    }

    async function updateScheduleTask(ctx, id, payload = {}) {
      const entry = await callHostTask(ctx, "phone.task.updateSchedule", {
        id: String(id),
        ...payload
      });
      const key = String(id);
      const timer = scheduleTimers.get(key);
      if (timer) clearTimeout(timer);
      scheduleTimers.delete(key);
      scheduleRevision += 1;
      const known = state.scheduled.some((item) => item.id === entry.id);
      setState({
        scheduled: known
          ? state.scheduled.map((item) => (item.id === entry.id ? entry : item))
          : state.scheduled.concat([entry])
      });
      return entry;
    }

    function armSchedule(ctx, entry) {
      const delay = entry.at - Date.now();
      if (delay <= 0) {
        removeScheduled(entry.id);
        startTask(ctx, null, entry.objective, { scheduled: true, focus: false }).catch(() => {});
        return;
      }
      const timer = setTimeout(() => {
        scheduleTimers.delete(entry.id);
        removeScheduled(entry.id);
        startTask(ctx, null, entry.objective, { scheduled: true, focus: false }).catch(() => {});
      }, Math.min(delay, 2147483647));
      scheduleTimers.set(entry.id, timer);
    }

    function removeScheduled(id) {
      scheduleRevision += 1;
      setState({ scheduled: state.scheduled.filter((item) => item.id !== id) });
    }

    async function cancelScheduleTask(ctx, id) {
      const key = String(id);
      try {
        await callHostTask(ctx, "phone.task.cancelSchedule", { id: key });
      } catch (error) {
        if (error.code !== "HOST_RPC_UNAVAILABLE" && error.code !== "NOT_FOUND") {
          throw error;
        }
      }
      const timer = scheduleTimers.get(key);
      if (timer) clearTimeout(timer);
      scheduleTimers.delete(key);
      removeScheduled(key);
    }

    function dismissTask() {
      if (state.active) setState({ active: null });
    }

    function restoreSchedules(ctx) {
      scheduleTimers.forEach((timer) => clearTimeout(timer));
      scheduleTimers.clear();
      const now = Date.now();
      const future = [];
      state.scheduled.forEach((entry) => {
        if (entry.at > now) {
          future.push(entry);
          armSchedule(ctx, entry);
        } else {
          upsertHistory(
            Object.assign({}, entry, {
              status: "missed",
              finishedAt: Date.now(),
              error: "页面关闭期间错过了定时"
            })
          );
        }
      });
      setState({ scheduled: future });
    }

    function clearTimers() {
      scheduleTimers.forEach((timer) => clearTimeout(timer));
      scheduleTimers.clear();
      watchers.forEach((watcher) => watcher.cleanup());
      watchers.clear();
      hostPollers.forEach((timer) => clearInterval(timer));
      hostPollers.clear();
    }

    const MAX_RESULT_AGE = 30 * 60 * 1000;
    const PANEL_ID = "phone-task";
    const WIFI_PREF_KEY = "dsh-plugin-phone/wifi/v1";
    // Each thumbnail tick is a full capture on the host and competes with the
    // agent's phone_screenshot for the service capture lock, so poll slowly.
    const THUMBNAIL_POLL_MS = 5000;

    function formatClock(ms) {
      const total = Math.max(0, Math.floor(Number(ms || 0) / 1000));
      const seconds = total % 60;
      const minutes = Math.floor(total / 60) % 60;
      const hours = Math.floor(total / 3600);
      const pad = (value) => String(value).padStart(2, "0");
      return hours > 0 ? `${hours}:${pad(minutes)}:${pad(seconds)}` : `${pad(minutes)}:${pad(seconds)}`;
    }

    function formatDuration(ms) {
      const total = Math.max(0, Math.floor(Number(ms || 0) / 1000));
      if (total < 60) return `${total} 秒`;
      const minutes = Math.floor(total / 60);
      const seconds = total % 60;
      if (minutes < 60) return seconds ? `${minutes} 分 ${seconds} 秒` : `${minutes} 分钟`;
      const hours = Math.floor(minutes / 60);
      const rest = minutes % 60;
      return rest ? `${hours} 小时 ${rest} 分` : `${hours} 小时`;
    }

    function sameDay(a, b) {
      return (
        a.getFullYear() === b.getFullYear() &&
        a.getMonth() === b.getMonth() &&
        a.getDate() === b.getDate()
      );
    }

    function formatTime(ts) {
      const value = Number(ts);
      if (!Number.isFinite(value)) return "";
      const date = new Date(value);
      if (Number.isNaN(date.getTime())) return "";
      const now = new Date();
      const pad = (number) => String(number).padStart(2, "0");
      const clock = `${pad(date.getHours())}:${pad(date.getMinutes())}`;
      if (sameDay(date, now)) return `今天 ${clock}`;
      const yesterday = new Date(now.getFullYear(), now.getMonth(), now.getDate() - 1);
      if (sameDay(date, yesterday)) return `昨天 ${clock}`;
      return `${date.getMonth() + 1} 月 ${date.getDate()} 日 ${clock}`;
    }

    function atFromClock(value, base) {
      const match = /^(\d{1,2}):(\d{2})$/.exec(String(value || ""));
      if (!match) return null;
      const hours = Number(match[1]);
      const minutes = Number(match[2]);
      if (!Number.isFinite(hours) || !Number.isFinite(minutes) || hours > 23 || minutes > 59) {
        return null;
      }
      const anchor = Number.isFinite(Number(base)) ? Number(base) : Date.now();
      const at = new Date(anchor);
      at.setHours(hours, minutes, 0, 0);
      if (at.getTime() <= anchor) at.setDate(at.getDate() + 1);
      return at.getTime();
    }

    function timeInputValue(ts) {
      const value = Number(ts);
      if (!Number.isFinite(value)) return "";
      const date = new Date(value);
      if (Number.isNaN(date.getTime())) return "";
      return `${String(date.getHours()).padStart(2, "0")}:${String(date.getMinutes()).padStart(2, "0")}`;
    }

    function delay(ms) {
      return new Promise((resolve) => setTimeout(resolve, ms));
    }

    function parseActionArguments(value) {
      if (!value) return {};
      if (typeof value === "object") return value;
      try {
        const parsed = JSON.parse(String(value));
        return parsed && typeof parsed === "object" ? parsed : {};
      } catch (_) {
        return {};
      }
    }

    function formatAction(action) {
      if (!action || !action.name) return "";
      const args = parseActionArguments(action.arguments);
      switch (action.name) {
        case "phone_screenshot":
          return "正在查看屏幕";
        case "phone_find_element":
          return args.keyword ? `正在查找「${String(args.keyword)}」` : "正在查找页面文字";
        case "phone_click": {
          const x = Number(args.x);
          const y = Number(args.y);
          return Number.isFinite(x) && Number.isFinite(y) ? `正在点击 (${x}, ${y})` : "正在点击屏幕";
        }
        case "phone_swipe":
          return "正在滑动屏幕";
        case "phone_text":
          return "正在输入文字";
        default:
          return `正在执行 ${action.name}`;
      }
    }

    function normalizeError(value) {
      if (!value) return null;
      if (typeof value === "object") {
        return {
          code: value.code ? String(value.code) : "",
          message: value.message ? String(value.message) : value.code ? String(value.code) : "任务失败"
        };
      }
      return { code: "", message: String(value) };
    }

    function friendlyError(error) {
      const code = error && error.code ? String(error.code) : "";
      const message = error && error.message ? String(error.message) : String(error || "");
      switch (code) {
        case "SERVICE_DOWN":
        case "PHONE_SERVICE_DOWN":
          return "本地服务没有运行，请先启动 phone 服务";
        case "DEVICE_NOT_READY":
          return message || "设备没有连接好";
        case "EYE_UNAVAILABLE":
          return "读不到手机画面：解锁手机，确认无线调试已连接";
        case "BUSY":
          return "设备正在上一次操作中，请稍后再试";
        case "PROVISION_UNAVAILABLE":
          return "配网服务不可用，请确认本机蓝牙已打开";
        case "HOST_RPC_UNAVAILABLE":
          return "插件后台不可用，请重新加载页面或重启 DSH";
        case "NOT_FOUND":
          return "后台接口不可用，请更新插件后重试";
        case "BAD_REQUEST":
          return message || "输入不完整";
        default:
          return message || "操作没有成功，请重试";
      }
    }

    function mergeHistory(snapshot) {
      const byId = new Map();
      const bySession = new Set();
      const add = (item, source) => {
        if (!item || !item.id || byId.has(item.id)) return;
        // A page-started task is recorded twice: the local optimistic record and
        // the host record share the same task session. Show only the host one so
        // every task appears once.
        if (item.sessionId && bySession.has(String(item.sessionId))) return;
        if (item.sessionId) bySession.add(String(item.sessionId));
        byId.set(item.id, Object.assign({}, item, { source, error: normalizeError(item.error) }));
      };
      (snapshot.hostHistory || []).forEach((item) => add(item, "host"));
      (snapshot.history || []).forEach((item) => add(item, "local"));
      return [...byId.values()].sort((a, b) => {
        const left = a.finishedAt || a.startedAt || a.at || 0;
        const right = b.finishedAt || b.startedAt || b.at || 0;
        return right - left;
      });
    }

    function readLastSsid() {
      try {
        const raw = globalThis.localStorage && globalThis.localStorage.getItem(WIFI_PREF_KEY);
        if (!raw) return "";
        const parsed = JSON.parse(raw);
        return parsed && typeof parsed.ssid === "string" ? parsed.ssid : "";
      } catch (_) {
        return "";
      }
    }

    function writeLastSsid(ssid) {
      try {
        if (globalThis.localStorage && ssid) {
          globalThis.localStorage.setItem(WIFI_PREF_KEY, JSON.stringify({ ssid }));
        }
      } catch (_) {}
    }

    function copyText(value) {
      try {
        if (globalThis.navigator && globalThis.navigator.clipboard && typeof globalThis.navigator.clipboard.writeText === "function") {
          return globalThis.navigator.clipboard.writeText(value);
        }
      } catch (_) {}
      return Promise.reject(new Error("clipboard unavailable"));
    }

    function deviceSummary(phase, status, error, provisionUpdate) {
      if (provisionUpdate && provisionUpdate.state === "running") {
        return {
          tone: "warn",
          title: "正在配网",
          meta: [provisionUpdate.ssid || ""].filter(Boolean)
        };
      }
      if (provisionUpdate && provisionUpdate.state === "waiting") {
        return {
          tone: "warn",
          title: "等待设备上线",
          meta: [provisionUpdate.ssid ? `Wi-Fi ${provisionUpdate.ssid}` : ""].filter(Boolean)
        };
      }
      if (provisionUpdate && provisionUpdate.state === "failed") {
        return {
          tone: "error",
          title: "配网没有成功",
          meta: [provisionUpdate.ssid || ""].filter(Boolean)
        };
      }
      if (provisionUpdate && provisionUpdate.state === "done") {
        return {
          tone: "ok",
          title: "设备已上线",
          meta: [provisionUpdate.ssid ? `Wi-Fi ${provisionUpdate.ssid}` : ""].filter(Boolean)
        };
      }
      if (phase === "booting") {
        return { tone: "muted", title: "正在检测设备…", meta: [] };
      }
      if (phase === "service_down") {
        const baseUrl = error && error.details && error.details.baseUrl;
        return {
          tone: "error",
          title: "本地服务没有运行",
          meta: [baseUrl || "127.0.0.1:8765"]
        };
      }
      if (phase === "offline" || !status) {
        if (phase !== "offline") {
          return { tone: "muted", title: "设备状态未知", meta: ["后台未提供状态接口"] };
        }
        return { tone: "warn", title: "设备未连接", meta: ["本地服务运行中"] };
      }
      const device = status.device || {};
      const isIos = device.platform === "ios";
      if (!device.reachable) {
        return {
          tone: "warn",
          title: "设备未连接",
          meta: [device.error && device.error.message ? "没有收到设备响应" : "本地服务运行中"]
        };
      }
      const meta = [];
      const wifi = device.wifi;
      if (wifi && wifi.state === "connected") {
        const suffix = Number.isFinite(wifi.rssi) ? ` · ${wifi.rssi} dBm` : "";
        meta.push(`Wi-Fi ${wifi.ssid || ""}${suffix}`.trim());
      } else if (wifi && wifi.state === "connecting") {
        meta.push("Wi-Fi 连接中");
      }
      if (Array.isArray(device.screen) && device.screen.length === 2) {
        meta.push(`手机 ${device.screen[0]}×${device.screen[1]}`);
      }
      const label = isIos ? "iPhone" : device.label || "设备";
      if (isIos && device.hand && device.hand.ok === false) {
        return {
          tone: "warn",
          title: "iPhone 屏幕在线 · 控制手未连接",
          meta: meta.concat(["眼可用，点击需接控制手"])
        };
      }
      if (device.usb === false) {
        return { tone: "warn", title: `${label}已连接 · 手机未识别`, meta };
      }
      if (device.state === "FAULT") {
        return {
          tone: "warn",
          title: `${label}异常`,
          meta: [device.lastError || "需要重新检测"]
        };
      }
      if (isIos) {
        return { tone: "ok", title: `${label} 已就绪`, meta };
      }
      return { tone: "ok", title: `${label}已就绪`, meta };
    }

    function provisionFailureMessage(status) {
      const raw = String((status && status.error) || "").trim();
      const tail = String((status && status.result && status.result.output_tail) || "");
      const text = `${raw}\n${tail}`.toLowerCase();
      if (/auth|password|handshake|invalid|denied/.test(text)) {
        return "Wi-Fi 密码不对，或路由器拒绝了连接";
      }
      if (/timeout|timed out/.test(text)) {
        return "连接超时：检查 Wi-Fi 名称和是否为 2.4GHz";
      }
      if (/not found|no ap|ssid/.test(text)) {
        return "没找到这个 Wi-Fi：检查名称，确保是 2.4GHz";
      }
      return raw || "配网没有成功，请重试";
    }

    function provisionTail(status) {
      const output = String((status && status.result && status.result.output_tail) || "");
      return output.split("\n").filter(Boolean).slice(-6).join("\n");
    }

    function Icon(props) {
      const name = props.name;
      const size = props.size || 16;
      const common = {
        width: size,
        height: size,
        viewBox: "0 0 16 16",
        fill: "none",
        "aria-hidden": "true",
        className: `pt-icon${props.className ? ` ${props.className}` : ""}`
      };
      const stroke = {
        stroke: "currentColor",
        strokeWidth: "1.6",
        strokeLinecap: "round",
        strokeLinejoin: "round"
      };
      let children = null;
      if (name === "check") {
        children = React.createElement("path", Object.assign({ d: "M3.5 8.5l3 3 6-7" }, stroke));
      } else if (name === "x") {
        children = [
          React.createElement("path", { key: "a", d: "M4.5 4.5l7 7", stroke: "currentColor", strokeWidth: "1.7", strokeLinecap: "round" }),
          React.createElement("path", { key: "b", d: "M11.5 4.5l-7 7", stroke: "currentColor", strokeWidth: "1.7", strokeLinecap: "round" })
        ];
      } else if (name === "clock") {
        children = [
          React.createElement("circle", { key: "a", cx: "8", cy: "8", r: "5.6", stroke: "currentColor", strokeWidth: "1.4" }),
          React.createElement("path", { key: "b", d: "M8 4.8V8l2.2 1.4", stroke: "currentColor", strokeWidth: "1.4", strokeLinecap: "round" })
        ];
      } else if (name === "refresh") {
        children = [
          React.createElement("path", { key: "a", d: "M13 6.2A5.4 5.4 0 1 0 13.2 10", stroke: "currentColor", strokeWidth: "1.4", strokeLinecap: "round" }),
          React.createElement("path", { key: "b", d: "M13.4 3v3.4H10", stroke: "currentColor", strokeWidth: "1.4", strokeLinecap: "round", strokeLinejoin: "round" })
        ];
      } else if (name === "wifi") {
        children = [
          React.createElement("path", { key: "a", d: "M2.5 6.2a8 8 0 0 1 11 0", stroke: "currentColor", strokeWidth: "1.4", strokeLinecap: "round" }),
          React.createElement("path", { key: "b", d: "M4.7 8.5a5 5 0 0 1 6.6 0", stroke: "currentColor", strokeWidth: "1.4", strokeLinecap: "round" }),
          React.createElement("path", { key: "c", d: "M6.9 10.8a2 2 0 0 1 2.2 0", stroke: "currentColor", strokeWidth: "1.4", strokeLinecap: "round" }),
          React.createElement("circle", { key: "d", cx: "8", cy: "12.6", r: ".8", fill: "currentColor" })
        ];
      } else if (name === "alert") {
        children = [
          React.createElement("path", { key: "a", d: "M8 2.7l5.3 9.2H2.7L8 2.7z", stroke: "currentColor", strokeWidth: "1.4", strokeLinejoin: "round" }),
          React.createElement("path", { key: "b", d: "M8 6.4v2.4", stroke: "currentColor", strokeWidth: "1.4", strokeLinecap: "round" }),
          React.createElement("circle", { key: "c", cx: "8", cy: "10.8", r: ".75", fill: "currentColor" })
        ];
      } else if (name === "stop") {
        children = React.createElement("rect", { x: "4.4", y: "4.4", width: "7.2", height: "7.2", rx: "1", fill: "currentColor" });
      } else if (name === "phone") {
        children = [
          React.createElement("rect", {
            key: "a",
            x: "4.25",
            y: "1.75",
            width: "7.5",
            height: "12.5",
            rx: "1.6",
            stroke: "currentColor",
            strokeWidth: "1.4"
          }),
          React.createElement("path", {
            key: "b",
            d: "M7 12.1h2",
            stroke: "currentColor",
            strokeWidth: "1.4",
            strokeLinecap: "round"
          })
        ];
      }
      return React.createElement("svg", common, children);
    }

    function Spinner(props) {
      return React.createElement("span", {
        className: `pt-spinner${props && props.className ? ` ${props.className}` : ""}`,
        "aria-hidden": "true"
      });
    }

    function Modal(props) {
      React.useEffect(() => {
        if (typeof document === "undefined" || !document.addEventListener) return undefined;
        const onKey = (event) => {
          if (event.key === "Escape" && props.onClose) props.onClose();
        };
        document.addEventListener("keydown", onKey);
        return () => document.removeEventListener("keydown", onKey);
      }, [props.onClose]);
      return React.createElement(
        "div",
        {
          className: "pt-modal-backdrop",
          onMouseDown: (event) => {
            if (event.target === event.currentTarget && props.onClose) props.onClose();
          }
        },
        React.createElement(
          "div",
          {
            className: `pt-modal${props.wide ? " is-wide" : ""}`,
            role: "dialog",
            "aria-modal": "true",
            "aria-label": props.title
          },
          React.createElement(
            "div",
            { className: "pt-modal-head" },
            React.createElement("h2", { className: "pt-modal-title" }, props.title),
            React.createElement(
              "button",
              { type: "button", className: "pt-icon-btn", onClick: props.onClose, "aria-label": "关闭" },
              React.createElement(Icon, { name: "x", size: 14 })
            )
          ),
          React.createElement("div", { className: "pt-modal-body" }, props.children)
        )
      );
    }

    function PhonePanelIcon(props) {
      const size = props && Number.isFinite(props.size) ? props.size : 16;
      return React.createElement(
        "span",
        {
          className: `pt-panel-icon${props && props.active ? " is-active" : ""}`,
          style: { width: `${size}px`, height: `${size}px` },
          "aria-hidden": "true"
        },
        React.createElement(
          "svg",
          { width: size, height: size, viewBox: "0 0 16 16", fill: "none" },
          React.createElement("rect", {
            x: "4.25",
            y: "1.75",
            width: "7.5",
            height: "12.5",
            rx: "1.6",
            stroke: "currentColor",
            strokeWidth: "1.4"
          }),
          React.createElement("path", {
            d: "M7 12.1h2",
            stroke: "currentColor",
            strokeWidth: "1.4",
            strokeLinecap: "round"
          })
        )
      );
    }

    function PageHeader() {
      return React.createElement(
        "header",
        { className: "pt-page-head" },
        React.createElement(
          "div",
          null,
          React.createElement("h1", { className: "pt-page-title" }, "手机任务"),
          React.createElement("p", { className: "pt-page-sub" }, "让 Agent 在这台手机上执行操作")
        )
      );
    }

    function StatusBar(props) {
      const info = props.info;
      const meta = Array.isArray(info.meta) ? info.meta.filter(Boolean) : [];
      const showRefresh = !props.running && !props.provisioning && typeof props.onRefresh === "function";
      return React.createElement(
        "div",
        {
          className: `pt-status is-${info.tone}`,
          role: "status",
          "aria-live": "polite"
        },
        React.createElement("span", { className: "pt-dot" }),
        React.createElement("span", { className: "pt-status-title" }, info.title),
        meta.map((text, index) =>
          React.createElement(
            React.Fragment,
            { key: `meta-${index}` },
            React.createElement("span", { className: "pt-status-sep" }),
            React.createElement("span", { className: "pt-status-meta" }, text)
          )
        ),
        props.eye
          ? React.createElement(
              React.Fragment,
              null,
              React.createElement("span", { className: "pt-status-sep" }),
              React.createElement(
                "span",
                {
                  className: "pt-status-meta",
                  title: props.eye.reason || "",
                  style: props.eye.state === "down" ? { color: "var(--pt-red-text)" } : undefined
                },
                props.eye.state === "down" ? "画面 断开" : "画面 正常"
              )
            )
          : null,
        React.createElement("span", { className: "pt-status-spacer" }),
        props.devices && props.devices.length > 1
          ? React.createElement(
              "select",
              {
                className: "pt-device-select",
                value: props.device,
                "aria-label": "目标设备",
                onChange: (event) => props.onDeviceChange(event.target.value)
              },
              props.devices.map((item) =>
                React.createElement("option", { key: item.name, value: item.name }, item.name)
              )
            )
          : null,
        showRefresh
          ? React.createElement(
              "button",
              {
                type: "button",
                className: "pt-link",
                onClick: props.onRefresh,
                disabled: props.refreshing
              },
              props.refreshing ? "检测中…" : "重新检测"
            )
          : null,
        props.onOpenSetup && !props.running && !props.provisioning
          ? React.createElement(
              "button",
              { type: "button", className: "pt-link", onClick: props.onOpenSetup },
              "配网设置"
            )
          : null
      );
    }

    function ServiceDownCard(props) {
      const [copied, setCopied] = React.useState("");
      const command = (props.error && props.error.details && props.error.details.command) || "";
      const copy = () => {
        copyText(command)
          .then(() => {
            setCopied("已复制");
            setTimeout(() => setCopied(""), 2000);
          })
          .catch(() => setCopied("复制失败，请手动选择命令"));
      };
      return React.createElement(
        "section",
        { className: "pt-card" },
        React.createElement("h2", { className: "pt-card-title" }, "启动 phone 服务后重试"),
        React.createElement(
          "p",
          { className: "pt-card-sub" },
          "手机任务需要本机的 phone 服务。启动后点「重新检测」，不需要重启 DSH。"
        ),
        command
          ? React.createElement(
              "div",
              { className: "pt-command" },
              React.createElement("code", null, command),
              React.createElement("button", { type: "button", className: "pt-btn pt-btn-ghost pt-btn-sm", onClick: copy }, "复制")
            )
          : null,
        copied ? React.createElement("p", { className: "pt-hint" }, copied) : null,
        React.createElement(
          "div",
          { className: "pt-actions", style: { marginTop: "14px" } },
          React.createElement(
            "button",
            {
              type: "button",
              className: "pt-btn pt-btn-primary",
              onClick: props.onRetry,
              disabled: props.refreshing
            },
            props.refreshing ? "检测中…" : "重新检测"
          )
        ),
        React.createElement(
          "p",
          { className: "pt-hint", style: { marginTop: "12px" } },
          "排查：确认终端里的 phone serve 正在运行；默认端口 8765；日志在 runs/phone-service.log。"
        )
      );
    }

    function LoadingCard(props) {
      return React.createElement(
        "section",
        { className: "pt-card pt-loading" },
        React.createElement(Spinner),
        React.createElement("span", null, (props && props.text) || "正在检测设备…")
      );
    }
    function SetupWizard(props) {
      const mode = props.mode === "reprovision" ? "reprovision" : "first";
      const [phase, setPhase] = React.useState("checking");
      const [devices, setDevices] = React.useState([]);
      const [selected, setSelected] = React.useState("");
      const [ssid, setSsid] = React.useState(props.currentSsid || readLastSsid() || "");
      const [password, setPassword] = React.useState("");
      const [showPassword, setShowPassword] = React.useState(false);
      const [busy, setBusy] = React.useState(false);
      const [error, setError] = React.useState(props.initialError || "");
      const [notice, setNotice] = React.useState("");
      const [provision, setProvision] = React.useState(null);
      const [details, setDetails] = React.useState("");
      const [showDetails, setShowDetails] = React.useState(false);
      const [failureKind, setFailureKind] = React.useState("");
      const propsRef = React.useRef(props);
      propsRef.current = props;
      const selectedRef = React.useRef(selected);
      selectedRef.current = selected;
      const ssidRef = React.useRef(ssid);
      ssidRef.current = ssid;
      const mountedRef = React.useRef(true);
      const bootedRef = React.useRef(false);
      const intervalRef = React.useRef(null);
      const onlineRef = React.useRef(null);
      const onlineDeadlineRef = React.useRef(0);
      const pollStartedRef = React.useRef(0);

      function stopProvisionPoll() {
        if (intervalRef.current) {
          clearInterval(intervalRef.current);
          intervalRef.current = null;
        }
      }

      function stopOnlinePoll() {
        if (onlineRef.current) {
          clearInterval(onlineRef.current);
          onlineRef.current = null;
        }
      }

      function notify(args) {
        try {
          propsRef.current.onProvisionUpdate?.(args);
        } catch (_) {}
      }

      async function scan(attempts = 1) {
        stopProvisionPoll();
        stopOnlinePoll();
        setBusy(true);
        setError("");
        setNotice("");
        setShowDetails(false);
        setPhase("scanning");
        try {
          let list = [];
          for (let index = 0; index < Math.max(1, attempts); index += 1) {
            if (index > 0) await delay(2000);
            if (!mountedRef.current) return;
            const result = await propsRef.current.provisionDevices();
            list = result && Array.isArray(result.devices) ? result.devices : [];
            if (list.length > 0) break;
          }
          if (!mountedRef.current) return;
          setDevices(list);
          setSelected((previous) => {
            const stillThere = list.some((item) => (item.name || item.address) === previous);
            if (stillThere) return previous;
            return list[0] ? list[0].name || list[0].address || "" : "";
          });
          setPhase("form");
          if (list.length === 0) {
            setError("没有发现可配网的设备。上电后等 5 秒再试，或检查设备是否已进入配网模式。");
          }
        } catch (scanError) {
          if (!mountedRef.current) return;
          setPhase("form");
          setError(friendlyError(scanError));
        } finally {
          if (mountedRef.current) setBusy(false);
        }
      }

      async function arm() {
        setBusy(true);
        setError("");
        setShowDetails(false);
        setPhase("arming");
        setNotice("设备正在重启进入配网（约 5 秒）…");
        try {
          await propsRef.current.armProvisioning();
          await delay(5000);
          if (!mountedRef.current) return;
          setNotice("正在扫描配网设备…");
          await scan(5);
        } catch (armError) {
          if (!mountedRef.current) return;
          setPhase("intro");
          setError(friendlyError(armError));
        } finally {
          if (mountedRef.current) setBusy(false);
        }
      }

      function startOnlinePoll() {
        stopOnlinePoll();
        onlineDeadlineRef.current = Date.now() + 60000;
        const tick = async () => {
          let value = null;
          try {
            value = await propsRef.current.refreshDevice({ force: true, quiet: true });
          } catch (_) {}
          if (!mountedRef.current) return;
          if (value && value.device && value.device.reachable) {
            stopOnlinePoll();
            setPhase("done");
            notify({ state: "done", ssid: ssidRef.current });
            propsRef.current.onToast?.("设备已上线");
            propsRef.current.onReady?.();
            return;
          }
          if (Date.now() > onlineDeadlineRef.current) {
            stopOnlinePoll();
            setFailureKind("online");
            setPhase("failed");
            setError("Wi-Fi 已保存，但设备 60 秒内没有上线。确认设备已上电、离路由器不远，然后重试。");
          }
        };
        tick();
        onlineRef.current = setInterval(tick, 2000);
      }

      function startProvisionPoll() {
        stopProvisionPoll();
        if (!pollStartedRef.current) pollStartedRef.current = Date.now();
        const tick = async () => {
          let next = null;
          try {
            next = await propsRef.current.provisionStatus();
          } catch (pollError) {
            if (!mountedRef.current) return;
            if (Date.now() - pollStartedRef.current > 180000) {
              stopProvisionPoll();
              setFailureKind("provision");
              setPhase("failed");
              setError(friendlyError(pollError));
            }
            return;
          }
          if (!mountedRef.current) return;
          setProvision(next);
          notify(next);
          if (next && next.state === "done") {
            stopProvisionPoll();
            setPhase("waiting");
            notify({ state: "waiting", ssid: next.ssid || ssidRef.current });
            startOnlinePoll();
          } else if (next && next.state === "failed") {
            stopProvisionPoll();
            setFailureKind("provision");
            setPhase("failed");
            setError(provisionFailureMessage(next));
            setDetails(provisionTail(next));
            setPassword("");
          } else if (Date.now() - pollStartedRef.current > 180000) {
            stopProvisionPoll();
            setFailureKind("provision");
            setPhase("failed");
            setError("配网超过 3 分钟没有结果。检查 Wi-Fi 是否可用，然后重试。");
          }
        };
        tick();
        intervalRef.current = setInterval(tick, 1000);
      }

      async function boot() {
        let current = null;
        try {
          current = await propsRef.current.provisionStatus();
        } catch (_) {}
        if (!mountedRef.current) return;
        if (current && current.state === "running") {
          pollStartedRef.current =
            Date.now() - Math.max(0, Number(current.elapsed_s || 0)) * 1000;
          setProvision(current);
          notify(current);
          setPhase("running");
          startProvisionPoll();
          return;
        }
        if (mode === "reprovision") {
          setPhase("intro");
          return;
        }
        if (current && current.state === "done") {
          setProvision(current);
          notify({ state: "waiting", ssid: current.ssid || "" });
          setPhase("waiting");
          startOnlinePoll();
          return;
        }
        await scan(1);
      }

      async function start() {
        const deviceName = selectedRef.current;
        const cleanSsid = ssidRef.current.trim();
        if (!deviceName) {
          setError("请先选择要配网的设备");
          return;
        }
        if (!cleanSsid) {
          setError("请填写 Wi-Fi 名称");
          return;
        }
        if (!password) {
          setError("请填写 Wi-Fi 密码");
          return;
        }
        setBusy(true);
        setError("");
        setShowDetails(false);
        setDetails("");
        setFailureKind("");
        writeLastSsid(cleanSsid);
        pollStartedRef.current = Date.now();
        setPhase("running");
        setProvision({ state: "running", ssid: cleanSsid, elapsed_s: 0 });
        notify({ state: "running", ssid: cleanSsid });
        try {
          await propsRef.current.startProvision({ ssid: cleanSsid, password, device: deviceName });
          startProvisionPoll();
        } catch (startError) {
          if (startError && startError.code === "BUSY") {
            startProvisionPoll();
          } else {
            setFailureKind("provision");
            setPhase("failed");
            setError(friendlyError(startError));
            setDetails(startError && startError.details ? String(startError.details.output_tail || "") : "");
            setPassword("");
          }
        } finally {
          if (mountedRef.current) setBusy(false);
        }
      }

      function retry() {
        stopProvisionPoll();
        stopOnlinePoll();
        setPhase("form");
        setError("");
        setDetails("");
        setShowDetails(false);
        setProvision(null);
        setFailureKind("");
        setPassword("");
        pollStartedRef.current = 0;
        notify(null);
      }

      function checkOnline() {
        setError("");
        setDetails("");
        setShowDetails(false);
        setFailureKind("");
        setPhase("waiting");
        startOnlinePoll();
        notify({ state: "waiting", ssid: ssidRef.current });
      }

      React.useEffect(() => {
        mountedRef.current = true;
        if (!bootedRef.current) {
          bootedRef.current = true;
          boot();
        }
        return () => {
          mountedRef.current = false;
          stopProvisionPoll();
          stopOnlinePoll();
        };
      }, []);

      const scanning = phase === "checking" || phase === "scanning";
      const arming = phase === "arming";
      const currentSsid = props.currentSsid || (props.deviceStatus && props.deviceStatus.device && props.deviceStatus.device.wifi && props.deviceStatus.device.wifi.ssid) || "";
      const screen = props.deviceStatus && props.deviceStatus.device ? props.deviceStatus.device.screen : null;
      const elapsed = provision && Number(provision.elapsed_s) > 0
        ? Math.round(Number(provision.elapsed_s))
        : Math.round(Math.max(0, Date.now() - (pollStartedRef.current || Date.now())) / 1000);

      function renderIntro() {
        return React.createElement(
          "section",
          { className: "pt-card" },
          React.createElement(
            "div",
            { className: "pt-card-head" },
            React.createElement(
              "div",
              null,
              React.createElement("h2", { className: "pt-card-title" }, "重新配网"),
              React.createElement(
                "p",
                { className: "pt-card-sub" },
                currentSsid ? `当前 Wi-Fi：${currentSsid}` : "设备已经连过 Wi-Fi，需要重新下发凭证。"
              )
            ),
            props.onClose
              ? React.createElement("button", { type: "button", className: "pt-link", onClick: props.onClose }, "取消")
              : null
          ),
          React.createElement(
            "p",
            { className: "pt-warn" },
            "设备会重启进入配网模式，Wi-Fi 会短暂断开（约 20 秒）。"
          ),
          error ? React.createElement("p", { className: "pt-error" }, error) : null,
          React.createElement(
            "div",
            { className: "pt-actions", style: { marginTop: "16px" } },
            React.createElement(
              "button",
              { type: "button", className: "pt-btn pt-btn-primary", onClick: arm, disabled: busy },
              busy ? "正在重启设备…" : "让设备进入配网模式"
            )
          )
        );
      }

      function renderProgress() {
        const failed = phase === "failed";
        const done = phase === "done";
        const waiting = phase === "waiting";
        const title = failed
          ? failureKind === "online"
            ? "设备没有上线"
            : "没连上 Wi-Fi"
          : done
          ? "设备已上线"
          : waiting
          ? "Wi-Fi 已保存，正在等待设备上线…"
          : "正在连接 Wi-Fi…";
        const subtitle = failed
          ? error
          : done
          ? "现在可以下发任务了。"
          : waiting
          ? "设备重启后会自动连回，通常需要 5–10 秒。"
          : `已用 ${elapsed} 秒，通常需要 20 秒左右。`;
        return React.createElement(
          "section",
          { className: "pt-card" },
          React.createElement(
            "div",
            { className: "pt-progress-head" },
            done
              ? React.createElement("span", { className: "pt-state-icon is-ok" }, React.createElement(Icon, { name: "check" }))
              : failed
              ? React.createElement("span", { className: "pt-state-icon is-error" }, React.createElement(Icon, { name: "alert" }))
              : React.createElement(Spinner),
            React.createElement(
              "div",
              { className: "pt-progress-main" },
              React.createElement("h2", { className: "pt-card-title" }, title),
              React.createElement("p", { className: "pt-card-sub" }, subtitle)
            )
          ),
          !failed && !done
            ? React.createElement(
                "p",
                { className: "pt-note" },
                waiting
                  ? "配网成功后设备会重启并自动上线。请不要断开电源。"
                  : "成功后设备会重启并自动上线，通常再等 5–10 秒。请不要断开电源。"
              )
            : null,
          !failed && !done
            ? React.createElement(
                "p",
                { className: "pt-hint" },
                "进度以设备返回为准；超过 1 分钟没有结果，可以重试或查看详情。"
              )
            : null,
          provision && provision.ssid && !failed && !done
            ? React.createElement("p", { className: "pt-hint" }, `Wi-Fi：${provision.ssid}`)
            : null,
          failed
            ? React.createElement(
                "div",
                { className: "pt-actions", style: { marginTop: "16px" } },
                React.createElement(
                  "button",
                  {
                    type: "button",
                    className: "pt-btn pt-btn-primary",
                    onClick: failureKind === "online" ? checkOnline : retry
                  },
                  failureKind === "online" ? "重新检测" : "重试"
                ),
                failureKind === "online"
                  ? React.createElement(
                      "button",
                      { type: "button", className: "pt-btn pt-btn-ghost", onClick: retry },
                      "回到配网"
                    )
                  : null,
                props.onClose
                  ? React.createElement(
                      "button",
                      { type: "button", className: "pt-btn pt-btn-ghost", onClick: props.onClose },
                      "取消"
                    )
                  : null
              )
            : null,
          done
            ? React.createElement(
                "div",
                { className: "pt-actions", style: { marginTop: "16px" } },
                React.createElement(
                  "button",
                  { type: "button", className: "pt-btn pt-btn-primary", onClick: () => propsRef.current.onReady?.() },
                  "开始执行"
                )
              )
            : null,
          details
            ? React.createElement(
                "div",
                { className: "pt-details" },
                React.createElement(
                  "button",
                  { type: "button", className: "pt-link", onClick: () => setShowDetails((value) => !value) },
                  showDetails ? "收起详情" : "查看详情"
                ),
                showDetails ? React.createElement("pre", { className: "pt-output" }, details) : null
              )
            : null
        );
      }

      function renderSteps() {
        const stepOneDone = Boolean(selected) && devices.length > 0 && !scanning && !arming;
        const tag = scanning
          ? "扫描中"
          : arming
          ? "等待设备"
          : devices.length > 0
          ? `已发现 ${devices.length} 台`
          : "未发现";
        return React.createElement(
          "section",
          { className: "pt-card" },
          React.createElement(
            "div",
            { className: "pt-card-head" },
            React.createElement(
              "div",
              null,
              React.createElement("h2", { className: "pt-card-title" }, mode === "reprovision" ? "重新配网" : "给设备配网"),
              React.createElement(
                "p",
                { className: "pt-card-sub" },
                mode === "reprovision"
                  ? "选择设备并输入新的 Wi-Fi 凭证。"
                  : "控制手通过 Wi-Fi 连接电脑。配好后页面会自动切到任务输入。"
              )
            ),
            props.onClose
              ? React.createElement("button", { type: "button", className: "pt-link", onClick: props.onClose }, "取消")
              : null
          ),
          React.createElement(
            "ol",
            { className: "pt-steps" },
            React.createElement(
              "li",
              { className: "pt-step" },
              React.createElement(
                "span",
                { className: `pt-step-num${stepOneDone ? " is-done" : ""}` },
                stepOneDone ? React.createElement(Icon, { name: "check", size: 12 }) : "1"
              ),
              React.createElement(
                "div",
                { className: "pt-step-main" },
                React.createElement(
                  "div",
                  { className: "pt-step-head" },
                  React.createElement("h3", null, "选择设备"),
                  React.createElement("span", { className: "pt-tag" }, tag)
                ),
                scanning || arming
                  ? React.createElement(
                      "div",
                      { className: "pt-scan-row" },
                      React.createElement(Spinner, { className: "pt-spinner-sm" }),
                      React.createElement(
                        "span",
                        null,
                        arming
                          ? notice || "设备正在重启进入配网…"
                          : "正在扫描附近设备，约 8 秒…"
                      )
                    )
                  : devices.length > 0
                  ? React.createElement(
                      "div",
                      { className: "pt-device-list", role: "radiogroup", "aria-label": "配网设备" },
                      devices.map((item) => {
                        const name = item.name || item.address || "未命名设备";
                        const isSelected = selected === (item.name || item.address);
                        return React.createElement(
                          "button",
                          {
                            key: name + (item.address || ""),
                            type: "button",
                            role: "radio",
                            "aria-checked": isSelected,
                            className: `pt-device-option${isSelected ? " is-selected" : ""}`,
                            onClick: () => setSelected(item.name || item.address || ""),
                            disabled: busy
                          },
                          React.createElement("span", { className: "pt-radio", "aria-hidden": "true" }),
                          React.createElement("span", { className: "pt-device-name" }, name),
                          Number.isFinite(item.rssi)
                            ? React.createElement("span", { className: "pt-device-meta" }, `信号 ${item.rssi} dBm`)
                            : null
                        );
                      })
                    )
                  : React.createElement(
                      "div",
                      { className: "pt-empty-inline" },
                      React.createElement(
                        "p",
                        { className: "pt-hint" },
                        "没发现可配网的设备。上电后等 5 秒再试；已经配过网的设备请点「配网设置 → 重新配网」。"
                      ),
                      React.createElement(
                        "button",
                        { type: "button", className: "pt-btn pt-btn-ghost pt-btn-sm", onClick: () => scan(1), disabled: busy },
                        "再扫一次"
                      )
                    ),
                error && devices.length === 0 && !scanning && !arming
                  ? React.createElement("p", { className: "pt-error", role: "alert" }, error)
                  : null,
                !scanning && !arming && devices.length > 0
                  ? React.createElement(
                      "div",
                      { className: "pt-row-hint" },
                      React.createElement("button", { type: "button", className: "pt-link", onClick: () => scan(1), disabled: busy }, "重新扫描"),
                      React.createElement("span", { className: "pt-hint" }, "上电后约 5 秒出现在列表里")
                    )
                  : null
              )
            ),
            React.createElement(
              "li",
              { className: "pt-step" },
              React.createElement("span", { className: "pt-step-num" }, "2"),
              React.createElement(
                "div",
                { className: "pt-step-main" },
                React.createElement(
                  "div",
                  { className: "pt-step-head" },
                  React.createElement("h3", null, "连接 Wi-Fi"),
                  React.createElement("span", { className: "pt-tag" }, "仅支持 2.4 GHz")
                ),
                React.createElement(
                  "div",
                  { className: "pt-fields" },
                  React.createElement(
                    "label",
                    { className: "pt-field" },
                    React.createElement("span", null, "Wi-Fi 名称"),
                    React.createElement("input", {
                      className: "pt-input",
                      value: ssid,
                      placeholder: "例如：Office",
                      autoComplete: "off",
                      spellCheck: false,
                      disabled: busy || arming,
                      onChange: (event) => setSsid(event.target.value)
                    })
                  ),
                  React.createElement(
                    "label",
                    { className: "pt-field" },
                    React.createElement("span", null, "密码"),
                    React.createElement(
                      "span",
                      { className: "pt-input-wrap" },
                      React.createElement("input", {
                        className: "pt-input",
                        type: showPassword ? "text" : "password",
                        value: password,
                        placeholder: "Wi-Fi 密码",
                        autoComplete: "off",
                        disabled: busy || arming,
                        onChange: (event) => setPassword(event.target.value)
                      }),
                      React.createElement(
                        "button",
                        {
                          type: "button",
                          className: "pt-reveal",
                          onClick: () => setShowPassword((value) => !value),
                          disabled: busy
                        },
                        showPassword ? "隐藏" : "显示"
                      )
                    )
                  )
                ),
                error && devices.length > 0
                  ? React.createElement("p", { className: "pt-error", role: "alert" }, error)
                  : null,
                React.createElement(
                  "div",
                  { className: "pt-step-actions" },
                  React.createElement(
                    "button",
                    {
                      type: "button",
                      className: "pt-btn pt-btn-primary",
                      onClick: start,
                      disabled: busy || !selected || !ssid.trim() || !password
                    },
                    busy ? "请稍候…" : "开始配网"
                  ),
                  React.createElement("span", { className: "pt-hint" }, "约 20 秒，设备会重启一次")
                )
              )
            )
          ),
          React.createElement(
            "div",
            { className: "pt-card-foot" },
            React.createElement("span", { className: `pt-dot${screen ? " is-ok" : ""}` }),
            React.createElement("span", null, screen ? `手机屏幕 ${screen[0]}×${screen[1]}` : "手机屏幕：设备上线后自动检测"),
            React.createElement("span", { className: "pt-spacer" }),
            React.createElement(
              "span",
              { className: "pt-hint" },
              currentSsid ? `当前 Wi-Fi：${currentSsid}` : "已经配过网？确认设备已上电，点「重新检测」。"
            )
          )
        );
      }

      if (phase === "intro") return renderIntro();
      if (phase === "running" || phase === "waiting" || phase === "failed" || phase === "done") {
        return renderProgress();
      }
      return renderSteps();
    }
    function useNow(intervalMs) {
      const [now, setNow] = React.useState(Date.now());
      React.useEffect(() => {
        const timer = setInterval(() => setNow(Date.now()), intervalMs);
        return () => clearInterval(timer);
      }, [intervalMs]);
      return now;
    }

    function useDeviceStatus(props, deviceName, paused) {
      const hasProbe = typeof props.deviceStatus === "function";
      const [snapshot, setSnapshot] = React.useState({
        phase: hasProbe ? "booting" : "ready",
        status: null,
        error: null,
        refreshing: false,
        checkedAt: 0,
        legacy: !hasProbe
      });
      const propsRef = React.useRef(props);
      propsRef.current = props;
      const deviceRef = React.useRef(deviceName);
      deviceRef.current = deviceName;
      const seqRef = React.useRef(0);

      const refresh = React.useCallback(async (options = {}) => {
        const probe = propsRef.current.deviceStatus;
        if (typeof probe !== "function") {
          setSnapshot((previous) =>
            Object.assign({}, previous, {
              phase: "ready",
              legacy: true,
              refreshing: false,
              checkedAt: Date.now()
            })
          );
          return null;
        }
        const sequence = ++seqRef.current;
        if (!options.quiet) {
          setSnapshot((previous) => Object.assign({}, previous, { refreshing: true }));
        }
        try {
          const value = await probe({
            force: Boolean(options.force),
            device: deviceRef.current
          });
          if (sequence !== seqRef.current) return null;
          const reachable = Boolean(value && value.device && value.device.reachable);
          setSnapshot({
            phase: reachable ? "ready" : "offline",
            status: value || null,
            error: null,
            refreshing: false,
            checkedAt: Number(value && value.checkedAt) || Date.now(),
            legacy: false
          });
          return value || null;
        } catch (error) {
          if (sequence !== seqRef.current) return null;
          const code = error && error.code ? String(error.code) : "";
          const legacy = code === "HOST_RPC_UNAVAILABLE" || code === "NOT_FOUND";
          const serviceDown = code === "SERVICE_DOWN" || code === "PHONE_SERVICE_DOWN";
          setSnapshot({
            phase: legacy ? "ready" : serviceDown ? "service_down" : "offline",
            status: null,
            error,
            refreshing: false,
            checkedAt: Date.now(),
            legacy
          });
          return null;
        }
      }, []);

      React.useEffect(() => {
        refresh({ force: true });
      }, [deviceName]);

      React.useEffect(() => {
        if (paused) return undefined;
        const timer = setInterval(() => {
          if (typeof document !== "undefined" && document.visibilityState === "hidden") return;
          refresh({ force: false, quiet: true });
        }, 5000);
        return () => clearInterval(timer);
      }, [paused, deviceName]);

      return Object.assign({}, snapshot, { refresh });
    }

    function useThumbnail(props, running, deviceName) {
      const [state, setState] = React.useState({ src: "", at: 0, loading: false, error: null });
      const propsRef = React.useRef(props);
      propsRef.current = props;
      const deviceRef = React.useRef(deviceName);
      deviceRef.current = deviceName;
      // Each host thumbnail call is a full capture; starting another one while
      // the previous is still running would just queue on the service lock.
      const inFlightRef = React.useRef(false);
      React.useEffect(() => {
        if (!running) {
          setState({ src: "", at: 0, loading: false, error: null });
          return undefined;
        }
        if (typeof props.thumbnail !== "function") return undefined;
        let cancelled = false;
        let timer = null;
        const schedule = () => {
          timer = setTimeout(tick, THUMBNAIL_POLL_MS);
        };
        const tick = async () => {
          if (cancelled) return;
          if (typeof document !== "undefined" && document.visibilityState === "hidden") {
            schedule();
            return;
          }
          if (inFlightRef.current) {
            // A slow capture (or one left over from a previous effect run) is
            // still pending: check again on the next poll instead of stacking.
            schedule();
            return;
          }
          inFlightRef.current = true;
          setState((previous) => Object.assign({}, previous, { loading: true }));
          try {
            const value = await propsRef.current.thumbnail({
              maxWidth: 240,
              device: deviceRef.current
            });
            if (cancelled) return;
            if (value && value.base64) {
              setState({
                src: `data:${value.mime || "image/png"};base64,${value.base64}`,
                at: Number(value.at) || Date.now(),
                loading: false,
                error: null
              });
            } else {
              setState((previous) => Object.assign({}, previous, { loading: false }));
            }
          } catch (error) {
            if (cancelled) return;
            const expected = error && (error.code === "BUSY" || error.code === "SCREENSHOT_EMPTY");
            // Keep the last good frame: only merge the error/loading flags, so a
            // failed poll never blanks the image the agent last produced.
            setState((previous) =>
              Object.assign({}, previous, { loading: false, error: expected ? null : error })
            );
          } finally {
            inFlightRef.current = false;
            if (!cancelled) schedule();
          }
        };
        tick();
        return () => {
          cancelled = true;
          if (timer) clearTimeout(timer);
        };
      }, [running, deviceName, props.thumbnail]);
      return state;
    }

    function TaskComposer(props) {
      const [scheduleOpen, setScheduleOpen] = React.useState(false);
      const [customMinutes, setCustomMinutes] = React.useState("15");
      const [customTime, setCustomTime] = React.useState(timeInputValue(Date.now() + 15 * 60000));
      const [scheduleBusy, setScheduleBusy] = React.useState(false);
      const [scheduleError, setScheduleError] = React.useState("");
      const wrapRef = React.useRef(null);
      const textareaRef = React.useRef(null);
      // Show every template: slicing to 4 hid newly added ones entirely.
      const templates = Array.isArray(props.templates) ? props.templates : [];
      const disabled = Boolean(props.disabled);
      const timeAt = atFromClock(customTime);

      React.useEffect(() => {
        if (!scheduleOpen) return undefined;
        const onPointer = (event) => {
          if (wrapRef.current && !wrapRef.current.contains(event.target)) setScheduleOpen(false);
        };
        const onKey = (event) => {
          if (event.key === "Escape") setScheduleOpen(false);
        };
        if (typeof document !== "undefined" && document.addEventListener) {
          document.addEventListener("mousedown", onPointer);
          document.addEventListener("keydown", onKey);
        }
        return () => {
          if (typeof document !== "undefined" && document.removeEventListener) {
            document.removeEventListener("mousedown", onPointer);
            document.removeEventListener("keydown", onKey);
          }
        };
      }, [scheduleOpen]);

      function chooseTemplate(template) {
        props.onTextChange(template.text);
        setScheduleOpen(false);
        setTimeout(() => {
          const element = textareaRef.current;
          if (!element) return;
          element.focus();
          const end = template.text.length;
          try {
            element.setSelectionRange(end, end);
          } catch (_) {}
        }, 0);
      }

      async function submitSchedule(payload) {
        setScheduleBusy(true);
        setScheduleError("");
        try {
          await props.onSchedule(payload);
          setScheduleOpen(false);
        } catch (error) {
          setScheduleError(friendlyError(error));
        } finally {
          setScheduleBusy(false);
        }
      }

      function submitMinutes(minutes) {
        const value = Number(minutes);
        if (!Number.isFinite(value) || value < 1 || value > MAX_MINUTES) {
          setScheduleError(`分钟数需在 1–${MAX_MINUTES} 之间`);
          return;
        }
        submitSchedule({ minutes: value });
      }

      function submitTime() {
        if (!timeAt) {
          setScheduleError("请选择有效时间");
          return;
        }
        submitSchedule({ at: timeAt });
      }

      return React.createElement(
        "section",
        { className: "pt-card pt-composer" },
        React.createElement(
          "div",
          { className: "pt-composer-head" },
          React.createElement("h2", { className: "pt-card-title" }, "要它做什么？"),
          props.onManageTemplates
            ? React.createElement(
                "button",
                { type: "button", className: "pt-link", onClick: props.onManageTemplates },
                "管理模板"
              )
            : null
        ),
        templates.length > 0
          ? React.createElement(
              "div",
              { className: "pt-chips", "aria-label": "常用任务" },
              templates.map((template, index) =>
                React.createElement(
                  "button",
                  {
                    key: `${template.label || template.name || "template"}-${index}`,
                    type: "button",
                    className: "pt-chip",
                    title: template.text || template.objective || "",
                    onClick: () => chooseTemplate(template),
                    disabled: disabled || props.busy
                  },
                  template.label || template.name || "任务"
                )
              )
            )
          : null,
        React.createElement("textarea", {
          ref: textareaRef,
          className: "pt-textarea",
          value: props.text,
          placeholder: "例如：打开京东，领取每日签到奖励",
          disabled: disabled || props.busy,
          rows: 3,
          onChange: (event) => {
            props.onTextChange(event.target.value);
            if (props.error && props.onClearError) props.onClearError();
          },
          onKeyDown: (event) => {
            if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent?.isComposing) {
              event.preventDefault();
              if (!disabled && !props.busy && props.text.trim()) props.onStart();
            }
          }
        }),
        props.disabledReason
          ? React.createElement("p", { className: "pt-warn" }, props.disabledReason)
          : null,
        props.error ? React.createElement("p", { className: "pt-error", role: "alert" }, props.error) : null,
        React.createElement(
          "div",
          { className: "pt-composer-foot" },
          React.createElement("span", { className: "pt-hint" }, "Enter 开始执行 · Shift + Enter 换行"),
          React.createElement(
            "div",
            { className: "pt-actions" },
            React.createElement(
              "div",
              { className: "pt-popover-wrap", ref: wrapRef },
              React.createElement(
                "button",
                {
                  type: "button",
                  className: "pt-btn pt-btn-ghost pt-btn-sm",
                  onClick: () => {
                    setScheduleError("");
                    setScheduleOpen((value) => !value);
                  },
                  disabled: disabled || props.busy || !props.text.trim(),
                  "aria-haspopup": "dialog",
                  "aria-expanded": scheduleOpen
                },
                React.createElement(Icon, { name: "clock", size: 14 }),
                "定时"
              ),
              scheduleOpen
                ? React.createElement(
                    "div",
                    { className: "pt-popover", role: "dialog", "aria-label": "定时执行" },
                    React.createElement("p", { className: "pt-popover-title" }, "什么时候执行？"),
                    React.createElement(
                      "div",
                      { className: "pt-presets" },
                      [5, 15, 30, 60].map((minutes) =>
                        React.createElement(
                          "button",
                          {
                            key: minutes,
                            type: "button",
                            className: "pt-preset",
                            onClick: () => submitMinutes(minutes),
                            disabled: scheduleBusy
                          },
                          `${minutes} 分钟后`
                        )
                      )
                    ),
                    React.createElement(
                      "div",
                      { className: "pt-custom" },
                      React.createElement("input", {
                        className: "pt-input pt-input-sm",
                        type: "number",
                        min: "1",
                        max: String(MAX_MINUTES),
                        value: customMinutes,
                        "aria-label": "自定义分钟数",
                        onChange: (event) => setCustomMinutes(event.target.value)
                      }),
                      React.createElement("span", { className: "pt-hint" }, "分钟后"),
                      React.createElement(
                        "button",
                        {
                          type: "button",
                          className: "pt-btn pt-btn-ghost pt-btn-sm",
                          onClick: () => submitMinutes(customMinutes),
                          disabled: scheduleBusy
                        },
                        "确定"
                      )
                    ),
                    React.createElement("div", { className: "pt-popover-divider" }),
                    React.createElement(
                      "div",
                      { className: "pt-custom" },
                      React.createElement("input", {
                        className: "pt-input pt-input-sm pt-time",
                        type: "time",
                        value: customTime,
                        "aria-label": "指定执行时间",
                        onChange: (event) => setCustomTime(event.target.value)
                      }),
                      React.createElement(
                        "button",
                        {
                          type: "button",
                          className: "pt-btn pt-btn-ghost pt-btn-sm",
                          onClick: submitTime,
                          disabled: scheduleBusy || !timeAt
                        },
                        timeAt ? "排期" : "选择时间"
                      )
                    ),
                    timeAt
                      ? React.createElement("p", { className: "pt-hint" }, `将安排在 ${formatTime(timeAt)}`)
                      : null,
                    scheduleError ? React.createElement("p", { className: "pt-error" }, scheduleError) : null,
                    React.createElement("p", { className: "pt-hint" }, "定时任务需要 DSH 保持运行。")
                  )
                : null
            ),
            React.createElement(
              "button",
              {
                type: "button",
                className: "pt-btn pt-btn-primary pt-btn-sm",
                onClick: props.onStart,
                disabled: disabled || props.busy || !props.text.trim()
              },
              props.busy ? "正在启动…" : "开始执行"
            )
          )
        )
      );
    }

    function RunningTask(props) {
      const task = props.task;
      const now = useNow(1000);
      const [stopping, setStopping] = React.useState(false);
      const action = formatAction(task.lastAction);
      const meta = [];
      if (task.status === "starting") meta.push("正在启动");
      else if (Number(task.steps) > 0) {
        meta.push(
          Number(task.total) > 0
            ? `第 ${Number(task.steps)}/${Number(task.total)} 步`
            : `第 ${Number(task.steps)} 步`
        );
      }
      if (task.phase === "env") meta.push("确认当前环境");
      else if (task.phase === "entry") meta.push("到达起始页");
      else if (task.phase === "guided") meta.push(task.guidedFull ? "自动执行中" : "自动导航中");
      else if (task.phase === "main") meta.push("执行主路径");
      meta.push(formatClock(now - Number(task.startedAt || now)));
      if (action) meta.push(action);
      const stop = async () => {
        if (stopping) return;
        setStopping(true);
        try {
          await props.onStop();
        } finally {
          setStopping(false);
        }
      };
      return React.createElement(
        "section",
        { className: "pt-card pt-running" },
        React.createElement(
          "button",
          {
            type: "button",
            className: "pt-thumb",
            onClick: props.onPreview,
            title: "查看手机画面",
            "aria-label": "查看手机画面"
          },
          props.thumbnail && props.thumbnail.src
            ? React.createElement("img", { src: props.thumbnail.src, alt: "手机当前画面" })
            : React.createElement(
                "span",
                { className: "pt-thumb-empty" },
                React.createElement(Icon, { name: "phone", size: 18 })
              )
        ),
        React.createElement(
          "div",
          { className: "pt-running-main" },
          React.createElement(
            "div",
            { className: "pt-running-state" },
            React.createElement("span", { className: "pt-dot" }),
            task.status === "starting" ? "正在启动…" : "执行中"
          ),
          React.createElement("h2", { className: "pt-running-title" }, task.objective),
          React.createElement("p", { className: "pt-running-meta" }, meta.join(" · "))
        ),
        React.createElement(
          "div",
          { className: "pt-actions" },
          React.createElement(
            "button",
            {
              type: "button",
              className: "pt-btn pt-btn-ghost pt-btn-sm",
              onClick: () => props.onOpen(task.sessionId),
              disabled: !task.sessionId
            },
            "查看会话"
          ),
          React.createElement(
            "button",
            { type: "button", className: "pt-btn pt-btn-danger pt-btn-sm", onClick: stop, disabled: stopping },
            stopping ? "停止中…" : "停止"
          )
        )
      );
    }

    function ResultCard(props) {
      const task = props.task;
      const isTemplate = task.mode === "template";
      const error = normalizeError(task.error);
      const status = task.status === "done" ? "done" : task.status === "stopped" ? "stopped" : "error";
      const title = isTemplate
        ? status === "done" ? "模板执行完成" : status === "stopped" ? "模板执行已停止" : "模板执行失败"
        : status === "done" ? "已完成" : status === "stopped" ? "已停止" : "没有完成";
      const duration = task.finishedAt && task.startedAt ? Number(task.finishedAt) - Number(task.startedAt) : 0;
      const update = task.templateUpdate || null;
      const meta = [];
      if (task.finishedAt) meta.push(formatTime(task.finishedAt));
      if (duration > 0) meta.push(`用时 ${formatDuration(duration)}`);
      if (update && update.newVersion && update.newVersion !== update.version) {
        meta.push(`模板 v${update.version} → v${update.newVersion}`);
      }
      if (isTemplate && task.envAtEntry) meta.push("已在起始页，跳过到达段");
      if (isTemplate && task.guidedFull) meta.push("已全程自动执行");
      else if (isTemplate && task.guided) meta.push("已自动导航到起始页");
      const learned = task.learned;
      if (isTemplate && learned && learned.steps > 0) {
        meta.push(
          `已写回到达路径（${learned.steps} 步` +
            (learned.newVersion ? `，v${learned.version} → v${learned.newVersion}` : "") +
            "）"
        );
      }
      return React.createElement(
        "section",
        { className: `pt-card pt-result pt-result-${status}` },
        React.createElement(
          "div",
          { className: "pt-result-main" },
          React.createElement(
            "div",
            { className: "pt-result-title" },
            React.createElement(
              "span",
              { className: `pt-state-icon is-${status === "error" ? "error" : status === "done" ? "ok" : "stopped"}` },
              React.createElement(Icon, { name: status === "error" ? "alert" : status === "done" ? "check" : "stop", size: 11 })
            ),
            React.createElement("span", null, title),
            meta.length > 0 ? React.createElement("span", { className: "pt-result-meta" }, meta.join(" · ")) : null
          ),
          React.createElement("h2", { className: "pt-running-title" }, task.objective),
          status === "error" && error
            ? React.createElement("p", { className: "pt-result-reason" }, friendlyError(Object.assign({ code: error.code }, error)))
            : null,
          isTemplate && Number(task.proposals) > 0
            ? React.createElement(
                "p",
                { className: "pt-hint" },
                `复盘：${task.proposals} 条建议` +
                  (update && update.newVersion
                    ? `，已自动应用保守修正到 v${update.newVersion}`
                    : "，可在「管理模板」里查看")
              )
            : null
        ),
        React.createElement(
          "div",
          { className: "pt-actions" },
          React.createElement(
            "button",
            {
              type: "button",
              className: "pt-btn pt-btn-ghost pt-btn-sm",
              onClick: () => props.onOpen(task.sessionId),
              disabled: !task.sessionId
            },
            "查看会话"
          ),
          React.createElement(
            "button",
            {
              type: "button",
              className: "pt-btn pt-btn-ghost pt-btn-sm",
              onClick: () =>
                isTemplate && props.onRetryTemplate
                  ? props.onRetryTemplate(task.templateId)
                  : props.onRetry(task.objective),
              disabled: props.busy
            },
            isTemplate ? "再执行一次" : status === "error" ? "重试" : "再来一次"
          ),
          status !== "error" && !isTemplate
            ? React.createElement(
                "button",
                {
                  type: "button",
                  className: "pt-btn pt-btn-ghost pt-btn-sm",
                  onClick: () => props.onSaveTemplate(task.objective, task.hostTaskId)
                },
                "存为模板"
              )
            : null,
          React.createElement(
            "button",
            {
              type: "button",
              className: "pt-icon-btn",
              "aria-label": "关闭结果",
              title: "关闭结果",
              onClick: props.onDismiss
            },
            React.createElement(Icon, { name: "x", size: 12 })
          )
        )
      );
    }

    function TaskRow(props) {
      const item = props.item;
      const status = item.status || "done";
      const error = normalizeError(item.error);
      const iconName = status === "done" ? "check" : status === "error" ? "x" : status === "stopped" ? "stop" : "alert";
      const iconTone = status === "done" ? "ok" : status === "error" ? "error" : "neutral";
      const time = formatTime(item.finishedAt || item.startedAt || item.at);
      const canReplay = item.source === "host" && (status === "done" || status === "error" || status === "stopped");
      const canDelete =
        typeof props.onDelete === "function" && !["starting", "running"].includes(status);
      const [confirming, setConfirming] = React.useState(false);
      const [deleting, setDeleting] = React.useState(false);

      async function remove() {
        setDeleting(true);
        try {
          await props.onDelete(item);
        } catch (_) {
          // Keep the row when the host could not delete it; the page shows why.
          setDeleting(false);
          setConfirming(false);
        }
      }

      const actions = [];
      if (item.sessionId) {
        actions.push(
          React.createElement(
            "button",
            {
              key: "open",
              type: "button",
              className: "pt-link",
              onClick: () => props.onOpen(item.sessionId)
            },
            "查看"
          )
        );
      }
      if (canReplay) {
        // Same action for every status: create a NEW agent task with the same
        // objective. "重放" used to imply template replay, which it is not.
        actions.push(
          React.createElement(
            "button",
            {
              key: "replay",
              type: "button",
              className: "pt-link",
              title: "让 agent 用同样的目标重新执行一遍（不是执行模板）",
              onClick: () => props.onReplay(item)
            },
            "再执行一次"
          )
        );
      }
      const canSaveTemplate =
        typeof props.onSaveTemplate === "function" &&
        status !== "error" &&
        (item.source === "host" || Boolean(item.hostTaskId));
      if (canSaveTemplate) {
        actions.push(
          React.createElement(
            "button",
            {
              key: "template",
              type: "button",
              className: "pt-link",
              title: "总结这次执行并生成模板",
              onClick: () => props.onSaveTemplate(item)
            },
            "存为模板"
          )
        );
      }
      if (canDelete) {
        if (confirming) {
          actions.push(
            React.createElement(
              "button",
              {
                key: "confirm",
                type: "button",
                className: "pt-link pt-link-danger",
                onClick: remove,
                disabled: deleting,
                title: "只删除这条记录，不删除会话"
              },
              deleting ? "删除中…" : "确认删除"
            ),
            React.createElement(
              "button",
              {
                key: "cancel",
                type: "button",
                className: "pt-link",
                onClick: () => setConfirming(false),
                disabled: deleting
              },
              "取消"
            )
          );
        } else {
          actions.push(
            React.createElement(
              "button",
              {
                key: "delete",
                type: "button",
                className: "pt-link",
                onClick: () => setConfirming(true),
                title: "只删除这条记录，不删除会话"
              },
              "删除"
            )
          );
        }
      }

      return React.createElement(
        "li",
        { className: "pt-task-row" },
        React.createElement(
          "span",
          { className: `pt-state-icon is-${iconTone}` },
          React.createElement(Icon, { name: iconName, size: 10 })
        ),
        React.createElement(
          "div",
          { className: "pt-task-body" },
          React.createElement("span", { className: "pt-task-title", title: item.objective }, item.objective),
          status === "missed"
            ? React.createElement("span", { className: "pt-task-sub" }, "页面关闭期间错过")
            : status === "interrupted"
            ? React.createElement("span", { className: "pt-task-sub" }, "页面关闭时中断")
            : error
            ? React.createElement("span", { className: "pt-task-sub" }, error.message)
            : null
        ),
        time ? React.createElement("span", { className: "pt-task-meta" }, time) : null,
        actions.length > 0
          ? React.createElement("span", { className: "pt-task-actions" }, actions)
          : null
      );
    }

    function RecentTasks(props) {
      const items = Array.isArray(props.items) ? props.items : [];
      const shown = items.slice(0, 3);
      return React.createElement(
        "section",
        { className: "pt-section" },
        React.createElement(
          "div",
          { className: "pt-section-head" },
          React.createElement("h2", { className: "pt-section-title" }, "最近任务"),
          items.length > 0 && props.onShowAll
            ? React.createElement(
                "button",
                { type: "button", className: "pt-link", onClick: props.onShowAll },
                "全部记录"
              )
            : null
        ),
        items.length === 0
          ? React.createElement("p", { className: "pt-empty" }, props.emptyText || "还没有任务，试试上面的模板。")
          : React.createElement(
              "ul",
              { className: "pt-task-list" },
              shown.map((item) =>
                React.createElement(TaskRow, {
                  key: item.id,
                  item,
                  onOpen: props.onOpen,
                  onReplay: props.onReplay,
                  onDelete: props.onDelete,
                  onSaveTemplate: props.onSaveTemplate
                })
              )
            )
      );
    }

    function HistoryPanel(props) {
      const items = Array.isArray(props.items) ? props.items : [];
      const canClear = typeof props.onClear === "function" && items.length > 0;
      const [confirmClear, setConfirmClear] = React.useState(false);
      const [clearing, setClearing] = React.useState(false);

      async function clear() {
        setClearing(true);
        try {
          await props.onClear();
        } catch (_) {
          // Keep the records when the host could not delete them.
          setClearing(false);
          setConfirmClear(false);
        }
      }

      return React.createElement(
        Modal,
        { title: "任务记录", onClose: props.onClose, wide: true },
        React.createElement(
          "div",
          { className: "pt-section-head pt-history-head" },
          React.createElement(
            "span",
            { className: "pt-hint" },
            items.length > 0 ? `共 ${items.length} 条记录 · 只删除记录，不删除会话` : ""
          ),
          canClear
            ? confirmClear
              ? React.createElement(
                  React.Fragment,
                  null,
                  React.createElement(
                    "button",
                    {
                      type: "button",
                      className: "pt-link pt-link-danger",
                      onClick: clear,
                      disabled: clearing
                    },
                    clearing ? "清空中…" : "确认清空"
                  ),
                  React.createElement(
                    "button",
                    {
                      type: "button",
                      className: "pt-link",
                      onClick: () => setConfirmClear(false),
                      disabled: clearing
                    },
                    "取消"
                  )
                )
              : React.createElement(
                  "button",
                  {
                    type: "button",
                    className: "pt-link pt-link-danger",
                    onClick: () => setConfirmClear(true)
                  },
                  "清空记录"
                )
            : null
        ),
        items.length === 0
          ? React.createElement("p", { className: "pt-empty" }, "还没有任务记录")
          : React.createElement(
              "ul",
              { className: "pt-task-list pt-task-list-modal" },
              items.map((item) =>
                React.createElement(TaskRow, {
                  key: item.id,
                  item,
                  onOpen: props.onOpen,
                  onReplay: props.onReplay,
                  onDelete: props.onDelete,
                  onSaveTemplate: props.onSaveTemplate
                })
              )
            )
      );
    }

    function ScheduledList(props) {
      const [cancelling, setCancelling] = React.useState("");
      const items = Array.isArray(props.items) ? props.items : [];
      if (items.length === 0) return null;
      const cancel = async (id) => {
        setCancelling(String(id));
        try {
          await props.onCancel(id);
        } finally {
          setCancelling("");
        }
      };
      return React.createElement(
        "section",
        { className: "pt-section" },
        React.createElement(
          "div",
          { className: "pt-section-head" },
          React.createElement("h2", { className: "pt-section-title" }, "已排期")
        ),
        React.createElement(
          "ul",
          { className: "pt-task-list" },
          items.map((item) =>
            React.createElement(
              "li",
              { key: item.id, className: "pt-task-row" },
              React.createElement(
                "span",
                { className: "pt-state-icon is-neutral" },
                React.createElement(Icon, { name: "clock", size: 10 })
              ),
              React.createElement(
                "div",
                { className: "pt-task-body" },
                React.createElement("span", { className: "pt-task-title" }, item.objective),
                React.createElement("span", { className: "pt-task-sub is-muted" }, `${formatTime(item.at)} · 需要 DSH 保持运行`)
              ),
              props.onEdit
                ? React.createElement(
                    "button",
                    { type: "button", className: "pt-link", onClick: () => props.onEdit(item) },
                    "编辑"
                  )
                : null,
              React.createElement(
                "button",
                {
                  type: "button",
                  className: "pt-link",
                  onClick: () => cancel(item.id),
                  disabled: cancelling === String(item.id)
                },
                cancelling === String(item.id) ? "取消中…" : "取消"
              )
            )
          )
        )
      );
    }

    function OfflineNotice(props) {
      return React.createElement(
        "section",
        { className: "pt-card" },
        React.createElement("h2", { className: "pt-card-title" }, "设备未连接"),
        React.createElement(
          "p",
          { className: "pt-card-sub" },
          "任务和模板照常可以查看、重命名、编辑；连上设备后才能执行。"
        ),
        React.createElement(
          "div",
          { className: "pt-actions" },
          props.onSetup
            ? React.createElement(
                "button",
                { type: "button", className: "pt-btn pt-btn-primary pt-btn-sm", onClick: props.onSetup },
                "去配网"
              )
            : null,
          React.createElement(
            "button",
            {
              type: "button",
              className: "pt-btn pt-btn-ghost pt-btn-sm",
              onClick: props.onRetry,
              disabled: props.refreshing
            },
            props.refreshing ? "检测中…" : "重新检测"
          )
        )
      );
    }

    function EyeDownNotice(props) {
      const reason = String(props.reason || "").trim();
      return React.createElement(
        "section",
        { className: "pt-card" },
        React.createElement("h2", { className: "pt-card-title" }, "读不到手机画面"),
        React.createElement(
          "p",
          { className: "pt-card-sub" },
          "控制手正常，但屏幕采集（adb/scrcpy）断开了，任务会在预检阶段被拦下。" +
            (reason ? `原因：${reason}` : "")
        ),
        React.createElement(
          "div",
          { className: "pt-actions" },
          React.createElement(
            "button",
            {
              type: "button",
              className: "pt-btn pt-btn-primary pt-btn-sm",
              onClick: props.onReconnect,
              disabled: props.reconnecting
            },
            props.reconnecting ? "重连中…" : "重连 ADB"
          ),
          React.createElement(
            "button",
            {
              type: "button",
              className: "pt-btn pt-btn-ghost pt-btn-sm",
              onClick: props.onRetry,
              disabled: props.refreshing
            },
            props.refreshing ? "检测中…" : "重新检测"
          )
        ),
        React.createElement(
          "p",
          { className: "pt-hint" },
          "重连会先试上次的无线调试地址，再试 mDNS 发现；都不行时需要在手机上重新配对无线调试。"
        )
      );
    }

    function TemplateEditor(props) {
      const file = props.file || {};
      const [content, setContent] = React.useState(file.content || "");
      const [error, setError] = React.useState(file.error || "");
      const [saving, setSaving] = React.useState(false);
      const initialRef = React.useRef(file.content || "");
      const dirty = content !== initialRef.current;

      React.useEffect(() => {
        // A new source file (same mounted component) must replace the draft;
        // identical content leaves the user's edits alone.
        if (file.content === initialRef.current) return;
        initialRef.current = file.content || "";
        setContent(file.content || "");
        setError(file.error || "");
      }, [file.path, file.content, file.error]);

      async function save() {
        setSaving(true);
        setError("");
        try {
          await props.onSave(content);
        } catch (saveError) {
          setError(friendlyError(saveError));
          setSaving(false);
        }
      }

      return React.createElement(
        Modal,
        {
          title: `编辑模板：${file.label || file.name || file.id || ""}`,
          wide: true,
          onClose: props.onClose
        },
        React.createElement(
          "div",
          { className: "pt-manager" },
          file.path
            ? React.createElement("p", { className: "pt-hint" }, file.path)
            : null,
          React.createElement("textarea", {
            className: "pt-textarea",
            style: {
              minHeight: "360px",
              fontFamily: "ui-monospace, SFMono-Regular, Menlo, monospace",
              fontSize: "12px",
              lineHeight: "1.55"
            },
            value: content,
            spellCheck: false,
            disabled: saving,
            onChange: (event) => setContent(event.target.value)
          }),
          error ? React.createElement("p", { className: "pt-error", role: "alert" }, error) : null,
          React.createElement(
            "div",
            { className: "pt-actions", style: { marginTop: "10px" } },
            React.createElement(
              "button",
              {
                type: "button",
                className: "pt-btn pt-btn-ghost pt-btn-sm",
                onClick: props.onClose,
                disabled: saving
              },
              "取消"
            ),
            React.createElement(
              "button",
              {
                type: "button",
                className: "pt-btn pt-btn-primary pt-btn-sm",
                onClick: save,
                disabled: saving || !dirty
              },
              saving ? "保存中…" : "保存 md"
            )
          ),
          React.createElement(
            "p",
            { className: "pt-hint" },
            "保存前会校验 frontmatter、## 步骤 和 steps:；改 name 会同步到模板列表。"
          )
        )
      );
    }

    const REVIEW_KIND_LABELS = {
      promote_coords: "升级为坐标点击",
      demote_coords: "降级为文字点击",
      timeout_raise: "调大 timeout",
      timeout_lower: "收紧 timeout",
      widen_roi: "放宽 ROI"
    };

    function TemplateReviewPanel(props) {
      const review = props.review || {};
      const proposals = Array.isArray(review.proposals) ? review.proposals : [];
      const samples = review.samples || {};
      const [selected, setSelected] = React.useState(
        () => new Set(proposals.map((proposal) => proposal.uid))
      );
      const [busy, setBusy] = React.useState("");
      const [diff, setDiff] = React.useState("");
      const [error, setError] = React.useState("");
      const [message, setMessage] = React.useState("");
      const [confirmRollback, setConfirmRollback] = React.useState(false);

      function toggle(uid) {
        const next = new Set(selected);
        if (next.has(uid)) next.delete(uid);
        else next.add(uid);
        setSelected(next);
      }

      async function preview() {
        setBusy("preview");
        setError("");
        setMessage("");
        try {
          const result = await props.onApply([...selected], true);
          setDiff((result && result.diff) || "（没有 diff，可能没有可应用的改动）");
        } catch (previewError) {
          setError(friendlyError(previewError));
        } finally {
          setBusy("");
        }
      }

      async function apply() {
        setBusy("apply");
        setError("");
        setMessage("");
        try {
          const result = await props.onApply([...selected], false);
          const applied = Array.isArray(result && result.applied) ? result.applied.length : 0;
          const skipped = Array.isArray(result && result.skipped) ? result.skipped.length : 0;
          const versions =
            result && result.new_version && result.new_version !== result.version
              ? `，v${result.version} → v${result.new_version}`
              : "";
          setMessage(`已应用 ${applied} 条${versions}${skipped ? `，跳过 ${skipped} 条` : ""}`);
          if (result && result.diff) setDiff(result.diff);
          if (props.onApplied) await props.onApplied();
        } catch (applyError) {
          setError(friendlyError(applyError));
        } finally {
          setBusy("");
        }
      }

      async function rollback() {
        setBusy("rollback");
        setError("");
        setMessage("");
        try {
          const result = await props.onRollback();
          setConfirmRollback(false);
          setMessage(
            result
              ? `已回滚：v${result.from_version} → v${result.to_version}`
              : "已回滚上一版"
          );
          if (props.onApplied) await props.onApplied();
        } catch (rollbackError) {
          setError(friendlyError(rollbackError));
        } finally {
          setBusy("");
        }
      }

      return React.createElement(
        Modal,
        {
          title: `复盘建议：${review.name || (props.template && props.template.label) || ""}`,
          wide: true,
          onClose: props.onClose
        },
        React.createElement(
          "div",
          { className: "pt-manager" },
          React.createElement(
            "p",
            { className: "pt-hint" },
            `样本 ${samples.runs || 0} 次，成功 ${samples.ok_runs || 0}/${samples.runs || 0}` +
              (samples.enough === false ? "（样本不足，建议再跑几次）" : "")
          ),
          proposals.length === 0
            ? React.createElement("p", { className: "pt-empty" }, "没有可应用的机械改动；语义调整请看 review.md")
            : React.createElement(
                "div",
                { className: "pt-review-list" },
                proposals.map((proposal) =>
                  React.createElement(
                    "label",
                    { className: "pt-review-row", key: proposal.uid || proposal.id },
                    React.createElement("input", {
                      type: "checkbox",
                      checked: selected.has(proposal.uid),
                      onChange: () => toggle(proposal.uid)
                    }),
                    React.createElement(
                      "span",
                      { className: "pt-review-main" },
                      React.createElement(
                        "span",
                        { className: "pt-review-title" },
                        `${proposal.step_id} · ${REVIEW_KIND_LABELS[proposal.kind] || proposal.kind}` +
                          (proposal.confidence === "low" ? "（样本偏低）" : "")
                      ),
                      React.createElement(
                        "span",
                        { className: "pt-review-reason" },
                        proposal.reason || ""
                      )
                    )
                  )
                )
              ),
          error ? React.createElement("p", { className: "pt-error", role: "alert" }, error) : null,
          message ? React.createElement("p", { className: "pt-hint" }, message) : null,
          diff ? React.createElement("pre", { className: "pt-diff" }, diff) : null,
          React.createElement(
            "div",
            { className: "pt-actions", style: { marginTop: "10px" } },
            React.createElement(
              "button",
              {
                type: "button",
                className: "pt-btn pt-btn-ghost pt-btn-sm",
                onClick: props.onClose
              },
              "关闭"
            ),
            React.createElement(
              "button",
              {
                type: "button",
                className: "pt-btn pt-btn-ghost pt-btn-sm",
                onClick: preview,
                disabled: busy || selected.size === 0
              },
              busy === "preview" ? "生成 diff…" : "预览 diff"
            ),
            React.createElement(
              "button",
              {
                type: "button",
                className: "pt-btn pt-btn-primary pt-btn-sm",
                onClick: apply,
                disabled: busy || selected.size === 0
              },
              busy === "apply" ? "应用中…" : `应用选中（${selected.size}）`
            ),
            confirmRollback
              ? React.createElement(
                  "button",
                  {
                    type: "button",
                    className: "pt-link pt-link-danger",
                    onClick: rollback,
                    disabled: busy
                  },
                  busy === "rollback" ? "回滚中…" : "确认回滚"
                )
              : React.createElement(
                  "button",
                  {
                    type: "button",
                    className: "pt-link",
                    onClick: () => setConfirmRollback(true),
                    disabled: busy
                  },
                  "回滚上一版"
                )
          ),
          React.createElement(
            "p",
            { className: "pt-hint" },
            "只应用机械改动（坐标 / timeout / ROI）；旧版本会备份到 templates/.versions。"
          )
        )
      );
    }

    function TemplateManager(props) {
      const templates = Array.isArray(props.templates) ? props.templates : [];
      const [name, setName] = React.useState(
        props.currentText ? props.currentText.slice(0, 20) : ""
      );
      const [objective, setObjective] = React.useState(props.currentText || "");
      const [busy, setBusy] = React.useState(false);
      const [error, setError] = React.useState("");
      const [notice, setNotice] = React.useState("");
      const [confirmId, setConfirmId] = React.useState("");
      const [renamingId, setRenamingId] = React.useState("");
      const [renameValue, setRenameValue] = React.useState("");
      const [editor, setEditor] = React.useState(null);
      const [adding, setAdding] = React.useState(templates.length === 0);
      const [hostVersion, setHostVersion] = React.useState(null);
      const [hostChecked, setHostChecked] = React.useState(false);
      const [reviews, setReviews] = React.useState({});
      const [reviewId, setReviewId] = React.useState("");
      const hostHint = staleHostHint(hostVersion);

      async function loadReview(id) {
        if (!id || typeof props.onReview !== "function") return;
        setReviews((previous) => ({ ...previous, [id]: { loading: true } }));
        try {
          const data = await props.onReview(id);
          setReviews((previous) => ({ ...previous, [id]: { loading: false, data } }));
        } catch (reviewError) {
          setReviews((previous) => ({
            ...previous,
            [id]: { loading: false, error: friendlyError(reviewError) }
          }));
        }
      }

      React.useEffect(() => {
        templates
          .filter((template) => template.id && template.file)
          .forEach((template) => {
            loadReview(template.id);
          });
      }, []);

      function reviewLink(template) {
        const entry = reviews[template.id];
        const count =
          entry && entry.data && Array.isArray(entry.data.proposals)
            ? entry.data.proposals.length
            : 0;
        if (!count) return null;
        return React.createElement(
          "button",
          {
            type: "button",
            className: "pt-link",
            onClick: () => setReviewId(template.id)
          },
          `建议 ${count}`
        );
      }

      React.useEffect(() => {
        let alive = true;
        if (typeof props.onHostInfo !== "function") {
          setHostChecked(true);
          return undefined;
        }
        Promise.resolve(props.onHostInfo())
          .then((info) => {
            if (!alive) return;
            setHostVersion(info && info.version ? String(info.version) : "");
            setHostChecked(true);
          })
          .catch(() => {
            if (alive) {
              setHostVersion("");
              setHostChecked(true);
            }
          });
        return () => {
          alive = false;
        };
      }, []);

      async function save() {
        if (!objective.trim()) {
          setError("请填写任务内容");
          return;
        }
        setBusy(true);
        setError("");
        setNotice("");
        try {
          await props.onSave({
            name: name.trim() || objective.trim().slice(0, 30),
            objective: objective.trim()
          });
          setNotice("已保存模板");
          setAdding(false);
          setName("");
          setObjective("");
        } catch (saveError) {
          setError(friendlyError(saveError));
        } finally {
          setBusy(false);
        }
      }

      async function remove(id) {
        setBusy(true);
        setError("");
        setNotice("");
        try {
          await props.onDelete(id);
          setNotice("已删除模板");
          setConfirmId("");
        } catch (deleteError) {
          setError(friendlyError(deleteError));
        } finally {
          setBusy(false);
        }
      }

      function startRename(template) {
        setError("");
        setNotice("");
        setConfirmId("");
        setRenamingId(template.id);
        setRenameValue(template.label || template.name || "");
      }

      async function submitRename(id) {
        const next = String(renameValue || "").trim();
        if (!next) {
          setError("名称不能为空");
          return;
        }
        setBusy(true);
        setError("");
        setNotice("");
        try {
          await props.onRename(id, next);
          setNotice("已重命名");
          setRenamingId("");
        } catch (renameError) {
          setError(friendlyError(renameError));
        } finally {
          setBusy(false);
        }
      }

      async function openEditor(template) {
        setError("");
        setNotice("");
        setConfirmId("");
        setRenamingId("");
        if (!template.id) {
          setError("这个模板没有 id，无法读取文件");
          return;
        }
        setEditor({ id: template.id, label: template.label, name: template.name, path: "", content: "", loading: true });
        try {
          const file = await props.onReadFile(template.id);
          setEditor({
            id: template.id,
            label: template.label || file.name,
            name: file.name,
            path: file.path,
            content: file.content || ""
          });
        } catch (readError) {
          setEditor(null);
          setError(friendlyError(readError));
        }
      }

      async function saveEditor(content) {
        if (!editor) return;
        await props.onWriteFile(editor.id, content);
        setEditor(null);
        setNotice("md 已保存");
      }

      const manager = React.createElement(
        Modal,
        { title: "模板", onClose: props.onClose },
        React.createElement(
          "div",
          { className: "pt-manager" },
          adding
            ? React.createElement(
                "div",
                { className: "pt-manager-add" },
            React.createElement("h3", { className: "pt-manager-title" }, "新增模板"),
            React.createElement(
              "label",
              { className: "pt-field" },
              React.createElement("span", null, "名称"),
              React.createElement("input", {
                className: "pt-input",
                value: name,
                placeholder: "例如：京东价保",
                onChange: (event) => setName(event.target.value)
              })
            ),
            React.createElement(
              "label",
              { className: "pt-field" },
              React.createElement("span", null, "任务内容"),
              React.createElement("textarea", {
                className: "pt-textarea pt-manager-textarea",
                rows: 2,
                value: objective,
                placeholder: "例如：打开京东，进入价格保护并一键价保",
                onChange: (event) => setObjective(event.target.value)
              })
            ),
            React.createElement(
              "div",
              { className: "pt-actions" },
              React.createElement(
                "button",
                {
                  type: "button",
                  className: "pt-btn pt-btn-ghost pt-btn-sm",
                  onClick: () => setAdding(false)
                },
                "取消"
              ),
              React.createElement(
                "button",
                {
                  type: "button",
                  className: "pt-btn pt-btn-primary pt-btn-sm",
                  onClick: save,
                  disabled: busy || !objective.trim()
                },
                busy ? "保存中…" : "保存模板"
              )
            )
              )
            : null,
          error ? React.createElement("p", { className: "pt-error", role: "alert" }, error) : null,
          notice ? React.createElement("p", { className: "pt-hint" }, notice) : null,
          hostChecked && hostHint
            ? React.createElement("p", { className: "pt-error", role: "alert" }, hostHint)
            : null,
          React.createElement(
            "div",
            { className: `pt-manager-list${adding ? "" : " is-first"}` },
            React.createElement(
              "div",
              { className: "pt-manager-list-head" },
              React.createElement(
                "h3",
                { className: "pt-manager-title" },
                `现有模板（${templates.length}）`
              ),
              !adding
                ? React.createElement(
                    "button",
                    { type: "button", className: "pt-link", onClick: () => setAdding(true) },
                    "新增模板"
                  )
                : null
            ),
            templates.length === 0
              ? React.createElement("p", { className: "pt-empty" }, "还没有模板")
              : templates.map((template, index) =>
                  React.createElement(
                    "div",
                    { className: "pt-manager-row", key: template.id || `${template.label}-${index}` },
                    React.createElement(
                      "div",
                      { className: "pt-manager-main" },
                      renamingId && renamingId === template.id
                        ? React.createElement(
                            "div",
                            { className: "pt-rename" },
                            React.createElement("input", {
                              className: "pt-input pt-input-sm",
                              value: renameValue,
                              autoFocus: true,
                              onChange: (event) => setRenameValue(event.target.value),
                              onKeyDown: (event) => {
                                if (event.key === "Enter") submitRename(template.id);
                                if (event.key === "Escape") setRenamingId("");
                              }
                            }),
                            React.createElement(
                              "button",
                              {
                                type: "button",
                                className: "pt-btn pt-btn-primary pt-btn-sm",
                                onClick: () => submitRename(template.id),
                                disabled: busy
                              },
                              "保存"
                            ),
                            React.createElement(
                              "button",
                              {
                                type: "button",
                                className: "pt-btn pt-btn-ghost pt-btn-sm",
                                onClick: () => setRenamingId("")
                              },
                              "取消"
                            )
                          )
                        : [
                            React.createElement(
                              "span",
                              { className: "pt-manager-name", key: "name" },
                              template.label || template.name
                            ),
                            template.file
                              ? React.createElement(
                                  "span",
                                  { className: "pt-hint", key: "file" },
                                  String(template.file).split("/").pop()
                                )
                              : null
                          ],
                      React.createElement(
                        "span",
                        { className: "pt-manager-objective" },
                        template.text || template.objective
                      )
                    ),
                    React.createElement(
                      "div",
                      { className: "pt-manager-actions" },
                      reviewLink(template),
                      template.id && !(renamingId === template.id)
                        ? React.createElement(
                            "button",
                            { type: "button", className: "pt-link", onClick: () => startRename(template) },
                            "重命名"
                          )
                        : null,
                      template.file && typeof props.onRun === "function"
                        ? React.createElement(
                            "button",
                            { type: "button", className: "pt-link", onClick: () => props.onRun(template.id) },
                            "执行模板"
                          )
                        : null,
                      template.file
                        ? React.createElement(
                            "button",
                            { type: "button", className: "pt-link", onClick: () => openEditor(template) },
                            "编辑模板"
                          )
                        : null,
                      confirmId === template.id
                        ? React.createElement(
                            React.Fragment,
                            null,
                            React.createElement(
                              "button",
                              {
                                type: "button",
                                className: "pt-link pt-link-danger",
                                onClick: () => remove(template.id),
                                disabled: busy
                              },
                              "确认删除"
                            ),
                            React.createElement(
                              "button",
                              { type: "button", className: "pt-link", onClick: () => setConfirmId("") },
                              "取消"
                            )
                          )
                        : template.id
                          ? React.createElement(
                              "button",
                              {
                                type: "button",
                                className: "pt-link",
                                onClick: () => setConfirmId(template.id)
                              },
                              "删除"
                            )
                          : null
                    )
                  )
                )
          )
        )
      );

      return React.createElement(
        React.Fragment,
        null,
        manager,
        // Mount only after the md arrived: the editor seeds its textarea from
        // `file.content` on mount, so mounting during `loading` would freeze it
        // empty when the content lands in a later render.
        editor && !editor.loading
          ? React.createElement(TemplateEditor, {
              key: editor.path || editor.id,
              file: editor,
              onClose: () => setEditor(null),
              onSave: saveEditor
            })
          : null,
        reviewId
          ? React.createElement(TemplateReviewPanel, {
              template: templates.find((template) => template.id === reviewId),
              review: (reviews[reviewId] && reviews[reviewId].data) || {},
              onClose: () => setReviewId(""),
              onApply: (uids, dryRun) => props.onApplyReview(reviewId, uids, dryRun),
              onRollback: () => props.onRollbackReview(reviewId),
              onApplied: () => loadReview(reviewId)
            })
          : null
      );
    }

    function ScheduleEditor(props) {
      const item = props.item;
      const [objective, setObjective] = React.useState(item.objective || "");
      const [time, setTime] = React.useState(timeInputValue(item.at));
      const [busy, setBusy] = React.useState(false);
      const [error, setError] = React.useState("");
      const at = atFromClock(time);

      async function save() {
        if (!objective.trim()) {
          setError("请填写任务内容");
          return;
        }
        if (!at) {
          setError("请选择有效时间");
          return;
        }
        setBusy(true);
        setError("");
        try {
          await props.onSave(item.id, { objective: objective.trim(), at });
          props.onClose();
        } catch (saveError) {
          setError(friendlyError(saveError));
        } finally {
          setBusy(false);
        }
      }

      return React.createElement(
        Modal,
        { title: "编辑排期", onClose: props.onClose },
        React.createElement(
          "div",
          { className: "pt-form" },
          React.createElement(
            "label",
            { className: "pt-field" },
            React.createElement("span", null, "任务"),
            React.createElement("input", {
              className: "pt-input",
              value: objective,
              onChange: (event) => setObjective(event.target.value)
            })
          ),
          React.createElement(
            "label",
            { className: "pt-field" },
            React.createElement("span", null, "执行时间"),
            React.createElement("input", {
              className: "pt-input pt-time",
              type: "time",
              value: time,
              onChange: (event) => setTime(event.target.value)
            })
          ),
          React.createElement(
            "p",
            { className: "pt-hint" },
            at ? `将安排在 ${formatTime(at)}` : "请选择时间"
          ),
          error ? React.createElement("p", { className: "pt-error", role: "alert" }, error) : null,
          React.createElement(
            "div",
            { className: "pt-actions pt-form-actions" },
            React.createElement(
              "button",
              { type: "button", className: "pt-btn pt-btn-ghost pt-btn-sm", onClick: props.onClose },
              "取消"
            ),
            React.createElement(
              "button",
              {
                type: "button",
                className: "pt-btn pt-btn-primary pt-btn-sm",
                onClick: save,
                disabled: busy || !at || !objective.trim()
              },
              busy ? "保存中…" : "保存排期"
            )
          )
        )
      );
    }

    function ImagePreview(props) {
      const [large, setLarge] = React.useState("");
      const [loading, setLoading] = React.useState(false);
      const [error, setError] = React.useState("");
      const load = () => {
        if (typeof props.loadLarge !== "function") return;
        setLoading(true);
        setError("");
        Promise.resolve(props.loadLarge())
          .then((value) => {
            if (value && value.base64) {
              setLarge(`data:${value.mime || "image/png"};base64,${value.base64}`);
            }
          })
          .catch((loadError) => setError(friendlyError(loadError)))
          .finally(() => setLoading(false));
      };
      React.useEffect(() => {
        load();
      }, []);
      const image = large || props.src;
      return React.createElement(
        Modal,
        { title: "手机画面", onClose: props.onClose, wide: true },
        React.createElement(
          "div",
          { className: "pt-preview" },
          image
            ? React.createElement("img", { src: image, alt: "手机当前画面" })
            : React.createElement(
                "div",
                { className: "pt-preview-empty" },
                loading ? React.createElement(Spinner) : React.createElement(Icon, { name: "phone", size: 28 })
              ),
          React.createElement(
            "div",
            { className: "pt-preview-foot" },
            React.createElement(
              "span",
              { className: "pt-hint" },
              loading ? "正在获取画面…" : error || "画面会随任务刷新"
            ),
            React.createElement(
              "button",
              {
                type: "button",
                className: "pt-btn pt-btn-ghost pt-btn-sm",
                onClick: load,
                disabled: loading
              },
              "刷新"
            )
          )
        )
      );
    }

    function Toast(props) {
      return React.createElement(
        "div",
        { className: `pt-toast${props.tone ? ` is-${props.tone}` : ""}`, role: "status", "aria-live": "polite" },
        props.message
      );
    }

    function PhoneTaskPage(props) {
      const snapshot = React.useSyncExternalStore(subscribe, getState, getState);
      const [text, setText] = React.useState("");
      const [starting, setStarting] = React.useState(false);
      const [startError, setStartError] = React.useState("");
      const [setupOpen, setSetupOpen] = React.useState(false);
      const [setupMode, setSetupMode] = React.useState("first");
      const [provisionUpdate, setProvisionUpdate] = React.useState(null);
      const [awaitingOnline, setAwaitingOnline] = React.useState(false);
      const [templateOpen, setTemplateOpen] = React.useState(false);
      const [historyOpen, setHistoryOpen] = React.useState(false);
      const [previewOpen, setPreviewOpen] = React.useState(false);
      const [editingSchedule, setEditingSchedule] = React.useState(null);
      const [toast, setToast] = React.useState(null);
      const active = snapshot.active;
      const running = Boolean(active && ["starting", "running"].includes(active.status));
      const provisioning = Boolean(
        provisionUpdate && (provisionUpdate.state === "running" || provisionUpdate.state === "waiting")
      );
      const deviceState = useDeviceStatus(props, snapshot.device || "default", running || provisioning);
      const thumbnail = useThumbnail(props, running, snapshot.device || "default");
      const result =
        active &&
        !running &&
        active.finishedAt &&
        Date.now() - Number(active.finishedAt) < MAX_RESULT_AGE
          ? active
          : null;
      const recent = mergeHistory(snapshot);
      const statusDevice = deviceState.status && deviceState.status.device;
      const captureStats = deviceState.status && deviceState.status.capture
        ? deviceState.status.capture.stats || null
        : null;
      const captureProbe = deviceState.status ? deviceState.status.captureProbe || null : null;
      const eyeReason = String(
        (captureProbe && captureProbe.error) || (captureStats && captureStats.error) || ""
      );
      // Prefer the real screenshot probe; the stream stats are stale as soon as
      // scrcpy dies while adb screencap still works.
      const eyeDown = Boolean(
        statusDevice &&
          statusDevice.reachable &&
          (captureProbe
            ? captureProbe.ok === false
            : captureStats && (captureStats.error || captureStats.running === false))
      );
      const [reconnectingAdb, setReconnectingAdb] = React.useState(false);
      const info = deviceSummary(deviceState.phase, deviceState.status, deviceState.error, provisionUpdate);
      // The wizard is a deliberate full-page mode now; an offline device keeps the
      // task page visible (templates/history still work) with execution disabled.
      const showWizard = setupOpen && !awaitingOnline;
      const wizardMode = setupOpen ? setupMode : "first";
      let blockedReason = "";
      if (deviceState.phase !== "ready") {
        blockedReason = "设备未连接 · 先配网或重新检测";
      } else if (statusDevice && statusDevice.usb === false) {
        blockedReason = "手机 USB 未识别 · 检查控制手与手机之间的连接";
      } else if (statusDevice && statusDevice.state === "FAULT") {
        blockedReason = "控制手处于异常状态 · 重新检测或重启设备";
      }

      const showToast = (message, tone) => setToast({ message, tone: tone || "ok", at: Date.now() });

      React.useEffect(() => {
        if (!toast) return undefined;
        const timer = setTimeout(() => setToast(null), 3500);
        return () => clearTimeout(timer);
      }, [toast]);

      React.useEffect(() => {
        if (!running) setPreviewOpen(false);
      }, [running]);

      async function runTask(value, options = {}) {
        if (starting || running) return;
        const objective = String(value || "").trim();
        if (!objective) return;
        setStarting(true);
        setStartError("");
        try {
          await props.startTask(objective);
          if (options.clear !== false) setText("");
        } catch (error) {
          setStartError(friendlyError(error));
          if (
            error &&
            ["DEVICE_NOT_READY", "SERVICE_DOWN", "PHONE_SERVICE_DOWN", "EYE_UNAVAILABLE"].includes(error.code)
          ) {
            await deviceState.refresh({ force: true });
          }
        } finally {
          setStarting(false);
        }
      }

      async function schedule(payload) {
        if (!text.trim()) throw new Error("请先输入任务");
        const entry = await props.scheduleTask(text.trim(), payload);
        if (entry && entry.at) showToast(`已排期 ${formatTime(entry.at)}`);
        return entry;
      }

      async function cancelSchedule(id) {
        try {
          await props.cancelSchedule(id);
          showToast("已取消排期");
        } catch (error) {
          showToast(friendlyError(error), "error");
        }
      }

      async function reconnectAdbFromPanel() {
        if (reconnectingAdb) return;
        setReconnectingAdb(true);
        try {
          const result = await props.reconnectAdb({});
          if (result && result.ok) {
            showToast(`ADB 已连接：${result.endpoint || "已恢复"}`);
          } else {
            showToast((result && result.hint) || "ADB 重连失败", "error");
          }
          await deviceState.refresh({ force: true });
        } catch (error) {
          showToast(friendlyError(error), "error");
        } finally {
          setReconnectingAdb(false);
        }
      }

      async function saveHistoryTemplate(item) {
        const objective = String((item && item.objective) || "").trim();
        if (!objective) {
          showToast("这条记录没有目标文本，无法生成模板", "error");
          return;
        }
        try {
          await saveResultTemplate(objective, (item && (item.hostTaskId || item.id)) || null);
        } catch (error) {
          showToast(friendlyError(error), "error");
        }
      }

      async function runTemplateFromPanel(templateId) {
        setTemplateOpen(false);
        try {
          await props.runTemplate(templateId);
          showToast("模板已开始执行");
        } catch (error) {
          showToast(friendlyError(error), "error");
        }
      }

      async function saveResultTemplate(objective, taskId) {
        if (taskId) showToast("正在总结执行过程，可能需要十几秒…");
        let saved = null;
        try {
          saved = await props.saveTemplate({
            name: objective.slice(0, 30),
            objective,
            ...(taskId ? { taskId } : {})
          });
        } catch (error) {
          showToast(`保存失败：${friendlyError(error)}`, "error");
          return;
        }
        if (!saved) {
          showToast("保存失败，请稍后再试", "error");
          return;
        }
        const file = saved.file ? String(saved.file).split("/").pop() : "";
        const warning = Array.isArray(saved.warnings) && saved.warnings.length ? saved.warnings[0] : "";
        if (file && saved.source === "summary") showToast(`已存为模板（已总结）：${file}`);
        else if (file) showToast(`已存为模板：${file}${warning ? `（${warning}）` : ""}`, warning ? "alert" : "ok");
        else if (warning) showToast(warning, "alert");
        else showToast("已存为模板");
      }

      async function saveManagedTemplate(payload) {
        const saved = await props.saveTemplate(payload);
        if (!saved) throw new Error("保存失败，请稍后再试");
        showToast("已保存模板");
        return saved;
      }

      async function renameManagedTemplate(id, nextName) {
        try {
          const saved = await props.renameTemplate(id, nextName);
          if (!saved) throw new Error("后台没有返回结果");
          showToast("已重命名");
          return saved;
        } catch (error) {
          throw new Error(`重命名失败：${friendlyError(error)}`);
        }
      }

      async function readManagedTemplateFile(id) {
        try {
          return await props.readTemplateFile(id);
        } catch (error) {
          throw new Error(`读不到模板 md：${friendlyError(error)}`);
        }
      }

      async function writeManagedTemplateFile(id, content) {
        try {
          const saved = await props.saveTemplateFile(id, content);
          if (!saved) throw new Error("后台没有返回结果");
          showToast("md 已保存");
          return saved;
        } catch (error) {
          throw new Error(`保存失败：${friendlyError(error)}`);
        }
      }

      async function deleteManagedTemplate(id) {
        const result = await props.deleteTemplate(id);
        if (!result) throw new Error("删除失败，请稍后再试");
        if (result.deleted === false) throw new Error("模板不存在或已删除，刷新后再试");
        showToast("已删除模板");
        return true;
      }

      async function deleteHistoryItem(item) {
        try {
          await props.deleteHistory(item);
          showToast("已删除记录");
        } catch (error) {
          showToast(friendlyError(error), "error");
          throw error;
        }
      }

      async function clearHistoryItems() {
        try {
          await props.clearHistory();
          showToast(
            active && ["starting", "running"].includes(active.status)
              ? "已清空记录（执行中的任务保留）"
              : "已清空任务记录"
          );
        } catch (error) {
          showToast(friendlyError(error), "error");
          throw error;
        }
      }

      async function updateScheduleHandler(id, payload) {
        await props.updateSchedule(id, payload);
        showToast("已更新排期");
      }

      function openSessionById(sessionId) {
        if (!sessionId) return;
        const opened = props.openTask(sessionId);
        if (opened === false) {
          showToast("没有打开会话，请在侧边栏选择任务会话", "error");
        }
      }

      async function replay(item) {
        try {
          await props.replayTask(item.id);
        } catch (error) {
          showToast(friendlyError(error), "error");
        }
      }

      async function readyFromWizard() {
        setSetupOpen(false);
        setProvisionUpdate({ state: "done" });
        setAwaitingOnline(true);
        try {
          await deviceState.refresh({ force: true });
        } finally {
          setAwaitingOnline(false);
          setProvisionUpdate(null);
        }
      }

      function closeWizard() {
        setSetupOpen(false);
        setProvisionUpdate(null);
        setAwaitingOnline(false);
        deviceState.refresh({ force: true });
      }

      function openSetup() {
        setSetupMode("reprovision");
        setSetupOpen(true);
        setProvisionUpdate(null);
        setAwaitingOnline(false);
      }

      let body;
      let notice = null;
      if (deviceState.phase === "booting") {
        body = React.createElement(LoadingCard);
      } else if (deviceState.phase === "service_down") {
        body = React.createElement(ServiceDownCard, {
          error: deviceState.error,
          refreshing: deviceState.refreshing,
          onRetry: () => deviceState.refresh({ force: true })
        });
      } else if (awaitingOnline && deviceState.phase !== "ready") {
        body = React.createElement(LoadingCard, { text: "设备已上线，正在刷新状态…" });
      } else if (showWizard && !running) {
        body = React.createElement(SetupWizard, {
          mode: wizardMode,
          currentSsid:
            (statusDevice && statusDevice.wifi && statusDevice.wifi.ssid) ||
            readLastSsid() ||
            "",
          deviceStatus: deviceState.status,
          provisionStatus: props.provisionStatus,
          provisionDevices: props.provisionDevices,
          startProvision: props.startProvision,
          armProvisioning: props.armProvisioning,
          refreshDevice: deviceState.refresh,
          onReady: readyFromWizard,
          onClose: setupOpen ? closeWizard : null,
          onProvisionUpdate: setProvisionUpdate,
          onToast: (message) => showToast(message),
          initialError: !setupOpen && deviceState.phase === "offline" ? startError : ""
        });
      } else if (running) {
        body = React.createElement(
          "div",
          { className: "pt-body" },
          React.createElement(RunningTask, {
            task: active,
            thumbnail,
            onPreview: () => setPreviewOpen(true),
            onOpen: openSessionById,
            onStop: props.stopTask
          }),
          React.createElement(
            "p",
            { className: "pt-hint", style: { marginTop: "10px" } },
            "任务结束后可以继续下发。需要人工确认时，执行会暂停并提示你。"
          ),
          React.createElement(ScheduledList, {
            items: snapshot.scheduled,
            onCancel: cancelSchedule,
            onEdit: (item) => setEditingSchedule(item)
          }),
          React.createElement(RecentTasks, {
            items: recent,
            emptyText: "还没有其他任务。",
            onShowAll: () => setHistoryOpen(true),
            onOpen: openSessionById,
            onReplay: replay,
            onDelete: deleteHistoryItem
          })
        );
      } else {
        if (deviceState.phase !== "ready") {
          notice = React.createElement(OfflineNotice, {
            onSetup: openSetup,
            onRetry: () => deviceState.refresh({ force: true }),
            refreshing: deviceState.refreshing
          });
        } else if (eyeDown) {
          notice = React.createElement(EyeDownNotice, {
            reason: eyeReason,
            reconnecting: reconnectingAdb,
            refreshing: deviceState.refreshing,
            onReconnect: reconnectAdbFromPanel,
            onRetry: () => deviceState.refresh({ force: true })
          });
        }
        body = React.createElement(
          "div",
          { className: "pt-body" },
          result
            ? React.createElement(ResultCard, {
                task: result,
                busy: starting,
                onOpen: openSessionById,
                onRetry: (objective) => runTask(objective, { clear: false }),
                onRetryTemplate: runTemplateFromPanel,
                onSaveTemplate: saveResultTemplate,
                onDismiss: props.dismissResult
              })
            : null,
          React.createElement(TaskComposer, {
            templates,
            text,
            busy: starting,
            error: startError,
            disabledReason: blockedReason,
            disabled: Boolean(blockedReason),
            onTextChange: (value) => {
              setText(value);
              if (startError) setStartError("");
            },
            onClearError: () => setStartError(""),
            onStart: () => runTask(text),
            onSchedule: schedule,
            onManageTemplates: () => setTemplateOpen(true)
          }),
          React.createElement(ScheduledList, {
            items: snapshot.scheduled,
            onCancel: cancelSchedule,
            onEdit: (item) => setEditingSchedule(item)
          }),
          React.createElement(RecentTasks, {
            items: recent,
            onShowAll: () => setHistoryOpen(true),
            onOpen: openSessionById,
            onReplay: replay,
            onDelete: deleteHistoryItem,
            onSaveTemplate: saveHistoryTemplate
          })
        );
      }

      return React.createElement(
        "div",
        { className: "pt-page" },
        React.createElement(
          "div",
          { className: "pt-inner" },
          React.createElement(PageHeader),
          React.createElement(StatusBar, {
            info,
            phase: deviceState.phase,
            devices: snapshot.devices || [],
            device: snapshot.device || "default",
            onDeviceChange: props.setDevice,
            onRefresh: () => deviceState.refresh({ force: true }),
            refreshing: deviceState.refreshing,
            eye: captureStats ? { state: eyeDown ? "down" : "ok", reason: eyeReason } : null,
            onOpenSetup:
              !running && !showWizard && !provisioning ? openSetup : null,
            running,
            provisioning
          }),
          notice,
          body,
          toast ? React.createElement(Toast, { message: toast.message, tone: toast.tone }) : null
        ),
        templateOpen
          ? React.createElement(TemplateManager, {
              templates,
              currentText: text,
              onClose: () => setTemplateOpen(false),
              onSave: saveManagedTemplate,
              onDelete: deleteManagedTemplate,
              onRename: renameManagedTemplate,
              onReadFile: readManagedTemplateFile,
              onWriteFile: writeManagedTemplateFile,
              onHostInfo: props.hostInfo,
              onRun: runTemplateFromPanel,
              onReview: props.reviewTemplate,
              onApplyReview: props.applyTemplateReview,
              onRollbackReview: props.rollbackTemplate
            })
          : null,
        historyOpen
          ? React.createElement(HistoryPanel, {
              items: recent,
              onClose: () => setHistoryOpen(false),
              onOpen: openSessionById,
              onReplay: replay,
              onDelete: deleteHistoryItem,
              onClear: clearHistoryItems
            })
          : null,
        editingSchedule
          ? React.createElement(ScheduleEditor, {
              item: editingSchedule,
              onClose: () => setEditingSchedule(null),
              onSave: updateScheduleHandler
            })
          : null,
        previewOpen
          ? React.createElement(ImagePreview, {
              src: thumbnail.src,
              onClose: () => setPreviewOpen(false),
              loadLarge: () =>
                typeof props.thumbnail === "function"
                  ? props.thumbnail({ maxWidth: 480, device: snapshot.device || "default" })
                  : null
            })
          : null
      );
    }
    const CSS = `
.pt-page {
  --pt-bg: var(--dsw-alias-bg-base, #fff);
  --pt-text: var(--dsw-alias-label-primary, #101828);
  --pt-text-2: var(--dsw-alias-label-secondary, #475467);
  --pt-muted: var(--dsw-alias-label-tertiary, #667085);
  --pt-caption: var(--dsw-alias-label-caption, #98a2b3);
  --pt-line: var(--dsw-alias-border-l1, #e4e7ec);
  --pt-line-2: var(--dsw-alias-border-l2, #d0d5dd);
  --pt-soft: var(--dsw-alias-bg-secondary, #f8f9fb);
  --pt-blue: var(--dsw-alias-state-business-primary, #2563eb);
  --pt-blue-ring: rgba(37, 99, 235, .12);
  --pt-green: #12b76a;
  --pt-green-text: #027a48;
  --pt-green-soft: #ecfdf3;
  --pt-amber: #f79009;
  --pt-amber-text: #b54708;
  --pt-amber-soft: #fffaeb;
  --pt-red: #f04438;
  --pt-red-text: #b42318;
  --pt-red-soft: #fef3f2;
  box-sizing: border-box;
  height: 100%;
  overflow: auto;
  padding: 28px 24px 56px;
  background: var(--pt-bg);
  color: var(--pt-text);
  font-size: 13px;
  line-height: 1.5;
  -webkit-font-smoothing: antialiased;
}
.pt-page * { box-sizing: border-box; }
.pt-inner { max-width: 820px; margin: 0 auto; }
.pt-page-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; margin-bottom: 20px; }
.pt-page-title { margin: 0; font-size: 20px; line-height: 28px; font-weight: 600; letter-spacing: -.01em; }
.pt-page-sub { margin: 3px 0 0; font-size: 12px; color: var(--pt-muted); }
.pt-icon { display: block; flex: none; }
.pt-panel-icon { display: inline-flex; align-items: center; justify-content: center; color: inherit; line-height: 1; }

.pt-btn {
  height: 32px;
  padding: 0 12px;
  border: 1px solid transparent;
  border-radius: 8px;
  background: transparent;
  color: var(--pt-text-2);
  font: inherit;
  font-size: 12.5px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
  cursor: pointer;
  white-space: nowrap;
}
.pt-btn:hover { text-decoration: none; }
.pt-btn:disabled { opacity: .45; cursor: not-allowed; }
.pt-btn-sm { height: 28px; padding: 0 10px; font-size: 12px; }
.pt-btn-ghost { border-color: var(--pt-line); background: var(--pt-bg); }
.pt-btn-ghost:hover { background: var(--pt-soft); }
.pt-btn-primary { background: var(--pt-blue); color: #fff; }
.pt-btn-primary:hover { filter: brightness(.94); }
.pt-btn-danger { border-color: #fda29b; background: var(--pt-bg); color: var(--pt-red-text); }
.pt-link {
  border: 0;
  background: transparent;
  color: var(--pt-text-2);
  font: inherit;
  font-size: 12px;
  padding: 0;
  cursor: pointer;
  white-space: nowrap;
}
.pt-link:hover { color: var(--pt-text); text-decoration: underline; text-underline-offset: 2px; }
.pt-link:disabled { opacity: .5; cursor: not-allowed; text-decoration: none; }
.pt-icon-btn {
  width: 24px;
  height: 24px;
  padding: 0;
  border: 0;
  border-radius: 6px;
  background: transparent;
  color: var(--pt-muted);
  display: inline-flex;
  align-items: center;
  justify-content: center;
  cursor: pointer;
}
.pt-icon-btn:hover { background: var(--pt-soft); color: var(--pt-text-2); }
.pt-page button:focus-visible,
.pt-page input:focus-visible,
.pt-page textarea:focus-visible,
.pt-page select:focus-visible {
  outline: 2px solid var(--pt-blue);
  outline-offset: 2px;
}

.pt-status {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  margin-bottom: 16px;
  padding: 9px 12px;
  border-radius: 10px;
  background: var(--pt-soft);
  color: var(--pt-text-2);
  font-size: 12px;
}
.pt-dot { width: 6px; height: 6px; border-radius: 50%; background: var(--pt-muted); flex: none; }
.pt-dot.is-ok { background: var(--pt-green); box-shadow: 0 0 0 3px rgba(18, 183, 106, .12); }
.pt-status.is-ok .pt-dot { background: var(--pt-green); box-shadow: 0 0 0 3px rgba(18, 183, 106, .12); }
.pt-status.is-warn .pt-dot { background: var(--pt-amber); box-shadow: 0 0 0 3px rgba(247, 144, 9, .14); }
.pt-status.is-error .pt-dot { background: var(--pt-red); box-shadow: 0 0 0 3px rgba(240, 68, 56, .12); }
.pt-status.is-muted .pt-dot { background: var(--pt-caption); }
.pt-status-title { color: var(--pt-text); font-weight: 600; }
.pt-status-meta { color: var(--pt-text-2); }
.pt-status-sep { width: 1px; height: 12px; background: var(--pt-line-2); opacity: .6; }
.pt-status-spacer { flex: 1; }
.pt-device-select {
  height: 26px;
  max-width: 150px;
  border: 1px solid var(--pt-line);
  border-radius: 6px;
  background: transparent;
  color: var(--pt-text-2);
  font: inherit;
  font-size: 12px;
}

.pt-card {
  margin-top: 16px;
  padding: 18px;
  border: 1px solid var(--pt-line);
  border-radius: 12px;
  background: var(--pt-bg);
}
.pt-card-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; }
.pt-card-title { margin: 0; font-size: 14px; line-height: 22px; font-weight: 600; }
.pt-card-sub { margin: 4px 0 0; font-size: 12px; color: var(--pt-muted); }
.pt-card-foot {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  margin-top: 14px;
  padding-top: 12px;
  border-top: 1px solid var(--pt-line);
  font-size: 12px;
  color: var(--pt-text-2);
}
.pt-spacer { flex: 1; }
.pt-actions { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.pt-hint { margin: 0; color: var(--pt-muted); font-size: 12px; }
.pt-error { margin: 10px 0 0; color: var(--pt-red-text); font-size: 12px; }
.pt-warn { margin: 10px 0 0; color: var(--pt-amber-text); font-size: 12px; }
.pt-note { margin: 14px 0 0; font-size: 12px; color: var(--pt-text-2); }
.pt-empty { margin: 10px 0 0; font-size: 12px; color: var(--pt-muted); }
.pt-command {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: 12px;
  padding: 8px 10px;
  border-radius: 8px;
  background: var(--pt-soft);
}
.pt-command code {
  flex: 1;
  min-width: 0;
  overflow: auto;
  white-space: nowrap;
  font: 12px/1.5 ui-monospace, SFMono-Regular, Menlo, monospace;
  color: var(--pt-text-2);
}
.pt-loading { display: flex; align-items: center; gap: 10px; font-size: 13px; color: var(--pt-muted); }
.pt-spinner {
  width: 16px;
  height: 16px;
  border-radius: 50%;
  border: 2px solid var(--pt-line);
  border-top-color: var(--pt-blue);
  flex: none;
  animation: pt-spin .9s linear infinite;
}
.pt-spinner-sm { width: 13px; height: 13px; border-width: 1.5px; margin-top: 1px; }
@keyframes pt-spin { to { transform: rotate(360deg); } }

.pt-progress-head { display: flex; align-items: flex-start; gap: 12px; }
.pt-progress-main { min-width: 0; }
.pt-state-icon {
  width: 18px;
  height: 18px;
  border-radius: 50%;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  flex: none;
}
.pt-state-icon.is-ok { background: var(--pt-green-soft); color: var(--pt-green-text); }
.pt-state-icon.is-error { background: var(--pt-red-soft); color: var(--pt-red-text); }
.pt-state-icon.is-stopped,
.pt-state-icon.is-neutral { background: var(--pt-soft); color: var(--pt-muted); }
.pt-details { margin-top: 12px; }
.pt-output {
  margin: 8px 0 0;
  padding: 8px;
  max-height: 140px;
  overflow: auto;
  border-radius: 8px;
  background: var(--pt-soft);
  color: var(--pt-red-text);
  font: 12px/1.45 ui-monospace, SFMono-Regular, Menlo, monospace;
  white-space: pre-wrap;
  word-break: break-word;
}

.pt-steps { list-style: none; margin: 8px 0 0; padding: 0; }
.pt-step {
  display: grid;
  grid-template-columns: 22px 1fr;
  gap: 12px;
  padding: 16px 0;
  border-top: 1px solid var(--pt-line);
}
.pt-step:first-child { border-top: 0; }
.pt-step-num {
  width: 22px;
  height: 22px;
  border-radius: 50%;
  border: 1px solid var(--pt-line-2);
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 12px;
  font-weight: 600;
  color: var(--pt-muted);
  margin-top: 1px;
}
.pt-step-num.is-done { background: var(--pt-green-soft); border-color: #abefc6; color: var(--pt-green-text); }
.pt-step-main { min-width: 0; }
.pt-step-head { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.pt-step-head h3 { margin: 0; font-size: 13px; font-weight: 600; line-height: 20px; }
.pt-tag { font-size: 12px; line-height: 18px; padding: 0 6px; border-radius: 4px; background: var(--pt-soft); color: var(--pt-muted); }
.pt-scan-row { display: flex; align-items: center; gap: 8px; margin-top: 10px; font-size: 12px; color: var(--pt-muted); }
.pt-device-list { display: flex; flex-direction: column; gap: 8px; margin-top: 10px; }
.pt-device-option {
  display: flex;
  align-items: center;
  gap: 10px;
  width: 100%;
  padding: 10px 12px;
  border: 1px solid var(--pt-line-2);
  border-radius: 8px;
  background: var(--pt-bg);
  color: var(--pt-text);
  font: inherit;
  font-size: 13px;
  text-align: left;
  cursor: pointer;
}
.pt-device-option.is-selected { border-color: var(--pt-blue); box-shadow: 0 0 0 3px var(--pt-blue-ring); }
.pt-device-option:disabled { cursor: default; opacity: .7; }
.pt-radio { width: 14px; height: 14px; border-radius: 50%; border: 1px solid var(--pt-line-2); flex: none; }
.pt-device-option.is-selected .pt-radio { border: 4px solid var(--pt-blue); }
.pt-device-name { font-weight: 500; }
.pt-device-meta { margin-left: auto; color: var(--pt-muted); font-size: 12px; }
.pt-empty-inline { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; margin-top: 10px; }
.pt-row-hint { display: flex; align-items: center; gap: 10px; margin-top: 8px; }
.pt-fields { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin-top: 12px; }
.pt-field { display: flex; flex-direction: column; gap: 6px; min-width: 0; }
.pt-field > span:first-child { font-size: 12px; color: var(--pt-text-2); }
.pt-input-wrap { position: relative; display: block; }
.pt-input, .pt-textarea {
  width: 100%;
  border: 1px solid var(--pt-line-2);
  border-radius: 8px;
  background: var(--pt-bg);
  color: var(--pt-text);
  font: inherit;
  font-size: 13px;
  outline: none;
}
.pt-input { height: 34px; padding: 0 10px; }
.pt-input-sm { height: 28px; padding: 0 8px; font-size: 12px; width: 64px; }
.pt-input:focus, .pt-textarea:focus { border-color: var(--pt-blue); box-shadow: 0 0 0 3px var(--pt-blue-ring); }
.pt-input:disabled, .pt-textarea:disabled { background: var(--pt-soft); color: var(--pt-muted); }
.pt-input-wrap .pt-input { padding-right: 52px; }
.pt-reveal {
  position: absolute;
  right: 4px;
  top: 4px;
  height: 26px;
  padding: 0 8px;
  border: 0;
  border-radius: 6px;
  background: transparent;
  color: var(--pt-muted);
  font: inherit;
  font-size: 12px;
  cursor: pointer;
}
.pt-reveal:hover { background: var(--pt-soft); color: var(--pt-text-2); }
.pt-step-actions { display: flex; align-items: center; gap: 12px; margin-top: 14px; flex-wrap: wrap; }

.pt-composer-head { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.pt-chips { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 10px; }
.pt-chip {
  height: 26px;
  padding: 0 9px;
  border: 1px solid var(--pt-line);
  border-radius: 6px;
  background: var(--pt-bg);
  color: var(--pt-text-2);
  font: inherit;
  font-size: 12px;
  cursor: pointer;
  max-width: 100%;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.pt-chip:hover { border-color: var(--pt-line-2); background: var(--pt-soft); }
.pt-chip:disabled { opacity: .5; cursor: default; }
.pt-textarea {
  display: block;
  min-height: 84px;
  resize: vertical;
  margin-top: 10px;
  padding: 10px 12px;
  line-height: 1.6;
  font-size: 13.5px;
}
.pt-textarea::placeholder { color: var(--pt-caption); }
.pt-composer-foot { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-top: 10px; }
.pt-popover-wrap { position: relative; }
.pt-popover {
  position: absolute;
  right: 0;
  top: calc(100% + 8px);
  width: 250px;
  padding: 12px;
  border: 1px solid var(--pt-line);
  border-radius: 10px;
  background: var(--pt-bg);
  box-shadow: 0 8px 24px rgba(16, 24, 40, .1);
  z-index: 20;
}
.pt-popover-title { margin: 0 0 8px; font-size: 12px; font-weight: 600; color: var(--pt-text); }
.pt-presets { display: grid; grid-template-columns: 1fr 1fr; gap: 6px; margin-bottom: 8px; }
.pt-preset {
  height: 28px;
  border: 1px solid var(--pt-line);
  border-radius: 6px;
  background: var(--pt-bg);
  color: var(--pt-text-2);
  font: inherit;
  font-size: 12px;
  cursor: pointer;
}
.pt-preset:hover { background: var(--pt-soft); }
.pt-preset:disabled { opacity: .5; cursor: default; }
.pt-custom { display: flex; align-items: center; gap: 6px; margin-bottom: 8px; }

.pt-running {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 20px;
  padding: 15px 16px;
  border-color: #cfdcfb;
}
.pt-running-main { min-width: 0; }
.pt-running-state { display: flex; align-items: center; gap: 7px; font-size: 12px; font-weight: 600; color: var(--pt-blue); }
.pt-running-state .pt-dot { background: var(--pt-blue); box-shadow: 0 0 0 3px var(--pt-blue-ring); }
.pt-running-title {
  margin: 5px 0 0;
  font-size: 14px;
  line-height: 22px;
  font-weight: 600;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.pt-running-meta {
  margin: 3px 0 0;
  font-size: 12px;
  color: var(--pt-muted);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.pt-running .pt-actions { flex: none; }

.pt-result { display: flex; align-items: flex-start; justify-content: space-between; gap: 20px; }
.pt-result-main { min-width: 0; flex: 1; }
.pt-result-title { display: flex; align-items: center; gap: 8px; font-size: 12px; font-weight: 600; color: var(--pt-text-2); flex-wrap: wrap; }
.pt-result-meta { font-weight: 400; color: var(--pt-muted); }
.pt-result-reason { margin: 6px 0 0; font-size: 12px; color: var(--pt-red-text); }
.pt-result .pt-actions { flex: none; margin-top: 1px; }

.pt-section { margin-top: 24px; }
.pt-section-head { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.pt-section-title { margin: 0; font-size: 13px; font-weight: 600; }
.pt-task-list { list-style: none; margin: 6px 0 0; padding: 0; }
.pt-task-row {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 10px 0;
  border-top: 1px solid var(--pt-line);
  font-size: 13px;
}
.pt-task-row:first-child { border-top: 0; }
.pt-task-body { flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 2px; }
.pt-task-title { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.pt-task-sub { font-size: 12px; color: var(--pt-red-text); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.pt-task-sub.is-muted { color: var(--pt-muted); }
.pt-task-meta { font-size: 12px; color: var(--pt-muted); white-space: nowrap; }
.pt-task-actions { display: inline-flex; align-items: center; gap: 10px; flex: none; }
.pt-task-row .pt-link { flex: none; }

.pt-toast {
  position: fixed;
  left: 50%;
  bottom: 28px;
  transform: translateX(-50%);
  z-index: 90;
  padding: 8px 14px;
  border-radius: 8px;
  background: #101828;
  color: #fff;
  font-size: 12px;
  box-shadow: 0 8px 24px rgba(16, 24, 40, .18);
}
.pt-toast.is-error { background: var(--pt-red-text); }

.pt-thumb {
  width: 46px;
  height: 82px;
  flex: none;
  padding: 0;
  border: 1px solid var(--pt-line);
  border-radius: 8px;
  background: var(--pt-soft);
  overflow: hidden;
  display: flex;
  align-items: center;
  justify-content: center;
  cursor: pointer;
}
.pt-thumb:hover { border-color: var(--pt-line-2); }
.pt-thumb img { width: 100%; height: 100%; object-fit: cover; display: block; }
.pt-thumb-empty { display: flex; color: var(--pt-caption); }
.pt-running-main { flex: 1; }

.pt-modal-backdrop {
  position: fixed;
  inset: 0;
  z-index: 80;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 24px;
  background: rgba(16, 24, 40, .35);
}
.pt-modal {
  width: min(520px, 100%);
  max-height: 80vh;
  display: flex;
  flex-direction: column;
  border: 1px solid var(--pt-line);
  border-radius: 12px;
  background: var(--pt-bg);
  box-shadow: 0 16px 48px rgba(16, 24, 40, .2);
}
.pt-modal.is-wide { width: min(680px, 100%); }
.pt-modal-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 14px 16px;
  border-bottom: 1px solid var(--pt-line);
}
.pt-modal-title { margin: 0; font-size: 14px; font-weight: 600; }
.pt-modal-body { padding: 16px; overflow: auto; }

.pt-manager-add { display: flex; flex-direction: column; gap: 10px; }
.pt-manager-list-head { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.pt-manager-title { margin: 0 0 4px; font-size: 13px; font-weight: 600; }
.pt-manager-textarea { min-height: 64px; margin-top: 0; }
.pt-manager-list {
  margin-top: 18px;
  padding-top: 14px;
  border-top: 1px solid var(--pt-line);
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.pt-manager-list.is-first { margin-top: 0; padding-top: 0; border-top: 0; }
.pt-manager-row {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 8px 0;
  border-top: 1px solid var(--pt-line);
}
.pt-manager-main { flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 2px; }
.pt-manager-name { font-size: 13px; font-weight: 500; }
.pt-manager-objective {
  font-size: 12px;
  color: var(--pt-muted);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.pt-link-danger { color: var(--pt-red-text); }
.pt-manager-actions { display: flex; align-items: center; gap: 10px; flex-shrink: 0; }
.pt-review-list { display: flex; flex-direction: column; gap: 6px; }
.pt-review-row { display: flex; align-items: flex-start; gap: 8px; padding: 6px 0; border-top: 1px solid var(--pt-line); }
.pt-review-row input { margin-top: 3px; }
.pt-review-main { display: flex; flex-direction: column; gap: 2px; min-width: 0; }
.pt-review-title { font-size: 13px; font-weight: 500; }
.pt-review-reason { font-size: 12px; color: var(--pt-muted); }
.pt-diff {
  max-height: 260px;
  overflow: auto;
  margin: 8px 0 0;
  padding: 8px 10px;
  border: 1px solid var(--pt-line);
  border-radius: 6px;
  background: var(--pt-bg-soft, rgba(127, 127, 127, 0.06));
  font: 11px/1.5 ui-monospace, SFMono-Regular, Menlo, monospace;
  white-space: pre;
}
.pt-rename { display: flex; align-items: center; gap: 6px; }
.pt-rename .pt-input { flex: 1; min-width: 120px; width: auto; }

.pt-form { display: flex; flex-direction: column; gap: 12px; }
.pt-form-actions { justify-content: flex-end; margin-top: 4px; }
.pt-input.pt-time { display: block; width: 140px; }
.pt-input-sm.pt-time { width: 100px; }

.pt-preview { display: flex; flex-direction: column; align-items: center; gap: 12px; }
.pt-preview img {
  max-width: 100%;
  max-height: 62vh;
  object-fit: contain;
  border: 1px solid var(--pt-line);
  border-radius: 8px;
}
.pt-preview-empty {
  width: 240px;
  height: 420px;
  max-height: 50vh;
  display: flex;
  align-items: center;
  justify-content: center;
  border: 1px dashed var(--pt-line-2);
  border-radius: 10px;
  background: var(--pt-soft);
  color: var(--pt-caption);
}
.pt-preview-foot { display: flex; align-items: center; justify-content: space-between; gap: 12px; width: 100%; }
.pt-popover-divider { height: 1px; background: var(--pt-line); margin: 8px 0; }
.pt-task-list-modal .pt-task-row { padding: 12px 0; }
.pt-history-head { margin: 0 0 6px; flex-wrap: wrap; gap: 8px; }

@media (max-width: 640px) {
  .pt-modal-backdrop { padding: 12px; }
  .pt-modal { max-height: 88vh; }
}

@media (max-width: 640px) {
  .pt-page { padding: 20px 16px 40px; }
  .pt-fields { grid-template-columns: 1fr; }
  .pt-composer-foot { flex-direction: column; align-items: stretch; }
  .pt-composer-foot .pt-actions { justify-content: flex-end; }
  .pt-running, .pt-result { flex-direction: column; align-items: stretch; gap: 12px; }
  .pt-running .pt-actions, .pt-result .pt-actions { justify-content: flex-end; }
  .pt-status .pt-status-meta { max-width: 100%; }
  .pt-task-row { flex-wrap: wrap; }
  .pt-task-row .pt-task-actions { margin-left: auto; flex-wrap: wrap; justify-content: flex-end; }
  .pt-task-row .pt-task-actions .pt-link { margin-left: 0; }
}

@media (prefers-reduced-motion: reduce) {
  .pt-spinner { animation: none; }
  .pt-page * { transition: none !important; }
}
`;

    function injectCss() {
      try {
        if (typeof document === "undefined") return;
        const tagId = "dsh-plugin-phone/task-dock.css";
        const existing = document.querySelector(`style[data-plugin-css="${tagId}"]`);
        if (existing) {
          // The plugin can be hot-reloaded without a full page refresh; update
          // the stylesheet instead of keeping the previous version.
          existing.textContent = CSS;
          return;
        }
        const tag = document.createElement("style");
        tag.dataset.plugin = "dsh-plugin-phone";
        tag.dataset.pluginCss = tagId;
        tag.textContent = CSS;
        document.head.appendChild(tag);
      } catch (_) {}
    }

    async function saveTemplate(ctx, input) {
      const payload = typeof input === "string" ? { name: input.slice(0, 30), objective: input } : (input || {});
      const objective = String(payload.objective || "").trim();
      const name = String(payload.name || objective.slice(0, 30)).trim();
      if (!name || !objective) return null;
      // Do not swallow host errors: "保存失败，请稍后再试" hid the real reason
      // (summary failed, trace missing, stale host…). The caller shows it.
      const saved = await callHostTask(
        ctx,
        "phone.task.saveTemplate",
        {
          name: name.slice(0, 80),
          objective,
          variables: Array.isArray(payload.variables) ? payload.variables : [],
          // 带上任务 id，Host 才能把这次执行的轨迹编译成 templates/<id>.md
          ...(payload.taskId ? { taskId: String(payload.taskId) } : {}),
          ...(payload.mechanical ? { mechanical: true } : {})
        },
        // Saving with a taskId runs the summariser (one model call) first.
        { timeoutMs: 180000 }
      );
      await syncHost(ctx);
      return saved;
    }

    async function deleteTemplate(ctx, id) {
      const key = id === undefined || id === null ? "" : String(id).trim();
      // Without an id the host cannot match anything; never send "undefined".
      if (!key || key === "undefined" || key === "null") return { deleted: false, reason: "missing_id" };
      try {
        const result = await callHostTask(ctx, "phone.task.deleteTemplate", { id: key });
        await syncHost(ctx);
        return result && typeof result === "object" ? result : { deleted: true };
      } catch (_) {
        return false;
      }
    }

    async function renameTemplate(ctx, id, name) {
      const key = id === undefined || id === null ? "" : String(id).trim();
      const next = String(name ?? "").trim();
      if (!key || !next) throw Object.assign(new Error("模板 id 或名称为空"), { code: "BAD_REQUEST" });
      // Errors propagate: the manager shows the real reason (stale host, bad id…).
      const saved = await callHostTask(ctx, "phone.task.renameTemplate", { id: key, name: next });
      await syncHost(ctx);
      return saved;
    }

    async function readTemplateFile(ctx, id) {
      const key = id === undefined || id === null ? "" : String(id).trim();
      if (!key) throw Object.assign(new Error("模板 id 为空"), { code: "BAD_REQUEST" });
      return callHostTask(ctx, "phone.task.templateFile", { id: key }, { timeoutMs: 8000 });
    }

    async function saveTemplateFile(ctx, id, content) {
      const key = id === undefined || id === null ? "" : String(id).trim();
      if (!key) throw Object.assign(new Error("模板 id 为空"), { code: "BAD_REQUEST" });
      const saved = await callHostTask(
        ctx,
        "phone.task.saveTemplateFile",
        { id: key, content: String(content ?? "") },
        { timeoutMs: 15000 }
      );
      await syncHost(ctx);
      return saved;
    }

    async function reviewTemplate(ctx, id) {
      const key = id === undefined || id === null ? "" : String(id).trim();
      if (!key) throw Object.assign(new Error("模板 id 为空"), { code: "BAD_REQUEST" });
      return callHostTask(ctx, "phone.task.templateReview", { id: key }, { timeoutMs: 60000 });
    }

    async function applyTemplateReview(ctx, id, uids, dryRun) {
      const key = id === undefined || id === null ? "" : String(id).trim();
      if (!key) throw Object.assign(new Error("模板 id 为空"), { code: "BAD_REQUEST" });
      const saved = await callHostTask(
        ctx,
        "phone.task.applyTemplateReview",
        { id: key, uids: Array.isArray(uids) ? uids : [], dryRun: dryRun === true },
        { timeoutMs: 120000 }
      );
      if (dryRun !== true) await syncHost(ctx);
      return saved;
    }

    async function rollbackTemplate(ctx, id) {
      const key = id === undefined || id === null ? "" : String(id).trim();
      if (!key) throw Object.assign(new Error("模板 id 为空"), { code: "BAD_REQUEST" });
      const saved = await callHostTask(ctx, "phone.task.rollbackTemplate", { id: key }, { timeoutMs: 60000 });
      await syncHost(ctx);
      return saved;
    }

    async function hostInfo(ctx) {
      try {
        return await callHostTask(ctx, "phone.task.info", {}, { timeoutMs: 5000 });
      } catch (_) {
        return null;
      }
    }

    async function replayTask(ctx, historyId) {
      const task = await callHostTask(ctx, "phone.task.replay", { id: historyId });
      if (task && task.sessionId) openTask(ctx, task.sessionId);
      return task;
    }

    function sameHistoryRecord(a, b) {
      if (!a || !b) return false;
      if (a.id && b.id && String(a.id) === String(b.id)) return true;
      // A local optimistic record carries the host task id/ session of the same
      // task; deleting either row must delete both.
      if (a.hostTaskId && b.id && String(a.hostTaskId) === String(b.id)) return true;
      if (b.hostTaskId && a.id && String(b.hostTaskId) === String(a.id)) return true;
      return Boolean(a.sessionId && b.sessionId && String(a.sessionId) === String(b.sessionId));
    }

    async function refreshHostHistory(ctx) {
      const list = await callHostTask(ctx, "phone.task.history", {});
      if (!Array.isArray(list)) return state.hostHistory;
      const hostHistory = list.slice(0, MAX_HISTORY);
      setState({ hostHistory });
      return hostHistory;
    }

    async function deleteHistoryRecord(ctx, item) {
      if (!item || !item.id) throw new Error("记录不存在");
      if (
        state.active &&
        ["starting", "running"].includes(state.active.status) &&
        sameHistoryRecord(item, state.active)
      ) {
        throw new Error("任务还在执行，结束后再删除");
      }
      // The page-started task only has a local optimistic record until the next
      // page load syncs host history. Fetch it first so deleting the visible row
      // also deletes the host record instead of letting it reappear after reload.
      const knownHost =
        item.source === "host" ||
        Boolean(item.hostTaskId) ||
        state.hostHistory.some((entry) => sameHistoryRecord(item, entry));
      let hostHistory = state.hostHistory;
      try {
        hostHistory = await refreshHostHistory(ctx);
      } catch (error) {
        // Never report success for a host-backed row we cannot confirm deleted.
        if (knownHost) throw error;
      }
      const hostTargets = hostHistory.filter((entry) => sameHistoryRecord(item, entry));
      const localTargets = state.history.filter((entry) => sameHistoryRecord(item, entry));
      if (hostTargets.length === 0 && localTargets.length === 0) return true;
      for (const target of hostTargets) {
        await callHostTask(ctx, "phone.task.deleteHistory", { id: String(target.id) });
      }
      const ids = new Set([...hostTargets, ...localTargets].map((entry) => String(entry.id)));
      setState({
        hostHistory: hostHistory.filter((entry) => !ids.has(String(entry.id))),
        history: state.history.filter((entry) => !ids.has(String(entry.id)))
      });
      return true;
    }

    async function clearHistoryRecords(ctx) {
      // Never report success while host records cannot be confirmed: otherwise
      // the cleared rows come back after the next page load.
      const hostHistory = await refreshHostHistory(ctx);
      const activeId = state.active && state.active.id;
      try {
        await callHostTask(ctx, "phone.task.clearHistory", {});
      } catch (error) {
        if (!(error.code === "NOT_FOUND" && hostHistory.length === 0)) throw error;
      }
      setState({
        hostHistory: [],
        // Keep the in-flight task's optimistic record; it is not finished yet.
        history: activeId
          ? state.history.filter((entry) => String(entry.id) === String(activeId))
          : []
      });
      return true;
    }

    async function syncHost(ctx) {
      try {
        const list = await callHostTask(ctx, "phone.task.templates", {});
        if (Array.isArray(list)) {
          templates = list.map((item) => ({
            id: item.id,
            label: item.name,
            text: item.objective,
            variables: item.variables || [],
            ...(item.file ? { file: item.file } : {})
          }));
          setState({ templateTick: Date.now() });
        }
      } catch (_) {}
      const revision = scheduleRevision;
      try {
        const list = await callHostTask(ctx, "phone.task.schedules", {});
        if (Array.isArray(list) && scheduleRevision === revision) setState({ scheduled: list });
      } catch (_) {}
      try {
        const list = await callHostTask(ctx, "phone.task.history", {});
        if (Array.isArray(list)) setState({ hostHistory: list.slice(0, MAX_HISTORY) });
      } catch (_) {}
      try {
        const list = await callHostTask(ctx, "phone.task.devices", {});
        if (Array.isArray(list) && list.length > 0) setState({ devices: list });
      } catch (_) {}
    }

    function apply(ctx) {
      injectCss();
      restoreSchedules(ctx);
      if (
        state.active &&
        state.active.sessionId &&
        ["starting", "running"].includes(state.active.status)
      ) {
        setTimeout(() => {
          try {
            watchTask(ctx, state.active.id, state.active.sessionId);
          } catch (_) {}
        }, 1000);
      }
      const taskCallbacks = {
        provisionDevices: () => callHostTask(ctx, "phone.provision.devices", {}),
        startProvision: (payload) => callHostTask(ctx, "phone.provision.start", payload || {}),
        provisionStatus: () => callHostTask(ctx, "phone.provision.status", {}),
        armProvisioning: () => callHostTask(ctx, "phone.provision.arm", {}),
        reconnectAdb: (payload) =>
          callHostTask(ctx, "phone.device.reconnectAdb", payload || {}, { timeoutMs: 90000 }),
        deviceStatus: (payload) =>
          callHostTask(ctx, "phone.device.status", payload || {}, { timeoutMs: 6000 }),
        thumbnail: (payload) =>
          callHostTask(ctx, "phone.device.thumbnail", payload || {}, { timeoutMs: 12000 }),
        startTask: (text) => startTask(ctx, null, text),
        runTemplate: (templateId) => startTemplateTask(ctx, templateId),
        stopTask: () => stopTask(ctx),
        openTask: (taskSessionId) => openTask(ctx, taskSessionId),
        scheduleTask: (text, minutes) => scheduleTask(ctx, text, minutes),
        cancelSchedule: (id) => cancelScheduleTask(ctx, id),
        updateSchedule: (id, payload) => updateScheduleTask(ctx, id, payload),
        saveTemplate: (text) => saveTemplate(ctx, text),
        deleteTemplate: (id) => deleteTemplate(ctx, id),
        renameTemplate: (id, name) => renameTemplate(ctx, id, name),
        readTemplateFile: (id) => readTemplateFile(ctx, id),
        saveTemplateFile: (id, content) => saveTemplateFile(ctx, id, content),
        hostInfo: () => hostInfo(ctx),
        reviewTemplate: (id) => reviewTemplate(ctx, id),
        applyTemplateReview: (id, uids, dryRun) => applyTemplateReview(ctx, id, uids, dryRun),
        rollbackTemplate: (id) => rollbackTemplate(ctx, id),
        replayTask: (historyId) => replayTask(ctx, historyId),
        deleteHistory: (item) => deleteHistoryRecord(ctx, item),
        clearHistory: () => clearHistoryRecords(ctx),
        dismissResult: () => dismissTask(),
        setDevice: (device) => setState({ device }),
        closePanel: () => {
          try {
            ctx.layout.selectPanel(null);
          } catch (_) {}
        }
      };
      exports.__test.taskCallbacks = taskCallbacks;
      ctx.slots.inject("sidebar.panellist", () =>
        ctx.slots.register(
          {
            name: "sidebar.panellist",
            id: PANEL_ID,
            order: 15,
            label: () => "手机任务"
          },
          PhonePanelIcon
        )
      );
      ctx.slots.inject("main", () =>
        ctx.slots.register(
          {
            name: "main",
            key: PANEL_ID,
            inject: () => taskCallbacks
          },
          PhoneTaskPage
        )
      );
      syncHost(ctx).catch(() => {});
      if (typeof ctx.effect === "function") {
        ctx.effect(() => () => clearTimers());
      }
    }

    exports.name = "dsh-plugin-phone";
    exports.inject = ["slots", "sessions", "connection", "layout", "uiWorkspace"];
    exports.apply = apply;
    exports.__test = {
      getState,
      setState,
      subscribe,
      buildTaskPrompt,
      openSession,
      openTask,
      getUiWorkspace,
      startTask,
      stopTask,
      scheduleTask,
      cancelScheduleTask,
      updateScheduleTask,
      dismissTask,
      clearTimers,
      useThumbnail,
      THUMBNAIL_POLL_MS,
      templates: () => templates,
      formatAction,
      formatDuration,
      formatClock,
      formatTime,
      atFromClock,
      timeInputValue,
      friendlyError,
      staleHostHint,
      CLIENT_VERSION,
      mergeHistory,
      sameHistoryRecord,
      deleteHistoryRecord,
      clearHistoryRecords,
      deviceSummary,
      PhoneTaskPage,
      StatusBar,
      SetupWizard,
      TaskComposer,
      RunningTask,
      ResultCard,
      RecentTasks,
      HistoryPanel,
      TemplateManager,
      TemplateEditor,
      TemplateReviewPanel,
      EyeDownNotice,
      OfflineNotice,
      ScheduleEditor,
      ImagePreview,
      PhonePanelIcon,
      PANEL_ID
    };
    return module.exports;
  }
});

# 第四阶段：本地控制服务 + DSH 插件

> v0.1 · 2026-11-10
>
> 状态：**离线完成 + 真机链路实测**。本地 HTTP 服务、Python 客户端、`dsh-plugin-phone` 已实现；4 个 DSH 工具已在 mock 与真机（Android 16）上实测。
>
> 测试：当前 Python **133 passed**，插件 Node **16 passed**（含客户端 Dock）。

## 1. 架构

```text
DSH 模型
   │  phone_screenshot / phone_find_element / phone_click / phone_swipe
   ▼
dsh-plugin-phone（薄 HTTP 客户端，不碰设备）
   │  HTTP + Bearer token
   ▼
phone serve（本机 127.0.0.1:8765）
   │  Phone = Device + FrameSource
   ▼
ESP32-S3 串口 / adb 截图 / 虚拟手机
```

服务拥有设备连接和 OCR；插件只做参数校验与 HTTP 调用。这样 DSH 插件可以独立升级，不会把 Python/串口逻辑耦合进 DSH。

## 2. 本地控制服务

### 启动

```bash
# 虚拟手机（无需硬件）
.venv/bin/phone --mock serve \
  --frames scenarios/frames/settings \
  --http-port 8765 \
  --token demo-token

# 真机：串口手 + adb 眼（单帧截图）
.venv/bin/phone --port /dev/cu.usbserial-XXXX serve \
  --adb <SERIAL> \
  --token demo-token

# 真机：串口手 + scrcpy 实时眼（推荐，采集不再等截图）
.venv/bin/phone --port /dev/cu.usbserial-XXXX serve \
  --scrcpy <SERIAL> \
  --token demo-token

# 没有 scrcpy 时的回退（adb screenrecord 实时流）
.venv/bin/phone --port /dev/cu.usbserial-XXXX serve \
  --screenrecord <SERIAL> \
  --token demo-token

# 只在本机演示时可关闭鉴权（不要暴露到 0.0.0.0）
.venv/bin/phone --mock serve --frames scenarios/frames/settings --no-auth
```

启动后打印：

```text
phone service: http://127.0.0.1:8765
auth: bearer token
token: demo-token
```

`--token-file PATH` 可把 token 写入文件（权限 600），例如：

```bash
.venv/bin/phone --mock serve --frames scenarios/frames/settings \
  --token-file ~/.dsh/phone-service/token
```

### 路由

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 无需鉴权，返回服务版本、`platform`（`android`/`ios`）、鉴权开关，以及 `capture{state,degraded,age_ms,reason}` 采集新鲜度探针（不碰手、不抢锁，供 supervisor 每 20s 轮询）；`state` ∈ `not_ready / streaming / static / stopped / error / device_unavailable / unknown` |
| GET | `/status` | 设备状态、屏幕尺寸、服务 uptime、`platform`、`hand{ok,error}`、`capture`（采集源名称 / 流统计 / OCR 帧缓存命中 / `health{degraded,age_ms,reason}`）；iOS 眼-only 时手未连接不影响本接口 |
| GET | `/screenshot` | 直接返回 PNG；`?format=json&base64=1&max_width=360` 返回 JSON（含 `frame_age_ms`） |
| GET | `/events` | SSE 事件流（action / screenshot / find / run） |
| POST | `/tap` | `{x,y,hold_ms?}` |
| POST | `/swipe` | `{x1,y1,x2,y2,ms?}` |
| POST | `/key` | `{code}` |
| POST | `/text` | `{value}` |
| POST | `/screenshot` | `{include_base64?,max_width?}`，返回 `frame_age_ms` |
| POST | `/find` | `{text,min_score?,mode?,roi?,index?}`，返回 `frame_age_ms` |
| POST | `/shutdown` | 让服务自行退出（supervisor 用于重启采集卡死的服务） |
| POST | `/run` | `{scenario,run_dir?}` |
| GET | `/provision/devices` | BLE 扫描配网设备 |
| POST | `/provision` | `{ssid,password,device?,pop?}` 后台配网 |
| GET | `/provision/status` | 配网状态/结果（成功后含 `wifi`） |
| POST | `/provision/arm` | 让已连接设备进入配网窗口 |

鉴权：`Authorization: Bearer <token>` 或 `X-Phone-Token`；SSE 可用 `?token=...`。

帧新鲜度：`/screenshot`、`/find` 返回的 `frame_age_ms` 是这一帧的年龄。**静止画面不等于失效**：
Android 只在画面变化时推流，所以 scrcpy 子进程活着时旧帧就是当前画面（不再每次走 adb 单帧兜底），
`/health` 报 `state=static, degraded=false`。只有采集子进程死掉、且帧龄超过
`--max-frame-age`（默认 60s，0 关闭）才返回 `503 STALE_FRAME`。`capture.degraded` 只在
"重启服务可能有用"时置真（`stopped` / `error`），供 supervisor 重启服务；设备本身就离线
（`device_unavailable`，例如 adb 未连接）时不置真，避免重启风暴，采集会在下一次 grab 时自行重试。

### 服务自动拉起

插件 Host 侧内置 `service-supervisor`：启动时和每 20s 检查 `http://127.0.0.1:8765/health`，
不可达则按 `cordis.patch.yml` 里的 `serviceCommand` 以 detached 方式拉起 `phone serve`，
日志写入 `serviceLog`（默认 `runs/phone-service.log`）。因此 DSH 重启、插件重载后不再需要
手动开服务；服务崩溃也会在 20s 内被拉起。

```yaml
config:
  autoStartService: true
  serviceCwd: /path/to/assistantAgent
  serviceCommand: [/path/to/.venv/bin/phone, --tcp, hand.local, serve, --scrcpy, --no-auth]
  serviceLog: /path/to/runs/phone-service.log
```

注意：`serviceCommand` 里的路径是本机安装时生成的，仓库移动后需要更新。

采集真故障（`/health` → `capture.degraded=true`，例如 scrcpy 子进程退出、或采集内部报错）时，
supervisor 会先调 `POST /shutdown` 让旧服务退出，再按 `serviceCommand` 重新拉起；重启间隔按
`serviceDegradedBackoffMs`（默认 120s）指数退避到 `serviceDegradedBackoffMaxMs`（默认 900s），
避免设备真的离线时反复重启风暴。`serviceRestartOnDegraded: false` 可关闭该行为。

### 采集源与 OCR 性能

- `--image / --adb / --scrcpy / --screenrecord / --frames` 五选一；`--scrcpy` 在后台线程解码
  H.264，`/find`、`/screenshot` 都取**最新帧**（不再触发 adb screencap）。
- 服务启动会 warmup OCR 模型；视觉依赖缺失时只告警，`/tap` 等仍可用。
- 采集卡死自愈：scrcpy 录制走 FIFO，等待写入端有 `--max-frame-age` 同源的超时（默认 10s），
  子进程在 Recording 前退出会立刻报错并重启流，不会再永久阻塞读线程。
- `ScrcpySource` 超过 `fallback_after`（默认 2.5s）没有新帧时：若采集子进程还活着，说明只是画面静止，
  直接复用最后一帧（毫秒级）；只有子进程死了才改用 `adb exec-out screencap` 单帧兜底
  （缓存 1.5s、失败退避 5s）。`stats` 里 `effective_age_ms = min(age_ms, fallback_age_ms)`
  才是 `/screenshot` 实际会返回的帧年龄，`stats.process_alive` 标记采集子进程是否存活。
- 采集启动即失败（例如服务启动时手机离线）会走 fatal 重试：下一次 grab 在 15s 冷却后自动重新拉起，
  不需要重启整个服务；`/health` 报 `device_unavailable` 且不触发 supervisor 重启。
- 多设备时优先用 USB 序列号（`adb devices` 里非 `host:port`/非 mDNS 的在线设备），无线 adb 整帧传输
  慢数倍；只有在没有 USB 设备时才让 adb 自己选默认设备。
- `/screenshot` 不再探测控制手（手只影响输入）；`/find` 每分钟最多探测一次手来刷新屏幕尺寸，
  手离线时沿用上次尺寸继续返回 OCR 结果。手部 TCP 域名解析带 2s 上限并缓存上次成功 IP，
  避免 mDNS 不应答时每次重连卡 2-5s。
- `GET /status` 的 `capture.stats` 有 `frames / fps / age_ms / fallback_age_ms / effective_age_ms /
  restarts / error`，用于真机长跑观察：

```bash
curl -s http://127.0.0.1:8765/status | jq '.capture'
# {"name":"scrcpy:XXXX","stats":{"frames":812,"fps":29.4,"age_ms":17.3,...},"ocr_cached_frames":37}
```

- OCR 两阶段 + 帧/文字块缓存让同一画面的连续 `/find` 降到毫秒级；详见
  [The Eye 设计与状态](phase2-eye.md) 第 7 节。

### 并发与安全

- 所有设备操作串行化；忙时返回 `409 BUSY`，不会并发写串口。
- 默认只绑定 `127.0.0.1`；`--no-auth` 仅用于本机演示。
- `/run` 会读取本机场景文件并执行动作，只应暴露给可信客户端。
- 设备错误返回 `502`，参数错误返回 `400`，鉴权失败 `401`。

### Python 客户端

```python
from phone_console.client import PhoneClient

phone = PhoneClient("http://127.0.0.1:8765", token="demo-token")
print(phone.status())
match = phone.find("设置")
if match["found"]:
    phone.tap(*match["match"]["device_center"])
phone.screenshot("runs/shot.png", max_width=540)
```

## 3. DSH 插件

目录：[dsh-plugin-phone](../dsh-plugin-phone/)。插件版本 **0.3.2**。

### 任务 Dock（自然语言）

侧边栏插件区（工作区上方）新增「📱 手机任务」入口；点击后通过 `main` slot 在主导航区打开独立任务页面（不再占用对话框）。实现使用 `sidebar.panellist` + `main key=phone-task`，不是 `sidebar.footer.action`。

- 输入自然语言任务，点「开始」
- 创建一个**独立任务会话**（不是当前聊天），Agent 用 `phone_*` 工具执行
- 任务 prompt 强制“截图 → 找元素 → 点击/滑动 → 重新截图验证”的循环
- 运行中可「停止」（取消任务会话）或「查看会话」
- 内置模板、历史记录、按分钟定时
- 安全约束：验证码/登录/支付/删除/购买默认停止并报告；最多 40 步

定时器现在是 Host 定时器：DSH Host 进程运行期间到点执行；Host 关闭后错过的任务标记为 missed。

客户端会话 API 兼容 `ctx.sessions.open/select` 与 `create` 返回 `SessionId`/`RpcResult` 两种形态；若当前 DSH 客户端没有导航 API，任务仍在独立会话后台启动，并提示从侧边栏打开。

### Host 任务 RPC（P1）

Host 侧新增 `/dsh-phone-task-v3` 同源 RPC（带 DSH 鉴权），由 `lib/task-service.js` 和 `lib/task-rpc.js` 提供：

- `phone.task.start`：先检查 `phone serve /health`；带 `sessionId` 时 attach 到客户端创建的任务会话，不带时 Host 自建 agent（定时/回放）
- `phone.task.status`：状态、步骤数（`tool/call` 中 `phone_*` 次数）、最近动作、错误
- `phone.task.stop`：取消 agent
- `phone.task.history`：持久化历史
- `phone.task.templates/saveTemplate/deleteTemplate`：任务库
- `phone.task.schedule/schedules/cancelSchedule`：Host 定时
- `phone.task.replay/devices`：回放与多设备

服务不可用时返回 `PHONE_SERVICE_DOWN`，并给出 `phone ... serve ...` 启动命令。

### P2 任务库与自动化

- 模板与变量：`{{wifi_name}}`、`{{account}}` 等由 Host `renderTemplate` 替换
- 定时：Host `setTimeout` 持久化，重启恢复；错过标记 missed（APScheduler 属阶段五）
- 多设备：`config.devices` 映射 baseUrl/token，Dock 可选设备
- 回放：历史记录一键 replay

### 工具

| 工具 | 参数 | 返回 |
|---|---|---|
| `phone_screenshot` | `maxWidth?`、`includeBase64?` | PNG 路径、宽高、字节数，可选 base64 |
| `phone_find_element` | `keyword`、`minScore?`、`mode?`、`roi?`、`index?` | `found`、文字、分数、手机像素 `x/y`、截图路径 |
| `phone_click` | `x`、`y`、`holdMs?` | `ok`、HID ACK |
| `phone_swipe` | `x1`、`y1`、`x2`、`y2`、`ms?` | `ok`、HID ACK |

`phone_screenshot` 默认返回文件路径，模型可用 `read_image` 查看；`includeBase64` 仅在内联数据确实需要时使用，避免上下文膨胀。

### 安装

本机已通过 DSH 插件管理器以 `file:` 方式安装并启用（0.3.2）：

```text
include:dsh-plugin-phone  active
```

修改客户端后需在插件管理器中重新安装/启用，并刷新 Web GUI 一次。

在别的机器上，用 DSH 插件管理器安装 `dsh-plugin-phone/` 目录（`file:` 路径）即可；包内 `cordis.patch.yml` 会把自己插入 profile。

### 配置

默认 `baseUrl` 是 `http://127.0.0.1:8765`。token 按以下顺序解析：

1. 插件配置 `token`
2. 环境变量 `PHONE_SERVICE_TOKEN`
3. `~/.dsh/phone-service/token`（可用 `tokenFile` 覆盖）

在 profile 的 `cordis.patch.yml` 中覆盖：

```yaml
- id: dsh-plugin-phone
  name: dsh-plugin-phone
  config:
    baseUrl: http://127.0.0.1:8765
    token: demo-token
    timeoutMs: 20000
```

不要提交真实 token。

## 4. 验证

### 自动化

- Python：`117 passed`（服务 9 + 客户端 5 + 既有 103）。
- 插件：`node --test` **16 passed**（工具 + 客户端 Dock）。
- 服务测试覆盖：健康检查、鉴权、tap、截图（PNG/JSON）、find 坐标、run、BUSY 409、错误请求 400、事件总线。
- 插件测试覆盖：HTTP 客户端鉴权/错误映射、4 个工具的输出映射与渲染。

### DSH 实测（mock 手机）

通过 DSH 子代理调用已安装插件：

```text
phone_find_element("设置") → found=true, x=133, y=168, score=0.9995
phone_screenshot(maxWidth=360) → 360x800, PNG path
phone_click(133, 168) → Clicked phone (133, 168)
```

三步全部成功，证明 DSH → 插件 → 服务 → 虚拟手机链路可用。

### 真机

已实测（HONOR PTP-AN00 / Android 16）：

```bash
phone --port /dev/cu.usbserial-XXXX serve --adb <SERIAL> --http-port 8765
```

- 服务 `find` 会先 `hello`，屏幕映射为 1264×2800。
- DSH 子代理：`phone_find_element("更多 WLAN 设置")` → (296,864)，`phone_click` 成功跳转页面。
- Eye→Hand：`phone eye find WLAN --adb ...` → (310,1200)，`eye tap` 打开 WLAN 页。

## 5. 限制与下一步

- 当前单机单手机、单动作串行；多设备路由未做。
- SSE 是基础事件流，没有断线补发/订阅过滤。
- 服务重启后 token 可能变化；用 `--token-file` 固定。
- 真机时延、adb 截图速度、断线重连仍需实测。
- 下一步：真机联调；阶段五 APScheduler + MQTT/Webhook；或按需补多设备与远程鉴权。

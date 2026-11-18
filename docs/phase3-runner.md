# 第三阶段：场景 Runner 与虚拟手机（离线核心）

> v0.1 · 2026-11-10
>
> 状态：**离线核心已完成**；3 个演示场景通过；上位机 103 个测试通过；真机验收待硬件。
>
> 目标：把“手”和“眼”串成可重复的确定性流程，并在没有手机时也能回归。

## 1. 链路

```text
scenario.json
   │
   ▼
ScenarioRunner ──> Phone ──> Device（串口/mock）
   │                 │
   │                 └──> FrameSource（adb/图片/帧序列）
   │
   ├── wait_for_text / assert_text：抓帧 → OCR → 匹配
   ├── tap_text：抓帧 → OCR → 坐标 → tap
   ├── swipe / key / text / tap：直接下发
   └── sleep：等待
```

`Phone` 是统一抽象：真机用 `DevicePhone`（`Device` + `FrameSource`），CI 用 `VirtualPhone`（帧序列 + `MockDevice`）。场景层不关心底层是真机还是虚拟手机。

## 2. 模块

| 模块 | 职责 |
|---|---|
| `phone.py` | `Phone` 协议、`DevicePhone`、`NullSource` |
| `scenario.py` | 场景 JSON 校验、步骤执行、重试/超时、报告、失败截图 |
| `virtual.py` | `VirtualPhone`：帧序列 + MockDevice，动作后前进一帧 |
| `mock.py` / `transport.py` | MockDevice 与故障注入（BUSY/FAULT、丢响应、写失败） |
| `cli.py` | `phone run` 子命令 |

## 3. 场景格式

```json
{
  "name": "settings-demo",
  "description": "设置 → 无线局域网 → 返回",
  "frames": "frames/settings",
  "screen": [1080, 2400],
  "defaults": {"timeout": 3.0, "retries": 1, "interval": 0.1, "retry_on_timeout": false},
  "steps": [
    {"action": "tap_text", "text": "无线局域网"},
    {"action": "assert_text", "text": "WLAN"},
    {"action": "key", "code": "BACK"},
    {"action": "swipe", "x1": 540, "y1": 1800, "x2": 540, "y2": 600, "ms": 400},
    {"action": "sleep", "ms": 100}
  ]
}
```

### 步骤

| action | 参数 | 说明 |
|---|---|---|
| `wait_for_text` | `text`、`timeout`、`interval`、`min_score`、`mode`、`roi` | 轮询当前帧，直到文字出现 |
| `assert_text` | 同上 | 同 `wait_for_text`，失败码为 `ASSERT_FAILED` |
| `tap_text` | `text`、`retries`、`interval`、`min_score`、`mode`、`roi`、`hold_ms`、`retry_on_timeout` | OCR 找文字 → 点中心；找不到或可重试错误时重试 |
| `tap` | `x`、`y`、`hold_ms`、`retries`、`retry_on_timeout` | 直接坐标点击 |
| `swipe` | `x1`、`y1`、`x2`、`y2`、`ms`、`retries`、`retry_on_timeout` | 滑动 |
| `key` | `code`、`retries`、`retry_on_timeout` | 按键 |
| `text` | `value`、`retries`、`retry_on_timeout` | ASCII 文本 |
| `sleep` | `ms` | 等待 |

### 语义

- `timeout` 用于 `wait_for_text` / `assert_text`；`retries` 用于 `tap_text` 与 HID 动作（重试次数，不含首次）。
- `tap_text` 的重试同时覆盖“没找到文字”和可重试的设备错误。
- 默认可重试错误：`BUSY`、`RATE_LIMIT`（请求被明确拒绝，动作未执行）。
- **`TIMEOUT` 默认不重试**：tap/swipe/key/text 可能已经执行，重试会造成“一次点击变两次”。确需重试时在步骤或 `defaults` 里显式设 `"retry_on_timeout": true`，仅建议虚拟手机或可幂等场景使用。
- 不重试：`NOT_READY`、`OUT_OF_RANGE`、`BAD_VALUE`、`DISCONNECT` 等，直接失败。
- 默认遇到第一个失败就停止；报告保留已执行步骤。
- `roi` 是截图像素 `[x,y,w,h]` 或 `"x,y,w,h"`，只 OCR 该区域，可显著提速。

## 4. 虚拟手机

```text
frames/01.png, 02.png, 03.png ...
```

- 按文件名排序加载；每个**成功动作**后前进一帧；`grab`（等待/断言）不前进。
- 因此帧序列要与动作对齐：`tap_text` 在第 0 帧找到并点击 → 成功 → 第 1 帧 → `assert_text` 看到第 1 帧。
- `--mock` 时：
  - 场景里有 `"frames"` 就直接用；
  - `--frames DIR` 覆盖；
  - `--image PATH` 用静态图（不前进）；
  - 都没有则只允许纯 HID 场景（`swipe/key/text/tap/sleep`）。
- 故障注入（测试用）：
  - `phone.transport.drop_responses = 1`：丢一次响应 → 客户端 `TIMEOUT`；
  - `phone.transport.write_error = ConnectionError(...)`：模拟断线；
  - `phone.mock.inject_busy(n)`：下 n 个动作返回 `BUSY`；
  - `phone.mock.inject_fault()`：进入 `FAULT`，动作返回 `NOT_READY`。

## 5. CLI

```bash
# 校验场景，不执行
.venv/bin/phone run scenarios/settings-demo.json --dry-run

# 虚拟手机跑演示场景（场景自带 frames）
.venv/bin/phone --mock run scenarios/settings-demo.json
.venv/bin/phone --mock run scenarios/price-protect-demo.json
.venv/bin/phone --mock run scenarios/game-daily-demo.json

# 指定帧目录 / 静态图
.venv/bin/phone --mock run my.json --frames path/to/frames
.venv/bin/phone --mock run my.json --image shot.png

# 真机：串口手 + adb 眼
.venv/bin/phone --port /dev/cu.usbserial-XXXX run my.json --adb <SERIAL>

# JSON 结果 / 指定产物目录
.venv/bin/phone --mock run scenarios/settings-demo.json --json --run-dir /tmp/run

# 录制会话：帧 + 动作（默认 run_dir/record）
.venv/bin/phone --mock run scenarios/settings-demo.json --record /tmp/session
```

`--record [DIR]` 会把每次抓取的帧保存为 `frames/NNNN.png`，动作写入 `actions.jsonl`；用于真机会话留档，之后可人工整理成帧序列场景。

`--json` 只输出最终结果 JSON；默认输出逐步进度和摘要。

## 6. 产物

默认写入 `runs/<时间戳>-<场景名>/`：

```text
scenario.json                 场景副本
report.jsonl                  每步一行 + summary
failure-01.png                失败时的当前帧
failure-01-annotated.png      带 OCR 框的失败帧
failure-01.json               失败步骤的错误详情
record/                       --record 时的帧与动作（可选）
```

`runs/` 已在 `.gitignore` 中。

## 7. 演示场景

| 场景 | 帧 | 步骤 |
|---|---|---|
| `settings-demo` | 3 张：设置、WLAN、返回 | tap 无线局域网 → assert WLAN → BACK → assert 设置 → swipe → sleep |
| `price-protect-demo` | 6 张：桌面 → 京东 → 我的 → 客服 → 价保 → 成功 | 5 次 tap_text + assert 申请成功 |
| `game-daily-demo` | 3 张：任务 → 战斗 → 胜利 | tap 开始挑战 → tap 继续 → assert 胜利 → BACK |

帧资源由 [tools/make_scenario_assets.py](../tools/make_scenario_assets.py) 生成，可重复构建。

## 8. 验证

- `pytest`：**103 passed**（原 53 + 新增 50）。
- 三个演示场景用真实 RapidOCR + 虚拟手机全部通过：
  - settings 6/6，约 1.0s
  - price 6/6，约 1.4s
  - game 4/4，约 0.7s
- 覆盖：场景校验、重试（BUSY；TIMEOUT 默认不重试、可显式开启）、FAULT/DISCONNECT、等待超时、失败截图、JSONL 报告、录制、CLI dry-run/JSON/退出码。

## 9. 限制与下一步

- 虚拟手机的“动作后前进一帧”是脚本化模型，不是真实 UI 状态机；帧数必须与动作对齐。
- `--record` 保存的是原始抓帧与动作，不会自动生成可回放场景；需要人工挑选/对齐帧。
- 真机验收仍需硬件：把 `--mock` 换成 `--port ... --adb`，场景脚本不变。
- adb 单帧采集较慢（数百毫秒）；实时 scrcpy 流接入后，`wait_for_text` 的轮询会更接近真实闭环。
- 下一步可选：真机联调、本地控制服务 + DSH 插件、或阶段五定时/远程。

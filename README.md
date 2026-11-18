# assistantAgent · ESP32-S3 HID 手机自动化 + DeepSeek Harness 插件

用手写一个「物理手」：一块 ESP32-S3 通过 USB 充当手机的外接鼠标/键盘，配合电脑侧的
**The Eye**（scrcpy / adb 截图 + OCR），让 AI agent 真正看得见、点得到手机。

本仓库包含四部分：

| 组件 | 作用 |
|---|---|
| `firmware/esp32s3-hand` | ESP32-S3 固件：USB HID 触摸/按键 + WiFi TCP 控制 + BLE 配网（PlatformIO） |
| `console/` | Python 后端 `phone_console`：设备连接、截图/OCR、场景 Runner、本地 HTTP 服务、模板引擎 |
| `dsh-plugin-phone/` | DeepSeek Harness 插件：`phone_*` 工具、任务面板、模板执行/复盘、模型引导执行 |
| `tools/`、`scripts/` | 命令行工具（模板编译/执行/复盘、ADB 重连、素材生成）与服务启动脚本 |

> 适合：让 AI 操作真实 Android 手机（签到、查账、设置、进入某个 App 的深层页面），
> 又不想 root / 不想用 Accessibility 服务、希望「所见即所得」的场景。

---

## 它解决什么问题

纯视觉方案（ADB 注入事件）在部分 App 上不稳定；纯坐标脚本换台设备就失效。
这里用「物理手 + 眼睛」的组合：

- **手**：ESP32-S3 通过 USB HID 发送真实触摸事件，App 无法区分它与真人点击
- **眼睛**：scrcpy 实时帧 + OCR，拿到屏幕上每个文字的物理像素坐标
- **模型**：DSH agent 用 `phone_screenshot` / `phone_find_element` / `phone_click` 等工具完成任务
- **固化**：成功任务可存为模板（`templates/*.md`），之后不用模型即可回放；页面漂移时自动校验失败

```
        ┌────────────── DSH (DeepSeek Harness) ──────────────┐
        │  agent  ⇄  phone_* tools  ⇄  dsh-plugin-phone      │
        └───────────────────────┬────────────────────────────┘
                                │ HTTP (127.0.0.1:8765)
                    ┌───────────▼───────────┐
                    │  phone serve (Python) │  ← OCR / 场景 Runner / 模板执行
                    └───────┬───────┬───────┘
             TCP :9000      │       │  scrcpy / adb
                    ┌───────▼───┐   │
                    │ ESP32-S3  │   │  截图、帧
                    │  HID 手   │   │
                    └───────┬───┘   │
                            │ USB HID
                    ┌───────▼───────┐
                    │   Android 手机 │
                    └───────────────┘
```

---

## 快速开始

### 0. 硬件

- ESP32-S3 开发板（USB OTG 支持 HID，带 WiFi）
- Android 手机（开启开发者选项 + USB 调试；`scrcpy` 可正常投屏）
- 电脑：macOS / Linux（Windows 未验证），Python ≥ 3.10，Node ≥ 22（插件），PlatformIO（固件）

### 1. 安装 Python 后端

```bash
git clone https://github.com/ldchaowin/assistantAgent.git
cd assistantAgent

python3 -m venv .venv
.venv/bin/pip install -e 'console[vision]'      # OCR（RapidOCR + OpenCV）
.venv/bin/pip install -e 'console[stream]'      # scrcpy H.264 实时流解码
.venv/bin/pip install pytest                    # 开发/测试
```

无硬件端到端自检：

```bash
make mock          # 虚拟手机 status
make doctor-demo   # 环境诊断
make test          # Python 测试
make test-plugin   # 插件测试（需要 Node 22+）
```

### 2. 烧录固件（有硬件时）

```bash
make build         # 触摸屏 HID 模式
make flash PORT=/dev/cu.usbserial-XXXX
```

固件会开一个 WiFi TCP 控制端口（默认 9000）并通过 mDNS 广播主机名。
细节见 [`docs/flashing.md`](docs/flashing.md)、[`docs/hid-modes.md`](docs/hid-modes.md)。

### 3. 启动本地服务

```bash
# 环境变量都可用；默认 hand.local:8765，无 token（仅本机监听）
export HAND_HOST=hand.local
export PHONE_SERVICE_TOKEN="$(openssl rand -hex 16)"   # 建议开启
scripts/serve_phone.sh
```

然后：

```bash
curl localhost:8765/health
curl localhost:8765/status      # state / screen / hand
```

### 4. 安装 DSH 插件

1. 编辑 DSH profile 的 `cordis.patch.yml`，把 `dsh-plugin-phone/cordis.patch.yml` 的内容
   合并进去，按提示改三处路径（仓库路径、`hand.local`、token）。
2. 同步插件代码到 profile：

```bash
make plugin-reinstall        # 默认 DSH_HOME=~/.dsh DSH_PROFILE=desktop
# 注意：cordis.patch.yml 里的三处 /path/to/assistantAgent 必须改成你的仓库绝对路径
# 或：make plugin-reinstall DSH_HOME=/path/to/.dsh DSH_PROFILE=profile-name
```

3. **完全退出并重开 DSH Desktop**（host 改动必须重启）。

打开 DSH，右侧会出现「手机」面板；对 agent 说「打开设置，看看 Wi-Fi 连的是哪个网络」即可。

---

## 核心能力

### 插件工具

| 工具 | 说明 |
|---|---|
| `phone_screenshot` | 截屏（可缩放、可返回 base64） |
| `phone_find_element` | OCR 找文字，返回手机物理像素坐标 |
| `phone_ocr_texts` | 一次列出整屏文字 + 坐标（比反复 find 快） |
| `phone_click` / `phone_swipe` / `phone_key` / `phone_text` | 物理手输入 |

### 任务面板

- 「任务」：写一句自然语言目标，agent 规划并执行
- 最近任务 / 全部记录：状态、耗时、逐步轨迹、复盘、再执行一次、存为模板
- 「执行模板」：模板 = 前置导航（文字/按键）+ 确认页面锚点 + 主路径（坐标 + 每步断言）
- 「存为模板」：把一次**成功**的执行总结成 md；模型总结失败会明确报错，不会偷偷降级
- 页面不在预期时，可交给模型现场导航（guided），成功路径可写回模板

### 模板格式（简版）

```yaml
id: example
name: 示例
version: 1
success_expect: 我知道了        # 只有成功那一刻才出现的文字（可瞬时）
screen: [1264, 2800]
entry:                           # 先到达确认页面，再执行主路径
  expect: 专属客服
  approach:
    - {do: key, code: HOME}
    - {do: tap_text, text: 淘宝}
steps:
  - {id: s1, do: tap, text: 专属客服, at: [0.82, 0.10], expect_after: 官方自助服务}
  - {id: s2, do: tap, text: 价保中心, at: [0.87, 0.17], expect_after: 一键价保}
```

- 坐标归一化 0–1（相对 `screen`），超过 30 天自动降级为文字点击
- `expect_after` 编译成紧跟该步的 `assert_text`；异步步骤用 `check_timeout`
- 完整语法：[`dsh-plugin-phone/README.md`](dsh-plugin-phone/README.md)

---

## 安全与隐私

本项目是**本机单用户**工具，默认没有云同步：

- 屏幕帧与 OCR 文本会经过本地服务进入你配置的模型，请按需选择模型/提供商
- `phone serve` 默认只监听 `127.0.0.1`；开启 token 后请求需携带 Bearer token
- 固件的 TCP 控制端口（默认 9000）**没有应用层鉴权**：只在可信局域网使用，
  不要把端口暴露到公网；详见 [`docs/wifi-tcp.md`](docs/wifi-tcp.md)
- 模板包含你手机上的坐标与页面文字，发布/分享前请自行脱敏

---

## 仓库结构

```
console/            Python 后端（phone_console + pytest）
dsh-plugin-phone/   DSH 插件（host lib + client + node:test）
firmware/           ESP32-S3 PlatformIO 工程（源码）
tools/              模板/场景/诊断 CLI
scripts/            服务启动脚本
docs/               烧录、校准、协议、WiFi、iOS、Runner、插件
scenarios/          合成帧与演示场景（可重新生成）
examples/templates/ 示例模板
```

## 开发

```bash
make test          # Python
make test-plugin   # 插件
make tpl-check     # 校验示例模板
make build         # 固件
```

CI：`.github/workflows/ci.yml`（Python 测试 + 插件测试）。

## 文档

| 文档 | 内容 |
|---|---|
| [`docs/flashing.md`](docs/flashing.md) | 固件编译/烧录/恢复 |
| [`docs/calibration.md`](docs/calibration.md) | 触摸校准 |
| [`docs/protocol.md`](docs/protocol.md) | 串口/TCP 协议 |
| [`docs/wifi-tcp.md`](docs/wifi-tcp.md) | 无线控制与配网 |
| [`docs/hid-modes.md`](docs/hid-modes.md) | 触摸 / 绝对鼠标模式 |
| [`docs/phase2-eye.md`](docs/phase2-eye.md) | 截图 / 视频流 / OCR |
| [`docs/phase3-runner.md`](docs/phase3-runner.md) | 场景 Runner |
| [`docs/phase4-service-plugin.md`](docs/phase4-service-plugin.md) | 本地服务与 DSH 插件 |
| [`docs/ios.md`](docs/ios.md) | iOS 实验性支持 |
| [`docs/test-plan.md`](docs/test-plan.md) | 验收清单 |

## License

MIT，见 [`LICENSE`](LICENSE)。第三方组件（esp_prov、Arduino/ESP-IDF 等）许可见
[`NOTICE`](NOTICE) 与 `console/phone_console/provisioning/vendor/VENDORED.md`。

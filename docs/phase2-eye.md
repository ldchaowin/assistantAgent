# 第二阶段 The Eye：离线核心 + 实时视频流

> v0.2 · 2026-11-11
>
> 状态：**离线核心、实时流适配、OCR 提速均已完成并测试**（上位机 197 个测试通过）。
> 真机实时流（scrcpy/screenrecord）代码路径已就绪，等待插手机做首次实测。

## 1. 目标

让控制台“看得见、看得快”：拿到手机当前画面，找到指定文字，返回手机物理分辨率下的中心坐标，并可交给 `tap` 执行。

本阶段不引入大模型，只做确定性视觉定位；实时流只做“采集”，输入仍走第一阶段的 HID（scrcpy 控制通道被显式关闭）。

## 2. 范围

### 做

- `FrameSource` 抽象：`ImageSource`（静态截图）、`AdbSource`（adb 单帧）、`ScrcpySource` / `ScreenrecordSource`（H.264 实时流）
- RapidOCR（PP-OCRv4 ONNX）两阶段封装：**缩图检测 → 原图裁切 → 逐行识别**
- 帧级 / 文字块级内容缓存：画面没变不重复推理，只重认变化的文字块
- 单行快速通道：给定 ROI 时跳过检测，直接识别（约 5 ms）
- 文本匹配：`contains` / `exact` / `regex`、置信度阈值、阅读顺序、`--index`
- 截图坐标 → 手机物理像素映射（含缩放与边界 clamp）
- CLI：`phone eye ocr / find / tap / shot / bench / stream / watch`
- USB 调试授权辅助：`phone adb status / wait`（把 `unauthorized` 变成可执行提示）
- 合成截图 fixture 与单元/集成测试（含 H.264 编解码端到端）

### 不做（后续阶段）

- 横屏 / 旋转 / 折叠屏适配、多语言模型切换、验证码与风控对抗
- scrcpy 控制通道（点击/滑动仍走 ESP32 HID，避免双通道竞争）
- iOS 原生采集已接入并真机跑通（USB + CoreMediaIO/AVFoundation helper）；投屏窗口眼已实现。**iOS 兼容为实验性/暂停状态**，见 [iOS 文档](ios.md)。
- 阶段三真实业务场景的稳定性（价保、游戏日常等仍需真机跑）

## 3. 链路

```text
scrcpy --record=- --record-format=mkv ─┐
adb exec-out screenrecord -o h264 ─────┤→ H.264 管道 ──PyAV──> 最新帧 Frame(BGR)
adb exec-out screencap -p ─────────────┤                        │
PNG / JPG ─────────────────────────────┘                        ↓
                                          两阶段 OCR：缩图检测 → 原图裁切 → 逐行识别
                                                        │
                                          TextLocator → phone 物理坐标 → device.tap
```

- 实时源在后台线程解码，`grab()` 永远返回**最新帧**；OCR 慢不会阻塞解码。
- 截图坐标 × (手机宽/帧宽) = 手机物理像素；前提是整屏、同宽高比（scrcpy 默认全屏满足）。

## 4. 模块

| 模块 | 职责 |
|---|---|
| `vision/frame.py` | `Frame`：BGR 图像、尺寸、裁剪、保存 |
| `vision/sources.py` | `ImageSource`、`AdbSource`、`FrameSource` 协议 |
| `vision/stream.py` | `H264StreamSource`、`ScrcpySource`、`ScreenrecordSource`、命令构造、PyAV 解码、重启与统计 |
| `vision/adb.py` | adb 解析、`status`/`wait`、USB 调试授权提示 |
| `vision/ocr.py` | `OcrItem`、两阶段 `RapidOcrEngine`、帧/文字块缓存、ROI 偏移 |
| `vision/locate.py` | 文本匹配、阅读顺序、坐标映射 |
| `vision/overlay.py` | 调试标注图 |
| `cli.py` | `phone eye ...`、`phone adb ...` 子命令 |

## 5. 用法

安装依赖（只做“手”时不需要）：

```bash
# OCR
.venv/bin/pip install -e 'console[vision]'
# 实时流解码（PyAV 自带 FFmpeg，无需系统 ffmpeg）
.venv/bin/pip install -e 'console[stream]'
# scrcpy 采集器本身（会带上 ffmpeg / sdl3 / libusb）
brew install scrcpy
```

> scrcpy 运行时需要能找到 `adb`。本仓库的 `ScrcpySource` 会用 `ADB` 环境变量把
> scrcpy 指向 `.tools/platform-tools/adb`（或 `.tools/bin/adb`），**不需要改 PATH**；
> 也可以在 `phone serve --scrcpy` / `eye stream` 时用 `PHONE_ADB` 指定别的 adb。
>
> **scrcpy 4.x 不再支持 `--record=-` 写 stdout**（会生成一个名为 `-` 的空文件），
> 本实现改为写入临时 FIFO 再由 PyAV 解 Matroska；另外 Android 只有在**画面变化**时
> 才产出编码帧，静止画面用 adb 单帧兜底（`ScrcpySource(fallback=True)`，默认开）。
> 手机必须**解锁并保持亮屏**，锁屏/息屏时投屏没有画面。

静态截图与 adb 单帧（同以前）：

```bash
.venv/bin/phone eye ocr --image docs/assets/eye-demo.png
.venv/bin/phone eye find 设置 --image docs/assets/eye-demo.png --screen 1080x2400
.venv/bin/phone eye shot --out shots/now.png --adb
```

实时流（新增）：

```bash
# 先确认设备已授权
.venv/bin/phone adb status
.venv/bin/phone adb wait --timeout 120

# 无线调试（手机与电脑同一 Wi-Fi；手机只有一个 USB 口给 ESP32 时用这个）
# 手机上：开发者选项 → 无线调试 → 使用配对码配对设备，记下 IP:端口 和 6 位码
.venv/bin/phone adb pair 192.168.1.23:37123 123456
.venv/bin/phone adb connect 192.168.1.23:41235   # 用无线调试页面的 IP:端口

# scrcpy 实时流：统计首帧/帧率，保存最后一帧
.venv/bin/phone eye stream --scrcpy --seconds 5 --save shots/live.png

# 没有 scrcpy 时：adb screenrecord 回退（延迟略高，每 180s 自动重启）
.venv/bin/phone eye stream --screenrecord --seconds 5

# 在实时流上找文字（每次都取最新帧）
.venv/bin/phone eye find 设置 --scrcpy --screen 1264x2800

# 轮询直到文字出现（可做场景前置等待）
.venv/bin/phone eye watch 设置 --scrcpy --timeout 20
```

`eye` 的 OCR 相关开关（`ocr / find / tap / bench / watch / stream` 通用）：

| 参数 | 作用 |
|---|---|
| `--det-max PX` | 检测图最长边，默认 1024；`0` = 原图（更准更慢） |
| `--fast` | 旧参数，等价 `--det-max 768` |
| `--line` | 把 `--roi` 当作单行文字，跳过检测（约 5 ms） |
| `--no-cache` | 关闭帧/文字块缓存 |
| `--roi X,Y,W,H` | 只识别该区域（截图像素） |
| `--stream-size / --stream-fps / --stream-bit-rate` | scrcpy 视频参数（默认 1024 / 30 / 8 Mbps） |
| `--stream-time-limit` | screenrecord 重启间隔（默认 180s） |

`eye bench` 输出分阶段延迟（capture / ocr / det / rec）与缓存命中：

```bash
.venv/bin/phone eye bench --image docs/assets/eye-demo.png --repeat 5
make bench
make bench-line          # ROI + 单行快速通道
.venv/bin/python tools/bench_vision.py   # 旧管线 vs 两阶段，批量真机截图
```

## 6. 坐标契约

- OCR 框、`center`、`--roi` 都用**截图像素**。
- `device_center` 用**手机物理像素**：

```text
x_phone = round(x_frame * screen_w / frame_w)
y_phone = round(y_frame * screen_h / frame_h)
```

- 结果 clamp 到 `[0, screen_w-1] × [0, screen_h-1]`。
- 前提：画面是**完整屏幕**且与手机同宽高比。adb 截图、scrcpy 默认全屏（未加 `--crop`）都满足。
- 屏幕像素坐标最终仍走第一阶段的 `cal` 校准映射到 HID；The Eye 不重复做校准。

## 7. 性能（本机实测）

环境：Apple Silicon macOS 26.6.2（10 核）、Python 3.12、RapidOCR 1.4.4（PP-OCRv4 mobile det+rec，`use_cls=False`）、PyAV 19.0.2。

真机截图来自 `runs/service-real/`（HONOR PTP-AN00，1264×2800，13–17 个文字块），合成图 `docs/assets/eye-demo.png`（1080×2400，13 个文字块）。

| 模式 | 合成图 | 真机截图（6 张均值） |
|---|---|---|
| 旧：整帧一次性 OCR | 246 ms | 327 ms |
| 新：缩图检测 + 原图裁切识别（`--det-max 1024`） | **113 ms** | **164 ms**（×2.0） |
| 新：同上，画面未变（帧缓存命中） | **2.2 ms** | **3.1 ms** |
| 新：同上，仅状态栏时间变化（文字块缓存） | — | ~70 ms |
| 新：原图检测（`--det-max 0`） | 265 ms | 265 ms |
| ROI 1080×300 + 缩图检测 | 33 ms | — |
| ROI 230×90 + `--line`（单行） | **5.3 ms** | — |
| H.264 解码（1080×2400 / 462×1026） | 0.79 / 0.24 ms/帧 | — |
| 真机 scrcpy 流（1024 长边，画面活动时） | — | 首帧 3.9 s（含启动）/ 33 fps / 6 s 206 帧 |
| 真机 scrcpy 静止画面 | — | 流不产帧，adb 单帧兜底（≈0.3–1 s）；`find` 仍可用 |
| 真机 462×1024 流帧 OCR | 174 ms（24 个文字块） | 坐标按 1264/462≈2.74 映射 |
| 真机闭环 `real-wlan-demo`（流→OCR→HID） | — | PASS；步骤 OCR 190/206 ms，总 4.97 s |
| 真机服务 `/find` 冷 / 热 | — | 2.77 s / 0.47 s（OCR 0 ms 命中帧缓存） |
| 真机连续滑动+识别 12 轮 | — | 12/12 命中，find 216–243 ms，流 30 fps |

补充：

- `phone eye find ... --adb` 的总耗时还要加上 adb 截图本身（`exec-out screencap -p`，真机约 0.2–0.5 s，随画面内容与 USB 传输波动）；换 `--scrcpy` 后采集变成“取最新帧”，这部分基本消失。
- 真机 `/find` 走服务时，服务进程常驻模型，单次 OCR 就是上表的 164 ms / 缓存 3 ms；服务启动时已 warmup，首次请求不再吃 ~400 ms 的模型加载。
- 缓存是**按像素精确匹配**的（帧：降采样签名逐字节相等；文字块：blake2b 摘要），不会把“看起来很像”的两块文字混用。

## 8. USB 调试模式（决策与边界）

实时流依赖 USB 调试（开发者选项 → USB 调试）。本轮明确**接受**在自有测试设备上开启，边界如下：

| 项 | 结论 |
|---|---|
| 用途 | 仅用于截图/投屏采集（scrcpy、screenrecord）；点击滑动仍走 ESP32 HID |
| 控制通道 | scrcpy 固定加 `--no-control`，不接管输入，避免与 HID 抢焦点 |
| 设备范围 | 仅自有/授权测试机；不用在日常主力机、银行/支付类账号上常开 |
| 风险 | 部分 App 会检测调试开关；已授权的电脑等价于拥有 shell 权限 |
| 退出 | 用完可在开发者选项关闭 USB 调试；关闭后 Eye 自动回退 `--adb` 单帧或 `--image` |
| 授权 | 首次连接手机弹“允许 USB 调试吗？”，选“始终允许”；`phone adb status / wait` 会给出明确提示 |
| 无线调试 | 手机 USB 口被 ESP32 占用时走无线：`phone adb pair HOST:PORT CODE` → `phone adb connect HOST:PORT`（同一 Wi-Fi，5GHz 更稳） |

```bash
phone adb status     # 列出设备、状态（device / unauthorized / offline）与处理提示
phone adb wait       # 轮询到授权为止，可配合插线/点“允许”
phone --mock doctor  # 预检：PyAV、scrcpy、adb 授权状态
```

## 9. 验收对照

| 项 | 状态 |
|---|---|
| 截图 → 文字 → 中心坐标 | 合成图通过，测试覆盖中英文 |
| 截图坐标 → 手机坐标 | 单元测试覆盖缩放、clamp、阅读顺序 |
| tap 端到端（mock） | 通过 |
| tap 端到端（真机） | 通过（第一阶段） |
| 单次 <100 ms | ROI + `--line` 5–33 ms；全屏 113–164 ms 未达标 |
| 画面未变时 <10 ms | 2–3 ms 达标 |
| 实时连续画面 | scrcpy / screenrecord 适配 + H.264 解码端到端测试通过；**真机待实测** |
| 实时流 + OCR 闭环 | `eye watch --scrcpy` 代码就绪，真机待实测 |

## 10. 真机接入 TODO

1. **安装 scrcpy**：`brew install scrcpy`，然后 `phone eye stream --scrcpy --seconds 5` 记录首帧延迟、解码帧率与 `stats`。
2. **实测坐标**：在真机设置页跑 `phone eye find 设置 --scrcpy --screen 1264x2800`，对比 `--adb` 单帧结果，确认 `--max-size` 缩放不引入偏移。
3. **压 1080p/30fps 稳定性**：`phone serve --scrcpy` 连续运行 30 分钟，观察 `GET /status` 的 `capture.stats`（frames/fps/restarts/error）。
4. **回退路径**：无 scrcpy 时用 `--screenrecord` 跑同一套命令，记录延迟差异，决定默认值。
5. **阶段一物理项**：8h 长稳、50 次拔插、供电/反灌、人工落点/断触，仍需你配合。

## 11. 风险

| 风险 | 对策 |
|---|---|
| scrcpy 需 USB 调试，部分 App 检测调试开关 | 仅自有设备/授权账号；控制通道关闭；用完关闭调试 |
| OpenCV 与 PyAV 各自带 FFmpeg，macOS 可能打印重复类警告 | 实测解码与 OCR 同时工作正常；如遇崩溃，先单独跑 `eye stream` 定位 |
| 缩图检测漏掉小字 | `--det-max 0` 回到原图；或用 `--roi` 放大局部 |
| 全屏 OCR 超 100 ms | 帧/文字块缓存、ROI、`--line`、实时流降分辨率 |
| scrcpy 版本差异（1.x 无 `--no-playback`） | 自动探测版本并回退 `--no-display` |
| `screenrecord` 有 180s 上限且延迟更高 | 自动重启；优先 scrcpy |
| 窗口截图带边框导致坐标偏移 | 强制全屏契约；不接窗口源 |

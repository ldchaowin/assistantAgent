# iOS / iPhone 调试支持（The Eye + The Hand）· 实验性

> ⏸ **阶段状态：暂停（2026-11-14）**。iPhone 兼容因 USB 角色冲突、系统私有采集通道与
> TCC/投屏环境等原因**暂不继续**，代码与文档作为实验性成果保留。决策、实测数据、已知坑与
> iOS 分支为**实验性、阶段性暂停**状态：截图/找文字可用，控制手方案未完成。
>
> 2026-11-14 · 真机验证：**iPhone 13 / iOS 26.7.3** · 固件 **0.4.0** ·
> Python **312** 测试 · 插件 **85** 测试

Android 走 `adb` + `scrcpy`；iPhone 没有 adb。本页记录「如果继续做，怎么做」：
iOS 的「眼」用 **macOS 原生 CoreMediaIO / AVFoundation 通道**（QuickTime 录制 iPhone
屏幕用的就是它），由仓库里的小 helper（`IOSEye`）取帧并通过 unix socket 交给 Python；
也可用投屏窗口（iPhone Mirroring / UxPlay）作眼（`--ios-mirror`）。
**不需要开发者模式、不需要 Xcode、不需要装 App**；「手」仍走 ESP32 绝对鼠标固件
或（可选）开发者模式下的 CoreDevice HID。

## 1. 状态

> 以下均为 **实验性** 成果：真机验证过的部分可直接用，未验证部分标注明确。暂停期间不再维护。

| 能力 | 状态 |
|---|---|
| iPhone 发现（usbmuxd，纯 Python） | ✅ 真机实测 |
| 原生取帧 helper（CoreMediaIO `iOSScreenCapture` + AVFoundation，无开发者模式） | ✅ **真机实测**：1170×2532，实时 ~38fps |
| `phone ios status / devices / wait / shot / stream` | ✅ 真机实测 |
| `phone eye ... --ios`（OCR / 找文字 / 坐标） | ✅ 真机实测（找「设置」276ms） |
| `phone serve --ios`（HTTP /screenshot、/find，DSH 工具可用） | ✅ 真机实测 |
| `phone doctor --ios` | ✅ 真机实测（13 pass / 2 warn / 0 fail） |
| 备选后端：pymobiledevice3（开发者模式）、idevicescreenshot | 已接入，未在本机验证（需开发者模式 / DDI） |
| 实时流 | ✅ 真机 ~38fps（helper 内 JPEG 编码；1170×2532 全分辨率） |
| **投屏窗口眼（方案 B）**：iPhone Mirroring / UxPlay 窗口采集 | ✅ 已实现（普通窗口验证 ~83ms/帧、~9fps；真实投屏窗口未验证） |
| 手（点按）：ESP32 绝对鼠标固件 + 辅助触控 | 固件已支持并编译通过，**iOS 真机落点未验证** |
| 手（点按）：CoreDevice `universal-hid`（开发者模式，USB 直控） | 工具能力已确认，未接线到本项目（见 §8） |

## 2. 为什么不能用 ffmpeg 直接取 iPhone 屏幕

macOS 上 iPhone 的屏幕**不是**普通 AVFoundation 摄像头：它由系统 DAL 插件
`/System/Library/Frameworks/CoreMediaIO.framework/.../iOSScreenCapture.plugin`
（配合 `com.apple.cmio.iOSScreenCaptureAssistant` 守护进程）提供，形态是
**muxed（混合）设备**，而且只有先把 CoreMediaIO 栈初始化（`AllowScreenCaptureDevices`）
之后，AVFoundation 才能枚举到它。实测：

- `ffmpeg -f avfoundation -list_devices` 只列 `Capture screen 0`，没有 iPhone；
- 直接 `AVCaptureDeviceDiscoverySession` 在未初始化 CMIO 时返回 0 台；
- 初始化 CMIO 后能拿到 `AVCaptureDeviceTypeExternal`、`mediaType muxed`、名字 `iPhone` 的设备。

所以本项目自带一个 **Objective-C helper** 直接用 AVFoundation 取帧（来源在
`tools/ios_eye/IOSEye.m`），不依赖 ffmpeg 的枚举。

## 3. 首次使用（一次性准备）

1. iPhone 用**数据线直连 Mac**，解锁并在「信任此电脑」弹窗点信任。
2. 让系统采集服务处于可用状态。任选其一：
   - 直接运行 `phone ios status` / `phone ios shot`（helper 会尝试唤醒）；或
   - 打开 **QuickTime Player → 文件 → 新建影片录制 → 点录制按钮旁的小箭头 → 选 iPhone**
     （这一步会拉起系统的 `iOSScreenCaptureAssistant`，之后 helper 就能用）。
3. 第一次启动 helper 时 macOS 会弹窗：

   > **“IOSEye” 想要访问相机。**

   点 **允许**（系统设置 → 隐私与安全性 → 相机 里也能改）。该授权按 App 签名记录，
   **重新编译/重新签名 helper 会重置**（见 §7）。

准备就绪后（本机实测）：

```bash
cd /path/to/assistantAgent
.venv/bin/phone ios status
# → ok=true, state=ready, capture_backend=native, serial=<UDID>
```

## 4. 常用命令

```bash
# 设备与状态
phone ios status                  # 设备 + 相机权限 + 原生取帧是否就绪
phone ios devices                 # usbmuxd 设备 + 备选工具列表
phone ios wait --timeout 120      # 等待 iPhone 可采集（含分阶段提示）

# 截图 / 实时流
phone ios shot --out shots/ios.png            # 原生截图（全分辨率 PNG）
phone ios stream --seconds 5 --save shots/ios-live.png
phone ios stream --seconds 5 --find 设置      # 在最新帧上 OCR 找字

# The Eye（OCR / 坐标）
phone eye find 设置 --ios
phone eye find 设置 --ios --debug shots/ios-ocr.png
phone eye ocr --ios

# 本地服务（DSH 的 phone_screenshot / phone_find_element 等直接用）
phone --tcp hand.local serve --ios --http-port 8765 --no-auth
# GET /health  → platform: ios
# GET /status  → platform/hand/capture；iOS 无手也能截图/OCR
# POST /screenshot, POST /find

# 体检
phone doctor --ios
```

`--ios` 也可以作为帧源用在 `phone run`（场景）和 `eye` 各子命令上，例如
`phone eye shot --out x.png --ios`。一次性截图的后端可用
`--ios --backend native|avfoundation|pymobiledevice3|idevicescreenshot` 强制指定，
默认按 `native → avfoundation → pymobiledevice3 → idevicescreenshot` 回退。

### 实测数字（iPhone 13, 1170×2532）

| 操作 | 结果 |
|---|---|
| `ios shot` | 1.6 s 端到端（含 helper 启动），PNG 1.4 MB |
| `ios stream --seconds 4` | 152 帧 / **37.97 fps**，首帧 428 ms，帧间隔 p50 17 ms、p95 49 ms |
| `eye find 设置 --ios` | OCR 276 ms，找到「设置」`device_center=[718,1903]`，frame=screen=1170×2532 |
| `serve --ios` POST /find | 找到「设置」score 0.9934，`screenshot_path` 落盘 |
| `doctor --ios` | 13 pass / 2 warn / 0 fail（warn：手是 touch 固件、默认校准） |

## 5. 取帧实现（helper）

```text
IOSEye.app（ad-hoc 签名，LaunchServices 启动以获得相机 TCC）
  ① CMIO 初始化：AllowScreenCaptureDevices = 1
  ② AVCaptureDeviceDiscoverySession(mediaType=.muxed) → “iPhone”
  ③ AVCaptureSession + AVCaptureVideoDataOutput(BGRA)
  ④ 每帧：CIImage → JPEG（可选缩放/质量/帧率限制）
  ⑤ 每个客户端一个写线程 + 有界队列（慢客户端丢旧帧，不阻塞采集）
  ⑥ unix socket 帧格式：[4B 大端长度][JPEG 数据]，状态写 JSON 文件
```

Python 侧（`console/phone_console/vision/ios.py`）：

- `IosNativeSource`：懒启动 helper、连接 socket、读线程解码、`grab/latest/wait_newer/stats/close`
  （与 Android 的 `H264StreamSource` 接口一致）；
- `capture_native_shot`：一次性模式的 helper；
- `ios_eye_probe`：`phone ios status` / `doctor` 用；
- helper 交付方式：仓库 `tools/ios_eye/IOSEye.m` + `Info.plist`，首次使用时用 `clang`
  自动编译到 `.tools/ios-eye/IOSEye.app`（已被 `.gitignore` 忽略），也可手动：

```bash
phone ios build             # 缺失时编译
phone ios build --force     # 强制重编（会重置相机授权，需要重新点一次「允许」）
make ios-eye-build
```

LaunchServices 启动时 `open --args` 传参不可靠，因此 Python 把启动配置写到
`/tmp/phone-ios-eye/config.json`，helper 启动时读取并删除。

## 6. 方案 B：ESP32 绝对鼠标 + 辅助触控 + 投屏眼

iPhone 的 USB 口同一时间只能当一种角色：接 Mac（眼）或接 ESP32（手）。方案 B 把 USB 让给
ESP32，屏幕则通过 macOS 投屏窗口（**iPhone Mirroring** 系统自带，或 **UxPlay**）无线取回，
由本项目的投屏窗口眼（`--ios-mirror`）采集。

### 6.1 眼：投屏窗口采集（已实现）

```bash
# 依赖（已装入本项目 .venv）：PyObjC Quartz
.venv/bin/pip install -e 'console[mirror]'

# 列出窗口 / 找到投屏窗口（iPhone Mirroring 或 UxPlay）
phone ios mirror list
# {"ok":true,"candidates":[{"owner":"iPhone镜像","title":"...","width":...}],"selected":{...}}

# 截一帧 / 采集统计 / 保存最后一帧
phone ios mirror shot --out shots/mirror.png
phone ios mirror stream --seconds 5 --save shots/mirror-live.png

# 把投屏窗口作为普通眼源使用
phone eye find 设置 --ios-mirror
phone --ios-mirror serve --http-port 8765 --no-auth
```

实现：`console/phone_console/vision/mirror.py`。用 `screencapture -l <windowID>` 定时抓窗口
（~83ms/帧、约 9fps，继承 DSH 的「屏幕录制」权限，不需要新 App / 新授权）。窗口可以按
owner/title 自动识别（`iPhone镜像` / `iPhone Mirroring` / `UxPlay`），也可以用 `--window ID`
显式指定。窗口关闭后会自动重找。

> ⚠️ 已知：`screencapture -l` 会连同**窗口标题栏**一起截入。真实 iPhone Mirroring 窗口实测
> 标题栏约 **78px（@2x）**；用于手眼坐标映射前需要先裁剪（或校准掉这段偏移）。当前
> `--ios-mirror` 尚未内置自动裁剪，属后续待办。

### 6.2 手：刷绝对鼠标固件

```bash
# 1) 刷 abs 固件（固件 0.4.0，已编译；NVS 里的 WiFi/校准会保留）
#    推荐用板子的 UART 口接 Mac（/dev/cu.usbserial-XXXX）：
make flash-abs PORT=/dev/cu.usbserial-XXXX
#    或用原生 USB 口进入下载模式（按住 BOOT、点 RST、松开 BOOT，Mac 出现 /dev/cu.usbmodem*）：
make flash-abs PORT=/dev/cu.usbmodemXXXX

# 2) 刷完把 ESP32 的 USB 口插到 iPhone（控制走 WiFi TCP，不需要 Mac）
phone --tcp hand.local hello        # 应看到 "hid":"absmouse"
# iPhone：设置 → 辅助功能 → 触控 → 辅助触控 → 打开（出现指针）

# 3) 按投屏窗口尺寸对齐手的绝对坐标（自动从 mirror 窗口取尺寸）
phone --tcp hand.local ios hand --mirror --save
#    或手动指定：ios hand --screen 804x1748 --save

# 4) 点按 / 手势
phone --tcp hand.local ios tap 590 1278
phone --tcp hand.local ios home / back / app-switch
```

> 风险：iOS 对「绝对坐标鼠标」的支持未实测。如果辅助触控指针不出现或落点明显错乱，
> 回退方案是（a）开发者模式 + CoreDevice `universal-hid`（一根 USB 同时看+点，见 §8），
> 或（b）给固件加相对鼠标移动 + 边缘校准。

### 6.3 投屏连接步骤（iPhone Mirroring）

1. Mac 与 iPhone 登录**同一个 Apple ID**，两边都打开 Wi-Fi + 蓝牙，iPhone 在 Mac 附近。
2. 打开「iPhone Mirroring」，解锁 iPhone，按提示连接（首次需要点「继续」）。
3. 连接后**保持投屏窗口打开、不要最小化**；`phone ios mirror list` 能看到窗口即可。
4. （替代）安装 UxPlay：`brew install uxplay`，运行 `uxplay` 后 iPhone 控制中心 →
   屏幕镜像 → 选择 UxPlay；窗口 owner 为 `uxplay`，本项目同样能识别。

### 6.4 方案对比

| 方案 | 眼 | 手 | 状态 |
|---|---|---|---|
| A | iPhone↔Mac USB（原生 helper，~38fps） | 不接 | ✅ 已真机跑通（看屏/OCR/找元素） |
| **B** | 投屏窗口（iPhone Mirroring / UxPlay，~9fps） | ESP32↔iPhone USB（绝对鼠标+辅助触控） | **眼已实现验证；手待实测** |
| C | USB 原生 helper | 开发者模式 CoreDevice `universal-hid` | 工具能力已确认，未接线 |

## 7. 排障

| 现象 | 原因 / 处理 |
|---|---|
| `ios-native: camera permission not granted (status=0/2)` | 首次弹出「“IOSEye”想要访问相机」要允许；或到 系统设置 → 隐私与安全性 → 相机 打开 IOSEye。重编译 helper 后需要重新允许一次 |
| `iPhone screen not found` | 数据线仅充电/未解锁/未信任；或系统采集服务没起来：打开 QuickTime → 新建影片录制 → 选一次 iPhone 再重试 |
| `ios:still:native` 之外报 Avfoundation 找不到设备 | 正常：ffmpeg 枚举不到 iPhone，请用 native 后端（默认就是） |
| `helper 启动失败` / 取不到画面 | 检查 `pgrep -f IOSEye.app` 是否有残留实例（程序会自动清理）；`phone ios status` 看原生状态；必要时 `phone ios build --force` + 重新允许相机 |
| 状态 `screen` 仍是 1264×2800 | 首次截图/取帧前服务还没拿到 iPhone 尺寸；截图一次后即为 1170×2532（手是 Android 尺寸时也会这样） |
| 重新签名后一切取帧失败 | 相机 TCC 与签名绑定：重新点一次「允许」即可；期间不修改 helper 就不会再重置 |
| `pymobiledevice3` / `idevicescreenshot` 失败 | 这两个是可选回退：前者需要开发者模式（iOS 17+ 免 DDI），后者需要挂载 developer disk image；日常用 native 即可 |

## 8. 后续

- 方案 B 真机联动验证（ESP32 abs 固件 + 辅助触控 + 投屏眼）
- 开发者模式 + CoreDevice `universal-hid-service`（`tap/drag/type/key`，坐标 0–65535）
  接入成 `--ios-input coredevice`，实现「一根 USB 看 + 点」，不依赖 ESP32。
- iOS 27+ 的 CoreDevice 屏幕流（`screen-capture` / RTP）可替换轮询式 helper。
- ESP32 绝对鼠标在 iOS 上的落点/边缘死区实测与校准（需把手机 USB 让给手）。
- 多台 iPhone 选择、helper 常驻后台（launchd）与断线重连。

## 9. 代码位置

| 文件 | 作用 |
|---|---|
| `tools/ios_eye/IOSEye.m`, `Info.plist` | 原生取帧 helper（CMIO + AVFoundation + unix socket） |
| `console/phone_console/vision/ios.py` | usbmuxd 发现、helper 构建/启动、`IosNativeSource`、截图回退、状态/提示 |
| `console/phone_console/vision/mirror.py` | 投屏窗口（iPhone Mirroring / UxPlay）发现与截图源 `MirrorWindowSource` |
| `console/phone_console/ios.py` | `IosPhone`（屏幕取原生尺寸、指针复位）、iOS 手势、手配置 |
| `console/phone_console/cli.py` | `phone ios` 子命令、`--ios` 眼/服务/场景、`doctor --ios` |
| `console/phone_console/service.py` | `platform`/`hand` 字段、iOS 眼-only 容错 |
| `console/phone_console/doctor.py` | `ios-tools / ios-device / ios-native / ios-capture` 检查 |
| `console/tests/test_ios.py` | iOS 单测（假 usbmuxd、假 helper、真实 helper 合成流端到端） |
| `firmware/esp32s3-hand/src/` | 绝对鼠标固件 + `move` 命令（0.4.0） |

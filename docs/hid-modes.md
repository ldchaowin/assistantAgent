# HID 模式：触摸屏 / 绝对鼠标

> 2026-11-10 · 真机：HONOR PTP-AN00 · Android 16（SDK 36）· 1264×2800

## 1. 结论

这台 Android 16 手机**不注册绝对鼠标**：

- ESP32-S3 作为 USB HID 复合设备枚举成功（`status.usb=true`）。
- Android 能看到 `Espressif ... Mouse` 的 `/dev/input/event9`（有 `ABS_X/ABS_Y`、`BTN_MOUSE`）。
- 但 `dumpsys input` 里没有对应的 `CURSOR` 输入设备，也没有指针控制器；按键事件到达内核，系统却不产生点击。

对照实验：

| 模式 | Android 结果 |
|---|---|
| 绝对鼠标（`USBHIDAbsoluteMouse`） | 枚举，但无 cursor、无点击 |
| 相对鼠标（`USBHIDRelativeMouse`） | 注册为 `CURSOR`，指针出现，可点击 |
| **单点触摸屏（Digitizer）** | 注册为 `TOUCHSCREEN`，绝对坐标点击生效 |
| 键盘 | 正常，可输入文本 |
| Consumer Control HOME/BACK | 正常，可回桌面/返回 |

因此默认固件改为**触摸屏 + 键盘 + Consumer Control**；绝对鼠标模式保留为编译选项，
**iOS 必须使用绝对鼠标模式**（Android 不注册绝对鼠标，iOS 不注册单点触摸屏）。
固件 0.4.0 增加 `move` 命令做指针复位，`phone ios` 命令族已接入，详见 [iOS 调试支持](ios.md)。

## 2. 默认模式：touch

```bash
make build          # 默认 esp32s3-hand：触摸屏模式
make flash PORT=/dev/cu.usbserial-XXXX
```

`phone hello` 会返回：

```json
{"evt":"ready","fw":"0.2.0","dev":"hand-A1B2","screen":[1264,2800],"cal":[0,32767,0,32767],"hid":"touch","wifi":{"state":"off","ssid":"","ip":"","rssi":0,"port":9000}}
```

HID 组成：

- 键盘：`USBHIDKeyboard`（report ID 1）
- 触摸屏：自定义单点 Digitizer（report ID 6，`ABS_X/Y` 0–32767）
- Consumer Control：HOME / BACK（report ID 3）
- 不再注册鼠标；`tap`/`swipe` 映射为触摸按下/移动/抬起

## 3. 绝对鼠标模式

```bash
make build-abs
make flash-abs PORT=/dev/cu.usbserial-XXXX
```

`hello` 返回 `"hid":"absmouse"`。适合注册绝对鼠标的主机；Android 16 目标机不要用这个模式。

## 4. 验证记录

- `phone --port ... tap 336 1199` → 打开「使用配对码配对设备」弹窗（坐标精确）。
- `phone key BACK` → 关闭弹窗；`phone key HOME` → 回桌面。
- `phone text wifi` → 设置搜索框出现 `wifi`。
- `phone accept --full`：
  - T4 5/5、T5 25/25、T6 0 失败、T7 0 失败、T8 pass、T9 pass、T10 1000/1000
  - p95 **40.94 ms**（目标 ≤50ms，达标）
  - 9 pass / 0 fail / 10 manual / 1 skip

## 5. 注意

- 触摸屏模式没有鼠标指针；`pointer visible` 这类人工项应改为“触摸是否生效”。
- `cal` 仍作用于 HID 0–32767；触摸屏把 0–32767 映射到整屏，默认 cal 通常已准确。
- Android 的触摸/手势可能与系统边缘手势冲突；校准点默认 5% 内缩。
- Consumer Control 的 HOME/BACK 映射依赖 Android 的 HID consumer 支持；已在目标机验证。
- 绝对鼠标模式在 iOS 上**待真机验证**（固件与命令已就绪）。

## 6. iOS：绝对鼠标 + 辅助触控

```bash
make build-abs                       # HAND_HID_TOUCHSCREEN=0 → USBHIDAbsoluteMouse
make flash-abs PORT=/dev/cu.usbserial-XXXX
```

- iPhone：设置 → 辅助功能 → 触控 → 辅助触控 → 打开；ESP32 用 USB 接 iPhone 后应出现指针。
- `hello` 应返回 `"hid":"absmouse"`。当前 Android 手是 `hid=touch`，`phone doctor --ios`
  会给出 `build-abs` 提示。
- 指针复位：固件 `move` 命令（0.4.0+）把指针送到 (0,0)，`phone ios` 会话首个动作前自动执行，
  规避 iOS 边缘死区；`--no-warmup` 可关闭。
- 屏幕尺寸用 `phone --tcp <hand> ios hand --save` 从 iPhone 采集帧自动写入；
  落点有整体偏移时用 `--margin 0.02` 内缩或按 [坐标校准](calibration.md) 校准。
- iOS 无 HOME/BACK 键：用 `phone ios home / back / app-switch` 的边缘手势。

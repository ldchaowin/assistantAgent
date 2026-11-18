# 烧录 ESP32-S3 固件

本文是 **手（ESP32-S3 HID）** 的构建/烧录指南。眼睛（截图/OCR）不需要烧录，见
[`phase2-eye.md`](phase2-eye.md)。

## 0. 需要什么

**硬件**

- ESP32-S3 开发板，建议 **N16R8**（16 MB Flash + 8 MB OPI PSRAM）
- 优先选**双 Type-C** 板：一个口烧录（UART），一个口接手机（原生 USB）
- 手机（Android；iOS 为实验性，见 [`ios.md`](ios.md)）、能传数据的数据线
- 单口板额外需要 USB-TTL 三线（TX/RX/GND）

**软件**

- Python ≥ 3.10 + `pyserial`（上位机 CLI）
- PlatformIO（构建/烧录）。可以装进本仓库的 `.venv`，也可以全局安装

## 1. 接线

| 连接 | 一端 | 另一端 |
|---|---|---|
| 烧录/控制 | PC USB | 板载 **UART** 口（CH340/CP210x） |
| 执行 | 板载**原生 USB** 口 | 手机 |
| 长稳供电 | PD 充电器 | PD + OTG 转接器 |

- 烧录口是 UART 口，**不是**接手机的原生 USB 口
- 单口板：USB-TTL 接 UART0 的 TX/RX/GND（以丝印为准，常见 GPIO43/44），**不要接 5V**
- 双口同时插时先确认 VBUS 不会反灌；没有电源路径保护就只从一个口取电
- 只充电的线会导致「有灯无端口」或「有串口无 HID」

## 2. 工具链

```bash
# 仓库根目录
python3 -m venv .venv
.venv/bin/pip install platformio pyserial
```

默认构建把 PlatformIO 的缓存固定到仓库内 `.pio-core/`（首次构建会下载工具链，约 GB 级）。
不想占用仓库空间可以全局安装 PlatformIO 并跳过 `PLATFORMIO_CORE_DIR`。

## 3. 构建

```bash
# 触摸屏 HID（Android 默认）
make build

# 绝对鼠标 HID（只有支持它的宿主才用，例如 iOS）
make build-abs
```

等价的原生命令：

```bash
PLATFORMIO_CORE_DIR=$PWD/.pio-core \
  .venv/bin/python -m platformio run -d firmware/esp32s3-hand
```

成功标志：末尾 `SUCCESS`，产物在
`firmware/esp32s3-hand/.pio/build/esp32s3-hand/firmware.bin`。

两个环境（`firmware/esp32s3-hand/platformio.ini`）：

| env | 模式 | 适用 |
|---|---|---|
| `esp32s3-hand` | 触摸屏 HID | Android（默认） |
| `esp32s3-hand-abs` | 绝对鼠标 HID | 支持绝对鼠标的宿主（iOS 实验） |

## 4. 烧录

```bash
# macOS: /dev/cu.usbserial-XXXX   Linux: /dev/ttyUSB0 或 /dev/ttyACM0
make flash PORT=/dev/cu.usbserial-XXXX

# 绝对鼠标固件
make flash-abs PORT=/dev/cu.usbserial-XXXX
```

等价的 pio 命令：

```bash
PLATFORMIO_CORE_DIR=$PWD/.pio-core \
  .venv/bin/python -m platformio run -d firmware/esp32s3-hand \
  -t upload --upload-port /dev/cu.usbserial-XXXX
```

- 端口名：`phone scan` 或 `.venv/bin/python -m platformio device list`
- 找不到端口：装 CH340/CP210x 驱动；**按住 BOOT → 点一下 RST → 松开 BOOT** 再烧
- 单口板 + USB-TTL：TX/RX 交叉接 UART0，GND 共地，不接 5V；烧录时先拔手机
- 烧录失败：把 `platformio.ini` 的 `upload_speed` 从 `921600` 降到 `460800`

串口日志：

```bash
make monitor PORT=/dev/cu.usbserial-XXXX     # 460800 波特率
```

## 5. 烧录后验证

1. 手机接**原生 USB** 口 → Android 应出现鼠标指针
2. PC 接 **UART** 口，确认端口并握手：

```bash
phone scan
phone --port /dev/cu.usbserial-XXXX hello
phone --port /dev/cu.usbserial-XXXX status
```

期望 `evt=ready`、`state=READY`，并返回屏幕尺寸与校准值。

3. 点一下屏幕中心，确认指针落点：

```bash
phone --port /dev/cu.usbserial-XXXX tap 540 1200
```

4. 第一次使用先做 [触摸校准](calibration.md)
5. 想拔掉 UART 用无线控制：见 [`wifi-tcp.md`](wifi-tcp.md)（BLE 配网 + TCP 9000）

## 6. 排错

| 现象 | 排查 |
|---|---|
| 找不到串口 | 装驱动；BOOT+RST 进入下载模式；换数据线 |
| 有串口但 `hello` 无响应 | 确认 460800 波特率、接的是 UART 口、固件已烧 |
| 手机没有指针 | USB Mode 是否为 TinyUSB；线是否支持数据；手机 OTG 是否开启 |
| 指针漂移/瞬移 | 先校准；若宿主不支持绝对 HID，用 `make build` 的触摸屏模式 |
| 烧录失败 | BOOT+RST；`upload_speed` 降速；换线/换口 |
| 反复重启 | 供电不足：用 PD 供电，避免两个口互相反灌 |

## 7. 安全

- 上电顺序不敏感，但不要双口同时反灌；长稳必须用 PD 分流器
- `stop` 在滑动过程中可用；BOOT 长按约 2 秒是空闲态急停
- 连接后 30 秒无请求会进入 `FAULT`，发 `hello` 恢复
- 任何超时/断线/复位都会先释放按键，再报错

## 8. Arduino IDE（备选）

PlatformIO 是主构建方式；坚持用 Arduino IDE 时：

- Board: `ESP32S3 Dev Module`
- Flash Size: `16MB`
- PSRAM: `OPI PSRAM`
- Partition Scheme: `Huge APP (3MB No OTA/1MB SPIFFS)`
- USB Mode: `USB-OTG (TinyUSB)`
- USB CDC On Boot: `Disabled`
- Upload Speed: `921600`

源码是 PlatformIO 布局：在 Arduino IDE 新建同名 sketch，把 `src/*.h`、`src/*.cpp`
拷进去，保留一个空的 `.ino` 入口即可。

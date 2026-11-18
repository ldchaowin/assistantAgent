# WiFi 控制通道（STA + TCP + BLE 一键配网）

> 2026-11-12 · 固件 0.3.2 · 协议 v0.2
>
> 目标：让 ESP32 摆脱 UART 线，接入局域网后用同一套 JSON Lines 协议被 `phone` / `phone serve` 控制。
> SoftAP 配网页是第二步，尚未实现。

## 1. 链路

```text
DSH 插件 ──HTTP──> phone serve（Mac）──┐
                                      ├── WiFi TCP :9000 ──> ESP32 ──USB HID──> 手机
phone CLI ──TCP──────────────────────┘        │
                                              └── UART0 460800（始终保留，fallback）
```

- UART0 和 TCP 共用同一个命令队列、同一个状态机；
- 响应回到请求来源：串口来的回串口，TCP 来的回那个客户端；
- 同一时刻只服务一个 TCP 客户端；
- 手机控制仍走 USB HID，WiFi 只替换“Mac ↔ ESP32”这一段。

## 2. BLE 一键配网（推荐，产品路径）

固件 0.3.0 集成 Espressif **Network Provisioner 1.0.2**：未配网时开机自动广播
`PROV_XXXX`（设备 ID 后 4 位），用官方 `esp_prov` 客户端从 Mac 直接下发 WiFi 凭证。

```bash
# 1) 扫描配网设备（Mac 自带蓝牙）
.venv/bin/phone wifi devices
# {"ok":true,"devices":[{"name":"PROV_A1B2","address":"…","rssi":-29}]}

# 2) 一键配网（PoP 默认 hand-XXXX）
.venv/bin/phone wifi provision --ssid "家里WiFi" --password "wifi密码"

# 3) 成功后设备自动连 WiFi，走 TCP
.venv/bin/phone --tcp 192.168.1.20 hello
```

真机实测（HONOR 环境，`YOUR_WIFI`）：BLE 会话建立 → 下发凭证 → `ApplyConfig` →
`WiFi state: Connected` → `Provisioning was successful`，**17.9 秒**；随后
`phone --tcp 192.168.1.20 status` 返回 `wifi.connected / usb:true / prov.idle`，mDNS
`hand.local` 可用。

0.3.2 起「重新配网」走**重启进入干净配网**：`进入配网` 写 `force_prov` 标志并重启，
旧凭证保留；配网窗口 30 分钟。实测 0.3.2 配网 **18.2 秒 / 一次通过**；`reboot` 后
自动连回 `YOUR_WIFI`（凭证持久化）。

安全与行为：

- **Security 1 + PoP**，PoP 为完整设备 ID（如 `hand-A1B2`，可印在标签上）；
- **连接成功才落盘**：密码错误不会覆盖原配置；
- 已保存网络连不上（3 次重试后）自动进入 BLE 配网；
- 配网窗口 10 分钟，超时停止广播；`wifi clear` 后立即重新进入配网；
- 设备开机自动广播配网（`wifi_configured=false` 时）。

协议新增：

```json
{"evt":"prov","prov":{"state":"started","name":"PROV_A1B2"}}
{"id":1,"cmd":"wifi","op":"prov"}          // 手动重新广播
```

`hello/status` 里增加 `prov` 对象（`state` / `name` / `err`）。

## 3. 插件「设备接入」向导

服务端新增配网 API（Bearer 鉴权）：

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/provision/devices?timeout=8` | BLE 扫描 `PROV_*`，返回名称/地址/RSSI |
| POST | `/provision` | `{ssid,password,device?,pop?,timeout?}`，后台任务开始配网 |
| POST | `/provision/arm` | 让当前已连接的设备重新进入 BLE 配网窗口（回包可能因 WiFi 重启丢失，返回 `warning` 属正常） |
| GET | `/provision/status` | `{state, ssid, device, running, elapsed_s, result, error, wifi?}` |

- 后台线程执行，同一时间只允许一个任务（第二个返回 `409 BUSY`）；
- 成功后 `status` 额外带 `wifi`（SSID/IP/RSSI），供 UI 显示；
- 密码只在内存中传给客户端，不出现在状态/日志里。

DSH 插件 0.4.0 在「手机任务」页新增「📶 设备接入」卡片：

```text
[扫描] → 选择 PROV_A1B2 → 输入 WiFi 名称/密码 → [开始配网]
状态：配网中… → 配网成功（显示 已连接 SSID · IP）
```

设备已经连上 WiFi、想换网时：点「进入配网」→ 设备重新广播 `PROV_XXXX` →
再走一遍扫描/配网（期间会短暂断网，成功后自动连回）。

Host 侧通过 `/dsh-phone-task-v3` 的 `phone.provision.devices/start/status` 代理到本地服务；
浏览器端不直接访问 8765，仍然同源、带 DSH 会话。

> 修改插件后需要在插件管理器里重新启用 `dsh-plugin-phone` 并硬刷新 Web GUI。

## 4. 串口配网（备用/开发）

```bash
# 1) 确认串口可用（固件 0.2.0 起 hello 里带 wifi 状态）
.venv/bin/phone --port /dev/cu.usbserial-XXXX hello
# {"evt":"ready","fw":"0.2.0",...,"wifi":{"state":"off",...}}

# 2) 写入 WiFi 凭证（会存 NVS 并立即连接）
.venv/bin/phone --port /dev/cu.usbserial-XXXX wifi set "家里WiFi" "wifi密码"

# 看到 connected 即成功：
# {"ok":true,"ssid":"家里WiFi","wifi":{"state":"connected","ip":"192.168.1.20",...}}
```

约束：ESP32-S3 **只支持 2.4GHz**；WPA2/WPA3 个人版；SSID 1–32 字节，密码 0（开放网络）或 8–63 字节。

## 5. 之后用 TCP

```bash
# 直连：host 或 host:port（默认 9000）
.venv/bin/phone --tcp 192.168.1.20 hello
.venv/bin/phone --tcp 192.168.1.20 status
.venv/bin/phone --tcp 192.168.1.20 tap 540 960
.venv/bin/phone --tcp 192.168.1.20 wifi status

# mDNS 主机名（固件注册 _phone-hand._tcp）
.venv/bin/phone --tcp hand.local hello

# 本地服务改用 TCP 通道（DSH 插件配置完全不变）
.venv/bin/phone --tcp 192.168.1.20 serve --scrcpy --no-auth
```

常用状态：

```bash
.venv/bin/phone --tcp 192.168.1.20 wifi status
# {"ok":true,"wifi":{"state":"connected","ssid":"家里WiFi","ip":"192.168.1.20","rssi":-52,"port":9000}}

.venv/bin/phone --port /dev/cu.usbserial-XXXX wifi clear   # 清凭证，回到纯串口
```

## 6. 固件行为

- 开机读 NVS：有 SSID → `connecting` → 连上后启动 TCP server 与 mDNS；没有 → `off`，只走串口。
- 连接超时 20s，最多重试 3 次 → `failed`（`err=timeout`）。
- 已连接后断开：自动回到 `connecting` 并 `WiFi.reconnect()`。
- 状态变化推送 `evt=wifi`（串口 + 当前 TCP 客户端）。
- 30s 无任何命令 → 与串口一致：释放按键、进入 `FAULT`；下一条命令前先 `hello`。
- TCP 客户端 15 分钟无数据自动断开；新客户端会**立即接管**旧连接（不等待超时）。
- 0.3.1：WiFi 重连时重建监听；配网失败/超时后用已存凭证重连 STA。

## 7. 排错

| 现象 | 可能原因 | 处理 |
|---|---|---|
| `wifi set` 后一直 `connecting` 然后 `failed` | 5GHz SSID / 密码错 / 信号弱 / 企业认证 | 确认 2.4GHz 个人版；靠近路由器重试 |
| `state=connected` 但 `--tcp` 连不上 | Mac 与 ESP32 不同网段；防火墙 | `ping IP`；确认同一子网、9000 端口放行 |
| `--tcp` 报 `cannot connect` | ESP32 未联网/IP 变了 | 串口 `wifi status` 看当前 IP；路由器绑静态租约 |
| TCP 中途断开 | ESP32 重启/WiFi 漫游/空闲超时 | 重连即可；`phone serve` 会在下次写失败时自动重连 |
| ping 通但 9000 连不上/无响应 | 0.3.0 在 WiFi 重启后没有重绑监听 | 升级 0.3.1（0.3.1 在重连时重建监听，并允许新客户端接管旧连接） |
| 串口正常、TCP 不通 | WiFi 连上但 server 未起 | 升级到 0.2.0；看串口是否打印 connected 事件 |

## 8. 性能（真机实测）

环境：ESP32-S3 + 固件 0.2.0，接入 `YOUR_WIFI`（2.4GHz，RSSI -58 ~ -61），Mac 有线/同一 LAN。

| 通道 | p50 | p95 | 说明 |
|---|---|---|---|
| ICMP ping | 4.8 ms | 6.5 ms | 链路基线 |
| UART ping | 7.3 ms | 7.4 ms | 460800，基线实现 |
| **WiFi TCP ping** | **12.9 ms** | **19.3 ms** | 优化后（优化前 36.6 / 41.8 ms） |

优化点：`serializeJson(doc, client)` 原来逐字符写，每个字节一个 TCP 段；改为先序列化到 512 字节缓冲再一次发送后，RTT 降了约 2/3。控制动作本身仍由 HID 耗时主导（tap ~40ms），TCP 只多约 5–12ms。

> 串口注意：ESP32 开发板的自动复位电路（DTR/RTS）会在**打开串口时复位芯片**。配网时这是正常的；配好之后日常操作请用 `--tcp`，不要反复开串口（否则手会重启、WiFi 短暂断开）。

### 真机无线闭环

- `phone --tcp 192.168.1.20 run scenarios/real-wlan-demo.json --scrcpy` → **PASS**，tap WLAN + assert 网络加速，OCR 152.8/155.1 ms，总 4.62 s；
- `phone --tcp 192.168.1.20 serve --scrcpy` → `/find` 冷 2.49 s / 热 0.49 s（OCR 0 ms 命中缓存）；`/tap` 打开目标页；
- 10 轮 `swipe + find`：10/10，swipe 386–446 ms，find 178–247 ms；流 24.8 fps、0 重启。

## 9. 安全（当前限制）

- TCP 通道目前**没有应用层鉴权**：能访问 9000 端口的同一局域网设备都能控制 HID。
- BLE 配网使用 PoP 认证；PoP 即设备 ID，知道 ID 且距离足够近才能配网（BLE 约 10–30m）。
- 建议只在可信内网使用；`phone serve` 仍只绑 `127.0.0.1` 并要求 Bearer token，DSH 插件不直接暴露 ESP32。
- 变更/清除 WiFi 凭证请走**串口**：`wifi set/clear` 会重建网络栈并断开当前 TCP 连接，响应可能送不回去（凭证仍会保存）。
- 下一步（第二步 SoftAP 同期）加：NVS 保存的 TCP token，请求带 `tok`，不匹配返回 `UNAUTHORIZED`；AP 配网页也用它。
- 密码在 NVS 中明文保存；高要求场景可开 NVS 加密或使用 IoT 专用 SSID。

## 10. 后续：SoftAP 兜底（可选）

- 无凭证开机 / `wifi ap` 时开 `PhoneHand-XXXX` 热点 + `192.168.4.1` 配置页；
- 页面扫描 2.4G 网络、填 SSID/密码、显示连接结果；
- 3 分钟无操作关闭热点；与串口配网共用同一组 NVS 键；
- 同期加 TCP token 鉴权。

## 11. 实现位置

| 文件 | 作用 |
|---|---|
| `firmware/esp32s3-hand/src/net.h/.cpp` | STA 状态机、TCP server、mDNS、客户端行解析、`evt=wifi` |
| `firmware/esp32s3-hand/src/protocol.h/.cpp` | 输出通道（UART/TCP）、`wifi` 对象、`wifi_event` |
| `firmware/esp32s3-hand/src/storage.cpp` | NVS 键 `ssid` / `pass` / `port` |
| `firmware/esp32s3-hand/src/main.cpp` | `wifi` 命令、行队列 channel、net_loop、estop 广播 |
| `console/phone_console/transport.py` | `TcpTransport`、`parse_tcp_endpoint` |
| `console/phone_console/device.py` | 断线重连、`wait_event`、`wifi_*` |
| `console/phone_console/cli.py` | `--tcp`、`phone wifi set/status/clear/prov/devices/provision` |
| `firmware/esp32s3-hand/src/prov.h/.cpp` | Network Provisioner BLE、PoP、状态机、自动恢复 |
| `console/phone_console/provisioning/` | 官方 `esp_prov` vendoring + Mac 封装 |

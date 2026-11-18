# 协议 v0.3（固件 0.4.0）

控制面：**UART0**（460800 8N1）或 **WiFi TCP**（默认 9000 端口），两条通道使用完全相同的 JSON Lines 帧格式；响应回到请求来源。执行面：原生 USB HID，与协议无关。

## 1. 帧格式

一行一个 JSON 对象，UTF-8，`\n` 结尾，单行 ≤512 字节。未知字段忽略。

- 请求必须带 `id`（整数 >0）和 `cmd`。
- 响应按 `id` 关联；`evt` 是异步事件，不带 `id`。
- 动作完成后才回 ACK：tap 约 40–60ms，swipe 为 `ms + 约 70ms`。
- 上位机默认超时 1s；swipe 用 `ms/1000 + 1s`。

## 2. 命令

| 命令 | 参数 | 成功响应 | 说明 |
|---|---|---|---|
| `hello` | — | `evt=ready` | 握手，返回固件/屏幕/校准/HID 模式/WiFi 状态 |
| `ping` | `ts` | `evt=pong` | 心跳，原样返回 ts |
| `cfg` | `screen{w,h}`、`cal{x0,x1,y0,y1}` | `ok` | 只改 RAM，不落盘 |
| `save` | — | `ok` | 写入 NVS |
| `tap` | `x,y,hold_ms?` | `ok` | 绝对点击，hold 默认 20ms |
| `move` | `x,y` | `ok` | 只移动绝对指针、不点击（固件 0.4.0+；iOS 指针复位用） |
| `swipe` | `x1,y1,x2,y2,ms` | `ok` | 平滑滑动，50–5000ms |
| `key` | `code` | `ok` | 按键或 Android 鼠标映射 |
| `text` | `s` | `ok` | ASCII 1–40 字符 |
| `status` | — | `ok` | 状态查询，含 `usb` 是否被主机枚举、`wifi` 状态 |
| `wifi` | `op=set/status/clear` | `ok` | WiFi STA 凭证与连接状态，见下节 |
| `stop` | — | `ok` | 立即释放所有按键 |

### cfg 示例

```json
{"id":2,"cmd":"cfg","screen":{"w":1080,"h":2400},"cal":{"x0":0,"x1":32767,"y0":0,"y1":32767}}
```

约束：`100 ≤ w,h ≤ 5000`；`0 ≤ cal ≤ 32767`；`x0 ≠ x1`、`y0 ≠ y1`。

### wifi 命令

```json
{"id":9,"cmd":"wifi","op":"set","ssid":"Office","pass":"secret123","port":9000}
{"id":10,"cmd":"wifi","op":"status"}
{"id":11,"cmd":"wifi","op":"clear"}
```

- `set`：校验 SSID（1–32 字节）、密码（0 或 8–63 字节）、端口（1–65535），写 NVS 并立即尝试连接；成功回 `ok`，连接结果通过 `evt=wifi` 异步上报。
- `status`：回 `{"id":N,"ok":true,"wifi":{...}}`。
- `clear`：清空凭证、关闭 WiFi，回 `ok` 并推送 `evt=wifi state=off`。
- 错误：`BAD_VALUE` + `msg` 为 `ssid` / `pass` / `port` / `op`；保存失败 `NVS_ERR`。

`wifi` 对象：

```json
{"state":"connected","ssid":"Office","ip":"192.168.1.20","rssi":-52,"port":9000}
```

`state`：`off` / `connecting` / `connected` / `failed`；失败时可能带 `err`（如 `timeout`）。

异步事件：

```json
{"evt":"wifi","wifi":{"state":"connecting","ssid":"Office","ip":"","rssi":0,"port":9000}}
{"evt":"wifi","wifi":{"state":"connected","ssid":"Office","ip":"192.168.1.20","rssi":-52,"port":9000}}
```

### key 代码

- `HOME` / `BACK`：Android 上分别用鼠标中键 / 右键实现；iOS 没有这两个 HID 消费者键，
  请用边缘手势（`phone ios home / back / app-switch`）。
- `ENTER`、`ESC`、`TAB`、`SPACE`、`BACKSPACE`、`DELETE`、`UP`、`DOWN`、`LEFT`、`RIGHT`、`MENU`。
- 单个可打印 ASCII 字符（`A`–`Z`、`0`–`9`、符号）。

### text

只接受 ASCII 0x20–0x7E，长度 1–40。非 ASCII 返回 `BAD_VALUE`。

## 3. 响应

```json
{"id":7,"ok":true,"t":42}
{"id":8,"ok":false,"err":"OUT_OF_RANGE","msg":"x/y"}
{"evt":"ready","fw":"0.4.0","dev":"hand-1A2B","screen":[1264,2800],"cal":[0,32767,0,32767],"hid":"touch","wifi":{"state":"connected","ssid":"Office","ip":"192.168.1.20","rssi":-52,"port":9000}}
{"evt":"pong","ts":1731000000123}
{"evt":"estop"}
```

`t` 是固件侧动作耗时，不含串口传输。

### move（固件 0.4.0+）

```json
{"id":6,"cmd":"move","x":0,"y":0}
{"id":6,"ok":true,"t":0}
```

`move` 只更新绝对指针位置，不按键。触摸屏固件（Android 默认）在未按下时不产生触摸事件，
因此该命令对两端都安全；旧固件返回 `UNKNOWN_CMD`，上位机按「不支持」静默跳过。
iOS 的绝对鼠标在屏幕边缘可能有死区，`phone ios ...` 会在会话首个动作前先 `move 0 0`。

## 4. 错误码

| 错误码 | 含义 |
|---|---|
| `BAD_JSON` | 不是合法 JSON，或缺 id/cmd |
| `UNKNOWN_CMD` | 未知命令 |
| `OUT_OF_RANGE` | 坐标、屏幕、校准或文本长度越界 |
| `BAD_VALUE` | key/text 内容不支持 |
| `BUSY` | 上一动作未结束（正常单线程下不应出现） |
| `NOT_READY` | 处于 FAULT，需先 `hello` |
| `RATE_LIMIT` | 动作间隔 <80ms |
| `ABORTED` | 动作被 `stop` 中断 |
| `NVS_ERR` | 保存失败 |
| `QUEUE_FULL` | 控制行队列溢出（status 的 last_err） |

## 5. 状态与安全

- 状态：`INIT → READY → BUSY → READY`；异常进入 `FAULT`。
- 心跳：任意合法请求刷新（串口/TCP 任意一条通道）；连接后 30s 无请求 → 释放按键、进入 `FAULT`。
- TCP 与 UART 共用同一个命令队列和状态机；同一时刻只接受一个 TCP 客户端。
- WiFi 连不上不影响 UART：凭证错误/超时会停在 `connecting`/`failed`，串口仍可用 `wifi` 命令修正。
- `stop` 在任意状态可用；滑动中收到 `stop`，固件在下一帧释放并回 `ABORTED`，随后回 `stop` 的 `ok`。
- `FAULT` 下动作命令返回 `NOT_READY`；`hello` 清除。
- 上电/复位、超时、断线都先释放按键，再报错。
- 动作命令最小间隔 80ms（约 10 次/秒）。

## 6. 会话示例

```text
→ {"id":1,"cmd":"hello"}
← {"evt":"ready","fw":"0.2.0","dev":"hand-1A2B","screen":[1264,2800],"cal":[0,32767,0,32767],"hid":"touch","wifi":{"state":"connected","ssid":"Office","ip":"192.168.1.20","rssi":-52,"port":9000}}
→ {"id":2,"cmd":"move","x":0,"y":0}
← {"id":2,"ok":true,"t":0}
→ {"id":3,"cmd":"tap","x":540,"y":960}
← {"id":3,"ok":true,"t":42}
→ {"id":4,"cmd":"swipe","x1":540,"y1":1800,"x2":540,"y2":400,"ms":400}
← {"id":4,"ok":true,"t":470}
→ {"id":5,"cmd":"stop"}
← {"id":5,"ok":true,"t":1}
```

# 安全与隐私

## 威胁模型

这是一个**本机单用户**工具：你的电脑通过 USB 控制自己桌上的手机。
默认假设：本机可信、局域网可信、模型提供商按你的配置调用。

## 网络面

| 端口 | 归属 | 鉴权 | 建议 |
|---|---|---|---|
| `8765` | `phone serve`（HTTP） | 可选 Bearer token（`PHONE_SERVICE_TOKEN`） | 保持只监听 `127.0.0.1`；开启 token |
| `9000` | ESP32-S3 固件（TCP） | **无应用层鉴权** | 只在可信局域网使用；不要做端口映射/公网暴露 |

- `phone serve` 默认绑定 `127.0.0.1`，不会主动对外监听
- `--no-auth` 仅用于本机调试；生产环境请设置 `PHONE_SERVICE_TOKEN` 并在插件配置里填同一个值
- 固件 WiFi 凭证保存在 NVS；配网使用 BLE，PoP 默认与设备 ID 相关，见 `docs/wifi-tcp.md`。
  如果你的环境对安全敏感，请自行修改 `firmware/esp32s3-hand/src/config.h` 里的默认值

## 数据面

- 屏幕帧、截图和 OCR 文本会经本地服务传给 DSH agent；agent 可能把这些内容发送给
  你配置的模型提供商。做敏感操作前请确认模型/网络策略
- `runs/`、`shots/` 只在本地写入，已加入 `.gitignore`，不会提交
- 模板（`templates/*.md`）包含页面文字与坐标，属于个人使用数据；发布/分享前请脱敏

## 报告漏洞

请通过 GitHub Security Advisories 或 issue 私密报告，不要在公开 issue 中贴设备信息/日志。

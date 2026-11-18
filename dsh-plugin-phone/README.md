# dsh-plugin-phone

DeepSeek Harness 插件：把一台真实 Android 手机接到 Agent 上。

- **工具**：`phone_screenshot` / `phone_find_element` / `phone_ocr_texts` / `phone_click` /
  `phone_swipe` / `phone_key` / `phone_text`
- **面板**：自然语言任务、实时进度、任务历史、模板管理、模板执行/复盘
- **模板**：把一次成功执行总结成可回放的 md；页面漂移时会校验失败而不是继续盲点
- **guided**：环境未知时由模型逐步执行；成功的导航路径会写回模板

依赖本仓库根目录的 Python 后端（`console/` + `scripts/serve_phone.sh`）。完整安装见
[根 README](../README.md)。

---

## 安装

1. 把本文件所在目录加入 DSH profile 的 `cordis.patch.yml`（参考
   [`cordis.patch.yml`](cordis.patch.yml)，改三处路径并填写 token）。
2. 同步代码到 profile：

```bash
make plugin-reinstall              # 默认 ~/.dsh/profiles/desktop
# 或：make plugin-reinstall DSH_HOME=/path/.dsh DSH_PROFILE=my-profile
```

3. **完全退出并重开 DSH Desktop**（Host 插件改动必须重启；仅 client 改动刷新页面即可）。

用于 Host 自己创建的 agent（总结、guided 导航）需要模型路由：插件配置优先，
否则读取 profile 的 `agent-default-model`。没有路由时会明确报 `SUMMARY_NO_MODEL`。

---

## 配置

| 字段 | 默认 | 说明 |
|---|---|---|
| `baseUrl` | `http://127.0.0.1:8765` | 本地 phone 服务 |
| `token` | `""` | 与 `PHONE_SERVICE_TOKEN` 一致；空 = 不鉴权（仅本机） |
| `autoStartService` | `true` | 由插件拉起服务 |
| `serviceCwd` / `serviceCommand` / `serviceLog` | — | 拉起方式与日志路径 |
| `serviceHealthIntervalMs` | `20000` | 健康轮询 |
| `serviceRestartOnDegraded` | `true` | 采集过期时重启服务（指数退避） |
| `provider` / `model` | 空 | Host 自建 agent 的模型路由；空则读 profile 默认模型 |
| `guidedFirst` | `true` | 环境未知时先 guided（`false` = 先回放 approach） |
| `guidedRun` | `true` | 环境未知时整份任务由模型执行 |
| `guidedApproach` | `true` | 只做「到达起始页」的模型导航 |
| `guidedMaxSteps` / `guidedFullMaxSteps` | 12 / 30 | 动作步数上限（看屏幕不计入） |
| `guidedTimeoutMs` / `guidedFullTimeoutMs` | 150s / 240s | 超时 |
| `guidedMaxObservations` | 120 | 屏幕读取次数上限（防失控） |
| `environmentCheck` | `true` | 执行前截图 + OCR 确认是否已在起始页 |
| `learnApproach` | `true` | guided 成功后把导航路径写回模板 |
| `templateRunTimeoutMs` | 900000 | 模板单次执行超时 |

---

## 面板

- **任务**：一句自然语言目标 → Agent 规划并执行；运行卡显示阶段（确认环境 / 到达起始页 / 自动执行中 / 执行主路径）、步骤、耗时、实时截图
- **历史**：每条记录有 查看会话 / 再执行一次 / 存为模板 / 删除；全部记录可清空
- **模板**：列表、重命名、编辑 md、执行模板、复盘（proposals 可预览/应用/回滚）
- **定时**：5/15/30/60 分钟或指定时间点；Host 进程内定时
- 设备离线时显示配网页；iOS 仅眼模式也能截图/找文字

## 工具

| 工具 | 参数要点 |
|---|---|
| `phone_screenshot` | `maxWidth` / `includeBase64`；返回截图路径 |
| `phone_find_element` | `keyword`、`minScore`、`mode`(contains/exact/regex)、`roi`、`index` |
| `phone_ocr_texts` | 一次列出整屏文字 + 设备坐标（比反复 find 便宜） |
| `phone_click` | 设备物理像素 `x`/`y`（先用 find/ocr 拿坐标） |
| `phone_swipe` | 起止点 + `ms` |
| `phone_key` | `code`（HOME/BACK/…） |
| `phone_text` | 输入文本 |

---

## 模板格式

```yaml
---
id: taobao-price-protection
name: 淘宝价保中心一键价保
version: 1
status: draft
screen: [1264, 2800]          # 坐标基准分辨率
coords_known: true
coords_updated: 2026-11-18
coords_ttl_days: 30           # 超过后 tap 自动降级为 tap_text
success_expect: 我知道了       # 唯一终态（可瞬时出现，用于 guided 完成判定）
entry:                         # 先确认起始页，再执行坐标主路径
  expect: 专属客服
  timeout: 20
  approach:                    # 只允许 key/tap_text/expect/sleep，不允许坐标
    - {do: key, code: HOME}
    - {do: tap_text, text: 淘宝}
    - {do: expect, text: 我的淘宝, timeout: 10}
---

## 步骤
```yaml
steps:
  - {id: s1, do: tap, text: 专属客服, at: [0.82, 0.10], expect_after: 官方自助服务}
  - {id: s3, do: tap, text: 一键价保, at: [0.50, 0.89], expect_after: 我知道了, check_timeout: 20}
```
```

动作：`tap` / `tap_text` / `expect` / `key` / `swipe` / `sleep` / `text`。
`expect_after`（≤16 字、无标点）会被编译成紧跟该步的 `assert_text`；
异步结果用 `check_timeout` 放宽等待。坐标归一化 0–1，相对 `screen`。

执行分四段：**确认环境 → 到达起始页（回放或 guided）→ 主路径 → 复盘**。

- 环境已在起始页：直接回放主路径
- 环境未知：默认 guided 全程执行（模板仅作计划）；`guidedFirst: false` 则先回放 approach
- 模板执行成功后才自动应用「保守修正」（降级坐标 / 放宽超时）；失败只出复盘建议，不改文件

## 复盘与写回

```bash
make tpl-review TPL=examples/templates/taobao-price-protection.md
make tpl-apply  TPL=examples/templates/taobao-price-protection.md ARGS=--yes
```

proposals 分两类：安全（`demote_coords`、`timeout_raise`、`widen_roi`）可自动应用；
乐观（`promote_coords`、`timeout_lower`）只建议。版本在 `templates/.versions/<id>/vN.md`，
`tpl_apply --rollback` 回退。

guided 成功到达起始页后，走过的文字/按键路径会写回 `entry.approach`（版本 +1、备份、变更记录）；
坐标动作不会被学习。

---

## 开发

```bash
node --test test/*.test.mjs        # 插件单元测试（Node 22+）
```

Host 侧代码在 `lib/`（`task-service.js` 是核心），client 在 `client/client.js`（纯 React.createElement，
无构建步骤）。RPC 走 `phone.task.*` / `phone.device.*` / `phone.provision.*` 三组端点。

## License

MIT（见 [`LICENSE`](LICENSE)）。

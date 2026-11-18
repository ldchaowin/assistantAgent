---
id: taobao-price-protection
name: 淘宝价保中心一键价保
version: 1
status: draft
screen: [1264, 2800]
device: default
coords_known: true
coords_updated: 2026-11-18
coords_ttl_days: 30
success_expect: 我知道了
entry:
  expect: 专属客服
  timeout: 20
  approach:
    - {do: key, code: HOME}
    - {do: tap_text, text: 淘宝}
    - {do: expect, text: 我的淘宝, timeout: 10}
    - {do: tap_text, text: 我的淘宝}
    - {do: expect, text: 专属客服, timeout: 10}
---

# 淘宝价保中心一键价保

> 示例用：坐标来自一次 1264×2800 的实测；换设备/换版本后在真机跑 2–3 次再标 `verified`。

## 目标

从任意状态进入淘宝 → 我的淘宝 → 专属客服 → 价保中心，执行一键价保并确认结果弹窗。

## 判据

- **成功**：点击「一键价保」后出现结果弹窗（弹窗内有「我知道了」）
- **成功**：点击「我知道了」后回到价保中心页面
- **失败**：任一步 expect 超时；页面与预期不符
- **不可逆点**：s3（提交价保）

## 前置

- 手机已解锁，淘宝已登录
- 桌面或任意 App 均可（entry 会先回桌面）

## 步骤

```yaml
# 坐标归一化 0–1；expect_after 是短标签，编译后会变成紧跟该步的 assert_text 校验。
steps:
  - {id: s1, do: tap, goal: 在我的淘宝页点击专属客服入口, text: 专属客服, at: [0.8228, 0.1018], expect_after: 官方自助服务, check_timeout: 15}
  - {id: s2, do: tap, goal: 进入价保中心, text: 价保中心, at: [0.8742, 0.1679], expect_after: 一键价保, check_timeout: 15}
  - {id: s3, do: tap, goal: 提交一键价保（结果异步返回）, text: 一键价保, at: [0.5000, 0.8900], expect_after: 我知道了, check_timeout: 20}
  - {id: s4, do: tap, goal: 关闭结果弹窗, text: 我知道了, at: [0.5000, 0.9132]}
```

## 变更记录

| 版本 | 日期 | 改了什么 | 依据 |
|---|---|---|---|
| 1 | 2026-11-18 | 示例模板（脱敏） | 示例 |

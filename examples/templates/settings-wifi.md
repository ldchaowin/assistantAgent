---
id: settings-wifi
name: 打开设置查看 Wi-Fi
version: 1
status: draft
screen: [1264, 2800]
device: default
coords_known: false
entry:
  expect: 设置
  timeout: 15
  approach:
    - {do: key, code: HOME}
    - {do: tap_text, text: 设置}
---

# 打开设置查看 Wi-Fi

> 纯文字动作示例：不需要坐标，适合验证「entry + 文字点击 + 断言」这条链路。

## 目标

从任意状态回到桌面并打开系统「设置」，进入 WLAN/Wi-Fi 页面。

## 判据

- **成功**：设置首页出现
- **成功**：WLAN 页面出现（不同系统文案可能是「WLAN」「Wi-Fi」「无线局域网」）

## 步骤

```yaml
steps:
  - {id: s1, do: tap_text, goal: 打开设置里的 WLAN 入口, text: WLAN}
  - {id: s2, do: expect, goal: 进入 WLAN 页面，标题通常仍是 WLAN, text: WLAN, timeout: 5}
```

## 变更记录

| 版本 | 日期 | 改了什么 | 依据 |
|---|---|---|---|
| 1 | 2026-11-18 | 示例模板 | 示例 |

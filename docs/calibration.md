# 坐标校准

> 下文 `phone` 指 `.venv/bin/phone`；激活 venv 后可直接写 `phone`。

## 1. 前提

- 手机锁定竖屏，关闭自动旋转、自动亮度、自动锁屏。
- 默认固件是触摸屏模式（`hid:"touch"`），没有鼠标指针；校准看“触摸落点”，不是指针。
- 已知**物理分辨率**（不是 CSS 像素）。有 adb 时：
  ```bash
  adb shell wm size
  ```
  取 `Physical size: 1080x2400` 这一行。
- 打开 `tools/grid_test.html`（复制到手机用 Chrome 打开，或在电脑上 `python3 -m http.server` 后让手机访问局域网地址）。它只用于看落点落在哪个格子，不用于精确像素测量。

## 2. 映射

```text
x_hid = x0 + x_px * (x1 - x0) / screen_w
y_hid = y0 + y_px * (y1 - y0) / screen_h
```

`x0/x1/y0/y1` 是 HID 的 0–32767 有效区间，默认 `0,32767,0,32767`。

## 3. 向导（推荐）

```bash
# 真机：按提示依次点四角+中心，输入实际落点；确认后写 NVS
phone --port /dev/cu.usbserial-XXXX calibrate --save

# 只算不存：省略 --save
phone --port /dev/cu.usbserial-XXXX calibrate

# 快速自检：假设每点都命中，仅验证链路（不做真实校准）
phone calibrate --auto --save --yes
```

向导会：

1. 依次点击四角与中心（默认 5% 内缩，避开圆角/手势区）。
2. 让你输入每个点的实际落点 `x,y`；直接回车表示命中目标。
3. 用线性反解计算新的 `cal`，显示旧值/新值；超出 `0..32767` 会拒绝保存。
4. `--save` 时写入 NVS，`--yes` 跳过确认。

`--auto` 只用于冒烟，不代表真实校准。触摸屏模式下默认 cal 通常已经准确（0–32767 映射整屏）；只有落点明显偏移时才需要向导。

## 4. 五步（手动）

1. 握手，确认固件看到的屏幕：
   ```bash
   phone hello
   ```
2. 写入实际分辨率：
   ```bash
   phone config --screen 1080x2400 --save
   ```
3. 写入默认校准并保存：
   ```bash
   phone config --cal 0,32767,0,32767 --save
   ```
4. 试打四角，观察落点：
   ```bash
   phone tap 10 10
   phone tap 1070 10
   phone tap 10 2390
   phone tap 1070 2390
   ```
5. 按现象调整，保存后复测：
   ```bash
   phone config --cal 0,32767,0,32767 --save
   phone selftest --cols 5 --rows 5 --dwell 0.4
   ```

## 5. 调整方向

| 现象 | 调整 |
|---|---|
| 所有点整体偏右 | 减小 `x0`（HID 值发大了） |
| 所有点整体偏左 | 增大 `x0` |
| 所有点整体偏下 | 减小 `y0` |
| 所有点整体偏上 | 增大 `y0` |
| 右侧/底部到不了 | 增大 `x1` / `y1` |
| 右侧/底部超出屏幕 | 减小 `x1` / `y1` |
| 左上角到不了 | 增大 `x0` / `y0` |
| 左上角超出屏幕 | 减小 `x0` / `y0` |

每次改 100–300 个 HID 单位，改完 `--save` 再复测。不要一次改到边界。

## 6. 验收

- `phone selftest --cols 5 --rows 5` 跑 25 点，每点落进目标格。
- 边缘点（距边 5–10px）不跑偏；跑偏优先检查圆角/贴膜遮挡。
- 单点偏差目标 ≤3px（约 0.3% 屏宽）。
- 换分辨率、换机、换壳后重跑本节。

## 7. 如果绝对坐标不工作

现象：指针不动、瞬移、只在局部区域响应。先确认：

- 手机为 Android 16，且识别到了 HID 鼠标（状态栏/指针出现）。
- `phone status` 不是 `FAULT`。
- 换一根支持数据的线，直连手机，不经过 Hub。

仍不行则停止校准。Android 对绝对 HID 的支持因厂商而异，v0.1 没有相对坐标回退；按实现计划中的 D2 决策评估 HID 触摸屏方案，不要用校准参数硬凑。

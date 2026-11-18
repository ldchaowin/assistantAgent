# 测试计划

> 下文 `phone` 指 `.venv/bin/phone`；激活 venv 后可直接写 `phone`。

验收指标见 根目录 `README.md`。本文只列“怎么测、按什么顺序”。

## 1. 烧录前（无需硬件）

```bash
make test                         # 133 个 Python 测试
.venv/bin/phone --mock doctor
.venv/bin/phone --mock accept --report-dir runs/accept-demo
.venv/bin/phone --mock calibrate --auto --save --yes
.venv/bin/phone --mock selftest --cols 5 --rows 5 --dwell 0
.venv/bin/phone --mock stress --n 50 --interval 0
```

期望：133 个测试通过；doctor 无 `fail`（默认 cal 的 `warn` 正常）；accept 无 `fail`（manual/skip 允许）；calibrate `valid=true` 且 `saved=true`；mock 网格与压测成功；固件 `pio run` 成功。

`accept` 会写 `runs/accept-demo/report.json` 与 `report.html`，包含每项状态、环境、耗时与 p95。

## 2. 真机测试

| 编号 | 项目 | 做法 | 通过标准 |
|---|---|---|---|
| T0 | 预检 | `phone --port ... doctor --service-url http://127.0.0.1:8765` | 无 `fail`；warn 已确认 |
| T1 | 供电/反灌 | 双口同时插，测 VBUS 与温度 | 无反灌，板子不异常发热 |
| T2 | 枚举 | 手机接原生 USB；默认触摸屏模式 | 3s 内触摸生效（无鼠标指针） |
| T3 | 握手 | `phone hello`、`status` | `ready`，`READY` |
| T4 | 中心/四角点击 | `phone accept --manual` 的四角项 | 落点正确，偏差 ≤3px |
| T5 | 25 点网格 | `phone accept --manual` 的网格项 | 每点进目标格 |
| T6 | 滑动 | 200/500/1000ms 各 5 次 | 无断触，时长误差 ≤±5% |
| T7 | 按键 | `key HOME/BACK/ENTER/UP` | 对应动作正确 |
| T8 | 文本 | `text Abc123` | 无丢字、无错序 |
| T9 | 滑动中急停 | 滑动时发 `phone stop` | 立即释放，无残留按压 |
| T10 | 压测 | `phone accept --full`（1000 次、0.3s） | 0 失败、0 卡键；p95 ≤50ms（full 1000 次实测 40.94ms） |
| T11 | 长稳 | `phone accept --manual --long-hours 8` | 无重启/掉线，板面 <60°C |
| T12 | 拔插/重启 | 拔插 50 次；手机重启/锁屏 | 枚举 ≥49 次；10s 内恢复 |

## 3. 记录

推荐一条命令生成完整验收报告：

```bash
phone --port /dev/cu.usbserial-XXXX accept --manual --full \
  --report-dir runs/accept-$(date +%Y%m%d-%H%M)
```

报告包含：固件/设备/屏幕/校准、每项状态、压测 p95、长稳 ping 数、失败详情；`report.html` 可直接归档。

同时用 `--log` 落 JSONL：

```bash
phone --port /dev/cu.usbserial-XXXX --log logs/t10.jsonl stress --n 1000
```

记录：固件版本、手机型号/Android 版本、线材、供电方式、失败项与原始日志。

## 4. D2 决策点

如果 T4/T5 不通过，先排除校准与线材；仍不通过则停止，不要用校准参数硬凑。D2 已实测 Android 16 不注册绝对鼠标，默认改用 HID 触摸屏。

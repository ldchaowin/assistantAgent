# phone-console

Python backend for the ESP32-S3 HID phone automation project.

- `phone` CLI: device/eye/service/scenario/template commands
- `phone_console.service`: local HTTP service (`/status`, `/screenshot`, `/ocr`, `/find`, `/run`, …)
- `phone_console.template`: replayable task templates (`.md` with YAML steps)

Install from the repository root:

```bash
pip install -e 'console[vision]'
pip install -e 'console[stream]'
```

Docs: [`../README.md`](../README.md) and [`../docs/`](../docs/).

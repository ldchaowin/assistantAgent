VENV ?= .venv
PYTHON ?= $(VENV)/bin/python
PY := $(PYTHON)
PHONE := $(VENV)/bin/phone
PYTEST := $(VENV)/bin/pytest
PIO := $(PYTHON) -m platformio
DSH_HOME ?= $(HOME)/.dsh
DSH_PROFILE ?= desktop
CORE := $(CURDIR)/.pio-core
PROJ := firmware/esp32s3-hand
HAND ?= hand.local            # override: make tcp-status HAND=hand-abcd.local
TEMPLATE_DIR ?= examples/templates

.PHONY: test test-plugin test-all build build-abs flash flash-abs monitor mock eye-demo bench bench-line stream-demo watch-demo wifi-status wifi-set tcp-status run-demo run-all serve-demo doctor-demo accept-demo calibrate-demo ios-eye-build ios-status ios-wait ios-shot ios-stream ios-mirror-list ios-mirror-shot ios-doctor ios-hand serve-ios serve-ios-mirror tpl-check run-tpl clean

test:
	cd console && ../$(PYTEST) -q

build:
	PLATFORMIO_CORE_DIR=$(CORE) $(PIO) run -d $(PROJ)

build-abs:
	PLATFORMIO_CORE_DIR=$(CORE) $(PIO) run -d $(PROJ) -e esp32s3-hand-abs

flash:
	PLATFORMIO_CORE_DIR=$(CORE) $(PIO) run -d $(PROJ) -t upload $(if $(PORT),--upload-port $(PORT),)

flash-abs:
	PLATFORMIO_CORE_DIR=$(CORE) $(PIO) run -d $(PROJ) -e esp32s3-hand-abs -t upload $(if $(PORT),--upload-port $(PORT),)

monitor:
	PLATFORMIO_CORE_DIR=$(CORE) $(PIO) device monitor -d $(PROJ) -b 460800

mock:
	$(PHONE) --mock status

eye-demo:
	$(PHONE) eye find 设置 --image docs/assets/eye-demo.png --screen 1080x2400

# OCR latency on the synthetic screen: cold, cached, per stage.
bench:
	$(PHONE) eye bench --image docs/assets/eye-demo.png --repeat 5

# ROI + single-line fast path (~10ms): point the ROI at one label.
bench-line:
	$(PHONE) eye bench --image docs/assets/eye-demo.png \
	  --roi 400,1535,230,90 --line --repeat 5

# Live stream smoke test (needs the phone + scrcpy, or use --screenrecord).
stream-demo:
	$(PHONE) eye stream --scrcpy --seconds 5

# ---------------------------------------------------------------- iOS (iPhone)
# Native iPhone eye (CoreMediaIO iOSScreenCapture helper, no Developer Mode).
ios-eye-build:
	$(PHONE) ios build --force

ios-status:
	$(PHONE) ios status

ios-wait:
	$(PHONE) ios wait --timeout 120

ios-shot:
	$(PHONE) ios shot --out shots/ios.png

ios-stream:
	$(PHONE) ios stream --seconds 5 --save shots/ios-live.png

# Plan B: mirrored-iPhone window (iPhone Mirroring / UxPlay) as the eye.
ios-mirror-list:
	$(PHONE) ios mirror list

ios-mirror-shot:
	$(PHONE) ios mirror shot --out shots/ios-mirror.png

serve-ios-mirror:
	$(PHONE) --tcp $(HAND) serve --ios-mirror --http-port 8765 --no-auth

ios-doctor:
	$(PHONE) doctor --ios --skip-adb

# Absolute-mouse hand: build/flash first with `make build-abs` / `make flash-abs`.
ios-hand:
	$(PHONE) --tcp $(HAND) ios hand --save

# Local service for DSH tools in iOS mode (eye-only still serves /screenshot,/find).
serve-ios:
	$(PHONE) --tcp $(HAND) serve --ios --http-port 8765 --no-auth

# Watch a live stream until text appears (needs the phone).
wifi-status:
	$(PHONE) wifi status

wifi-set:
	$(PHONE) wifi set "$(SSID)" "$(PASS)"

tcp-status:
	$(PHONE) --tcp $(TCP) status

watch-demo:
	$(PHONE) eye watch 设置 --scrcpy --timeout 20

run-demo:
	$(PHONE) --mock run scenarios/settings-demo.json

run-all:
	$(PHONE) --mock run scenarios/settings-demo.json
	$(PHONE) --mock run scenarios/price-protect-demo.json
	$(PHONE) --mock run scenarios/game-daily-demo.json

# 任务模板：校验 / 执行 / 复盘（make run-tpl TPL=examples/templates/taobao-price-protection.md）
TEMPLATE_DIR ?= examples/templates

tpl-check:
	@for f in $(TEMPLATE_DIR)/*.md; do $(PY) tools/tpl_compile.py "$$f" --check >/dev/null || exit 1; done
	@echo "templates ok"

# DSH desktop profile 里是 file: 依赖的副本，改完插件必须显式重装才会生效
plugin-reinstall:
	rm -rf $(DSH_HOME)/profiles/$(DSH_PROFILE)/node_modules/dsh-plugin-phone
	cd $(DSH_HOME)/profiles/$(DSH_PROFILE) && pnpm install --prefer-offline
	diff -rq --exclude=test --exclude=runs dsh-plugin-phone \
		$(DSH_HOME)/profiles/$(DSH_PROFILE)/node_modules/dsh-plugin-phone \
		&& echo "plugin copy in sync; 重启 DSH Desktop 生效"

run-tpl:
	@test -n "$(TPL)" || { echo "usage: make run-tpl TPL=examples/templates/xxx.md [ARGS=...]"; exit 1; }
	$(PY) tools/tpl_run.py $(TPL) $(ARGS)

tpl-review:
	@test -n "$(TPL)" || { echo "usage: make tpl-review TPL=examples/templates/xxx.md"; exit 1; }
	$(PY) tools/tpl_review.py $(TPL) --runs $(or $(RUNS),10)

# 应用复盘建议：默认预览 diff，make tpl-apply TPL=x ARGS=--yes
tpl-apply:
	@test -n "$(TPL)" || { echo "usage: make tpl-apply TPL=examples/templates/xxx.md [ARGS='--yes --only s3']"; exit 1; }
	$(PY) tools/tpl_apply.py $(TPL) $(ARGS)

serve-demo:
	$(PHONE) --mock serve --frames scenarios/frames/settings --http-port 8765 --no-auth

test-plugin:
	cd dsh-plugin-phone && node --test test/*.test.mjs

test-all: test test-plugin

doctor-demo:
	$(PHONE) --mock doctor

accept-demo:
	$(PHONE) --mock accept --report-dir runs/accept-demo

calibrate-demo:
	$(PHONE) --mock calibrate --auto --save --yes

clean:
	PLATFORMIO_CORE_DIR=$(CORE) $(PIO) run -d $(PROJ) -t clean

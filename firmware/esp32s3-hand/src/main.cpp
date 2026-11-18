#include <Arduino.h>
#include <ArduinoJson.h>
#include <USB.h>
#include <USBHIDKeyboard.h>
#include <esp_idf_version.h>
#include <esp_task_wdt.h>
#include <tusb.h>
#include <freertos/FreeRTOS.h>
#include <freertos/queue.h>
#include <freertos/task.h>

#include <cstring>

#include "abs_mouse.h"
#include "config.h"
#include "keys.h"
#include "motion.h"
#include "net.h"
#include "protocol.h"
#include "prov.h"
#include "storage.h"

namespace {

struct Line {
  char data[IO_LINE_MAX];
  uint8_t channel;  // 0 = UART0, 1 = TCP
};

AbsMouse mouse;
USBHIDKeyboard keyboard;
USBHIDConsumerControl consumer;
Storage storage;
Motion motion(mouse, keyboard, storage);
KeySender keys(mouse, keyboard, consumer);

QueueHandle_t line_queue = nullptr;
volatile bool stop_flag = false;
volatile bool queue_overflow = false;

State state = State::INIT;
bool linked = false;
uint32_t last_heartbeat = 0;
uint32_t last_action_ms = 0;
char last_error[24] = "";

void set_error(const char *code) {
  strlcpy(last_error, code == nullptr ? "" : code, sizeof(last_error));
}

void clear_error() { last_error[0] = '\0'; }

void save_storage() { storage.save(); }

WifiInfo current_wifi() {
  WifiInfo info = net::info();
  prov::fill(info);
  return info;
}

void init_watchdog() {
#if ESP_IDF_VERSION_MAJOR >= 5
  esp_task_wdt_config_t cfg = {};
  cfg.timeout_ms = 5000;
  cfg.idle_core_mask = 0;
  cfg.trigger_panic = true;
  const esp_err_t err = esp_task_wdt_init(&cfg);
  if (err == ESP_ERR_INVALID_STATE) {
    esp_task_wdt_reconfigure(&cfg);
  }
#else
  esp_task_wdt_init(5, true);
#endif
  esp_task_wdt_add(nullptr);
}

void queue_line(const char *line, uint8_t channel) {
  Line item = {};
  strlcpy(item.data, line, sizeof(item.data));
  item.channel = channel;
  if (xQueueSend(line_queue, &item, 0) != pdTRUE) {
    queue_overflow = true;
  }
  if (std::strstr(line, "\"cmd\":\"stop\"") != nullptr ||
      std::strstr(line, "\"cmd\": \"stop\"") != nullptr) {
    stop_flag = true;
  }
}

void net_line_handler(const char *line, uint8_t channel) {
  queue_line(line, channel);
}

void serial_task(void *) {
  static char buf[IO_LINE_MAX];
  size_t len = 0;

  for (;;) {
    while (Serial0.available()) {
      const char c = static_cast<char>(Serial0.read());
      if (c == '\r') {
        continue;
      }
      if (c == '\n') {
        if (len > 0) {
          buf[len] = '\0';
          queue_line(buf, 0);
          len = 0;
        }
      } else if (len < sizeof(buf) - 1) {
        buf[len++] = c;
      } else {
        len = 0;
        queue_overflow = true;
      }
    }
    vTaskDelay(2 / portTICK_PERIOD_MS);
  }
}

void poll_boot_button() {
  static uint32_t pressed_at = 0;
  static bool triggered = false;
  const bool pressed = digitalRead(ESTOP_PIN) == LOW;

  if (pressed && !triggered) {
    if (pressed_at == 0) {
      pressed_at = millis();
    } else if (millis() - pressed_at >= BOOT_STOP_MS) {
      stop_flag = false;
      motion.release_all();
      if (state != State::FAULT) {
        state = State::READY;
      }
      clear_error();
      proto::estop();
      net::notify_estop();
      triggered = true;
    }
  } else if (!pressed) {
    pressed_at = 0;
    triggered = false;
  }
}

void handle_cfg(int id, JsonDocument &doc) {
  Config &cfg = storage.config();
  const int w = doc["screen"]["w"] | cfg.screen_w;
  const int h = doc["screen"]["h"] | cfg.screen_h;
  const int x0 = doc["cal"]["x0"] | cfg.cal_x0;
  const int x1 = doc["cal"]["x1"] | cfg.cal_x1;
  const int y0 = doc["cal"]["y0"] | cfg.cal_y0;
  const int y1 = doc["cal"]["y1"] | cfg.cal_y1;

  if (w < 100 || w > 5000 || h < 100 || h > 5000) {
    set_error("OUT_OF_RANGE");
    proto::err(id, "OUT_OF_RANGE", "screen");
    return;
  }
  if (x0 < 0 || x1 < 0 || y0 < 0 || y1 < 0 || x0 > HID_MAX ||
      x1 > HID_MAX || y0 > HID_MAX || y1 > HID_MAX || x0 == x1 || y0 == y1) {
    set_error("OUT_OF_RANGE");
    proto::err(id, "OUT_OF_RANGE", "cal");
    return;
  }

  cfg.screen_w = static_cast<uint16_t>(w);
  cfg.screen_h = static_cast<uint16_t>(h);
  cfg.cal_x0 = static_cast<int16_t>(x0);
  cfg.cal_x1 = static_cast<int16_t>(x1);
  cfg.cal_y0 = static_cast<int16_t>(y0);
  cfg.cal_y1 = static_cast<int16_t>(y1);
  clear_error();
  proto::ok(id, 0);
}

void handle_wifi(int id, JsonDocument &doc) {
  Config &cfg = storage.config();
  const char *op = doc["op"] | "status";
  if (std::strcmp(op, "status") == 0) {
    proto::wifi_status(id, current_wifi());
    return;
  }
  if (std::strcmp(op, "prov") == 0) {
    // Reboot into a clean provisioning stack: starting BLE while the station
    // is connected leaves the protocomm state unreliable. The old credentials
    // remain in NVS and are used again if provisioning fails.
    cfg.force_prov = true;
    storage.save();
    clear_error();
    proto::ok(id, 0);
    delay(200);
    ESP.restart();
    return;
  }
  if (std::strcmp(op, "set") == 0) {
    const char *ssid = doc["ssid"] | "";
    const char *pass = doc["pass"] | "";
    const int port = doc["port"] | cfg.tcp_port;
    char err[16] = "";
    prov::stop();
    cfg.wifi_configured = true;
    if (!net::set_credentials(cfg, ssid, pass, port, err, sizeof(err))) {
      set_error("BAD_VALUE");
      proto::err(id, "BAD_VALUE", err);
      return;
    }
    if (!storage.save()) {
      set_error("NVS_ERR");
      proto::err(id, "NVS_ERR");
      return;
    }
    clear_error();
    proto::ok(id, 0);
    return;
  }
  if (std::strcmp(op, "clear") == 0) {
    prov::stop();
    cfg.wifi_configured = false;
    cfg.force_prov = true;
    net::clear_credentials(cfg);
    storage.save();
    clear_error();
    proto::ok(id, 0);
    delay(250);
    ESP.restart();
    return;
  }
  set_error("BAD_VALUE");
  proto::err(id, "BAD_VALUE", "op");
}

void handle_action_start() {
  state = State::BUSY;
  last_action_ms = millis();
}

void handle_action_end(int id, MotionResult result, uint32_t started_ms) {
  state = State::READY;
  last_action_ms = millis();
  if (result == MotionResult::OK) {
    clear_error();
    proto::ok(id, millis() - started_ms);
  } else if (result == MotionResult::RANGE) {
    set_error("OUT_OF_RANGE");
    proto::err(id, "OUT_OF_RANGE");
  } else {
    set_error("ABORTED");
    proto::err(id, "ABORTED");
  }
}

void dispatch_line(char *line) {
  StaticJsonDocument<1024> doc;
  const DeserializationError error = deserializeJson(doc, line);
  if (error) {
    set_error("BAD_JSON");
    proto::err(0, "BAD_JSON", error.c_str());
    return;
  }

  const int id = doc["id"] | -1;
  const char *cmd = doc["cmd"] | "";
  if (id < 0 || cmd == nullptr || cmd[0] == '\0') {
    set_error("BAD_JSON");
    proto::err(id < 0 ? 0 : id, "BAD_JSON", "id/cmd");
    return;
  }

  last_heartbeat = millis();
  linked = true;

  if (std::strcmp(cmd, "hello") == 0) {
    stop_flag = false;
    state = State::READY;
    clear_error();
    proto::ready(storage.config(), current_wifi());
    return;
  }
  if (std::strcmp(cmd, "ping") == 0) {
    const uint32_t ts = doc["ts"] | 0;
    proto::pong(ts);
    return;
  }
  if (std::strcmp(cmd, "stop") == 0) {
    stop_flag = false;
    motion.release_all();
    if (state != State::FAULT) {
      state = State::READY;
    }
    clear_error();
    proto::ok(id, 0);
    return;
  }
  if (std::strcmp(cmd, "status") == 0) {
    proto::status(id, state, last_error, millis(),
                  uxQueueMessagesWaiting(line_queue), tud_mounted(),
                  current_wifi());
    return;
  }
  if (std::strcmp(cmd, "wifi") == 0) {
    handle_wifi(id, doc);
    return;
  }
  if (std::strcmp(cmd, "reboot") == 0) {
    clear_error();
    proto::ok(id, 0);
    delay(200);
    ESP.restart();
    return;
  }
  if (std::strcmp(cmd, "cfg") == 0) {
    handle_cfg(id, doc);
    return;
  }
  if (std::strcmp(cmd, "save") == 0) {
    if (storage.save()) {
      clear_error();
      proto::ok(id, 0);
    } else {
      set_error("NVS_ERR");
      proto::err(id, "NVS_ERR");
    }
    return;
  }

  if (state == State::FAULT) {
    set_error("NOT_READY");
    proto::err(id, "NOT_READY", "hello first");
    return;
  }
  if (state == State::BUSY) {
    proto::err(id, "BUSY");
    return;
  }

  if (std::strcmp(cmd, "move") == 0) {
    int x = doc["x"] | -1;
    int y = doc["y"] | -1;
    if (x < 0 || y < 0) {
      set_error("OUT_OF_RANGE");
      proto::err(id, "OUT_OF_RANGE", "x/y");
      return;
    }
    if (millis() - last_action_ms < CMD_MIN_INTERVAL_MS) {
      set_error("RATE_LIMIT");
      proto::err(id, "RATE_LIMIT");
      return;
    }
    const MotionResult result = motion.move(x, y);
    last_action_ms = millis();
    if (result == MotionResult::OK) {
      clear_error();
      proto::ok(id, 0);
    } else if (result == MotionResult::RANGE) {
      set_error("OUT_OF_RANGE");
      proto::err(id, "OUT_OF_RANGE", "x/y");
    } else {
      set_error("ABORTED");
      proto::err(id, "ABORTED");
    }
    return;
  }

  const bool action = std::strcmp(cmd, "tap") == 0 ||
                      std::strcmp(cmd, "swipe") == 0 ||
                      std::strcmp(cmd, "key") == 0 ||
                      std::strcmp(cmd, "text") == 0;
  if (!action) {
    set_error("UNKNOWN_CMD");
    proto::err(id, "UNKNOWN_CMD", cmd);
    return;
  }
  if (millis() - last_action_ms < CMD_MIN_INTERVAL_MS) {
    set_error("RATE_LIMIT");
    proto::err(id, "RATE_LIMIT");
    return;
  }

  const uint32_t started = millis();

  if (std::strcmp(cmd, "tap") == 0) {
    int x = doc["x"] | -1;
    int y = doc["y"] | -1;
    int hold = doc["hold_ms"] | TAP_HOLD_DEFAULT_MS;
    if (x < 0 || y < 0) {
      set_error("OUT_OF_RANGE");
      proto::err(id, "OUT_OF_RANGE", "x/y");
      return;
    }
    if (hold < 10) hold = TAP_HOLD_DEFAULT_MS;
    if (hold > TAP_HOLD_MAX_MS) hold = TAP_HOLD_MAX_MS;
    handle_action_start();
    const MotionResult result =
        motion.tap(x, y, static_cast<uint16_t>(hold));
    handle_action_end(id, result, started);
    return;
  }

  if (std::strcmp(cmd, "swipe") == 0) {
    const int x1 = doc["x1"] | -1;
    const int y1 = doc["y1"] | -1;
    const int x2 = doc["x2"] | -1;
    const int y2 = doc["y2"] | -1;
    int ms = doc["ms"] | 400;
    if (x1 < 0 || y1 < 0 || x2 < 0 || y2 < 0) {
      set_error("OUT_OF_RANGE");
      proto::err(id, "OUT_OF_RANGE", "x/y");
      return;
    }
    if (ms < SWIPE_MIN_MS) ms = SWIPE_MIN_MS;
    if (ms > SWIPE_MAX_MS) ms = SWIPE_MAX_MS;
    handle_action_start();
    const MotionResult result =
        motion.swipe(x1, y1, x2, y2, static_cast<uint16_t>(ms));
    handle_action_end(id, result, started);
    return;
  }

  if (std::strcmp(cmd, "key") == 0) {
    const char *code = doc["code"] | "";
    handle_action_start();
    const bool ok = keys.send(code);
    state = State::READY;
    last_action_ms = millis();
    if (ok) {
      clear_error();
      proto::ok(id, millis() - started);
    } else {
      set_error("BAD_VALUE");
      proto::err(id, "BAD_VALUE", "code");
    }
    return;
  }

  if (std::strcmp(cmd, "text") == 0) {
    const char *text = doc["s"] | "";
    const size_t len = text == nullptr ? 0 : std::strlen(text);
    if (len == 0 || len > TEXT_MAX) {
      set_error("OUT_OF_RANGE");
      proto::err(id, "OUT_OF_RANGE", "s");
      return;
    }
    handle_action_start();
    const bool ok = keys.text(text, len);
    state = State::READY;
    last_action_ms = millis();
    if (ok) {
      clear_error();
      proto::ok(id, millis() - started);
    } else {
      set_error("BAD_VALUE");
      proto::err(id, "BAD_VALUE", "s");
    }
    return;
  }
}

}  // namespace

void setup() {
  Serial0.begin(CONTROL_BAUD);
  pinMode(ESTOP_PIN, INPUT_PULLUP);

  storage.begin();
  motion.set_stop_flag(&stop_flag);

  mouse.begin();
  keyboard.begin();
  consumer.begin();
  USB.begin();

  line_queue = xQueueCreate(QUEUE_DEPTH, sizeof(Line));
  xTaskCreatePinnedToCore(serial_task, "uart-rx", 4096, nullptr, 2, nullptr, 0);

  Config &boot_cfg = storage.config();
  const bool force_prov = boot_cfg.force_prov;
  if (force_prov) {
    boot_cfg.force_prov = false;
    storage.save();
  }
  net::set_line_handler(net_line_handler);
  // A forced provisioning boot keeps WiFi off until the (fresh) BLE stack
  // has received credentials; old credentials stay in NVS as a fallback.
  net::begin(boot_cfg, !force_prov);
  if (force_prov) {
    prov::start(boot_cfg);
  } else {
    prov::begin(boot_cfg, save_storage);
  }

  init_watchdog();
  state = State::READY;
  last_heartbeat = millis();
  proto::ready(storage.config(), current_wifi());
}

void loop() {
  esp_task_wdt_reset();

  if (queue_overflow) {
    queue_overflow = false;
    set_error("QUEUE_FULL");
  }

  net::loop();
  prov::loop();

  // If the stored network is unreachable, fall back to BLE provisioning.
  static bool recovery_started = false;
  if (net::connected()) {
    recovery_started = false;
  } else if (net::failed() && !recovery_started) {
    recovery_started = true;
    prov::request_start();
  }

  poll_boot_button();

  if (linked && state == State::READY &&
      millis() - last_heartbeat > HEARTBEAT_TIMEOUT_MS) {
    stop_flag = false;
    motion.release_all();
    state = State::FAULT;
    set_error("HEARTBEAT");
  }

  Line line = {};
  if (xQueueReceive(line_queue, &line, 0) == pdTRUE) {
    Print *sink = line.channel == 1 ? net::client_sink() : &Serial0;
    if (sink == nullptr) {
      sink = &Serial0;
    }
    proto::set_output(sink);
    dispatch_line(line.data);
    proto::set_output(nullptr);
  }
  delay(1);
}

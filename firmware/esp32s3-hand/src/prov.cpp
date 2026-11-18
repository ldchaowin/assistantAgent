#include "prov.h"

#include <Arduino.h>
#include <WiFi.h>
// Arduino core releases BTDM memory at boot unless a BLE library is linked.
// The Network Provisioner uses IDF NimBLE directly, so mark BLE as in use.
#include <esp32-hal-alloc-ble-mem.h>
#include <esp_wifi.h>
#include <network_provisioning/manager.h>
#include <network_provisioning/scheme_ble.h>

#include <cstdio>
#include <cstring>

#include "net.h"
#include "protocol.h"

namespace {

Config *g_cfg = nullptr;
void (*g_on_configured)() = nullptr;
bool g_inited = false;
const char *g_state = "idle";
char g_name[24] = "";
char g_err[16] = "";
uint32_t g_started_ms = 0;
volatile bool g_pending_save = false;
volatile bool g_pending_emit = false;
volatile bool g_start_requested = false;
volatile bool g_kick_requested = false;
char g_rx_ssid[33] = "";
char g_rx_pass[64] = "";

constexpr uint32_t PROV_TIMEOUT_MS = PROV_WINDOW_MS;

void fill_info(WifiInfo &info) {
  info.prov = g_state;
  strlcpy(info.prov_name, g_name, sizeof(info.prov_name));
  strlcpy(info.prov_err, g_err, sizeof(info.prov_err));
}

void emit_event() {
  WifiInfo info = net::info();
  fill_info(info);
  proto::prov_event(info, Serial0);
  Print *sink = net::client_sink();
  if (sink != nullptr) {
    proto::prov_event(info, *sink);
  }
}

void set_state(const char *state, const char *err = nullptr) {
  g_state = state;
  if (err != nullptr) {
    strlcpy(g_err, err, sizeof(g_err));
  } else {
    g_err[0] = '\0';
  }
  g_pending_emit = true;
}

void prov_event_handler(void *, network_prov_cb_event_t event, void *data) {
  switch (event) {
    case NETWORK_PROV_START:
      set_state("started");
      break;
    case NETWORK_PROV_WIFI_CRED_RECV: {
      // Keep the candidate credentials in RAM only: a wrong password must
      // not overwrite the working configuration.
      const auto *sta = static_cast<const wifi_sta_config_t *>(data);
      if (sta != nullptr) {
        strlcpy(g_rx_ssid, reinterpret_cast<const char *>(sta->ssid),
                sizeof(g_rx_ssid));
        strlcpy(g_rx_pass, reinterpret_cast<const char *>(sta->password),
                sizeof(g_rx_pass));
      }
      set_state("credentials");
      break;
    }
    case NETWORK_PROV_WIFI_CRED_FAIL: {
      g_rx_ssid[0] = '\0';
      g_rx_pass[0] = '\0';
      g_kick_requested = true;
      const auto *reason = static_cast<const network_prov_wifi_sta_fail_reason_t *>(data);
      if (reason != nullptr && *reason == NETWORK_PROV_WIFI_STA_AUTH_ERROR) {
        set_state("failed", "auth");
      } else {
        set_state("failed", "no_ap");
      }
      break;
    }
    case NETWORK_PROV_WIFI_CRED_SUCCESS:
      if (g_cfg != nullptr && g_rx_ssid[0] != '\0') {
        strlcpy(g_cfg->wifi_ssid, g_rx_ssid, sizeof(g_cfg->wifi_ssid));
        strlcpy(g_cfg->wifi_pass, g_rx_pass, sizeof(g_cfg->wifi_pass));
        g_cfg->wifi_configured = true;
        g_pending_save = true;
      }
      set_state("connected");
      break;
    case NETWORK_PROV_END:
      if (g_state[0] == '\0' || std::strcmp(g_state, "connected") != 0) {
        set_state("idle");
      }
      break;
    case NETWORK_PROV_DEINIT:
      set_state("idle");
      break;
    default:
      break;
  }
}

}  // namespace

namespace prov {

void begin(Config &cfg, void (*on_configured)()) {
  g_cfg = &cfg;
  g_on_configured = on_configured;
  if (!cfg.wifi_configured) {
    start(cfg);
  }
}

void request_start() { g_start_requested = true; }

void start(Config &cfg) {
  g_cfg = &cfg;
  g_start_requested = false;
  if (!g_inited) {
    network_prov_mgr_config_t config = {};
    config.scheme = network_prov_scheme_ble;
    config.scheme_event_handler = NETWORK_PROV_SCHEME_BLE_EVENT_HANDLER_FREE_BTDM;
    config.app_event_handler.event_cb = &prov_event_handler;
    config.app_event_handler.user_data = nullptr;
    const esp_err_t err = network_prov_mgr_init(config);
    if (err != ESP_OK) {
      set_state("failed", "init");
      return;
    }
    g_inited = true;
  }

  const size_t len = std::strlen(cfg.device_id);
  const char *suffix = len > 4 ? cfg.device_id + len - 4 : cfg.device_id;
  snprintf(g_name, sizeof(g_name), "PROV_%s", suffix);

  WiFi.mode(WIFI_STA);
  g_started_ms = millis();
  // Security 1: X25519 + PoP. The PoP is the device id (printed on the label).
  const esp_err_t err = network_prov_mgr_start_provisioning(
      NETWORK_PROV_SECURITY_1, static_cast<const void *>(cfg.device_id),
      g_name, nullptr);
  if (err != ESP_OK) {
    set_state("failed", "start");
    return;
  }
  set_state("started");
}

void stop() {
  if (!g_inited) {
    return;
  }
  network_prov_mgr_stop_provisioning();
  set_state("idle");
}

bool active() {
  return std::strcmp(g_state, "started") == 0 ||
         std::strcmp(g_state, "credentials") == 0;
}

void loop() {
  if (g_start_requested && g_cfg != nullptr) {
    start(*g_cfg);
  }
  if (g_kick_requested && !active()) {
    g_kick_requested = false;
    net::kick();
  }
  if (g_pending_save) {
    g_pending_save = false;
    if (g_on_configured != nullptr) {
      g_on_configured();
    }
  }
  if (g_pending_emit) {
    g_pending_emit = false;
    emit_event();
  }
  if (std::strcmp(g_state, "started") == 0 &&
      millis() - g_started_ms > PROV_TIMEOUT_MS) {
    network_prov_mgr_stop_provisioning();
    set_state("timeout", "timeout");
    net::kick();
  }
}

void fill(WifiInfo &info) { fill_info(info); }

}  // namespace prov

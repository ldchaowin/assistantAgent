#include "net.h"

#include <ESPmDNS.h>
#include <WiFi.h>

#include <cctype>
#include <cstdio>
#include <cstring>

#include "protocol.h"

namespace {

enum class Link : uint8_t { OFF, CONNECTING, CONNECTED, FAILED };

Config *g_cfg = nullptr;
Link g_state = Link::OFF;
WiFiServer server;
WiFiClient client;
bool client_active = false;
bool server_started = false;
bool mdns_started = false;
uint32_t attempt_ms = 0;
uint8_t retries = 0;
char g_err[16] = "";
char rx_line[IO_LINE_MAX];
size_t rx_len = 0;
uint32_t last_client_ms = 0;
void (*line_handler)(const char *line, uint8_t channel) = nullptr;

constexpr uint32_t CLIENT_IDLE_MS = 15UL * 60UL * 1000UL;

const char *state_name(Link state) {
  switch (state) {
    case Link::OFF: return "off";
    case Link::CONNECTING: return "connecting";
    case Link::CONNECTED: return "connected";
    case Link::FAILED: return "failed";
  }
  return "off";
}

void emit_event() {
  const WifiInfo info = net::info();
  proto::wifi_event(info, Serial0);
  if (client_active && client.connected()) {
    proto::wifi_event(info, client);
  }
}

void start_server() {
  if (server_started) {
    return;
  }
  server.begin(g_cfg->tcp_port);
  server.setNoDelay(true);
  server_started = true;

  char host[24];
  snprintf(host, sizeof(host), "%s", g_cfg->device_id);
  for (char *p = host; *p != '\0'; ++p) {
    *p = static_cast<char>(std::tolower(static_cast<unsigned char>(*p)));
  }
  if (MDNS.begin(host)) {
    MDNS.addService("phone-hand", "tcp", g_cfg->tcp_port);
    mdns_started = true;
  }
}

void stop_server() {
  if (server_started) {
    server.end();
    server_started = false;
  }
  if (mdns_started) {
    MDNS.end();
    mdns_started = false;
  }
  if (client_active) {
    client.stop();
    client_active = false;
  }
  rx_len = 0;
}

void drop_client() {
  if (!client_active) {
    return;
  }
  client.stop();
  client_active = false;
  rx_len = 0;
}

void begin_sta() {
  g_err[0] = '\0';
  g_state = Link::CONNECTING;
  retries = 0;
  attempt_ms = millis();
  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);
  WiFi.persistent(false);
  WiFi.begin(g_cfg->wifi_ssid, g_cfg->wifi_pass);
  emit_event();
}

void on_connected() {
  g_state = Link::CONNECTED;
  retries = 0;
  g_err[0] = '\0';
  // The listener may be stale after a WiFi restart (e.g. provisioning):
  // always rebind a fresh one.
  stop_server();
  start_server();
  emit_event();
}

void on_disconnected() {
  stop_server();
  g_state = Link::CONNECTING;
  retries = 0;
  attempt_ms = millis();
  g_err[0] = '\0';
  if (g_cfg != nullptr && g_cfg->wifi_ssid[0] != '\0') {
    WiFi.mode(WIFI_STA);
    WiFi.setSleep(false);
    WiFi.persistent(false);
    WiFi.begin(g_cfg->wifi_ssid, g_cfg->wifi_pass);
  }
  emit_event();
}

void on_attempt_timeout() {
  if (retries < WIFI_MAX_RETRIES) {
    ++retries;
    attempt_ms = millis();
    WiFi.disconnect();
    WiFi.begin(g_cfg->wifi_ssid, g_cfg->wifi_pass);
    emit_event();
    return;
  }
  g_state = Link::FAILED;
  strlcpy(g_err, "timeout", sizeof(g_err));
  emit_event();
}

void handle_client() {
  if (client_active && !client.connected()) {
    drop_client();
  }
  // Last client wins: a new connection takes over a stale one instead of
  // waiting for the 15-minute idle timeout.
  WiFiClient incoming = server.accept();
  if (incoming) {
    if (client_active) {
      drop_client();
    }
    client = incoming;
    client.setNoDelay(true);
    client_active = true;
    rx_len = 0;
    last_client_ms = millis();
  }
  if (!client_active) {
    return;
  }

  while (client.available()) {
    const int c = client.read();
    if (c < 0) {
      break;
    }
    last_client_ms = millis();
    if (c == '\r') {
      continue;
    }
    if (c == '\n') {
      if (rx_len > 0) {
        rx_line[rx_len] = '\0';
        if (line_handler != nullptr) {
          line_handler(rx_line, 1);
        }
        rx_len = 0;
      }
      continue;
    }
    if (rx_len < sizeof(rx_line) - 1) {
      rx_line[rx_len++] = static_cast<char>(c);
    } else {
      rx_len = 0;  // drop oversized line
    }
  }

  if (millis() - last_client_ms > CLIENT_IDLE_MS) {
    drop_client();
  }
}

}  // namespace

namespace net {

void begin(Config &cfg, bool connect_wifi) {
  g_cfg = &cfg;
  if (!connect_wifi || !cfg.wifi_configured || cfg.wifi_ssid[0] == '\0') {
    g_state = Link::OFF;
    WiFi.mode(WIFI_OFF);
    return;
  }
  begin_sta();
}

void loop() {
  if (g_cfg == nullptr || g_cfg->wifi_ssid[0] == '\0') {
    return;
  }

  // BLE provisioning may bring the station up before we ever started STA.
  if ((g_state == Link::OFF || g_state == Link::FAILED) &&
      WiFi.status() == WL_CONNECTED) {
    on_connected();
    return;
  }

  if (g_state == Link::CONNECTING) {
    if (WiFi.status() == WL_CONNECTED) {
      on_connected();
    } else if (millis() - attempt_ms > WIFI_CONNECT_TIMEOUT_MS) {
      on_attempt_timeout();
    }
    return;
  }

  if (g_state == Link::CONNECTED) {
    if (WiFi.status() != WL_CONNECTED) {
      on_disconnected();
      return;
    }
    handle_client();
  }
}

bool connected() { return g_state == Link::CONNECTED; }

void kick() {
  if (g_cfg == nullptr || !g_cfg->wifi_configured ||
      g_cfg->wifi_ssid[0] == '\0') {
    return;
  }
  if (WiFi.status() == WL_CONNECTED) {
    return;
  }
  stop_server();
  g_state = Link::CONNECTING;
  retries = 0;
  attempt_ms = millis();
  g_err[0] = '\0';
  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);
  WiFi.persistent(false);
  WiFi.begin(g_cfg->wifi_ssid, g_cfg->wifi_pass);
  emit_event();
}

bool failed() { return g_state == Link::FAILED; }

WifiInfo info() {
  WifiInfo out;
  out.state = state_name(g_state);
  if (g_cfg != nullptr) {
    strlcpy(out.ssid, g_cfg->wifi_ssid, sizeof(out.ssid));
    out.port = g_cfg->tcp_port;
  }
  if (g_state == Link::CONNECTED) {
    const String ip = WiFi.localIP().toString();
    strlcpy(out.ip, ip.c_str(), sizeof(out.ip));
    const String ssid = WiFi.SSID();
    if (ssid.length() > 0) {
      strlcpy(out.ssid, ssid.c_str(), sizeof(out.ssid));
    }
    out.rssi = WiFi.RSSI();
  }
  strlcpy(out.err, g_err, sizeof(out.err));
  return out;
}

bool set_credentials(Config &cfg, const char *ssid, const char *pass, int port,
                     char *err, size_t err_len) {
  const size_t ssid_len = ssid == nullptr ? 0 : std::strlen(ssid);
  const size_t pass_len = pass == nullptr ? 0 : std::strlen(pass);
  if (ssid_len == 0 || ssid_len > 32) {
    snprintf(err, err_len, "ssid");
    return false;
  }
  if (pass_len > 63 || (pass_len > 0 && pass_len < 8)) {
    snprintf(err, err_len, "pass");
    return false;
  }
  if (port <= 0 || port > 65535) {
    snprintf(err, err_len, "port");
    return false;
  }

  g_cfg = &cfg;
  cfg.wifi_configured = true;
  strlcpy(cfg.wifi_ssid, ssid, sizeof(cfg.wifi_ssid));
  strlcpy(cfg.wifi_pass, pass == nullptr ? "" : pass, sizeof(cfg.wifi_pass));
  cfg.tcp_port = static_cast<uint16_t>(port);

  stop_server();
  WiFi.disconnect(true);
  begin_sta();
  return true;
}

void clear_credentials(Config &cfg) {
  g_cfg = &cfg;
  cfg.wifi_configured = false;
  cfg.wifi_ssid[0] = '\0';
  cfg.wifi_pass[0] = '\0';
  stop_server();
  WiFi.disconnect(true);
  WiFi.mode(WIFI_OFF);
  g_state = Link::OFF;
  g_err[0] = '\0';
  emit_event();
}

Print *client_sink() { return client_active ? &client : nullptr; }

void notify_estop() {
  if (client_active && client.connected()) {
    proto::estop_to(client);
  }
}

void set_line_handler(void (*handler)(const char *line, uint8_t channel)) {
  line_handler = handler;
}

}  // namespace net

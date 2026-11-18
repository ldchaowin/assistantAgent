#include "protocol.h"

#include <ArduinoJson.h>

namespace {

Print *g_out = nullptr;

void write_doc(JsonDocument &doc, Print &out) {
  // Serialize first: Print::write for WiFiClient emits one TCP segment per
  // call, so writing character by character would turn a 200-byte reply into
  // dozens of small packets (measured ~35ms RTT over WiFi vs ~5ms ICMP).
  char buffer[512];
  const size_t len = serializeJson(doc, buffer, sizeof(buffer));
  if (len == 0) {
    return;
  }
  out.write(reinterpret_cast<const uint8_t *>(buffer), len);
  out.write(reinterpret_cast<const uint8_t *>("\r\n"), 2);
}

void send(JsonDocument &doc) { write_doc(doc, *proto::output()); }

void add_prov(JsonDocument &doc, const WifiInfo &wifi) {
  JsonObject obj = doc.createNestedObject("prov");
  obj["state"] = wifi.prov == nullptr ? "idle" : wifi.prov;
  obj["name"] = wifi.prov_name;
  if (wifi.prov_err[0] != '\0') {
    obj["err"] = wifi.prov_err;
  }
}

void add_wifi(JsonDocument &doc, const WifiInfo &wifi) {
  add_prov(doc, wifi);
  JsonObject obj = doc.createNestedObject("wifi");
  obj["state"] = wifi.state == nullptr ? "off" : wifi.state;
  obj["ssid"] = wifi.ssid;
  obj["ip"] = wifi.ip;
  obj["rssi"] = wifi.rssi;
  obj["port"] = wifi.port;
  if (wifi.err[0] != '\0') {
    obj["err"] = wifi.err;
  }
}

}  // namespace

namespace proto {

void set_output(Print *out) { g_out = out; }

Print *output() { return g_out == nullptr ? &Serial0 : g_out; }

void ok(int id, uint32_t elapsed_ms) {
  StaticJsonDocument<128> doc;
  doc["id"] = id;
  doc["ok"] = true;
  doc["t"] = elapsed_ms;
  send(doc);
}

void err(int id, const char *code, const char *message) {
  StaticJsonDocument<192> doc;
  doc["id"] = id;
  doc["ok"] = false;
  doc["err"] = code == nullptr ? "ERROR" : code;
  if (message != nullptr && message[0] != '\0') {
    doc["msg"] = message;
  }
  send(doc);
}

void ready(const Config &cfg, const WifiInfo &wifi) {
  StaticJsonDocument<512> doc;
  doc["evt"] = "ready";
  doc["fw"] = FW_VERSION;
  doc["dev"] = cfg.device_id;
  JsonArray screen = doc.createNestedArray("screen");
  screen.add(cfg.screen_w);
  screen.add(cfg.screen_h);
  JsonArray cal = doc.createNestedArray("cal");
  cal.add(cfg.cal_x0);
  cal.add(cfg.cal_x1);
  cal.add(cfg.cal_y0);
  cal.add(cfg.cal_y1);
#if HAND_HID_TOUCHSCREEN
  doc["hid"] = "touch";
#else
  doc["hid"] = "absmouse";
#endif
  add_wifi(doc, wifi);
  send(doc);
}

void pong(uint32_t ts) {
  StaticJsonDocument<96> doc;
  doc["evt"] = "pong";
  doc["ts"] = ts;
  send(doc);
}

void status(int id, State state, const char *last_error, uint32_t uptime_ms,
            uint32_t queued, bool usb_mounted, const WifiInfo &wifi) {
  StaticJsonDocument<384> doc;
  doc["id"] = id;
  doc["ok"] = true;
  doc["state"] = state_name(state);
  doc["uptime"] = uptime_ms;
  doc["queue"] = queued;
  doc["last_err"] = last_error == nullptr ? "" : last_error;
  doc["usb"] = usb_mounted;
  add_wifi(doc, wifi);
  send(doc);
}

void wifi_status(int id, const WifiInfo &wifi) {
  StaticJsonDocument<320> doc;
  doc["id"] = id;
  doc["ok"] = true;
  add_wifi(doc, wifi);
  send(doc);
}

void wifi_event(const WifiInfo &wifi, Print &out) {
  StaticJsonDocument<320> doc;
  doc["evt"] = "wifi";
  add_wifi(doc, wifi);
  write_doc(doc, out);
}

void prov_event(const WifiInfo &wifi, Print &out) {
  StaticJsonDocument<128> doc;
  doc["evt"] = "prov";
  add_prov(doc, wifi);
  write_doc(doc, out);
}

void estop() {
  StaticJsonDocument<64> doc;
  doc["evt"] = "estop";
  send(doc);
}

void estop_to(Print &out) {
  StaticJsonDocument<64> doc;
  doc["evt"] = "estop";
  write_doc(doc, out);
}

}  // namespace proto

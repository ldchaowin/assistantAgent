#include "storage.h"

#include <cstdio>

void Storage::begin() {
  ready_ = prefs_.begin("hand", false);
  if (!ready_) {
    return;
  }
  cfg_.screen_w = prefs_.getUShort("sw", DEFAULT_SCREEN_W);
  cfg_.screen_h = prefs_.getUShort("sh", DEFAULT_SCREEN_H);
  cfg_.cal_x0 = static_cast<int16_t>(prefs_.getInt("x0", 0));
  cfg_.cal_x1 = static_cast<int16_t>(prefs_.getInt("x1", HID_MAX));
  cfg_.cal_y0 = static_cast<int16_t>(prefs_.getInt("y0", 0));
  cfg_.cal_y1 = static_cast<int16_t>(prefs_.getInt("y1", HID_MAX));

  String dev = prefs_.getString("dev", "");
  if (dev.length() == 0) {
    char buf[16];
    std::snprintf(buf, sizeof(buf), "hand-%04X",
                  static_cast<unsigned>(ESP.getEfuseMac() & 0xFFFF));
    cfg_.device_id[0] = '\0';
    strlcpy(cfg_.device_id, buf, sizeof(cfg_.device_id));
    prefs_.putString("dev", cfg_.device_id);
  } else {
    strlcpy(cfg_.device_id, dev.c_str(), sizeof(cfg_.device_id));
  }

  // Guard against a corrupt/empty NVS.
  if (cfg_.screen_w < 100 || cfg_.screen_h < 100) {
    cfg_.screen_w = DEFAULT_SCREEN_W;
    cfg_.screen_h = DEFAULT_SCREEN_H;
  }

  String ssid = prefs_.getString("ssid", "");
  String pass = prefs_.getString("pass", "");
  strlcpy(cfg_.wifi_ssid, ssid.c_str(), sizeof(cfg_.wifi_ssid));
  strlcpy(cfg_.wifi_pass, pass.c_str(), sizeof(cfg_.wifi_pass));
  cfg_.tcp_port = prefs_.getUShort("port", TCP_DEFAULT_PORT);
  if (cfg_.tcp_port == 0) {
    cfg_.tcp_port = TCP_DEFAULT_PORT;
  }
  // Migration: older firmware stored only ssid/pass.
  cfg_.wifi_configured = prefs_.getBool("wcfg", cfg_.wifi_ssid[0] != '\0');
  cfg_.force_prov = prefs_.getBool("fprov", false);
}

bool Storage::save() {
  if (!ready_) {
    return false;
  }
  prefs_.putUShort("sw", cfg_.screen_w);
  prefs_.putUShort("sh", cfg_.screen_h);
  prefs_.putInt("x0", cfg_.cal_x0);
  prefs_.putInt("x1", cfg_.cal_x1);
  prefs_.putInt("y0", cfg_.cal_y0);
  prefs_.putInt("y1", cfg_.cal_y1);
  prefs_.putString("dev", cfg_.device_id);
  prefs_.putString("ssid", cfg_.wifi_ssid);
  prefs_.putString("pass", cfg_.wifi_pass);
  prefs_.putUShort("port", cfg_.tcp_port);
  prefs_.putBool("wcfg", cfg_.wifi_configured);
  prefs_.putBool("fprov", cfg_.force_prov);
  return true;
}

void Storage::reset() {
  cfg_ = Config();
  if (ready_) {
    prefs_.clear();
    save();
  }
}

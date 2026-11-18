#pragma once

#include "config.h"

// BLE provisioning (Espressif Network Provisioner).
//
// The device advertises as PROV_XXXX (last 4 hex of the device id) when it has
// no stored WiFi credentials, or when the user asks for it with `wifi prov`.
// Credentials are received over BLE (protocomm security 1, PoP = device id),
// stored in NVS and then the normal WiFi/TCP path takes over.
namespace prov {

void begin(Config &cfg, void (*on_configured)());
void loop();

void start(Config &cfg);
void request_start();
void stop();
bool active();

// Copy the provisioning state into a WifiInfo snapshot (hello/status/events).
void fill(WifiInfo &info);

}  // namespace prov

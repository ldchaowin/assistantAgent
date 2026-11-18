#pragma once

#include <Print.h>

#include "config.h"

namespace proto {

// Responses go to the channel that issued the request: UART0 by default, or
// the active TCP client while handling a line received over WiFi.
void set_output(Print *out);
Print *output();

void ok(int id, uint32_t elapsed_ms);
void err(int id, const char *code, const char *message = nullptr);
void ready(const Config &cfg, const WifiInfo &wifi);
void pong(uint32_t ts);
void status(int id, State state, const char *last_error, uint32_t uptime_ms,
            uint32_t queued, bool usb_mounted, const WifiInfo &wifi);
void wifi_status(int id, const WifiInfo &wifi);
void wifi_event(const WifiInfo &wifi, Print &out);
void prov_event(const WifiInfo &wifi, Print &out);
void estop();
void estop_to(Print &out);

}  // namespace proto

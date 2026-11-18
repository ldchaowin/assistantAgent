#pragma once

#include <Arduino.h>

#include "config.h"

// WiFi station link + TCP control channel.
//
// The firmware keeps UART0 as the always-on control channel. When WiFi
// credentials are stored, this module additionally connects to the configured
// 2.4 GHz AP and serves the same JSON-Lines protocol over TCP so the console
// (and `phone serve`) can talk to the hand without a cable.
namespace net {

void begin(Config &cfg, bool connect_wifi = true);
void loop();

bool connected();
bool failed();

// Bring the station back up with the stored credentials (after provisioning).
void kick();
WifiInfo info();

// Validate + apply credentials. Returns false and fills `err` on bad input.
bool set_credentials(Config &cfg, const char *ssid, const char *pass, int port,
                     char *err, size_t err_len);
void clear_credentials(Config &cfg);

// Sink for the current TCP client (nullptr when nobody is connected).
Print *client_sink();

// Called from the main loop when the emergency-stop button fires.
void notify_estop();

// Lines received over TCP are handed to this callback (channel = 1).
void set_line_handler(void (*handler)(const char *line, uint8_t channel));

}  // namespace net

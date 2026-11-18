#pragma once

#include <Arduino.h>
#include <stdint.h>

#define FW_VERSION "0.4.0"

// Control channel: UART0 through the board's UART port or a USB-TTL adapter.
static constexpr uint32_t CONTROL_BAUD = 460800;
// WiFi control channel: JSON Lines over TCP, same framing as UART0.
static constexpr uint16_t TCP_DEFAULT_PORT = 9000;
static constexpr uint32_t WIFI_CONNECT_TIMEOUT_MS = 20000;
// BLE provisioning window: long enough for a user to find the phone and type
// a password without racing the timer.
static constexpr uint32_t PROV_WINDOW_MS = 30UL * 60UL * 1000UL;
static constexpr uint8_t WIFI_MAX_RETRIES = 3;
static constexpr size_t IO_LINE_MAX = 512;
static constexpr uint8_t QUEUE_DEPTH = 8;

// Defaults; overwritten by cfg and persisted with save.
static constexpr uint16_t DEFAULT_SCREEN_W = 1080;
static constexpr uint16_t DEFAULT_SCREEN_H = 2400;
static constexpr int16_t HID_MAX = 32767;

static constexpr uint32_t HEARTBEAT_TIMEOUT_MS = 30000;
static constexpr uint32_t CMD_MIN_INTERVAL_MS = 80;
static constexpr uint16_t TAP_HOLD_DEFAULT_MS = 20;
static constexpr uint16_t TAP_HOLD_MAX_MS = 2000;
static constexpr uint16_t SWIPE_MIN_MS = 50;
static constexpr uint16_t SWIPE_MAX_MS = 5000;
static constexpr size_t TEXT_MAX = 40;

static constexpr uint8_t ESTOP_PIN = 0;
static constexpr uint32_t BOOT_STOP_MS = 2000;

struct Config {
  uint16_t screen_w = DEFAULT_SCREEN_W;
  uint16_t screen_h = DEFAULT_SCREEN_H;
  int16_t cal_x0 = 0;
  int16_t cal_x1 = HID_MAX;
  int16_t cal_y0 = 0;
  int16_t cal_y1 = HID_MAX;
  char device_id[16] = "hand-0000";
  // WiFi STA credentials (empty ssid = WiFi disabled, UART only).
  char wifi_ssid[33] = "";
  char wifi_pass[64] = "";
  uint16_t tcp_port = TCP_DEFAULT_PORT;
  // True after BLE provisioning or a successful serial `wifi set`: suppresses
  // automatic BLE provisioning on the next boot.
  bool wifi_configured = false;
  // Set by `wifi prov` / `wifi clear`: reboot straight into BLE provisioning.
  bool force_prov = false;
};

// Snapshot of the network state reported in hello/status/wifi events.
struct WifiInfo {
  const char *state = "off";
  char ssid[33] = "";
  char ip[16] = "";
  int32_t rssi = 0;
  uint16_t port = TCP_DEFAULT_PORT;
  char err[16] = "";
  // BLE provisioning state: idle / started / credentials / connected / failed
  const char *prov = "idle";
  char prov_name[24] = "";
  char prov_err[16] = "";
};

enum class State : uint8_t {
  INIT = 0,
  READY,
  BUSY,
  FAULT,
};

inline const char *state_name(State s) {
  switch (s) {
    case State::INIT: return "INIT";
    case State::READY: return "READY";
    case State::BUSY: return "BUSY";
    case State::FAULT: return "FAULT";
  }
  return "UNKNOWN";
}

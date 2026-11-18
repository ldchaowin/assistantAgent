#include "motion.h"

#include <USBHIDMouse.h>
#include <esp_task_wdt.h>

#include <algorithm>

namespace {
void feed_watchdog() { esp_task_wdt_reset(); }
}  // namespace

bool Motion::stop_requested() const {
  return stop_flag_ != nullptr && *stop_flag_;
}

bool Motion::in_range(int x, int y) const {
  const Config &cfg = storage_.config();
  return x >= 0 && y >= 0 && x < cfg.screen_w && y < cfg.screen_h;
}

int16_t Motion::map_x(int x) const {
  const Config &cfg = storage_.config();
  int32_t lo = cfg.cal_x0;
  int32_t hi = cfg.cal_x1;
  if (hi < lo) {
    std::swap(lo, hi);
  }
  const int32_t width = cfg.screen_w > 0 ? cfg.screen_w : 1;
  int64_t v = lo + (static_cast<int64_t>(x) * (hi - lo)) / width;
  if (v < 0) v = 0;
  if (v > HID_MAX) v = HID_MAX;
  return static_cast<int16_t>(v);
}

int16_t Motion::map_y(int y) const {
  const Config &cfg = storage_.config();
  int32_t lo = cfg.cal_y0;
  int32_t hi = cfg.cal_y1;
  if (hi < lo) {
    std::swap(lo, hi);
  }
  const int32_t height = cfg.screen_h > 0 ? cfg.screen_h : 1;
  int64_t v = lo + (static_cast<int64_t>(y) * (hi - lo)) / height;
  if (v < 0) v = 0;
  if (v > HID_MAX) v = HID_MAX;
  return static_cast<int16_t>(v);
}

MotionResult Motion::tap(int x, int y, uint16_t hold_ms) {
  if (!in_range(x, y)) {
    return MotionResult::RANGE;
  }
  hold_ms = std::max<uint16_t>(10, std::min<uint16_t>(hold_ms, TAP_HOLD_MAX_MS));

  mouse_.move(map_x(x), map_y(y));
#if HAND_HID_TOUCHSCREEN
  delay(5);
#else
  delay(10);
#endif
  feed_watchdog();
  if (stop_requested()) {
    release_all();
    return MotionResult::ABORTED;
  }

  mouse_.press(MOUSE_LEFT);
  uint16_t remaining = hold_ms;
  while (remaining > 0) {
    const uint16_t step = std::min<uint16_t>(remaining, 20);
    delay(step);
    remaining -= step;
    feed_watchdog();
    if (stop_requested()) {
      release_all();
      return MotionResult::ABORTED;
    }
  }
  mouse_.release(MOUSE_LEFT);
#if HAND_HID_TOUCHSCREEN
  delay(5);
#else
  delay(10);
#endif
  feed_watchdog();
  return MotionResult::OK;
}

MotionResult Motion::move(int x, int y) {
  if (!in_range(x, y)) {
    return MotionResult::RANGE;
  }
  mouse_.move(map_x(x), map_y(y));
  feed_watchdog();
  return MotionResult::OK;
}

MotionResult Motion::swipe(int x1, int y1, int x2, int y2, uint16_t ms) {
  if (!in_range(x1, y1) || !in_range(x2, y2)) {
    return MotionResult::RANGE;
  }
  ms = std::max<uint16_t>(SWIPE_MIN_MS, std::min<uint16_t>(ms, SWIPE_MAX_MS));

  const int16_t sx = map_x(x1);
  const int16_t sy = map_y(y1);
  const int16_t ex = map_x(x2);
  const int16_t ey = map_y(y2);

  mouse_.move(sx, sy);
  delay(30);
  feed_watchdog();
  if (stop_requested()) {
    release_all();
    return MotionResult::ABORTED;
  }
  mouse_.press(MOUSE_LEFT);

  const int steps = std::max(8, static_cast<int>(ms) / 12);
  const int dt = std::max(1, static_cast<int>(ms) / steps);
  for (int i = 1; i <= steps; ++i) {
    const float t = static_cast<float>(i) / static_cast<float>(steps);
    const float e = t * t * (3.0f - 2.0f * t);  // smoothstep
    const int16_t x = static_cast<int16_t>(sx + (ex - sx) * e);
    const int16_t y = static_cast<int16_t>(sy + (ey - sy) * e);
    mouse_.move(x, y);
    delay(dt);
    feed_watchdog();
    if (stop_requested()) {
      release_all();
      return MotionResult::ABORTED;
    }
  }

  mouse_.move(ex, ey);
  delay(30);
  mouse_.release(MOUSE_LEFT);
#if HAND_HID_TOUCHSCREEN
  delay(5);
#else
  delay(10);
#endif
  feed_watchdog();
  return MotionResult::OK;
}

void Motion::release_all() {
  mouse_.releaseAll();
  keyboard_.releaseAll();
}

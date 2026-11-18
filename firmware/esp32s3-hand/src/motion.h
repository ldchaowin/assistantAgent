#pragma once

#include <USBHIDKeyboard.h>

#include "abs_mouse.h"
#include "config.h"
#include "storage.h"

enum class MotionResult : uint8_t {
  OK = 0,
  RANGE,
  ABORTED,
};

class Motion {
 public:
  Motion(AbsMouse &mouse, USBHIDKeyboard &keyboard, Storage &storage)
      : mouse_(mouse), keyboard_(keyboard), storage_(storage) {}

  void set_stop_flag(volatile bool *flag) { stop_flag_ = flag; }
  MotionResult tap(int x, int y, uint16_t hold_ms);
  // Move the pointer without pressing. Used by iOS: absolute-pointer hosts
  // can have edge dead zones, so the console warms the pointer at (0,0) first.
  MotionResult move(int x, int y);
  MotionResult swipe(int x1, int y1, int x2, int y2, uint16_t ms);
  void release_all();

 private:
  int16_t map_x(int x) const;
  int16_t map_y(int y) const;
  bool stop_requested() const;
  bool in_range(int x, int y) const;

  AbsMouse &mouse_;
  USBHIDKeyboard &keyboard_;
  Storage &storage_;
  volatile bool *stop_flag_ = nullptr;
};

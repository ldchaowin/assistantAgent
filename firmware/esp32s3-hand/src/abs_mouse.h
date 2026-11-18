#pragma once

#include <USBHIDMouse.h>

// Pointing device used by Motion.
//
// HAND_HID_TOUCHSCREEN=1 (default): a single-touch digitizer. Android does not
// register the core's absolute mouse (no REL_X/REL_Y), but it does register a
// touchscreen, which gives precise absolute taps without a cursor.
//
// HAND_HID_TOUCHSCREEN=0: the original absolute mouse, kept for hosts that
// support it (for example iOS with AssistiveTouch).
class AbsMouse {
 public:
  void begin();
  void move(int16_t x, int16_t y);
  void press(uint8_t button = MOUSE_LEFT);
  void release(uint8_t button = MOUSE_LEFT);
  void releaseAll();
  void click(uint8_t button = MOUSE_LEFT, uint16_t hold_ms = 30);

 private:
#if HAND_HID_TOUCHSCREEN
  class TouchHID;
  TouchHID *hid_ = nullptr;
  int16_t x_ = 0;
  int16_t y_ = 0;
  bool pressed_ = false;
#else
  USBHIDAbsoluteMouse mouse_;
#endif
};

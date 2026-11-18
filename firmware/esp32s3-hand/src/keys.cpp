#include "keys.h"

#include "config.h"

#include <USBHIDMouse.h>
#include <cctype>
#include <cstring>

namespace {

bool equalsIgnoreCase(const char *a, const char *b) {
  while (*a && *b) {
    if (std::toupper(static_cast<unsigned char>(*a)) !=
        std::toupper(static_cast<unsigned char>(*b))) {
      return false;
    }
    ++a;
    ++b;
  }
  return *a == '\0' && *b == '\0';
}

}  // namespace

bool KeySender::send(const char *code) {
  if (code == nullptr || code[0] == '\0') {
    return false;
  }

#if HAND_HID_TOUCHSCREEN
  // No mouse buttons in touch mode: use Consumer Control AC Home/Back.
  if (equalsIgnoreCase(code, "HOME")) {
    consumer_.press(CONSUMER_CONTROL_HOME);
    delay(25);
    consumer_.release();
    return true;
  }
  if (equalsIgnoreCase(code, "BACK")) {
    consumer_.press(CONSUMER_CONTROL_BACK);
    delay(25);
    consumer_.release();
    return true;
  }
#else
  // Android maps right click to BACK and middle click to HOME.
  if (equalsIgnoreCase(code, "HOME")) {
    mouse_.click(MOUSE_MIDDLE);
    return true;
  }
  if (equalsIgnoreCase(code, "BACK")) {
    mouse_.click(MOUSE_RIGHT);
    return true;
  }
#endif

  uint8_t key = 0;
  if (equalsIgnoreCase(code, "ENTER") || equalsIgnoreCase(code, "RETURN")) {
    key = KEY_RETURN;
  } else if (equalsIgnoreCase(code, "ESC") || equalsIgnoreCase(code, "ESCAPE")) {
    key = KEY_ESC;
  } else if (equalsIgnoreCase(code, "TAB")) {
    key = KEY_TAB;
  } else if (equalsIgnoreCase(code, "SPACE")) {
    key = KEY_SPACE;
  } else if (equalsIgnoreCase(code, "BACKSPACE")) {
    key = KEY_BACKSPACE;
  } else if (equalsIgnoreCase(code, "DELETE") || equalsIgnoreCase(code, "DEL")) {
    key = KEY_DELETE;
  } else if (equalsIgnoreCase(code, "UP")) {
    key = KEY_UP_ARROW;
  } else if (equalsIgnoreCase(code, "DOWN")) {
    key = KEY_DOWN_ARROW;
  } else if (equalsIgnoreCase(code, "LEFT")) {
    key = KEY_LEFT_ARROW;
  } else if (equalsIgnoreCase(code, "RIGHT")) {
    key = KEY_RIGHT_ARROW;
  } else if (equalsIgnoreCase(code, "MENU")) {
    key = KEY_MENU;
  } else if (std::strlen(code) == 1 &&
             std::isprint(static_cast<unsigned char>(code[0]))) {
    key = static_cast<uint8_t>(code[0]);
  }

  if (key == 0) {
    return false;
  }
  keyboard_.press(key);
  delay(25);
  keyboard_.release(key);
  keyboard_.releaseAll();
  return true;
}

bool KeySender::text(const char *text, size_t len) {
  if (text == nullptr || len == 0 || len > TEXT_MAX) {
    return false;
  }
  for (size_t i = 0; i < len; ++i) {
    const uint8_t c = static_cast<uint8_t>(text[i]);
    if (c < 0x20 || c > 0x7E) {
      keyboard_.releaseAll();
      return false;
    }
    if (keyboard_.write(c) == 0) {
      keyboard_.releaseAll();
      return false;
    }
    delay(18);
  }
  keyboard_.releaseAll();
  return true;
}

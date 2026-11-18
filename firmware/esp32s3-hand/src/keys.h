#pragma once

#include <USBHIDConsumerControl.h>
#include <USBHIDKeyboard.h>

#include "abs_mouse.h"

class KeySender {
 public:
  KeySender(AbsMouse &mouse, USBHIDKeyboard &keyboard,
            USBHIDConsumerControl &consumer)
      : mouse_(mouse), keyboard_(keyboard), consumer_(consumer) {}

  bool send(const char *code);
  bool text(const char *text, size_t len);

 private:
  AbsMouse &mouse_;
  USBHIDKeyboard &keyboard_;
  USBHIDConsumerControl &consumer_;
};

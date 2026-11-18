#include "abs_mouse.h"

#include <Arduino.h>

#include "config.h"

#if HAND_HID_TOUCHSCREEN

#include <USBHID.h>

namespace {

// Single-touch digitizer (touch screen), report ID 6 (vendor slot).
static const uint8_t kTouchDescriptor[] = {
    0x05, 0x0D,        // Usage Page (Digitizer)
    0x09, 0x04,        // Usage (Touch Screen)
    0xA1, 0x01,        // Collection (Application)
    0x85, 0x06,        //   Report ID (6)
    0x09, 0x22,        //   Usage (Finger)
    0xA1, 0x02,        //   Collection (Logical)
    0x09, 0x42,        //     Usage (Tip Switch)
    0x15, 0x00,        //     Logical Minimum (0)
    0x25, 0x01,        //     Logical Maximum (1)
    0x75, 0x01,        //     Report Size (1)
    0x95, 0x01,        //     Report Count (1)
    0x81, 0x02,        //     Input (Data,Var,Abs)
    0x95, 0x07,        //     Report Count (7)
    0x81, 0x03,        //     Input (Const,Var,Abs)
    0x09, 0x51,        //     Usage (Contact Identifier)
    0x75, 0x08,        //     Report Size (8)
    0x95, 0x01,        //     Report Count (1)
    0x81, 0x02,        //     Input (Data,Var,Abs)
    0x05, 0x01,        //     Usage Page (Generic Desktop)
    0x09, 0x30,        //     Usage (X)
    0x09, 0x31,        //     Usage (Y)
    0x16, 0x00, 0x00,  //     Logical Minimum (0)
    0x26, 0xFF, 0x7F,  //     Logical Maximum (32767)
    0x75, 0x10,        //     Report Size (16)
    0x95, 0x02,        //     Report Count (2)
    0x81, 0x02,        //     Input (Data,Var,Abs)
    0xC0,              //   End Collection
    0xC0,              // End Collection
};

struct __attribute__((packed)) TouchReport {
  uint8_t buttons;  // bit0 = tip switch
  uint8_t contact;
  uint16_t x;
  uint16_t y;
};

}  // namespace

class AbsMouse::TouchHID : public USBHIDDevice {
 public:
  TouchHID() : hid_(HID_ITF_PROTOCOL_NONE) {
    hid_.addDevice(this, sizeof(kTouchDescriptor));
  }
  void begin() { hid_.begin(); }
  uint16_t _onGetDescriptor(uint8_t *buffer) override {
    memcpy(buffer, kTouchDescriptor, sizeof(kTouchDescriptor));
    return sizeof(kTouchDescriptor);
  }
  void send(bool down, int16_t x, int16_t y) {
    TouchReport report;
    report.buttons = down ? 0x01 : 0x00;
    report.contact = 0;
    report.x = static_cast<uint16_t>(constrain(x, 0, HID_MAX));
    report.y = static_cast<uint16_t>(constrain(y, 0, HID_MAX));
    hid_.SendReport(HID_REPORT_ID_VENDOR, &report, sizeof(report));
  }

 private:
  USBHID hid_;
};

void AbsMouse::begin() {
  if (hid_ == nullptr) {
    hid_ = new TouchHID();
  }
  hid_->begin();
}

void AbsMouse::move(int16_t x, int16_t y) {
  x_ = constrain(x, 0, HID_MAX);
  y_ = constrain(y, 0, HID_MAX);
  if (pressed_) {
    hid_->send(true, x_, y_);
  }
}

void AbsMouse::press(uint8_t) {
  pressed_ = true;
  hid_->send(true, x_, y_);
}

void AbsMouse::release(uint8_t) {
  if (!pressed_) {
    return;
  }
  pressed_ = false;
  hid_->send(false, x_, y_);
}

void AbsMouse::releaseAll() {
  if (pressed_) {
    pressed_ = false;
    hid_->send(false, x_, y_);
  }
}

void AbsMouse::click(uint8_t, uint16_t hold_ms) {
  press();
  delay(hold_ms);
  release();
}

#else  // absolute mouse

void AbsMouse::begin() { mouse_.begin(); }

void AbsMouse::move(int16_t x, int16_t y) {
  mouse_.move(constrain(x, 0, HID_MAX), constrain(y, 0, HID_MAX));
}

void AbsMouse::press(uint8_t button) { mouse_.press(button); }

void AbsMouse::release(uint8_t button) { mouse_.release(button); }

void AbsMouse::releaseAll() { mouse_.buttons(0); }

void AbsMouse::click(uint8_t button, uint16_t hold_ms) {
  mouse_.press(button);
  delay(hold_ms);
  mouse_.release(button);
}

#endif  // HAND_HID_TOUCHSCREEN

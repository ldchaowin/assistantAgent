#pragma once

#include <Preferences.h>

#include "config.h"

class Storage {
 public:
  void begin();
  Config &config() { return cfg_; }
  bool save();
  void reset();

 private:
  Preferences prefs_;
  Config cfg_;
  bool ready_ = false;
};

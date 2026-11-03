#pragma once
#include <cstdint>

namespace intercom::vendor {
// Consumed resident presses swallow the complete stock multi-click group.
// The input thread calls the cutoff hook on press, before stock release grouping.
class ResidentButtonGroup {
 public:
  template<class Cutoff>
  bool Consume(int value, std::uint64_t now_ms, Cutoff&& cutoff) {
    if (swallow_ && !down_ && now_ms >= last_ms_ && now_ms - last_ms_ >= 500) swallow_ = false;
    if (value == 1) { down_ = true; if (cutoff()) swallow_ = true; }
    if (value == 0) down_ = false;
    if (!swallow_) return false;
    last_ms_ = now_ms;
    return true;
  }
 private:
  bool swallow_ = false;
  bool down_ = false;
  std::uint64_t last_ms_ = 0;
};
}  // namespace intercom::vendor

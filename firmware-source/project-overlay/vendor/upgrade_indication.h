#pragma once
// Offline candidate only. Worker owns the controller; main owns LED/player.
#include <array>
#include <cstdint>
#include <mutex>
#include "endpoint_id.h"
namespace intercom::vendor {
struct UpgradeToken {
  Id call{}; std::uint64_t owner = 0, epoch = 0, attempt = 0; std::uint32_t mode = 0;
};
inline bool SameUpgrade(const UpgradeToken& a, const UpgradeToken& b) {
  return a.call == b.call && a.owner == b.owner && a.epoch == b.epoch &&
         a.attempt == b.attempt && a.mode == b.mode;
}
class UpgradeIndication {
 public:
  enum class Event { None, Start, Cleanup };
  struct Message { Event event = Event::None; UpgradeToken token{}; };
  struct Result { bool started = false, output = false, led = false, complete = false; };
  bool Begin(const UpgradeToken& t) {
    std::lock_guard<std::mutex> lock(mu_);
    if (!t.owner || !t.epoch || !t.attempt || active_ || cleanup_) return false;
    token_=t; active_=pending_=true; result_={}; return true;
  }
  void Cancel(const UpgradeToken& t) {
    std::lock_guard<std::mutex> lock(mu_);
    if (active_ && SameUpgrade(t,token_)) { active_=pending_=false; cleanup_=true; result_={}; }
  }
  Message Take() {
    std::lock_guard<std::mutex> lock(mu_);
    if (cleanup_) return {Event::Cleanup,token_};
    if (active_ && pending_) { pending_=false; return {Event::Start,token_}; }
    return {};
  }
  bool Valid(const UpgradeToken& t) {
    std::lock_guard<std::mutex> lock(mu_);
    return active_ && !cleanup_ && SameUpgrade(t,token_);
  }
  void Retry(const UpgradeToken& t) {
    std::lock_guard<std::mutex> lock(mu_);
    if (active_ && !cleanup_ && SameUpgrade(t,token_)) pending_ = true;
  }
  void Started(const UpgradeToken& t, bool output, bool led) {
    std::lock_guard<std::mutex> lock(mu_);
    if (active_ && SameUpgrade(t,token_)) { result_.started=true; result_.output=output; result_.led=led; }
  }
  void Completed(const UpgradeToken& t) {
    std::lock_guard<std::mutex> lock(mu_);
    if (active_ && SameUpgrade(t,token_)) result_.complete=true;
  }
  Result Read(const UpgradeToken& t) {
    std::lock_guard<std::mutex> lock(mu_);
    return active_ && SameUpgrade(t,token_) ? result_ : Result{};
  }
  void Commit(const UpgradeToken& t) {
    std::lock_guard<std::mutex> lock(mu_);
    if (active_ && SameUpgrade(t,token_)) { active_=pending_=false; result_={}; }
  }
  void Cleaned(const UpgradeToken& t) {
    std::lock_guard<std::mutex> lock(mu_);
    if (cleanup_ && SameUpgrade(t,token_)) { cleanup_=false; token_={}; }
  }
 private:
  std::mutex mu_; UpgradeToken token_{}; Result result_{};
  bool active_=false, pending_=false, cleanup_=false;
};
} // namespace intercom::vendor

#pragma once
// OFFLINE HA-authority candidate. Rate, durable floor/generation and boot ID
// are supplied enrollment premises; this file proves no protected storage.
#include <atomic>
#include "intercom/endpoint_id.h"
#include "intercom/endpoint_frame.h"
#include <cstdint>
#include <memory>
#include <mutex>
#include <time.h>

namespace intercom {
struct UtcInterval { std::uint64_t low=0, high=0; };
inline bool ClockAdd(std::uint64_t a,std::uint64_t b,std::uint64_t& out) {
  if(b>UINT64_MAX-a)return false;
  out=a+b;return true;
}
inline bool BootUs(std::uint64_t& out) {
  timespec t{};
  if(clock_gettime(CLOCK_BOOTTIME,&t)!=0||t.tv_sec<0||t.tv_nsec<0||t.tv_nsec>=1000000000||
     static_cast<std::uint64_t>(t.tv_sec)>(UINT64_MAX-999999)/1000000)return false;
  out=static_cast<std::uint64_t>(t.tv_sec)*1000000+static_cast<std::uint64_t>(t.tv_nsec)/1000;return true;
}
inline bool ElapsedBounds(std::uint64_t d,std::uint64_t rate,UtcInterval& out) {
  if(rate>1000000000)return false;
  std::uint64_t margin=0;
  if(!ClockAdd(d/1000000000*rate,((d%1000000000)*rate+999999999)/1000000000,margin)||
     !ClockAdd(margin,1,margin)||!ClockAdd(d,margin,out.high))return false;
  out.low=d>margin?d-margin:0;return true;
}
class HaClockSource;
class HaCredentialEpoch;
class HaClockSnapshot final {
 public:
  HaClockSnapshot(const HaClockSnapshot&)=delete;
  HaClockSnapshot& operator=(const HaClockSnapshot&)=delete;
  void Revoke() const {alive_->store(false);}
  bool Read(UtcInterval& out,std::uint64_t& now) const {
    if(!alive_->load()||!Numeric(out,now)) {alive_->store(false);return false;}return true;
  }
 private:
  friend class HaClockSource;friend class HaCredentialEpoch;
  HaClockSnapshot(UtcInterval interval,std::uint64_t anchor,std::uint64_t rate,
                  std::uint64_t generation,const Id& boot,
                  std::shared_ptr<std::atomic<bool>> alive)
      :interval_(interval),anchor_(anchor),rate_(rate),generation_(generation),boot_(boot),alive_(std::move(alive)){}
  bool Numeric(UtcInterval& out,std::uint64_t& now) const {
    UtcInterval elapsed;
    return BootUs(now)&&now>=anchor_&&ElapsedBounds(now-anchor_,rate_,elapsed)&&
      ClockAdd(interval_.low,elapsed.low,out.low)&&ClockAdd(interval_.high,elapsed.high,out.high);
  }
  const UtcInterval interval_;
  const std::uint64_t anchor_,rate_,generation_;
  const Id boot_;
  const std::shared_ptr<std::atomic<bool>> alive_;
};
class HaClockSource final {
 public:
  // Floor/generation must come from the approved physical enrollment transaction.
  HaClockSource(std::uint64_t rate,std::uint64_t floor,std::uint64_t generation,const Id& boot)
      :rate_(rate),floor_(floor),generation_(generation),boot_(boot){}
  ~HaClockSource(){Close();}
  HaClockSource(const HaClockSource&)=delete;
  HaClockSource& operator=(const HaClockSource&)=delete;
  // Pinned-SSH observation: the device BOOTTIME sample lies inside the HA UTC bracket.
  bool Attest(UtcInterval utc,std::uint64_t anchor) {
    std::uint64_t now=0;
    if(used_||!alive_->load()||rate_!=1000000||!floor_||!generation_||IsZero(boot_.data(),boot_.size())||
       !utc.low||utc.low<floor_||utc.high<utc.low||utc.high-utc.low>120000000||
       !BootUs(now)||now<anchor||now-anchor>120000000)return Fail();
    used_=true;
    snapshot_=std::shared_ptr<const HaClockSnapshot>(new HaClockSnapshot(utc,anchor,rate_,generation_,boot_,alive_));
    return true;
  }
  bool Begin() {
    if(used_||!alive_->load()||rate_>1000000000||!floor_||!generation_||IsZero(boot_.data(),boot_.size())||!BootUs(sent_))return Fail();
    used_=pending_=true;return true;
  }
  bool Complete(std::uint32_t epoch) {
    std::uint64_t now=0,high=0;UtcInterval elapsed;
    const std::uint64_t low=static_cast<std::uint64_t>(epoch)*1000000;
    if(!pending_||!alive_->load()||!epoch||low<floor_||!BootUs(now)||now<sent_||
       !ElapsedBounds(now-sent_,rate_,elapsed)||!ClockAdd(low,1000000,high)||!ClockAdd(high,elapsed.high,high))return Fail();
    pending_=false;
    snapshot_=std::shared_ptr<const HaClockSnapshot>(new HaClockSnapshot({low,high},now,rate_,generation_,boot_,alive_));
    return true;
  }
  std::shared_ptr<const HaClockSnapshot> Snapshot() const{return snapshot_;}
  void Close(){alive_->store(false);}
 private:
  bool Fail(){Close();pending_=false;return false;}
  const std::uint64_t rate_,floor_,generation_;
  const Id boot_;
  std::uint64_t sent_=0;bool used_=false,pending_=false;
  std::shared_ptr<std::atomic<bool>> alive_=std::make_shared<std::atomic<bool>>(true);
  std::shared_ptr<const HaClockSnapshot> snapshot_;
};

// One holder per loaded, activated credential generation/boot, retained across
// reconnects. New sessions cannot replace its first numerical expiry horizon.
class HaCredentialEpoch final {
 public:
  HaCredentialEpoch(std::uint64_t generation,const Id& boot):generation_(generation),boot_(boot){}
  bool Check(const std::shared_ptr<const HaClockSnapshot>& sample,std::uint64_t starts,std::uint64_t expires,
             UtcInterval& current) {
    std::lock_guard<std::mutex> lock(mutex_);std::uint64_t now=0;
    if(expired_)return false;
    // An unavailable native source denies this connection but does not renew or
    // discard the retained numerical horizon for a later native reconnection.
    if(!sample||!sample->Read(current,now))return false;
    if(sample->generation_!=generation_||sample->boot_!=boot_||!generation_||IsZero(boot_.data(),boot_.size())||
       now<last_||starts>=expires)return Fail();
    if(!first_){first_=sample;starts_=starts;expires_=expires;}
    else { // Newly discovered chain certificates only narrow the horizon.
      if(starts>starts_)starts_=starts;
      if(expires<expires_)expires_=expires;
      UtcInterval original;std::uint64_t original_now=0;
      if(!first_->Numeric(original,original_now)||
         original.low<starts_||original.high>=expires_||
         current.high<original.low||current.low>original.high)return Fail();
    }
    if(current.low<starts_||current.high>=expires_)return Fail();
    last_=now;return true;
  }
 private:
  bool Fail(){expired_=true;return false;}
  const std::uint64_t generation_;
  const Id boot_;
  std::mutex mutex_;bool expired_=false;std::uint64_t starts_=0,expires_=0,last_=0;
  std::shared_ptr<const HaClockSnapshot> first_;
};
} // namespace intercom

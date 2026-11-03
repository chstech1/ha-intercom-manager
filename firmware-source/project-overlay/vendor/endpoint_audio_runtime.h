#pragma once

#include "intercom/endpoint_controller.h"
#include "intercom/endpoint_tls.h"

#include <atomic>
#include <cstdint>
#include <memory>
#include <string>

namespace lva::audio { class LibMpvPlayer; class PcmRingBuffer; class WebRtcProcessor; }
namespace lva::satellite { class Satellite; }

namespace intercom::vendor {

struct RuntimeConfig {
  bool enabled = false;
  unsigned profile = 3;
  Id favorite{};
  bool local_commands_enabled = false;
  std::string bind_address;
  std::uint16_t port = 6054;
  TlsConfig tls;
  std::shared_ptr<HaClockSource> clock_source;
  Id device{};
  std::string sink;
  double sink_volume = 1.0;
  std::string beep_file;
  std::uint32_t aec_tail_ms = 160;
  std::uint32_t capture_source_age_us = 20000;
};

bool LoadRuntimeConfig(const std::string& path, RuntimeConfig& out,
                       std::string& error);

class EndpointAudioRuntime {
 public:
  EndpointAudioRuntime(RuntimeConfig config, lva::audio::PcmRingBuffer& tap,
                       const std::atomic<bool>& muted,
                       lva::audio::WebRtcProcessor& processor,
                       lva::audio::LibMpvPlayer& announce,
                       lva::satellite::Satellite& satellite,
                       bool capture_running, bool aec_configured);
  ~EndpointAudioRuntime();
  EndpointAudioRuntime(const EndpointAudioRuntime&) = delete;
  EndpointAudioRuntime& operator=(const EndpointAudioRuntime&) = delete;

  bool Start();
  void Stop();
  int MainLoopFd() const;
  void DrainMainLoop();
  // Input-thread safe. Closes the local gate before notifying the worker.
  bool ResidentButtonPress();
  bool LocalWake();
  void SetLocalWakeAvailable(bool available);
  const std::atomic<bool>& OutputActive() const;

 private:
  struct Impl;
  std::unique_ptr<Impl> impl_;
};

}  // namespace intercom::vendor

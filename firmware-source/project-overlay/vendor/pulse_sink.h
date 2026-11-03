#pragma once

#include <array>
#include <cstdint>
#include <string>

#include <pulse/def.h>

struct pa_context;
struct pa_mainloop;
struct pa_operation;
struct pa_sink_info;
struct pa_sink_input_info;
struct pa_stream;

namespace intercom::vendor {

class PulseSink {
 public:
  PulseSink(const std::string& device, double stream_volume);
  ~PulseSink();
  PulseSink(const PulseSink&) = delete;
  PulseSink& operator=(const PulseSink&) = delete;

  void Service();
  bool Ready() const;
  bool CanWrite() const;
  bool Write(const std::array<std::uint8_t, 1024>& pcm);
  bool Progressed();
  bool Failed() const { return failed_; }
  bool FlushPending() const { return flush_pending_; }
  std::uint64_t Underruns() const { return underruns_; }
  std::uint64_t LatencyUs() const { return latency_us_; }
  bool LatencyValid() const { return latency_valid_; }
  bool SinkUsable() const;
  pa_buffer_attr BufferAttributes() const;
  void Flush();
  bool SetGainPercent(unsigned percent) {
    if (percent < 5 || percent > 100) return false;
    gain_percent_ = percent; return true;
  }

 private:
  static void ContextState(pa_context*, void* self);
  static void StreamState(pa_stream*, void* self);
  static void Timing(pa_stream*, int success, void* self);
  static void Underflow(pa_stream*, void* self);
  static void FlushDone(pa_stream*, int success, void* self);
  static void SinkInfo(pa_context*, const pa_sink_info*, int eol, void* self);
  static void SinkInputInfo(pa_context*, const pa_sink_input_info*, int eol, void* self);
  void ConnectStream();

  std::string device_;
  double stream_volume_ = 0.5;
  pa_mainloop* loop_ = nullptr;
  pa_context* context_ = nullptr;
  pa_stream* stream_ = nullptr;
  std::uint64_t read_index_ = 0;
  std::uint64_t baseline_index_ = 0;
  std::uint64_t submitted_bytes_ = 0;
  std::uint64_t reported_rendered_bytes_ = 0;
  std::uint64_t latency_us_ = 0;
  bool failed_ = false;
  bool timing_pending_ = false;
  bool flush_pending_ = false;
  bool latency_valid_ = false;
  bool baseline_pending_ = true;
  bool timing_render_valid_ = false;
  bool primed_ = false;
  pa_operation* timing_operation_ = nullptr;
  pa_operation* sink_info_operation_ = nullptr;
  pa_operation* sink_input_info_operation_ = nullptr;
  std::uint64_t sink_info_us_ = 0;
  std::uint64_t sink_input_info_us_ = 0;
  std::uint64_t next_sink_info_us_ = 0;
  bool sink_usable_ = false;
  bool sink_input_usable_ = false;
  std::uint64_t underruns_ = 0;
  unsigned gain_percent_ = 100;
};

}  // namespace intercom::vendor

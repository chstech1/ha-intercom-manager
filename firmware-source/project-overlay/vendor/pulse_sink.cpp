#include "intercom/pulse_sink.h"
#include "intercom/manager_volume.h"

#include <pulse/pulseaudio.h>

#include <algorithm>
#include <chrono>

namespace intercom::vendor {

namespace {
std::uint64_t NowUs() {
  return static_cast<std::uint64_t>(std::chrono::duration_cast<std::chrono::microseconds>(
      std::chrono::steady_clock::now().time_since_epoch()).count());
}
bool HasWritableBudget(pa_stream* stream) {
  const std::size_t requested = pa_stream_writable_size(stream);
  // Pulse permits a write larger than its requested budget. One fixed 1024-byte
  // frame can therefore overshoot by less than 1024 bytes; maxlength is 16384.
  return requested != static_cast<std::size_t>(-1) && requested != 0;
}
}

PulseSink::PulseSink(const std::string& device, double stream_volume)
    : device_(device), stream_volume_(stream_volume) {
  loop_ = pa_mainloop_new();
  if (!loop_) { failed_ = true; return; }
  context_ = pa_context_new(pa_mainloop_get_api(loop_), "intercom_m3_poc");
  if (!context_) { failed_ = true; return; }
  pa_context_set_state_callback(context_, &ContextState, this);
  if (pa_context_connect(context_, nullptr, PA_CONTEXT_NOFLAGS, nullptr) < 0)
    failed_ = true;
}

PulseSink::~PulseSink() {
  if (timing_operation_) { pa_operation_cancel(timing_operation_); pa_operation_unref(timing_operation_); }
  if (sink_info_operation_) { pa_operation_cancel(sink_info_operation_); pa_operation_unref(sink_info_operation_); }
  if (sink_input_info_operation_) {
    pa_operation_cancel(sink_input_info_operation_);
    pa_operation_unref(sink_input_info_operation_);
  }
  if (stream_) { pa_stream_disconnect(stream_); pa_stream_unref(stream_); }
  if (context_) { pa_context_disconnect(context_); pa_context_unref(context_); }
  if (loop_) pa_mainloop_free(loop_);
}

void PulseSink::ContextState(pa_context* context, void* opaque) {
  auto* self = static_cast<PulseSink*>(opaque);
  const pa_context_state_t state = pa_context_get_state(context);
  if (state == PA_CONTEXT_READY) self->ConnectStream();
  else if (state == PA_CONTEXT_FAILED || state == PA_CONTEXT_TERMINATED)
    self->failed_ = true;
}

void PulseSink::ConnectStream() {
  if (stream_ || failed_) return;
  const pa_sample_spec spec{PA_SAMPLE_S16LE, 16000, 1};
  stream_ = pa_stream_new(context_, "intercom_m3_poc", &spec, nullptr);
  if (!stream_) { failed_ = true; return; }
  pa_stream_set_state_callback(stream_, &StreamState, this);
  pa_stream_set_underflow_callback(stream_, &Underflow, this);
  pa_buffer_attr attr{};
  attr.maxlength = 16384;
  attr.tlength = 8192;   // 256 ms native jitter target at 32 kB/s.
  attr.prebuf = UINT32_MAX;  // Let Pulse derive prebuffering from the target.
  attr.minreq = 1024;
  attr.fragsize = UINT32_MAX;
  const char* device = device_.empty() ? nullptr : device_.c_str();
  pa_cvolume volume{};
  pa_cvolume_set(&volume, 1, static_cast<pa_volume_t>(
      std::clamp(stream_volume_, 0.05, 1.0) * PA_VOLUME_NORM));
  if (pa_stream_connect_playback(stream_, device, &attr,
          static_cast<pa_stream_flags_t>(PA_STREAM_AUTO_TIMING_UPDATE |
                                         PA_STREAM_INTERPOLATE_TIMING),
          &volume, nullptr) < 0) failed_ = true;
}

void PulseSink::StreamState(pa_stream* stream, void* opaque) {
  auto* self = static_cast<PulseSink*>(opaque);
  const pa_stream_state_t state = pa_stream_get_state(stream);
  if (state == PA_STREAM_FAILED || state == PA_STREAM_TERMINATED)
    self->failed_ = true;
}

void PulseSink::Service() {
  if (!loop_ || failed_) return;
  int result = 0;
  if (pa_mainloop_iterate(loop_, 0, &result) < 0) { failed_ = true; return; }
  if (Ready() && !primed_ && HasWritableBudget(stream_)) {
    static const std::array<std::uint8_t, 1024> silence{};
    if (pa_stream_write(stream_, silence.data(), silence.size(), nullptr, 0,
                        PA_SEEK_RELATIVE) < 0)
      failed_ = true;
    else primed_ = true;  // Bounded startup prime; never credited as call progress.
  }
  if (Ready() && !timing_pending_) {
    timing_pending_ = true;
    timing_operation_ = pa_stream_update_timing_info(stream_, &Timing, this);
    if (!timing_operation_) timing_pending_ = false;
  }
  const std::uint64_t now = NowUs();
  if (Ready() && now >= next_sink_info_us_) {
    const char* selected = pa_stream_get_device_name(stream_);
    if (selected && !sink_info_operation_) sink_info_operation_ =
        pa_context_get_sink_info_by_name(context_, selected, &SinkInfo, this);
    const std::uint32_t stream_index = pa_stream_get_index(stream_);
    if (!sink_input_info_operation_ && stream_index != PA_INVALID_INDEX)
      sink_input_info_operation_ = pa_context_get_sink_input_info(
          context_, stream_index, &SinkInputInfo, this);
    next_sink_info_us_ = now + 250000;
  }
}

void PulseSink::Underflow(pa_stream*, void* opaque) {
  ++static_cast<PulseSink*>(opaque)->underruns_;
}

bool PulseSink::Ready() const {
  return stream_ && pa_stream_get_state(stream_) == PA_STREAM_READY && !failed_;
}

bool PulseSink::CanWrite() const {
  return Ready() && SinkUsable() && !flush_pending_ && latency_valid_ && !baseline_pending_ &&
         HasWritableBudget(stream_);
}

bool PulseSink::SinkUsable() const {
  const std::uint64_t now = NowUs();
  return sink_usable_ && sink_input_usable_ && sink_info_us_ != 0 &&
         sink_input_info_us_ != 0 && now >= sink_info_us_ &&
         now >= sink_input_info_us_ && now - sink_info_us_ < 2000000 &&
         now - sink_input_info_us_ < 2000000;
}

pa_buffer_attr PulseSink::BufferAttributes() const {
  const pa_buffer_attr* attributes = Ready() ? pa_stream_get_buffer_attr(stream_) : nullptr;
  return attributes ? *attributes : pa_buffer_attr{};
}

void PulseSink::SinkInputInfo(pa_context*, const pa_sink_input_info* info,
                              int eol, void* opaque) {
  auto* self = static_cast<PulseSink*>(opaque);
  if (info) {
    self->sink_input_usable_ = !info->mute && !info->corked &&
                               pa_cvolume_max(&info->volume) > PA_VOLUME_MUTED;
    self->sink_input_info_us_ = NowUs();
  }
  if (eol && self->sink_input_info_operation_) {
    pa_operation_unref(self->sink_input_info_operation_);
    self->sink_input_info_operation_ = nullptr;
  }
}

void PulseSink::SinkInfo(pa_context*, const pa_sink_info* info, int eol, void* opaque) {
  auto* self = static_cast<PulseSink*>(opaque);
  if (info) {
    self->sink_usable_ = !info->mute && info->state != PA_SINK_SUSPENDED &&
                         pa_cvolume_max(&info->volume) > PA_VOLUME_MUTED;
    self->sink_info_us_ = NowUs();
  }
  if (eol && self->sink_info_operation_) {
    pa_operation_unref(self->sink_info_operation_);
    self->sink_info_operation_ = nullptr;
  }
}

bool PulseSink::Write(const std::array<std::uint8_t, 1024>& pcm) {
  if (!CanWrite()) return false;
  const auto scaled = ApplyStreamGain(pcm, gain_percent_);
  if (pa_stream_write(stream_, scaled.data(), scaled.size(), nullptr, 0,
                      PA_SEEK_RELATIVE) < 0) {
    failed_ = true; return false;
  }
  submitted_bytes_ += pcm.size();
  return true;
}

void PulseSink::Timing(pa_stream* stream, int success, void* opaque) {
  auto* self = static_cast<PulseSink*>(opaque);
  if (self->timing_operation_) {
    pa_operation_unref(self->timing_operation_);
    self->timing_operation_ = nullptr;
  }
  self->timing_pending_ = false;
  if (!success || self->flush_pending_) return;
  const pa_timing_info* info = pa_stream_get_timing_info(stream);
  if (info && info->read_index >= 0) {
    self->read_index_ = static_cast<std::uint64_t>(info->read_index);
    if (self->baseline_pending_) {
      self->baseline_index_ = self->read_index_;
      self->reported_rendered_bytes_ = 0;
      self->baseline_pending_ = false;
    }
  }
  pa_usec_t latency = 0;
  int negative = 0;
  if (pa_stream_get_latency(stream, &latency, &negative) == 0) {
    self->latency_us_ = negative ? 0 : latency;
    self->latency_valid_ = !negative;
  }
  const auto* spec = pa_stream_get_sample_spec(stream);
  const std::uint64_t since_underrun_us = info && info->since_underrun >= 0 && spec
      ? pa_bytes_to_usec(static_cast<std::uint64_t>(info->since_underrun), spec) : 0;
  self->timing_render_valid_ = info && info->playing && !info->read_index_corrupt &&
      info->since_underrun >= 0 && since_underrun_us > info->sink_usec;
}

bool PulseSink::Progressed() {
  if (!timing_render_valid_ || baseline_pending_ || read_index_ <= baseline_index_) return false;
  const std::uint64_t rendered = std::min(read_index_ - baseline_index_, submitted_bytes_);
  if (rendered <= reported_rendered_bytes_) return false;
  reported_rendered_bytes_ = rendered;
  return true;
}

void PulseSink::Flush() {
  if (!Ready() || flush_pending_) return;
  if (timing_operation_) {
    pa_operation_cancel(timing_operation_);
    pa_operation_unref(timing_operation_);
    timing_operation_ = nullptr;
    timing_pending_ = false;
  }
  flush_pending_ = true;
  latency_valid_ = false;
  read_index_ = baseline_index_ = submitted_bytes_ = reported_rendered_bytes_ = 0;
  timing_render_valid_ = false;
  primed_ = false;
  pa_operation* op = pa_stream_flush(stream_, &FlushDone, this);
  if (op) pa_operation_unref(op); else { flush_pending_ = false; failed_ = true; }
}

void PulseSink::FlushDone(pa_stream*, int success, void* opaque) {
  auto* self = static_cast<PulseSink*>(opaque);
  self->read_index_ = self->baseline_index_ = 0;
  self->submitted_bytes_ = self->reported_rendered_bytes_ = 0;
  self->timing_render_valid_ = false;
  self->primed_ = false;
  self->baseline_pending_ = true;
  self->latency_valid_ = false;
  self->flush_pending_ = false;
  if (!success) self->failed_ = true;
}

}  // namespace intercom::vendor

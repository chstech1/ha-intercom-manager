#include "intercom/endpoint_audio_runtime.h"
#include "intercom/boot_clock.h"

#include "audio/LibMpvPlayer.h"
#include "audio/PcmRingBuffer.h"
#include "audio/WebRtcProcessor.h"
#include "intercom/capture_accumulator.h"
#include "intercom/endpoint_vendor.h"
#include "intercom/upgrade_indication.h"
#include "intercom/pulse_sink.h"
#include "satellite/Satellite.h"
#include "tr/LedRing.h"
#include "util/Log.h"

#include <arpa/inet.h>
#include <fcntl.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <sys/eventfd.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <unistd.h>

#include <nlohmann/json.hpp>

#include <algorithm>
#include <array>
#include <chrono>
#include <condition_variable>
#include <fstream>
#include <limits>
#include <mutex>
#include <thread>

namespace intercom::vendor {
namespace {
constexpr const char* kTag = "intercom";

bool HexId(const std::string& text, Id& id) {
  std::string hex;
  for (char c : text) if (c != '-') hex.push_back(c);
  if (hex.size() != 32) return false;
  for (std::size_t i = 0; i < id.size(); ++i) {
    const auto digit = [](char c) -> int {
      if (c >= '0' && c <= '9') return c - '0';
      if (c >= 'a' && c <= 'f') return c - 'a' + 10;
      if (c >= 'A' && c <= 'F') return c - 'A' + 10;
      return -1;
    };
    const int hi = digit(hex[2 * i]), lo = digit(hex[2 * i + 1]);
    if (hi < 0 || lo < 0) return false;
    id[i] = static_cast<std::uint8_t>((hi << 4) | lo);
  }
  return !IsZero(id.data(), id.size());
}

bool LocalFile(const std::string& value) {
  return !value.empty() && value[0] == '/' && value.find("://") == std::string::npos;
}

bool SafeRootFile(const std::string& value) {
  struct stat st{};
  return LocalFile(value) && stat(value.c_str(), &st) == 0 && S_ISREG(st.st_mode) &&
         st.st_uid == 0 && (st.st_mode & 0022) == 0;
}

int Nonblocking(int fd) {
  const int flags = fcntl(fd, F_GETFL, 0);
  return flags < 0 || fcntl(fd, F_SETFL, flags | O_NONBLOCK) < 0 ? -1 : fd;
}

int LowLatency(int fd) {
  const int one = 1;
  return setsockopt(fd, IPPROTO_TCP, TCP_NODELAY, &one, sizeof(one));
}

std::uint32_t PcmPeak(const std::array<std::uint8_t, 1024>& pcm) {
  std::uint32_t peak = 0;
  for (std::size_t i = 0; i < pcm.size(); i += 2) {
    const std::uint16_t word = static_cast<std::uint16_t>(pcm[i]) |
        static_cast<std::uint16_t>(static_cast<std::uint16_t>(pcm[i + 1]) << 8);
    const std::uint32_t magnitude = (word & 0x8000u)
        ? static_cast<std::uint32_t>((~word + 1u) & 0xffffu) : word;
    peak = std::max(peak, magnitude);
  }
  return peak;
}

class UniqueFd {
 public:
  UniqueFd() = default;
  explicit UniqueFd(int fd) : fd_(fd) {}
  ~UniqueFd() { Reset(); }
  UniqueFd(const UniqueFd&) = delete;
  UniqueFd& operator=(const UniqueFd&) = delete;
  int Get() const { return fd_; }
  int Release() { const int fd = fd_; fd_ = -1; return fd; }
  explicit operator bool() const { return fd_ >= 0; }
  void Reset(int fd = -1) {
    if (fd_ >= 0) close(fd_);
    fd_ = fd;
  }
 private:
  int fd_ = -1;
};
}  // namespace

bool LoadRuntimeConfig(const std::string& path, RuntimeConfig& out,
                       std::string& error) {
  out = {};
  struct stat st{};
  if (stat(path.c_str(), &st) != 0) { error = "config unavailable"; return false; }
  if (!S_ISREG(st.st_mode) || st.st_uid != 0 || (st.st_mode & 0022) != 0) {
    error = "config must be a root-owned regular file without group/world write";
    return false;
  }
  try {
    std::ifstream input(path);
    const std::string config_bytes{std::istreambuf_iterator<char>(input),{}};
    const auto j = nlohmann::json::parse(config_bytes);
    out.enabled = j.at("enabled").get<bool>();
    out.profile = j.value("profile", 3u);
    out.local_commands_enabled = j.value("local_commands_enabled", false);
    if (out.local_commands_enabled && out.profile != 4)
      throw std::runtime_error("local commands require ICM4");
    if (out.profile != 3 && out.profile != 4) throw std::runtime_error("unsupported endpoint profile");
    if (j.contains("favorite_device_uuid") &&
        (out.profile != 4 || !HexId(j.at("favorite_device_uuid").get<std::string>(), out.favorite)))
      throw std::runtime_error("invalid favorite device/profile");
    out.bind_address = j.value("bind_address", "0.0.0.0");
    const unsigned port = j.value("port", 6054u);
    if (port == 0 || port > 65535) throw std::runtime_error("invalid port");
    out.port = static_cast<std::uint16_t>(port);
    out.tls.ca_file = j.at("ca_file").get<std::string>();
    out.tls.certificate_file = j.at("certificate_file").get<std::string>();
    out.tls.key_file = j.at("key_file").get<std::string>();
    out.tls.peer_uri = j.at("peer_uri").get<std::string>();
    out.tls.peer_spki_sha256 = j.at("peer_spki_sha256").get<std::string>();
    out.tls.trusted_wall_clock = false;
    out.sink = j.value("sink", "");
    out.sink_volume = j.value("sink_volume", 1.0);
    out.beep_file = j.at("beep_file").get<std::string>();
    out.aec_tail_ms = j.value("aec_tail_ms", 160u);
    out.capture_source_age_us = j.value("capture_source_age_us", 20000u);
    if (!HexId(j.at("device_uuid").get<std::string>(), out.device) ||
        !SafeRootFile(out.tls.ca_file) || !SafeRootFile(out.tls.certificate_file) ||
        !SafeRootFile(out.tls.key_file) || !SafeRootFile(out.beep_file) ||
        out.tls.peer_uri.empty() || out.tls.peer_spki_sha256.empty() ||
        out.aec_tail_ms > 2000 || out.capture_source_age_us >= 200000 ||
        out.sink_volume < 0.05 || out.sink_volume > 1.0)
      throw std::runtime_error("invalid identity, path, pin, or AEC tail");
    if(out.enabled && !LoadBootClock(j,config_bytes,out.tls,out.clock_source,error))
      throw std::runtime_error("HA boot clock admission unavailable");
  } catch (const std::exception& ex) {
    out = {}; error = ex.what(); return false;
  }
  if (!out.enabled) { error = "disabled"; return false; }
  return true;
}

struct EndpointAudioRuntime::Impl {
  struct Token { Id call{}; std::uint64_t owner = 0; std::uint32_t mode = 0; std::uint64_t epoch = 0; };
  enum class Event { None, Acquire, Release };

  RuntimeConfig config;
  lva::audio::PcmRingBuffer& tap;
  const std::atomic<bool>& muted;
  lva::audio::WebRtcProcessor& processor;
  lva::audio::LibMpvPlayer& announce;
  lva::satellite::Satellite& satellite;
  bool capture_running;
  bool aec_configured;
  int listener = -1;
  int wake = -1;
  std::thread worker;
  std::atomic<bool> stop{false};
  std::atomic<bool> output_active{false};
  std::mutex output_mutex;
  bool chime_active = false;
  UpgradeToken chime_token{};
  std::atomic<bool> resident_engaged{false};
  std::atomic<bool> resident_cutoff{false};
  std::atomic<bool> resident_accept{false};
  std::atomic<bool> resident_ringing{false};
  std::atomic<bool> resident_source_ready{false};
  std::atomic<bool> resident_source_requested{false};
  std::atomic<bool> local_wake_available{false};
  std::atomic<bool> local_session_commands{false};
  std::atomic<bool> local_wake_requested{false};
  std::atomic<bool> local_wake_paused{false};
  std::mutex local_wake_mutex;
  std::mutex mailbox_mutex;
  std::mutex lifecycle_mutex;
  Event event = Event::None;
  Token event_token{};
  StreamVolume event_volume{};
  bool completion = false;
  Token completion_token{};
  bool started = false;
  bool started_output = false;
  bool started_led = false;
  Token started_token{};
  bool main_owned = false;
  Token main_token{};
  struct VolumeDelivery { Token token{}; StreamVolume value{}; } volume_delivery;
  bool volume_main_pending = false, volume_done = false, volume_ok = false;
  Id boot{};
  std::atomic<bool> worker_failed{false};
  std::atomic<bool> release_pending{false}, acquire_waiting{false}, upgrade_waiting{false};
  UpgradeToken prepared_upgrade{};
  UpgradeIndication upgrade;

  void CloseListener() {
    std::lock_guard<std::mutex> lock(lifecycle_mutex);
    if (listener >= 0) { close(listener); listener = -1; }
  }

  Impl(RuntimeConfig c, lva::audio::PcmRingBuffer& t,
       const std::atomic<bool>& m, lva::audio::WebRtcProcessor& p,
       lva::audio::LibMpvPlayer& a, lva::satellite::Satellite& s,
       bool running, bool aec)
      : config(std::move(c)), tap(t), muted(m), processor(p), announce(a),
        satellite(s), capture_running(running), aec_configured(aec) {}

  void Post(Event next) { Post(next, Token{}); }
  void Post(Event next, const Token& token, const StreamVolume& volume = {}) {
    { std::lock_guard<std::mutex> lock(mailbox_mutex); event = next; event_token = token; event_volume = volume; }
    const std::uint64_t one = 1;
    if (wake >= 0) { const auto written = write(wake, &one, sizeof(one)); (void)written; }
  }

  void Complete(const Token& token) {
    std::lock_guard<std::mutex> lock(mailbox_mutex);
    completion = true; completion_token = token;
  }

  void Started(const Token& token, bool output, bool led) {
    std::lock_guard<std::mutex> lock(mailbox_mutex);
    started = true; started_output = output; started_led = led; started_token = token;
  }

  void SetOutputActive(bool active) {
    std::lock_guard<std::mutex> lock(output_mutex);
    output_active.store(active, std::memory_order_release);
    processor.SetEndpointOutputActive(active || chime_active);
  }
  void SetChimeActive(const UpgradeToken& token) {
    std::lock_guard<std::mutex> lock(output_mutex);
    chime_token = token; chime_active = true;
    processor.SetEndpointOutputActive(true);
  }
  void ClearChime(const UpgradeToken& token) {
    std::lock_guard<std::mutex> lock(output_mutex);
    if (!chime_active || !SameUpgrade(chime_token, token)) return;
    chime_active = false; chime_token = {};
    processor.SetEndpointOutputActive(output_active.load(std::memory_order_acquire));
  }
  void ClearAllChimes() {
    std::lock_guard<std::mutex> lock(output_mutex);
    chime_active = false; chime_token = {};
    processor.SetEndpointOutputActive(output_active.load(std::memory_order_acquire));
  }
  void ClearEndpointOutput() { SetOutputActive(false); }

  void Run() {
    std::unique_ptr<PulseSink> sink(new PulseSink(config.sink, config.sink_volume));
    CaptureAccumulator accumulator;
    std::unique_ptr<EndpointAdapter> adapter;
    UniqueFd client;
    Token indicated{}, active{};
    std::uint32_t generation = 0;
    std::uint32_t negotiated_capabilities = 0;
    std::uint64_t connection_epoch = 0, upgrade_began_at = 0, chime_tail_until = 0, indication_retry_at = 0;
    UpgradeToken upgrade_token{}, chime_tail_token{};
    bool upgrade_started = false;
    bool owned = false;
    std::uint64_t tail_until = 0;
    std::uint64_t seen_underruns = 0;
    std::uint64_t ref_frames = processor.ReferenceFramesProcessed();
    std::uint64_t ref_progress_us = 0;
    std::uint64_t capture_progress_us = 0;
    std::uint64_t output_progress_us = 0;
    std::uint64_t next_status_us = 0;
    std::uint64_t next_sink_retry_us = 0;
    bool chime_complete = false;
    bool led_confirmed = false;
    std::uint64_t tap_drops = tap.DroppedSamples();
    Token pending_began{}, pending_done{};
    std::uint64_t scheduled_volume_revision = 0;
    unsigned applied_gain_maximum = 100;
    bool began_pending = false, began_output = false, began_led = false, done_pending = false;
    std::uint64_t capture_overruns = 0;
    struct CallDiagnostics {
      std::uint64_t rx_taken = 0, drop_latency = 0, write_ok = 0, write_fail = 0;
      std::uint64_t rendered_progress = 0, max_latency_us = 0, blocked_queued_loops = 0;
      std::uint64_t blocked_latency_invalid = 0, blocked_sink_unusable = 0;
      std::uint64_t blocked_not_ready = 0, blocked_flush_pending = 0;
      std::uint64_t blocked_unclassified = 0, loop_gap_max_us = 0;
      std::uint64_t rx_interarrival_max_us = 0, last_loop_us = 0, last_rx_accept_us = 0;
      std::uint32_t rx_media_next = 0;
      std::uint32_t pcm_peak = 0;
    } diagnostics;
    const auto log_diagnostics = [&diagnostics, &sink] {
      const pa_buffer_attr buffer = sink->BufferAttributes();
      LVA_LOGI(kTag,
          "intercom sink summary rx_taken=%llu drop_latency=%llu write_ok=%llu "
          "write_fail=%llu rendered_progress=%llu pcm_peak=%u max_latency_us=%llu "
          "blocked_queued_loops=%llu blocked_latency_invalid=%llu "
          "blocked_sink_unusable=%llu blocked_not_ready=%llu "
          "blocked_flush_pending=%llu blocked_unclassified=%llu "
          "loop_gap_max_us=%llu rx_interarrival_max_us=%llu "
          "buffer_tlength=%u buffer_prebuf=%u buffer_minreq=%u",
          static_cast<unsigned long long>(diagnostics.rx_taken),
          static_cast<unsigned long long>(diagnostics.drop_latency),
          static_cast<unsigned long long>(diagnostics.write_ok),
          static_cast<unsigned long long>(diagnostics.write_fail),
          static_cast<unsigned long long>(diagnostics.rendered_progress), diagnostics.pcm_peak,
          static_cast<unsigned long long>(diagnostics.max_latency_us),
          static_cast<unsigned long long>(diagnostics.blocked_queued_loops),
          static_cast<unsigned long long>(diagnostics.blocked_latency_invalid),
          static_cast<unsigned long long>(diagnostics.blocked_sink_unusable),
          static_cast<unsigned long long>(diagnostics.blocked_not_ready),
          static_cast<unsigned long long>(diagnostics.blocked_flush_pending),
          static_cast<unsigned long long>(diagnostics.blocked_unclassified),
          static_cast<unsigned long long>(diagnostics.loop_gap_max_us),
          static_cast<unsigned long long>(diagnostics.rx_interarrival_max_us),
          buffer.tlength, buffer.prebuf, buffer.minreq);
    };
    std::array<std::int16_t, 2048> capture{};

    while (!stop.load(std::memory_order_relaxed)) {
      std::uint64_t now = MonotonicUs();
      sink->Service();
      const std::uint64_t current_ref_frames = processor.ReferenceFramesProcessed();
      if (current_ref_frames != ref_frames) {
        ref_frames = current_ref_frames;
        ref_progress_us = now;
      }
      if (sink->Underruns() != seen_underruns) {
        seen_underruns = sink->Underruns();
        LVA_LOGW(kTag, "PulseAudio underrun count=%llu",
                 static_cast<unsigned long long>(seen_underruns));
      }
      if (!owned && sink->Failed() && now >= next_sink_retry_us) {
        sink.reset(new PulseSink(config.sink, config.sink_volume));
        seen_underruns = 0;
        next_sink_retry_us = Deadline(now, 1000000);
        LVA_LOGI(kTag, "recreated idle PulseAudio sink after failure");
      }
      if (!adapter) {
        sockaddr_storage address{}; socklen_t length = sizeof(address);
        UniqueFd incoming(accept(listener, reinterpret_cast<sockaddr*>(&address), &length));
        if (incoming && Nonblocking(incoming.Get()) >= 0 && LowLatency(incoming.Get()) == 0) {
          if (connection_epoch == UINT64_MAX) { stop.store(true); continue; }
          ++connection_epoch;
          client.Reset(incoming.Release());
          const std::uint32_t capabilities =
              (!IsZero(config.favorite.data(), config.favorite.size()) ? 64u : 0u) |
              (local_wake_available.load(std::memory_order_acquire) ? 128u : 0u) | 256u;
          negotiated_capabilities = capabilities;
          adapter.reset(new EndpointAdapter(client.Get(), config.tls, config.device, boot, now,
                                           config.profile, &resident_cutoff, &local_wake_paused, capabilities, &muted));
        }
      }

      EndpointController* core = adapter && adapter->Session() ? adapter->Session()->Core() : nullptr;
      // Privacy ordering: local policy precedes every adapter/network service.
      if (core && resident_cutoff.load(std::memory_order_acquire)) core->LocalStop(now);
      if (core && resident_accept.exchange(false, std::memory_order_acq_rel)) core->LocalAccept(now);
      if (resident_source_requested.exchange(false, std::memory_order_acq_rel) && core) {
        if (core->OwnsAudio()) core->LocalStop(now);
        else core->LocalSourceRequest(config.favorite, now);
      }
      {
        std::lock_guard<std::mutex> lock(local_wake_mutex);
        if (local_wake_requested.exchange(false, std::memory_order_acq_rel) && core)
          core->LocalWake(now);
      }
      if (core) core->LocalPolicy(muted.load(std::memory_order_acquire), false, false, now);
      if (adapter && !adapter->Service(now)) {
        adapter.reset(); client.Reset(); core = nullptr;
      }
      core = adapter && adapter->Session() ? adapter->Session()->Core() : nullptr;
      local_session_commands.store(core && (negotiated_capabilities & 128u), std::memory_order_release);

      if (core && core->ModeGeneration() != generation) {
        generation = core->ModeGeneration(); accumulator.Clear(); sink->Flush();
        while (tap.Read(capture.data(), capture.size()) != 0) {}
        output_progress_us = 0;
      }
      if (core && core->CommandHolding()) {
        const bool capture_recent = capture_progress_us && now - capture_progress_us < 500000;
        const bool reference_recent = ref_progress_us && now - ref_progress_us < 500000;
        if (!capture_running || !aec_configured || !capture_recent || !reference_recent)
          core->LocalStop(now);
        else if (!sink->FlushPending()) core->CommandFlushed(now);
      }
      {
        std::lock_guard<std::mutex> lock(local_wake_mutex);
        if (!local_wake_requested.load(std::memory_order_acquire))
          local_wake_paused.store(core && core->CommandHolding(), std::memory_order_release);
      }
      if (core && core->OwnsAudio() && !owned) {
        resident_engaged.store(true, std::memory_order_release);
        owned = true; output_progress_us = 0; diagnostics = {};
        diagnostics.last_loop_us = now; diagnostics.last_rx_accept_us = now;
        diagnostics.rx_media_next = core->RxMediaNext();
        active = {core->Call(), core->Owner(), core->ModeGeneration()};
      } else if ((!core || !core->OwnsAudio()) && owned) {
        resident_engaged.store(false, std::memory_order_release);
        log_diagnostics();
        owned = false; output_progress_us = 0; accumulator.Clear(); sink->Flush();
        ClearEndpointOutput();
        sink->SetGainPercent(100); scheduled_volume_revision = 0; applied_gain_maximum = 100;
        release_pending.store(true, std::memory_order_release);
        Post(Event::Release); indicated = {}; chime_complete = false; led_confirmed = false;
      }
      if (!owned) resident_cutoff.store(false, std::memory_order_release);
      resident_ringing.store(core && core->WaitingForAnswer(), std::memory_order_release);
      resident_source_ready.store(core && core->CanRequestSource(now) &&
          !IsZero(config.favorite.data(), config.favorite.size()), std::memory_order_release);
      {
        std::lock_guard<std::mutex> lock(mailbox_mutex);
        if (volume_done && !sink->FlushPending()) {
          if (core) core->VolumeApplied(volume_delivery.token.call, volume_delivery.token.owner,
              volume_delivery.token.mode, volume_delivery.value.revision, volume_ok, now);
          volume_done = false; scheduled_volume_revision = 0;
        }
        if (core && core->VolumePending() && core->State() != EndpointState::Indicating &&
            !volume_main_pending && !volume_done &&
            scheduled_volume_revision != core->DesiredVolume().revision) {
          const auto& value = core->DesiredVolume();
          if (value.maximum < applied_gain_maximum) sink->Flush();
          applied_gain_maximum = value.maximum;
          sink->SetGainPercent(value.level); // clamps output before the asynchronous chime ACK
          volume_delivery = {{core->Call(),core->Owner(),core->ModeGeneration()},value};
          scheduled_volume_revision = value.revision; volume_main_pending = true;
          const std::uint64_t one = 1;
          if (wake >= 0) { const auto written = write(wake, &one, sizeof(one)); (void)written; }
        }
      }

      if (owned && core) {
        if (now >= diagnostics.last_loop_us)
          diagnostics.loop_gap_max_us = std::max(
              diagnostics.loop_gap_max_us, now - diagnostics.last_loop_us);
        diagnostics.last_loop_us = now;
        const std::uint32_t current_rx_media = core->RxMediaNext();
        if (current_rx_media != diagnostics.rx_media_next) {
          if (now >= diagnostics.last_rx_accept_us)
            diagnostics.rx_interarrival_max_us = std::max(
                diagnostics.rx_interarrival_max_us, now - diagnostics.last_rx_accept_us);
          diagnostics.last_rx_accept_us = now;
          diagnostics.rx_media_next = current_rx_media;
        }
      }

      if (core && core->State() == EndpointState::Indicating &&
          !release_pending.load(std::memory_order_acquire) &&
          (core->Call() != indicated.call || core->Owner() != indicated.owner)) {
        indicated = {core->Call(), core->Owner(), core->ModeGeneration(), connection_epoch};
        sink->SetGainPercent(core->DesiredVolume().level);
        Post(Event::Acquire, indicated, core->DesiredVolume());
      }
      // Each accepted upgrade has a local attempt id and a runtime connection
      // epoch. The controller remains speaker-only until indication completes.
      const bool upgrading = core && core->OwnsAudio() && core->State() == EndpointState::Upgrade;
      const UpgradeToken current_upgrade = upgrading
          ? UpgradeToken{core->Call(), core->Owner(), connection_epoch, core->UpgradeAttempt(), core->ModeGeneration()}
          : UpgradeToken{};
      if (upgrade_token.attempt && (!upgrading || !SameUpgrade(upgrade_token, current_upgrade))) {
        upgrade.Cancel(upgrade_token);
        const std::uint64_t one = 1; if (wake >= 0) (void)write(wake, &one, sizeof(one));
        upgrade_token = {}; upgrade_started = false; led_confirmed = false; chime_complete = false;
      }
      if (upgrading && !upgrade_token.attempt && !release_pending.load(std::memory_order_acquire) &&
          upgrade.Begin(current_upgrade)) {
        upgrade_token = current_upgrade; upgrade_started = false; upgrade_began_at = now;
        led_confirmed = false; chime_complete = false;
        const std::uint64_t one = 1; if (wake >= 0) (void)write(wake, &one, sizeof(one));
      }
      if (upgrade_token.attempt && core && core->State() == EndpointState::Upgrade &&
          core->UpgradeAttempt() == upgrade_token.attempt && core->Call() == upgrade_token.call &&
          core->Owner() == upgrade_token.owner && connection_epoch == upgrade_token.epoch &&
          core->ModeGeneration() == upgrade_token.mode) {
        const auto result = upgrade.Read(upgrade_token);
        if (result.started && !upgrade_started) {
          const bool physical_mute = muted.load(std::memory_order_acquire);
          core->LocalPolicy(physical_mute, false, false, now);
          const bool capture_recent = capture_running && capture_progress_us > upgrade_began_at && now - capture_progress_us < 500000;
          const bool reference_recent = aec_configured && ref_progress_us > upgrade_began_at && now - ref_progress_us < 250000;
          if (!physical_mute && !resident_cutoff.load(std::memory_order_acquire) &&
              core->State() == EndpointState::Upgrade &&
              (!capture_running || !aec_configured || !result.output || !result.led ||
               (capture_recent && reference_recent && sink->SinkUsable()))) {
            led_confirmed = core->Indication(upgrade_token.call, upgrade_token.owner, upgrade_token.mode,
                result.led && !muted.load(std::memory_order_acquire), false,
                result.output && sink->SinkUsable(), capture_recent, reference_recent, now);
            upgrade_started = true;
          }
        }
        if (result.complete && upgrade_started && core->State() == EndpointState::Upgrade &&
            upgrade.Valid(upgrade_token)) {
          const bool physical_mute = muted.load(std::memory_order_acquire);
          core->LocalPolicy(physical_mute, false, false, now);
          const bool capture_recent = capture_running && capture_progress_us > upgrade_began_at && now - capture_progress_us < 500000;
          const bool reference_recent = aec_configured && ref_progress_us > upgrade_began_at && now - ref_progress_us < 250000;
          if (!physical_mute && !resident_cutoff.load(std::memory_order_acquire) &&
              core->State() == EndpointState::Upgrade) {
            chime_complete = core->Indication(upgrade_token.call, upgrade_token.owner, upgrade_token.mode,
                led_confirmed && !muted.load(std::memory_order_acquire), true,
                result.output && sink->SinkUsable(), capture_recent, reference_recent, now);
            if (chime_complete && core->State() == EndpointState::Full) {
              generation = core->ModeGeneration(); accumulator.Clear(); sink->Flush();
              while (tap.Read(capture.data(), capture.size()) != 0) {}
              output_progress_us = 0; capture_progress_us = 0;
              chime_tail_token = upgrade_token;
              chime_tail_until = Deadline(now, static_cast<std::uint64_t>(config.aec_tail_ms) * 1000);
              upgrade.Commit(upgrade_token); upgrade_token = {}; upgrade_started = false;
            }
          }
        }
      }
      {
        std::lock_guard<std::mutex> lock(mailbox_mutex);
        if (started) {
          pending_began = started_token; began_output = started_output;
          began_led = started_led;
          started = false; began_pending = true;
        }
        if (completion) { pending_done = completion_token; completion = false; done_pending = true; }
      }
      const auto current_initial = [&](const Token& token) {
        return core && core->OwnsAudio() && token.epoch == connection_epoch &&
            token.call == core->Call() && token.owner == core->Owner() &&
            token.mode == core->ModeGeneration() &&
            (core->State() == EndpointState::Indicating || core->State() == EndpointState::Ringing);
      };
      if (began_pending && !current_initial(pending_began)) began_pending = false;
      if (done_pending && !current_initial(pending_done)) done_pending = false;
      if (began_pending && core) {
        const bool capture_recent = capture_progress_us && now - capture_progress_us < 500000;
        const bool reference_recent = aec_configured && ref_progress_us &&
                                      now - ref_progress_us < 500000;
        const bool known_full_unavailable = core->Mode() == 1 &&
            (!capture_running || !aec_configured);
        if (known_full_unavailable || !began_output || (sink->SinkUsable() &&
            (core->Mode() != 1 || (capture_recent && reference_recent)))) {
          led_confirmed = core->Indication(
              pending_began.call, pending_began.owner, pending_began.mode,
              began_led, false, began_output && sink->SinkUsable(),
              capture_recent, reference_recent, now);
          began_pending = false;
        }
      }
      if (done_pending && !began_pending && core) {
        const std::uint64_t capture_age_us = capture_progress_us ? now - capture_progress_us : 0;
        const std::uint64_t reference_age_us = ref_progress_us ? now - ref_progress_us : 0;
        const bool capture_recent = capture_progress_us && capture_age_us < 500000;
        const bool reference_recent = aec_configured && ref_progress_us != 0 &&
                                      reference_age_us < 250000;
        constexpr auto kMaxLogAge = static_cast<std::uint64_t>(
            std::numeric_limits<long long>::max());
        const long long capture_age_log = capture_progress_us
            ? static_cast<long long>(std::min(capture_age_us, kMaxLogAge)) : -1;
        const long long reference_age_log = ref_progress_us
            ? static_cast<long long>(std::min(reference_age_us, kMaxLogAge)) : -1;
        const bool sink_usable = sink->SinkUsable();
        const auto state_before = core->State();
        chime_complete = core->Indication(pending_done.call, pending_done.owner, pending_done.mode,
            led_confirmed, true, began_output && sink_usable,
            capture_recent, reference_recent, now);
        LVA_LOGI(kTag,
            "indication completion accepted=%u state=%u->%u capture_age_us=%lld "
            "reference_age_us=%lld sink=%u output=%u led=%u",
            chime_complete ? 1u : 0u, static_cast<unsigned>(state_before),
            static_cast<unsigned>(core->State()),
            capture_age_log, reference_age_log, sink_usable ? 1u : 0u,
            began_output ? 1u : 0u, led_confirmed ? 1u : 0u);
        done_pending = false;
        const UpgradeToken completed_chime{pending_done.call, pending_done.owner, pending_done.epoch, 0, pending_done.mode};
        if (chime_complete) { chime_tail_token = completed_chime; chime_tail_until = Deadline(now, static_cast<std::uint64_t>(config.aec_tail_ms) * 1000); }
        else ClearChime(completed_chime);
      }

      const std::size_t backlog = tap.Size();
      const std::uint64_t current_tap_drops = tap.DroppedSamples();
      if (current_tap_drops != tap_drops || backlog > tap.Capacity() - 160) {
        while (tap.Read(capture.data(), capture.size()) != 0) {}
        accumulator.Clear();
        const std::uint64_t delta = current_tap_drops - tap_drops;
        capture_overruns += delta ? delta : 1;
        tap_drops = current_tap_drops;
        LVA_LOGW(kTag, "intercom capture discontinuity count=%llu",
                 static_cast<unsigned long long>(capture_overruns));
        std::this_thread::sleep_for(std::chrono::milliseconds(4));
        continue;
      }
      const std::size_t available = std::min(backlog, capture.size());
      const std::size_t read_count = tap.Read(capture.data(), available);
      if (read_count != 0) capture_progress_us = now;
      const std::uint64_t span = static_cast<std::uint64_t>(backlog) * 1000000 / 16000;
      const std::uint64_t total_age = span + config.capture_source_age_us;
      const std::uint64_t oldest = now > total_age ? now - total_age : 0;
      const bool mic_allowed = core && (core->MicAllowed() || core->CommandReady()) &&
                               (!local_wake_paused.load(std::memory_order_acquire) || core->CommandReady()) &&
                               !resident_cutoff.load(std::memory_order_acquire) &&
                               !muted.load(std::memory_order_acquire);
      accumulator.Push(capture.data(), read_count, oldest,
                       core ? core->ModeGeneration() : 0, mic_allowed,
                       [&](const std::array<std::uint8_t,1024>& pcm,
                           std::uint32_t gen, std::uint64_t stamp) {
        now = MonotonicUs();
        if (core && !resident_cutoff.load(std::memory_order_acquire) &&
            (!local_wake_paused.load(std::memory_order_acquire) || core->CommandReady()) &&
            !muted.load(std::memory_order_acquire) && now >= stamp) {
          if (core->CommandReady()) core->CaptureCommand(pcm, gen, now - stamp, now);
          else core->Capture(pcm, gen, now - stamp, now);
        }
      });

      if (core && sink->LatencyValid())
        diagnostics.max_latency_us = std::max(diagnostics.max_latency_us, sink->LatencyUs());
      if (core && !sink->CanWrite() && core->RxQueued() != 0) {
        ++diagnostics.blocked_queued_loops;
        if (!sink->LatencyValid()) ++diagnostics.blocked_latency_invalid;
        if (!sink->SinkUsable()) ++diagnostics.blocked_sink_unusable;
        if (!sink->Ready()) ++diagnostics.blocked_not_ready;
        if (sink->FlushPending()) ++diagnostics.blocked_flush_pending;
        if (sink->LatencyValid() && sink->SinkUsable() && sink->Ready() &&
            !sink->FlushPending()) ++diagnostics.blocked_unclassified;
      } else if (core && sink->CanWrite()) {
        PcmQueue<6>::Batch batch;
        if (core->TakeRx(now, batch) && batch.mode == core->ModeGeneration()) {
          ++diagnostics.rx_taken;
          diagnostics.pcm_peak = std::max(diagnostics.pcm_peak, PcmPeak(batch.pcm));
          if (!sink->LatencyValid() ||
              Deadline(now, sink->LatencyUs() + 32000) >= batch.capture_deadline) {
            ++diagnostics.drop_latency;
          } else if (!sink->Write(batch.pcm)) {
            ++diagnostics.write_fail;
          } else {
            ++diagnostics.write_ok;
            active = {core->Call(), core->Owner(), batch.mode};
            SetOutputActive(true);
            tail_until = Deadline(now, Deadline(sink->LatencyUs(), 32000 +
                static_cast<std::uint64_t>(config.aec_tail_ms) * 1000));
          }
        }
      }
      if (core && sink->Progressed()) {
        ++diagnostics.rendered_progress;
        output_progress_us = now;
        core->OutputProgress(active.call, active.owner, active.mode, true, now);
      }
      if (chime_tail_token.owner && now >= chime_tail_until) {
        ClearChime(chime_tail_token); chime_tail_token = {}; chime_tail_until = 0;
      }
      if (output_active.load(std::memory_order_relaxed) && now >= tail_until) {
        ClearEndpointOutput();
      }
      if (sink->Failed() && core && core->OwnsAudio()) core->LocalStop(now);
      if (core && (core->State() == EndpointState::Full ||
                   core->State() == EndpointState::Speaker ||
                   core->State() == EndpointState::Upgrade) &&
          !sink->SinkUsable()) core->LocalStop(now);
      if (core && (core->OwnsAudio() || config.profile == 4) && now >= next_status_us) {
        std::uint16_t health = 0;
        if (output_progress_us && now - output_progress_us < 500000) health |= 1u;
        if (capture_progress_us && now - capture_progress_us < 500000) health |= 2u;
        if (led_confirmed) health |= 4u;
        if (chime_complete) health |= 8u;
        if (aec_configured && ref_progress_us && now - ref_progress_us < 500000) health |= 16u;
        core->ReportStatus(health, now);
        next_status_us = Deadline(now, 250000);
      }
      if ((acquire_waiting.load(std::memory_order_acquire) || upgrade_waiting.load(std::memory_order_acquire)) && now >= indication_retry_at) {
        const std::uint64_t one = 1; if (wake >= 0) (void)write(wake, &one, sizeof(one));
        indication_retry_at = Deadline(now, 50000);
      }
      std::this_thread::sleep_for(std::chrono::milliseconds(4));
    }
    if (owned) log_diagnostics();
    resident_engaged.store(false, std::memory_order_release);
    resident_source_ready.store(false, std::memory_order_release);
    local_session_commands.store(false, std::memory_order_release);
    sink->Flush();
    adapter.reset();
    client.Reset();
    ClearEndpointOutput();
    if (upgrade_token.attempt) upgrade.Cancel(upgrade_token);
    if (owned) { release_pending.store(true, std::memory_order_release); Post(Event::Release); }
  }
};

EndpointAudioRuntime::EndpointAudioRuntime(RuntimeConfig config,
    lva::audio::PcmRingBuffer& tap, const std::atomic<bool>& muted,
    lva::audio::WebRtcProcessor& processor, lva::audio::LibMpvPlayer& announce,
    lva::satellite::Satellite& satellite, bool capture_running, bool aec_configured)
    : impl_(new Impl(std::move(config), tap, muted, processor, announce,
                     satellite, capture_running, aec_configured)) {}

EndpointAudioRuntime::~EndpointAudioRuntime() { Stop(); }

bool EndpointAudioRuntime::Start() {
  if (!SecureRandom(impl_->boot.data(), impl_->boot.size())) return false;
  impl_->wake = eventfd(0, EFD_CLOEXEC | EFD_NONBLOCK);
  impl_->listener = socket(AF_INET, SOCK_STREAM | SOCK_CLOEXEC, 0);
  if (impl_->wake < 0 || impl_->listener < 0 || Nonblocking(impl_->listener) < 0) return false;
  int one = 1; setsockopt(impl_->listener, SOL_SOCKET, SO_REUSEADDR, &one, sizeof(one));
  sockaddr_in address{}; address.sin_family = AF_INET; address.sin_port = htons(impl_->config.port);
  if (inet_pton(AF_INET, impl_->config.bind_address.c_str(), &address.sin_addr) != 1 ||
      bind(impl_->listener, reinterpret_cast<sockaddr*>(&address), sizeof(address)) < 0 ||
      listen(impl_->listener, 1) < 0) return false;
  try {
    impl_->worker = std::thread([this] {
      try { impl_->Run(); }
      catch (const std::exception& ex) {
        impl_->worker_failed.store(true, std::memory_order_release);
        impl_->ClearEndpointOutput();
        LVA_LOGE(kTag, "intercom worker failed closed: %s", ex.what());
        impl_->Post(Impl::Event::Release);
        impl_->CloseListener();
      } catch (...) {
        impl_->worker_failed.store(true, std::memory_order_release);
        impl_->ClearEndpointOutput();
        LVA_LOGE(kTag, "intercom worker failed closed");
        impl_->Post(Impl::Event::Release);
        impl_->CloseListener();
      }
    });
  } catch (...) { return false; }
  return true;
}

void EndpointAudioRuntime::Stop() {
  if (!impl_) return;
  impl_->stop.store(true, std::memory_order_relaxed);
  if (impl_->worker.joinable()) impl_->worker.join();
  impl_->CloseListener();
  if (impl_->wake >= 0) { close(impl_->wake); impl_->wake = -1; }
}

int EndpointAudioRuntime::MainLoopFd() const { return impl_->wake; }
const std::atomic<bool>& EndpointAudioRuntime::OutputActive() const { return impl_->output_active; }

bool EndpointAudioRuntime::ResidentButtonPress() {
  if (!impl_->resident_engaged.load(std::memory_order_acquire)) {
    if (!impl_->resident_source_ready.load(std::memory_order_acquire)) return false;
    impl_->resident_source_requested.store(true, std::memory_order_release);
    return true;
  }
  if (impl_->resident_ringing.load(std::memory_order_acquire)) {
    impl_->resident_accept.store(true, std::memory_order_release);
    return true;
  }
  impl_->resident_cutoff.store(true, std::memory_order_release);
  return true;
}

void EndpointAudioRuntime::SetLocalWakeAvailable(bool available) {
  impl_->local_wake_available.store(available && impl_->config.local_commands_enabled &&
      impl_->capture_running && impl_->aec_configured, std::memory_order_release);
}

bool EndpointAudioRuntime::LocalWake() {
  if (!impl_->local_wake_available.load(std::memory_order_acquire) ||
      !impl_->local_session_commands.load(std::memory_order_acquire) ||
      impl_->muted.load(std::memory_order_acquire)) return false;
  std::lock_guard<std::mutex> lock(impl_->local_wake_mutex);
  impl_->local_wake_requested.store(true, std::memory_order_release);
  const bool active = impl_->resident_engaged.load(std::memory_order_acquire);
  if (active) impl_->local_wake_paused.store(true, std::memory_order_release);
  return active;
}

void EndpointAudioRuntime::DrainMainLoop() {
  std::uint64_t count = 0;
  while (read(impl_->wake, &count, sizeof(count)) > 0) {}
  Impl::Event event; Impl::Token token; StreamVolume volume;
  { std::lock_guard<std::mutex> lock(impl_->mailbox_mutex); event = impl_->event; token = impl_->event_token; volume = impl_->event_volume; impl_->event = Impl::Event::None; }
  if (event == Impl::Event::Acquire) {
    if (!impl_->release_pending.load(std::memory_order_acquire)) {
      if (!impl_->main_owned) {
        impl_->main_owned = true; impl_->main_token = token;
        impl_->announce.AcquireExclusive(); impl_->satellite.IntercomAcquireAudio();
      }
      const bool same_owner = impl_->main_token.call == token.call && impl_->main_token.owner == token.owner &&
                              impl_->main_token.epoch == token.epoch;
      if (same_owner && !impl_->release_pending.load(std::memory_order_acquire)) {
        if (!impl_->announce.ReadyForExclusive()) {
          std::lock_guard<std::mutex> lock(impl_->mailbox_mutex);
          if (impl_->event == Impl::Event::None) {
            impl_->event = Impl::Event::Acquire; impl_->event_token = token; impl_->event_volume = volume;
            impl_->acquire_waiting.store(true, std::memory_order_release);
          }
        } else if (!impl_->release_pending.load(std::memory_order_acquire) &&
                   !impl_->resident_cutoff.load(std::memory_order_acquire) &&
                   impl_->resident_engaged.load(std::memory_order_acquire) &&
                   !impl_->stop.load(std::memory_order_acquire)) {
          impl_->acquire_waiting.store(false, std::memory_order_release);
          const bool gain = impl_->announce.SetExclusiveGain(static_cast<double>(volume.chime) / 100.0);
          const bool led = lva::tr::SetIntercomLed(true, impl_->muted.load(std::memory_order_acquire));
          const UpgradeToken chime{token.call, token.owner, token.epoch, 0, token.mode};
          impl_->SetChimeActive(chime);
          const bool still_owned = !impl_->release_pending.load(std::memory_order_acquire) &&
              !impl_->resident_cutoff.load(std::memory_order_acquire) &&
              impl_->resident_engaged.load(std::memory_order_acquire) &&
              !impl_->stop.load(std::memory_order_acquire);
          const bool beep = gain && still_owned && impl_->announce.PlayExclusive(impl_->config.beep_file,
              [this, token] { impl_->Complete(token); });
          if (impl_->release_pending.load(std::memory_order_acquire) ||
              impl_->resident_cutoff.load(std::memory_order_acquire) ||
              !impl_->resident_engaged.load(std::memory_order_acquire) ||
              impl_->stop.load(std::memory_order_acquire)) {
            impl_->announce.Stop(); impl_->ClearChime(chime);
            lva::tr::SetIntercomLed(false, impl_->muted.load(std::memory_order_acquire));
          }
          if (!beep) impl_->ClearChime(chime);
          impl_->Started(token, beep, led);
        }
      }
    }
  } else if (event == Impl::Event::Release) {
    impl_->acquire_waiting.store(false, std::memory_order_release);
    impl_->upgrade_waiting.store(false, std::memory_order_release);
    impl_->prepared_upgrade = {};
    impl_->ClearAllChimes();
    if (impl_->main_owned) {
    impl_->main_owned = false;
    impl_->main_token = {};
    impl_->announce.Stop();
    impl_->announce.ReleaseExclusive();
    impl_->satellite.IntercomReleaseAudio();
    lva::tr::SetIntercomLed(false, impl_->muted.load(std::memory_order_acquire));
    }
    impl_->release_pending.store(false, std::memory_order_release);
  }
  const auto request = impl_->upgrade.Take();
  if (request.event == UpgradeIndication::Event::Cleanup) {
    if (impl_->main_owned && request.token.call == impl_->main_token.call &&
        request.token.owner == impl_->main_token.owner && request.token.epoch == impl_->main_token.epoch) {
      impl_->announce.Stop();
      impl_->ClearChime(request.token);
      lva::tr::SetIntercomLed(false, impl_->muted.load(std::memory_order_acquire));
    }
    if (SameUpgrade(impl_->prepared_upgrade, request.token)) impl_->prepared_upgrade = {};
    impl_->upgrade_waiting.store(false, std::memory_order_release);
    impl_->upgrade.Cleaned(request.token);
  } else if (request.event == UpgradeIndication::Event::Start) {
    const auto token = request.token;
    const bool valid = impl_->main_owned && token.call == impl_->main_token.call &&
        token.owner == impl_->main_token.owner && token.epoch == impl_->main_token.epoch && impl_->upgrade.Valid(token) &&
        !impl_->muted.load(std::memory_order_acquire) &&
        !impl_->resident_cutoff.load(std::memory_order_acquire) &&
        !impl_->release_pending.load(std::memory_order_acquire);
    if (valid && !SameUpgrade(impl_->prepared_upgrade, token)) {
      impl_->announce.Stop(); impl_->prepared_upgrade = token;
    }
    if (valid && !impl_->announce.ReadyForExclusive()) {
      impl_->upgrade.Retry(token);
      impl_->upgrade_waiting.store(true, std::memory_order_release);
    } else if (valid) {
      impl_->upgrade_waiting.store(false, std::memory_order_release);
      const bool led = lva::tr::SetIntercomLed(true, false);
      const bool still_valid = impl_->upgrade.Valid(token) &&
          !impl_->muted.load(std::memory_order_acquire) &&
          !impl_->resident_cutoff.load(std::memory_order_acquire) &&
          !impl_->release_pending.load(std::memory_order_acquire);
      if (led && still_valid) impl_->SetChimeActive(token);
      const bool started = led && still_valid && impl_->announce.PlayExclusive(
          impl_->config.beep_file, [this, token] { impl_->upgrade.Completed(token); });
      if (!impl_->upgrade.Valid(token) || impl_->muted.load(std::memory_order_acquire) ||
          impl_->resident_cutoff.load(std::memory_order_acquire) ||
          impl_->release_pending.load(std::memory_order_acquire)) {
        impl_->announce.Stop();
        impl_->ClearChime(token);
        lva::tr::SetIntercomLed(false, impl_->muted.load(std::memory_order_acquire));
      }
      if (!started) {
        impl_->ClearChime(token);
      }
      impl_->upgrade.Started(token, started, led && still_valid);
    } else {
      impl_->upgrade_waiting.store(false, std::memory_order_release);
      impl_->upgrade.Started(token, false, false);
    }
  }
  {
    std::lock_guard<std::mutex> lock(impl_->mailbox_mutex);
    if (impl_->volume_main_pending) {
      const auto& delivery = impl_->volume_delivery;
      impl_->volume_ok = impl_->main_owned && delivery.token.call == impl_->main_token.call &&
          delivery.token.owner == impl_->main_token.owner &&
          impl_->announce.SetExclusiveGain(static_cast<double>(delivery.value.chime) / 100.0);
      impl_->volume_main_pending = false; impl_->volume_done = true;
    }
  }
}

}  // namespace intercom::vendor

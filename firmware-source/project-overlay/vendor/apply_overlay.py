#!/usr/bin/env python3
import argparse
import pathlib
import shutil

from controller_corrections import apply as apply_controller_corrections
from manager_corrections import apply as apply_manager_corrections


def replace_once(path, old, new):
    text = path.read_text()
    count = text.count(old)
    if count != 1:
        raise SystemExit("anchor mismatch in {}: expected 1, found {}".format(path, count))
    path.write_text(text.replace(old, new))


parser = argparse.ArgumentParser()
parser.add_argument("--vendor", required=True, type=pathlib.Path)
parser.add_argument("--m4", required=True, type=pathlib.Path)
args = parser.parse_args()
vendor = args.vendor.resolve()
m4 = args.m4.resolve()
here = pathlib.Path(__file__).resolve().parent
if not (vendor / "src/main.cpp").is_file() or not (vendor / "CMakeLists.txt").is_file():
    raise SystemExit("--vendor is not a copied linux-voice-assistant-cpp tree")
target = vendor / "src/intercom"
target.mkdir(parents=True, exist_ok=True)

for name in ("endpoint_controller.h", "endpoint_frame.h", "endpoint_session.h",
             "endpoint_tls.h", "endpoint_tls.cpp", "endpoint_vendor.h", "endpoint_vendor.cpp"):
    source = m4 / name
    if not source.is_file():
        raise SystemExit("missing accepted M4 source {}".format(source))
    shutil.copy2(str(source), str(target / name))
for name in ("capture_accumulator.h", "pulse_sink.h", "pulse_sink.cpp",
             "endpoint_tls.cpp", "endpoint_tls.h", "ha_clock.h", "endpoint_id.h", "boot_clock.cpp", "boot_clock.h",
             "endpoint_audio_runtime.h", "endpoint_audio_runtime.cpp", "resident_button.h",
             "endpoint_manager_controls.h", "manager_volume.h", "upgrade_indication.h"):
    shutil.copy2(str(here / name), str(target / name))

ring_h = vendor / "src/audio/PcmRingBuffer.h"
replace_once(ring_h, "    std::size_t FreeSpace() const noexcept;\n",
             "    std::size_t FreeSpace() const noexcept;\n"
             "    std::uint64_t DroppedSamples() const noexcept {\n"
             "        return dropped_samples_.load(std::memory_order_relaxed);\n"
             "    }\n")
replace_once(ring_h, "    std::atomic<std::size_t>  tail_{0};\n",
             "    std::atomic<std::size_t>  tail_{0};\n"
             "    std::atomic<std::uint64_t> dropped_samples_{0};\n")
ring_cpp = vendor / "src/audio/PcmRingBuffer.cpp"
replace_once(ring_cpp, "    const std::size_t to_write = std::min(n, free);\n",
             "    const std::size_t to_write = std::min(n, free);\n"
             "    if (to_write < n) dropped_samples_.fetch_add(n - to_write, std::memory_order_relaxed);\n")

apply_controller_corrections(target)
apply_manager_corrections(target)

cmake = vendor / "CMakeLists.txt"
replace_once(cmake, "pkg_check_modules(PULSE_SIMPLE REQUIRED IMPORTED_TARGET libpulse-simple)\n",
             "pkg_check_modules(PULSE_SIMPLE REQUIRED IMPORTED_TARGET libpulse-simple)\n"
             "pkg_check_modules(PULSE REQUIRED IMPORTED_TARGET libpulse)\n"
             "find_package(OpenSSL REQUIRED)\n")
replace_once(cmake, "    src/entities/UpdateEntity_Ota.cpp\n    src/util/Log.cpp\n)",
             "    src/entities/UpdateEntity_Ota.cpp\n    src/util/Log.cpp\n"
             "    src/intercom/endpoint_tls.cpp\n"
             "    src/intercom/boot_clock.cpp\n"
             "    src/intercom/endpoint_vendor.cpp\n"
             "    src/intercom/pulse_sink.cpp\n"
             "    src/intercom/endpoint_audio_runtime.cpp\n)")
replace_once(cmake, "        PkgConfig::PULSE_SIMPLE\n", "        PkgConfig::PULSE_SIMPLE\n        PkgConfig::PULSE\n")
replace_once(cmake, "        PkgConfig::AVAHI_CLIENT\n        Threads::Threads\n        ${CMAKE_DL_LIBS}",
             "        PkgConfig::AVAHI_CLIENT\n        Threads::Threads\n"
             "        OpenSSL::SSL\n        OpenSSL::Crypto\n        ${CMAKE_DL_LIBS}")

webrtc_h = vendor / "src/audio/WebRtcProcessor.h"
replace_once(webrtc_h, "    void SetFarEndActive(bool active);\n",
             "    void SetFarEndActive(bool active);\n"
             "    void SetEndpointOutputActive(bool active);\n"
             "    std::uint64_t ReferenceFramesProcessed() const {\n"
             "        return reference_frames_processed_.load(std::memory_order_relaxed);\n"
             "    }\n")
replace_once(webrtc_h, "    std::atomic<bool> far_end_active_{false};\n",
             "    std::atomic<bool> far_end_active_{false};\n"
             "    std::atomic<bool> endpoint_output_active_{false};\n")
replace_once(webrtc_h, "    bool              echo_on_applied_ = false;\n",
             "    bool              echo_on_applied_ = false;\n"
             "    std::atomic<std::uint64_t> reference_frames_processed_{0};\n")
webrtc_cpp = vendor / "src/audio/WebRtcProcessor.cpp"
replace_once(webrtc_cpp, "void WebRtcProcessor::ApplyConfigLocked(bool echo_on) {",
             "void WebRtcProcessor::SetEndpointOutputActive(bool active) {\n"
             "    if (!aec_enabled_) return;\n"
             "    endpoint_output_active_.store(active, std::memory_order_relaxed);\n"
             "}\n\nvoid WebRtcProcessor::ApplyConfigLocked(bool echo_on) {")
replace_once(webrtc_cpp,
             "    const bool want_echo = far_end_active_.load(std::memory_order_relaxed);",
             "    const bool want_echo = far_end_active_.load(std::memory_order_relaxed) ||\n"
             "                           endpoint_output_active_.load(std::memory_order_relaxed);")
replace_once(webrtc_cpp,
             "    return true;\n}\n\nvoid WebRtcProcessor::ResetEcho() {",
             "    reference_frames_processed_.fetch_add(n / kFrameSamples,\n"
             "                                          std::memory_order_relaxed);\n"
             "    return true;\n}\n\nvoid WebRtcProcessor::ResetEcho() {")

mpv_h = vendor / "src/audio/LibMpvPlayer.h"
replace_once(mpv_h, "    void        Play(const std::string& url,\n                     std::function<void()> done_callback = {}) override;",
             "    void        Play(const std::string& url,\n"
             "                     std::function<void()> done_callback = {}) override;\n"
             "    void AcquireExclusive();\n"
             "    bool SetExclusiveGain(double gain);\n"
             "    bool PlayExclusive(const std::string& url, std::function<void()> done_callback);\n"
             "    void ReleaseExclusive();")
replace_once(mpv_h, "    static void OnMpvWakeupTrampoline(void* opaque);",
             "    void PlayImpl(const std::string& url, std::function<void()> done_callback);\n"
             "    static void OnMpvWakeupTrampoline(void* opaque);")
replace_once(mpv_h, "    double user_volume_ = 1.0;",
             "    double user_volume_ = 1.0;\n    bool exclusive_ = false;\n    double exclusive_gain_ = 1.0;")
mpv_cpp = vendor / "src/audio/LibMpvPlayer.cpp"
replace_once(mpv_cpp,
             "    state_.store(PlayerState::kIdle, std::memory_order_relaxed);\n}",
             "    // END_FILE publishes Idle on the main event loop, including stop-keyword requests.\n}")
media_cpp = vendor / "src/entities/MediaPlayerEntity.cpp"
replace_once(media_cpp,
             "    if (state_.webrtc_processor != nullptr) {\n"
             "        auto is_active = [](lva::audio::PlayerState st) {\n"
             "            return st == lva::audio::PlayerState::kLoading ||\n"
             "                   st == lva::audio::PlayerState::kPlaying;\n"
             "        };\n"
             "        bool far_end = is_active(s);\n"
             "        if (state_.music_player != nullptr) {\n"
             "            far_end = far_end || is_active(state_.music_player->State());\n"
             "        }\n"
             "        if (state_.announce_player != nullptr) {\n"
             "            far_end = far_end || is_active(state_.announce_player->State());\n"
             "        }\n"
             "        state_.webrtc_processor->SetFarEndActive(far_end);\n"
             "    }\n\n"
             "    PlayState new_local;\n"
             "    switch (s) {\n"
             "        case lva::audio::PlayerState::kIdle:\n"
             "        case lva::audio::PlayerState::kError:\n"
             "            new_local = PlayState::kIdle;\n"
             "            break;\n"
             "        case lva::audio::PlayerState::kLoading:\n"
             "        case lva::audio::PlayerState::kPlaying:\n"
             "            new_local = PlayState::kPlaying;\n"
             "            break;\n"
             "        case lva::audio::PlayerState::kPaused:\n"
             "            new_local = PlayState::kPaused;\n"
             "            break;\n"
             "    }",
             "    auto is_active = [](lva::audio::PlayerState st) {\n"
             "        return st == lva::audio::PlayerState::kLoading ||\n"
             "               st == lva::audio::PlayerState::kPlaying;\n"
             "    };\n"
             "    const auto music = state_.music_player != nullptr\n"
             "        ? state_.music_player->State() : lva::audio::PlayerState::kIdle;\n"
             "    const auto announce = state_.announce_player != nullptr\n"
             "        ? state_.announce_player->State() : lva::audio::PlayerState::kIdle;\n"
             "    const bool far_end = is_active(s) || is_active(music) || is_active(announce);\n"
             "    if (state_.webrtc_processor != nullptr) {\n"
             "        state_.webrtc_processor->SetFarEndActive(far_end);\n"
             "    }\n\n"
             "    // Both players share this entity; one player's EOF must not hide the other.\n"
             "    const bool paused = s == lva::audio::PlayerState::kPaused ||\n"
             "                        music == lva::audio::PlayerState::kPaused ||\n"
             "                        announce == lva::audio::PlayerState::kPaused;\n"
             "    const PlayState new_local = far_end ? PlayState::kPlaying :\n"
             "                                paused ? PlayState::kPaused : PlayState::kIdle;")
replace_once(mpv_cpp,
             "if (reason == MPV_END_FILE_REASON_EOF ||\n                    reason == MPV_END_FILE_REASON_ERROR) {",
             "if (reason == MPV_END_FILE_REASON_EOF ||\n"
             "                    (!exclusive_ && reason == MPV_END_FILE_REASON_ERROR)) {")
replace_once(mpv_cpp,
             "void LibMpvPlayer::Play(const std::string& url,\n                        std::function<void()> done_callback) {\n    if (mpv_ == nullptr) {",
             "void LibMpvPlayer::Play(const std::string& url,\n"
             "                        std::function<void()> done_callback) {\n"
             "    if (exclusive_) return;\n"
             "    PlayImpl(url, std::move(done_callback));\n"
             "}\n\n"
             "void LibMpvPlayer::AcquireExclusive() { exclusive_ = true; Stop(); }\n"
             "bool LibMpvPlayer::PlayExclusive(const std::string& url, std::function<void()> done_callback) {\n"
             "    if (!exclusive_) return false;\n"
             "    PlayImpl(url, std::move(done_callback));\n"
             "    return State() == PlayerState::kLoading;\n"
             "}\n"
             "void LibMpvPlayer::ReleaseExclusive() { exclusive_ = false; exclusive_gain_ = 1.0; SetVolume(user_volume_); }\n\n"
             "bool LibMpvPlayer::SetExclusiveGain(double gain) {\n"
             "    if (!exclusive_ || !mpv_ || gain < 0.05 || gain > 1.0) return false;\n"
             "    exclusive_gain_ = gain;\n"
             "    double percent = std::sqrt(user_volume_) * 100.0 * gain;\n"
             "    return mpv_set_property(mpv_, \"volume\", MPV_FORMAT_DOUBLE, &percent) >= 0;\n"
             "}\n\n"
             "void LibMpvPlayer::PlayImpl(const std::string& url,\n"
             "                            std::function<void()> done_callback) {\n"
             "    if (mpv_ == nullptr) {")
replace_once(mpv_cpp, "    double percent = std::sqrt(user_volume_) * 100.0;",
             "    double percent = std::sqrt(user_volume_) * 100.0 * (exclusive_ ? exclusive_gain_ : 1.0);")

satellite_h = vendor / "src/satellite/Satellite.h"
replace_once(satellite_h, "    void OnMuted();\n\n   private:",
             "    void OnMuted();\n\n"
             "    // Main-loop-only ownership seam for the disposable intercom PoC.\n"
             "    void IntercomAcquireAudio();\n"
             "    void IntercomReleaseAudio();\n\n"
             "   private:")
replace_once(satellite_h, "    bool                                  timer_ringing_ = false;",
             "    bool                                  timer_ringing_ = false;\n"
             "    std::atomic<bool> intercom_owned_{false};\n"
             "    bool   intercom_paused_timer_ = false;")
satellite_cpp = vendor / "src/satellite/Satellite.cpp"
replace_once(satellite_cpp, "void Satellite::OnWakeDetected(const std::string& model_id, float prob) {",
             "void Satellite::OnWakeDetected(const std::string& model_id, float prob) {\n"
             "    if (intercom_owned_.load(std::memory_order_acquire)) return;")
replace_once(satellite_cpp, "void Satellite::OnStopDetected() {",
             "void Satellite::OnStopDetected() {\n"
             "    if (intercom_owned_.load(std::memory_order_acquire)) return;")
replace_once(satellite_cpp, "void Satellite::OnMuted() {",
             "void Satellite::IntercomAcquireAudio() {\n"
             "    if (intercom_owned_.exchange(true, std::memory_order_acq_rel)) return;\n"
             "    StopAudioStreaming();\n"
             "    if (timer_ringing_) intercom_paused_timer_ = true;\n"
             "    pipeline_active_ = false;\n"
             "    continue_conversation_ = false;\n"
             "    continue_listen_at_ns_.store(0, std::memory_order_relaxed);\n"
             "    if (announce_player_) announce_player_->Stop();\n"
             "    tts_url_.clear();\n"
             "    tts_played_ = false;\n"
             "    Duck();\n"
             "}\n\n"
             "void Satellite::IntercomReleaseAudio() {\n"
             "    if (!intercom_owned_.exchange(false, std::memory_order_acq_rel)) return;\n"
             "    if (intercom_paused_timer_ && timer_ringing_) {\n"
             "        intercom_paused_timer_ = false;\n"
             "        OnTimerSoundEof();\n"
             "    } else {\n"
             "        intercom_paused_timer_ = false;\n"
             "        Unduck();\n"
             "    }\n"
             "}\n\n"
             "void Satellite::OnMuted() {")
replace_once(satellite_cpp,
             "void Satellite::Unduck() {\n    if (!ducked_) return;",
             "void Satellite::Unduck() {\n"
             "    if (intercom_owned_.load(std::memory_order_acquire)) return;\n"
             "    if (!ducked_) return;")
replace_once(satellite_cpp,
             "    if (announce_player_ != nullptr) {\n        announce_player_->Stop();\n    }\n    Unduck();\n    lva::tr::Show(lva::tr::LedState::Idle);",
             "    if (announce_player_ != nullptr &&\n"
             "        !intercom_owned_.load(std::memory_order_acquire)) {\n"
             "        announce_player_->Stop();\n"
             "    }\n"
             "    Unduck();\n"
             "    lva::tr::Show(lva::tr::LedState::Idle);")
replace_once(satellite_cpp,
             "    if (announce_player_) announce_player_->Stop();\n    if (state_.music_player) state_.music_player->Stop();",
             "    if (!intercom_owned_.load(std::memory_order_acquire)) {\n"
             "        if (announce_player_) announce_player_->Stop();\n"
             "        if (state_.music_player) state_.music_player->Stop();\n"
             "    }")
replace_once(satellite_cpp,
             "    timer_ring_start_ = std::chrono::steady_clock::now();\n    if (announce_player_ != nullptr) {",
             "    timer_ring_start_ = std::chrono::steady_clock::now();\n"
             "    if (intercom_owned_.load(std::memory_order_acquire)) { intercom_paused_timer_ = true; return; }\n"
             "    if (announce_player_ != nullptr) {")
replace_once(satellite_cpp,
             "void Satellite::OnTimerSoundEof() {\n    if (!timer_ringing_) return;  // user cancelled",
             "void Satellite::OnTimerSoundEof() {\n"
             "    if (!timer_ringing_) return;  // user cancelled\n"
             "    if (intercom_owned_.load(std::memory_order_acquire)) { intercom_paused_timer_ = true; return; }")

led_h = vendor / "src/tr/LedRing.h"
replace_once(led_h, "void Show(LedState state);\n",
             "bool Show(LedState state);\n"
             "bool SetIntercomLed(bool active, bool muted);\n")
led_cpp = vendor / "src/tr/LedRing.cpp"
replace_once(led_cpp, "#include <cstring>\n", "#include <cstring>\n#include <atomic>\n")
replace_once(led_cpp, "constexpr const char* kTag    = \"led\";",
             "constexpr const char* kTag    = \"led\";\n"
             "std::atomic<bool> g_intercom_led_owned{false};")
replace_once(led_cpp, "void Show(LedState state) {\n    const char* anim_name = AnimationFor(state);",
             "bool Show(LedState state) {\n"
             "    if (g_intercom_led_owned.load(std::memory_order_acquire) &&\n"
             "        state != LedState::Muted) return false;\n"
             "    const char* anim_name = AnimationFor(state);")
replace_once(led_cpp, "        return;\n    }\n    std::string anim_path", "        return false;\n    }\n    std::string anim_path")
replace_once(led_cpp, "        return;\n    }\n    LVA_LOGD(kTag, \"show '%s' (pid=%d)\", anim_name, (int)pid);",
             "        return false;\n    }\n    LVA_LOGD(kTag, \"show '%s' (pid=%d)\", anim_name, (int)pid);\n"
             "    return true;")
replace_once(led_cpp, "void ShowVolumeChanged() {",
             "bool SetIntercomLed(bool active, bool muted) {\n"
             "    g_intercom_led_owned.store(false, std::memory_order_release);\n"
             "    const bool shown = Show(muted ? LedState::Muted : (active ? LedState::Speaking : LedState::Idle));\n"
             "    g_intercom_led_owned.store(active, std::memory_order_release);\n"
             "    return shown;\n"
             "}\n\nvoid ShowVolumeChanged() {")

main = vendor / "src/main.cpp"
button_h = vendor / "src/tr/HomeButton.h"
replace_once(button_h, "#include <cstdint>\n", "#include <cstdint>\n#include <functional>\n")
replace_once(button_h, "        std::uint32_t entity_key  = 0;\n",
             "        std::uint32_t entity_key  = 0;\n"
             "        std::function<bool()> resident_press;\n")
button_cpp = vendor / "src/tr/HomeButton.cpp"
replace_once(button_cpp, '#include "tr/HomeButton.h"\n',
             '#include "tr/HomeButton.h"\n#include "intercom/resident_button.h"\n')
replace_once(button_cpp, "    int    click_count           = 0;\n",
             "    intercom::vendor::ResidentButtonGroup resident_group;\n"
             "    int    click_count           = 0;\n")
replace_once(button_cpp, "        if (ev.value != 0) continue;  // 0 = release\n",
             "        const auto button_ms = static_cast<std::uint64_t>(\n"
             "            std::chrono::duration_cast<std::chrono::milliseconds>(\n"
             "                std::chrono::steady_clock::now().time_since_epoch()).count());\n"
             "        if (resident_group.Consume(ev.value, button_ms, [this] {\n"
             "                return opts_.resident_press && opts_.resident_press();\n"
             "            })) {\n"
             "            click_count = 0; release_pending_flush = false;\n"
             "            pending_clicks_.store(0, std::memory_order_release);\n"
             "            continue;\n"
             "        }\n"
             "        if (ev.value != 0) continue;  // 0 = release\n")
replace_once(main, "    home_btn_opts.entity_key = home_button_entity_key;\n",
             "    home_btn_opts.entity_key = home_button_entity_key;\n"
             "    home_btn_opts.resident_press = [&intercom_runtime] {\n"
             "        return intercom_runtime && intercom_runtime->ResidentButtonPress();\n"
             "    };\n")
replace_once(main, "    auto wakeword_engine =\n",
             "    bool manager_local_wake_model = false;\n    auto wakeword_engine =\n")
replace_once(main, "                    wakeword_engine->AddOpenModel(std::move(model));\n",
             "                    wakeword_engine->AddOpenModel(std::move(model));\n"
             "                    manager_local_wake_model = true;\n")
replace_once(main, "                    wakeword_engine->AddModel(std::move(model));\n",
             "                    wakeword_engine->AddModel(std::move(model));\n"
             "                    manager_local_wake_model = true;\n")
replace_once(main, '            LVA_LOGE(kTag, "OpenWakeWord features failed to load");',
             '            manager_local_wake_model = false;\n'
             '            LVA_LOGE(kTag, "OpenWakeWord features failed to load");')
replace_once(main, '    wakeword_engine->SetDetectionCallback(\n        [&satellite]',
             '    wakeword_engine->SetDetectionCallback(\n        [&satellite, &intercom_runtime]')
replace_once(main, '                satellite->OnWakeDetected(model_id, prob);',
             '                if (!intercom_runtime || !intercom_runtime->LocalWake())\n'
             '                    satellite->OnWakeDetected(model_id, prob);')
replace_once(main, '    if (!wakeword_engine->Start()) {',
             '    const bool manager_wake_started = wakeword_engine->Start();\n'
             '    if (intercom_runtime) intercom_runtime->SetLocalWakeAvailable(\n'
             '        manager_wake_started && manager_local_wake_model);\n'
             '    if (!manager_wake_started) {')
replace_once(main, '    std::string wakeword_models = "okay_nabu";',
             '    std::string wakeword_models;  // Intercom-only default; explicit CLI opt-in remains available.')
replace_once(main,
             "    auto sound_watcher = std::make_unique<lva::tr::SoundConfWatcher>(\n"
             "        state, cli.preferences_file);",
             "    auto sound_watcher = std::make_unique<lva::tr::SoundConfWatcher>(\n"
             "        state, cli.preferences_file);\n"
             "    // Physical buttons write the stock file even during a RAM preferences test.\n"
             "    std::unique_ptr<lva::tr::SoundConfWatcher> button_sound_watcher;\n"
             "    if (cli.preferences_file != \"/data/conf/sound.json\")\n"
             "        button_sound_watcher = std::make_unique<lva::tr::SoundConfWatcher>(\n"
             "            state, \"/data/conf/sound.json\");")
replace_once(main,
             "[&satellite, &mic_mute_gpio, &sound_watcher,",
             "[&satellite, &mic_mute_gpio, &sound_watcher, &button_sound_watcher,")
replace_once(main, "                sound_watcher->Poll();",
             "                sound_watcher->Poll();\n"
             "                if (button_sound_watcher) button_sound_watcher->Poll();")
replace_once(main, '#include "entities/UpdateEntity.h"\n',
             '#include "entities/UpdateEntity.h"\n#include "intercom/endpoint_audio_runtime.h"\n')
replace_once(main,
             "    auto satellite_ring = std::make_unique<lva::audio::PcmRingBuffer>(\n        16'000 * 2);",
             "    auto satellite_ring = std::make_unique<lva::audio::PcmRingBuffer>(\n        16'000 * 2);\n"
             "    auto intercom_ring = std::make_unique<lva::audio::PcmRingBuffer>(16'384);")
replace_once(main, "    audio_capture->AddTap(*satellite_ring);\n",
             "    audio_capture->AddTap(*satellite_ring);\n    audio_capture->AddTap(*intercom_ring);\n")
replace_once(main,
             "    if (!audio_capture->Start()) {\n        LVA_LOGW(kTag, \"audio capture failed to start; mic-using \"\n                       \"features (wake word) will be unavailable\");\n    }",
             "    const bool capture_running = audio_capture->Start();\n"
             "    if (!capture_running) {\n        LVA_LOGW(kTag, \"audio capture failed to start; mic-using \"\n"
             "                       \"features (wake word) will be unavailable\");\n    }")
satellite_anchor = "    state.satellite = satellite.get();\n"
satellite_insert = satellite_anchor + "\n" + r'''    std::unique_ptr<intercom::vendor::EndpointAudioRuntime> intercom_runtime;
    intercom::vendor::RuntimeConfig intercom_config;
    std::string intercom_error;
    const char* intercom_config_env = std::getenv("INTERCOM_POC_CONFIG");
    const std::string intercom_config_path =
        (intercom_config_env != nullptr && intercom_config_env[0] != '\0')
            ? intercom_config_env : "/data/conf/intercom_m3_poc.json";
    if (intercom::vendor::LoadRuntimeConfig(
            intercom_config_path, intercom_config, intercom_error)) {
        intercom_runtime = std::make_unique<intercom::vendor::EndpointAudioRuntime>(
            intercom_config, *intercom_ring, state.muted, *webrtc_processor,
            *announce_player, *satellite, capture_running, aec_enabled);
        if (intercom_runtime->Start()) {
            server.AddAuxFd(intercom_runtime->MainLoopFd(), [&intercom_runtime] {
                intercom_runtime->DrainMainLoop();
            });
            LVA_LOGI(kTag, "intercom_m3_poc endpoint enabled on port %u",
                     static_cast<unsigned>(intercom_config.port));
        } else {
            LVA_LOGE(kTag, "intercom_m3_poc failed closed during startup");
            intercom_runtime.reset();
        }
    } else {
        LVA_LOGI(kTag, "intercom_m3_poc inactive: %s", intercom_error.c_str());
    }
'''
replace_once(main, satellite_anchor, satellite_insert)
replace_once(main, "    wakeword_engine->Stop();\n    audio_capture->Stop();",
             "    wakeword_engine->Stop();\n    if (intercom_runtime) intercom_runtime->Stop();\n    audio_capture->Stop();")
from upgrade_player import apply as apply_upgrade_player
apply_upgrade_player(vendor)
print("applied M5/M6 vendor overlay to {}".format(vendor))



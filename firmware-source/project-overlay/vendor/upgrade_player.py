"""Candidate: identify the exclusive mpv entry before starting playback.

Pinned mpv 0.35.1 player/command.c cmd_loadfile returns playlist_entry_id in
mpv_command_ret's result. append does not start playback; play-index does.
"""
from pathlib import Path

def apply(vendor):
    root=Path(vendor)
    def change(name, old, new):
        p=root/name; text=p.read_text(encoding='utf-8')
        if text.count(old)!=1: raise ValueError(f'{name}: unexpected source: {old}')
        p.write_text(text.replace(old,new,1),encoding='utf-8')
    change('src/audio/LibMpvPlayer.h', '#include <atomic>', '#include <atomic>\n#include <cstdint>')
    change('src/audio/LibMpvPlayer.h', '    double exclusive_gain_ = 1.0;',
           '    double exclusive_gain_ = 1.0;\n    std::int64_t exclusive_entry_id_ = -1, retired_exclusive_id_ = -1, started_entry_id_ = -1;\n'
           '    bool stop_barrier_ = false, await_idle_ = false, event_overflow_ = false, idle_event_enabled_ = false;')
    change('src/audio/LibMpvPlayer.h', '    bool PlayExclusive(const std::string& url, std::function<void()> done_callback);',
           '    bool PlayExclusive(const std::string& url, std::function<void()> done_callback);\n'
           '    bool ReadyForExclusive();')
    change('src/audio/LibMpvPlayer.cpp',
        '            case MPV_EVENT_START_FILE:\n                set_state(PlayerState::kLoading);',
        '            case MPV_EVENT_START_FILE:\n'
        '                if (auto* start = static_cast<mpv_event_start_file*>(ev->data);\n'
        '                    start) {\n'
        '                    started_entry_id_ = start->playlist_entry_id;\n'
        '                    if (stop_barrier_) await_idle_ = true;\n'
        '                    if (start->playlist_entry_id == retired_exclusive_id_) break;\n'
        '                } else { event_overflow_ = true; }\n'
        '                set_state(PlayerState::kLoading);')
    change('src/audio/LibMpvPlayer.cpp',
        '                const int reason = end ? static_cast<int>(end->reason) : -1;',
        '                if (end && end->playlist_entry_id == started_entry_id_) started_entry_id_ = -1;\n'
        '                if (end && end->playlist_entry_id == retired_exclusive_id_) {\n'
        '                    retired_exclusive_id_ = -1; break;\n'
        '                }\n'
        '                if (exclusive_ && (!end || exclusive_entry_id_ < 0 ||\n'
        '                    end->playlist_entry_id != exclusive_entry_id_)) break;\n'
        '                const int reason = end ? static_cast<int>(end->reason) : -1;')
    change('src/audio/LibMpvPlayer.cpp',
        'if (reason == MPV_END_FILE_REASON_EOF ||\n                    (!exclusive_ && reason == MPV_END_FILE_REASON_ERROR)) {',
        'if ((!exclusive_ && (reason == MPV_END_FILE_REASON_EOF || reason == MPV_END_FILE_REASON_ERROR)) ||\n'
        '                    (exclusive_ && reason == MPV_END_FILE_REASON_EOF && end &&\n'
        '                     exclusive_entry_id_ >= 0 && end->playlist_entry_id == exclusive_entry_id_)) {')
    change('src/audio/LibMpvPlayer.cpp',
        '                        done_callback_ = nullptr;\n                    }\n                    if (cb) {',
        '                        done_callback_ = nullptr;\n                    }\n'
        '                    if (exclusive_) exclusive_entry_id_ = -1;\n                    if (cb) {')
    change('src/audio/LibMpvPlayer.cpp',
        '                break;\n            }\n            case MPV_EVENT_LOG_MESSAGE:',
        '                if (exclusive_ && end && end->playlist_entry_id == exclusive_entry_id_)\n'
        '                    exclusive_entry_id_ = -1;\n'
        '                break;\n            }\n            case MPV_EVENT_LOG_MESSAGE:')
    change('src/audio/LibMpvPlayer.cpp',
        '            case MPV_EVENT_LOG_MESSAGE: {',
        '            case MPV_EVENT_IDLE:\n'
        '                await_idle_ = false; event_overflow_ = false; started_entry_id_ = -1;\n'
        '                retired_exclusive_id_ = -1; break;\n'
        '            case MPV_EVENT_QUEUE_OVERFLOW:\n'
        '                event_overflow_ = true; break;\n'
        '            case MPV_EVENT_LOG_MESSAGE: {')
    change('src/audio/LibMpvPlayer.cpp',
        '    mpv_set_wakeup_callback(mpv_, &OnMpvWakeupTrampoline, this);',
        '    idle_event_enabled_ = mpv_request_event(mpv_, MPV_EVENT_IDLE, 1) >= 0;\n'
        '    mpv_set_wakeup_callback(mpv_, &OnMpvWakeupTrampoline, this);')
    change('src/audio/LibMpvPlayer.cpp',
        '    if (!exclusive_) return false;\n    PlayImpl(url, std::move(done_callback));\n    return State() == PlayerState::kLoading;',
        '''    if (!ReadyForExclusive()) return false;
    // Append is non-playing; get this exact entry ID before selecting it.
    const char* append[] = {"loadfile", url.c_str(), "append", nullptr};
    mpv_node result{};
    if (mpv_command_ret(mpv_, append, &result) < 0) return false;
    std::int64_t id = -1;
    if (result.format == MPV_FORMAT_NODE_MAP && result.u.list) {
        for (int i = 0; i < result.u.list->num; ++i) {
            const auto& value = result.u.list->values[i];
            if (result.u.list->keys[i] && std::strcmp(result.u.list->keys[i], "playlist_entry_id") == 0 &&
                value.format == MPV_FORMAT_INT64) id = value.u.int64;
        }
    }
    mpv_free_node_contents(&result);
    std::int64_t count = 0, index = -1;
    if (id >= 0 && mpv_get_property(mpv_, "playlist/count", MPV_FORMAT_INT64, &count) >= 0 &&
        count >= 1 && count <= 1024) {
        for (std::int64_t i = 0; i < count; ++i) {
            std::int64_t candidate = -1;
            const std::string key = "playlist/" + std::to_string(i) + "/id";
            if (mpv_get_property(mpv_, key.c_str(), MPV_FORMAT_INT64, &candidate) < 0) break;
            if (candidate == id) { index = i; break; }
        }
    }
    if (index < 0) {
        const char* clear[] = {"playlist-clear", nullptr};
        (void)mpv_command(mpv_, clear); return false;
    }
    {
        std::lock_guard<std::mutex> lock(cb_mutex_);
        done_callback_ = std::move(done_callback);
    }
    exclusive_entry_id_ = id;
    const std::string pos = std::to_string(index);
    const char* play[] = {"playlist-play-index", pos.c_str(), nullptr};
    if (mpv_command(mpv_, play) < 0) {
        exclusive_entry_id_ = -1;
        { std::lock_guard<std::mutex> lock(cb_mutex_); done_callback_ = nullptr; }
        const char* clear[] = {"playlist-clear", nullptr};
        (void)mpv_command(mpv_, clear); return false;
    }
    stop_barrier_ = false;
    state_.store(PlayerState::kLoading, std::memory_order_relaxed);
    return true;''')
    change('src/audio/LibMpvPlayer.cpp',
        'void LibMpvPlayer::Stop() {\n    if (mpv_ == nullptr) return;',
        'void LibMpvPlayer::Stop() {\n'
        '    if (exclusive_entry_id_ >= 0) retired_exclusive_id_ = exclusive_entry_id_;\n'
        '    exclusive_entry_id_ = -1; stop_barrier_ = false;\n    if (mpv_ == nullptr) return;')
    change('src/audio/LibMpvPlayer.cpp',
        '    // END_FILE publishes Idle on the main event loop, including stop-keyword requests.\n}',
        '    stop_barrier_ = true; await_idle_ = started_entry_id_ >= 0;\n'
        '    // END_FILE publishes Idle on the main event loop, including stop-keyword requests.\n}')
    change('src/audio/LibMpvPlayer.cpp',
        'bool LibMpvPlayer::PlayExclusive(const std::string& url, std::function<void()> done_callback) {',
        '''bool LibMpvPlayer::ReadyForExclusive() {
    if (!exclusive_ || !mpv_ || !idle_event_enabled_ || !stop_barrier_) return false;
    DrainEvents();
    if (await_idle_ || event_overflow_) return false;
    retired_exclusive_id_ = -1;
    return true;
}
bool LibMpvPlayer::PlayExclusive(const std::string& url, std::function<void()> done_callback) {''')

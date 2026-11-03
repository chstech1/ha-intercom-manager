#!/usr/bin/env python3
import argparse
import pathlib


def replace_once(path, old, new):
    text = path.read_text()
    count = text.count(old)
    if count != 1:
        raise SystemExit("controller anchor mismatch in {}: expected 1, found {}".format(path, count))
    path.write_text(text.replace(old, new))


def apply(module_directory):
    frame = pathlib.Path(module_directory) / "endpoint_frame.h"
    session = pathlib.Path(module_directory) / "endpoint_session.h"
    controller = pathlib.Path(module_directory) / "endpoint_controller.h"
    for path in (frame, session, controller):
        if not path.is_file():
            raise SystemExit("missing {} in {}".format(path.name, module_directory))
    replace_once(frame, "        case 12: return size == 88;\n", "        case 12: return size == 100;\n")
    replace_once(frame,
                 'case 10: return std::memcmp(b, "ICM2", 4) == 0 && U16(b + 4) == 2 && U16(b + 6) == 2',
                 'case 10: return std::memcmp(b, "ICM3", 4) == 0 && U16(b + 4) == 3 && U16(b + 6) == 2')
    replace_once(frame,
                 'case 11: return std::memcmp(b, "ICM2", 4) == 0 && U16(b + 4) == 2 && U16(b + 6) == 0',
                 'case 11: return std::memcmp(b, "ICM3", 4) == 0 && U16(b + 4) == 3 && U16(b + 6) == 0')
    replace_once(frame,
                 "            if (state > 6 || U16(b + 66) > 255) return false;\n",
                 "            if (state > 6 || U16(b + 66) > 255 || U64(b + 88) == 0) return false;\n")
    replace_once(session,
                 'hello.size=67;\n        auto* b=hello.body.data();std::memcpy(b,"ICM2",4);PutLe(b+4,2,2);PutLe(b+6,2,2);',
                 'hello.size=67;\n        auto* b=hello.body.data();std::memcpy(b,"ICM3",4);PutLe(b+4,3,2);PutLe(b+6,2,2);')
    replace_once(session,
                 "case 12:frame.size=88;b[64]=notice.state;b[65]=notice.mode;PutLe(b+66,notice.health,2);PutLe(b+68,notice.mode_generation,4);\n"
                 "                    PutLe(b+72,core_->RxMediaNext()?core_->RxMediaNext()-1:0,4);PutLe(b+76,tx_media_?tx_media_-1:0,4);\n"
                 "                    PutLe(b+80,core_->RxProgressMs(now),4);PutLe(b+84,core_->TxProgressMs(now),4);break;",
                 "case 12:frame.size=100;b[64]=notice.state;b[65]=notice.mode;PutLe(b+66,notice.health,2);PutLe(b+68,notice.mode_generation,4);\n"
                 "                    PutLe(b+72,notice.rx_media,4);PutLe(b+76,notice.tx_media,4);\n"
                 "                    PutLe(b+80,notice.rx_progress_ms,4);PutLe(b+84,notice.tx_progress_ms,4);\n"
                 "                    PutLe(b+88,notice.report_mono_us,8);PutLe(b+96,notice.output_progress_ms,4);break;")
    replace_once(session,
                 "        if(core_->PeekNotice(notice)) {\n            Frame frame=Envelope(notice.type,notice.call,notice.owner);\n",
                 "        if(core_->PeekNotice(notice)) {\n"
                 "            notice.tx_media=tx_media_?tx_media_-1:0; // urgent priority freezes TX while queued\n"
                 "            Frame frame=Envelope(notice.type,notice.call,notice.owner);\n")
    replace_once(session,
                 "                PutLe(b+80,200000-std::min<std::uint64_t>(200000,batch.expiry-now),4);PutLe(b+84,512,2);",
                 "                PutLe(b+80,2000000-std::min<std::uint64_t>(2000000,batch.capture_deadline-now),4);PutLe(b+84,512,2);")
    replace_once(controller,
                 "    struct Batch { std::array<std::uint8_t,1024> pcm{}; std::uint32_t mode = 0;\n                   std::uint64_t expiry = 0; };",
                 "    struct Batch { std::array<std::uint8_t,1024> pcm{}; std::uint32_t mode = 0;\n                   std::uint64_t expiry = 0, capture_deadline = 0; };")
    replace_once(controller,
                 "        batch.expiry = Deadline(now,std::min<std::uint64_t>(96000,200000-capture_age));\n",
                 "        batch.expiry = Deadline(now,std::min<std::uint64_t>(96000,2000000-capture_age));\n"
                 "        batch.capture_deadline = Deadline(now,2000000-capture_age);\n")
    replace_once(controller,
                 "            if (!clock_.Age(U64(b+72),U32(b+80),now,age) || age >= 200000) return true; // drop, no progress\n"
                 "            PcmQueue<6>::Batch batch; std::copy(b+86,b+1110,batch.pcm.begin());\n"
                 "            batch.mode = mode_generation_; batch.expiry = Deadline(now,std::min<std::uint64_t>(192000,200000-age));",
                 "            if (!clock_.Age(U64(b+72),U32(b+80),now,age) || age >= 2000000) return true; // drop, no progress\n"
                 "            PcmQueue<6>::Batch batch; std::copy(b+86,b+1110,batch.pcm.begin());\n"
                 "            batch.mode = mode_generation_; batch.expiry = Deadline(now,std::min<std::uint64_t>(192000,2000000-age));\n"
                 "            batch.capture_deadline = Deadline(now,2000000-age);")
    replace_once(controller,
                 "        if (!Tick(now) || !MicAllowed() || generation != mode_generation_ || capture_age >= 200000) return false;",
                 "        if (!Tick(now) || !MicAllowed() || generation != mode_generation_ || capture_age >= 2000000) return false;")
    replace_once(controller,
                 "                std::uint64_t nonce = 0, t1 = 0, t2 = 0; std::uint8_t state = 0; std::uint16_t health = 0; };\n",
                 "                std::uint64_t nonce = 0, t1 = 0, t2 = 0, report_mono_us = 0;\n"
                 "                std::uint32_t rx_media = 0, tx_media = 0, rx_progress_ms = UINT32_MAX,\n"
                 "                              tx_progress_ms = UINT32_MAX, output_progress_ms = UINT32_MAX;\n"
                 "                std::uint8_t state = 0; std::uint16_t health = 0; };\n")
    replace_once(controller,
                 "    void LocalStop(std::uint64_t now) { if (Tick(now) && owned_) End(13); }\n",
                 "    void LocalStop(std::uint64_t now) { if (Tick(now) && owned_) End(13); }\n"
                 "    bool ReportStatus(std::uint16_t health, std::uint64_t now) {\n"
                 "        if (!Tick(now) || !owned_) return false;\n"
                 "        if (last_status_report_ && now-last_status_report_ < 250000) return true;\n"
                 "        health_ = health; last_status_report_ = now; Emit(12,0); return !closed_;\n"
                 "    }\n")
    replace_once(controller,
                 "        notices_[notices_size_-1].health = Health();\n",
                 "        auto& notice = notices_[notices_size_-1];\n"
                 "        notice.health = Health(); notice.report_mono_us = last_now_;\n"
                 "        notice.rx_media = media_rx_ ? media_rx_-1 : 0;\n"
                 "        notice.rx_progress_ms = rx_seen_ ? ProgressMs(last_now_,last_rx_) : UINT32_MAX;\n"
                 "        notice.tx_progress_ms = tx_seen_ ? ProgressMs(last_now_,last_tx_) : UINT32_MAX;\n"
                 "        notice.output_progress_ms = output_seen_ ? ProgressMs(last_now_,last_output_) : UINT32_MAX;\n")
    replace_once(controller,
                 "    std::uint64_t notice_serial_ = 0;\n",
                 "    std::uint64_t notice_serial_ = 0, last_status_report_ = 0;\n")
    replace_once(controller,
                 "    void Emit(std::uint8_t type, std::uint16_t code) {\n"
                 "        if (notices_size_ == notices_.size() || notice_serial_ == UINT64_MAX) { Close(10); return; }\n"
                 "        if (type == 6 || type == 9 || type == 13) tx_.Clear();\n",
                 "    void Emit(std::uint8_t type, std::uint16_t code) {\n"
                 "        if (notices_size_ == notices_.size() || notice_serial_ == UINT64_MAX) { Close(10); return; }\n"
                 "        if (type == 6 || type == 9 || type == 13) tx_.Clear();\n"
                 "        if (type == 12) last_status_report_ = last_now_;\n")
    replace_once(controller,
                 "        if (f.type == 1) {\n"
                 "            if (!Active() || !AudioGenerationMatches(f,mode_generation_) || U32(b+68) != media_rx_ || media_rx_ == UINT32_MAX) return Protocol();\n"
                 "            std::uint64_t age = 0;\n"
                 "            ++next_rx_; ++media_rx_;",
                 "        if (f.type == 1) {\n"
                 "            if (!Active() || U32(b+68) != media_rx_ || media_rx_ == UINT32_MAX) return Protocol();\n"
                 "            const auto incoming_mode = U32(b+64);\n"
                 "            if (incoming_mode != mode_generation_) {\n"
                 "                if (incoming_mode > mode_generation_) return Protocol();\n"
                 "                ++next_rx_; ++media_rx_; return true;\n"
                 "            }\n"
                 "            std::uint64_t age = 0;\n"
                 "            ++next_rx_; ++media_rx_;")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("module_directory", type=pathlib.Path)
    arguments = parser.parse_args()
    apply(arguments.module_directory.resolve())
    print("applied disposable controller corrections to {}".format(arguments.module_directory))

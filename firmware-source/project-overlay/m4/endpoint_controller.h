#pragma once
#include "endpoint_frame.h"
#include <cmath>

namespace intercom {
using Id = std::array<std::uint8_t, 16>;
constexpr std::uint64_t LeaseUs = 5000000, ClockUs = 3000000;
inline std::uint64_t Deadline(std::uint64_t now, std::uint64_t delta) {
    return now > UINT64_MAX - delta ? UINT64_MAX : now + delta;
}
inline bool IncrementMode(std::uint32_t& value) {
    if (value == UINT32_MAX) return false;
    ++value; return true;
}

// Local monotonic clock relation, peer minus local. Caller must match nonce/t1
// against its outstanding authenticated PING before Observe. No wall clock.
class ClockRelation {
public:
    bool Observe(std::uint64_t t1, std::uint64_t t2, std::uint64_t t3,
                 std::uint64_t t4, std::uint64_t resolution = 1) {
        valid_ = false;
        if (t4 < t1 || t3 < t2 || t2 < peer_ || t4 < measured_ ||
            t4-t1 < t3-t2 || resolution == 0) return false;
        const auto rtt = t4-t1-(t3-t2);
        offset_ = ((static_cast<long double>(t2)-t1) +
                   (static_cast<long double>(t3)-t4))/2;
        uncertainty_ = std::max(static_cast<long double>(rtt)/2,
                                static_cast<long double>(resolution)) +
                       std::ceil(static_cast<long double>(t4-t1)*0.0002L)+1;
        measured_ = t4; peer_ = t3; valid_ = true; return true;
    }
    bool Fresh(std::uint64_t now) const {
        return valid_ && now >= measured_ && now-measured_ < ClockUs;
    }
    bool Age(std::uint64_t sent, std::uint64_t prior, std::uint64_t now,
             std::uint64_t& upper) const {
        if (!Fresh(now)) return false;
        const auto guard = std::ceil(static_cast<long double>(now-measured_)*0.0002L)+1;
        const auto center = static_cast<long double>(now)-sent+offset_;
        const auto uncertainty = uncertainty_+guard;
        if (center+uncertainty < 0) return false; // impossible future timestamp
        const auto bound = std::ceil(static_cast<long double>(prior)+std::max(0.L,center+uncertainty));
        if (bound >= static_cast<long double>(UINT64_MAX)) return false;
        upper = static_cast<std::uint64_t>(bound); return true;
    }
private:
    long double offset_ = 0, uncertainty_ = 0;
    std::uint64_t measured_ = 0, peer_ = 0;
    bool valid_ = false;
};

// Fixed storage, drop oldest. Each sample batch retains its permission generation
// and local expiry. No concurrent reset: controller/queues have one loop owner.
template<std::size_t Capacity> class PcmQueue {
public:
    struct Batch { std::array<std::uint8_t,1024> pcm{}; std::uint32_t mode = 0;
                   std::uint64_t expiry = 0; };
    void Clear() { for (auto& item : items_) item = {}; size_ = 0; head_ = 0; }
    void Push(const Batch& batch) {
        if (size_ == Capacity) { items_[head_] = {}; head_ = (head_+1)%Capacity; --size_; ++drops; }
        items_[(head_+size_)%Capacity] = batch; ++size_;
    }
    bool Pop(std::uint64_t now, std::uint32_t mode, Batch& out) {
        out = {};
        while (size_) {
            auto batch = items_[head_]; items_[head_] = {}; head_ = (head_+1)%Capacity; --size_;
            if (batch.mode != mode || now >= batch.expiry) { ++drops; continue; }
            out = batch; return true;
        }
        return false;
    }
    std::size_t Size() const { return size_; }
    std::uint64_t drops = 0;
private:
    std::array<Batch,Capacity> items_{};
    std::size_t head_ = 0, size_ = 0;
};

enum class EndpointState : std::uint8_t { Idle, Indicating, Ringing, Full, Speaker, Upgrade, Cleanup };
struct Notice { std::uint8_t type = 0, mode = 0; std::uint16_t code = 0;
                std::uint32_t mode_generation = 0; Id call{}; std::uint64_t owner = 0, serial = 0;
                std::uint64_t nonce = 0, t1 = 0, t2 = 0; std::uint8_t state = 0; std::uint16_t health = 0; };

// Isolated runtime decision core. Construct ONLY after mTLS/role/device/epoch
// validation. This class does not implement that validation or touch real audio.
// All methods run on one local loop. Adapter must enforce a shared atomic privacy
// gate at actual capture/write/render and run Tick independently of socket writes.
class EndpointController {
public:
    EndpointController(Id connection, Id boot, std::uint64_t now)
        : connection_(connection), boot_(boot), last_now_(now), last_pong_(now) {}
    EndpointState State() const { return state_; }
    bool Closed() const { return closed_; }
    bool MicAllowed() const { return !closed_ && state_ == EndpointState::Full && !muted_ && !dnd_ && !maintenance_ && indicator_ && owned_; }
    bool Connected(std::uint64_t now) const {
        return !closed_ && Active() && rx_seen_ && output_seen_ && clock_.Fresh(now) && now >= last_rx_ && now-last_rx_ < 500000 &&
               now >= last_output_ && now-last_output_ < 500000 &&
               (state_ != EndpointState::Full || (tx_seen_ && now >= last_tx_ && now-last_tx_ < 500000));
    }
    std::uint64_t LeaseExpiry() const { return lease_; }
    std::uint32_t ModeGeneration() const { return mode_generation_; }
    std::size_t RxQueued() const { return rx_.Size(); }
    std::size_t TxQueued() const { return tx_.Size(); }
    bool OwnsAudio() const { return owned_; }
    bool UrgentPending() const { return urgent_; }
    const Id& Call() const { return call_; }
    std::uint64_t Owner() const { return owner_; }
    std::uint8_t Mode() const { return mode_; }
    std::uint32_t RxMediaNext() const { return media_rx_; }
    std::uint32_t RxProgressMs(std::uint64_t now) const { return ProgressMs(now,last_rx_); }
    std::uint32_t TxProgressMs(std::uint64_t now) const { return ProgressMs(now,last_tx_); }
    std::uint16_t Health() const { return health_ | (muted_?32:0) | (dnd_?64:0) | (maintenance_?128:0); }

    bool Tick(std::uint64_t now) {
        if (closed_) return false;
        if (now < last_now_) { Close(8); return false; }
        last_now_ = now;
        if (urgent_ && now-urgent_since_ >= 250000) { Close(10); return false; }
        if (state_ != EndpointState::Idle && state_ != EndpointState::Cleanup) {
            if (now >= lease_) End(11);
            else if (state_ == EndpointState::Upgrade && now >= setup_deadline_) {
                state_ = EndpointState::Speaker; Emit(15,2);
            } else if ((state_ == EndpointState::Indicating || state_ == EndpointState::Ringing) && now >= setup_deadline_) End(11);
            else if (Active() && ((now-last_rx_ >= 2000000) || now-last_output_ >= 2000000 ||
                     (state_ == EndpointState::Full && now-last_tx_ >= 2000000))) End(9);
        }
        if (now-last_pong_ >= ClockUs) { Close(10); return false; }
        return true;
    }
    // The transport owns request nonce generation (OS CSPRNG). Only one pending
    // request; timeout is not extended by replacing it with another request.
    bool ExpectPong(std::uint64_t nonce, std::uint64_t now) {
        if (!Tick(now) || pending_ping_) return false;
        pending_ping_ = true; nonce_ = nonce; ping_time_ = now; return true;
    }
    bool Receive(const Frame& f, std::uint64_t now) {
        if (!Tick(now)) return false; // expiry always precedes incoming renewal
        if (!ValidBody(f) || f.type == 10 || f.type == 11 || next_rx_ == UINT64_MAX ||
            std::memcmp(f.body.data(),connection_.data(),16) ||
            std::memcmp(f.body.data()+16,boot_.data(),16) || U64(f.body.data()+32) != next_rx_) return Protocol();
        const auto* b = f.body.data();
        const bool idle_envelope = IsZero(b+40,16) && U64(b+56) == 0;
        const bool owner = owned_ && std::memcmp(b+40,call_.data(),16) == 0 && U64(b+56) == owner_;
        if (f.type == 2) {
            if (IsZero(b+40,16) || U64(b+56) == 0) return Protocol();
            if (state_ == EndpointState::Idle && U64(b+56) <= highest_owner_) return Protocol();
            ++next_rx_;
            if (state_ != EndpointState::Idle) { EmitFor(f,9,1); return true; }
            if (dnd_ || maintenance_ || (b[64] == 1 && muted_) || !clock_.Fresh(now)) {
                EmitFor(f,9,dnd_?2:maintenance_?12:muted_?3:5); return true;
            }
            std::copy(b+40,b+56,call_.begin()); owner_ = highest_owner_ = U64(b+56); mode_ = b[64]; mode_generation_ = 0;
            owned_ = true; indicator_ = false; state_ = EndpointState::Indicating;
            lease_ = Deadline(now,LeaseUs); setup_deadline_ = Deadline(now,10000000);
            last_rx_ = last_tx_ = last_output_ = now; media_rx_ = 0; rx_.Clear(); tx_.Clear(); return true;
        }
        if (f.type == 4 && (idle_envelope || owner)) {
            ++next_rx_;
            if (owner && state_ != EndpointState::Cleanup) {
                std::uint64_t age = 0;
                if (clock_.Age(U64(b+72),0,now,age) && age < LeaseUs)
                    lease_ = std::max(lease_,Deadline(now,LeaseUs-age));
            }
            EmitFor(f,5,0); return !closed_; // PONG serialization/echo belongs to transport
        }
        if (f.type == 5 && (idle_envelope || owner)) {
            if (!pending_ping_ || U64(b+64) != nonce_ || U64(b+72) != ping_time_ ||
                !clock_.Observe(ping_time_,U64(b+80),U64(b+88),now)) return Protocol();
            ++next_rx_; pending_ping_ = false; last_pong_ = now; return true;
        }
        const bool repeated_stop = f.type == 3 && std::memcmp(b+40,last_call_.data(),16) == 0 &&
                                   last_owner_ != 0 && U64(b+56) == last_owner_;
        if (f.type == 3 && (owner || repeated_stop)) { ++next_rx_; if (owner) End(U16(b+64)); return true; }
        if (!owner) return Protocol();
        if (f.type == 6) { ++next_rx_; End(U16(b+64)); return true; }
        if (f.type == 14) {
            ++next_rx_;
            if (state_ != EndpointState::Speaker) { Emit(15,2); return true; }
            if (U32(b+64) != mode_generation_) { Emit(15,3); return true; }
            if (muted_ || !Connected(now)) { Emit(15,muted_?1:2); return true; }
            state_ = EndpointState::Upgrade; setup_deadline_ = Deadline(now,10000000); return true;
        }
        if (f.type == 1) {
            if (!Active() || !AudioGenerationMatches(f,mode_generation_) || U32(b+68) != media_rx_ || media_rx_ == UINT32_MAX) return Protocol();
            std::uint64_t age = 0;
            ++next_rx_; ++media_rx_;
            if (!clock_.Age(U64(b+72),U32(b+80),now,age) || age >= 200000) return true; // drop, no progress
            PcmQueue<6>::Batch batch; std::copy(b+86,b+1110,batch.pcm.begin());
            batch.mode = mode_generation_; batch.expiry = Deadline(now,std::min<std::uint64_t>(192000,200000-age));
            rx_.Push(batch); rx_seen_ = true; last_rx_ = now; return true;
        }
        return Protocol();
    }
    // Completion callbacks must carry the exact owner/mode token; delayed chimes
    // cannot answer a superseded call. Health is measured by the future adapter.
    bool Indication(const Id& call, std::uint64_t owner, std::uint32_t mode,
                    bool led, bool chime_complete, bool output, bool capture, bool aec,
                    std::uint64_t now) {
        if (!Tick(now) || !owned_ || call != call_ || owner != owner_ || mode != mode_generation_) return false;
        if (state_ != EndpointState::Indicating && state_ != EndpointState::Ringing && state_ != EndpointState::Upgrade) return false;
        const bool upgrading = state_ == EndpointState::Upgrade;
        if (!led || !output || ((upgrading || mode_ == 1) && (muted_ || !capture || !aec))) {
            if (upgrading) { state_ = EndpointState::Speaker; Emit(15,2); }
            else End(5);
            return false;
        }
        indicator_ = true;
        health_ = 1 | (capture?2:0) | 4 | (chime_complete?8:0) | (aec?16:0);
        if (!chime_complete) { if (!upgrading) { state_ = EndpointState::Ringing; Emit(7,0); } return true; }
        if (upgrading) {
            if (!BumpMode()) return false;
            mode_ = 1; state_ = EndpointState::Full; Emit(15,0);
        } else { state_ = mode_ == 1 ? EndpointState::Full : EndpointState::Speaker; Emit(8,0); }
        last_rx_ = last_tx_ = last_output_ = now;
        rx_seen_ = tx_seen_ = output_seen_ = false; return !closed_;
    }
    void LocalPolicy(bool muted, bool dnd, bool maintenance, std::uint64_t now) {
        if (!Tick(now)) return;
        muted_ = muted; dnd_ = dnd; maintenance_ = maintenance;
        if (!owned_) return;
        if (dnd || maintenance) { End(dnd?2:12); return; }
        if (muted && (state_ == EndpointState::Full || state_ == EndpointState::Upgrade)) {
            if (!BumpMode()) return;
            mode_ = 2; state_ = EndpointState::Speaker; Emit(12,0);
        } else if (muted && mode_ == 1) End(3);
        // Clearing mute never enables TX; explicit owner upgrade is mandatory.
    }
    void LocalStop(std::uint64_t now) { if (Tick(now) && owned_) End(13); }
    void LostTransport() { Close(10); }
    bool OutputProgress(const Id& call, std::uint64_t owner, std::uint32_t generation,
                        bool indicator, std::uint64_t now) {
        if (!Tick(now) || !owned_ || call != call_ || owner != owner_ || generation != mode_generation_) return false;
        if (!indicator) { End(5); return false; }
        output_seen_ = true; last_output_ = now; return true;
    }
    void FinishCleanup() { if (state_ == EndpointState::Cleanup && notices_size_ == 0) { state_ = EndpointState::Idle; mode_ = 0; call_ = {}; owner_ = 0; } }
    bool Capture(const std::array<std::uint8_t,1024>& pcm, std::uint32_t generation,
                 std::uint64_t capture_age, std::uint64_t now) {
        if (!Tick(now) || !MicAllowed() || generation != mode_generation_ || capture_age >= 200000) return false;
        PcmQueue<3>::Batch batch; batch.pcm = pcm; batch.mode = generation;
        batch.expiry = Deadline(now,std::min<std::uint64_t>(96000,200000-capture_age));
        tx_.Push(batch); tx_seen_ = true; last_tx_ = now; return true;
    }
    bool TakeTx(std::uint64_t now, PcmQueue<3>::Batch& out) {
        out = {}; return Tick(now) && MicAllowed() && notices_size_ == 0 && tx_.Pop(now,mode_generation_,out);
    }
    bool TakeRx(std::uint64_t now, PcmQueue<6>::Batch& out) {
        out = {}; return Tick(now) && Active() && rx_.Pop(now,mode_generation_,out);
    }
    bool PeekNotice(Notice& out) const {
        out = {}; if (!notices_size_) return false;
        out = notices_[0]; return true;
    }
    // Acknowledge only after complete serialization to the TLS writer. Merely
    // copying/starting a nonblocking write must not cancel the urgent deadline.
    bool NoticeFlushed(std::uint64_t serial) {
        if (!notices_size_ || notices_[0].serial != serial) return false;
        for (std::size_t i=1;i<notices_size_;++i) notices_[i-1] = notices_[i];
        notices_[--notices_size_] = {}; if (!notices_size_) urgent_ = false; return true;
    }
private:
    static std::uint32_t ProgressMs(std::uint64_t now,std::uint64_t progress) {
        return now>=progress?static_cast<std::uint32_t>(std::min<std::uint64_t>(UINT32_MAX,(now-progress)/1000)):UINT32_MAX;
    }
    bool Active() const { return state_ == EndpointState::Full || state_ == EndpointState::Speaker || state_ == EndpointState::Upgrade; }
    bool Protocol() { Close(8); return false; }
    bool BumpMode() {
        if (!IncrementMode(mode_generation_)) { Close(7); return false; }
        rx_seen_ = tx_seen_ = output_seen_ = false;
        rx_.Clear(); tx_.Clear(); notices_ = {}; notices_size_ = 0; urgent_ = false; return true;
    }
    void Emit(std::uint8_t type, std::uint16_t code) {
        if (notices_size_ == notices_.size() || notice_serial_ == UINT64_MAX) { Close(10); return; }
        if (type == 6 || type == 9 || type == 13) tx_.Clear();
        if (!urgent_ && (type == 6 || type == 9 || type == 13)) { urgent_ = true; urgent_since_ = last_now_; }
        notices_[notices_size_++] = {type,mode_,code,mode_generation_,call_,owner_,++notice_serial_};
        notices_[notices_size_-1].state = static_cast<std::uint8_t>(state_);
        notices_[notices_size_-1].health = Health();
    }
    void EmitFor(const Frame& f, std::uint8_t type, std::uint16_t code) {
        if (notices_size_ == notices_.size() || notice_serial_ == UINT64_MAX) { Close(10); return; }
        if (type == 6 || type == 9 || type == 13) tx_.Clear();
        if (!urgent_ && (type == 6 || type == 9 || type == 13)) { urgent_ = true; urgent_since_ = last_now_; }
        Id call; std::copy(f.body.begin()+40,f.body.begin()+56,call.begin());
        notices_[notices_size_++] = {type,0,code,0,call,U64(f.body.data()+56),++notice_serial_};
        if (type == 5) {
            auto& notice = notices_[notices_size_-1];
            notice.nonce = U64(f.body.data()+64); notice.t1 = U64(f.body.data()+72); notice.t2 = last_now_;
        }
    }
    void End(std::uint16_t reason) {
        rx_.Clear(); tx_.Clear(); indicator_ = owned_ = false;
        rx_seen_ = tx_seen_ = output_seen_ = false;
        last_call_ = call_; last_owner_ = owner_; state_ = EndpointState::Cleanup;
        notices_ = {}; notices_size_ = 0; urgent_ = false; Emit(13,reason);
    }
    void Close(std::uint16_t reason) {
        // Do not recurse through Emit on notice-serial exhaustion.
        if (owned_ && notice_serial_ != UINT64_MAX) End(reason);
        closed_ = true; owned_ = indicator_ = false; rx_.Clear(); tx_.Clear();
    }
    Id connection_{}, boot_{}, call_{}, last_call_{};
    std::uint64_t owner_ = 0, last_owner_ = 0, highest_owner_ = 0, next_rx_ = 0, lease_ = 0, setup_deadline_ = 0;
    std::uint64_t notice_serial_ = 0;
    std::uint64_t last_now_ = 0, last_pong_ = 0, last_rx_ = 0, last_tx_ = 0, last_output_ = 0, nonce_ = 0, ping_time_ = 0, urgent_since_ = 0;
    std::uint32_t mode_generation_ = 0, media_rx_ = 0;
    std::uint8_t mode_ = 0;
    std::uint16_t health_ = 0;
    EndpointState state_ = EndpointState::Idle;
    bool closed_ = false, owned_ = false, indicator_ = false, muted_ = true, dnd_ = false, maintenance_ = false, pending_ping_ = false, urgent_ = false;
    bool rx_seen_ = false, tx_seen_ = false, output_seen_ = false;
    ClockRelation clock_;
    PcmQueue<3> tx_;
    PcmQueue<6> rx_;
    std::array<Notice,16> notices_{};
    std::size_t notices_size_ = 0;
};
} // namespace intercom

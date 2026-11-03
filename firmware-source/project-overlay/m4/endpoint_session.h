#pragma once
#include "endpoint_controller.h"
#include <optional>

namespace intercom {
inline void PutLe(std::uint8_t* p, std::uint64_t value, unsigned count) {
    for (unsigned i=0;i<count;++i) p[i] = static_cast<std::uint8_t>(value >> (8*i));
}
struct SessionConfig { Id device{}, boot{}, connection{}; };

// Shared host/vendor adapter seam. Socket authentication is completed by
// TlsStream BEFORE construction. Physical I/O is supplied by later vendor hooks;
// this module never invents successful indication/capture/render progress.
class EndpointSession {
public:
    EndpointSession(SessionConfig config, std::uint64_t now) : config_(config), ack_deadline_(Deadline(now,5000000)) {
        if (IsZero(config.device.data(),16)||IsZero(config.boot.data(),16)||IsZero(config.connection.data(),16)) { closed_=true;return; }
        Frame hello;hello.type=10;hello.size=67;
        auto* b=hello.body.data();std::memcpy(b,"ICM2",4);PutLe(b+4,2,2);PutLe(b+6,2,2);
        std::copy(config.device.begin(),config.device.end(),b+8);
        std::copy(config.boot.begin(),config.boot.end(),b+24);
        std::copy(config.connection.begin(),config.connection.end(),b+40);
        PutLe(b+56,31,4);PutLe(b+60,1,4);b[64]=2;b[65]='m';b[66]='4';
        SetOutput(hello,0,false,false);
    }
    bool Closed() const { return closed_ || (core_ && core_->Closed()); }
    EndpointController* Core() { return core_?&*core_:nullptr; }
    bool Ready() const { return core_.has_value()&&!Closed(); }
    void Close() { closed_=true;if(core_)core_->LostTransport();output_.fill(0);size_=offset_=0; }
    bool Tick(std::uint64_t now) {
        tick_now_=now;
        if(Closed()) { Close();return false; }
        if(!core_) { if(now>=ack_deadline_) {Close();return false;} return true; }
        if(!core_->Tick(now)) {Close();return false;}
        // A half-written superseded permission/audio frame cannot be spliced
        // with a replacement. Close the stream if any TLS write was attempted.
        if(size_ && !PendingValid()) {
            if(attempted_) {Close();return false;}
            output_.fill(0);size_=offset_=0;
        }
        return true;
    }
    bool Input(const std::uint8_t* bytes, std::size_t count, std::uint64_t now) {
        if(!Tick(now))return false;
        const bool ok=parser_.Feed(bytes,count,[&](const Frame& frame) {
            if(!core_) {
                const auto* b=frame.body.data();
                if(frame.type!=11 || !ValidBody(frame) || std::memcmp(b+8,config_.device.data(),16) ||
                    std::memcmp(b+24,config_.boot.data(),16)||std::memcmp(b+40,config_.connection.data(),16) || size_)return false;
                core_.emplace(config_.connection,config_.boot,now);next_ping_=now;return true;
            }
            const auto old_owner=core_->Owner();
            if(!core_->Receive(frame,now))return false;
            if(frame.type==2 && core_->Owner()!=old_owner && core_->OwnsAudio()) tx_media_=0;
            return true;
        });
        if(!ok)Close();
        return ok;
    }
    // Pollable writer: same buffer/range is retained across WANT_READ/WRITE.
    // Caller must Tick even when the socket cannot write and invoke Written only
    // for positive plaintext-byte counts accepted by the authenticated TLS API.
    const std::uint8_t* Output(std::uint64_t now,std::size_t& count,std::uint64_t nonce) {
        count=0;if(!Tick(now))return nullptr;
        if(size_) {count=size_-offset_;return output_.data()+offset_;}
        if(!core_)return nullptr;
        Notice notice;
        if(core_->PeekNotice(notice)) {
            Frame frame=Envelope(notice.type,notice.call,notice.owner);
            auto* b=frame.body.data();
            switch(notice.type) {
                case 5:frame.size=96;PutLe(b+64,notice.nonce,8);PutLe(b+72,notice.t1,8);PutLe(b+80,notice.t2,8);PutLe(b+88,now,8);break;
                case 7:frame.size=65;b[64]=3;break;
                case 8:frame.size=72;b[64]=notice.mode;b[65]=1;PutLe(b+66,notice.health,2);PutLe(b+68,notice.mode_generation,4);break;
                case 9:case 13:frame.size=66;PutLe(b+64,notice.code,2);break;
                case 12:frame.size=88;b[64]=notice.state;b[65]=notice.mode;PutLe(b+66,notice.health,2);PutLe(b+68,notice.mode_generation,4);
                    PutLe(b+72,core_->RxMediaNext()?core_->RxMediaNext()-1:0,4);PutLe(b+76,tx_media_?tx_media_-1:0,4);
                    PutLe(b+80,core_->RxProgressMs(now),4);PutLe(b+84,core_->TxProgressMs(now),4);break;
                case 15:frame.size=71;PutLe(b+64,notice.mode_generation,4);b[68]=static_cast<std::uint8_t>(notice.code);PutLe(b+69,notice.health,2);break;
                default:Close();return nullptr;
            }
            if(!ValidBody(frame)) {Close();return nullptr;}
            SetOutput(frame,notice.serial,false,true);
        } else if(now>=next_ping_ && core_->ExpectPong(nonce,now)) {
            Frame ping=Envelope(4,Id{},0);ping.size=80;PutLe(ping.body.data()+64,nonce,8);PutLe(ping.body.data()+72,now,8);
            SetOutput(ping,0,false,true);next_ping_=Deadline(now,1000000);
        } else {
            PcmQueue<3>::Batch batch;
            if(core_->TakeTx(now,batch)) {
                if(tx_media_==UINT32_MAX) {Close();return nullptr;}
                Frame audio=Envelope(1,core_->Call(),core_->Owner());audio.size=1110;
                auto* b=audio.body.data();PutLe(b+64,batch.mode,4);PutLe(b+68,tx_media_,4);PutLe(b+72,now,8);
                // This intentionally conservative bound also includes sender
                // queue residence. It never under-reports age from an expiring batch.
                PutLe(b+80,200000-std::min<std::uint64_t>(200000,batch.expiry-now),4);PutLe(b+84,512,2);
                std::copy(batch.pcm.begin(),batch.pcm.end(),b+86);
                SetOutput(audio,0,true,true);pcm_expiry_=batch.expiry;pcm_mode_=batch.mode;
            } else if(core_->State()==EndpointState::Cleanup)core_->FinishCleanup();
        }
        if(size_) {count=size_;return output_.data();}return nullptr;
    }
    bool Attempt(std::uint64_t now) {
        if(!Tick(now)||!size_)return false;
        if(!attempted_ && sequenced_) {if(next_tx_==UINT64_MAX){Close();return false;}++next_tx_;}
        attempted_=true;return true;
    }
    bool Written(std::size_t count,std::uint64_t now) {
        if(!Tick(now)||!attempted_||count==0||count>size_-offset_) {Close();return false;}
        offset_+=count;
        if(offset_!=size_)return true;
        if(serial_ && !core_->NoticeFlushed(serial_)) {Close();return false;}
        if(pcm_)++tx_media_;
        output_.fill(0);size_=offset_=0;attempted_=false;serial_=0;return true;
    }
private:
    Frame Envelope(std::uint8_t type,const Id& call,std::uint64_t owner) const {
        Frame frame;frame.type=type;auto* b=frame.body.data();
        std::copy(config_.connection.begin(),config_.connection.end(),b);
        std::copy(config_.boot.begin(),config_.boot.end(),b+16);PutLe(b+32,next_tx_,8);
        std::copy(call.begin(),call.end(),b+40);PutLe(b+56,owner,8);return frame;
    }
    void SetOutput(const Frame& frame,std::uint64_t serial,bool pcm,bool sequenced) {
        output_.fill(0);output_[0]=frame.type;PutLe(output_.data()+1,frame.size,2);
        std::copy_n(frame.body.begin(),frame.size,output_.begin()+3);
        size_=frame.size+3;offset_=0;serial_=serial;pcm_=pcm;sequenced_=sequenced;attempted_=false;
    }
    bool PendingValid() const {
        if(!core_)return true;
        if(serial_) {Notice n;return core_->PeekNotice(n)&&n.serial==serial_;}
        if(pcm_)return (attempted_||!core_->UrgentPending())&&core_->MicAllowed()&&core_->ModeGeneration()==pcm_mode_&&now_for_expiry()<pcm_expiry_;
        return true;
    }
    // Tick supplies the monotonic time used for every pending write recheck.
    std::uint64_t now_for_expiry() const {return tick_now_;}
    SessionConfig config_;
    StreamParser parser_;
    std::optional<EndpointController> core_;
    std::array<std::uint8_t,2051> output_{};
    std::size_t size_=0,offset_=0;
    std::uint64_t ack_deadline_=0,next_ping_=0,next_tx_=0,serial_=0,pcm_expiry_=0,tick_now_=0;
    std::uint32_t tx_media_=0,pcm_mode_=0;
    bool closed_=false,pcm_=false,sequenced_=false,attempted_=false;
};
} // namespace intercom

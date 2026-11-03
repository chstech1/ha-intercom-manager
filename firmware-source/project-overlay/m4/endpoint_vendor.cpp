#include "endpoint_vendor.h"
#include <cerrno>
#include <sys/random.h>
#include <time.h>

namespace intercom {
bool SecureRandom(std::uint8_t* data,std::size_t size) {
    while(size) {
        const auto count=getrandom(data,size,GRND_NONBLOCK);
        if(count<0 && errno==EINTR)continue;
        if(count<=0)return false;
        data+=count;size-=static_cast<std::size_t>(count);
    }
    return true;
}
std::uint64_t MonotonicUs() {
    timespec value{};
    if(clock_gettime(CLOCK_MONOTONIC,&value)!=0)return 0;
    return static_cast<std::uint64_t>(value.tv_sec)*1000000+static_cast<std::uint64_t>(value.tv_nsec)/1000;
}
EndpointAdapter::EndpointAdapter(int fd,const TlsConfig& trust,const Id& device,const Id& boot,std::uint64_t now)
    :tls_(fd,trust,true),device_(device),boot_(boot),handshake_deadline_(Deadline(now,5000000)) {
    closed_=!tls_.Valid()||IsZero(device.data(),device.size())||IsZero(boot.data(),boot.size())||now==0;
}
EndpointAdapter::~EndpointAdapter()=default;
bool EndpointAdapter::Closed() const {return closed_||(session_&&session_->Closed());}
EndpointSession* EndpointAdapter::Session() {return session_.get();}
bool EndpointAdapter::Service(std::uint64_t now) {
    auto fail=[&] {closed_=true;if(session_)session_->Close();return false;};
    if(Closed()||now==0)return fail();
    if(!session_) {
        if(now>=handshake_deadline_)return fail();
        const auto result=tls_.Handshake();
        if(result.code==IoCode::Again)return true;
        if(result.code!=IoCode::Done)return fail();
        SessionConfig config;config.device=device_;config.boot=boot_;
        if(!SecureRandom(config.connection.data(),config.connection.size()))return fail();
        session_.reset(new EndpointSession(config,now));
    }
    if(!session_->Tick(now))return fail();
    // Retain a cross-direction TLS retry before issuing another operation.
    // Ordinary Read/WANT_READ permits a new outbound application write; a
    // Write/WANT_* or Read/WANT_WRITE must retry its original buffer first.
    if(pending_!=Pending::Write) {
        const auto read=tls_.Read(input_.data(),input_.size());
        if(read.code==IoCode::Done) {
            pending_=Pending::None;
            if(read.count && !session_->Input(input_.data(),read.count,now))return fail();
            input_.fill(0);
        } else if(read.code==IoCode::Again) {
            if(read.want_write||pending_==Pending::Read) {pending_=Pending::Read;return true;}
        } else return fail();
    }
    std::size_t size=0;std::uint64_t nonce=0;
    if(!SecureRandom(reinterpret_cast<std::uint8_t*>(&nonce),sizeof(nonce)))return fail();
    const auto* output=session_->Output(now,size,nonce);
    if(output && size) {
        if(!session_->Attempt(now))return fail();
        const auto written=tls_.Write(output,size);
        if(written.code==IoCode::Done) {
            pending_=Pending::None;
            if(!session_->Written(written.count,now))return fail();
        } else if(written.code==IoCode::Again)pending_=Pending::Write;
        else return fail();
    }
    return !Closed();
}
} // namespace intercom

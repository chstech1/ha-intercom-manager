#pragma once
#include "endpoint_session.h"
#include "endpoint_tls.h"
#include <memory>

namespace intercom {
// Reusable adapter for the vendor's auxiliary-FD/timer owner. Owns no listener
// or hardware. Service must be scheduled even during TLS backpressure; real
// privacy/health hooks are supplied by M4A/M5 onward, never remote commands.
class EndpointAdapter {
public:
    EndpointAdapter(int fd,const TlsConfig& trust,const Id& device,const Id& boot,std::uint64_t now);
    ~EndpointAdapter();
    EndpointAdapter(const EndpointAdapter&)=delete;
    EndpointAdapter& operator=(const EndpointAdapter&)=delete;
    bool Service(std::uint64_t now);
    bool Closed() const;
    int FailureCode() const {return tls_.FailureCode();}
    EndpointSession* Session();
private:
    TlsStream tls_;
    Id device_{},boot_{};
    std::unique_ptr<EndpointSession> session_;
    std::uint64_t handshake_deadline_=0;
    std::array<std::uint8_t,2048> input_{};
    enum class Pending {None,Read,Write};
    Pending pending_=Pending::None;
    bool closed_=false;
};
bool SecureRandom(std::uint8_t* data,std::size_t size);
std::uint64_t MonotonicUs();
} // namespace intercom

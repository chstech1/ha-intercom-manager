#include "endpoint_session.h"
#include <cassert>
#include <iostream>
using namespace intercom;

// Uses the source archive's transformed ICM4 session, no device or actual TLS.
struct Rig {
    SessionConfig config{{1},{2},{3},4};
    EndpointSession session{config,1000000};
    std::uint64_t now=1000000,sequence=0;
    Id call{4};
    Frame drain() {
        std::size_t n=0;const auto* bytes=session.Output(now,n,7);assert(bytes&&n);
        Frame result;StreamParser parser;
        assert(parser.Feed(bytes,n,[&](const Frame& f){result=f;return true;}));
        assert(parser.End()&&session.Attempt(now)&&session.Written(n,now));return result;
    }
    Frame incoming(std::uint8_t type,std::uint16_t size,bool owner=true) {
        Frame f;f.type=type;f.size=size;auto* b=f.body.data();
        std::copy(config.connection.begin(),config.connection.end(),b);
        std::copy(config.boot.begin(),config.boot.end(),b+16);PutLe(b+32,sequence++,8);
        if(owner){std::copy(call.begin(),call.end(),b+40);PutLe(b+56,1,8);}return f;
    }
    void feed(const Frame& f) {
        std::array<std::uint8_t,2051> bytes{};bytes[0]=f.type;PutLe(bytes.data()+1,f.size,2);
        std::copy_n(f.body.begin(),f.size,bytes.begin()+3);
        assert(session.Input(bytes.data(),f.size+3,now));
    }
    Rig() {
        assert(drain().type==10);
        Frame ack;ack.type=11;ack.size=60;auto* b=ack.body.data();std::memcpy(b,"ICM4",4);PutLe(b+4,4,2);
        std::copy(config.device.begin(),config.device.end(),b+8);std::copy(config.boot.begin(),config.boot.end(),b+24);
        std::copy(config.connection.begin(),config.connection.end(),b+40);PutLe(b+56,1,4);feed(ack);
        auto ping=drain();assert(ping.type==4);
        auto pong=incoming(5,96,false);std::copy_n(ping.body.data()+64,16,pong.body.data()+64);
        PutLe(pong.body.data()+80,now,8);PutLe(pong.body.data()+88,now,8);feed(pong);
        session.Core()->LocalPolicy(false,false,false,now);
        auto start=incoming(2,71);start.body[64]=1;start.body[65]=1;feed(start);
        assert(session.Core()->Indication(call,1,0,true,false,true,true,true,now));assert(drain().type==7);
        assert(session.Core()->Indication(call,1,0,true,true,true,true,true,now));assert(drain().type==8);
        assert(session.Core()->MicAllowed());
    }
    void select(int kind) {
        auto* core=session.Core();
        if(kind==1){std::array<std::uint8_t,1024> pcm{};assert(core->Capture(pcm,0,0,now));}
        if(kind==5){auto ping=incoming(4,80);PutLe(ping.body.data()+64,123,8);PutLe(ping.body.data()+72,now,8);feed(ping);}
        if(kind==12)assert(core->ReportStatus(31,now));
        std::size_t n=0;const auto* data=session.Output(now,n,7);assert(data&&n&&data[0]==kind);
    }
};
int main() {
    for(int kind : {1,5,12})for(bool attempted : {false,true}) {
        Rig r;r.select(kind);if(attempted)assert(r.session.Attempt(r.now)); // models a TLS WANT: no Written() call
        r.session.Core()->LocalPolicy(true,false,false,r.now);
        const bool alive=r.session.Tick(r.now);
        assert(alive==!attempted);assert(r.session.Closed()==attempted);
        assert(!r.session.Core()->MicAllowed());
        if(!attempted){
            auto status=r.drain();assert(status.type==12&&status.body[64]==4&&status.body[65]==2);
            auto* core=r.session.Core();core->LocalPolicy(false,false,false,r.now);assert(!core->MicAllowed());
            auto audio=r.incoming(1,1110);PutLe(audio.body.data()+64,1,4);PutLe(audio.body.data()+68,0,4);
            PutLe(audio.body.data()+72,r.now,8);PutLe(audio.body.data()+84,512,2);r.feed(audio);
            assert(core->OutputProgress(r.call,1,1,true,r.now));
            auto request=r.incoming(14,68);PutLe(request.body.data()+64,1,4);r.feed(request);
            assert(core->State()==EndpointState::Upgrade&&!core->MicAllowed());
            assert(core->Indication(r.call,1,1,true,false,true,true,true,r.now));assert(!core->MicAllowed());
            assert(core->Indication(r.call,1,1,true,true,true,true,true,r.now));assert(core->MicAllowed());
        }
        std::cout<<"type="<<kind<<" attempted="<<attempted<<" alive="<<alive<<" mic="<<r.session.Core()->MicAllowed()<<"\n";
    }
    for(int kind : {1,5,12}) {
        Rig r;r.select(kind);assert(r.session.Attempt(r.now));assert(r.session.Written(1,r.now));
        r.session.Core()->LocalPolicy(true,false,false,r.now);
        assert(!r.session.Tick(r.now)&&r.session.Closed()&&!r.session.Core()->MicAllowed());
        std::cout<<"type="<<kind<<" accepted=1 alive=0 mic=0\n";
    }
    std::cout<<"PASS: transformed ICM4 session closes superseded attempted or partially written PCM/PONG/STATUS; unattempted output is discarded, and unmute alone cannot reopen capture before a new completed indication. TLS WANT is modeled, and field cause remains unproved.\n";
}

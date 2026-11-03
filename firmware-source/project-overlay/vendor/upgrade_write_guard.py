"""Candidate guard after the pinned controller/manager transformations."""
from pathlib import Path

def apply(directory):
    root = Path(directory)
    def change(name, old, new):
        p=root/name; text=p.read_text(encoding='utf-8')
        if text.count(old)!=1: raise ValueError(f'{name}: unexpected source: {old}')
        p.write_text(text.replace(old,new,1),encoding='utf-8')
    change('endpoint_session.h',
        '    const std::atomic<bool>* local_pause = nullptr; std::uint32_t manager_capabilities = 0; };',
        '    const std::atomic<bool>* local_pause = nullptr; const std::atomic<bool>* local_mute = nullptr; std::uint32_t manager_capabilities = 0; };')
    change('endpoint_session.h', '    bool Ready() const { return core_.has_value()&&!Closed(); }',
        '    bool Ready() const { return core_.has_value()&&!Closed(); }\n    bool PendingMic() const { return size_ && pcm_; }')
    change('endpoint_session.h',
        '        if(pcm_)return (attempted_||!core_->UrgentPending())',
        '        if(pcm_)return (!config_.local_mute || !config_.local_mute->load(std::memory_order_acquire)) && (attempted_||!core_->UrgentPending())')
    change('endpoint_vendor.h',
        'const std::atomic<bool>* local_pause = nullptr, std::uint32_t capabilities = 0);',
        'const std::atomic<bool>* local_pause = nullptr, std::uint32_t capabilities = 0, const std::atomic<bool>* local_mute = nullptr);')
    change('endpoint_vendor.h',
        '    const std::atomic<bool>* local_pause_ = nullptr; std::uint32_t capabilities_ = 0;',
        '    const std::atomic<bool>* local_pause_ = nullptr; std::uint32_t capabilities_ = 0;\n    const std::atomic<bool>* local_mute_ = nullptr;')
    change('endpoint_vendor.cpp',
        'const std::atomic<bool>* local_pause,std::uint32_t capabilities)\n',
        'const std::atomic<bool>* local_pause,std::uint32_t capabilities,const std::atomic<bool>* local_mute)\n')
    change('endpoint_vendor.cpp',
        'local_pause_(local_pause),capabilities_(capabilities),handshake_deadline_',
        'local_pause_(local_pause),capabilities_(capabilities),local_mute_(local_mute),handshake_deadline_')
    change('endpoint_vendor.cpp',
        'config.local_pause=local_pause_;config.manager_capabilities=capabilities_;',
        'config.local_pause=local_pause_;config.local_mute=local_mute_;config.manager_capabilities=capabilities_;')
    change('endpoint_vendor.cpp',
        '        const auto written=tls_.Write(output,size);',
        '        if (session_->PendingMic() && local_mute_ && local_mute_->load(std::memory_order_acquire)) return fail();\n'
        '        const auto written=tls_.Write(output,size);')

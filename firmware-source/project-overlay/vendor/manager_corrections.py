"""Apply manager changes only to the disposable copy of the accepted headers."""
import pathlib
from controller_corrections import replace_once


def apply(directory):
    root = pathlib.Path(directory)
    frame, controller, session = (root / name for name in
                                  ('endpoint_frame.h', 'endpoint_controller.h', 'endpoint_session.h'))
    replace_once(frame, 'case 15: return size == 71;',
                 'case 15: return size == 71;\n        case 16: return size == 77;\n        case 17: return size == 88;')
    replace_once(frame, 'b[65] == 1 && U16(b + 66) == 0',
                 '(b[65] == 1 || b[65] == 2) && U16(b + 66) == 0')
    replace_once(frame, ' && U64(b + 64) >> 32 == 0;', ';')
    for kind in (10, 11):
        replace_once(frame, f'case {kind}: return std::memcmp(b, "ICM3", 4) == 0 && U16(b + 4) == 3',
                     f'case {kind}: return ((std::memcmp(b, "ICM3", 4) == 0 && U16(b + 4) == 3) || '
                     '(std::memcmp(b, "ICM4", 4) == 0 && U16(b + 4) == 4))')
    replace_once(frame, '        case 15: return b[68] <= 4',
                 '        case 17: return IsZero(b+40,16) && U64(b+56) == 0 && U64(b+64) != 0 && !IsZero(b+72,16);\n'
                 '        case 16: return b[68] >= 1 && b[68] <= 3 && '
                 '(b[68] != 2 || U64(b + 69) == 0);\n        case 15: return b[68] <= 4')
    replace_once(controller, 'EndpointController(Id connection, Id boot, std::uint64_t now)\n'
                 '        : connection_(connection), boot_(boot), last_now_(now), last_pong_(now) {}',
                 'EndpointController(Id connection, Id boot, std::uint64_t now, unsigned profile = 3)\n'
                 '        : profile_(profile), acceptance_counter_(U64(connection.data())),\n'
                 '          connection_(connection), boot_(boot), last_now_(now), last_pong_(now) {}')
    replace_once(controller, '            if (IsZero(b+40,16) || U64(b+56) == 0) return Protocol();',
                 '            if (IsZero(b+40,16) || U64(b+56) == 0 || '
                 '(b[65] == 2 && profile_ != 4)) return Protocol();')
    replace_once(controller, '            owned_ = true; indicator_ = false; state_ = EndpointState::Indicating;',
                 '            manual_ = b[65] == 2; accepted_ = notice_complete_ = false; acceptance_token_ = 0;\n'
                 '            owned_ = true; indicator_ = false; state_ = EndpointState::Indicating;')
    replace_once(controller, '        if (f.type == 14) {',
                 '        if (f.type == 16) {\n'
                 '            if (profile_ != 4 || U32(b+64) != mode_generation_) return Protocol();\n'
                 '            ++next_rx_;\n'
                 '            if (b[68] == 2) { End(13); return true; }\n'
                 '            if (b[68] == 1) {\n'
                 '                if (U64(b+69) != 0) return Protocol();\n'
                 '                return LocalAccept(now);\n'
                 '            }\n'
                 '            if (!manual_ || !accepted_ || !notice_complete_ || '
                 '!acceptance_token_ || U64(b+69) != acceptance_token_) return Protocol();\n'
                 '            if (Active()) return true; // exact duplicate commit\n'
                 '            if (state_ != EndpointState::Ringing || (muted_ && mode_ == 1)) return Protocol();\n'
                 '            Activate(now); return !closed_;\n'
                 '        }\n'
                 '        if (f.type == 14) {')
    replace_once(controller, '        } else { state_ = mode_ == 1 ? EndpointState::Full : EndpointState::Speaker; Emit(8,0); }',
                 '        } else {\n'
                 '            notice_complete_ = true;\n'
                 '            if (manual_) { state_ = EndpointState::Ringing; EmitAcceptance(); return !closed_; }\n'
                 '            Activate(now);\n'
                 '        }')
    replace_once(controller, '    void LocalStop(std::uint64_t now) {',
                 '    bool CanRequestSource(std::uint64_t now) const {\n'
                 '        return !closed_ && profile_ == 4 && state_ == EndpointState::Idle && '
                 '!muted_ && !dnd_ && !maintenance_ && clock_.Fresh(now) && '
                 '(!source_deadline_ || now >= source_deadline_);\n'
                 '    }\n'
                 '    bool LocalSourceRequest(const Id& target, std::uint64_t now) {\n'
                 '        if (!Tick(now) || profile_ != 4 || state_ != EndpointState::Idle || '
                 'muted_ || dnd_ || maintenance_ || !clock_.Fresh(now) || IsZero(target.data(),16) || '
                 '(source_deadline_ && now < source_deadline_)) return false;\n'
                 '        if (!now || source_nonce_ >= now) return false;\n'
                 '        source_deadline_ = Deadline(now,15000000); source_nonce_ = now; Emit(17,0);\n'
                 '        if (notices_size_) { auto& n=notices_[notices_size_-1]; n.nonce=source_nonce_; n.target=target; }\n'
                 '        return !closed_;\n'
                 '    }\n'
                 '    bool LocalAccept(std::uint64_t now) {\n'
                 '        if (!Tick(now) || profile_ != 4 || !owned_ || !manual_) return false;\n'
                 '        if (accepted_) return true;\n'
                 '        if (state_ != EndpointState::Indicating && state_ != EndpointState::Ringing) return false;\n'
                 '        if (acceptance_counter_ == UINT64_MAX) return Protocol();\n'
                 '        acceptance_token_ = ++acceptance_counter_; accepted_ = true; EmitAcceptance(); return !closed_;\n'
                 '    }\n'
                 '    bool WaitingForAnswer() const { return manual_ && owned_ && !Active(); }\n'
                 '    void LocalStop(std::uint64_t now) {')
    replace_once(controller, 'if (!Tick(now) || !owned_) return false;\n        if (last_status_report_',
                 'if (!Tick(now) || (!owned_ && profile_ != 4)) return false;\n        if (last_status_report_')
    replace_once(controller, 'private:\n    static std::uint32_t ProgressMs',
                 'private:\n'
                 '    void Activate(std::uint64_t now) {\n'
                 '        state_ = mode_ == 1 ? EndpointState::Full : EndpointState::Speaker; Emit(8,0);\n'
                 '        last_rx_ = last_tx_ = last_output_ = now; rx_seen_ = tx_seen_ = output_seen_ = false;\n'
                 '    }\n'
                 '    void EmitAcceptance() {\n'
                 '        if (accepted_ && notice_complete_ && !acceptance_emitted_) {\n'
                 '            acceptance_emitted_ = true; Emit(16,1);\n'
                 '            if (notices_size_) notices_[notices_size_-1].nonce = acceptance_token_;\n'
                 '        }\n'
                 '    }\n'
                 '    static std::uint32_t ProgressMs')
    replace_once(controller, 'manual_ = b[65] == 2; accepted_ = notice_complete_ = false;',
                 'manual_ = b[65] == 2; accepted_ = notice_complete_ = acceptance_emitted_ = false;')
    replace_once(controller, '    Id connection_{}, boot_{}, call_{}, last_call_{};',
                 '    unsigned profile_ = 3;\n'
                 '    std::uint64_t source_nonce_ = 0, source_deadline_ = 0;\n'
                 '    std::uint64_t acceptance_counter_ = 0, acceptance_token_ = 0;\n'
                 '    bool manual_ = false, accepted_ = false, notice_complete_ = false, acceptance_emitted_ = false;\n'
                 '    Id connection_{}, boot_{}, call_{}, last_call_{};')
    replace_once(controller, 'std::uint8_t state = 0; std::uint16_t health = 0; };',
                 'std::uint8_t state = 0; std::uint16_t health = 0; Id target{}; };')
    replace_once(controller, '        last_now_ = now;\n',
                 '        last_now_ = now;\n'
                 '        if (source_deadline_ && now >= source_deadline_) {\n'
                 '            for (std::size_t i=0;i<notices_size_;) {\n'
                 '                if (notices_[i].type != 17) { ++i; continue; }\n'
                 '                for (std::size_t j=i+1;j<notices_size_;++j) notices_[j-1]=notices_[j];\n'
                 '                notices_[--notices_size_] = {};\n'
                 '            }\n'
                 '        }\n')
    replace_once(session, '#include <optional>', '#include <optional>\n#include <atomic>')
    replace_once(session, 'struct SessionConfig { Id device{}, boot{}, connection{}; };',
                 'struct SessionConfig { Id device{}, boot{}, connection{}; unsigned profile = 3;\n'
                 '    const std::atomic<bool>* local_cutoff = nullptr;\n'
                 '    const std::atomic<bool>* local_pause = nullptr; std::uint32_t manager_capabilities = 0; };')
    replace_once(session, 'std::memcpy(b,"ICM3",4);PutLe(b+4,3,2);',
                 'std::memcpy(b,config_.profile == 4 ? "ICM4" : "ICM3",4);PutLe(b+4,config_.profile,2);')
    replace_once(session, 'PutLe(b+56,31,4);',
                 'PutLe(b+56,config_.profile == 4 ? (63 | config_.manager_capabilities) : 31,4);')
    replace_once(session, '        if (IsZero(config.device.data(),16)',
                 '        if ((config.profile != 3 && config.profile != 4) || IsZero(config.device.data(),16)')
    replace_once(session, '        if(!core_->Tick(now))',
                 '        if(config_.local_cutoff && config_.local_cutoff->load(std::memory_order_acquire)) core_->LocalStop(now);\n'
                 '        if(!core_->Tick(now))')
    replace_once(session, 'if(frame.type!=11 || !ValidBody(frame)',
                 'if(frame.type!=11 || U16(b+4) != config_.profile || !ValidBody(frame)')
    replace_once(session, 'core_.emplace(config_.connection,config_.boot,now);',
                 'core_.emplace(config_.connection,config_.boot,now,config_.profile);')
    replace_once(session, '                case 15:frame.size=71;',
                 '                case 17:frame.size=88;PutLe(b+64,notice.nonce,8);std::copy(notice.target.begin(),notice.target.end(),b+72);break;\n'
                 '                case 16:frame.size=77;PutLe(b+64,notice.mode_generation,4);b[68]=1;PutLe(b+69,notice.nonce,8);break;\n'
                 '                case 15:frame.size=71;')
    vendor_h, vendor_cpp = root / 'endpoint_vendor.h', root / 'endpoint_vendor.cpp'
    replace_once(vendor_h, 'const Id& boot,std::uint64_t now);',
                 'const Id& boot,std::uint64_t now, unsigned profile = 3, const std::atomic<bool>* local_cutoff = nullptr,\n'
                 '                    const std::atomic<bool>* local_pause = nullptr, std::uint32_t capabilities = 0);')
    replace_once(vendor_h, '    Id device_{},boot_{};',
                 '    Id device_{},boot_{};\n    unsigned profile_ = 3;\n    const std::atomic<bool>* local_cutoff_ = nullptr;\n'
                 '    const std::atomic<bool>* local_pause_ = nullptr; std::uint32_t capabilities_ = 0;')
    replace_once(vendor_cpp, 'const Id& boot,std::uint64_t now)\n',
                 'const Id& boot,std::uint64_t now,unsigned profile,const std::atomic<bool>* local_cutoff,\n'
                 '                                 const std::atomic<bool>* local_pause,std::uint32_t capabilities)\n')
    replace_once(vendor_cpp, 'boot_(boot),handshake_deadline_',
                 'boot_(boot),profile_(profile),local_cutoff_(local_cutoff),local_pause_(local_pause),capabilities_(capabilities),handshake_deadline_')
    replace_once(vendor_cpp, 'SessionConfig config;config.device=device_;config.boot=boot_;',
                 'SessionConfig config;config.device=device_;config.boot=boot_;config.profile=profile_;config.local_cutoff=local_cutoff_;\n'
                 '        config.local_pause=local_pause_;config.manager_capabilities=capabilities_;')
    from manager_hold import apply as apply_hold
    apply_hold(root)
    from manager_volume import apply as apply_volume
    apply_volume(root)

    from upgrade_core import apply as upgrade_core
    from upgrade_write_guard import apply as upgrade_write_guard
    upgrade_core(root)
    upgrade_write_guard(root)

"""ICM4 local-wake command hold, applied after the base manager overlay."""
from controller_corrections import replace_once

def apply(root):
    frame=root/'endpoint_frame.h';core=root/'endpoint_controller.h';session=root/'endpoint_session.h'
    replace_once(frame,'case 17: return size == 88;',
                 'case 17: return size == 88;\n        case 19: return size == 85;\n        case 20: return size == 1118;')
    replace_once(frame,'b[68] <= 3 &&','b[68] <= 5 &&')
    replace_once(frame,'            if (state > 6 ||','            if (state > 7 ||')
    replace_once(frame,'        case 17: return IsZero',
                 '        case 19: return U64(b+68) != 0 && U64(b+76) != 0 && b[84] == 1;\n'
                 '        case 20: return U64(b+68) != 0 && U16(b+92) == 512;\n'
                 '        case 17: return IsZero')
    replace_once(core,'Full, Speaker, Upgrade, Cleanup };','Full, Speaker, Upgrade, Cleanup, Held };')
    replace_once(core,'EndpointState State() const { return state_; }',
                 'EndpointState State() const { return command_hold_ ? EndpointState::Held : state_; }')
    replace_once(core,'return !closed_ && state_ == EndpointState::Full',
                 'return !closed_ && !command_hold_ && state_ == EndpointState::Full')
    replace_once(core,'return manual_ && owned_ && !Active();',
                 'return manual_ && owned_ && !command_hold_ && !Active();')
    replace_once(core,'if (accepted_ && notice_complete_ && !acceptance_emitted_)',
                 'if (accepted_ && notice_complete_ && !acceptance_emitted_ && !command_hold_)')
    replace_once(core,'return !closed_ && Active() && rx_seen_',
                 'return !closed_ && !command_hold_ && Active() && rx_seen_')
    replace_once(core,'else if (Active() && ((now-last_rx_',
                 'else if (!command_hold_ && Active() && ((now-last_rx_')
    replace_once(core,'        if (now-last_pong_ >= ClockUs)',
                 '        if (command_hold_) {\n'
                 '            if (muted_ || now-command_progress_ >= 2000000) End(9);\n'
                 '            else if (now >= command_deadline_) {\n'
                 '                if (command_resuming_) End(9); else ResumeCommand(now);\n'
                 '            }\n'
                 '        }\n'
                 '        if (now-last_pong_ >= ClockUs)')
    replace_once(core,'            if (b[68] == 2) { End(13); return true; }',
                 '            if (b[68] == 4) return Protocol(); // a remote peer cannot open command capture\n'
                 '            if (b[68] == 5) {\n'
                 '                if (!command_hold_ || command_resuming_ || U64(b+69) != command_token_) return Protocol();\n'
                 '                return ResumeCommand(now);\n'
                 '            }\n'
                 '            if (b[68] == 2) { End(13); return true; }')
    replace_once(core,'            if (!manual_ || !accepted_ || !notice_complete_ ||',
                 '            if (command_hold_ || !manual_ || !accepted_ || !notice_complete_ ||')
    replace_once(core,'            if (!Active() || U32(b+68)',
                 '            if (command_hold_) {\n'
                 '                if (U32(b+64) > mode_generation_ || U32(b+68) != media_rx_ || media_rx_ == UINT32_MAX) return Protocol();\n'
                 '                ++next_rx_; ++media_rx_; return true; // expected conversational stream is suspended\n'
                 '            }\n'
                 '            if (!Active() || U32(b+68)')
    replace_once(core,'    void LocalStop(std::uint64_t now) {',
                 '#include "endpoint_manager_controls.h"\n    void LocalStop(std::uint64_t now) {')
    replace_once(core,'out = {}; return Tick(now) && Active() && rx_.Pop',
                 'out = {}; return Tick(now) && !command_hold_ && Active() && rx_.Pop')
    replace_once(core,'notice.health = Health(); notice.report_mono_us',
                 'notice.state = static_cast<std::uint8_t>(State());\n        notice.health = Health(); notice.report_mono_us')
    replace_once(core,'    void End(std::uint16_t reason) {\n',
                 '    void End(std::uint16_t reason) {\n'
                 '        command_hold_=command_ready_=command_resuming_=false;command_tx_.Clear();\n')
    replace_once(session,'                case 17:frame.size=88;',
                 '                case 19:frame.size=85;PutLe(b+64,notice.mode_generation,4);PutLe(b+68,notice.nonce,8);PutLe(b+76,notice.t1,8);b[84]=1;break;\n'
                 '                case 17:frame.size=88;')
    replace_once(session,'b[68]=1;PutLe(b+69,notice.nonce,8);break;',
                 'b[68]=static_cast<std::uint8_t>(notice.code);PutLe(b+69,notice.nonce,8);break;')
    replace_once(session,'            PcmQueue<3>::Batch batch;\n            if(core_->TakeTx(now,batch)) {',
                 '            PcmQueue<3>::Batch batch;\n'
                 '            const bool command=core_->TakeCommand(now,batch);\n'
                 '            if(command || core_->TakeTx(now,batch)) {')
    replace_once(session,'Frame audio=Envelope(1,core_->Call(),core_->Owner());audio.size=1110;',
                 'Frame audio=Envelope(command ? 20 : 1,core_->Call(),core_->Owner());audio.size=command ? 1118 : 1110;')
    replace_once(session,'                std::copy(batch.pcm.begin(),batch.pcm.end(),b+86);',
                 '                if(command) {\n'
                 '                    if(command_generation_ != batch.mode) { command_generation_=batch.mode;command_media_=0; }\n'
                 '                    if(command_media_ == UINT32_MAX) { Close();return nullptr; }\n'
                 '                    PutLe(b+68,core_->CommandToken(),8);PutLe(b+76,command_media_,4);PutLe(b+80,now,8);\n'
                 '                    PutLe(b+88,2000000-std::min<std::uint64_t>(2000000,batch.capture_deadline-now),4);PutLe(b+92,512,2);\n'
                 '                }\n'
                 '                std::copy(batch.pcm.begin(),batch.pcm.end(),b+(command ? 94 : 86));')
    replace_once(session,'SetOutput(audio,0,true,true);pcm_expiry_',
                 'SetOutput(audio,0,true,true);command_pcm_=command;pcm_expiry_')
    replace_once(session,'core_->MicAllowed()&&core_->ModeGeneration()==pcm_mode_',
                 '(command_pcm_ ? core_->CommandReady() : (core_->MicAllowed() && '
                 '(!config_.local_pause || !config_.local_pause->load(std::memory_order_acquire))))&&core_->ModeGeneration()==pcm_mode_')
    replace_once(session,'bool closed_=false,pcm_=false,',
                 'bool command_pcm_=false;\n    bool closed_=false,pcm_=false,')
    replace_once(session,'        if(pcm_)++tx_media_;',
                 '        if(pcm_) { if(command_pcm_)++command_media_;else ++tx_media_; }')
    replace_once(session,'    std::uint32_t tx_media_=0,pcm_mode_=0;',
                 '    std::uint32_t tx_media_=0,pcm_mode_=0,command_media_=0,command_generation_=0;')

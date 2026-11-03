from controller_corrections import replace_once

def apply(root):
    frame=root/'endpoint_frame.h';core=root/'endpoint_controller.h';session=root/'endpoint_session.h'
    replace_once(frame,'case 17: return size == 88;',
                 'case 17: return size == 88;\n        case 18: return size == 80;')
    replace_once(frame,'        case 19: return U64',
                 '        case 18: return b[68] >= 5 && b[68] <= b[69] && b[69] <= 100 && '
                 'b[70] >= 5 && b[70] <= 100 && U64(b+71) > 0 && U64(b+71) <= 9007199254740991ULL && b[79] <= 3;\n'
                 '        case 19: return U64')
    replace_once(core,'#include "endpoint_frame.h"','#include "endpoint_frame.h"\n#include "manager_volume.h"')
    replace_once(core,'Id target{}; };','Id target{}; StreamVolume volume{}; };')
    replace_once(core,'        last_now_ = now;\n',
                 '        last_now_ = now;\n'
                 '        if (prepared_owner_ && now >= prepared_deadline_) {\n'
                 '            highest_owner_=std::max(highest_owner_,prepared_owner_);\n'
                 '            prepared_owner_=prepared_deadline_=0;prepared_volume_={};\n'
                 '        }\n')
    replace_once(core,'                EmitFor(f,9,dnd_?2:maintenance_?12:muted_?3:5); return true;',
                 '                highest_owner_=std::max(highest_owner_,prepared_owner_);prepared_owner_=prepared_deadline_=0;prepared_volume_={};\n'
                 '                EmitFor(f,9,dnd_?2:maintenance_?12:muted_?3:5); return true;')
    replace_once(core,'        if (f.type == 2) {',
                 '        if (f.type == 18 && state_ == EndpointState::Idle) {\n'
                 '            if (profile_ != 4 || U32(b+64) != 0 || b[79] != 0 || IsZero(b+40,16) || '
                 'U64(b+56) <= highest_owner_ || !clock_.Fresh(now)) return Protocol();\n'
                 '            ++next_rx_;\n'
                 '            if (prepared_owner_ && (U64(b+56) != prepared_owner_ || '
                 'std::memcmp(b+40,prepared_call_.data(),16) || prepared_volume_.revision != U64(b+71) || '
                 'prepared_volume_.level != b[68] || prepared_volume_.maximum != b[69] || prepared_volume_.chime != b[70])) return Protocol();\n'
                 '            std::copy(b+40,b+56,prepared_call_.begin()); prepared_owner_=U64(b+56);\n'
                 '            prepared_volume_={b[68],b[69],b[70],U64(b+71)};\n'
                 '            if (!prepared_deadline_) prepared_deadline_=Deadline(now,5000000);\n'
                 '            EmitFor(f,18,3);if(notices_size_)notices_[notices_size_-1].volume=prepared_volume_;return !closed_;\n'
                 '        }\n'
                 '        if (f.type == 2) {')
    replace_once(core,'            std::copy(b+40,b+56,call_.begin());',
                 '            if (prepared_owner_ && (prepared_owner_ != U64(b+56) || '
                 'std::memcmp(b+40,prepared_call_.data(),16) || now >= prepared_deadline_)) {\n'
                 '                highest_owner_=std::max(highest_owner_,prepared_owner_);prepared_owner_=prepared_deadline_=0;\n'
                 '                EmitFor(f,9,11);return true;\n'
                 '            }\n'
                 '            std::copy(b+40,b+56,call_.begin());')
    replace_once(core,'        if (f.type == 16) {',
                 '        if (f.type == 18) {\n'
                 '            if (profile_ != 4 || U32(b+64) != mode_generation_ || b[79] != 0) return Protocol();\n'
                 '            ++next_rx_; const StreamVolume request{b[68],b[69],b[70],U64(b+71)};\n'
                 '            if (request.revision < volume_request_.revision) return Protocol();\n'
                 '            if (request.revision == volume_request_.revision) {\n'
                 '                if (request.level != volume_request_.level || request.maximum != volume_request_.maximum || '
                 'request.chime != volume_request_.chime) return Protocol();\n'
                 '                if (!VolumePending()) EmitVolume(1);\n                return !closed_;\n'
                 '            }\n'
                 '            auto applied=request;\n'
                 '            if (volume_request_.revision && request.maximum != volume_request_.maximum)\n'
                 '                applied.level=std::min(volume_desired_.level,request.maximum);\n'
                 '            volume_request_=request;volume_desired_=applied;return true;\n'
                 '        }\n'
                 '        if (f.type == 16) {')
    replace_once(core,'#include "endpoint_manager_controls.h"',
                 '    bool VolumePending() const { return owned_ && volume_desired_.revision != volume_applied_revision_; }\n'
                 '    const StreamVolume& DesiredVolume() const { return volume_desired_; }\n'
                 '    bool VolumeApplied(const Id& call,std::uint64_t owner,std::uint32_t mode,std::uint64_t revision,bool ok,std::uint64_t now) {\n'
                 '        if (!Tick(now) || !owned_ || call != call_ || owner != owner_ || mode != mode_generation_ || '
                 'revision != volume_desired_.revision) return false;\n'
                 '        if (!ok) { EmitVolume(2); End(5); return false; }\n'
                 '        volume_applied_revision_=revision;EmitVolume(1);return !closed_;\n'
                 '    }\n'
                 'private:\n'
                 '    void EmitVolume(std::uint16_t result) { Emit(18,result);if(notices_size_)notices_[notices_size_-1].volume=volume_desired_; }\n'
                 '    StreamVolume volume_request_{},volume_desired_{},prepared_volume_{};std::uint64_t volume_applied_revision_=0;\n'
                 '    Id prepared_call_{};std::uint64_t prepared_owner_=0,prepared_deadline_=0;\n'
                 'public:\n#include "endpoint_manager_controls.h"')
    replace_once(core,'manual_ = b[65] == 2;',
                 'volume_request_={};volume_desired_={};volume_applied_revision_=0;\n            manual_ = b[65] == 2;')
    replace_once(core,'volume_request_={};volume_desired_={};volume_applied_revision_=0;',
                 'volume_request_={};volume_desired_={};volume_applied_revision_=0;\n'
                 '            if(prepared_owner_) { volume_request_=volume_desired_=prepared_volume_;prepared_owner_=prepared_deadline_=0; }')
    replace_once(session,'                case 19:frame.size=85;',
                 '                case 18:frame.size=80;PutLe(b+64,notice.mode_generation,4);b[68]=notice.volume.level;'
                 'b[69]=notice.volume.maximum;b[70]=notice.volume.chime;PutLe(b+71,notice.volume.revision,8);'
                 'b[79]=static_cast<std::uint8_t>(notice.code);break;\n'
                 '                case 19:frame.size=85;')

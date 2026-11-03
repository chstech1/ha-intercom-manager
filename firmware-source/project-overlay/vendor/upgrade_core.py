"""Offline candidate transform: track each accepted upgrade attempt locally.

Apply only after the pinned controller and manager transforms, to a disposable
source tree. This is not a complete firmware fix or a wire protocol change.
"""
from pathlib import Path


def apply(directory):
    controller = Path(directory) / 'endpoint_controller.h'
    text = controller.read_text(encoding='utf-8')
    def replace_once(old, new):
        nonlocal text
        if text.count(old) != 1:
            raise ValueError('unexpected controller source: ' + old)
        text = text.replace(old, new, 1)
    replace_once('    std::uint32_t ModeGeneration() const { return mode_generation_; }',
                 '    std::uint32_t ModeGeneration() const { return mode_generation_; }\n'
                 '    std::uint64_t UpgradeAttempt() const { return upgrade_attempt_; }')
    replace_once('            state_ = EndpointState::Upgrade; setup_deadline_ = Deadline(now,10000000); return true;',
                 '            if (upgrade_attempt_ == UINT64_MAX) { Close(7); return false; }\n'
                 '            ++upgrade_attempt_;\n'
                 '            state_ = EndpointState::Upgrade; setup_deadline_ = Deadline(now,10000000); return true;')
    replace_once('    std::uint64_t notice_serial_ = 0, last_status_report_ = 0;',
                 '    std::uint64_t notice_serial_ = 0, last_status_report_ = 0, upgrade_attempt_ = 0;')
    controller.write_text(text, encoding='utf-8')


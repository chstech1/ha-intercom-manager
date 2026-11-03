// Included inside EndpointController in the disposable overlay only.
public:
    bool CommandHolding() const { return command_hold_; }
    bool CommandReady() const { return command_hold_ && command_ready_ && !command_resuming_; }
    std::uint64_t CommandToken() const { return command_token_; }
    bool LocalWake(std::uint64_t now) {
        if (!Tick(now) || profile_ != 4 || muted_ || dnd_ || maintenance_ || !clock_.Fresh(now)) return false;
        if (command_hold_ || (owned_ && state_ != EndpointState::Full && state_ != EndpointState::Speaker &&
            !(state_ == EndpointState::Ringing && notice_complete_))) return false;
        if (!owned_ && state_ != EndpointState::Idle) return false;
        if (command_counter_ == UINT64_MAX) return Protocol();
        command_token_ = ++command_counter_; command_started_ = now;
        if (!owned_) { EmitCommand(19,0); return !closed_; }
        if (!BumpMode()) return false;
        command_hold_ = true; command_ready_ = command_resuming_ = false;
        command_deadline_ = Deadline(now,15000000); command_progress_ = now;
        command_tx_.Clear(); return true;
    }
    // Real adapter calls only after conversational capture + output are flushed.
    bool CommandFlushed(std::uint64_t now) {
        if (!Tick(now) || !owned_ || !command_hold_) return false;
        if (command_resuming_) {
            command_hold_ = command_ready_ = command_resuming_ = false;
            last_rx_ = last_tx_ = last_output_ = now;
            rx_seen_ = tx_seen_ = output_seen_ = false;
            EmitCommand(16,5); EmitAcceptance(); return !closed_;
        }
        if (!command_ready_) {
            command_ready_ = true; EmitCommand(19,0); EmitCommand(16,4);
        }
        return !closed_;
    }
    bool CaptureCommand(const std::array<std::uint8_t,1024>& pcm, std::uint32_t generation,
                        std::uint64_t age, std::uint64_t now) {
        if (!Tick(now) || !CommandReady() || muted_ || generation != mode_generation_ || age >= 2000000) return false;
        PcmQueue<3>::Batch batch; batch.pcm=pcm; batch.mode=generation;
        batch.expiry=Deadline(now,std::min<std::uint64_t>(96000,2000000-age));
        batch.capture_deadline=Deadline(now,2000000-age);
        command_tx_.Push(batch); command_progress_=now; return true;
    }
    bool TakeCommand(std::uint64_t now, PcmQueue<3>::Batch& out) {
        out={};return Tick(now) && CommandReady() && notices_size_ == 0 && command_tx_.Pop(now,mode_generation_,out);
    }
private:
    void EmitCommand(std::uint8_t type, std::uint16_t op) {
        Emit(type,op);
        if (notices_size_) { auto& n=notices_[notices_size_-1];n.nonce=command_token_;n.t1=command_started_; }
    }
    bool ResumeCommand(std::uint64_t now) {
        if (!command_hold_) return false;
        command_tx_.Clear(); if (!BumpMode()) return false;
        command_ready_=false; command_resuming_=true;
        command_deadline_=Deadline(now,2000000); // actual resume flush must finish promptly
        return true;
    }
    bool command_hold_=false, command_ready_=false, command_resuming_=false;
    std::uint64_t command_counter_=0,command_token_=0,command_started_=0;
    std::uint64_t command_deadline_=0,command_progress_=0;
    PcmQueue<3> command_tx_;
public:

class IntercomM3Processor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.capture = [];
    this.capturePhase = 0;
    this.fir = this.makeFir(63, Math.min(0.45, 7200 / sampleRate));
    this.history = new Float32Array(this.fir.length);
    this.historyAt = 0;
    this.captureOutstanding = 0;
    this.playback = [];
    this.playbackPhase = 0;
    this.playbackLast = 0;
    this.playbackFiltered = 0;
    this.playbackDrops = 0;
    this.playbackQueueDrops = 0;
    this.playbackDeadlineDrops = 0;
    this.playbackQueueResidenceMaxUs = 0;
    this.played = 0;
    this.outputEndTime = null;
    this.captureEnabled = true;
    this.port.onmessage = ({ data }) => {
      if (data?.type === "play" && data.samples instanceof Int16Array) {
        let queueDropped=false;
        if (this.playback.length === 3) { this.playback.shift(); this.playbackDrops++; this.playbackQueueDrops++; queueDropped=true; }
        this.playback.push({ samples: data.samples, offset: 0, deadline: data.deadline, queuedAt: currentTime, started: false });
        this.port.postMessage({ type: "playback-accepted" });
        if(queueDropped)this.port.postMessage({type:"playback-stats",queueDrops:this.playbackQueueDrops,deadlineDrops:this.playbackDeadlineDrops,queueResidenceMaxUs:this.playbackQueueResidenceMaxUs});
      } else if (data?.type === "capture-ack") {
        this.captureOutstanding = Math.max(0, this.captureOutstanding - 1);
      } else if (data?.type === "capture-enabled") {
        this.captureEnabled = Boolean(data.enabled);
        if (!this.captureEnabled) this.capture.length = 0;
      } else if (data?.type === "clear") {
        this.capture.length = 0;
        this.playback.length = 0;
      }
    };
  }

  makeFir(length, cutoff) {
    const taps = new Float32Array(length), middle = (length - 1) / 2;
    let sum = 0;
    for (let i = 0; i < length; i++) {
      const x = i - middle;
      const sinc = x === 0 ? 2 * cutoff : Math.sin(2 * Math.PI * cutoff * x) / (Math.PI * x);
      const window = 0.42 - 0.5 * Math.cos(2 * Math.PI * i / (length - 1)) + 0.08 * Math.cos(4 * Math.PI * i / (length - 1));
      taps[i] = sinc * window; sum += taps[i];
    }
    for (let i = 0; i < length; i++) taps[i] /= sum;
    return taps;
  }

  filter(value) {
    this.history[this.historyAt] = value;
    let result = 0, at = this.historyAt;
    for (let i = 0; i < this.fir.length; i++) { result += this.fir[i] * this.history[at]; at = at ? at - 1 : this.fir.length - 1; }
    this.historyAt = (this.historyAt + 1) % this.fir.length;
    return result;
  }

  process(inputs, outputs) {
    const input = inputs[0]?.[0];
    const output = outputs[0]?.[0];
    if (input && this.captureEnabled) {
      const ratio = sampleRate / 16000;
      for (let i = 0; i < input.length; i++) {
        const filtered = this.filter(input[i]);
        this.capturePhase += 1;
        if (this.capturePhase >= ratio) {
          this.capturePhase -= ratio;
          this.capture.push(Math.max(-32768, Math.min(32767, Math.round(filtered * 32767))));
          if (this.capture.length === 512) {
            const samples = Int16Array.from(this.capture);
            this.capture.length = 0;
            if (this.captureOutstanding < 3) {
              this.captureOutstanding++;
              this.port.postMessage({ type: "capture", samples, captureEndTime: (currentFrame + input.length) / sampleRate, outputEndTime: this.outputEndTime }, [samples.buffer]);
            }
          }
        }
      }
    }
    if (output) {
      for (let i = 0; i < output.length; i++) {
        const renderTime = currentFrame / sampleRate + i / sampleRate;
        let expired = false;
        while (this.playback[0] && renderTime >= this.playback[0].deadline) {
          const frame=this.playback.shift();
          this.playbackQueueResidenceMaxUs=Math.max(this.playbackQueueResidenceMaxUs,Math.max(0,Math.round((renderTime-frame.queuedAt)*1_000_000)));
          this.playbackDrops++;this.playbackDeadlineDrops++;expired=true;this.playbackLast=0;this.playbackFiltered=0;
        }
        if(expired)this.port.postMessage({type:"playback-stats",queueDrops:this.playbackQueueDrops,deadlineDrops:this.playbackDeadlineDrops,queueResidenceMaxUs:this.playbackQueueResidenceMaxUs});
        this.playbackPhase += 16000;
        if (this.playbackPhase >= sampleRate) {
          this.playbackPhase -= sampleRate;
          const frame = this.playback[0];
          if (frame) {
            if(!frame.started){frame.started=true;this.playbackQueueResidenceMaxUs=Math.max(this.playbackQueueResidenceMaxUs,Math.max(0,Math.round((renderTime-frame.queuedAt)*1_000_000)));}
            this.playbackLast = frame.samples[frame.offset++] / 32768;
            if (frame.offset === frame.samples.length) this.playback.shift();
            this.played++;
          } else {
            this.playbackLast = 0;
          }
        }
        this.playbackFiltered += Math.min(1, 16000 / sampleRate) * (this.playbackLast - this.playbackFiltered);
        output[i] = this.playbackFiltered * 0.25;
      }
      if (this.played >= 512) {
        this.played %= 512;
        this.port.postMessage({ type: "played", queuedFrames: this.playback.length, droppedFrames: this.playbackDrops, queueDrops:this.playbackQueueDrops, deadlineDrops:this.playbackDeadlineDrops, queueResidenceMaxUs:this.playbackQueueResidenceMaxUs });
      }
      // Piggyback local silent-output progress on bounded capture messages.
      // This is preflight health, never evidence of received room PCM.
      this.outputEndTime = (currentFrame + output.length) / sampleRate;
    }
    return true;
  }
}

registerProcessor("intercom-m3-poc", IntercomM3Processor);

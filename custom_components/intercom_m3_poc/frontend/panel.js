const TYPES = { ATTACH: 1, ATTACH_OK: 2, AUDIO: 3, READY: 4, PING: 5, PONG: 6, TERMINATE: 7, ERROR: 8 };
const LENGTHS = new Map([[1, 72], [2, 26], [3, 1042], [4, 8], [5, 16], [6, 32], [7, 2], [8, 2]]);
const FRAME_BYTES = 1046;
const MAX_BUFFERED = FRAME_BYTES * 3;
const text = new TextEncoder();

function b64url(value, bytes) {
  if (typeof value !== "string" || value.length !== Math.ceil(bytes * 4 / 3) || !/^[A-Za-z0-9_-]+$/.test(value)) throw new Error("INVALID_IDENTITY");
  const raw = atob(value.replace(/-/g, "+").replace(/_/g, "/") + "===".slice((value.length + 3) % 4));
  if (raw.length !== bytes) throw new Error("INVALID_IDENTITY");
  const decoded = Uint8Array.from(raw, c => c.charCodeAt(0));
  const canonical = btoa(String.fromCharCode(...decoded)).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  if (canonical !== value) throw new Error("INVALID_IDENTITY");
  return decoded;
}

function randomId() {
  const raw = crypto.getRandomValues(new Uint8Array(16));
  return btoa(String.fromCharCode(...raw)).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

function generation(value) {
  if (typeof value !== "string" || !/^[0-9a-f]{16}$/.test(value)) throw new Error("INVALID_GENERATION");
  return BigInt(`0x${value}`);
}

function frame(type, payload) {
  if (payload.byteLength !== LENGTHS.get(type)) throw new Error("INVALID_FRAME");
  const out = new Uint8Array(payload.byteLength + 4);
  const view = new DataView(out.buffer);
  view.setUint8(0, type); view.setUint8(1, 1); view.setUint16(2, payload.byteLength, true);
  out.set(new Uint8Array(payload.buffer, payload.byteOffset, payload.byteLength), 4);
  return out;
}

function parseFrame(data) {
  if (!(data instanceof ArrayBuffer) || data.byteLength < 4) throw new Error("INVALID_FRAME");
  const view = new DataView(data);
  const type = view.getUint8(0), length = view.getUint16(2, true);
  if (view.getUint8(1) !== 1 || LENGTHS.get(type) !== length || data.byteLength !== length + 4) throw new Error("INVALID_FRAME");
  return { type, view: new DataView(data, 4), bytes: new Uint8Array(data, 4) };
}

function writeU64(view, offset, value) { view.setBigUint64(offset, BigInt(value), true); }
function nowUs() { return BigInt(Math.floor(performance.now() * 1000)); }
function canQueueAudio(bufferedAmount) { return bufferedAmount + FRAME_BYTES <= MAX_BUFFERED; }
function mediaCloseReason(reason) { return typeof reason==="string"&&/^[A-Z][A-Z0-9_]{0,63}$/.test(reason) ? `Call ended: ${reason}` : "Media connection closed."; }
function safeServerMetrics(value) {
  const source=value&&typeof value==="object"?value:{};
  const result={};
  for(const key of ["uplink_frames","downlink_frames","stale_drops","queue_drops","endpoint_sink_frames","endpoint_capture_frames","endpoint_audio_prior_age_max_ms","endpoint_audio_transit_age_max_ms","endpoint_clock_uncertainty_ms","endpoint_audio_stale_drops","browser_uplink_age_max_ms","endpoint_tx_queue_hold_max_ms","browser_downlink_queue_hold_max_ms","browser_downlink_age_max_ms","browser_clock_rejected_samples","browser_clock_rejected_uncertainty_max_ms","endpoint_clock_rejected_samples","endpoint_clock_rejected_uncertainty_max_ms"]){if(Number.isSafeInteger(source[key])&&source[key]>=0)result[key]=source[key];}
  if(typeof source.simulation==="boolean")result.simulation=source.simulation;
  if(typeof source.room_mic_muted==="boolean")result.room_mic_muted=source.room_mic_muted;
  return result;
}
function bounded(promise, ms, reason) { let timer; return Promise.race([promise, new Promise((_, reject) => { timer = setTimeout(() => reject(new Error(reason)), ms); })]).finally(() => clearTimeout(timer)); }

class ClockRelation {
  constructor(clock = () => performance.now()) { this.now = clock; this.pending = new Map(); this.updated = 0; this.offset = 0; this.uncertainty = Infinity; this.lastT1 = 0n; this.lastT2 = 0n; this.lastPeerSend = -Infinity; this.rejectedSamples=0; this.rejectedUncertaintyMax=0; }
  ping() {
    const payload = new ArrayBuffer(16), view = new DataView(payload), random = crypto.getRandomValues(new Uint8Array(8)), nonce = new DataView(random.buffer).getBigUint64(0, true), t1 = BigInt(Math.floor(this.now() * 1000));
    writeU64(view, 0, nonce); writeU64(view, 8, t1); this.pending.set(nonce.toString(), t1); while (this.pending.size > 3) this.pending.delete(this.pending.keys().next().value); return frame(TYPES.PING, new Uint8Array(payload));
  }
  pong(view) {
    const nonce = view.getBigUint64(0, true), t1 = view.getBigUint64(8, true), t2 = view.getBigUint64(16, true), t3 = view.getBigUint64(24, true), t4 = BigInt(Math.floor(this.now() * 1000));
    if (this.pending.get(nonce.toString()) !== t1 || t3 < t2 || t4 < t1 || t1 < this.lastT1 || t2 < this.lastT2) throw new Error("CLOCK_INVALID");
    this.pending.delete(nonce.toString());
    const rtt = Number((t4 - t1) - (t3 - t2));
    if (rtt < 0) throw new Error("CLOCK_INVALID");
    const candidateOffset = (Number(t2 - t1) + Number(t3 - t4)) / 2;
    const resolution = 1000; // Conservative 1 ms browser timer-resolution bound.
    const candidateUncertainty = Math.max(rtt / 2, resolution) + Math.ceil((rtt + 3_000_000) * 0.0002) + resolution;
    if(candidateUncertainty>500000){this.rejectedSamples++;this.rejectedUncertaintyMax=Math.max(this.rejectedUncertaintyMax,candidateUncertainty);return false;}
    this.offset = candidateOffset; this.uncertainty = candidateUncertainty;
    this.lastT1 = t1; this.lastT2 = t2; this.updated = this.now();
    return true;
  }
  fresh() { return this.updated > 0 && this.now() - this.updated <= 3000; }
  ageUpper(sent, prior, jitterUs) {
    if (!this.fresh()) throw new Error("CLOCK_STALE");
    const peerSend=Number(sent), mapped = peerSend - this.offset, localNow = this.now() * 1000, transit = localNow - mapped;
    if (peerSend < this.lastPeerSend || transit < -this.uncertainty || mapped < 0) throw new Error("CLOCK_INVALID");
    this.lastPeerSend=peerSend;
    return Number(prior) + Math.max(0, transit) + this.uncertainty + jitterUs;
  }
}

class IntercomM3PocPanel extends HTMLElement {
  constructor() {
    super(); this.attachShadow({ mode: "open" }); this.browserId = randomId(); this.panelConfig={simulation:true,backend:"simulation"}; this.status = "Ready"; this.softMuted = false; this.callMode="full_duplex"; this.metrics = this.emptyMetrics(); this.rooms=[]; this.selectedDeviceId=""; this.roomLoad="unknown"; this.attempt = 0; this.inflight = false; this.cleaning = false; this.render();
    this.onPageHide = () => this.cleanup("PAGE_HIDDEN", true);
    this.onVisibility = () => { if (document.visibilityState !== "visible") this.cleanup("PAGE_HIDDEN", true); };
  }
  emptyMetrics() { return { pocVersion: this.panelConfig?.version || "0.4.2", callerInput:"unknown", callMode:this.callMode||"full_duplex", admissionAvailable:null, admissionBusy:null, simulation: this.panelConfig?.simulation!==false, endpointBackend:this.panelConfig?.backend||"simulation", path: "unknown", audioAgeLimitUs: 2000000, captureFrames: 0, capturePeak: 0, nonzeroCaptureFrames: 0, renderedFrames: 0, txDrops: 0, rxDrops: 0, staleDrops: 0, clockStaleDrops: 0, browserClockRejectedSamples: 0, browserClockRejectedUncertaintyMaxUs: 0, admissionExpiredDrops: 0, mainQueueDrops: 0, workletQueueDrops: 0, workletDeadlineDrops: 0, receivedMediaAgeMaxUs: 0, workletQueueResidenceMaxUs: 0, clockUncertaintyUs: 0, outputLeadUs: 0, baseLatencyUs: 0, outputLatencyUs: 0, clockFresh: false, backend: "IDLE", server: {}, physicalIoLatency: "unproven", acousticLatency: "unproven", outputRoute: "unknown" }; }
  setConfig(config) { this.panelConfig={...this.panelConfig,...config}; this.metrics=this.emptyMetrics(); if(config?.device_id)this.selectedDeviceId=String(config.device_id); this.render(); }
  set panel(value) { if (!this.callId && !this.inflight) this.setConfig(value?.config || {}); }
  set hass(value) { const previous=this._hass?.connection,next=value?.connection;if(previous!==next){this.roomRequest=Symbol();if(previous)this.rooms=[];this.roomLoad="unknown";if(previous&&this.roomControlLost)previous.removeEventListener?.("disconnected",this.roomControlLost);this.roomControlLost=()=>{this.roomRequest=Symbol();this.rooms=[];this.roomLoad="failed";this.render();};next?.addEventListener?.("disconnected",this.roomControlLost);}this._hass=value;this.render();if(previous!==next&&this.isConnected)this.refreshRooms(); }
  connectedCallback() { if(this.roomControlLost)this._hass?.connection?.addEventListener?.("disconnected",this.roomControlLost);addEventListener("pagehide", this.onPageHide); document.addEventListener("visibilitychange", this.onVisibility); this.render(); this.refreshRooms();if(!this.roomInterval)this.roomInterval=setInterval(()=>{if(document.visibilityState==="visible"&&!this.inflight&&!this.cleaning)this.refreshRooms();},5000); }
  disconnectedCallback() { this.roomRequest=Symbol();removeEventListener("pagehide", this.onPageHide); document.removeEventListener("visibilitychange", this.onVisibility);if(this.roomInterval)clearInterval(this.roomInterval);this.roomInterval=null;if(this._hass?.connection&&this.roomControlLost)this._hass.connection.removeEventListener?.("disconnected",this.roomControlLost);this.rooms=[];this.roomLoad="unknown";this.cleanup("PANEL_REMOVED", true); }
  roomEntry(value) { if(!value?.device_id)return null; const boolean=key=>typeof value[key]==="boolean"?value[key]:null; return {deviceId:String(value.device_id),name:String(value.room_name||value.name||this.panelConfig.room_name||"Intercom room").slice(0,80),available:boolean("available"),busy:boolean("busy"),muted:boolean("muted")}; }
  async refreshRooms() {
    if(this.panelConfig.announcement_only||!this._hass?.connection||this.inflight||this.cleaning)return;
    const request=Symbol();this.roomRequest=request;this.roomLoad="checking";this.render();
    try{const result=await bounded(this._hass.connection.sendMessagePromise({type:"intercom_native/list_devices"}),5000,"CONTROL_TIMEOUT");if(this.roomRequest!==request||this.inflight)return;const values=Array.isArray(result)?result:result?.devices;this.rooms=(Array.isArray(values)?values:[]).map(value=>this.roomEntry(value)).filter(Boolean);const preferred=this.selectedDeviceId||String(this.panelConfig.device_id||"");if(!this.rooms.some(room=>room.deviceId===preferred))this.selectedDeviceId=this.panelConfig.device_id?preferred:this.rooms[0]?.deviceId||"";this.roomLoad="complete";}
    catch{if(this.roomRequest===request){this.rooms=[];this.roomLoad="failed";}}
    finally{if(this.roomRequest===request)this.render();}
  }
  render() {
    const previousDetails=this.shadowRoot.querySelector("#diagnostics"),detailsOpen=typeof previousDetails?.open==="boolean"?previousDetails.open:Boolean(this.detailsOpen),activeId=this.shadowRoot.activeElement?.id;this.detailsOpen=detailsOpen;
    const simulated=this.metrics?.simulation!==false, target=simulated?"simulated endpoint":"authenticated room endpoint";
    const room=this.rooms.find(value=>value.deviceId===this.selectedDeviceId), available=room?.available===true?"Online":room?.available===false?"Offline":"Unknown", ownCall=Boolean(room&&(this.callId||this.inflight)), busy=ownCall?"Busy — this call":room?.busy===true?"Busy":room?.busy===false?"Free":"Unknown";
    const roomMuted=ownCall&&typeof this.metrics.server?.room_mic_muted==="boolean"?this.metrics.server.room_mic_muted:room?.muted;
    const unavailable=this.callId || this.inflight || this.cleaning || this.permissionPending, cannotCall=unavailable||!room||room.available===false||room.busy===true;
    const options=this.rooms.map((value,index)=>`<option value="${index}" ${value.deviceId===this.selectedDeviceId?"selected":""}>${this.escape(value.name)}</option>`).join("");
    const roomMessage=this.panelConfig.announcement_only?"":!room&&this.roomLoad==="checking"?"Checking rooms…":this.roomLoad==="failed"?"Room status unavailable. Try again.":!room?"No enrolled room available.":"";
    this.shadowRoot.innerHTML = `<style>[hidden]{display:none!important}:host{display:block;padding:16px;max-width:760px;margin:auto;font:16px system-ui;color:var(--primary-text-color,#222)}article{border:1px solid var(--divider-color,#bbb);border-radius:14px;padding:20px;background:var(--card-background-color,#fff)}h1{font-size:1.5rem;margin-top:0}label{display:block;font-weight:700;margin:12px 0 6px}select,button{font:inherit;min-height:52px;border-radius:9px}select{box-sizing:border-box;width:100%;padding:0 12px}.state{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px;margin:16px 0}.state div{padding:10px;border-radius:8px;background:rgba(127,127,127,.1)}.state strong{display:block;font-size:.8rem}.actions{display:grid;grid-template-columns:repeat(3,1fr);gap:8px}.actions button{font-weight:700}#call{background:#1976d2;color:#fff}#mute{background:#f2b705;color:#111}#hang{background:#b00020;color:#fff}button:disabled{opacity:.5}#status{padding:12px;border-left:5px solid #1976d2;background:rgba(25,118,210,.08)}details{margin-top:18px}details button{margin:6px}small{display:block;margin:12px 0;line-height:1.45}pre{white-space:pre-wrap;font-size:.8rem;max-height:20rem;overflow:auto}@media(max-width:520px){.actions,.state{grid-template-columns:1fr}}</style><article><h1>Intercom Lab</h1><label for="room">Room</label><select id="room" ${unavailable?"disabled":""}>${options}</select>${roomMessage?`<p>${this.escape(roomMessage)}</p>`:""}<label for="mode">Call mode</label><select id="mode" ${unavailable?"disabled":""}><option value="full_duplex" ${this.callMode==="full_duplex"?"selected":""}>Talk and listen</option><option value="speaker_only" ${this.callMode==="speaker_only"?"selected":""}>Speak to room only</option></select><small>Speak to room only sends phone audio to the room and does not transmit the room microphone.</small><div class="state" aria-label="Room and call status"><div><strong>Available</strong>${available}</div><div><strong>Busy</strong>${busy}</div><div><strong>Room microphone</strong>${roomMuted===true?"Muted":roomMuted===false?"On":"Unknown"}</div><div><strong>Call</strong>${this.escape(this.metrics.backend==="IDLE"&&!this.inflight?"Idle":this.metrics.backend)}</div></div><p id="status" role="status" aria-live="polite">${this.escape(this.status)}</p><div class="actions"><button id="call" ${cannotCall?"disabled":""}>CALL</button><button id="mute" ${this.callId?"":"disabled"}>${this.softMuted?"UNMUTE MY MIC":"MUTE MY MIC"}</button><button id="hang" ${this.callId||this.inflight?"":"disabled"}>HANG UP</button></div><details id="diagnostics" ${detailsOpen?"open":""}><summary>Diagnostics and connection test</summary><p><strong>${simulated?"Simulation":"Real endpoint"}:</strong> ${simulated?"Synthetic endpoint; no physical hardware proof.":"Connected status requires endpoint answer, fresh clock, browser media, endpoint capture, and sink progress."}</p><button id="silent" ${cannotCall?"disabled":""}>TEST connection (silent uplink)</button><button id="speaker" ${!this.callId||!this.speakerOutput()||this.outputAudio?"disabled":""}>Use speaker (test)</button><button id="outputs">Check audio outputs</button><button id="copy">Copy metadata report</button><p><a href="/config/integrations/integration/intercom_m3_poc">Setup and removal</a></p><small>CALL requests microphone permission. Silent testing does not test the phone microphone or physical speech. Hiding the app, losing control, or any media failure ends the call and releases its audio input.</small><pre>${this.escape(JSON.stringify(this.metrics,null,2))}</pre></details></article>`;
    if(this.panelConfig.announcement_only){
      for(const selector of ["#room","label[for=room]","#mode","label[for=mode]","#call","#silent","#speaker"])this.shadowRoot.querySelector(selector).hidden=true;
      this.shadowRoot.querySelector("h1").textContent="Live announcement microphone";
      this.shadowRoot.querySelector("small").textContent="Your microphone sends only to accepted destinations. End or leave this screen to release it. Destination microphones stay closed.";
      this.shadowRoot.querySelector(".state").innerHTML=`<div><strong>Source</strong>Foreground browser microphone</div><div><strong>Delivery</strong>${this.escape(this.metrics.backend)}</div>`;
    }
    this.shadowRoot.querySelector("#room").onchange=event=>{const selected=this.rooms[Number(event.target.value)];if(selected)this.selectedDeviceId=selected.deviceId;this.render();};this.shadowRoot.querySelector("#mode").onchange=event=>{if(event.target.value==="full_duplex"||event.target.value==="speaker_only"){this.callMode=event.target.value;this.metrics.callMode=this.callMode;}this.render();};this.shadowRoot.querySelector("#call").onclick = () => this.call(); this.shadowRoot.querySelector("#silent").onclick = () => this.call(true); this.shadowRoot.querySelector("#hang").onclick = () => this.cleanup("USER_HANGUP", true); this.shadowRoot.querySelector("#mute").onclick = () => this.toggleMute(); this.shadowRoot.querySelector("#copy").onclick = () => this.copyReport(); this.shadowRoot.querySelector("#outputs").onclick = () => this.inspectOutputs(); this.shadowRoot.querySelector("#speaker").onclick = () => this.useSpeaker();const details=this.shadowRoot.querySelector("#diagnostics");details.open=detailsOpen;details.ontoggle=()=>{this.detailsOpen=details.open;};const focus=activeId&&this.shadowRoot.querySelector(`#${activeId}`);if(focus&&!focus.disabled)try{focus.focus({preventScroll:true});}catch{focus.focus();}
  }
  escape(value) { return String(value).replace(/[&<>]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;"}[c])); }
  fail(reason) { this.status = reason; this.render(); }
  assertAttempt(attempt) { if (attempt !== this.attempt || !this.inflight) throw new Error("CALL_ABORTED"); }
  async call(silent=false, announcement=null) {
    if (this.callId || this.inflight || this.cleaning || this.permissionPending) return;
    if (!isSecureContext || location.protocol !== "https:") return this.fail("Secure HTTPS ingress is required for audio testing.");
    if (document.visibilityState !== "visible") return this.fail("Bring this panel to the foreground before calling.");
    if (!this._hass?.connection || !this._hass?.auth?.data?.access_token) return this.fail("Home Assistant authenticated frontend access is unavailable.");
    if (!globalThis.AudioContext || !globalThis.AudioWorkletNode || (!silent&&!navigator.mediaDevices?.getUserMedia)) return this.fail("This browser cannot provide the required audio path.");
    const device=announcement?{available:null,busy:null}:this.rooms.find(value=>value.deviceId===this.selectedDeviceId);if(!device)return this.fail("Select an enrolled room.");if(device.available===false||device.busy===true)return this.fail("Room is offline or busy.");
    if(announcement)this.callMode="speaker_only";
    const attempt = ++this.attempt; this.inflight = true; this.roomRequest=Symbol();this.softMuted=false; this.metrics=this.emptyMetrics();this.metrics.callerInput=silent?"synthetic_silence":"microphone";this.metrics.admissionAvailable=device.available;this.metrics.admissionBusy=device.busy; this.mediaStarted=0; this.outputDevices=[]; this.lastCapture=0; this.lastPlayed=0; this.lastOutputProgress=0; this.mediaWasActive=false; this.status = silent?"Starting connection test with synthetic silence — microphone not tested":"Requesting microphone permission…"; this.render();
    this.controlLost = () => this.cleanup("Home Assistant control connection lost.", false);
    this._hass.connection.addEventListener?.("disconnected", this.controlLost);
    try {
      this.ctx = new AudioContext({ latencyHint: "interactive" });
      await bounded(this.ctx.resume(), 3000, "AUDIO_CONTEXT_TIMEOUT"); this.assertAttempt(attempt);
      if(!silent){
        const rawPermission = navigator.mediaDevices.getUserMedia({ audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true }, video: false });
        this.permissionPromise = rawPermission; this.permissionPending=true; rawPermission.then(stream => { if(attempt!==this.attempt||!this.inflight)stream.getTracks().forEach(track=>track.stop()); },()=>{}).finally(()=>{this.permissionPending=false;if(!this.inflight)this.render();});
        const stream = await bounded(rawPermission, 15000, "MICROPHONE_TIMEOUT");
        if (attempt !== this.attempt || !this.inflight) throw new Error("CALL_ABORTED");
        this.permissionPromise=null; this.permissionPending=false; this.stream = stream; this.stream.getTracks().forEach(track => track.addEventListener("ended", () => this.cleanup("Microphone stopped.", true), { once: true }));
      }
      if (document.visibilityState !== "visible") throw new Error("PAGE_HIDDEN");
      await bounded(this.ctx.audioWorklet.addModule("/intercom_m3_poc/worklet.js?v=0.4.2"), 5000, "WORKLET_LOAD_TIMEOUT"); this.assertAttempt(attempt);
      if (this.ctx.sampleRate < 16000) throw new Error("CAPTURE_RATE_UNSUPPORTED");
      this.node = new AudioWorkletNode(this.ctx, "intercom-m3-poc", { numberOfInputs: 1, numberOfOutputs: 1, outputChannelCount: [1] });
      this.node.addEventListener("processorerror", () => this.cleanup("Audio processing failed.", true), { once: true });
      this.ctx.addEventListener("statechange", () => { if (this.inflight && this.ctx?.state !== "running") this.cleanup("Audio context stopped.", true); });
      if(silent){if(!this.ctx.createConstantSource)throw new Error("SILENT_INPUT_UNSUPPORTED");this.silenceSource=this.ctx.createConstantSource();this.silenceSource.offset.value=0;this.source=this.silenceSource;this.source.connect(this.node);this.source.start();}
      else {this.source = this.ctx.createMediaStreamSource(this.stream);this.source.connect(this.node);}
      this.node.connect(this.ctx.destination); this.node.port.onmessage = event => this.onWorklet(event.data);
      const request=announcement?{...announcement,type:"intercom_native/start_announcement",source:{kind:"live",browser_instance_id:this.browserId}}:{type:"intercom_native/start",device_id:device.deviceId,browser_instance_id:this.browserId,mode:this.callMode};
      const rawStart=this._hass.connection.sendMessagePromise(request); this.pendingStart=rawStart; rawStart.then(started=>{if(started.call_id&&(attempt!==this.attempt||!this.inflight))this._hass?.connection?.sendMessagePromise({type:"intercom_native/stop",call_id:started.call_id,owner_generation:started.owner_generation}).catch(()=>{});},()=>{}).finally(()=>{if(this.pendingStart===rawStart)this.pendingStart=null;});
      const started = await bounded(rawStart, 5000, "CONTROL_TIMEOUT"); this.assertAttempt(attempt); this.pendingStart=null;
      if(announcement)this.dispatchEvent(new CustomEvent("announcement-result",{detail:started}));
      if(!started.call_id)throw new Error("NO_ACCEPTED_DESTINATIONS");
      this.callId = started.call_id; this.ownerGeneration = started.owner_generation; this.modeGeneration = started.mode_generation ?? 0;
      await bounded(this.subscribe(attempt), 5000, "SUBSCRIBE_TIMEOUT"); this.assertAttempt(attempt);
      await bounded(this.openMedia(attempt), 10000, "MEDIA_SETUP_TIMEOUT"); this.assertAttempt(attempt); await this.inspectOutputs();
    } catch (error) {
      const failures={MICROPHONE_TIMEOUT:"Microphone did not start. Check permission and try again.",ENDPOINT_UNAVAILABLE:"Room is offline or busy.",CONTROL_TIMEOUT:"Home Assistant did not respond.",MEDIA_SETUP_TIMEOUT:"Media connection did not start.",SUBSCRIBE_TIMEOUT:"Call status did not start."};
      const denied = error?.name === "NotAllowedError" ? "Microphone permission was denied. Allow it in iPhone/Home Assistant settings, then use CALL again." : failures[error?.message]||`Call unavailable: ${error?.message || "UNKNOWN_ERROR"}`;
      await this.cleanup(denied, true);
    }
  }
  async subscribe(attempt) {
    const unsubscribe = await this._hass.connection.subscribeMessage(event => {
      if (!event || event.call_id !== this.callId) return;
      this.metrics.backend = event.state || "UNKNOWN";
      this.metrics.server = safeServerMetrics(event.metrics);
      if (event.state === "TERMINATED") this.cleanup(`Call ended: ${event.reason || "TERMINATED"}`, false);
      else this.updateConnected();
    }, { type: "intercom_native/subscribe_call", call_id: this.callId, owner_generation: this.ownerGeneration });
    if(attempt!==this.attempt||!this.inflight){unsubscribe();throw new Error("CALL_ABORTED");} this.unsubscribe=unsubscribe;
  }
  async openMedia(attempt) {
    if(typeof this._hass?.fetchWithAuth!=="function")throw new Error("HA_AUTH_UNAVAILABLE");
    this.fetchController=new AbortController();
    const response = await bounded(this._hass.fetchWithAuth("/api/intercom_native/media-ticket", { method: "POST", cache: "no-store", signal:this.fetchController.signal, headers: { "Content-Type": "application/json" }, body: JSON.stringify({ call_id: this.callId, browser_instance_id: this.browserId }) }), 5000, "MEDIA_TICKET_TIMEOUT");
    this.assertAttempt(attempt);
    if (!response.ok) throw new Error(response.status === 403 ? "SECURE_INGRESS_REQUIRED" : "MEDIA_TICKET_REFUSED");
    const ticketResult = await bounded(response.json(), 2000, "MEDIA_TICKET_TIMEOUT"); this.fetchController=null;
    this.assertAttempt(attempt);
    const ticket = b64url(ticketResult.ticket, 32), call = b64url(this.callId, 16), browser = b64url(this.browserId, 16), gen = generation(this.ownerGeneration);
    this.ws = new WebSocket(`wss://${location.host}/api/intercom_native/ws`); this.ws.binaryType = "arraybuffer"; this.clock = new ClockRelation(); this.peerPingSeen = false; this.rxSequence = 0; this.txSequence = 0; this.playbackPending = []; this.playbackPortInflight = 0;
    await new Promise((resolve, reject) => { this.attachReject=reject; this.attachTimer = setTimeout(() => reject(new Error("MEDIA_ATTACH_TIMEOUT")), 3000); this.ws.onopen = () => { const payload = new Uint8Array(72), view = new DataView(payload.buffer); payload.set(call, 0); writeU64(view, 16, gen); payload.set(browser, 24); payload.set(ticket, 40); this.ws.send(frame(TYPES.ATTACH, payload)); }; this.ws.onmessage = event => { try { const parsed = parseFrame(event.data); if (parsed.type !== TYPES.ATTACH_OK) throw new Error("ATTACH_REFUSED"); for (let i=0;i<16;i++) if(parsed.bytes[i]!==call[i]) throw new Error("ATTACH_REFUSED"); const backend=parsed.view.getUint8(24); if (parsed.view.getBigUint64(16,true)!==gen || (backend!==1&&backend!==2)) throw new Error("ATTACH_REFUSED"); this.metrics.simulation=backend===1;this.metrics.endpointBackend=backend===1?"simulation":"real_endpoint"; this.metrics.path = ({1:"direct_lan",2:"nabu_remote"})[parsed.view.getUint8(25)] || "unknown"; clearTimeout(this.attachTimer); this.attachTimer=null;this.attachReject=null; resolve(); } catch(e){ clearTimeout(this.attachTimer);this.attachTimer=null;this.attachReject=null;reject(e); } }; this.ws.onerror = () => reject(new Error("MEDIA_SOCKET_FAILED")); });
    this.assertAttempt(attempt);
    this.ws.onmessage = event => { try { this.onMedia(parseFrame(event.data)); } catch (error) { this.cleanup(`Media ended: ${error.message}`, true); } }; this.ws.onclose = event => { if (this.callId) this.cleanup(mediaCloseReason(event.reason), true); };
    this.mediaStarted=performance.now(); this.intervals = [setInterval(() => this.sendReady(), 250), setInterval(() => this.sendPing(), 1000), setInterval(() => this.updateConnected(), 250)]; this.sendPing(); this.status = "Connecting media…"; this.render();
  }
  onWorklet(data) {
    if (data?.type === "capture") {
      this.metrics.captureFrames++;
      let peak=0;for(const sample of data.samples)peak=Math.max(peak,Math.abs(sample));this.metrics.capturePeak=Math.max(this.metrics.capturePeak,peak);if(peak>0)this.metrics.nonzeroCaptureFrames++;
      if(!this.inflight||!this.ctx||!this.node)return;
      const filterDelay=(31/this.ctx.sampleRate)*1_000_000, quantum=128/this.ctx.sampleRate*1_000_000, contextHold = Math.max(0, (this.ctx.currentTime - data.captureEndTime) * 1_000_000)+quantum, freshCapture = contextHold <= 64000;
      if(freshCapture)this.lastCapture = performance.now(); else this.metrics.txDrops++;
      if (freshCapture && Number.isFinite(data.outputEndTime) && this.ctx.currentTime >= data.outputEndTime && this.ctx.currentTime - data.outputEndTime <= .064) this.lastOutputProgress = performance.now();
      if (freshCapture && !this.softMuted && this.clock?.fresh() && this.peerPingSeen && this.ws?.readyState === WebSocket.OPEN) {
        if (!canQueueAudio(this.ws.bufferedAmount)) this.metrics.txDrops++;
        else if (this.txSequence >= 0xffffffff) this.cleanup("Media sequence exhausted.", true);
        else { const payload = new Uint8Array(1042), view = new DataView(payload.buffer), age = Math.min(0xffffffff, Math.ceil(32000 + contextHold + filterDelay)); view.setUint32(0, this.txSequence++, true); writeU64(view, 4, nowUs()); view.setUint32(12, age, true); view.setUint16(16, 512, true); payload.set(new Uint8Array(data.samples.buffer), 18); this.ws.send(frame(TYPES.AUDIO, payload)); }
      }
      this.node?.port.postMessage({type:"capture-ack"});
    } else if (data?.type === "playback-accepted") { this.playbackPortInflight=Math.max(0,this.playbackPortInflight-1); this.pumpPlayback(); }
    else if (data?.type === "played" || data?.type === "playback-stats") {
      this.metrics.workletQueueDrops=Math.max(this.metrics.workletQueueDrops,data.queueDrops||0);
      this.metrics.workletDeadlineDrops=Math.max(this.metrics.workletDeadlineDrops,data.deadlineDrops||0);
      this.metrics.workletQueueResidenceMaxUs=Math.max(this.metrics.workletQueueResidenceMaxUs,data.queueResidenceMaxUs||0);
      this.metrics.rxDrops=this.metrics.mainQueueDrops+this.metrics.workletQueueDrops+this.metrics.workletDeadlineDrops;
      if(data.type==="played"){this.metrics.renderedFrames++;this.lastPlayed=performance.now();}
    }
  }
  pumpPlayback() { while(this.inflight&&this.node&&this.playbackPortInflight<2&&this.playbackPending?.length){ const item=this.playbackPending.shift(); this.playbackPortInflight++; this.node.port.postMessage(item,[item.samples.buffer]); } }
  onMedia({ type, view }) {
    if (type === TYPES.AUDIO) {
      const received = performance.now(), seq = view.getUint32(0, true); if (seq !== this.rxSequence || this.rxSequence >= 0xffffffff) throw new Error("INVALID_SEQUENCE"); this.rxSequence++;
      if (view.getUint16(16, true) !== 512) throw new Error("INVALID_AUDIO");
      if (!this.clock.fresh()) { this.metrics.staleDrops++; this.metrics.clockStaleDrops++; this.metrics.clockUncertaintyUs=Number.isFinite(this.clock.uncertainty)?Math.ceil(this.clock.uncertainty):0; return; }
      const ceiling = 2000000;
      // The worklet deadline charges actual queue residence; do not subtract it twice.
      const age = this.clock.ageUpper(view.getBigUint64(4,true), view.getUint32(12,true), Math.max(0,(performance.now()-received)*1000));
      const outputLatency=this.ctx.outputLatency||0, baseLatency=this.ctx.baseLatency||0;
      const outputLead = (outputLatency + baseLatency + 128 / this.ctx.sampleRate) * 1_000_000;
      this.metrics.receivedMediaAgeMaxUs=Math.max(this.metrics.receivedMediaAgeMaxUs,Math.ceil(age));
      this.metrics.clockUncertaintyUs=Math.ceil(this.clock.uncertainty||0);
      this.metrics.outputLatencyUs=Math.ceil(outputLatency*1_000_000);this.metrics.baseLatencyUs=Math.ceil(baseLatency*1_000_000);this.metrics.outputLeadUs=Math.ceil(outputLead);
      if (age + outputLead > ceiling) { this.metrics.staleDrops++; this.metrics.admissionExpiredDrops++; return; }
      const samples = new Int16Array(512); for(let i=0;i<512;i++) samples[i]=view.getInt16(18+i*2,true);
      const item={type:"play",samples,deadline:this.ctx.currentTime+(ceiling-age-outputLead)/1_000_000}; if(this.playbackPending.length===1){this.playbackPending.shift();this.metrics.mainQueueDrops++;this.metrics.rxDrops++;} this.playbackPending.push(item); this.pumpPlayback();
    } else if (type === TYPES.PING) { const payload = new ArrayBuffer(32), out = new DataView(payload), t2=nowUs(); writeU64(out,0,view.getBigUint64(0,true)); writeU64(out,8,view.getBigUint64(8,true)); writeU64(out,16,t2); writeU64(out,24,nowUs()); this.ws.send(frame(TYPES.PONG,new Uint8Array(payload))); this.peerPingSeen=true; }
    else if (type === TYPES.PONG) { this.clock.pong(view); this.metrics.clockUncertaintyUs=Number.isFinite(this.clock.uncertainty)?Math.ceil(this.clock.uncertainty):0;this.metrics.browserClockRejectedSamples=this.clock.rejectedSamples;this.metrics.browserClockRejectedUncertaintyMaxUs=Math.ceil(this.clock.rejectedUncertaintyMax); }
    else if (type === TYPES.TERMINATE || type === TYPES.ERROR) throw new Error(`SERVER_${view.getUint16(0,true)}`);
    else throw new Error("INVALID_FRAME");
  }
  sendReady() { if(this.ws?.readyState!==WebSocket.OPEN)return; const now=performance.now(), capture=now-(this.lastCapture||0)<500, playback=Boolean(this.lastOutputProgress)&&now-this.lastOutputProgress<500, input=this.metrics.callerInput==="synthetic_silence"?Boolean(this.silenceSource):this.stream?.getAudioTracks().some(t=>t.readyState==="live"&&!t.muted), running=this.ctx?.state==="running"; if(!(capture&&playback&&input&&running&&this.outputReady()&&document.visibilityState==="visible"&&isSecureContext))return; const payload=new ArrayBuffer(8), view=new DataView(payload); view.setUint8(0,15); view.setUint32(1,this.ctx.sampleRate,true); view.setUint8(5,0); view.setUint16(6,0,true); this.ws.send(frame(TYPES.READY,new Uint8Array(payload))); }
  sendPing() { if(this.ws?.readyState===WebSocket.OPEN)this.ws.send(this.clock.ping()); }
  updateConnected() {
    if(!this.inflight&&!this.callId)return;
    this.metrics.clockFresh=Boolean(this.clock?.fresh());
    const now=performance.now(), capture=now-(this.lastCapture||0)<500, playback=now-(this.lastPlayed||0)<500;
    const roomMuted=this.metrics.server?.room_mic_muted===true,speakerOnly=this.metrics.backend==="ACTIVE_SPEAKER_ONLY",explicitOneWay=this.metrics.callMode==="speaker_only";
    const backend=this.metrics.backend==="ACTIVE_FULL"||speakerOnly;
    this.mediaWasActive=Boolean(this.mediaWasActive||backend);
    const localOutput=Boolean(this.lastOutputProgress)&&now-this.lastOutputProgress<500;
    const elapsed=now-(this.mediaStarted||now);
    // READY proves the local graph before START. Actual room PCM remains
    // required after activation; waiting for it before READY deadlocks startup.
    const outputProgress=this.mediaWasActive ? (speakerOnly||roomMuted||explicitOneWay?localOutput:playback) : localOutput;
    const outputStamp=this.mediaWasActive&&!(speakerOnly||roomMuted||explicitOneWay)?this.lastPlayed:this.lastOutputProgress;
    const captureStalled=elapsed>2000&&now-(this.lastCapture||this.mediaStarted)>2000,outputStalled=elapsed>2000&&now-(outputStamp||this.mediaStarted)>2000;
    if(this.inflight&&(captureStalled||outputStalled||this.ctx?.state!=="running")&&elapsed>2000){this.cleanup("Media progress stopped.",true);return;}
    if(this.inflight&&!this.mediaWasActive&&elapsed>10000){this.cleanup("Media setup timed out.",true);return;}
    const output=this.outputReady();
    if(this.outputAudio&&!output&&now-this.outputSwitchStarted>2000){this.cleanup("Speaker output stopped.",true);return;}
    const target=this.metrics.simulation?"simulated endpoint":"authenticated room endpoint";
    const inputLabel=this.metrics.callerInput==="synthetic_silence"?" — synthetic silence uplink; microphone not tested":"";
    this.status=capture&&(speakerOnly?localOutput:playback)&&backend&&this.metrics.clockFresh&&output ? (speakerOnly?`CONNECTED to ${target} — one-way audio to room; room microphone not transmitted${roomMuted?" (room microphone muted)":""}${inputLabel}`:`CONNECTED to ${target} — bidirectional media progressing${inputLabel}`) : (this.routingPending ? "Selecting speaker output…" : `Connecting ${target} media…${inputLabel}`);
    this.render();
  }

  speakerOutput() { return this.outputDevices?.find(device=>device.kind==="audiooutput"&&device.label.trim().toLowerCase()==="speaker"&&device.deviceId); }
  outputReady() {
    const audio=this.outputAudio;
    if(!audio)return true;
    const now=performance.now();
    if(Number.isFinite(audio.currentTime)&&audio.currentTime>this.outputLastTime){this.outputLastTime=audio.currentTime;this.outputLastAdvance=now;this.metrics.outputElementAdvances++;}
    return !this.routingPending&&!audio.paused&&!audio.ended&&!audio.muted&&audio.volume>0&&audio.readyState>=2&&now-this.outputLastAdvance<500;
  }
  async useSpeaker() {
    const device=this.speakerOutput();
    if(!this.callId||!this.inflight||!device||this.outputAudio)return;
    const attempt=this.attempt;
    try {
      const audio=document.createElement("audio");
      if(typeof audio.setSinkId!=="function"||!this.ctx.createMediaStreamDestination)throw new Error("Speaker selection is unavailable in this browser");
      this.outputAudio=audio; this.outputBridge=this.ctx.createMediaStreamDestination();
      this.outputSwitchStarted=performance.now();this.outputLastTime=0;this.outputLastAdvance=-Infinity;this.routingPending=true;
      this.metrics.requestedOutput="Speaker";this.metrics.routeControl="media_element";this.metrics.routeApplied=false;this.metrics.outputElementAdvances=0;this.metrics.outputStageLatency="unmeasured";this.metrics.playoutDeadlineScope="worklet; added media-element stage unmeasured";
      audio.hidden=true;audio.setAttribute("playsinline","");audio.srcObject=this.outputBridge.stream;this.append(audio);
      for(const event of ["error","ended"])audio.addEventListener(event,()=>{if(this.inflight&&this.outputAudio===audio)this.cleanup("Speaker output stopped.",true);});
      this.node.connect(this.outputBridge);this.node.disconnect(this.ctx.destination);
      // Invoke both while the button's user activation is current. No playback-only AudioSession change.
      const selecting=Promise.all([audio.setSinkId(device.deviceId),audio.play()]);
      this.status="Selecting speaker output…";this.render();
      await bounded(selecting,3000,"Speaker selection timed out");this.assertAttempt(attempt);
      this.routingPending=false;this.metrics.routeApplied=true;this.updateConnected();
    }catch(error){if(attempt===this.attempt&&this.inflight)await this.cleanup("Speaker output unavailable; start a new call to retry.",true);}
  }
  async toggleMute() {
    if(!this.callId||!this.inflight)return;
    const attempt=this.attempt, enabled=this.softMuted;
    try {
      await bounded(this._hass.connection.sendMessagePromise({type:"intercom_native/set_caller_tx_enabled",call_id:this.callId,owner_generation:this.ownerGeneration,enabled}),3000,"CONTROL_TIMEOUT");
      this.assertAttempt(attempt);
      this.softMuted=!enabled; this.render();
    } catch(error) {
      if(attempt===this.attempt&&this.inflight)await this.cleanup(`Mute control failed: ${error?.message||"CONTROL_LOST"}`,true);
    }
  }
  async cleanup(reason, tellServer) {
    if(this.cleaning)return this.cleanupPromise;
    this.cleaning=true; this.inflight=false; this.attempt++;
    const callId=this.callId, ownerGeneration=this.ownerGeneration, ctx=this.ctx, awaitingPermission=this.permissionPending; this.permissionPromise=null; this.pendingStart=null; this.callId=null; this.ownerGeneration=null; this.intervals?.forEach(clearInterval); this.intervals=[]; try{this.unsubscribe?.();}catch{} this.unsubscribe=null;
    if(this.controlLost)this._hass?.connection?.removeEventListener?.("disconnected",this.controlLost); this.controlLost=null;
    this.fetchController?.abort();this.fetchController=null;if(this.attachTimer){clearTimeout(this.attachTimer);this.attachTimer=null;} if(this.attachReject){this.attachReject(new Error("CALL_ABORTED"));this.attachReject=null;} if(this.ws){this.ws.onopen=null;this.ws.onmessage=null;this.ws.onerror=null;this.ws.onclose=null;try{this.ws.close();}catch{}} this.ws=null; this.node?.port.postMessage({type:"clear"}); try{this.silenceSource?.stop();}catch{} try{this.source?.disconnect();}catch{} try{this.node?.disconnect();}catch{} this.stream?.getTracks().forEach(track=>track.stop()); this.stream=null; this.silenceSource=null; this.source=null; this.ctx=null; this.node=null; this.playbackPending=[];
    const outputAudio=this.outputAudio, outputBridge=this.outputBridge;this.outputAudio=null;this.outputBridge=null;this.outputDevices=[];this.routingPending=false;
    if(outputAudio){try{outputAudio.pause();outputAudio.srcObject=null;outputAudio.remove();}catch{}}
    outputBridge?.stream.getTracks().forEach(track=>track.stop());
    this.status=(reason||"Call ended")+(awaitingPermission?" Dismiss the browser microphone prompt before starting another call.":""); this.metrics.backend="TERMINATED"; this.render();
    this.cleanupPromise=(async()=>{ if(ctx)await bounded(ctx.close(),3000,"AUDIO_CLOSE_TIMEOUT").catch(()=>{}); if(tellServer&&callId&&this._hass?.connection)await bounded(this._hass.connection.sendMessagePromise({type:"intercom_native/stop",call_id:callId,owner_generation:ownerGeneration}),3000,"STOP_TIMEOUT").catch(()=>{}); this.cleaning=false; this.render(); })();
    return this.cleanupPromise;
  }
  async inspectOutputs() {
    if(this.outputCheckPending)return;
    this.outputCheckPending=true;
    const metrics=this.metrics;let outputs=[];
    const result={audioContextSetSinkId:typeof globalThis.AudioContext?.prototype?.setSinkId==="function",mediaElementSetSinkId:typeof globalThis.HTMLMediaElement?.prototype?.setSinkId==="function",selectAudioOutput:typeof navigator.mediaDevices?.selectAudioOutput==="function",audioSession:typeof navigator.audioSession?.type==="string",outputLabels:[],enumeration:"unavailable",physicalRoute:"unverified"};
    try {
      if(navigator.mediaDevices?.enumerateDevices){
        const devices=await bounded(navigator.mediaDevices.enumerateDevices(),3000,"OUTPUT_CHECK_TIMEOUT");
        outputs=devices.filter(device=>device.kind==="audiooutput").slice(0,8).map(device=>({kind:"audiooutput",deviceId:String(device.deviceId||""),label:String(device.label||"Unlabelled output").slice(0,80)}));
        result.outputLabels=outputs.map(device=>device.label);
        result.enumeration="complete";
      }
    }catch(error){result.enumeration=error?.message==="OUTPUT_CHECK_TIMEOUT"?"timeout":"failed";}
    finally{this.outputCheckPending=false;}
    if(this.metrics!==metrics)return;
    if(this.callId&&this.inflight&&result.enumeration==="complete")this.outputDevices=outputs;
    metrics.outputCapabilities=result; this.render();
  }
  async copyReport() { const inputNote=this.metrics.callerInput==="synthetic_silence"?" Synthetic silence uplink; microphone and physical speech were not tested.":""; const report={title:"Intercom M3 PoC metadata-only report",created_at:new Date().toISOString(),...this.metrics,status:this.status,note:`${this.metrics.simulation?"Simulated endpoint":"Real endpoint"}; no recording or audio payload included.${inputNote} Physical I/O and acoustic latency are unproven.`}; try{await navigator.clipboard.writeText(JSON.stringify(report,null,2));this.shadowRoot.querySelector("#copy").textContent="Metadata copied";}catch{this.shadowRoot.querySelector("#copy").textContent="Clipboard unavailable; select metadata below";} }
}

if (globalThis.customElements && !customElements.get("intercom-m3-poc-panel")) customElements.define("intercom-m3-poc-panel", IntercomM3PocPanel);
globalThis.IntercomM3FrontendTest = { b64url, frame, parseFrame, generation, canQueueAudio, safeServerMetrics, mediaCloseReason, ClockRelation, IntercomM3PocPanel, LENGTHS };

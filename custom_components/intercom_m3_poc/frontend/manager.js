import "./panel.js?v=0.4.2";
import { IntercomManagerClient, managerError, escapeHtml as esc, roomState } from "./client.js?v=0.4.2";

const pages = ["Overview", "Call", "Rooms", "Speakers", "People", "Announcements", "History", "Settings", "Diagnostics"];
const clone = value => JSON.parse(JSON.stringify(value));
const option = (value, label, selected) => `<option value="${esc(value)}" ${selected === value ? "selected" : ""}>${esc(label)}</option>`;
const field = (id, label, value = "", type = "text", extra = "") => `<label for="${id}">${label}</label><input id="${id}" name="${id}" type="${type}" value="${esc(value)}" ${extra}>`;
const check = (id, label, value) => `<label class="check"><input id="${id}" name="${id}" type="checkbox" ${value ? "checked" : ""}> ${label}</label>`;
const unsupported = text => `<p class="notice">${esc(text)}</p>`;

export class IntercomManagerPanel extends HTMLElement {
  constructor() {
    super(); this.attachShadow({ mode: "open" }); this.page = "Overview"; this.snapshot = null; this.historyOffset = 0; this.epoch = 0;
    this.shadowRoot.innerHTML = `<style>
      :host{display:block;color:var(--primary-text-color,#172536);background:var(--primary-background-color,#f5f7fa);min-height:100%;font:16px system-ui;--accent:var(--primary-color,#1769aa)}
      *{box-sizing:border-box}header,main{max-width:1180px;margin:auto;padding:20px}header{padding-bottom:4px}h1{font-size:1.8rem;margin:0}h2{font-size:1.35rem;margin-top:0}h3{margin:0 0 8px}p{line-height:1.5}nav{display:flex;gap:8px;overflow-x:auto;padding:16px 0}button,input,select,textarea{font:inherit;border:1px solid var(--divider-color,#9aa8b8);border-radius:8px;min-height:46px;padding:10px;background:var(--card-background-color,#fff);color:inherit}button{cursor:pointer}button:focus-visible,input:focus-visible,select:focus-visible,textarea:focus-visible,a:focus-visible{outline:3px solid var(--accent);outline-offset:3px}button:disabled{opacity:.5;cursor:default}button.primary,nav button[aria-current=page]{background:var(--accent);color:#fff}.danger{color:var(--error-color,#b00020)}label{display:block;margin:14px 0 5px;font-weight:600}input:not([type=checkbox]),select,textarea{width:100%}textarea{min-height:100px;resize:vertical}.check{display:flex;align-items:center;gap:10px}.check input{min-height:24px;width:24px}section[hidden],div[hidden]{display:none!important}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:16px}.card,form,.notice{overflow-wrap:anywhere;background:var(--card-background-color,#fff);border:1px solid var(--divider-color,#d6dde5);border-radius:12px;padding:18px;margin-bottom:16px}.notice{border-left:4px solid var(--accent)}.muted,small{color:var(--secondary-text-color,#536779)}small{display:block;line-height:1.5}.actions{display:flex;flex-wrap:wrap;gap:10px;margin-top:18px}.row{display:flex;flex-wrap:wrap;gap:12px;align-items:center}.row>*{flex:1;min-width:150px}.status{min-height:26px;margin:6px 0;color:var(--secondary-text-color,#536779)}dl{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:0}dt{font-size:.9rem;color:var(--secondary-text-color,#536779)}dd{margin:0}pre{white-space:pre-wrap;overflow-wrap:anywhere;max-height:60vh;overflow:auto;font-size:.85rem}table{width:100%;border-collapse:collapse;font-size:.9rem}th,td{text-align:left;padding:10px;border-bottom:1px solid var(--divider-color,#d6dde5)}.table{overflow-x:auto}.empty{padding:24px;text-align:center}.badge{display:inline-block;padding:4px 8px;border-radius:20px;background:rgba(127,127,127,.12);font-size:.85rem}a{color:var(--accent)}#call-host{display:block}details{margin:16px 0}summary{cursor:pointer;min-height:44px;padding:10px} @media(max-width:520px){header,main{padding:14px}h1{font-size:1.5rem}.grid{grid-template-columns:1fr}nav button{min-width:max-content}dl{grid-template-columns:1fr 1fr}.actions button{flex:1}th,td{padding:8px}}
    </style><header><h1>Intercom Manager</h1><p class="muted">Local rooms, calls and household controls</p><nav aria-label="Intercom screens">${pages.map(name => `<button type="button" data-page="${name}" ${name === "Speakers" ? "hidden" : ""}>${name === "People" ? "People & permissions" : name}</button>`).join("")}</nav><p id="status" class="status" role="status" aria-live="polite">Connecting to Home Assistant…</p></header><main>${pages.map(name => `<section id="page-${name}" aria-label="${name}" ${name === this.page ? "" : "hidden"}><div id="content-${name}"></div></section>`).join("")}</main>`;
    this.shadowRoot.querySelectorAll("[data-page]").forEach(button => { button.onclick = () => this.navigate(button.dataset.page); });
    this.callPanel = document.createElement("intercom-m3-poc-panel");
    this.shadowRoot.querySelector("#content-Call").innerHTML = `<h2>Call a room</h2><p>Choose a room and tap CALL to start an announced drop-in. Your browser microphone opens only after your action.</p><div id="call-host"></div><div id="room-call"></div>`;
    this.shadowRoot.querySelector("#call-host").append(this.callPanel);
    this.paintNavigation();
  }
  set panel(value) { this._panel = value; this.callPanel.panel = value; if(this.announcementPanel)this.announcementPanel.panel=value; }
  set hass(value) {
    const previous = this._hass?.connection, changed = value?.connection !== previous;
    if (changed && previous) {
      this.epoch++; this.viewRequest=Symbol(); this.refreshing=false; this.snapshot=null; this.configuration=null;
      clearInterval(this.timer); this.timer=null; this.unsubscribe?.(); this.unsubscribe=null; this.removeControlListener();
      this.callPanel.cleanup?.("CONTROL_LOST",true); this.clearPrivateScreens(); this.paintOverview(); this.paintRoomCall();
      this.setStatus("Connecting to Home Assistant. Room status is unknown.");
    }
    this._hass = value; this.client = new IntercomManagerClient(value); this.callPanel.hass = value; if(this.announcementPanel)this.announcementPanel.hass=value;
    if (changed && this.isConnected) this.connect();
  }
  connectedCallback() { this.connect(); }
  disconnectedCallback() { this.clearPrivateScreens(); this.epoch++; this.refreshing=false; clearInterval(this.timer); this.timer = null; this.unsubscribe?.(); this.unsubscribe = null; this.removeControlListener(); }
  removeControlListener() { if (this.controlConnection && this.controlLost) this.controlConnection.removeEventListener?.("disconnected", this.controlLost); }
  async connect() {
    if (!this.client || !this.isConnected) return;
    const epoch = ++this.epoch; this.refreshing=false; this.unsubscribe?.(); this.unsubscribe = null; this.removeControlListener();
    this.controlConnection = this._hass.connection;
    this.controlLost = () => { this.epoch++; this.refreshing=false; this.viewRequest=Symbol(); this.snapshot = null; this.configuration = null; this.clearPrivateScreens(); this.setStatus("Home Assistant disconnected. Room status is unknown."); this.paintLive(); };
    this.controlConnection?.addEventListener?.("disconnected", this.controlLost);
    clearInterval(this.timer); this.timer = setInterval(() => { if (document.visibilityState === "visible") { this.refresh(); if(this.page === "Speakers") this.loadSpeakers(); } }, 5000);
    await this.refresh();
    try { const unsubscribe = await this.client.subscribe(snapshot => { if (epoch === this.epoch) this.acceptSnapshot(snapshot); }); if (epoch !== this.epoch) unsubscribe(); else this.unsubscribe = unsubscribe; } catch { /* Read polling is the compatibility fallback. */ }
  }
  async refresh() {
    if (!this.client || this.refreshing) return;
    const epoch = this.epoch; this.refreshing = true;
    try { const snapshot = await this.client.snapshot(); if (epoch === this.epoch) this.acceptSnapshot(snapshot); }
    catch (error) { if (epoch === this.epoch) { this.snapshot = null; this.configuration = null; this.clearPrivateScreens(); const reason=managerError(error); this.setStatus(reason === "NOT_AUTHORIZED" ? "Manager unavailable: NOT_AUTHORIZED. Ask a Home Assistant administrator to grant room access." : `Manager unavailable: ${reason}. Retry after connection recovers.`); this.paintLive(); } }
    finally { if (epoch === this.epoch) this.refreshing = false; }
  }
  acceptSnapshot(snapshot) {
    if (!snapshot || !Array.isArray(snapshot.rooms) || !Number.isSafeInteger(snapshot.revision)) return;
    if (this.snapshot && snapshot.revision < this.snapshot.revision) return;
    const rosterChanged=this.snapshot&&JSON.stringify(this.snapshot.rooms.map(room=>room.room_id))!==JSON.stringify(snapshot.rooms.map(room=>room.room_id));
    if(rosterChanged){this.shadowRoot.querySelector("#room-call").innerHTML="";this.announcementPanel?.cleanup("ROOM_ACCESS_CHANGED",true);this.announcementPanel=null;this.content("Announcements").innerHTML="";}
    const authorityChanged=this.snapshot&&(this.snapshot.revision!==snapshot.revision||this.snapshot.is_admin!==snapshot.is_admin||JSON.stringify(this.snapshot.rooms.map(room=>room.room_id))!==JSON.stringify(snapshot.rooms.map(room=>room.room_id)));
    if(authorityChanged){this.viewRequest=Symbol();this.content("History").innerHTML=unsupported("Permissions or configuration changed. Reload History to fetch authorized records.");this.announcementSources=null;this.announcementResult=null;if(this.page==="Announcements"&&!this.announcementPanel?.inflight)this.content("Announcements").innerHTML=unsupported("Configuration changed. Reopen Announcements to reload authorized sources.");}
    this.snapshot = snapshot; const speakerTab=this.shadowRoot.querySelector('[data-page="Speakers"]'); if(speakerTab)speakerTab.hidden=!snapshot.is_admin;
    if (!snapshot.is_admin) { this.clearDeployment(); this.endpointProfiles=null; this.profileRequest=Symbol(); this.configuration = null; for (const name of ["Rooms", "Speakers", "People", "Settings", "Diagnostics"]) this.content(name).innerHTML = unsupported("Home Assistant administrator access is required for these controls."); }
    this.setStatus(`Connected · ${snapshot.rooms.length} permitted room${snapshot.rooms.length === 1 ? "" : "s"} · configuration ${snapshot.revision}`); this.paintLive();
  }
  clearPrivateScreens() { const tab=this.shadowRoot.querySelector('[data-page="Speakers"]'); if(tab)tab.hidden=true; this.clearDeployment(); this.endpointProfiles=null; this.profileRequest=Symbol(); this.announcementPanel?.cleanup("CONTROL_LOST",true); this.announcementPanel=null; this.announcementSources=null; this.announcementResult=null; for (const name of ["Rooms", "Speakers", "People", "Settings", "History", "Diagnostics", "Announcements"]) this.content(name).innerHTML = unsupported("Reconnect to Home Assistant to load authorized data."); }
  setStatus(message) { const node = this.shadowRoot.querySelector("#status"); if (node.textContent !== message) node.textContent = message; }
  paintNavigation() { this.shadowRoot.querySelectorAll("[data-page]").forEach(button => { button.setAttribute("aria-current", button.dataset.page === this.page ? "page" : "false"); if(button.dataset.page === "Speakers") button.hidden = !this.snapshot?.is_admin; }); }
  async navigate(page) {
    if (!pages.includes(page)) return;
    if(this.page === "Speakers" || page === "Speakers") this.clearDeployment();
    if (this.page === "Call" && page !== "Call" && (this.callPanel.inflight || this.callPanel.callId)) await this.callPanel.cleanup("LEFT_CALL_SCREEN", true);
    if(this.page === "Announcements" && page !== "Announcements" && this.announcementPanel)await this.announcementPanel.cleanup("LEFT_ANNOUNCEMENT_SCREEN",true);
    this.page = page; const request = this.navigationRequest = this.viewRequest = Symbol(), epoch = this.epoch; this.paintNavigation();
    for (const name of pages) this.shadowRoot.querySelector(`#page-${name}`).hidden = name !== page;
    this.paintLive();
    try {
      if (["Rooms", "People", "Settings"].includes(page)) {
        if (!this.snapshot?.is_admin) { this.content(page).innerHTML = unsupported("Home Assistant administrator access is required for these controls."); return; }
        if (this.content(page).querySelector("#configuration-conflict")) return;
        const config = await this.client.configuration(); if (this.page !== page || request !== this.viewRequest || epoch !== this.epoch || !this.snapshot?.is_admin) return; this.configuration = config; this.paintConfiguration(page);
      }
      if (page === "Speakers") await this.loadSpeakers();
      if (page === "History") await this.loadHistory(0);
      if (page === "Diagnostics") await this.loadDiagnostics();
      if(page === "Announcements")await this.loadAnnouncementSources();
    } catch (error) { this.setStatus(`Unable to load ${page.toLowerCase()}: ${managerError(error)}`); }
  }
  content(page = this.page) { return this.shadowRoot.querySelector(`#content-${page}`); }
  paintLive() {
    if (this.page === "Overview") this.paintOverview();
    if (this.page === "Call") this.paintRoomCall();
    if(this.page === "Rooms")this.paintRoomVolumeStatus();
    if(this.page === "Announcements"){if(!this.content().querySelector("form"))this.paintAnnouncements();this.paintAnnouncementStatus();}
  }
  paintOverview() {
    const rooms = this.snapshot?.rooms || [], calls = this.snapshot?.calls || [];
    const active = this.shadowRoot.activeElement, focusId = active?.id, focusRoom = active?.dataset?.callRoom;
    this.content("Overview").innerHTML = `<h2>Rooms</h2>${!this.snapshot ? unsupported("Room status is unavailable. No live availability can be inferred.") : ""}<div class="grid">${rooms.map(room => `<article class="card"><h3>${esc(room.name || room.room_name)}</h3><span class="badge">${roomState(room)}</span><dl><dt>Activity</dt><dd>${esc(room.activity || "Unknown")}</dd><dt>Room microphone</dt><dd>${room.muted === true ? "Muted" : room.muted === false ? "On" : "Unknown"}</dd><dt>Last report</dt><dd>${Number.isFinite(room.last_report_age_ms) ? `${Math.round(room.last_report_age_ms / 1000)} seconds ago` : "Unknown"}</dd><dt>Audio modes</dt><dd>${(room.supported_modes || []).map(mode => mode === "speaker_only" ? "Speak only" : "Talk and listen").join(", ") || "Unknown"}</dd></dl><div class="actions"><button type="button" data-call-room="${esc(room.device_id)}" ${room.available === false || room.busy || room.dnd ? "disabled" : ""}>Call this room</button></div></article>`).join("")}</div>${!rooms.length && this.snapshot ? `<p class="empty">${this.snapshot.is_admin ? "No rooms are enrolled. Open Rooms to configure a room, or Settings to manage this integration." : "No rooms are available to this account. Ask an administrator to enroll a room or grant access."}</p>` : ""}<h2>Active calls</h2><div class="grid">${calls.map(call => `<article class="card"><h3>${esc(call.type || "Call")}</h3><p>${esc(call.state || "Unknown")}</p><p>${esc((call.room_ids || []).map(id => rooms.find(room => room.room_id === id)?.name || "Room").join(" · "))}</p></article>`).join("") || '<p class="empty">No active calls visible to this account.</p>'}</div><div class="actions"><button id="overview-refresh">Refresh status</button><button id="overview-call">Open call controls</button></div>`;
    this.content("Overview").querySelector("#overview-refresh").onclick = () => this.refresh();
    this.content("Overview").querySelector("#overview-call").onclick = () => this.navigate("Call");
    this.content("Overview").querySelectorAll("[data-call-room]").forEach(button => { button.onclick = () => { this.callPanel.selectedDeviceId = button.dataset.callRoom; this.callPanel.render(); this.navigate("Call"); }; });
    const restore = focusRoom ? [...this.content("Overview").querySelectorAll("[data-call-room]")].find(button => button.dataset.callRoom === focusRoom) : focusId ? this.content("Overview").querySelector(`#${focusId}`) : null;
    if (restore && !restore.disabled) restore.focus({ preventScroll: true });
  }
  paintRoomCall() {
    const target = this.shadowRoot.querySelector("#room-call");
    const capabilities = this.snapshot?.capabilities || {}, rooms = this.snapshot?.rooms || [];
    if (!rooms.length) { target.innerHTML = unsupported("No permitted rooms are available for room calls. Configure rooms and access in Intercom Manager."); return; }
    if (!capabilities.room_call) { target.innerHTML = `<h2>Room calls and conferences</h2>${unsupported("Room calls, regular ringing, invitations and conferences appear here when the connected coordinator and endpoints advertise those capabilities. Browser calls above use the existing authenticated audio path.")}<p class="muted">Leaving the Call screen ends your browser call. Physical microphone mute always takes precedence.</p>`; return; }
    // Refresh only status while a form is being edited: incoming snapshots must
    // not discard target selection, user input or keyboard focus.
    if (!target.querySelector("form")) {
      target.innerHTML = `<h2>Room calls${capabilities.conference ? " and conferences" : ""}</h2><form id="room-call-form"><label for="call-source">Source room</label><select id="call-source">${rooms.map(room => option(room.room_id, room.name, "")).join("")}</select><label for="call-targets">Destination room${capabilities.conference ? "s" : ""}</label><select id="call-targets" ${capabilities.conference ? 'multiple size="4"' : ""}>${rooms.map(room => option(room.room_id, room.name, "")).join("")}</select><small>Select a different source and destination. Every selected initial room must be available and permitted. Regular ringing requires a supporting profile on every room; the coordinator rejects incompatible choices.</small><label for="room-answer">Answer policy</label><select id="room-answer"><option value="drop_in">Announced drop-in</option>${capabilities.manual_answer ? '<option value="ring">Ring for acceptance</option>' : ""}</select><label for="room-mode">Audio mode</label><select id="room-mode"><option value="full_duplex">Talk and listen</option><option value="speaker_only">Source speaks only</option></select><div class="actions"><button type="submit" class="primary">Start room call</button></div></form><div id="managed-calls"></div><p class="muted">Physical microphone mute always takes precedence. Browser calls above end when you leave this screen; independent room calls have explicit end controls.</p>`;
      target.querySelector("#room-call-form").onsubmit = async event => {
        event.preventDefault(); const get = id => target.querySelector(`#${id}`), source = get("call-source").value, targets = [...get("call-targets").selectedOptions].map(value => value.value);
        if (!this.snapshot?.capabilities?.room_call || !this.snapshot.rooms.some(room => room.room_id === source) || !targets.length || targets.some(id => !this.snapshot.rooms.some(room => room.room_id === id)) || targets.includes(source)) { this.setStatus("Choose a source and at least one different destination."); return; }
        const button = target.querySelector("button[type=submit]"); button.disabled = true;
        try { const result = await this.client.request("start_call", { source, target_room_ids: targets, mode: get("room-mode").value, answer_policy: get("room-answer").value }); this.setStatus(`Room call ${result.state || "started"}`); await this.refresh(); }
        catch (error) { this.setStatus(`Room call refused: ${managerError(error)}`); }
        finally { button.disabled = false; }
      };
    }
    this.paintManagedCalls(target.querySelector("#managed-calls"));
  }
  paintManagedCalls(target) {
    const rooms=this.snapshot?.rooms||[], calls=rooms.length?(this.snapshot?.calls||[]).filter(call=>call.type!=="announcement"):[];
    const focus=this.shadowRoot.activeElement?.dataset?.controlKey;
    const selections=new Map([...target.querySelectorAll("[data-invite-target]")].map(node=>[node.dataset.inviteTarget,node.value]));
    const control=(call,operation,label,participant=null,enabled=null)=>`<button type="button" data-operation="${operation}" data-call-id="${esc(call.call_id)}" ${participant?`data-participant-id="${esc(participant)}"`:""} ${enabled===null?"":`data-enabled="${enabled}"`} data-control-key="${esc(`${call.call_id}:${operation}:${participant||""}`)}">${label}</button>`;
    target.innerHTML=`<h3>Active room calls</h3>${calls.map(call=>`<article class="card"><h3>${esc(call.type||"Call")}</h3><p>${esc(call.state)} · ${esc((call.room_ids||[]).map(id=>rooms.find(room=>room.room_id===id)?.name||"Room").join(" · "))}</p>${(call.participants||[]).map(participant=>`<section><h4>${esc(rooms.find(room=>room.room_id===participant.room_id)?.name||"Your room")}</h4><p>Microphone: ${participant.tx_enabled===true?"Enabled by call policy":participant.tx_enabled===false?"Soft muted":"Unknown"}${participant.held?" · Command hold":""}</p><div class="actions">${participant.can_answer?control(call,"answer_call","Answer here",participant.room_id):""}${participant.can_decline?control(call,"decline_call","Decline here",participant.room_id):""}${participant.can_mute&&typeof participant.tx_enabled==="boolean"?control(call,"set_participant_tx_enabled",participant.tx_enabled?"Mute my room microphone":"Enable my room microphone",participant.room_id,!participant.tx_enabled):""}${participant.can_leave?control(call,"leave_call","Leave from this room",participant.room_id):""}</div></section>`).join("")}<div class="actions">${call.can_end?control(call,"end_call","End this call"):""}</div>${call.can_invite?`<label for="invite-${esc(call.call_id)}">Invite a room (rings for acceptance)</label><select id="invite-${esc(call.call_id)}" data-invite-target="${esc(call.call_id)}" data-control-key="${esc(`${call.call_id}:target`)}">${rooms.filter(room=>!(call.room_ids||[]).includes(room.room_id)&&(call.invite_targets||[]).includes(room.room_id)).map(room=>option(room.room_id,room.name,selections.get(call.call_id))).join("")}</select>${control(call,"invite_participant","Invite room")}`:""}</article>`).join("")||"<p>No active room calls.</p>"}`;
    target.querySelectorAll("[data-operation]").forEach(button=>{button.onclick=()=>{
      const {operation,callId,participantId,enabled}=button.dataset, payload={call_id:callId};
      if(participantId)payload.participant_id=participantId;
      if(enabled!==undefined)payload.enabled=enabled==="true";
      if(operation==="invite_participant"){
        payload.participant_id=[...target.querySelectorAll("[data-invite-target]")].find(node=>node.dataset.inviteTarget===callId)?.value;
        if(!payload.participant_id){this.setStatus("Choose an eligible room to invite.");return;}
      }
      this.callControl(button,operation,payload);
    };});
    const restore=[...target.querySelectorAll("[data-control-key]")].find(node=>node.dataset.controlKey===focus);restore?.focus({preventScroll:true});
  }
  async callControl(button,operation,payload){
    button.disabled=true;
    try{await this.client.request(operation,payload);await this.refresh();this.setStatus(operation==="invite_participant"?"Invitation requested; waiting for the room to answer.":"Call control accepted. Physical microphone privacy still applies.");}
    catch(error){this.setStatus(`Call control refused: ${managerError(error)}`);}
    finally{button.disabled=false;}
  }

  async saveConfiguration(next, success = "Configuration saved") {
    if (this.saving || !this.snapshot?.is_admin) return;
    const page=this.page, content=this.content(page);
    if (content.querySelector("#configuration-conflict")) { this.setStatus("Configuration changed. Discard edits and reload before saving again."); return; }
    const epoch=this.epoch, request=this.viewRequest; this.saving = true; content.querySelectorAll("button[type=submit]").forEach(button => button.disabled = true);
    try { await this.client.update(next); const configuration = await this.client.configuration(); if(epoch!==this.epoch||!this.snapshot?.is_admin||request!==this.viewRequest)return; this.configuration=configuration; await this.refresh(); this.setStatus(success); if (["Rooms", "People", "Settings"].includes(this.page)) this.paintConfiguration(this.page); }
    catch (error) { if(epoch===this.epoch) { if (managerError(error) === "CONFIGURATION_CONFLICT" && this.snapshot?.is_admin) this.showConfigurationConflict(page); this.setStatus(`Save failed: ${managerError(error)}. Your edits remain in this form.`); } }
    finally { this.saving = false; content.querySelectorAll("button[type=submit]").forEach(button => button.disabled = Boolean(content.querySelector("#configuration-conflict"))); }
  }
  showConfigurationConflict(page, explanation = "Configuration changed in another session.") {
    const content = this.content(page), notice = document.createElement("div");
    notice.id = "configuration-conflict"; notice.className = "notice"; notice.setAttribute("role", "alert");
    notice.innerHTML = `<p>${esc(explanation)} Your edits remain below and have not been saved. Copy any edits you want to keep, then discard them and load the latest configuration. Review and reapply your changes before saving again.</p><button type="button">Discard edits and reload</button><p class="status" role="status" aria-live="polite"></p>`;
    content.prepend(notice);
    const selector = content.querySelector("#room-edit"); if (selector) selector.disabled = true;
    const button = notice.querySelector("button"), status = notice.querySelector("[role=status]");
    button.onclick = async () => {
      if (button.disabled || !this.snapshot?.is_admin) return;
      const epoch = this.epoch; button.disabled = true;
      try {
        const configuration = await this.client.configuration();
        if (epoch !== this.epoch || this.page !== page || !this.snapshot?.is_admin || content.querySelector("#configuration-conflict") !== notice) return;
        this.configuration = configuration; this.paintConfiguration(page);
        this.setStatus("Latest configuration loaded. Review and reapply your changes before saving.");
      } catch (error) { if (epoch === this.epoch && content.querySelector("#configuration-conflict") === notice) {
        const reason = managerError(error);
        if (["NOT_AUTHORIZED", "HA_AUTH_REQUIRED"].includes(reason)) { this.snapshot = null; this.configuration = null; this.clearPrivateScreens(); this.setStatus(`Reload refused: ${reason}. Reconnect with administrator access.`); this.paintLive(); }
        else status.textContent = `Reload failed: ${reason}. Your edits remain below. Retry when available.`;
      } }
      finally { button.disabled = false; }
    };
  }
  paintConfiguration(page) {
    const config = this.configuration;
    if (page === "Rooms") this.paintRooms(config);
    if (page === "People") this.paintPeople(config);
    if (page === "Settings") this.paintSettings(config);
  }
  clearDeployment() {
    this.deployment = null; this.deploymentRequest = Symbol(); this.deploymentLoading = false; this.deploymentRender = null;
  }
  deploymentCurrent(epoch, request) {
    return epoch === this.epoch && request === this.deploymentRequest && this.page === "Speakers" && this.snapshot?.is_admin;
  }
  acceptDeployment(result) {
    if (!result || !Number.isSafeInteger(result.revision) || result.revision < 0 || result.mode !== "permanent" || !Array.isArray(result.devices)
        || new Set(result.devices.map(device => device?.device_id)).size !== result.devices.length
        || result.devices.some(device => !device || typeof device.device_id !== "string" || !device.device_id || typeof device.capabilities !== "object" || !device.capabilities || typeof device.firmware_state !== "object" || !device.firmware_state)) throw new Error("INVALID_RESPONSE");
    if (this.deployment && result.revision < this.deployment.revision) return;
    this.deployment = result; this.paintSpeakers();
  }
  async loadSpeakers() {
    if (!this.snapshot?.is_admin) { this.clearDeployment(); this.content("Speakers").innerHTML = unsupported("Home Assistant administrator access is required for speaker software."); return; }
    if (this.deploymentLoading || this.deploymentBusy) return;
    const epoch = this.epoch, request = this.deploymentRequest = Symbol(); this.deploymentLoading = true;
    if (!this.deployment) this.content("Speakers").innerHTML = unsupported("Loading speaker software status…");
    try {
      const result = await this.client.request("deployment_status");
      if (this.deploymentCurrent(epoch, request)) this.acceptDeployment(result);
    } catch (error) { if (this.deploymentCurrent(epoch, request)) this.deploymentFailure(error); }
    finally { if (request === this.deploymentRequest) this.deploymentLoading = false; }
  }
  deploymentFailure(error) {
    const reason = managerError(error); this.clearDeployment();
    if (["NOT_AUTHORIZED", "HA_AUTH_REQUIRED"].includes(reason)) {
      this.snapshot = null; this.configuration = null; this.clearPrivateScreens(); this.paintNavigation();
      this.setStatus("Administrator access was lost. Reconnect to reload authorized controls."); return;
    }
    this.content("Speakers").innerHTML = `<h2>Speakers and software</h2>${unsupported(`Software status unavailable: ${reason}. No current device state can be inferred. Reload status before continuing.`)}<button id="deployment-retry" type="button">Reload status</button>`;
    this.content("Speakers").querySelector("#deployment-retry").onclick = () => this.loadSpeakers();
  }
  deploymentReason(code) {
    const labels = { PERSISTENT_INSTALL_NOT_AUTHORIZED: "Permanent installation needs exact device approval.",
      PERMANENT_INSTALL_GATE_CLOSED: "Permanent installation is gated pending device approval and recovery verification.",
      EXACT_PHYSICAL_CHECKPOINT_REQUIRED: "This exact speaker installation still needs physical test approval.",
      BOOT_CLOCK_FLOOR_UNRESOLVED: "Secure startup after reboot is not yet ready.",
      MONITOR_ADOPTION_DEVICE_ACCEPTANCE_REQUIRED: "Application restart handling still needs speaker testing.",
      DURABLE_RECOVERY_DEVICE_ACCEPTANCE_REQUIRED: "Recovery after an interrupted operation still needs speaker testing.",
      PACKAGE_UNAVAILABLE: "This Home Assistant package does not contain an approved speaker software bundle.",
      RECONCILIATION_REQUIRED: "A previous operation needs its result checked before another change.",
      READ_ONLY_PREFLIGHT_COMPLETE: "Read-only identity and compatibility checks passed.",
      HOST_KEY_REJECTED: "The speaker management identity did not match enrollment. Verify it before continuing.",
      STORAGE_UNAVAILABLE: "Home Assistant could not access deployment storage.",
      RECOVERY_NOT_VERIFIED: "Recovery has not been verified.", BOOT_FRESHNESS_NOT_VERIFIED: "Secure startup after reboot has not been verified.",
      PREFLIGHT_REQUIRED: "Run Check speaker before preparing software.", ACTIVE_CALL: "End active calls before continuing.",
      NO_PREVIOUS_RELEASE: "No previous software is available.", UNCERTAIN: "The result is uncertain. Check the speaker before another operation." };
    const text = String(code || "Unavailable");
    return labels[code] || (/^[A-Z][A-Z0-9_]+$/.test(text) ? text.replaceAll("_", " ").toLocaleLowerCase() : text);
  }
  deploymentReviewReady(device) {
    const operation = device.operation, target = operation && Object.hasOwn(operation, "target_version") ? operation.target_version : device.firmware_state.target_version;
    return typeof target === "string" && Boolean(target) && typeof operation?.app_restart_required === "boolean" && typeof operation?.reboot_required === "boolean";
  }
  paintSpeakers() {
    if (!this.snapshot?.is_admin || !this.deployment) return;
    const result = this.deployment, devices = result.devices;
    if (!devices.some(device => device.device_id === this.selectedSpeaker)) this.selectedSpeaker = devices[0]?.device_id;
    const device = devices.find(device => device.device_id === this.selectedSpeaker);
    const signature = JSON.stringify([result, this.selectedSpeaker, Boolean(this.deploymentBusy)]);
    if (signature === this.deploymentRender) return;
    this.deploymentRender = signature;
    const host = this.content("Speakers"), activeId = this.shadowRoot.activeElement?.id;
    const actions = { install: "Install permanent software", rollback_previous: "Roll back software", restore_stock: "Restore stock voice", remove: "Remove permanent software" };
    const stages = { UNCHECKED: "Not checked", CHECKING: "Checking speaker", READY: "Check passed", PREPARING: "Preparing software", REVIEW_REQUIRED: "Ready for your review", RUNNING: "Operation running", SUCCEEDED: "Operation succeeded", FAILED: "Operation failed", UNCERTAIN: "Result uncertain" };
    const operation = device?.operation, state = device?.firmware_state || {}, busy = this.deploymentBusy;
    const targetVersion = operation && Object.hasOwn(operation, "target_version") ? operation.target_version : state.target_version;
    const restart = operation?.app_restart_required === true ? "Speaker voice software will restart." : operation?.app_restart_required === false ? "This step does not restart speaker voice software." : "Voice software restart requirements are unconfirmed.";
    const reboot = operation?.reboot_required === true ? "A speaker reboot is required." : operation?.reboot_required === false ? "This operation does not reboot the speaker." : "Speaker reboot requirements are unconfirmed.";
    const available = action => !busy && device?.capabilities?.[action] === true;
    const reasons = action => Array.isArray(device?.reasons?.[action]) ? device.reasons[action].map(code => this.deploymentReason(code)).join(" ") : "";
    const review = operation?.stage === "REVIEW_REQUIRED" && operation.review_required === true && Object.hasOwn(actions, operation.action) && typeof operation.operation_id === "string" && operation.operation_id;
    host.innerHTML = `<h2>Speakers and software</h2><p>Manage supported speakers from Home Assistant. Speaker software and room settings are separate.</p><p><a href="/config/integrations/integration/intercom_m3_poc">Add or configure a speaker in the Intercom integration</a></p><small>Use Configure to enter management credentials in Home Assistant. Verify the speaker identity before enrollment.</small><div class="actions"><button id="deployment-refresh" type="button" ${busy ? "disabled" : ""}>Reload status</button></div>${devices.length ? `<label for="deployment-device">Speaker</label><select id="deployment-device" ${busy ? "disabled" : ""}>${devices.map(value => option(value.device_id, value.label || value.model || "Enrolled speaker", this.selectedSpeaker)).join("")}</select>` : unsupported("No management speakers are enrolled. Open the Intercom integration to configure a supported speaker.")}${device ? `<article class="card"><h3>${esc(device.label || "Enrolled speaker")}</h3><dl><dt>Model</dt><dd>${esc(device.model || "Unknown")}</dd><dt>Current software</dt><dd>${esc(state.current_version || "Unknown — check speaker")}</dd><dt>Target software</dt><dd>${esc(state.target_version || "Not selected")}</dd><dt>Current installation</dt><dd>${esc(({ stock: "Stock voice", ram: "Temporary — lost after reboot", permanent: "Permanent", unknown: "Unknown" })[state.mode] || "Unknown")}</dd><dt>Target installation</dt><dd>Permanent — starts with Home Assistant after reboot</dd></dl><p id="deployment-operation" role="status" aria-live="polite">${operation ? esc(stages[operation.stage] || "Unknown operation stage") : "No operation recorded"}${operation?.reason ? ` · ${esc(this.deploymentReason(operation.reason))}` : ""}</p><small>Status is stored on Home Assistant. Leaving this screen does not cancel an operation. Reload status after reconnecting.</small><div class="actions"><button id="deployment-probe" type="button" ${available("probe") ? "" : "disabled"}>Check speaker</button>${device.capabilities.reconcile ? `<button id="deployment-reconcile" type="button" ${available("reconcile") ? "" : "disabled"}>Check operation result</button>` : ""}</div><small>Check speaker reads identity, compatibility and current runtime. It does not write speaker storage or restart it.</small>${reasons("probe") ? unsupported(reasons("probe")) : ""}</article><h3>Software actions</h3><p>Permanent software writes speaker storage and configures startup after reboot. The speaker waits for Home Assistant to authorize startup and returns to idle. An operation can interrupt calls and stock voice. Removing the Home Assistant integration does not remove speaker software.</p>${Object.entries(actions).map(([action, label]) => `<article class="card"><h3>${label}</h3>${reasons(action) ? unsupported(reasons(action)) : ""}<button type="button" data-deployment-prepare="${action}" ${available("prepare") ? "" : "disabled"}>Prepare ${action === "install" ? "installation" : action === "rollback_previous" ? "rollback" : action === "restore_stock" ? "stock restore" : "removal"}</button><small>Preparation reviews this action and its package. It does not apply it to the speaker.</small></article>`).join("")}${review ? `<form id="deployment-review"><h3>Review: ${esc(actions[operation.action])}</h3><p>Speaker: ${esc(device.label || "Enrolled speaker")}. Target software: ${esc(targetVersion || "Unknown")}.</p><p>This action changes persistent speaker storage and can interrupt voice service. The backend must admit this exact operation before it runs.</p><p>${esc(restart)} ${esc(reboot)}</p>${!this.deploymentReviewReady(device) ? unsupported("The target software or restart requirements are unconfirmed. Execution is unavailable until the backend supplies the complete review.") : ""}${check("deployment-confirm", "I reviewed this speaker and the persistent software change.", false)}${reasons(operation.action) ? unsupported(reasons(operation.action)) : ""}<button type="submit" id="deployment-execute" class="danger" disabled>${esc(actions[operation.action])}</button></form>` : ""}` : ""}${(Array.isArray(result.limitations) ? result.limitations : []).map(value => unsupported(this.deploymentReason(value))).join("")}`;
    host.querySelector("#deployment-refresh").onclick = () => this.loadSpeakers();
    if (device) {
      host.querySelector("#deployment-device").onchange = event => { this.selectedSpeaker = event.target.value; this.deploymentRender = null; this.paintSpeakers(); };
      host.querySelector("#deployment-probe").onclick = () => this.deploymentAction("probe");
      if (host.querySelector("#deployment-reconcile")) host.querySelector("#deployment-reconcile").onclick = () => this.deploymentAction("reconcile");
      host.querySelectorAll("[data-deployment-prepare]").forEach(button => { button.onclick = () => this.deploymentAction("prepare", button.dataset.deploymentPrepare); });
      if (review) {
        const confirm = host.querySelector("#deployment-confirm"), submit = host.querySelector("#deployment-execute");
        confirm.onchange = () => { submit.disabled = !confirm.checked || !available(operation.action) || !this.deploymentReviewReady(device); };
        host.querySelector("#deployment-review").onsubmit = event => { event.preventDefault(); if (confirm.checked) return this.deploymentAction("execute", operation.action, operation.operation_id); };
      }
    }
    const focus = activeId ? host.querySelector(`#${activeId}`) : null;
    if (focus && !focus.disabled) focus.focus({ preventScroll: true });
  }
  async deploymentAction(kind, action, operationId) {
    const device = this.deployment?.devices.find(value => value.device_id === this.selectedSpeaker);
    if (!this.snapshot?.is_admin || this.page !== "Speakers" || this.deploymentBusy || !device) return;
    if (!["probe", "prepare", "execute", "reconcile"].includes(kind) || device.capabilities[kind === "execute" ? action : kind] !== true) return;
    if (["prepare", "execute"].includes(kind) && !["install", "rollback_previous", "restore_stock", "remove"].includes(action)) return;
    if (kind === "execute" && (device.operation?.stage !== "REVIEW_REQUIRED" || device.operation.review_required !== true || device.operation.action !== action || device.operation.operation_id !== operationId || !this.deploymentReviewReady(device) || !this.content("Speakers").querySelector("#deployment-confirm")?.checked)) return;
    const epoch = this.epoch, request = this.deploymentRequest = Symbol(), revision = this.deployment.revision;
    const payload = { device_id: device.device_id, expected_deployment_revision: revision, ...(["prepare", "execute"].includes(kind) ? { action } : {}), ...(kind === "execute" ? { operation_id: operationId } : {}) };
    this.deploymentLoading = false; this.deploymentBusy = true; this.paintSpeakers();
    try {
      const result = await this.client.request(`deployment_${kind}`, payload);
      if (!this.deploymentCurrent(epoch, request)) return;
      this.deploymentBusy = false; this.acceptDeployment(result);
      this.setStatus(kind === "probe" ? "Speaker check returned. Review the observed status." : kind === "prepare" ? "Preparation returned. Review the action and any requirements below." : "Operation status received. Review its observed result below.");
    } catch (error) {
      if (this.deploymentCurrent(epoch, request)) { this.deploymentFailure(error); if(this.snapshot?.is_admin)this.setStatus("The operation result was not confirmed. Reload status to recover its stored result before another action."); }
    } finally { this.deploymentBusy = false; if (this.page === "Speakers" && this.snapshot?.is_admin && this.deployment) this.paintSpeakers(); }
  }
  paintRooms(config) {
    if(!this.snapshot?.is_admin){this.content("Rooms").innerHTML=unsupported("Home Assistant administrator access is required for these controls.");return;}
    this.endpointProfiles=null;
    const selected = this.editRoomId || config.rooms[0]?.room_id, room = config.rooms.find(value => value.room_id === selected);
    this.editRoomId = room?.room_id;
    this.content().innerHTML = `<h2>Rooms and groups</h2><p>Only enrolled endpoint profiles can be selected. Device addresses and private credentials stay on Home Assistant.</p><label for="room-edit">Room to configure</label><select id="room-edit">${config.rooms.map(value => option(value.room_id, value.name, room?.room_id)).join("")}</select>${room ? `<form id="room-form"><h3>${esc(room.name)}</h3>${field("name", "Room name", room.name, "text", 'required maxlength="64"')}<label for="room_backend">Endpoint type</label><select id="room_backend">${option("simulation","Simulation (no hardware)",room.backend||"simulation")}${option("real_endpoint","Enrolled physical endpoint profile",room.backend)}</select><label for="room_endpoint">Speaker profile</label><select id="room_endpoint" disabled>${option(room.endpoint_ref||"",room.endpoint_ref||"Choose a stored speaker",room.endpoint_ref||"")}</select><small id="room-endpoint-status" role="status">Loading current speaker choices. Changing endpoint type preserves other room settings and may end affected calls.</small>${field("area_id", "Home Assistant area ID (optional)", room.area_id || "")}${field("aliases", "Aliases (comma separated)", room.aliases.join(", "))}${check("dnd", "Do not disturb", room.dnd)}${check("quiet_enabled", "Enable quiet hours", room.quiet_hours.enabled)}<div class="row">${field("quiet_start", "Quiet hours start", room.quiet_hours.start, "time", "required")}${field("quiet_end", "Quiet hours end", room.quiet_hours.end, "time", "required")}</div>${field("quiet_days", "Quiet days (0 Monday – 6 Sunday, comma separated)", room.quiet_hours.days.join(","))}<label for="favorite">Physical favorite destination</label><select id="favorite">${option("", "None — stock idle button action", room.favorite_room_id || "")}${config.rooms.filter(value => value.room_id !== room.room_id).map(value => option(value.room_id, value.name, room.favorite_room_id)).join("")}</select><small>Favorite is a stored default for a supporting physical-button profile. The current legacy profile retains its stock idle behavior.</small><details><summary>Directional permissions and local identity</summary>${check("destinations_default", "Allow destinations among all enrolled rooms by default", room.allowed_destination_ids == null)}<label for="allowed_destinations">Explicit allowed destinations</label><select id="allowed_destinations" multiple size="4" ${room.allowed_destination_ids == null ? "disabled" : ""}>${config.rooms.filter(value => value.room_id !== room.room_id).map(value => `<option value="${esc(value.room_id)}" ${(room.allowed_destination_ids || []).includes(value.room_id) ? "selected" : ""}>${esc(value.name)}</option>`).join("")}</select><small>Turn off the default and select no rooms to deny every outgoing destination. HA user grants are required independently. Duplex calls must be permitted in both directions.</small>${field("local_user_id", "Local Home Assistant user ID (optional)", room.local_user_id || "", "text", 'maxlength="128"')}${field("ha_device_id", "Home Assistant device ID (optional)", room.ha_device_id || "", "text", 'maxlength="128"')}${field("assist_pipeline_id", "Local Assist pipeline ID (optional)", room.assist_pipeline_id || "", "text", 'maxlength="128"')}${field("stt_entity_id", "Local STT entity ID (optional)", room.stt_entity_id || "", "text", 'maxlength="128"')}<small>These explicit bindings identify the resident and local pipeline; they do not grant access or authenticate an endpoint by themselves. Local Assist requires a supporting authenticated runtime.</small></details><details><summary>Volume and capabilities</summary>${unsupported(room.volumes.calibrated ? "Stored calibration is present. Applied output levels require a supporting endpoint profile. Requested settings below are separate from applied state." : "Volume is not calibrated. Stored values do not prove an applied or safe output level.")}${["initial", "maximum", "chime", "announcement"].map(key => field(`volume_${key}`, `${key[0].toUpperCase()}${key.slice(1)} volume (0–100, blank = uncalibrated)`, room.volumes[key] ?? "", "number", `min="5" max="100" step="1" ${this.snapshot?.rooms.find(value=>value.room_id===room.room_id)?.volume?.supported ? "" : "disabled"}`)).join("")}${check("volume_calibrated", "Room volume calibration completed", room.volumes.calibrated)}<p id="applied-volume">Applied output: ${esc(this.appliedVolume(room.room_id))}. Stored requests are not evidence of an applied or acoustically safe level.</p></details><div class="actions"><button type="submit" class="primary">Save room settings</button><button type="button" id="remove-room" class="danger">Remove room</button></div></form>` : '<p class="notice">No rooms are enrolled. Configure a room below; saved endpoint profiles remain on Home Assistant.</p>'}<details><summary>Enroll a room</summary><form id="add-room">${field("new_room_id", "Stable room UUID", "", "text", "required")}${field("new_name", "Room name", "", "text", 'required maxlength="64"')}<label for="new_backend">Endpoint type</label><select id="new_backend"><option value="simulation">Simulation (no hardware)</option><option value="real_endpoint">Enrolled physical endpoint profile</option></select>${field("new_endpoint", "Protected endpoint profile reference (physical only)")}<small>Use a provisioned server-side profile reference. Adding a room does not install firmware or trust a discovered device.</small><button type="submit">Enroll room</button></form></details><h3>Groups</h3><form id="groups-form"><label for="groups-json">Groups (JSON array: group_id, name, room_ids)</label><textarea id="groups-json" spellcheck="false">${esc(JSON.stringify(config.groups, null, 2))}</textarea><button type="submit">Save groups</button></form><h3>Speakers</h3><p>Removing a room keeps its speaker profile. Remove speaker deletes the Home Assistant profile and every room using it, removes their HA call access and ends affected calls. Device trust and external credential files remain unchanged.</p><button id="load-endpoints" type="button">Refresh speakers</button><div id="endpoint-profiles"></div><div id="endpoint-enrollment"></div>`;
    this.content().querySelector("#room-edit").onchange = event => { this.editRoomId = event.target.value; this.paintRooms(config); };
    const get = id => this.content().querySelector(`#${id}`);
    if (room) {
      get("room_backend").onchange=()=>{get("room_endpoint").disabled=get("room_backend").value!=="real_endpoint"||!this.endpointProfiles;};
      get("destinations_default").onchange = event => { get("allowed_destinations").disabled = event.target.checked; };
      get("volume_calibrated").disabled = !this.snapshot?.rooms.find(value=>value.room_id===room.room_id)?.volume?.supported;
      get("room-form").onsubmit = event => { event.preventDefault(); const next = clone(config), edited = next.rooms.find(value => value.room_id === room.room_id); Object.assign(edited, { name: get("name").value, area_id: get("area_id").value || null, aliases: get("aliases").value.split(",").map(value => value.trim()).filter(Boolean), dnd: get("dnd").checked, favorite_room_id: get("favorite").value || null, quiet_hours: { enabled: get("quiet_enabled").checked, start: get("quiet_start").value, end: get("quiet_end").value, days: get("quiet_days").value.split(",").filter(value => value.trim()).map(Number) } }); Object.assign(edited, {allowed_destination_ids: get("destinations_default").checked ? null : [...get("allowed_destinations").selectedOptions].map(value => value.value)}); for(const key of ["local_user_id", "ha_device_id", "assist_pipeline_id", "stt_entity_id"]) edited[key] = get(key).value.trim() || null; edited.volumes.calibrated = get("volume_calibrated").checked; for (const key of ["initial", "maximum", "chime", "announcement"]) edited.volumes[key] = get(`volume_${key}`).value === "" ? null : Number(get(`volume_${key}`).value); if(!this.applyRoomEndpoint(edited,config))return; this.saveConfiguration(next, "Room settings saved"); };
      get("remove-room").onclick = () => { const next = clone(config); next.rooms = next.rooms.filter(value => value.room_id !== room.room_id); for (const value of next.rooms) { if (value.favorite_room_id === room.room_id) value.favorite_room_id = null; if (Array.isArray(value.allowed_destination_ids)) value.allowed_destination_ids = value.allowed_destination_ids.filter(id => id !== room.room_id); } next.groups = next.groups.map(group => ({ ...group, room_ids: group.room_ids.filter(id => id !== room.room_id) })).filter(group => group.room_ids.length); for (const grant of next.permissions) grant.room_ids = grant.room_ids.filter(id => id !== room.room_id); if(Array.isArray(next.settings.automation_bindings))next.settings.automation_bindings=next.settings.automation_bindings.map(binding=>({...binding,room_ids:binding.room_ids.filter(id=>id!==room.room_id)})).filter(binding=>binding.room_ids.length); this.saveConfiguration(next, "Room removed. Its endpoint profile remains stored on Home Assistant."); };
    }
    get("add-room").onsubmit = event => { event.preventDefault(); const next = clone(config); next.rooms.push({ room_id: get("new_room_id").value, name: get("new_name").value, area_id: null, aliases: [], backend: get("new_backend").value, endpoint_ref: get("new_endpoint").value || null, dnd: false, quiet_hours: { enabled: false, days: [0, 1, 2, 3, 4, 5, 6], start: "22:00", end: "07:00" }, favorite_room_id: null, allowed_destination_ids: null, local_user_id: null, ha_device_id: null, assist_pipeline_id: null, stt_entity_id: null, volumes: { initial: null, maximum: null, chime: null, announcement: null, calibrated: false } }); this.saveConfiguration(next, "Room enrolled"); };
    get("load-endpoints").onclick = () => this.loadEndpointProfiles(config);
    get("groups-form").onsubmit = event => { event.preventDefault(); try { const next = clone(config); next.groups = JSON.parse(get("groups-json").value); if (!Array.isArray(next.groups)) throw new Error(); this.saveConfiguration(next, "Groups saved"); } catch { this.setStatus("Groups must be a JSON array. No change was submitted."); } };
    return this.loadEndpointProfiles(config);
  }
  async loadEndpointProfiles(config) {
    const host=this.content("Rooms").querySelector("#endpoint-profiles"), epoch=this.epoch, revision=this.snapshot?.revision,view=this.viewRequest;
    if(!host||!this.snapshot?.is_admin)return;
    const request=this.profileRequest=Symbol();
    const current=()=>epoch===this.epoch&&view===this.viewRequest&&request===this.profileRequest&&this.page==="Rooms"&&this.snapshot?.is_admin&&this.content("Rooms").querySelector("#endpoint-profiles")===host;
    this.endpointProfiles=null;const selection=this.content("Rooms").querySelector("#room_endpoint");if(selection)selection.disabled=true;
    host.innerHTML=unsupported("Loading stored speakers…");
    try {
      const result=await this.client.request("list_endpoint_profiles");if(!current())return;
      if(!Array.isArray(result?.endpoint_profiles)||result.endpoint_profiles.some(profile=>typeof profile.endpoint_ref!=="string"||typeof profile.device_uuid!=="string"||![3,4].includes(profile.profile)))throw new Error("INVALID_RESPONSE");
      const profiles=result.endpoint_profiles;
      if(revision!==this.snapshot.revision||config.revision!==this.snapshot.revision)this.setStatus("Configuration changed. Reload Rooms before selecting speakers.");
      else{this.endpointProfiles=profiles;this.profileRevision=config.revision;this.paintEndpointChoices(config,profiles);this.paintEndpointEnrollment(config,profiles);}
      if(!profiles.length){host.innerHTML=unsupported("No speaker profiles are stored. You can configure rooms above using a provisioned profile.");return;}
      host.innerHTML=`<form id="remove-speaker-form"><label for="speaker-ref">Stored speaker profile</label><select id="speaker-ref">${profiles.map(profile=>option(profile.endpoint_ref,`${profile.endpoint_ref} · ${config.rooms.filter(room=>room.endpoint_ref===profile.endpoint_ref).map(room=>room.name).join(", ")||"No room assigned"}`,"")).join("")}</select><p id="speaker-impact"></p>${check("speaker-confirm","I want to remove this speaker profile and all its rooms from Home Assistant.",false)}<button id="remove-speaker" type="submit" class="danger" disabled>Remove speaker</button></form>`;
      const get=id=>host.querySelector(`#${id}`), impact=()=>{const rooms=config.rooms.filter(room=>room.endpoint_ref===get("speaker-ref").value);get("speaker-impact").textContent=rooms.length?`Rooms to remove: ${rooms.map(room=>room.name).join(", ")}. Their active calls will end.`:"This stored profile has no room. Removing it removes its HA enrollment.";get("speaker-confirm").checked=false;get("remove-speaker").disabled=true;};
      get("speaker-ref").onchange=impact;impact();
      get("speaker-confirm").onchange=()=>{get("remove-speaker").disabled=!get("speaker-confirm").checked;};
      get("remove-speaker-form").onsubmit=event=>{event.preventDefault();if(get("speaker-confirm").checked&&profiles.some(profile=>profile.endpoint_ref===get("speaker-ref").value))return this.removeSpeaker(config,get("speaker-ref").value,host);};
    } catch(error) {if(current()){const reason=managerError(error);if(["NOT_AUTHORIZED","HA_AUTH_REQUIRED"].includes(reason)){this.snapshot=null;this.configuration=null;this.clearPrivateScreens();this.setStatus("Administrator access was lost. Reconnect to reload authorized controls.");}else host.innerHTML=unsupported(`Speaker list unavailable: ${reason}. Use Refresh speakers to retry.`);}}
  }
  paintEndpointChoices(config,profiles){
    const content=this.content("Rooms"), select=content.querySelector("#room_endpoint");if(!select)return;
    const selected=select.value, present=profiles.some(profile=>profile.endpoint_ref===selected);
    select.innerHTML=option("","Choose a stored speaker",selected)+(!present&&selected?option(selected,`${selected} (unavailable — refresh or choose another)`,selected):"")+profiles.map(profile=>option(profile.endpoint_ref,`${profile.endpoint_ref} · profile ${profile.profile}`,selected)).join("");
    select.value=selected;select.disabled=content.querySelector("#room_backend").value!=="real_endpoint";
    const status=content.querySelector("#room-endpoint-status");if(status)status.textContent="Choose an enrolled profile. Switching endpoint type preserves room settings; simulation cannot use calibrated volume settings.";
  }
  applyRoomEndpoint(edited,config){
    if(!this.snapshot?.is_admin||this.page!=="Rooms")return false;
    const get=id=>this.content("Rooms").querySelector(`#${id}`), backend=get("room_backend").value||edited.backend||"simulation", ref=get("room_endpoint").value;
    if(config.revision!==this.snapshot.revision){this.showConfigurationConflict("Rooms");this.setStatus("Configuration changed. Your room edits remain; discard and reload before saving.");return false;}
    if(backend==="real_endpoint"){
      const profile=this.endpointProfiles?.find(profile=>profile.endpoint_ref===ref);
      if(!profile||this.profileRevision!==config.revision){this.setStatus("Choose a current enrolled speaker profile. Refresh speakers or reload Rooms; your edits remain.");return false;}
      if(config.rooms.some(room=>room.room_id!==edited.room_id&&room.backend==="real_endpoint"&&room.endpoint_ref===ref)){this.setStatus("This speaker is assigned to another room. Choose an unassigned speaker; your edits remain.");return false;}
      if(profile.profile!==4&&Object.entries(edited.volumes).some(([key,value])=>key==="calibrated"?value!==false:value!==null)){this.setStatus("Stored volume settings require profile 4. Choose a compatible profile; no volume values were reset.");return false;}
    }else if(backend!=="simulation"){this.setStatus("Choose a supported endpoint type.");return false;}
    else if(Object.entries(edited.volumes).some(([key,value])=>key==="calibrated"?value!==false:value!==null)){this.setStatus("Simulation does not support stored volume settings. Keep the physical endpoint or explicitly clear calibration and volumes on a supporting physical profile first. No volume values were reset.");return false;}
    edited.backend=backend;edited.endpoint_ref=backend==="real_endpoint"?ref:null;return true;
  }
  paintEndpointEnrollment(config,profiles){
    const host=this.content("Rooms").querySelector("#endpoint-enrollment");if(!host||!this.snapshot?.is_admin)return;
    if(host.querySelector("#enroll-speaker-form")){
      const select=host.querySelector("#enroll-ref"), selected=select.value;
      select.innerHTML=option("","Enroll a new speaker",selected)+(!profiles.some(profile=>profile.endpoint_ref===selected)&&selected?option(selected,`${selected} (unavailable)`,selected):"")+profiles.map(profile=>option(profile.endpoint_ref,`Replace ${profile.endpoint_ref}`,selected)).join("");select.value=selected;return;
    }
    host.innerHTML=`<details><summary>Enroll or replace a native speaker profile</summary><p>Configure an already provisioned prototype endpoint. This stores HA connection settings; it does not install firmware, pair a device, upload trust files or establish whole-device security. Replacing a profile ends affected calls.</p><form id="enroll-speaker-form"><label for="enroll-ref">Operation</label><select id="enroll-ref">${option("","Enroll a new speaker","")}${profiles.map(profile=>option(profile.endpoint_ref,`Replace ${profile.endpoint_ref}`,"")).join("")}</select>${field("enroll-uuid","Expected endpoint UUID","","text",'required autocomplete="off"')}${field("enroll-host","Endpoint host or address","","text",'required autocomplete="off"')}${field("enroll-port","Endpoint port",6054,"number",'required min="1" max="65535" step="1"')}<label for="enroll-profile">Native protocol profile</label><select id="enroll-profile"><option value="3">3 — ICM3</option><option value="4">4 — ICM4</option></select>${field("enroll-pin","Endpoint SPKI SHA-256 (64 hexadecimal characters)","","text",'required minlength="64" maxlength="64" pattern="[0-9a-fA-F]{64}" autocomplete="off"')}<details open><summary>Advanced TLS files on the HA server</summary><small>Enter existing server-local paths. The browser cannot verify file existence. Never paste a private key or certificate contents here. Replacement requires every connection field because stored addresses, pins and paths are not disclosed by this API.</small>${field("enroll-ca","CA certificate path","","text",'required autocomplete="off"')}${field("enroll-cert","HA client certificate path","","text",'required autocomplete="off"')}${field("enroll-key","HA client private-key path","","text",'required autocomplete="off"')}</details><p id="enroll-error" role="alert"></p><button type="submit" class="primary">Save speaker profile</button></form></details>`;
    const get=id=>host.querySelector(`#${id}`);
    get("enroll-ref").onchange=()=>{const profile=this.endpointProfiles?.find(profile=>profile.endpoint_ref===get("enroll-ref").value);get("enroll-uuid").value=profile?.device_uuid||"";get("enroll-uuid").readOnly=Boolean(profile);get("enroll-profile").value=String(profile?.profile||3);get("enroll-error").textContent="";};
    get("enroll-speaker-form").onsubmit=event=>{event.preventDefault();return this.enrollSpeaker(config,host);};
  }
  async enrollSpeaker(config,host){
    const content=this.content("Rooms"),epoch=this.epoch,navigation=this.navigationRequest;
    // A subscribed configuration update may arrive before the enrollment receipt.
    // Navigation invalidates completion independently of that revision update.
    const current=()=>epoch===this.epoch&&navigation===this.navigationRequest&&this.page==="Rooms"&&this.snapshot?.is_admin&&content.querySelector("#endpoint-enrollment")===host;
    if(!current()||this.saving)return;
    const get=id=>host.querySelector(`#${id}`), fail=message=>{get("enroll-error").textContent=message;};
    if(content.querySelector("#configuration-conflict"))return fail("Discard edits and reload before saving a speaker.");
    if(!this.endpointProfiles)return fail("Refresh speakers and wait for current profile choices before saving. Entered fields remain.");
    if(config.revision!==this.snapshot.revision||this.profileRevision!==config.revision){this.showConfigurationConflict("Rooms");return fail("Configuration changed. All entered fields remain; discard and reload before saving.");}
    const ref=get("enroll-ref").value, prior=this.endpointProfiles?.find(profile=>profile.endpoint_ref===ref), uuid=get("enroll-uuid").value.trim().toLowerCase();
    if(ref&&!prior)return fail("This speaker profile is no longer available. Refresh speakers and review your selection.");
    if(!/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(uuid))return fail("Enter a valid endpoint UUID.");
    if(prior&&prior.device_uuid!==uuid)return fail("Replacement must keep the existing endpoint UUID.");
    if(this.endpointProfiles?.some(profile=>profile.endpoint_ref!==ref&&profile.device_uuid===uuid))return fail("This endpoint UUID is already enrolled. Select its existing profile to replace it.");
    const endpoint={endpoint_host:get("enroll-host").value.trim(),endpoint_port:Number(get("enroll-port").value),expected_endpoint_uuid:uuid,expected_endpoint_uri:`urn:intercom:endpoint:${uuid}`,expected_endpoint_spki_sha256:get("enroll-pin").value.trim().toLowerCase(),endpoint_ca_path:get("enroll-ca").value.trim(),endpoint_client_cert_path:get("enroll-cert").value.trim(),endpoint_client_key_path:get("enroll-key").value.trim(),endpoint_profile:Number(get("enroll-profile").value)};
    if(!endpoint.endpoint_host||!Number.isInteger(endpoint.endpoint_port)||endpoint.endpoint_port<1||endpoint.endpoint_port>65535||![3,4].includes(endpoint.endpoint_profile)||!/^[0-9a-f]{64}$/.test(endpoint.expected_endpoint_spki_sha256)||![endpoint.endpoint_ca_path,endpoint.endpoint_client_cert_path,endpoint.endpoint_client_key_path].every(Boolean))return fail("Enter host, port 1–65535, profile 3 or 4, a 64-character hexadecimal SPKI pin and every existing HA TLS file path.");
    if(ref&&endpoint.endpoint_profile!==4&&config.rooms.some(room=>room.endpoint_ref===ref&&Object.entries(room.volumes).some(([key,value])=>key==="calibrated"?value!==false:value!==null)))return fail("This speaker has stored room volume settings that require profile 4. Keep profile 4 or explicitly clear those settings before replacing it. No volume values were reset.");
    this.saving=true;content.querySelectorAll("button[type=submit]").forEach(button=>button.disabled=true);fail("");let accepted=false;
    try{
      const receipt=await this.client.request("enroll_endpoint",{expected_revision:config.revision,endpoint,...(ref?{endpoint_ref:ref}:{})});if(!current())return;
      if(!Number.isSafeInteger(receipt?.revision)||receipt.revision!==config.revision+1||typeof receipt.endpoint_ref!=="string"||!receipt.endpoint_ref||ref&&receipt.endpoint_ref!==ref)throw new Error("ENROLLMENT_NOT_CONFIRMED");
      accepted=true;
      const configuration=await this.client.configuration();if(!current())return;
      await this.refresh();
      if(current()){this.showConfigurationConflict("Rooms","Speaker profile saved. Reload the current configuration before editing rooms or speakers.");content.querySelectorAll("button[type=submit]").forEach(button=>button.disabled=true);this.setStatus("Speaker profile saved on Home Assistant. Your drafts remain; discard and reload to use the new revision. Connectivity and audible audio still require testing.");}
    }catch(error){
      if(!current())return;const reason=managerError(error);
      if(["NOT_AUTHORIZED","HA_AUTH_REQUIRED"].includes(reason)){this.configuration=null;this.snapshot=null;this.clearPrivateScreens();this.setStatus("Administrator access was lost. Reconnect to reload authorized controls.");return;}
      if(reason==="CONFIGURATION_CONFLICT"||accepted)this.showConfigurationConflict("Rooms",accepted?"Speaker save was accepted, but current configuration could not be loaded.":undefined);
      fail(accepted?`Speaker saved; refresh failed: ${reason}. Reload before continuing.`:`Speaker save not confirmed: ${reason}. Entered fields remain. Check existing server TLS files for INVALID_REQUEST; refresh and review before retrying.`);
    }finally{this.saving=false;if(current())content.querySelectorAll("button[type=submit]").forEach(button=>button.disabled=Boolean(content.querySelector("#configuration-conflict")));}
  }

  async removeSpeaker(config,endpointRef,host) {
    const content=this.content("Rooms"),epoch=this.epoch;
    const current=()=>epoch===this.epoch&&this.page==="Rooms"&&this.snapshot?.is_admin&&this.content("Rooms").querySelector("#endpoint-profiles")===host;
    if(!current()||this.saving)return;
    if(content.querySelector("#configuration-conflict")){this.setStatus("Discard edits and reload before removing a speaker.");return;}
    this.saving=true;content.querySelectorAll("button[type=submit]").forEach(button=>button.disabled=true);
    let receipt,removed=false;
    const summary=()=>{const cleanup=receipt.runtime_cleanup==="complete"?"Runtime cleanup complete.":`Runtime cleanup ${["pending","failed"].includes(receipt.runtime_cleanup)?receipt.runtime_cleanup:"unconfirmed"}; check Diagnostics while Home Assistant retries cleanup.`;return `Speaker removed from Home Assistant. ${cleanup} Device trust and external credential files were not erased.`;};
    try {
      receipt=await this.client.request("remove_endpoint",{expected_revision:config.revision,endpoint_ref:endpointRef});if(!current())return;
      if(receipt?.roster_removed!==true||receipt.endpoint_ref!==endpointRef||!Number.isSafeInteger(receipt.revision)||receipt.device_trust_reset!==false||receipt.external_credentials_erased!==false)throw new Error("REMOVAL_NOT_CONFIRMED");
      removed=true;
      const configuration=await this.client.configuration();if(!current())return;
      await this.refresh();if(!current())return;
      this.configuration=configuration;this.saving=false;const painting=this.paintRooms(configuration), refreshedHost=this.content("Rooms").querySelector("#endpoint-profiles");await painting;
      if(epoch!==this.epoch||this.page!=="Rooms"||!this.snapshot?.is_admin||this.content("Rooms").querySelector("#endpoint-profiles")!==refreshedHost)return;
      this.setStatus(summary());
    } catch(error) {
      if(!current())return;
      const reason=managerError(error);await this.refresh();if(!current())return;
      this.saving=false;await this.loadEndpointProfiles(config);if(!current())return;
      if(removed)this.showConfigurationConflict("Rooms","Speaker removal was accepted, but the current configuration could not be loaded.");
      else if(reason==="CONFIGURATION_CONFLICT")this.showConfigurationConflict("Rooms");
      this.setStatus(removed?`${summary()} Refresh failed: ${reason}. Discard edits and reload before continuing.`:`Removal not confirmed: ${reason}. Review the current speaker list and reload Rooms before another attempt.`);
    } finally {this.saving=false;content.querySelectorAll("button[type=submit]").forEach(button=>button.disabled=Boolean(content.querySelector("#configuration-conflict")));const remove=content.querySelector("#remove-speaker"),confirm=content.querySelector("#speaker-confirm");if(remove&&confirm)remove.disabled=!confirm.checked||Boolean(content.querySelector("#configuration-conflict"));}
  }
  appliedVolume(roomId){
    const volume=this.snapshot?.rooms.find(room=>room.room_id===roomId)?.volume, applied=volume?.applied;
    if(!volume?.supported)return "Unsupported or no fresh endpoint capability";
    if(!applied)return "Unknown — no current acknowledgement";
    return `level ${applied.level ?? "unknown"}, maximum ${applied.maximum ?? "unknown"}, chime ${applied.chime ?? "unknown"}, revision ${applied.revision ?? "unknown"}`;
  }
  paintRoomVolumeStatus(){
    const content=this.content("Rooms"), output=content.querySelector("#applied-volume");if(!output)return;
    output.textContent=`Applied output: ${this.appliedVolume(this.editRoomId)}. Stored requests are not evidence of an applied or acoustically safe level.`;
    const supported=this.snapshot?.rooms.find(room=>room.room_id===this.editRoomId)?.volume?.supported===true;
    for(const key of ["initial","maximum","chime","announcement","calibrated"]){const input=content.querySelector(`#volume_${key}`);if(input)input.disabled=!supported;}
  }
  paintPeople(config) {
    this.content().innerHTML = `<h2>People and permissions</h2><p>Use existing Home Assistant user IDs. Administrator rights come from Home Assistant. Empty room lists grant no room access.</p><div class="grid">${config.permissions.map((grant, index) => `<article class="card"><h3>${esc(grant.user_id)}</h3><p>${esc(grant.room_ids.map(id => config.rooms.find(room => room.room_id === id)?.name || id).join(", ") || "No rooms")}</p><p>${esc(grant.actions.join(", ") || "No actions")}</p><button data-edit-grant="${index}">Edit grant</button> <button data-remove-grant="${index}" class="danger">Remove grant</button></article>`).join("")}</div><form id="grant-form"><h3>Add or update a person's access</h3>${field("grant-user", "Home Assistant user ID", "", "text", 'required maxlength="128"')}<label for="grant-rooms">Permitted rooms</label><select id="grant-rooms" multiple size="4">${config.rooms.map(room => option(room.room_id, room.name, "")).join("")}</select>${check("grant-call", "May call these rooms", false)}${check("grant-history", "May read history for these rooms", false)}${check("grant-announce", "May announce to these rooms", false)}<button type="submit" class="primary">Save person's access</button></form><details><summary>Advanced permission configuration</summary><form id="permissions-form"><label for="permissions-json">Room grants (JSON array: user_id, room_ids, actions)</label><textarea id="permissions-json" rows="14" spellcheck="false">${esc(JSON.stringify(config.permissions, null, 2))}</textarea><button type="submit">Save permissions JSON</button></form></details>`;
    const get = id => this.content().querySelector(`#${id}`);
    this.content().querySelectorAll("[data-edit-grant]").forEach(button => { button.onclick = () => { const grant = config.permissions[Number(button.dataset.editGrant)]; get("grant-user").value = grant.user_id; for (const option of get("grant-rooms").options) option.selected = grant.room_ids.includes(option.value); for (const action of ["call", "history", "announce"]) get(`grant-${action}`).checked = grant.actions.includes(action); get("grant-user").focus(); }; });
    this.content().querySelectorAll("[data-remove-grant]").forEach(button => { button.onclick = () => { const next = clone(config); next.permissions.splice(Number(button.dataset.removeGrant), 1); this.saveConfiguration(next, "Person's access removed"); }; });
    get("grant-form").onsubmit = event => { event.preventDefault(); const next = clone(config), user = get("grant-user").value.trim(), grant = { user_id: user, room_ids: [...get("grant-rooms").selectedOptions].map(option => option.value), actions: ["call", "history", "announce"].filter(action => get(`grant-${action}`).checked) }; const index = next.permissions.findIndex(value => value.user_id === user); if (index < 0) next.permissions.push(grant); else next.permissions[index] = grant; this.saveConfiguration(next, "Person's access saved"); };
    this.content().querySelector("#permissions-form").onsubmit = event => { event.preventDefault(); try { const next = clone(config); next.permissions = JSON.parse(this.content().querySelector("#permissions-json").value); if (!Array.isArray(next.permissions)) throw new Error(); this.saveConfiguration(next, "Permissions saved"); } catch { this.setStatus("Permissions must be a JSON array. No change was submitted."); } };
  }
  paintSettings(config) {
    const settings = config.settings;
    this.content().innerHTML = `<h2>Household settings</h2><form id="settings-form">${check("silence_enabled", "Disconnect calls after speech silence", settings.silence_enabled)}${field("silence_seconds", "Silence duration (seconds)", settings.silence_seconds, "number", 'required min="5" max="3600" step="1"')}${check("phone_silence", "Apply silence policy to browser calls", settings.silence_call_types.includes("browser"))}${unsupported(this.snapshot?.capabilities?.silence_detection ? "Silence detection uses fresh microphone activity. Media health deadlines remain independent." : "Speech-silence detection is not available on the current endpoint runtime. These defaults are stored for supported room/conference calls; they do not lengthen media health deadlines.")}${check("history_enabled", "Keep metadata history", settings.history_enabled)}${field("history_days", "History retention (days)", settings.history_retention_days, "number", 'required min="1" max="365" step="1"')}<small>History contains call metadata only. Disabling history stops new records; use History to delete existing records explicitly.</small><button class="primary" type="submit">Save household settings</button></form><h3>Setup and removal</h3><p><a href="/config/integrations/integration/intercom_m3_poc">Open the Intercom integration</a></p><p>Remove the integration entry to disable calls and the sidebar panel. Remove dashboard custom resources/cards separately. Back up configuration and retain the approved previous package before updating. Open Speakers to check, prepare and review permanent speaker software. Device changes require their own explicit review.</p>`;
    this.content().querySelector("#settings-form").onsubmit = event => { event.preventDefault(); const get = id => this.content().querySelector(`#${id}`), next = clone(config); Object.assign(next.settings, { silence_enabled: get("silence_enabled").checked, silence_seconds: Number(get("silence_seconds").value), silence_call_types: get("phone_silence").checked ? ["room", "conference", "browser"] : ["room", "conference"], history_enabled: get("history_enabled").checked, history_retention_days: Number(get("history_days").value) }); this.saveConfiguration(next, "Household settings saved"); };
    this.paintAutomationBindings(config);
  }
  paintAutomationBindings(config){
    const bindings=config.settings.automation_bindings;
    if(!Array.isArray(bindings))return;
    const host=document.createElement("div");this.content().append(host);
    host.innerHTML=`<h3>Trusted automation bindings</h3><p>Bind an existing HA automation to an administrator user and explicit actions/rooms. Empty bindings grant nothing. Live microphones cannot be opened by automations.</p>${bindings.map((binding,index)=>`<article class="card"><h4>${esc(binding.automation_entity_id)}</h4><p>${esc(binding.actions.join(", "))} · ${esc(binding.room_ids.map(id=>config.rooms.find(room=>room.room_id===id)?.name||"Room").join(", "))}</p><button type="button" data-edit-binding="${index}">Edit binding</button> <button type="button" class="danger" data-remove-binding="${index}">Remove binding</button></article>`).join("")}<form id="binding-form">${field("binding-entity","HA automation entity ID","","text",'required pattern="automation\\.[a-z0-9_]+" maxlength="128"')}${field("binding-user","Bound Home Assistant administrator user ID","","text",'required maxlength="128"')}<label for="binding-rooms">Explicit permitted rooms</label><select id="binding-rooms" multiple size="4" required>${config.rooms.map(room=>option(room.room_id,room.name,"")).join("")}</select>${["call","announce","hangup"].map(action=>check(`binding-${action}`,`Allow ${action}`,false)).join("")}<button type="submit" class="primary">Save automation binding</button></form>`;
    const get=id=>host.querySelector(`#${id}`);
    host.querySelectorAll("[data-edit-binding]").forEach(button=>{button.onclick=()=>{const binding=bindings[Number(button.dataset.editBinding)];get("binding-entity").value=binding.automation_entity_id;get("binding-user").value=binding.user_id;for(const node of get("binding-rooms").options)node.selected=binding.room_ids.includes(node.value);for(const action of ["call","announce","hangup"])get(`binding-${action}`).checked=binding.actions.includes(action);get("binding-entity").focus();};});
    host.querySelectorAll("[data-remove-binding]").forEach(button=>{button.onclick=()=>{const next=clone(config);next.settings.automation_bindings.splice(Number(button.dataset.removeBinding),1);this.saveConfiguration(next,"Automation binding removed");};});
    get("binding-form").onsubmit=event=>{event.preventDefault();const binding={automation_entity_id:get("binding-entity").value.trim(),user_id:get("binding-user").value.trim(),room_ids:[...get("binding-rooms").selectedOptions].map(node=>node.value),actions:["call","announce","hangup"].filter(action=>get(`binding-${action}`).checked)};if(!binding.actions.length||!binding.room_ids.length){this.setStatus("Choose at least one room and action for the binding.");return;}const next=clone(config), index=next.settings.automation_bindings.findIndex(value=>value.automation_entity_id===binding.automation_entity_id);if(index<0)next.settings.automation_bindings.push(binding);else next.settings.automation_bindings[index]=binding;this.saveConfiguration(next,"Automation binding saved");};
  }

  async loadAnnouncementSources(){
    const request=this.viewRequest, epoch=this.epoch;
    if(!this.snapshot?.capabilities?.announcements)return;
    try{
      const sources=await this.client.request("list_announcement_sources");
      if(this.page!=="Announcements"||request!==this.viewRequest||epoch!==this.epoch)return;
      this.announcementSources=sources;this.paintAnnouncements();this.paintAnnouncementStatus();
    }catch(error){if(this.page==="Announcements"&&epoch===this.epoch)this.setStatus(`Announcement sources unavailable: ${managerError(error)}`);}
  }
  paintAnnouncements() {
    if(this.announcementPanel?.inflight||this.announcementPanel?.callId)return;
    const sources=this.announcementSources, capabilities=this.snapshot?.capabilities||{}, rooms=this.snapshot?.rooms||[];
    this.content("Announcements").innerHTML=`<h2>Announcements</h2><p>Send once to eligible rooms. Occupied, offline, DND and forbidden rooms return individual outcomes. Destination microphones stay closed.</p>${!capabilities.announcements?unsupported("Announcement delivery is unavailable on this coordinator."):!sources?unsupported("Load authorized announcement sources to continue."):""}<form id="announcement-form"><label for="announcement-group">Target group</label><select id="announcement-group">${option("","Choose rooms below","")}${(this.snapshot?.groups||[]).map(group=>option(group.group_id,group.name,"")).join("")}</select><label for="announcement-targets">Destination rooms</label><select id="announcement-targets" multiple size="4">${rooms.map(room=>option(room.room_id,room.name,"")).join("")}</select><label for="announcement-kind">Source</label><select id="announcement-kind"><option value="file">Approved file</option><option value="tts">Local text to speech</option>${sources?.live_supported&&capabilities.live_announcements?'<option value="live">Live browser microphone</option>':""}</select><div id="announcement-file-fields"><label for="announcement-file">Approved audio file</label><select id="announcement-file">${(sources?.files||[]).map(file=>option(file.file_id,file.name,"")).join("")}</select></div><div id="announcement-tts-fields" hidden><label for="announcement-engine">Local TTS engine</label><select id="announcement-engine">${(sources?.tts_engines||[]).map(engine=>option(engine.engine_id,engine.name,"")).join("")}</select><label for="announcement-message">Message (not retained in history)</label><textarea id="announcement-message" maxlength="1000"></textarea></div><div id="announcement-live-fields" hidden><p>Start opens your browser microphone for foreground delivery. Leaving this screen, hiding the page or losing control ends the live source.</p></div><div class="actions"><button id="announcement-start" type="submit" class="primary" ${sources&&capabilities.announcements&&rooms.length?"":"disabled"}>Start announcement</button><button id="announcement-reload" type="button">Reload sources</button></div></form><div id="announcement-media"></div><div id="announcement-status" role="status" aria-live="polite"></div>`;
    const get=id=>this.content("Announcements").querySelector(`#${id}`);
    this.announcementPanel=document.createElement("intercom-m3-poc-panel");this.announcementPanel.panel=this._panel;this.announcementPanel.setConfig({announcement_only:true,simulation:false,backend:"real_endpoint"});this.announcementPanel.hass=this._hass;
    this.announcementPanel.addEventListener("announcement-result",event=>{this.announcementResult=event.detail;this.paintAnnouncementStatus();});
    get("announcement-media").append(this.announcementPanel);
    const kindChanged=()=>{const kind=get("announcement-kind").value;for(const value of ["file","tts","live"])get(`announcement-${value}-fields`).hidden=kind!==value;};
    get("announcement-kind").onchange=kindChanged;kindChanged();
    get("announcement-group").onchange=event=>{get("announcement-targets").disabled=Boolean(event.target.value);};
    get("announcement-reload").onclick=()=>this.loadAnnouncementSources();
    get("announcement-form").onsubmit=async event=>{
      event.preventDefault();const button=get("announcement-start"), group=get("announcement-group").value, target_room_ids=[...get("announcement-targets").selectedOptions].map(node=>node.value), kind=get("announcement-kind").value;
      if(!this.snapshot?.capabilities?.announcements||!this.snapshot.rooms.length||target_room_ids.some(id=>!this.snapshot.rooms.some(room=>room.room_id===id))){this.setStatus("Reload authorized rooms and announcement targets before starting.");return;}
      if(!group&&!target_room_ids.length){this.setStatus("Select a group or at least one destination room.");return;}
      if(this.announcementPanel.inflight||this.announcementPanel.callId){this.setStatus("End the current live announcement first.");return;}
      const targets=group?{group_id:group}:{target_room_ids};
      const source=kind==="file"?{kind,file_id:get("announcement-file").value}:{kind,engine_id:get("announcement-engine").value,message:get("announcement-message").value};
      if(kind==="file"&&!source.file_id||kind==="tts"&&(!source.engine_id||!source.message.trim())){this.setStatus("Choose an available source and enter a message for local TTS.");return;}
      button.disabled=true;
      try{
        if(kind==="live")await this.announcementPanel.call(false,targets);
        else{this.announcementResult=await this.client.request("start_announcement",{...targets,source});this.paintAnnouncementStatus();this.setStatus("Announcement request accepted. Review each destination outcome; acceptance does not prove audibility.");}
        await this.refresh();
      }catch(error){this.setStatus(`Announcement refused: ${managerError(error)}`);}
      finally{button.disabled=false;}
    };
  }
  paintAnnouncementStatus(){
    const target=this.content("Announcements").querySelector("#announcement-status");if(!target)return;
    const calls=(this.snapshot?.calls||[]).filter(call=>call.type==="announcement"), results=this.announcementResult?.results||[], rooms=this.snapshot?.rooms||[];
    const focus=this.shadowRoot.activeElement?.dataset?.stopAnnouncement;
    target.innerHTML=`<h3>Delivery status</h3><p>Last request admission results are shown below. <span>Accepted reserves an eligible path; active reports worker/media progress. Neither is proof that speech was heard.</span></p>${results.length?`<ul>${results.map(result=>`<li>${esc(rooms.find(room=>room.room_id===result.room_id)?.name||"Room")}: ${esc(result.status)} · ${esc(result.reason)}</li>`).join("")}</ul>`:""}${calls.map(call=>`<article class="card"><p>${esc(call.state)} · ${esc(call.reason||"")}</p>${(call.results||[]).map(result=>`<p>${esc(rooms.find(room=>room.room_id===result.room_id)?.name||"Room")}: ${esc(result.status)} · ${esc(result.reason)}</p>`).join("")}${call.can_end?`<button type="button" class="danger" data-stop-announcement="${esc(call.call_id)}">Stop this announcement</button>`:""}</article>`).join("")||"<p>No active announcement visible to this account.</p>"}`;
    target.querySelectorAll("[data-stop-announcement]").forEach(button=>{button.onclick=async()=>{
      if(this.announcementPanel?.callId===button.dataset.stopAnnouncement)await this.announcementPanel.cleanup("USER_STOPPED_ANNOUNCEMENT",true);
      await this.callControl(button,"stop_announcement",{call_id:button.dataset.stopAnnouncement});
    };if(button.dataset.stopAnnouncement===focus)button.focus({preventScroll:true});});
  }

  async loadHistory(offset = 0) {
    const epoch=this.epoch, request=this.viewRequest, result = await this.client.request("history", { offset, limit: 50 }); if (this.page !== "History"||epoch!==this.epoch||request!==this.viewRequest||!this.snapshot) return;
    this.historyOffset = offset;
    this.content().innerHTML = `<h2>Call history</h2><p>Permission-filtered metadata only. No recordings or transcripts.</p><label for="history-filter">Filter this page by room, type or outcome</label><input id="history-filter" type="search"><div class="table"><table><thead><tr><th>Started</th><th>Rooms</th><th>Type</th><th>Duration</th><th>Outcome</th></tr></thead><tbody>${result.items.map(item => `<tr><td>${esc(Number.isFinite(item.started_at)?new Date(item.started_at*1000).toLocaleString():"Unknown")}</td><td>${esc((item.room_ids || []).map(id => this.snapshot?.rooms.find(room => room.room_id === id)?.name || id).join(", "))}</td><td>${esc(item.type)}</td><td>${item.timing !== "unknown" && Number.isFinite(item.duration_seconds) ? `${esc(item.duration_seconds)} s` : "Unknown"}</td><td>${esc(item.outcome)} ${esc(item.reason)}</td></tr>`).join("")}</tbody></table></div>${!result.items.length ? '<p class="empty">No visible history records.</p>' : ""}<div class="actions"><button id="history-prev" ${offset ? "" : "disabled"}>Previous</button><button id="history-next" ${result.next_offset == null ? "disabled" : ""}>Next</button><button id="history-refresh">Refresh history</button></div>${this.snapshot?.is_admin ? '<details><summary>Delete stored history</summary><p>This removes all stored call metadata. Active calls are unaffected.</p><label class="check"><input id="delete-confirm" type="checkbox"> I want to delete all stored history</label><button id="history-delete" class="danger" disabled>Delete all history</button></details>' : ""}`;
    const get = id => this.content().querySelector(`#${id}`);
    get("history-filter").oninput = event => { const query = event.target.value.toLocaleLowerCase(); this.content().querySelectorAll("tbody tr").forEach(row => { row.hidden = !row.textContent.toLocaleLowerCase().includes(query); }); };
    get("history-prev").onclick = () => this.historyAction(Math.max(0, offset - 50)); get("history-next").onclick = () => this.historyAction(result.next_offset); get("history-refresh").onclick = () => this.historyAction(offset);
    if (get("history-delete")) { get("delete-confirm").onchange = event => get("history-delete").disabled = !event.target.checked; get("history-delete").onclick = async () => { try { await this.client.request("delete_history", { expected_revision: this.snapshot.revision }); this.setStatus("Stored history deleted"); await this.loadHistory(0); } catch (error) { this.setStatus(`Delete failed: ${managerError(error)}`); } }; }
  }
  async historyAction(offset) { try { await this.loadHistory(offset); } catch (error) { this.setStatus(`History unavailable: ${managerError(error)}`); } }
  async loadDiagnostics() {
    if (!this.snapshot?.is_admin) { this.content().innerHTML = unsupported("Home Assistant administrator access is required for diagnostics."); return; }
    const epoch=this.epoch, request=this.viewRequest, result = await this.client.request("diagnostics"); if (this.page !== "Diagnostics"||epoch!==this.epoch||request!==this.viewRequest||!this.snapshot?.is_admin) return;
    this.content().innerHTML = `<h2>Diagnostics</h2><p>Bounded metadata and counters. Frame progress is not proof of audible speech, acoustic latency or AEC.</p><div class="actions"><button id="diagnostics-refresh">Refresh diagnostics</button><button id="diagnostics-copy">Copy metadata report</button></div><pre id="diagnostics-report">${esc(JSON.stringify(result, null, 2))}</pre>`;
    this.content().querySelector("#diagnostics-refresh").onclick = () => this.loadDiagnostics().catch(error => this.setStatus(`Diagnostics unavailable: ${managerError(error)}`));
    this.content().querySelector("#diagnostics-copy").onclick = async () => { try { await navigator.clipboard.writeText(JSON.stringify(result, null, 2)); this.setStatus("Metadata diagnostics copied"); } catch { this.setStatus("Clipboard unavailable. Select the metadata report to copy it."); } };
  }
}

if (!customElements.get("intercom-manager-panel")) customElements.define("intercom-manager-panel", IntercomManagerPanel);

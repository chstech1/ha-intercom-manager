import "./manager.js?v=0.4.1";
import { IntercomManagerClient, managerError, escapeHtml as esc, roomState } from "./client.js?v=0.4.1";

class IntercomRoomCard extends HTMLElement {
  constructor() { super(); this.attachShadow({ mode: "open" }); this.audio = document.createElement("intercom-m3-poc-panel"); this.shadowRoot.append(this.audio); }
  setConfig(config) {
    if (config.device_id != null && typeof config.device_id !== "string") throw new Error("device_id must be an enrolled device UUID");
    this.config = config; this.audio.setConfig(config);
  }
  set hass(value) { this.audio.hass = value; }
  getCardSize() { return 7; }
  static getStubConfig() { return { type: "custom:intercom-room-card" }; }
}

class IntercomOverviewCard extends HTMLElement {
  constructor() { super(); this.attachShadow({ mode: "open" }); this.epoch = 0; }
  setConfig(config) { this.config = config; this.paint(); }
  set hass(value) { const previous = this._hass?.connection, changed = value?.connection !== previous; if (changed && previous) { this.epoch++; this.request=Symbol(); clearInterval(this.timer); this.timer=null; if(this.onLost)previous.removeEventListener?.("disconnected",this.onLost); this.snapshot=null; this.error=null; this.paint(); } this._hass = value; this.client = new IntercomManagerClient(value); if (changed && this.isConnected) this.connect(); }
  connectedCallback() { this.connect(); }
  disconnectedCallback() { this.epoch++; clearInterval(this.timer); this.timer = null; if (this.connection && this.onLost) this.connection.removeEventListener?.("disconnected", this.onLost); this.snapshot = null; this.paint(); }
  async connect() {
    if (!this.client || !this.isConnected) return;
    const epoch = ++this.epoch; clearInterval(this.timer);
    if (this.connection && this.onLost) this.connection.removeEventListener?.("disconnected", this.onLost);
    this.connection = this._hass.connection; this.onLost = () => { this.request = Symbol(); this.snapshot = null; this.error = "HA_DISCONNECTED"; this.paint(); };
    this.connection?.addEventListener?.("disconnected", this.onLost);
    const update = async () => { const request = this.request = Symbol(); try { const snapshot = await this.client.snapshot(); if (epoch === this.epoch && request === this.request) { this.snapshot = snapshot; this.error = null; this.paint(); } } catch (error) { if (epoch === this.epoch && request === this.request) { this.snapshot = null; this.error = managerError(error); this.paint(); } } };
    await update(); if (epoch === this.epoch) this.timer = setInterval(() => { if (document.visibilityState === "visible") update(); }, 5000);
  }
  paint() {
    this.shadowRoot.innerHTML = `<style>:host{display:block;font:16px system-ui;color:var(--primary-text-color,#172536)}article{padding:20px;border-radius:12px;background:var(--card-background-color,#fff);border:1px solid var(--divider-color,#d6dde5)}h2{margin-top:0;font-size:1.3rem}li{padding:12px 0;border-bottom:1px solid var(--divider-color,#d6dde5);line-height:1.5}ul{list-style:none;padding:0}small{display:block;color:var(--secondary-text-color,#536779)}a{display:inline-block;padding:12px;color:var(--primary-color,#1769aa)}a:focus-visible{outline:3px solid currentColor}</style><article><h2>${esc(this.config?.title || "Intercom rooms")}</h2>${this.error ? `<p role="status">Room status unknown: ${esc(this.error)}</p>` : ""}<ul>${(this.snapshot?.rooms || []).map(room => `<li><strong>${esc(room.name || room.room_name)}</strong> · ${roomState(room)}<small>Microphone: ${room.muted === true ? "muted" : room.muted === false ? "on" : "unknown"} · ${esc(room.activity || "activity unknown")}</small></li>`).join("") || "<li>No room status available</li>"}</ul><a href="/intercom-m3-poc">Open Intercom Manager</a></article>`;
  }
  getCardSize() { return 3; }
  static getStubConfig() { return { type: "custom:intercom-overview-card" }; }
}

if (!customElements.get("intercom-room-card")) customElements.define("intercom-room-card", IntercomRoomCard);
if (!customElements.get("intercom-overview-card")) customElements.define("intercom-overview-card", IntercomOverviewCard);
window.customCards = window.customCards || [];
for (const [type, name, description] of [["intercom-room-card", "Intercom room call", "Authenticated browser call controls with microphone cleanup"], ["intercom-overview-card", "Intercom overview", "Permitted room availability and manager link"]]) {
  if (!window.customCards.some(card => card.type === type)) window.customCards.push({ type, name, description, preview: false });
}

// Mount only inside an existing authenticated HA custom panel/dashboard host.
// The caller retains its HA object; this helper never obtains/stores a token.
export function mountIntercomWidget(host, hass, config = {}) {
  const type = { call: "intercom-room-card", overview: "intercom-overview-card", manager: "intercom-manager-panel" }[config.view || "overview"];
  if (!type || !host?.append) throw new Error("INVALID_WIDGET_HOST");
  const widget = document.createElement(type); widget.setConfig?.(config); widget.hass = hass; host.append(widget);
  return { element: widget, updateHass(value) { widget.hass = value; }, remove() { widget.remove(); } };
}

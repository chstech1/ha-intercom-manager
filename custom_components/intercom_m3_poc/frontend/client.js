// The host supplies its authenticated Home Assistant frontend object. No token
// copies, URL credentials, browser storage, or separate authentication flow.
export class IntercomManagerClient {
  constructor(hass) { this.hass = hass; }
  request(operation, data = {}) {
    if (!/^[a-z_]+$/.test(operation)) return Promise.reject(new Error("INVALID_OPERATION"));
    const message = { ...data, type: `intercom_native/${operation}` };
    const request = this.hass?.callWS ? this.hass.callWS(message) : this.hass?.connection?.sendMessagePromise(message);
    if (!request) return Promise.reject(new Error("HA_AUTH_REQUIRED"));
    let timer;
    return Promise.race([request, new Promise((_, reject) => { timer = setTimeout(() => reject(new Error("CONTROL_TIMEOUT")), 10000); })]).finally(() => clearTimeout(timer));
  }
  subscribe(listener) {
    if (!this.hass?.connection?.subscribeMessage) return Promise.reject(new Error("SUBSCRIPTION_UNAVAILABLE"));
    return this.hass.connection.subscribeMessage(listener, { type: "intercom_native/subscribe_manager" });
  }
  snapshot() { return this.request("list_rooms"); }
  configuration() { return this.request("get_configuration"); }
  update(configuration) {
    const { revision, ...body } = configuration;
    if (!Number.isSafeInteger(revision)) return Promise.reject(new Error("REVISION_REQUIRED"));
    return this.request("update_configuration", { expected_revision: revision, configuration: body });
  }
}

export function managerError(error) {
  const reason = String(error?.code || error?.message || "REQUEST_FAILED").toUpperCase();
  return /^[A-Z][A-Z0-9_]{0,63}$/.test(reason) ? reason : "REQUEST_FAILED";
}

export function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, char => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[char]));
}

export function roomState(room) {
  if (room.available !== true) return room.available === false ? "Offline" : "Unknown";
  if (room.dnd) return "Do not disturb";
  return room.busy ? "Busy" : "Available";
}

import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';import vm from 'node:vm';import {TextEncoder} from 'node:util';
const ctx=vm.createContext({TextEncoder,HTMLElement:class {},setTimeout,clearTimeout,clearInterval});
vm.runInContext(readFileSync(new URL('../custom_components/intercom_m3_poc/frontend/panel.js',import.meta.url),'utf8'),ctx);
const P=ctx.IntercomM3FrontendTest.IntercomM3PocPanel;
function make(){const p=Object.create(P.prototype),nodes=new Map();p.shadowRoot={innerHTML:'',querySelector(s){if(!nodes.has(s))nodes.set(s,{});return nodes.get(s)}};Object.assign(p,{panelConfig:{simulation:false},metrics:{simulation:false,backend:'ACTIVE_FULL',callMode:'full_duplex',server:{}},callMode:'full_duplex',rooms:[{deviceId:'room',name:'Room'}],selectedDeviceId:'room',status:'Connected',callId:'call',ownerGeneration:'owner',inflight:true,attempt:1,softMuted:false,intervals:[],cleaning:false});return [p,nodes]}
const [p,nodes]=make();let starts=0;p.call=()=>{starts++};await p.cleanup('Call ended: CLOCK_INVALID',false);
assert.equal(starts,0,'cleanup must not create a new call');assert.equal(p.callId,null);assert.match(p.status,/audio was interrupted.*call ended/i);assert.match(p.shadowRoot.innerHTML,/RETRY CALL/);nodes.get('#call').onclick();assert.equal(starts,1,'retry requires explicit action');
const [q]=make();await q.cleanup('USER_HANGUP',false);assert.doesNotMatch(q.shadowRoot.innerHTML,/RETRY CALL/);assert.equal(q.retryOffered,false);
for(const reason of ['Call ended: ENDPOINT_CLOSED','Call ended: ENDPOINT_END_11','Call ended: ENDPOINT_BUSY_1','Call ended: MEDIA_TIMEOUT','Call ended: RECOVERY_TIMEOUT','Call ended: RECOVERY_NOT_READY','Media progress stopped.']){const [r]=make();await r.cleanup(reason,false);assert.equal(r.retryOffered,true,reason);assert.match(r.status,/call ended/i)}
for(const reason of ['Call ended: AUTH_REVOKED','Call ended: OWNER_REVOKED','Call ended: ENDPOINT_BUSY_2','Call ended: ENDPOINT_BUSY_3','USER_HANGUP']){const [r]=make();await r.cleanup(reason,false);assert.equal(r.retryOffered,false,reason)}
console.log('PASS: clock/transport/lease failure exposes explicit fresh-call retry; auth/owner revoke and hangup never auto retry');






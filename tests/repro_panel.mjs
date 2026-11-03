import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import {TextEncoder} from 'node:util';
let now=10000; const context=vm.createContext({TextEncoder,HTMLElement:class {},performance:{now:()=>now},setTimeout,clearTimeout});
vm.runInContext(readFileSync(new URL('../custom_components/intercom_m3_poc/frontend/panel.js',import.meta.url),'utf8'),context);
const P=context.IntercomM3FrontendTest.IntercomM3PocPanel;
function panel(){const p=Object.create(P.prototype),ended=[];Object.assign(p,{inflight:true,callId:'call',metrics:{backend:'ACTIVE_FULL',callMode:'full_duplex',server:{room_mic_muted:false},simulation:false},ctx:{state:'running'},clock:{fresh:()=>true},mediaStarted:1000,mediaWasActive:true,lastActiveMode:'full_duplex',fullModeStarted:2000,lastCapture:now,lastOutputProgress:now,lastPlayed:now,render(){},outputReady(){return true},cleanup(reason){ended.push(reason)}});return [p,ended]}
const [p,ended]=panel();p.updateConnected();
p.metrics.backend='ACTIVE_SPEAKER_ONLY';p.metrics.server.room_mic_muted=true;now+=5000;p.lastCapture=p.lastOutputProgress=now;p.updateConnected();
p.metrics.server.room_mic_muted=false;p.updateConnected();p.metrics.backend='CONNECTING';p.updateConnected();assert.deepEqual(ended,[],'old room playback must not end connecting one-way call');
p.metrics.backend='ACTIVE_FULL';p.updateConnected();assert.deepEqual(ended,[],'allow fresh playback to arrive after full state');assert.match(p.status,/Connecting/,'old room playback must not claim full connectivity');
now+=100;p.lastCapture=p.lastOutputProgress=p.lastPlayed=now;p.updateConnected();assert.match(p.status,/bidirectional media progressing/);
now+=2100;p.lastCapture=p.lastOutputProgress=now;p.updateConnected();assert.deepEqual(ended,['Media progress stopped.'],'actual full-mode playback stall must end call');
const [q,stalled]=panel();q.metrics.backend='ACTIVE_SPEAKER_ONLY';q.metrics.server.room_mic_muted=true;q.updateConnected();q.metrics.backend='CONNECTING';q.metrics.server.room_mic_muted=false;now+=2100;q.lastCapture=now;q.updateConnected();assert.deepEqual(stalled,['Media progress stopped.'],'local output stall must end connecting one-way call');
const [r,still]=panel();r.metrics.backend='CONNECTING';now+=2100;r.lastCapture=r.lastOutputProgress=now;r.updateConnected();assert.deepEqual(still,['Media progress stopped.'],'connecting after established full mode must not mask room playback stall');
console.log('PASS: full → muted one-way → connecting → fresh full playback, bounded full and local output stall checks');


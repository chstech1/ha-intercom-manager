import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import {TextEncoder} from 'node:util';
import {webcrypto} from 'node:crypto';

const frontend = new URL('../custom_components/intercom_m3_poc/frontend/', import.meta.url);
let now=10000, callback, processor;
class WorkletBase { constructor(){this.port={out:[],postMessage(m){this.out.push(m)}}} }
const wc=vm.createContext({AudioWorkletProcessor:WorkletBase,registerProcessor:(_,p)=>processor=p,sampleRate:48000,currentTime:10,currentFrame:480000,Int16Array,Float32Array,Math});
vm.runInContext(readFileSync(new URL('worklet.js',frontend),'utf8'),wc);
const pc=vm.createContext({TextEncoder,HTMLElement:class{},performance:{now:()=>now},setTimeout,clearTimeout,clearInterval,crypto:webcrypto,WebSocket:{OPEN:1},Int16Array,Uint8Array,ArrayBuffer,DataView});
vm.runInContext(readFileSync(new URL('panel.js',frontend),'utf8'),pc);
const {IntercomM3PocPanel:Panel,ClockRelation}=pc.IntercomM3FrontendTest;
const clock=new ClockRelation(()=>now);
const cframe=clock.ping('0123456789ABCDEF');
assert.equal(new DataView(cframe.buffer).getBigUint64(4,true),0x0123456789abcdefn);
assert.equal(clock.pending.get('81985529216486895'),10000000n);
assert.throws(()=>clock.ping('0123456789ABCDEF'),/CLOCK_INVALID/);

const worklet=new processor(), pendingToWorklet=[], sent=[], ended=[], p=Object.create(Panel.prototype);
Object.assign(p,{panelConfig:{simulation:false},callMode:'full_duplex',callId:'call',ownerGeneration:'owner',inflight:true,attempt:1,
 ctx:{state:'running',sampleRate:48000,currentTime:10,outputLatency:0,baseLatency:0},
 clock:{fresh:()=>true,ageUpper:()=>0,ping:value=>{assert.ok(['0123456789ABCDEF','2222222222222222'].includes(value));return new Uint8Array([5])}},peerPingSeen:true,
 ws:{readyState:1,bufferedAmount:0,send(data){sent.push(data)}}, node:{port:{postMessage(m){pendingToWorklet.push(m)}}},
 mediaStarted:1000,mediaWasActive:true,lastActiveMode:'full_duplex',fullModeStarted:2000,lastCapture:now,lastOutputProgress:now,lastPlayed:2000,
 playbackPending:[],playbackPortInflight:0,rxSequence:0,txSequence:0,softMuted:false,render(){},outputReady(){return true},
 cleanup(reason){ended.push(reason);this.inflight=false},_hass:{connection:{subscribeMessage:async cb=>{callback=cb;return ()=>{}},sendMessagePromise:()=>Promise.resolve({})}}});
p.metrics=p.emptyMetrics();p.metrics.backend='ACTIVE_FULL';p.metrics.server={room_mic_muted:false};
await p.subscribe(1);
const event=(reason,overrides={})=>callback({call_id:'call',owner_generation:'owner',mode_generation:0,state:'CONNECTING',mode:'full_duplex',reason,metrics:{room_mic_muted:false},...overrides});
const audio=(seq,marker)=>{const b=new ArrayBuffer(1042),v=new DataView(b);v.setUint32(0,seq,true);v.setBigUint64(4,1n,true);v.setUint16(16,512,true);for(let i=0;i<512;i++)v.setInt16(18+i*2,marker,true);return {type:3,view:v}};
const capture=()=>p.onWorklet({type:'capture',samples:new Int16Array(512),captureEpoch:p.captureEpoch??null,captureEndTime:p.ctx.currentTime-.002,outputEndTime:p.ctx.currentTime-.003});
function deliverWorklet(){while(pendingToWorklet.length)worklet.port.onmessage({data:pendingToWorklet.shift()});while(worklet.port.out.length)p.onWorklet(worklet.port.out.shift())}

// A frame already sent to the worklet is followed by clear in the same port.
p.onMedia(audio(0,123));assert.equal(pendingToWorklet[0].type,'play');
worklet.capture.push(99);worklet.history.fill(1);worklet.capturePhase=2;worklet.playbackLast=.8;worklet.playbackFiltered=.7;worklet.played=99;
event('RECOVERY_PAUSE_0123456789ABCDEF',{call_id:'another'});assert.equal(p.recovery,undefined);
event('RECOVERY_PAUSE_0123456789ABCDEF');assert.equal(p.recovery.stage,'clearing');assert.equal(p.playbackPending.length,0);
event('RECOVERY_INPUT_0123456789ABCDEF');assert.equal(p.recovery.stage,'clearing');
p.onWorklet({type:'clear-ack',challenge:'FFFFFFFFFFFFFFFF'});assert.equal(sent.length,0);
p.onMedia(audio(1,234));assert.equal(p.rxSequence,2);capture();assert.equal(sent.length,0);
deliverWorklet();assert.equal(worklet.capture.length,0);assert.equal(worklet.playback.length,0);assert.ok(worklet.history.every(v=>v===0));assert.equal(worklet.capturePhase,0);assert.equal(worklet.playbackLast,0);assert.equal(worklet.playbackFiltered,0);assert.equal(worklet.played,0);assert.equal(p.recovery.stage,'fenced');assert.equal(sent.length,1);assert.equal(sent[0][0],5);
p.onWorklet({type:'clear-ack',challenge:'0123456789ABCDEF'});assert.equal(sent.length,1);
event('RECOVERY_INPUT_FFFFFFFFFFFFFFFF');assert.equal(p.recovery.stage,'fenced');
event('RECOVERY_READY_0123456789ABCDEF');assert.equal(p.recovery.stage,'fenced');
// Generate a real processor capture after clear ACK but before INPUT, and hold its port delivery.
for(let i=0;i<13;i++) worklet.process([[new Float32Array(128).fill(.7)]],[[new Float32Array(128)]]);
const delayed=worklet.port.out.filter(x=>x.type==='capture');assert.ok(delayed.length,'processor generated pre-INPUT capture');worklet.port.out.length=0;
event('RECOVERY_INPUT_0123456789ABCDEF');assert.equal(p.recovery.stage,'input');
const before=sent.length; for(const message of delayed)p.onWorklet(message);
assert.equal(sent.length,before,'delayed pre-INPUT capture must be dropped');
// A partial frame and FIR tail generated across INPUT must be cleared in the
// audio thread before its first challenge-tagged frame is made.
assert.ok(worklet.capture.length > 0, 'partial capture exists before reset');
assert.ok(worklet.history.some(x=>x!==0), 'filter history exists before reset');
deliverWorklet();
assert.equal(worklet.capture.length,0);assert.ok(worklet.history.every(v=>v===0));
assert.equal(worklet.captureEpoch,'0123456789ABCDEF');
for(let i=0;i<13;i++)worklet.process([[new Float32Array(128).fill(.4)]],[[new Float32Array(128)]]);
const fresh=worklet.port.out.filter(x=>x.type==='capture');assert.ok(fresh.length);
for(const message of fresh)p.onWorklet(message);
assert.ok(sent.length>before,'capture generated after reset proceeds');
console.log('PASS: delayed pre-INPUT capture and partial/filter tail cannot cross boundary');
capture();assert.equal(sent.at(-1)[0],3);
p.onMedia(audio(2,345));assert.equal(p.rxSequence,3);assert.equal(p.playbackPending.length,0);
now+=2100;p.ctx.currentTime=now/1000;capture();p.lastOutputProgress=now;p.updateConnected();assert.deepEqual(ended,[]);assert.equal(p.metrics.watchdog.expectRoomPlayback,false);
event('RECOVERY_READY_0123456789ABCDEF');assert.equal(p.recovery,null);
const afterReady=sent.length;for(const message of delayed)p.onWorklet(message);
assert.equal(sent.length,afterReady,'old epoch stays rejected after READY');
const playedBefore=p.metrics.renderedFrames;p.onWorklet({type:'played',playbackEpoch:null});
assert.equal(p.metrics.renderedFrames,playedBefore,'old playback cannot prove new-epoch progress');
p.onWorklet({type:'played',playbackEpoch:'0123456789ABCDEF'});assert.equal(p.metrics.renderedFrames,playedBefore+1);
p.onMedia(audio(3,456));assert.equal(p.rxSequence,4);assert.ok(pendingToWorklet.some(v=>v.type==='play'));
event('RECOVERY_PAUSE_0123456789ABCDEF');assert.equal(p.recovery,null);
// A distinct later incident is allowed, but late prior-incident capture,
// clear acknowledgements and replayed challenges remain rejected.
event('RECOVERY_PAUSE_2222222222222222');assert.equal(p.recovery.stage,'clearing');
p.onWorklet({type:'clear-ack',challenge:'0123456789ABCDEF'});assert.equal(p.recovery.stage,'clearing');
deliverWorklet();assert.equal(p.recovery.stage,'fenced');
event('RECOVERY_INPUT_0123456789ABCDEF');assert.equal(p.recovery.stage,'fenced');
event('RECOVERY_INPUT_2222222222222222');assert.equal(p.recovery.stage,'input');
const oldBefore=sent.length;for(const message of fresh)p.onWorklet(message);
assert.equal(sent.length,oldBefore,'prior incident capture must not enter later incident');
deliverWorklet();assert.equal(worklet.captureEpoch,'2222222222222222');
event('RECOVERY_READY_2222222222222222');assert.equal(p.recovery,null);
const playedSecond=p.metrics.renderedFrames;p.onWorklet({type:'played',playbackEpoch:'0123456789ABCDEF'});
assert.equal(p.metrics.renderedFrames,playedSecond,'prior incident playback must not prove new progress');
event('RECOVERY_PAUSE_0123456789ABCDEF');assert.equal(p.recovery,null,'old challenge must not replay');
now+=2100;p.lastCapture=now;p.lastOutputProgress=now;p.updateConnected();assert.ok(ended.includes('Media progress stopped.'));
// A separate call with caller input muted may recover room listening, while no caller PCM is sent.
p.inflight=true;p.attempt=2;p.softMuted=true;p.captureEpoch=null;worklet.captureEpoch=null;p.recoveryUsed=null;p.recovery=null;sent.length=0;
event('RECOVERY_PAUSE_0123456789ABCDEF');assert.equal(p.recovery.stage,'clearing');deliverWorklet();
assert.equal(p.recovery.stage,'fenced');assert.equal(sent.length,1);
event('RECOVERY_INPUT_0123456789ABCDEF');capture();assert.equal(sent.length,1);
event('RECOVERY_READY_0123456789ABCDEF');capture();assert.equal(sent.length,1);assert.equal(p.softMuted,true);
console.log('PASS: owner-bound pause, worklet clear/ack, challenge PING, phase ordering, stale/duplicate rejection, capture/output gates, clock bookkeeping and active watchdog');


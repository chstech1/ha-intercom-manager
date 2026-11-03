import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';
import { TextEncoder } from 'node:util';

const base = new URL('../custom_components/intercom_m3_poc/frontend/', import.meta.url);
let Processor, now = 1000;
class WorkletBase { constructor() { this.port = {messages: [], postMessage(m) { this.messages.push(m); }}; } }
const wc = vm.createContext({AudioWorkletProcessor: WorkletBase, registerProcessor: (_, p) => Processor=p, sampleRate:48000, currentFrame:0, currentTime:0, Int16Array, Float32Array, Math});
vm.runInContext(readFileSync(new URL('worklet.js', base), 'utf8'), wc);
const pc = vm.createContext({TextEncoder, HTMLElement:class {}, performance:{now:()=>now}, setTimeout, clearTimeout});
vm.runInContext(readFileSync(new URL('panel.js', base), 'utf8'), pc);
const Panel=pc.IntercomM3FrontendTest.IntercomM3PocPanel;
function setup() {
  const w=new Processor(), p=Object.create(Panel.prototype), ended=[];
  Object.assign(p,{panelConfig:{simulation:false}, callMode:'full_duplex', inflight:true, callId:'c', ctx:{state:'running',sampleRate:48000,currentTime:0}, clock:{fresh:()=>true}, mediaStarted:1000, mediaWasActive:true,lastActiveMode:'speaker_only',fullModeStarted:0,lastCapture:0,lastPlayed:0,lastOutputProgress:0,softMuted:false,render(){},outputReady(){return true},cleanup(reason){ended.push(reason)},node:{port:{postMessage(data){w.port.onmessage({data});}}}});
  p.metrics=p.emptyMetrics(); p.metrics.backend='ACTIVE_SPEAKER_ONLY';p.metrics.server={room_mic_muted:true};
  let captures=0,played=0, blocks=0;
  function step(seconds,{input=true,output=true,process=true}={}) {
    for(let i=0;i<Math.ceil(seconds*48000/128);i++) {
      wc.currentFrame=blocks*128; wc.currentTime=wc.currentFrame/48000;blocks++;
      p.ctx.currentTime=blocks*128/48000;now=1000+p.ctx.currentTime*1000;
      if(process)w.process([input?[new Float32Array(128)]:[]],[output?[new Float32Array(128)]:[]]);
      for(const m of w.port.messages.splice(0)){if(m.type==='capture')captures++;if(m.type==='played')played++;p.onWorklet(m);}
      if(blocks%90===0)p.updateConnected();
    }
    p.updateConnected();
  }
  return {w,p,ended,step,counts:()=>({captures,played})};
}
{
 const t=setup();t.step(3.1);assert.deepEqual(t.ended,[]);assert.ok(t.counts().captures>=95);assert.equal(t.counts().played,0);assert.ok(now-t.p.lastCapture<70&&now-t.p.lastOutputProgress<70);
 t.p.softMuted=true;t.step(2.1);assert.deepEqual(t.ended,[]);assert.equal(t.counts().played,0);
 t.p.metrics.backend='CONNECTING';t.p.metrics.server.room_mic_muted=false;t.step(2.1);assert.deepEqual(t.ended,[]);
 t.p.metrics.backend='ACTIVE_FULL';t.p.updateConnected();t.step(2.1);assert.ok(t.ended.includes('Media progress stopped.'));assert.equal(t.p.metrics.watchdog.expectRoomPlayback,true);
 console.log('PASS: actual processor empty-playback local graph survives physical mute and connecting/unmute; full mode without room PCM stops');
}
for(const [label,options,predicate] of [['missing input',{input:false},'captureStalled'],['missing output',{output:false},'outputStalled'],['processor stopped',{process:false},'captureStalled']]) {
 now=1000;const t=setup();t.step(.2);t.step(2.2,options);assert.ok(t.ended.includes('Media progress stopped.'));assert.equal(t.p.metrics.watchdog[predicate],true);console.log('PASS: '+label+' is detected');
}
now=1000;{const t=setup();t.step(.2);t.p.metrics.backend='ACTIVE_FULL';t.p.metrics.server.room_mic_muted=false;t.p.updateConnected();for(let i=0;i<80;i++){t.w.port.onmessage({data:{type:'play',samples:new Int16Array(512),deadline:t.p.ctx.currentTime+1}});t.step(.032);}assert.ok(t.counts().played>=79);assert.deepEqual(t.ended,[]);console.log('PASS: real worklet room-frame rendering maintains full-mode playback progress');}
now=1000;{const t=setup();t.step(2.1);t.p.ctx.state='suspended';t.p.updateConnected();assert.ok(t.ended.includes('Media progress stopped.'));assert.equal(t.p.metrics.watchdog.contextRunning,false);console.log('PASS: suspended context is detected');}


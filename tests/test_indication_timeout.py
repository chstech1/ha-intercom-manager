"""A genuine stalled indication is bounded and never resumes capture."""
import asyncio
from types import SimpleNamespace
from check_real_media import FakeWS,FakeEndpoint,Principal,media,AUDIO,encode_frame
async def main():
 ws,ep=FakeWS(),FakeEndpoint();ep.mode='speaker_only';ep.ready_value=True;events=[]
 owner=SimpleNamespace(call_id=bytes(16),generation=1,mode_generation=0,initial_mode='full_duplex',attached=True,caller_tx_enabled=True,principal=Principal())
 async def run(_config,_call,_owner,_mode,_generation,bridge,_authorize):await bridge(ep)
 class Clock(media.ClockRelation):
  def fresh(self,now):return True
  def pong(self,*args):pass
  def age_upper(self,sent,prior,nowus,now):return prior
 oldrun,oldclock=media.run_endpoint,media.ClockRelation;media.run_endpoint=run;media.ClockRelation=Clock
 async def feed():
  i=0
  while True:
   now=asyncio.get_running_loop().time();ep.last_sink=now;ep.status_rx_sequence+=1
   await ws.incoming.put(SimpleNamespace(type=media.WSMsgType.BINARY,data=encode_frame(4,media.READY.pack(15,48000,0,0))))
   await ws.incoming.put(SimpleNamespace(type=media.WSMsgType.BINARY,data=encode_frame(3,AUDIO.pack(i,int(now*1e6),0,512,bytes(1024)))));i+=1;await asyncio.sleep(.05)
 async def record(s,r,m):events.append((s,r))
 f=asyncio.create_task(feed());t=asyncio.create_task(media._run_real_media(ws,owner,'full_duplex',lambda:asyncio.sleep(0,result=True),record,lambda:True,object()))
 try:
  for _ in range(80):
   await asyncio.sleep(.03)
   if any(s=='ACTIVE_SPEAKER_ONLY' for s,_ in events):break
  assert any(s=='ACTIVE_SPEAKER_ONLY' for s,_ in events),events
  ep.upgrade_generation=0;ep.upgrade_started=asyncio.get_running_loop().time()-11.1
  await asyncio.wait_for(t,2)
  assert events[-1]==('TERMINATED','UPGRADE_TIMEOUT') and ep.mode=='speaker_only',events
  print('PASS: stalled indication ends at existing bound and never enables room capture')
 finally:
  f.cancel();t.cancel();await asyncio.gather(f,t,return_exceptions=True);media.run_endpoint=oldrun;media.ClockRelation=oldclock
asyncio.run(main())

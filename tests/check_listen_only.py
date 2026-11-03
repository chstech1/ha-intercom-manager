"""An originally speaker-only call must not auto-upgrade on physical unmute."""
import asyncio,sys
from pathlib import Path
from types import SimpleNamespace
p=Path(__file__).resolve().parent;sys.path.insert(0,str(p))
from check_real_media import FakeWS,FakeEndpoint,Principal,media,AUDIO,encode_frame
async def main():
 ws,endpoint=FakeWS(),FakeEndpoint();endpoint.mode='speaker_only';endpoint.mode_generation=1;endpoint.status=4;endpoint.ready_value=True
 owner=SimpleNamespace(call_id=bytes(16),generation=1,mode_generation=1,initial_mode='speaker_only',attached=True,caller_tx_enabled=True,principal=Principal())
 calls=[];states=[]
 async def request(g):calls.append(g)
 endpoint.request_duplex=request
 async def fake_run(_config,_call,_owner,_mode,_generation,bridge,_authorize):await bridge(endpoint)
 class StableClock(media.ClockRelation):
  def fresh(self,now):return True
  def pong(self,payload,now_us,now):pass
  def age_upper(self,sent,prior,now_us,now):return prior
 old_run,old_clock=media.run_endpoint,media.ClockRelation;media.run_endpoint,media.ClockRelation=fake_run,StableClock
 async def feed():
  sequence=0
  while True:
   now=asyncio.get_running_loop().time();endpoint.last_sink=now;endpoint.status_rx_sequence+=1
   await ws.incoming.put(SimpleNamespace(type=media.WSMsgType.BINARY,data=encode_frame(4,media.READY.pack(14,48000,0,0))))
   await ws.incoming.put(SimpleNamespace(type=media.WSMsgType.BINARY,data=encode_frame(3,AUDIO.pack(sequence,int(now*1000000),0,512,bytes(1024)))))
   sequence+=1;await asyncio.sleep(.05)
 task=asyncio.create_task(media._run_real_media(ws,owner,'speaker_only',lambda:asyncio.sleep(0,result=True),lambda state,reason,metrics:asyncio.sleep(0,result=states.append((state,reason))),lambda:True,object()))
 feeder=asyncio.create_task(feed())
 try:
  for _ in range(100):
   if any(x[0]=='ACTIVE_SPEAKER_ONLY' for x in states):break
   await asyncio.sleep(.02)
  else:raise AssertionError('speaker-only call never active')
  endpoint.unmute_edges+=1;await asyncio.sleep(.2)
  assert not calls and not task.done(), 'original one-way call was upgraded or ended'
  print('PASS original speaker-only ignores physical unmute edge')
 finally:
  feeder.cancel();task.cancel();await asyncio.gather(feeder,task,return_exceptions=True);media.run_endpoint,media.ClockRelation=old_run,old_clock
asyncio.run(main())



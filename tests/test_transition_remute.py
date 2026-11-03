"""Actual HA media/endpoint state path with delayed matching PONG during indication."""
import asyncio,sys,uuid,struct
from pathlib import Path
sys.path.insert(0,str(Path(__file__).parent))
from check_real_media import FakeWS,Principal,media,AUDIO,encode_frame
from endpoint import EndpointClient,EndpointConfig,HEADER,ENVELOPE,STATUS,PING,PONG,AUDIO as EAUDIO
from types import SimpleNamespace
async def main():
 loop=asyncio.get_running_loop();ws=FakeWS();events=[];controls=[];pings=[];reader=None
 async def auth():return True
 client=EndpointClient(EndpointConfig('localhost',6054,uuid.UUID(int=1),'x','x','x','x','x',4),b'c'*16,1,'full_duplex',renewal_authorize=auth)
 client.connection=b'a'*16;client.boot=b'b'*16;client.answered=client.started=True;client.capabilities=4
 client.clock.offset_us=0;client.clock.uncertainty_us=22000;client.clock.updated=loop.time();client.reader=asyncio.StreamReader()
 owner=SimpleNamespace(call_id=client.call,generation=1,mode_generation=0,initial_mode='full_duplex',attached=True,caller_tx_enabled=True,principal=Principal())
 async def send_control(kind,tail,**kwargs):
  controls.append(kind)
  if kind==4:pings.append(PING.unpack(tail))
 client.send_control=send_control;client.start=lambda:asyncio.sleep(0)
 entered=asyncio.Event()
 async def fake_run(*args,**kwargs):
  nonlocal reader
  reader=asyncio.create_task(client.reader_loop());entered.set()
  try:await args[5](client)
  finally:reader.cancel();await asyncio.gather(reader,return_exceptions=True)
 async def frame(kind,tail):
  seq=client.rx_sequence;body=ENVELOPE.pack(client.connection,client.boot,seq,client.call,client.owner)+tail
  client.reader.feed_data(HEADER.pack(kind,len(body))+body)
  for _ in range(100):
   await asyncio.sleep(.005)
   if reader.done():raise reader.exception()
   if client.rx_sequence>seq:return
  raise AssertionError('reader did not process frame')
 async def status(state,mode,health,gen):
  now=int(loop.time()*1e6);report=max(client.last_status_report_us+1,now)
  await frame(12,STATUS.pack(state,mode,health,gen,client.rx_sequence,0,10,10,report,10))
 async def pcm(gen,sequence):
  now=int(loop.time()*1e6);await frame(1,EAUDIO.pack(gen,sequence,now,0,512)+bytes(1024))
 async def feed():
  sequence=0
  while True:
   now=int(loop.time()*1e6)
   await ws.incoming.put(SimpleNamespace(type=media.WSMsgType.BINARY,data=encode_frame(4,media.READY.pack(15,48000,0,0))))
   await ws.incoming.put(SimpleNamespace(type=media.WSMsgType.BINARY,data=encode_frame(3,AUDIO.pack(sequence,now,0,512,bytes(1024)))));sequence+=1
   await asyncio.sleep(.05)
 oldrun,oldclock=media.run_endpoint,media.ClockRelation
 class Clock(oldclock):
  def fresh(self,now):return True
  def pong(self,*args):pass
  def age_upper(self,sent,prior,nowus,now):return prior
 media.run_endpoint=fake_run;media.ClockRelation=Clock
 feeder=asyncio.create_task(feed());task=asyncio.create_task(media._run_real_media(ws,owner,'full_duplex',auth,lambda s,r,m:asyncio.sleep(0,result=events.append((s,r))),lambda:True,object()))
 pinger=asyncio.create_task(client.pinger_loop())
 try:
  await asyncio.wait_for(entered.wait(),2)
  for _ in range(40):
   await status(3,1,31,0);await pcm(0,client.rx_media_sequence);await asyncio.sleep(.05)
   if any(s=='ACTIVE_FULL' for s,r in events):break
  assert any(s=='ACTIVE_FULL' for s,r in events),events
  await status(4,2,45,1)
  for _ in range(30):
   await status(4,2,45,1);await asyncio.sleep(.05)
   if any(s=='ACTIVE_SPEAKER_ONLY' for s,r in events):break
  assert client.mode=='speaker_only' and client.room_mic_muted and client.capture.empty()
  await status(4,2,13,1)
  for _ in range(40):
   await status(4,2,13,1);await asyncio.sleep(.05)
   if 14 in controls:break
  assert controls.count(14)==1 and client.upgrade_generation==1,events
  while len(pings)<2:
   await status(5,2,19,1);await asyncio.sleep(.05)
  nonce,t1=pings[0]
  await client._clock_frame(5,PONG.pack(nonce,t1,t1+1000,t1+2000),idle=False)
  assert client.clock.rejected_samples==1 and client.clock.fresh(loop.time()) and not task.done()
  nonce,t1=pings[1];now=int(loop.time()*1e6);mid=max(t1,now-1000)
  await client._clock_frame(5,PONG.pack(nonce,t1,mid,mid),idle=False)
  assert client.mode=='speaker_only' and client.upgrade_generation==1
  await status(4,2,45,2)
  assert client.room_mic_muted and client.upgrade_generation is None and client.mode=='speaker_only'
  await asyncio.sleep(.15)
  assert controls.count(14)==1 and client.mode=='speaker_only' and client.capture.empty()
  print('PASS: remute cancels pending upgrade and late calibration cannot reopen microphone')
  return
  for _ in range(40):
   await status(3,1,31,2);await pcm(2,client.rx_media_sequence);await asyncio.sleep(.05)
   if events[-1][0]=='ACTIVE_FULL':break
  assert events[-1][0]=='ACTIVE_FULL' and client.mode_generation==2 and not task.done(),events[-10:]
  assert controls.count(14)==1
  print('PASS: active duplex -> physical mute -> indication + delayed PONG -> fresh clock -> accepted result -> fresh duplex; same owner/call remains active')
 finally:
  pinger.cancel();feeder.cancel();task.cancel();await asyncio.gather(pinger,feeder,task,return_exceptions=True);media.run_endpoint=oldrun;media.ClockRelation=oldclock
asyncio.run(main())



"""Bounded real-clock pending-response recovery; no physical/audio claims."""
import asyncio,sys,uuid
from pathlib import Path
sys.path.insert(0,str(Path(__file__).parent.parent/'custom_components/intercom_m3_poc'))
import endpoint as e,media as m
from session import decode_frame
async def test_endpoint():
 loop=asyncio.get_running_loop();sent=[];second=asyncio.Event()
 async def allowed():return True
 c=e.EndpointClient(e.EndpointConfig('localhost',6054,uuid.UUID(int=1),'x','x','x','x','x',4),b'c'*16,1,'speaker_only',renewal_authorize=allowed);c.started=True;c.clock.updated=loop.time();c.clock.uncertainty_us=22000;c.clock.offset_us=0
 async def send(kind,tail,**kw):
  if kind==4:
   sent.append(e.PING.unpack(tail))
   if len(sent)==2:second.set()
 c.send_control=send;task=asyncio.create_task(c.pinger_loop())
 try:
  await asyncio.wait_for(second.wait(),2.5);nonce,t1=sent[0];old=c.clock.updated
  await c._clock_frame(5,e.PONG.pack(nonce,t1,t1+1000,t1+2000),idle=False)
  assert c.clock.updated==old and c.clock.rejected_samples==1 and c.clock.fresh(loop.time())
  try:await c._clock_frame(5,e.PONG.pack(nonce,t1,t1+1000,t1+2000),idle=False)
  except e.EndpointEnded as ex: assert str(ex)=='CLOCK_INVALID'
  else:raise AssertionError('replayed PONG accepted')
  nonce,t1=sent[1];now=int(loop.time()*1e6);mid=max(t1,now-1000)
  await c._clock_frame(5,e.PONG.pack(nonce,t1,mid,mid),idle=False)
  assert c.clock.updated>=old and c.clock.fresh(loop.time())
  assert not c.clock.fresh(c.clock.updated+3.01),'no PONG must not extend clock'
 finally:task.cancel();await asyncio.gather(task,return_exceptions=True)
def test_browser():
 c=m.ClockRelation();c.updated=1.;c.offset_us=0;c.uncertainty_us=3000
 _,b=decode_frame(c.ping(1000000));n1,t1=m.PING.unpack(b)
 _,b=decode_frame(c.ping(2000000));n2,t2=m.PING.unpack(b)
 c.pong(m.PONG.pack(n1,t1,1501000,1501000),2002000,2.002)
 assert c.updated==1 and c.rejected_samples==1 and c.fresh(2.002)
 try:c.pong(m.PONG.pack(n1,t1,1501000,1501000),2003000,2.003)
 except m.MediaEnded as ex:assert str(ex)=='CLOCK_INVALID'
 else:raise AssertionError('replay accepted')
 c.pong(m.PONG.pack(n2,t2,2001000,2001000),2002000,2.002);assert c.updated==2.002
 assert not c.fresh(5.01)
 bad=e.ClockRelation();bad.pending={4:1000000}
 try:bad.pong(4,1000000,1501000,1501000,2002000,5.0)
 except e.EndpointEnded as ex:assert str(ex)=='CLOCK_UNTRUSTED'
 else:raise AssertionError('untrusted sample renewed stale relation')
 for alteration in ('nonce','timestamp','regression'):
  bad=e.ClockRelation();bad.pending={4:1000000};bad.last_peer_us=1001000 if alteration=='regression' else -1
  try:bad.pong(5 if alteration=='nonce' else 4,1000001 if alteration=='timestamp' else 1000000,1000000,1000000,1002000,1.002)
  except e.EndpointEnded as ex:assert str(ex)=='CLOCK_INVALID'
  else:raise AssertionError('invalid clock accepted: '+alteration)
 c=m.ClockRelation()
 for i in range(5):c.ping(1000000+i*1000)
 assert len(c.pending)==3
asyncio.run(test_endpoint());test_browser()
print('PASS: delayed matched PONG dropped as untrusted with fresh clock, subsequent sample recovers; replay rejected, missing PONG expires, memory bounded')



"""Validate fixed diagnostic categories and unchanged public terminal code."""
import asyncio,sys,logging
from pathlib import Path
sys.path.insert(0,str(Path(__file__).parent.parent/'custom_components/intercom_m3_poc'))
import media,endpoint
from types import SimpleNamespace
class Collector(logging.Handler):
 def __init__(self):super().__init__();self.lines=[]
 def emit(self,record):self.lines.append(record.getMessage())
async def main():
 c=endpoint.ClockRelation();c.pending={1:1000}
 try:c.pong(2,1000,1010,1011,1020,1)
 except endpoint.EndpointEnded as ex:assert str(ex)=='CLOCK_INVALID' and ex.clock_branch=='endpoint_pong_match'
 else:raise AssertionError('unmatched nonce accepted')
 c=media.ClockRelation();c.pending={1:1000}
 try:c.pong(media.PONG.pack(1,1000,1000,1100),1050,1)
 except media.MediaEnded as ex:assert str(ex)=='CLOCK_INVALID' and ex.clock_branch=='browser_pong_rtt'
 else:raise AssertionError('negative RTT accepted')
 events=[];h=Collector();media._LOGGER.addHandler(h);old=media.run_endpoint
 async def fail(*args,**kwargs):raise endpoint.EndpointClockInvalid('endpoint_pong_match')
 class WS:
  async def close(self,**kw):self.closed=kw
 async def on_state(state,reason,metrics):events.append((state,reason))
 ws=WS();owner=SimpleNamespace(call_id=b'c'*16,generation=1,mode_generation=0,attached=True,principal=SimpleNamespace(ingress_class='direct_lan'))
 media.run_endpoint=fail
 try:await media._run_real_media(ws,owner,'full_duplex',lambda:asyncio.sleep(0,result=True),on_state,lambda:True,object())
 finally:media.run_endpoint=old;media._LOGGER.removeHandler(h)
 assert events[-1]==('TERMINATED','CLOCK_INVALID')
 lines=' '.join(h.lines);assert 'branch=endpoint_pong_match' in lines and 'upgrade_pending=False' in lines
 assert 'nonce=' not in lines and 'token=' not in lines and 'pcm=' not in lines
 print('PASS: fixed clock branch recorded without secrets; public termination remains CLOCK_INVALID')
asyncio.run(main())


import asyncio,sys,uuid
from pathlib import Path
sys.path.insert(0,str(Path(__file__).parent.parent/'custom_components/intercom_m3_poc'))
import endpoint as e
async def main():
 loop=asyncio.get_running_loop();sent=[];allowed=True
 async def auth():return allowed
 c=e.EndpointClient(e.EndpointConfig('localhost',6054,uuid.UUID(int=1),'x','x','x','x','x',4),b'c'*16,1,'speaker_only',renewal_authorize=auth);c.started=True;c.clock.updated=loop.time()-2.8;c.clock.uncertainty_us=22000
 async def send(k,tail,**kw):sent.append(k)
 c.send_control=send
 try:await asyncio.wait_for(c.pinger_loop(),2.0)
 except e.EndpointEnded as ex:assert str(ex)=='CLOCK_STALE'
 else:raise AssertionError('missing PONG did not expire')
 assert sent==[4]
 allowed=False;c.clock.updated=loop.time();sent.clear()
 try:await c.pinger_loop()
 except e.EndpointEnded as ex:assert str(ex)=='AUTH_REVOKED'
 else:raise AssertionError('revoked authority did not terminate')
 assert not sent,'must not renew after owner revoke'
 print('PASS: no-PONG expires within existing deadline; revoked authority sends no renewal')
asyncio.run(main())


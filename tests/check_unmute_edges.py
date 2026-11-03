"""Check physical edge recognition in the exact clean HA candidate endpoint."""
import asyncio,importlib.util,struct,sys,uuid
from pathlib import Path
path=Path(__file__).resolve().parent.parent/'custom_components/intercom_m3_poc/endpoint.py'
spec=importlib.util.spec_from_file_location('edge_endpoint',path);e=importlib.util.module_from_spec(spec);sys.modules[spec.name]=e;spec.loader.exec_module(e)
async def main():
 loop=asyncio.get_running_loop();config=e.EndpointConfig('localhost',6054,uuid.UUID(int=1),'x','x','x','x','x',4)
 c=e.EndpointClient(config,b'c'*16,1,'speaker_only');c.connection,c.boot,c.answered=b'a'*16,b'b'*16,True
 c.clock.offset_us,c.clock.uncertainty_us,c.clock.updated=123000000,1000,loop.time();c.reader=asyncio.StreamReader();reader=asyncio.create_task(c.reader_loop())
 async def status(health):
  seq=c.rx_sequence;report=max(c.last_status_report_us+1,int(loop.time()*1000000+c.clock.offset_us));tail=e.STATUS.pack(4,2,health,0,0,0,10,0,report,10)
  body=e.ENVELOPE.pack(c.connection,c.boot,seq,c.call,c.owner)+tail;c.reader.feed_data(e.HEADER.pack(12,len(body))+body)
  for _ in range(100):
   await asyncio.sleep(.001)
   if reader.done():raise reader.exception()
   if c.rx_sequence>seq:return
  raise AssertionError('status not processed')
 try:
  await status(13);assert c.unmute_edges==0
  await status(77);await status(13);assert c.unmute_edges==0, 'DND clear was mistaken for physical unmute'
  await status(45);assert c.room_mic_muted and c.unmute_edges==0
  await status(13);assert not c.room_mic_muted and c.unmute_edges==1
  await status(13);assert c.unmute_edges==1
  print('PASS unknown initial, DND clear, genuine physical edge and duplicate status')
 finally:reader.cancel();await asyncio.gather(reader,return_exceptions=True)
asyncio.run(main())



"""Synthetic real STATUS reader and watchdog; no device or real TLS."""
import asyncio
from types import SimpleNamespace
import uuid
from check_real_media import FakeWS, Principal, media, AUDIO, encode_frame
from endpoint import EndpointClient, EndpointConfig, ENVELOPE, STATUS

async def main():
    loop=asyncio.get_running_loop(); ws=FakeWS(); events=[]
    async def authorized(): return True
    client=EndpointClient(EndpointConfig('localhost',6054,uuid.UUID(int=1),'x','x','x','x','x',4),b'c'*16,1,'speaker_only',1,authorized)
    client.connection=b'a'*16; client.boot=b'b'*16; client.answered=True; client.started=True;client.capabilities=4
    client.clock.fresh=lambda now: True; client.clock.report_transit_us=lambda report, nowus, now: 0
    client.reader=asyncio.StreamReader(); client.start=lambda: asyncio.sleep(0)
    owner=SimpleNamespace(call_id=client.call,generation=1,mode_generation=1,initial_mode='full_duplex',attached=True,caller_tx_enabled=True,principal=Principal())
    entered=asyncio.Event()
    async def endpoint_run(*args,**kwargs):
        reader=asyncio.create_task(client.reader_loop()); entered.set()
        try: await args[5](client)
        finally:
            reader.cancel(); await asyncio.gather(reader,return_exceptions=True)
    async def status(state,health):
        report=max(client.last_status_report_us+1,int(loop.time()*1_000_000))
        tail=STATUS.pack(state,2,health,1,client.rx_sequence,0,10,10,report,10)
        body=ENVELOPE.pack(client.connection,client.boot,client.rx_sequence,client.call,client.owner)+tail
        from endpoint import HEADER
        client.reader.feed_data(HEADER.pack(12,len(body))+body)
        await asyncio.sleep(.015)
    async def feeder():
        seq=0
        while True:
            now=loop.time(); await ws.incoming.put(SimpleNamespace(type=media.WSMsgType.BINARY,data=encode_frame(4,media.READY.pack(15,48000,0,0))))
            await ws.incoming.put(SimpleNamespace(type=media.WSMsgType.BINARY,data=encode_frame(3,AUDIO.pack(seq,int(now*1e6),0,512,bytes(1024)))));seq+=1
            await asyncio.sleep(.05)
    oldrun,oldclock=media.run_endpoint,media.ClockRelation
    class Clock(oldclock):
        def fresh(self,now): return True
        def pong(self,*args): pass
        def age_upper(self,sent,prior,nowus,now): return prior
    media.run_endpoint=endpoint_run; media.ClockRelation=Clock
    feeder_task=asyncio.create_task(feeder()); task=asyncio.create_task(media._run_real_media(ws,owner,'full_duplex',authorized,lambda s,r,m: asyncio.sleep(0,result=events.append((s,r))),lambda:True,object()))
    try:
        await asyncio.wait_for(entered.wait(),2)
        for _ in range(100):
            await status(4,13); await asyncio.sleep(.05)
            if task.done(): print('ENDED_EARLY',events); break
            if any(x[0]=='ACTIVE_SPEAKER_ONLY' for x in events):break
        assert any(x[0]=='ACTIVE_SPEAKER_ONLY' for x in events),events
        client.upgrade_generation=1;client.upgrade_started=loop.time()
        for _ in range(55):
            await status(5,19) # output/capture/AEC healthy; fresh LED/chime incomplete
            await asyncio.sleep(.035)
            if task.done(): break
        assert not task.done(), events[-8:]
        assert client.mode=='speaker_only' and client.upgrade_generation==1
        for _ in range(65):
            await status(5,18) # actual sink health missing
            await asyncio.sleep(.035)
            if task.done(): break
        await asyncio.wait_for(task,2)
        assert events[-1]==('TERMINATED','MEDIA_TIMEOUT'),events[-8:]
        print('PASS: pending indication preserves real speaker progress; failed sink still times out')
    finally:
        task.cancel();feeder_task.cancel();await asyncio.gather(task,feeder_task,return_exceptions=True);media.run_endpoint=oldrun;media.ClockRelation=oldclock
asyncio.run(main())












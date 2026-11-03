import ast, asyncio, base64, copy, json, re, sys, tempfile, types
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
import probatio
root=Path(__file__).parent.parent/'custom_components/intercom_m3_poc'
pkg=types.ModuleType('fixture');pkg.__path__=[];sys.modules['fixture']=pkg
class Refused(ValueError): pass
ns={'__name__':'fixture','__package__':'fixture','re':re,'base64':base64,'Any':object,'Refused':Refused}
init=ast.parse((root/'__init__.py').read_text(encoding='utf8'))
fnames={'_b64','_encode_generation','_decode_id','_decode_generation','_browser_watchdog_snapshot','ws_call_diagnostic','ws_stop'}
nodes=[];schemas={}
class WS:
 @staticmethod
 def websocket_command(schema):
  def wrap(fn): schemas[fn.__name__]=probatio.Schema(schema);return fn
  return wrap
 @staticmethod
 def async_response(fn):return fn
ns['websocket_api']=WS;ns['probatio']=probatio
for node in init.body:
 if isinstance(node,ast.Assign) and any(isinstance(t,ast.Name) and t.id in {'SAFE_ID','SAFE_GENERATION','SAFE_REASON','ALLOWED_STATES','OWNER_SCHEMA','CLIENT_REASONS'} for t in node.targets): nodes.append(node)
 if isinstance(node,(ast.FunctionDef,ast.AsyncFunctionDef)) and node.name in fnames:
  if node.name not in {'ws_call_diagnostic','ws_stop'}:node.decorator_list=[]
  nodes.append(node)
exec(compile(ast.fix_missing_locations(ast.Module(body=nodes,type_ignores=[])),'candidate/__init__.py','exec'),ns)
for k,v in ns.items():setattr(pkg,k,v)
manager_ast=ast.parse((root/'manager.py').read_text(encoding='utf8'));klass=next(n for n in manager_ast.body if isinstance(n,ast.ClassDef) and n.name=='Manager');klass.body=[n for n in klass.body if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef)) and n.name in {'diagnostic_load','diagnostic_start','diagnostic_event','diagnostic_schedule_save'}];klass.decorator_list=[]
mn={'__name__':'fixture.manager','__package__':'fixture','datetime':datetime,'timezone':timezone,'deepcopy':copy.deepcopy};exec(compile(ast.fix_missing_locations(ast.Module(body=[klass],type_ignores=[])),'candidate/manager.py','exec'),mn);Manager=mn['Manager']
class Store:
 def __init__(self,data=None):self.data=copy.deepcopy(data);self.block=None;self.fail=False;self.saves=0
 async def async_load(self):
  if self.block:await self.block.wait()
  return copy.deepcopy(self.data)
 async def async_save(self,value):
  if self.block:await self.block.wait()
  if self.fail:raise OSError('disk')
  self.data=copy.deepcopy(value);self.saves+=1
class Hass:
 def async_create_background_task(self,coro,name):return asyncio.create_task(coro)
class Conn:
 def __init__(self):self.result=None;self.error=None
 def send_result(self,i,v):self.result=v
class Runtime:
 def __init__(self,m,principal):self.manager=m;self.principal=principal;self.session=SimpleNamespace(owner=None);self.terminal_owner=None
 def principal_from_connection(self,c):return self.principal
ns['_send_error']=lambda c,m,e:setattr(c,'error',e)
ns['_runtime']=lambda h,device_id=None: h.runtime
ns['_message_runtime']=lambda h,m:h.runtime
# actual handler uses its globals, not pkg's snapshot of globals
snapshot={'frontendBuild':'0.4.9','captureStalled':False,'outputStalled':True,'contextRunning':True,'captureAgeMs':100,'outputAgeMs':2300,'contextState':'running','backendState':'CONNECTING','callMode':'full_duplex','lastActiveMode':'full_duplex','roomMuted':False,'expectRoomPlayback':True}
def manager(store):
 m=Manager();m.hass=Hass();m.diagnostic_store=store;m.diagnostic_lock=asyncio.Lock();m.diagnostic_records=[];m.diagnostic_owners={};m.diagnostic_rates={};m.diagnostic_write_failures=0;m.diagnostic_load_failed=False;m.diagnostic_loading=True;m.diagnostic_dirty=False;m.diagnostic_task=None;return m
async def flush(m):
 for i in range(20):
  if m.diagnostic_task is None:return
  await asyncio.wait_for(asyncio.shield(m.diagnostic_task),1)
 raise AssertionError('writer stuck')
async def main():
 store=Store();m=manager(store);await m.diagnostic_load();cid=ns['_b64'](bytes(range(16)));gen='0000000000000001';owner=SimpleNamespace(call_id=bytes(range(16)),generation=1,principal='alice');m.diagnostic_start(owner,'room');r=Runtime(m,'alice');h=SimpleNamespace(runtime=r)
 msg={'type':'intercom_native/call_diagnostic','call_id':cid,'owner_generation':gen,'snapshot':snapshot,'id':1}
 # HA's wrapper adds id; websocket registration validates command schema on the message sans id here.
 clean={k:v for k,v in msg.items() if k!='id'};schemas['ws_call_diagnostic'](clean)
 c=Conn();await ns['ws_call_diagnostic'](h,c,msg);assert c.result=={'accepted':True,'durable':False};await flush(m)
 assert store.data['calls'][0]['events'][-1]['snapshot']==snapshot
 # Optional rearm evidence is current-owner/epoch bound and never persisted.
 r.current_owner=lambda candidate:r.session.owner is candidate
 r.session.owner=owner;owner.recovery_epoch='0123456789ABCDEF';owner.recovery_playback=None
 healthy=snapshot|{'outputStalled':False,'outputAgeMs':0,'expectRoomPlayback':True,'backendState':'ACTIVE_FULL'}
 evidence=msg|{'snapshot':healthy,'playback_frames':100,'playback_epoch':owner.recovery_epoch}
 schemas['ws_call_diagnostic']({k:v for k,v in evidence.items() if k!='id'})
 await ns['ws_call_diagnostic'](h,Conn(),evidence);assert owner.recovery_playback[0]==100
 await ns['ws_call_diagnostic'](h,Conn(),evidence|{'playback_epoch':'FFFFFFFFFFFFFFFF','playback_frames':1000});assert owner.recovery_playback[0]==100
 await ns['ws_call_diagnostic'](h,Conn(),evidence);assert owner.recovery_playback is None
 await ns['ws_call_diagnostic'](h,Conn(),evidence|{'playback_frames':-1});assert owner.recovery_playback is None
 r.principal='mallory';bad=Conn();await ns['ws_call_diagnostic'](h,bad,evidence|{'playback_frames':101});assert bad.error=='NOT_OWNER' and owner.recovery_playback is None
 # Identical user and token on another control connection remains a different principal.
 principal_a=('alice','token','connection-a');principal_b=('alice','token','connection-b')
 m.diagnostic_owners[cid]=(principal_a,1,'room');owner.principal=principal_a;r.principal=principal_b
 bad=Conn();await ns['ws_call_diagnostic'](h,bad,evidence|{'playback_frames':101});assert bad.error=='NOT_OWNER' and owner.recovery_playback is None
 m.diagnostic_owners[cid]=('alice',1,'room');owner.principal='alice'
 r.principal='alice';other_owner=SimpleNamespace(call_id=b'z'*16,generation=9,principal='alice',recovery_epoch=owner.recovery_epoch,recovery_playback=None);r.session.owner=other_owner
 await ns['ws_call_diagnostic'](h,Conn(),evidence|{'playback_frames':102});assert other_owner.recovery_playback is None
 r.session.owner=None;assert 'playback_frames' not in repr(m.diagnostic_records)
 # Duplicate routine rate-limited, terminal bypasses rate and is idempotent.
 await ns['ws_call_diagnostic'](h,Conn(),msg);before=len(m.diagnostic_records[0]['events']);terminal=msg|{'terminal':True,'client_reason':'watchdog'};schemas['ws_call_diagnostic']({k:v for k,v in terminal.items() if k!='id'});await ns['ws_call_diagnostic'](h,Conn(),terminal);await ns['ws_call_diagnostic'](h,Conn(),terminal);assert len(m.diagnostic_records[0]['events'])==before+1
 # Replacement owner exists; an old matching principal may append its own record, not mutate the new one.
 owner2=SimpleNamespace(call_id=bytes(range(1,17)),generation=2,principal='bob');m.diagnostic_start(owner2,'room');r.session.owner=owner2;other=copy.deepcopy(m.diagnostic_records[-1]);r.principal='mallory';bad=Conn();await ns['ws_call_diagnostic'](h,bad,terminal);assert bad.error=='NOT_OWNER';assert m.diagnostic_records[-1]==other
 r.principal='alice';await ns['ws_call_diagnostic'](h,Conn(),terminal);assert m.diagnostic_records[-1]==other
 # invalid/secret fields fail, no leakage.
 bad=Conn();await ns['ws_call_diagnostic'](h,bad,msg|{'snapshot':snapshot|{'pcm':'SECRET'}});assert bad.error=='INVALID_REQUEST';assert 'SECRET' not in str(m.diagnostic_records)
 await flush(m);saved=copy.deepcopy(store.data);m2=manager(Store(saved));await m2.diagnostic_load();assert not m2.diagnostic_load_failed and m2.diagnostic_records==saved['calls']
 corrupt=Store({'schema':1,'calls':[{'secret':'token'}]});m3=manager(corrupt);await m3.diagnostic_load();assert m3.diagnostic_load_failed;m3.diagnostic_start(owner,'room');assert corrupt.saves==0 and m3.diagnostic_records
 # File-backed adapter verifies serialized metadata can be read by a fresh manager.
 class DiskStore:
  def __init__(self,path):self.path=path
  async def async_load(self):return json.loads(self.path.read_text(encoding='utf8')) if self.path.exists() else None
  async def async_save(self,value):self.path.write_text(json.dumps(value),encoding='utf8')
 with tempfile.TemporaryDirectory(prefix='intercom-test-') as directory:
  file=Path(directory)/'test-store-roundtrip.json';disk=manager(DiskStore(file));await disk.diagnostic_load();disk.diagnostic_start(owner,'room');disk.diagnostic_event(cid,'client_reported',{'snapshot':snapshot});await flush(disk)
  restarted=manager(DiskStore(file));await restarted.diagnostic_load();assert not restarted.diagnostic_load_failed and restarted.diagnostic_records==disk.diagnostic_records
 # Slow and failed disk do not block call-side record creation.
 blocked=Store();blocked.block=asyncio.Event();m4=manager(blocked);t=asyncio.create_task(m4.diagnostic_load());await asyncio.sleep(0);m4.diagnostic_start(owner,'room');assert m4.diagnostic_records and not t.done();blocked.block.set();await t;await flush(m4)
 m4.diagnostic_store.fail=True;m4.diagnostic_event(cid,'server',{'state':'TERMINATED','reason':'MEDIA_ENDED'});await flush(m4);assert m4.diagnostic_write_failures==1 and m4.diagnostic_dirty
 # Terminal snapshot is recorded before teardown timeout; diagnostic disk is stalled.
 m6=manager(Store());await m6.diagnostic_load();m6.diagnostic_store.block=asyncio.Event();m6.diagnostic_start(owner,'room');rr=Runtime(m6,'alice');rr.session=SimpleNamespace(owner=owner, require_owner=lambda principal,call,generation: owner if (principal,call,generation)==('alice',owner.call_id,1) else (_ for _ in ()).throw(Refused('NOT_OWNER')))
 async def failed_revoke(reason):
  assert any(e.get('watchdog')==snapshot for e in m6.diagnostic_records[0]['events'])
  rr.session.owner=None;rr.terminal_owner=('alice',owner.call_id,1);return False
 rr.revoke=failed_revoke;hh=SimpleNamespace(runtime=rr);stop={'type':'intercom_native/stop','call_id':cid,'owner_generation':gen,'watchdog':snapshot,'client_reason':'watchdog','id':5}
 schemas['ws_stop']({k:v for k,v in stop.items() if k!='id'});cc=Conn();await asyncio.wait_for(ns['ws_stop'](hh,cc,stop),.2);assert cc.error=='TEARDOWN_TIMEOUT';assert any(e.get('watchdog')==snapshot for e in m6.diagnostic_records[0]['events']);m6.diagnostic_store.block.set();await flush(m6)
 # Existing administrator export returns an independent copy and rejects non-admin.
 api=ast.parse((root/'manager_api.py').read_text(encoding='utf8'));diag=next(n for n in api.body if isinstance(n,ast.AsyncFunctionDef) and n.name=='diagnostics');diag.decorator_list=[]
 ha=types.ModuleType('homeassistant');ha.__path__=[];loader=types.ModuleType('homeassistant.loader')
 async def integration(*args):return SimpleNamespace(version='0.4.9')
 loader.async_get_integration=integration;sys.modules['homeassistant']=ha;sys.modules['homeassistant.loader']=loader;pkg.DOMAIN='intercom_m3_poc'
 dn={'__name__':'fixture.manager_api','__package__':'fixture','deepcopy':copy.deepcopy,'admin':lambda u:None if u.is_admin else (_ for _ in ()).throw(PermissionError())};exec(compile(ast.fix_missing_locations(ast.Module(body=[diag],type_ignores=[])),'candidate/manager_api.py','exec'),dn)
 m6.revision=1;m6.calls={};m6.runtimes={};m6.configuration={'rooms':[]};m6.history_write_failures=0;m6.listener_failures=0;m6.runtime_cleanup_failures=0
 exported=await dn['diagnostics'](m6,SimpleNamespace(is_admin=True),None,None,{})
 assert exported['call_diagnostics']==m6.diagnostic_records;exported['call_diagnostics'].clear();assert m6.diagnostic_records
 try:await dn['diagnostics'](m6,SimpleNamespace(is_admin=False),None,None,{})
 except PermissionError:pass
 else:raise AssertionError('non-admin export')
 # Actual runtime disconnect path records the last server-observed terminal reason.
 runtime_class=next(n for n in init.body if isinstance(n,ast.ClassDef) and n.name=='Runtime')
 revoke=next(n for n in runtime_class.body if isinstance(n,ast.FunctionDef) and n.name=='revoke_now');revoke.decorator_list=[]
 rv={'asyncio':asyncio,'_b64':ns['_b64']};exec(compile(ast.fix_missing_locations(ast.Module(body=[revoke],type_ignores=[])),'candidate/runtime.py','exec'),rv)
 m7=manager(Store());await m7.diagnostic_load();m7.diagnostic_start(owner,'room');m7.schedule_record=lambda item:None
 session=SimpleNamespace(owner=owner);session.revoke=lambda:setattr(session,'owner',None)
 rr=SimpleNamespace(session=session,manager=m7,device_id='room',call_started_at=0,lease_task=None,control_connection=None,revoke_unsub=None,subscribers={},media_ws=None,_emit=lambda *a:None,_notify_status=lambda:None)
 rv['revoke_now'](rr,'CONTROL_CLOSED');assert session.owner is None and m7.diagnostic_records[0]['events'][-1]['reason']=='CONTROL_CLOSED';await flush(m7)
 assert m7.diagnostic_store.data['calls'][0]['events'][-1]['reason']=='CONTROL_CLOSED'
 # bounded retention and events
 m5=manager(Store());await m5.diagnostic_load()
 for i in range(105):
  o=SimpleNamespace(call_id=i.to_bytes(16,'big'),generation=i+1,principal='alice');m5.diagnostic_start(o,'room')
 for i in range(70):m5.diagnostic_event(ns['_b64'](o.call_id),'server',{'state':'CONNECTING','reason':'PROGRESS'})
 assert len(m5.diagnostic_records)==100 and len(m5.diagnostic_records[-1]['events'])==40;await flush(m5)
 print('PASS registered probatio schema, handler, persistence/reload, corruption, slow/error disk, rate, terminal duplicate, principal and replacement isolation, bounds/redaction')
asyncio.run(main())


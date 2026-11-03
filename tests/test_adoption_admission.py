"""Run actual adoption admission and authorization methods without Home Assistant."""
import ast
import asyncio
from dataclasses import asdict, dataclass, replace
import hashlib
import json
from pathlib import Path
import re
import time
from types import SimpleNamespace as NS

ROOT=Path(__file__).resolve().parents[1]/'custom_components/intercom_m3_poc'
tree=ast.parse((ROOT/'manager_deployment.py').read_text())
names={'_sha','_catalog','DeploymentError','PUBLIC_FILES'}
nodes=[n for n in tree.body if (isinstance(n,(ast.FunctionDef,ast.ClassDef)) and n.name in names) or (isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id in names for t in n.targets))]
cls=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name=='DeploymentManager')
methods=[n for n in cls.body if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef)) and n.name in {'_adoption_check','_adoption_authorization','_approval','_reconcile_material'}]
nodes.append(ast.ClassDef(name='Manager',bases=[],keywords=[],body=methods,decorator_list=[]))
ns=dict(hashlib=hashlib,json=json,re=re,time=time,asdict=asdict)
exec(compile(ast.fix_missing_locations(ast.Module(body=nodes,type_ignores=[])),'admission','exec'),ns)
pins={};exec((ROOT/'deployment_release.py').read_text(),pins)
m=ns['Manager']();m.bundle_root=ROOT/'firmware';m.compatibility_pin=pins['COMPATIBILITY_SHA256'];m.manifest_pin=pins['BUNDLE_MANIFEST_SHA256'];m._active=lambda _:None
catalog,_=ns['_catalog'](m.bundle_root,m.compatibility_pin,m.manifest_pin)
metadata=NS(prestate=NS(application_sha256=catalog['application_sha256'],init_sha256=catalog['files']['S99ha-speaker'],bootstrap_sha256=catalog['files']['bootstrap.sh'],application_mode='stopped'),established_6054=0,transaction_pending=False,configuration_sha256='a'*64)
m._adoption_check('device',metadata)
def denied(change,code):
 saved={}
 for obj,key,value in change:
  saved[(id(obj),key)]=(obj,getattr(obj,key));setattr(obj,key,value)
 try:
  try: m._adoption_check('device',metadata)
  except ns['DeploymentError'] as error: assert str(error)==code
  else: raise AssertionError('unsafe adoption admitted')
 finally:
  for (_,key),(obj,value) in saved.items():setattr(obj,key,value)
denied([(metadata.prestate,'application_sha256','b'*64)],'ADOPTION_BUILD_MISMATCH')
denied([(metadata.prestate,'application_mode','system')],'ADOPTION_STOPPED_BASELINE_REQUIRED')
for key,value in [('established_6054',1),('transaction_pending',True),('configuration_sha256',None)]:denied([(metadata,key,value)],'ADOPTION_STOPPED_BASELINE_REQUIRED')
m._active=lambda _:{} 
try:m._adoption_check('device',metadata)
except ns['DeploymentError'] as error:assert str(error)=='ADOPTION_ALREADY_MANAGED'
else:raise AssertionError('duplicate adoption admitted')
@dataclass
class Target: host:str='private-target'
m.targets={'device':Target()};op={'operation_id':'1'*32,'artifact_sha256':'2'*64,'action':'adopt'}
a=m._adoption_authorization('device',op)
assert a['schema']==2 and a['purpose']=='adoption'
assert not {'approved','boot_clock_ready','recovery_ready'} & a.keys()
assert 'private-target' not in json.dumps(a)
m.targets['device']=Target('different-target')
assert a['target_binding_sha256']!=m._adoption_authorization('device',op)['target_binding_sha256']
m.targets['device']=Target()
print('PASS: exact stopped build only; live connections, pending operations, missing configuration and duplicate management refused; authority records no invented physical-test results')
# Accepted adoption authority survives a reboot, but cannot authorize a new action,
# a different target/package or an expired interactive execution.
import tempfile
with tempfile.TemporaryDirectory() as tmp:
    m.root=Path(tmp);directory=m.root/'device'/op['operation_id'];directory.mkdir(parents=True)
    p=directory/'accepted-checkpoint.json';p.write_text(json.dumps(a))
    ns['_private_read']=lambda path:path.read_bytes()
    assert m._approval('device',op)==a
    for key,value in [('target_binding_sha256','f'*64),('prepared_archive_sha256','e'*64),('purpose','other'),('expires_epoch',True),('expires_epoch',int(time.time())-1)]:
        p.write_text(json.dumps(a|{key:value}))
        try:m._approval('device',op)
        except ns['DeploymentError']:pass
        else:raise AssertionError('changed/expired authorization admitted')
    p.write_text(json.dumps(a|{'expires_epoch':0}))
    assert m._approval('device',op,accepted_boot=True)['expires_epoch']==0
print('PASS: accepted authority is target/package/action bound; interactive expiry and boolean expiry rejected')

async def reconcile_stopped():
    async def executor(fn,*args): return fn(*args)
    cleanup=[]
    async def clean(target,credentials,origin):cleanup.append(origin)
    ns['cleanup_permanent_stage']=clean
    m.hass=NS(async_add_executor_job=executor);m.reservations={'private-target'}
    record={'action':'adopt','adoption_previous_config_sha256':'a'*64,
        'expected':{key:getattr(metadata.prestate,key) for key in ('application_sha256','init_sha256','bootstrap_sha256')}}
    ns['_private_read']=lambda _:json.dumps(record).encode()
    operation={'reconciles_operation_id':'1'*32,'mutation_started':True}
    await m._reconcile_material('device',operation,Target(),None,metadata)
    assert not operation['mutation_started'] and not m.reservations and cleanup==['1'*32]
    metadata.configuration_sha256='c'*64
    try:await m._reconcile_material('device',operation,Target(),None,metadata)
    except ns['DeploymentError'] as error:assert str(error)=='RECONCILIATION_REQUIRED'
    else:raise AssertionError('unknown stopped configuration reconciled')
asyncio.run(reconcile_stopped())
print('PASS: failed adoption reconciles only the exact restored stopped configuration')

"""Exercise the real transaction in its isolated filesystem fixture; no network."""
import hashlib
import json
import os
import sys
from pathlib import Path
import subprocess
import tempfile

TX = Path(__file__).resolve().parents[1] / 'custom_components/intercom_m3_poc/firmware/transaction.sh'
PRIVATE = ('ca.pem', 'endpoint.pem', 'endpoint.key', 'intercom_m3_poc.json', 'sound.json')
sha = lambda p: hashlib.sha256(p.read_bytes()).hexdigest()

def run(fail=False, crash=None):
    with tempfile.TemporaryDirectory(prefix='intercom-permanent-fixture.', dir='/tmp') as tmp:
        root = Path(tmp)
        state = root/'data/intercom-m56-install'
        stage = root/'tmp/intercom-permanent-alpha'
        app = root/'usr/bin/linux-voice-assistant-cpp'
        init = root/'etc/init.d/S99ha-speaker'
        helper = state/'bootstrap.sh'
        def put(p, text, mode=0o600):
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text); p.chmod(mode)
        put(root/'.intercom-fixture', 'offline-paired-fixture-v1')
        for p in (app, init, helper): put(p, p.name+' supported bytes', 0o755 if p != helper else 0o700)
        for name in PRIVATE: put(state/'private'/name, 'old '+name)
        put(state/'engine.sh', 'existing engine', 0o700)
        put(root/'fixture-service.sh', '#!/bin/sh\n'+('case \"$1\" in verify) exit 1;; esac\nexit 0\n' if fail else 'exit 0\n'), 0o700)
        pair = ''.join(f'{sha(p)}  {name}\n' for p,name in [(app,'application'),(init,'S99ha-speaker'),(helper,'bootstrap.sh')])
        oldref = '1'*32
        snap = state/'snapshots'/oldref
        for p,name in [(app,'application'),(init,'S99ha-speaker'),(helper,'bootstrap.sh')]: put(snap/name,p.read_text(),0o700)
        for name in PRIVATE: put(snap/'private'/name, (state/'private'/name).read_text())
        put(snap/'pair.sha256',pair)
        put(snap/'private.sha256',''.join(f'{sha(state/"private"/name)}  {name}\n' for name in PRIVATE))
        put(state/'current.ref',oldref+'\n')
        put(state/'stock.ref',oldref+'\n')
        for name in ('application','S99ha-speaker','bootstrap.sh','transaction.sh','S98intercom-recover','attention.wav'):
            text = TX.read_text() if name=='transaction.sh' else (app.read_text() if name=='application' else init.read_text() if name=='S99ha-speaker' else helper.read_text() if name=='bootstrap.sh' else name)
            put(stage/name,text,0o600 if name=='attention.wav' else 0o700)
        manifest=''.join(f'{sha(stage/name)}  {name}\n' for name in ('application','S99ha-speaker','bootstrap.sh','transaction.sh','S98intercom-recover','attention.wav'))
        put(stage/'bundle.sha256',manifest); put(stage/'approved-manifest.sha256',sha(stage/'bundle.sha256')+'\n')
        put(stage/'operation.id','2'*32+'\n'); put(stage/'expected.sha256',pair)
        for name in PRIVATE: put(stage/'enrollment'/name,'new '+name)
        # Darwin appends @ to ls permissions; normalize only in this offline copy.
        runner = TX
        if sys.platform == 'darwin':
            runner = root/'transaction-test.sh'
            runner.write_text(TX.read_text().replace("awk '{print $1}'", "awk '{sub(/@$/, \"\", $1); print $1}'"))
        if runner != TX:
            put(stage/'transaction.sh',runner.read_text(),0o700)
            manifest=''.join(f'{sha(stage/name)}  {name}\n' for name in ('application','S99ha-speaker','bootstrap.sh','transaction.sh','S98intercom-recover','attention.wav'))
            put(stage/'bundle.sha256',manifest); put(stage/'approved-manifest.sha256',sha(stage/'bundle.sha256')+'\n')
        if sys.platform == 'darwin':
            source=runner.read_text()
            lock_line=next(line for line in source.splitlines() if line.startswith("printf '%s %s %s") and 'lock/owner' in line)
            source=source.replace(lock_line, "printf '999999 1 offline-boot\\n' > \"$STATE/lock/owner\"")
            source=source.replace('set -eu', 'set -eu\n# Process-interruption fixture, not a disk-power-loss test.\nsync() { :; }', 1)
            runner.write_text(source)
            put(stage/'transaction.sh',source,0o700)
            manifest=''.join(f'{sha(stage/name)}  {name}\n' for name in ('application','S99ha-speaker','bootstrap.sh','transaction.sh','S98intercom-recover','attention.wav'))
            put(stage/'bundle.sha256',manifest); put(stage/'approved-manifest.sha256',sha(stage/'bundle.sha256')+'\n')
        before = {p: p.read_bytes() for p in (app,init,helper,state/'engine.sh',state/'stock.ref')}
        env = os.environ | {'INTERCOM_FIXTURE_ROOT': tmp}
        if crash: env['INTERCOM_FIXTURE_CRASH']=crash
        result=subprocess.run(['/bin/sh',str(runner),'adopt'],env=env,capture_output=True,text=True)
        if crash:
            assert result.returncode != 0
            recovery_source=(Path(__file__).parent/'fixtures/transaction-v0410.sh').read_text()
            assert hashlib.sha256(recovery_source.encode()).hexdigest()=='12695864f090495cdceda2bbac0be4d9a27d5bad306e3dc82b191d54f0b7f6a1'
            if sys.platform=='darwin':
                recovery_source=recovery_source.replace('/tmp/intercom-permanent-fixture.*)', '/tmp/intercom-permanent-fixture.*|/private/tmp/intercom-permanent-fixture.*)')
                recovery_source=recovery_source.replace("awk '{print $1}'", "awk '{sub(/@$/, \"\", $1); print $1}'")
                recovery_source=recovery_source.replace('set -eu','set -eu\nsync() { :; }',1)
            recovery=root/'old-recovery.sh';recovery.write_text(recovery_source)
            result=subprocess.run(['/bin/sh',str(recovery),'boot-reconcile'],env=os.environ|{'INTERCOM_FIXTURE_ROOT':tmp},capture_output=True,text=True)
            assert result.returncode==0, result.stderr
        elif fail:
            assert result.returncode != 0, result.stdout
        else:
            assert result.returncode == 0, result.stderr
            assert json.loads(result.stdout)['status']=='VERIFIED'
        for p, data in before.items(): assert p.read_bytes()==data, p
        for name in PRIVATE:
            assert (state/'private'/name).read_text()==('old ' if fail or crash else 'new ')+name
        assert not (state/'pending').exists()

run()
run(fail=True)
for seam in ('journal_written','private_replaced','start_verified','previous_committed','current_committed'):
    run(crash=seam)
print('PASS: adoption changes only private pairing; failed starts and process-interruption seams using the original recovery engine restore the old stopped state')

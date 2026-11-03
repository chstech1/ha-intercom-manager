"""The actual media-state handler persists only non-secret recovery metadata."""
import ast
import asyncio
import re
from pathlib import Path
from types import SimpleNamespace

root = Path(__file__).parent.parent / 'custom_components/intercom_m3_poc'
tree = ast.parse((root / '__init__.py').read_text(encoding='utf-8'))
cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'Runtime')
method = next(n for n in cls.body if isinstance(n, ast.AsyncFunctionDef) and n.name == 'media_state')
method.decorator_list = []
ns = {'Any': object, 're': re, 'math': __import__('math'), 'ALLOWED_STATES': {'CONNECTING'},
      'SAFE_REASON': re.compile(r'^[A-Z0-9_]{1,40}$'), 'SAFE_METRICS': {'room_mic_muted'},
      '_b64': lambda value: 'call'}
exec(compile(ast.fix_missing_locations(ast.Module(body=[method], type_ignores=[])), str(root), 'exec'), ns)

async def main():
    events, emitted = [], []
    owner = SimpleNamespace(call_id=b'1' * 16, mode='full_duplex')
    manager = SimpleNamespace(diagnostic_event=lambda call, source, event: events.append(event),
                              diagnostic_write_failures=0)
    obj = SimpleNamespace(session=SimpleNamespace(owner=owner, clock=lambda: 1), manager=manager,
                          device_id='room', room_mic_muted=False,
                          _emit=lambda *args: emitted.append(args), _notify_status=lambda: None)
    secret = '0123456789ABCDEF'
    await ns['media_state'](obj, owner, 'CONNECTING', 'RECOVERY_PAUSE_' + secret, {})
    assert obj.reason == 'RECOVERY_PAUSE' and events[-1]['reason'] == 'RECOVERY_PAUSE'
    assert emitted[-1][2] == 'RECOVERY_PAUSE_' + secret
    assert secret not in repr(events)
    await ns['media_state'](obj, owner, 'CONNECTING', 'NATIVE_EOF_BODY_3', {})
    assert events[-1]['reason'] == 'NATIVE_EOF_BODY_3'
    print('PASS: call record redacts challenge and retains EOF phase/count')

asyncio.run(main())

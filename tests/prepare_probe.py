#!/usr/bin/env python3
"""Generate the transformed session headers for the offline host probe."""
from pathlib import Path
import shutil
import sys

root = Path(__file__).resolve().parents[1]
source = root / 'firmware-source' / 'project-overlay'
if len(sys.argv) != 2:
    raise SystemExit('usage: python tests/prepare_probe.py OUTPUT_DIRECTORY')
out = Path(sys.argv[1])
if out.exists() and any(out.iterdir()):
    raise SystemExit('output directory must be empty')
out.mkdir(parents=True, exist_ok=True)
for path in (source / 'm4').iterdir():
    if path.is_file():
        shutil.copy2(path, out / path.name)
for name in ('endpoint_tls.h', 'endpoint_tls.cpp'):
    shutil.copy2(source / 'vendor' / name, out / name)
sys.path.insert(0, str(source / 'vendor'))
import controller_corrections, manager_corrections
controller_corrections.apply(out)
manager_corrections.apply(out)
print('Prepared transformed host-probe inputs in', out)

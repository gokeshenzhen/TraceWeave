"""Fresh stdio MCP process with locally recorded import/version evidence."""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

root, receipt = Path(sys.argv[1]).resolve(), Path(sys.argv[2])
sys.path.insert(0, str(root))
os.chdir(root)
import server


def record():
    modules = {}
    for name, module in list(sys.modules.items()):
        source = getattr(module, '__file__', None)
        if source and Path(source).is_relative_to(root) and Path(source).is_file():
            modules[name] = dict(path=source, sha256=hashlib.sha256(Path(source).read_bytes()).hexdigest())
    receipt.write_text(json.dumps(dict(
        pid=os.getpid(), executable=sys.executable, cwd=str(root),
        head=subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip(),
        modules=modules,
    ), indent=2) + '\n')


record()
try:
    asyncio.run(server.main())
finally:
    record()

"""Measure one uvicorn worker's RSS after 10 idle minutes, without production data."""
import json
import os
import secrets
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

root = Path(__file__).resolve().parent.parent
output = root / '.local/idle-measurement.json'
output.parent.mkdir(exist_ok=True)
with tempfile.TemporaryDirectory(prefix='evorove-idle-') as directory:
    env = dict(os.environ, PYTHONPATH=str(root), DB_PATH=str(Path(directory) / 'idle.sqlite3'),
               SECRET_KEY=secrets.token_urlsafe(48), TELEGRAM_BOT_TOKEN='', TELEGRAM_CHAT_ID='')
    with open(Path(directory) / 'server.log', 'w') as log:
        worker = subprocess.Popen([sys.executable, '-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1',
                                   '--port', '8765', '--workers', '1', '--no-access-log', '--no-proxy-headers'],
                                  cwd=directory, env=env, stdout=log, stderr=log)
        try:
            for attempt in range(100):
                if worker.poll() is not None:
                    raise RuntimeError('Measurement server failed to start')
                try:
                    assert httpx.get('http://127.0.0.1:8765/health', timeout=1).status_code == 200
                    break
                except httpx.HTTPError:
                    time.sleep(.1)
            else:
                raise RuntimeError('Measurement server did not become healthy')
            started = time.monotonic()
            report = {'started_at': datetime.now(timezone.utc).isoformat(), 'pid': worker.pid, 'status': 'measuring',
                      'python': sys.version.split()[0], 'duration_seconds': 600, 'scheduler': 'enabled', 'businesses': 0}
            output.write_text(json.dumps(report, indent=2) + '\n')
            print('Idle RSS measurement started: 600 seconds', flush=True)
            while time.monotonic() - started < 600:
                if worker.poll() is not None:
                    raise RuntimeError('Measurement server stopped unexpectedly')
                time.sleep(min(5, 600 - (time.monotonic() - started)))
            rss_kib = int(subprocess.check_output(['ps', '-o', 'rss=', '-p', str(worker.pid)], text=True).strip())
            report.update(status='complete', elapsed_seconds=round(time.monotonic() - started, 2), rss_kib=rss_kib,
                          rss_mib=round(rss_kib / 1024, 2), finished_at=datetime.now(timezone.utc).isoformat(),
                          app_python_lines=sum(len(file.read_text().splitlines()) for file in (root / 'app').rglob('*.py')))
            output.write_text(json.dumps(report, indent=2) + '\n')
            print(json.dumps(report), flush=True)
        finally:
            worker.terminate()
            worker.wait(timeout=30)

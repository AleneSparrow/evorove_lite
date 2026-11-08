"""Exercise make's systemd boundary with a stub; never launch a real unit."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('unit_status', [0, 23])
def test_backup_target_loads_environment_and_propagates_failure(tmp_path, unit_status):
    recorder = tmp_path / 'arguments.json'
    systemd = tmp_path / 'systemd-run'
    systemd.write_text(f'#!{sys.executable}\n' + '''import json, os, sys
from pathlib import Path
Path(os.environ['PROBE_ARGUMENTS']).write_text(json.dumps(sys.argv[1:]))
raise SystemExit(int(os.environ['PROBE_EXIT']))
''')
    systemd.chmod(0o755)
    result = subprocess.run(['make', 'backup-check'], cwd=ROOT, capture_output=True, text=True,
                            env={**os.environ, 'PATH': str(tmp_path) + os.pathsep + os.environ['PATH'],
                                 'PROBE_ARGUMENTS': str(recorder), 'PROBE_EXIT': str(unit_status)})
    assert (result.returncode == 0) is (unit_status == 0)
    arguments = json.loads(recorder.read_text())
    assert {'--wait', '--pipe', '--collect', '--property=User=evorove', '--property=Group=evorove',
            '--property=EnvironmentFile=/etc/evorove/litestream.env'} <= set(arguments)
    assert arguments[-1] == str(ROOT / 'deploy/backup-check.sh')

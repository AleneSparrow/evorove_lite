"""Deployment checks use synthetic env data, local HTTP and disposable SQLite only."""
import importlib.util
from pathlib import Path
import sqlite3
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest

spec = importlib.util.spec_from_file_location('deploy_checks', Path(__file__).parents[1] / 'deploy/checks.py')
checks = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checks)


def test_env_presence_only():
    example = 'A=\nB=\nC=\nD=\nE=\n'
    actual = '# comment\nA="secret"\nB=\'\'\nC=   \nD=$(never-execute)\nA=last-value\n'
    assert checks.missing_env(example, actual) == ['B', 'C', 'E']


@pytest.mark.parametrize('address,safe', [
    ('127.0.0.1:8000', True), ('0.0.0.0:8000', False),
    ('[::]:8000', False), ('[::1]:8000', False), ('*:8000', False),
])
def test_listener_addresses(address, safe):
    assert checks.listeners_safe(f'LISTEN 0 128 {address} 0.0.0.0:*') is safe
    assert checks.listeners_safe('')
    assert not checks.listeners_safe('invalid ss output')


def test_multiple_listeners():
    assert not checks.listeners_safe('LISTEN 0 128 127.0.0.1:8000 *:*\nLISTEN 0 128 [::]:8000 *:*')


@pytest.mark.parametrize('status,body,allowed,ok', [
    (200, b'ok', (200,), True), (401, b'Invalid signature', (400, 401), True),
    (400, b'Invalid signature', (400, 401), True), (500, b'error', (400, 401), False),
    (200, b'Traceback (most recent call last):', (200,), False),
    (200, b'Internal Server Error', (200,), False),
])
def test_response(status, body, allowed, ok):
    assert bool(checks.response_ok(status, body, allowed)) is ok


@pytest.mark.parametrize('bad', [None, '/health', '/', '/login', '/pricing', '/billing/webhook', '/static/style.css', '/static/htmx.min.js'])
def test_smoke_over_http(bad, capsys):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(500 if self.path == bad else 200)
            self.end_headers()
            self.wfile.write(b'private-response-body')

        def do_POST(self):
            self.rfile.read(int(self.headers.get('Content-Length', '0')))
            self.send_response(500 if self.path == bad else 401)
            self.end_headers()
            self.wfile.write(b'private-response-body')

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        assert checks.smoke(f'http://127.0.0.1:{server.server_port}') == int(bad is not None)
        assert 'private-response-body' not in capsys.readouterr().out
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def database(path):
    with sqlite3.connect(path) as db:
        for table in checks.TABLES:
            db.execute(f'CREATE TABLE "{table}" (id INTEGER)')
            db.execute(f'INSERT INTO "{table}" VALUES (1)')


def test_compare_with_wal_and_mismatch(tmp_path, capsys):
    live, restored = tmp_path / 'live.db', tmp_path / 'backup.db'
    database(live)
    database(restored)
    assert checks.compare_databases(live, restored)
    with sqlite3.connect(live) as db:
        db.execute('PRAGMA journal_mode=WAL')
        db.execute('INSERT INTO users VALUES (2)')
        db.commit()
        assert not checks.compare_databases(live, restored)
    assert 'users: live=2 backup=1' in capsys.readouterr().out


@pytest.mark.parametrize('failure', [False, True])
def test_restore_command_and_cleanup(tmp_path, monkeypatch, failure):
    live = tmp_path / 'live.db'
    database(live)
    targets = []

    def restore(args, **kwargs):
        assert args[:4] == ['litestream', 'restore', '-config', '/test/config.yml']
        assert args[-1] == str(live)
        target = Path(args[args.index('-o') + 1])
        assert not target.exists()
        targets.append(target)
        assert kwargs['stderr'] == subprocess.DEVNULL
        if failure:
            target.write_bytes(b'partial')
            raise subprocess.CalledProcessError(1, args)
        database(target)

    monkeypatch.setattr(checks.subprocess, 'run', restore)
    if failure:
        with pytest.raises(subprocess.CalledProcessError):
            checks.backup_check(str(live), '/test/config.yml')
    else:
        assert checks.backup_check(str(live), '/test/config.yml') == 0
    assert not targets[0].parent.exists()
    assert live.exists()


def test_corrupt_backup(tmp_path):
    live, restored = tmp_path / 'live.db', tmp_path / 'backup.db'
    database(live)
    restored.write_bytes(b'not a SQLite database')
    with pytest.raises(sqlite3.DatabaseError):
        checks.compare_databases(live, restored)


def test_preflight_synthetic_files(monkeypatch, capsys):
    original = Path.read_text

    def read(path, *args, **kwargs):
        if str(path) == '/opt/evorove/.env':
            return 'A=secret-do-not-print\nB=""'
        if path.name == '.env.example':
            return 'A=\nB=\n'
        if str(path) == '/proc/meminfo':
            return 'MemAvailable: 262144 kB\nSwapFree: 524288 kB\n'
        raise AssertionError('unexpected file access')

    monkeypatch.setattr(Path, 'read_text', read)
    monkeypatch.setattr(Path, 'stat', lambda self: type('Stat', (), {'st_mode': 0o100600})())
    monkeypatch.setattr(checks.shutil, 'which', lambda _: '/usr/bin/tool')
    monkeypatch.setattr(checks.shutil, 'disk_usage', lambda _: type('Disk', (), {'free': 2 * 1024**3})())
    monkeypatch.setattr(checks, 'command', lambda args: {'systemctl': 'active', 'ufw': 'Status: active\n', 'ss': ''}[args[0]])
    assert checks.preflight() == 1
    output = capsys.readouterr().out
    assert 'MISSING B' in output
    assert 'secret-do-not-print' not in output
    monkeypatch.setattr(Path, 'read_text', original)


@pytest.mark.parametrize('broken', [None, 'memory', 'swap', 'disk', 'binary', 'service', 'token', 'ufw', 'listener'])
def test_preflight_failures(monkeypatch, broken):
    def read(path, *args, **kwargs):
        if str(path) == '/proc/meminfo':
            return f'MemAvailable: {0 if broken == "memory" else 262144} kB\nSwapFree: {0 if broken == "swap" else 524288} kB\n'
        if path.name in ('.env', '.env.example'):
            return 'A=synthetic'
        raise AssertionError('unexpected file access')

    monkeypatch.setattr(Path, 'read_text', read)
    monkeypatch.setattr(Path, 'stat', lambda self: type('Stat', (), {'st_mode': 0o640 if broken == 'token' else 0o600})())
    monkeypatch.setattr(checks.shutil, 'which', lambda _: None if broken == 'binary' else '/usr/bin/tool')
    monkeypatch.setattr(checks.shutil, 'disk_usage', lambda _: type('Disk', (), {'free': 0 if broken == 'disk' else 2 * 1024**3})())

    def command(args):
        if args[0] == 'systemctl':
            return 'inactive' if broken == 'service' else 'active'
        if args[0] == 'ufw':
            return 'Status: inactive' if broken == 'ufw' else 'Status: active'
        return 'LISTEN 0 128 0.0.0.0:8000 *:*' if broken == 'listener' else ''

    monkeypatch.setattr(checks, 'command', command)
    assert checks.preflight() == int(broken is not None)

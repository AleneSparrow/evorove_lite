"""Deployment checks use synthetic env data, local HTTP and disposable SQLite only."""
import ast
import importlib.util
from pathlib import Path
import sqlite3
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest

from app.db import connect, migrate, now
from app.settings import Settings as AppSettings, encrypt_password

spec = importlib.util.spec_from_file_location('deploy_checks', Path(__file__).parents[1] / 'deploy/checks.py')
checks = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checks)


def required_values():
    return '\n'.join(f'{key}=synthetic' for key in checks.REQUIRED_ENV)


def test_env_presence_only():
    actual = required_values() + '\nSECRET_KEY=""\nDB_PATH=   \nAPP_BASE_URL=$(never-execute)'
    assert checks.missing_env(actual) == ['DB_PATH', 'SECRET_KEY']


@pytest.mark.parametrize('missing', sorted(checks.REQUIRED_ENV))
def test_each_required_variable(missing, capsys):
    assert not checks.check_environment('', required_values() + f'\n{missing}=')
    assert 'MISSING ' + missing in capsys.readouterr().out


@pytest.mark.parametrize('token,chat,ok', [('', '', True), ('synthetic-token', '', False),
                                        ('', 'synthetic-chat', False), ('synthetic-token', 'synthetic-chat', True)])
def test_optional_and_telegram_pair(token, chat, ok, capsys):
    example = (checks.ROOT / '.env.example').read_text()
    actual = required_values() + f'\nTELEGRAM_BOT_TOKEN={token}\nTELEGRAM_CHAT_ID={chat}'
    assert checks.check_environment(example, actual) is ok
    output = capsys.readouterr().out
    for name in checks.env_values(example).keys() - checks.REQUIRED_ENV:
        if not checks.env_values(actual).get(name):
            if name in checks.WARN_IF_EMPTY:
                assert 'WARN ' + name + ' is empty' in output
            else:
                assert 'INFO optional not set: ' + name in output
    assert 'synthetic' not in output


def test_empty_operator_warns_and_does_not_fail(capsys):
    example = (checks.ROOT / '.env.example').read_text()
    assert 'OPERATOR_NAME=' in example and 'OPERATOR_ADDRESS=' in example
    assert {'OPERATOR_NAME', 'OPERATOR_ADDRESS'} <= checks.OPTIONAL_ENV
    assert checks.check_environment(example, required_values()) is True
    output = capsys.readouterr().out
    assert 'WARN OPERATOR_NAME is empty' in output
    assert 'WARN OPERATOR_ADDRESS is empty' in output
    assert 'FAIL OPERATOR_NAME' not in output and 'FAIL OPERATOR_ADDRESS' not in output
    assert checks.check_environment(example, required_values() + '\nOPERATOR_NAME=Ada\nOPERATOR_ADDRESS=1 Main') is True
    filled = capsys.readouterr().out
    assert 'WARN OPERATOR_NAME' not in filled and 'WARN OPERATOR_ADDRESS' not in filled
    assert 'Ada' not in filled and '1 Main' not in filled


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


@pytest.mark.parametrize('bad', [None, '/health', '/', '/login', '/faq', '/terms', '/billing/webhook', '/static/style.css', '/static/htmx.min.js'])
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
            return required_values() + '\nSECRET_KEY=\nOPTIONAL=secret-do-not-print'
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
    assert 'MISSING SECRET_KEY' in output
    assert 'secret-do-not-print' not in output
    monkeypatch.setattr(Path, 'read_text', original)


@pytest.mark.parametrize('broken', [None, 'memory', 'swap', 'disk', 'binary', 'service', 'token', 'ufw', 'listener'])
def test_preflight_failures(monkeypatch, broken):
    def read(path, *args, **kwargs):
        if str(path) == '/proc/meminfo':
            return f'MemAvailable: {0 if broken == "memory" else 262144} kB\nSwapFree: {0 if broken == "swap" else 524288} kB\n'
        if path.name in ('.env', '.env.example'):
            return required_values()
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


def test_smoke_paths_match_main_routes():
    tree = ast.parse((checks.ROOT / 'app/main.py').read_text())
    routes = set()
    mounts = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if not isinstance(node.func.value, ast.Name) or node.func.value.id != 'app':
            continue
        if not node.args or not isinstance(node.args[0], ast.Constant):
            continue
        if node.func.attr in ('get', 'post'):
            routes.add((node.args[0].value, node.func.attr.upper()))
        elif node.func.attr == 'mount':
            mounts.add(node.args[0].value)
    for path, method, _ in checks.SMOKE_REQUESTS:
        assert (path, method) in routes or (method == 'GET' and any(
            path.startswith(mount + '/') for mount in mounts))
    assert ('/faq', 'GET') in routes and ('/terms', 'GET') in routes
    assert all(path != '/pricing' for path, _, _ in checks.SMOKE_REQUESTS)


# --- launch-check ------------------------------------------------------------

def ready_db(db_path, **business_overrides):
    migrate(str(db_path))
    cipher_settings = AppSettings({'DB_PATH': str(db_path), 'SECRET_KEY': 'k' * 32})
    fields = {'id': 'cz', 'name': 'Acme', 'created_at': now(),
              'postal_address': '100 Main Street, Austin, TX 78701',
              'smtp_host': 'smtp.example.com', 'smtp_user': 'sales@acme.example',
              'smtp_password_enc': encrypt_password(cipher_settings, 'smtp-secret'),
              'imap_host': 'imap.example.com', 'imap_user': 'sales@acme.example',
              'imap_password_enc': encrypt_password(cipher_settings, 'imap-secret'),
              'brief': 'US dental clinics'}
    fields.update(business_overrides)
    with connect(str(db_path)) as db:
        db.execute('INSERT INTO businesses({}) VALUES ({})'.format(
            ','.join(fields), ','.join('?' * len(fields))), tuple(fields.values()))
        db.execute("INSERT INTO runs(id,business_id,kind,started_at,finished_at,status) VALUES "
                   "('r1',NULL,'summary',?,?,'ok')", (now(), now()))


def ready_env(db_path, **overrides):
    values = {'OPERATOR_NAME': 'Alena K', 'OPERATOR_ADDRESS': '1 Main St, Austin, TX',
              'TELEGRAM_BOT_TOKEN': 'telegram-token-do-not-print',
              'TELEGRAM_CHAT_ID': 'telegram-chat-do-not-print',
              'CLIENT_ZERO_BUSINESS_ID': 'cz', 'DB_PATH': str(db_path)}
    values.update(overrides)
    return '\n'.join(f'{key}={value}' for key, value in values.items() if value is not None)


def patch_env(monkeypatch, text):
    original = Path.read_text

    def read(self, *args, **kwargs):
        if str(self) == '/opt/evorove/.env':
            return text
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, 'read_text', read)


def test_launch_check_passes_when_everything_is_ready(tmp_path, monkeypatch, capsys):
    db_path = tmp_path / 'launch.sqlite3'
    ready_db(db_path)
    patch_env(monkeypatch, ready_env(db_path))
    assert checks.launch_check() == 0
    assert 'FAIL' not in capsys.readouterr().out


def test_launch_check_fails_when_operator_identity_is_missing(tmp_path, monkeypatch, capsys):
    db_path = tmp_path / 'launch.sqlite3'
    ready_db(db_path)
    patch_env(monkeypatch, ready_env(db_path, OPERATOR_NAME='', OPERATOR_ADDRESS=''))
    assert checks.launch_check() == 1
    output = capsys.readouterr().out
    assert 'FAIL operator identity is set' in output
    assert 'FAIL legal pages render with no placeholder text' in output  # the stub wording leaks through


def test_launch_check_fails_when_a_legal_page_still_has_stub_text(tmp_path, monkeypatch, capsys):
    db_path = tmp_path / 'launch.sqlite3'
    ready_db(db_path)
    patch_env(monkeypatch, ready_env(db_path))
    monkeypatch.setattr(checks, 'render_legal_pages', lambda settings: ['This section will be added.'])
    assert checks.launch_check() == 1
    output = capsys.readouterr().out
    assert 'FAIL legal pages render with no placeholder text' in output
    assert 'OK operator identity is set' in output


@pytest.mark.parametrize('overrides', [{'TELEGRAM_BOT_TOKEN': ''}, {'TELEGRAM_CHAT_ID': ''}])
def test_launch_check_fails_when_telegram_is_not_fully_configured(tmp_path, monkeypatch, capsys, overrides):
    db_path = tmp_path / 'launch.sqlite3'
    ready_db(db_path)
    patch_env(monkeypatch, ready_env(db_path, **overrides))
    assert checks.launch_check() == 1
    assert 'FAIL Telegram is configured and a summary ran in the last 26 hours' in capsys.readouterr().out


def test_launch_check_fails_when_no_summary_ran_recently(tmp_path, monkeypatch, capsys):
    db_path = tmp_path / 'launch.sqlite3'
    ready_db(db_path)
    with connect(str(db_path)) as db:
        db.execute("UPDATE runs SET finished_at=? WHERE id='r1'",
                  ((checks.datetime.now(checks.timezone.utc) - checks.timedelta(hours=30)).isoformat(),))
    patch_env(monkeypatch, ready_env(db_path))
    assert checks.launch_check() == 1
    assert 'FAIL Telegram is configured and a summary ran in the last 26 hours' in capsys.readouterr().out


@pytest.mark.parametrize('overrides', [{'postal_address': '12 St'}, {'smtp_host': ''}, {'imap_user': ''}, {'brief': ''}])
def test_launch_check_fails_when_client_zero_is_not_ready(tmp_path, monkeypatch, capsys, overrides):
    db_path = tmp_path / 'launch.sqlite3'
    ready_db(db_path, **overrides)
    patch_env(monkeypatch, ready_env(db_path))
    assert checks.launch_check() == 1
    assert 'FAIL client-zero business can send and receive mail' in capsys.readouterr().out


def test_launch_check_reports_billing_test_mode_allowlist_as_info_not_failure(tmp_path, monkeypatch, capsys):
    db_path = tmp_path / 'launch.sqlite3'
    ready_db(db_path)
    patch_env(monkeypatch, ready_env(db_path, BILLING_TEST_MODE_ALLOWED_EMAILS='owner@example.test'))
    assert checks.launch_check() == 0
    output = capsys.readouterr().out
    assert 'INFO BILLING_TEST_MODE_ALLOWED_EMAILS is set' in output
    assert 'FAIL' not in output


@pytest.mark.parametrize('test_mode,expected', [(True, 1), (False, 0)])
def test_launch_check_lemonsqueezy_key_mode(tmp_path, monkeypatch, capsys, test_mode, expected):
    db_path = tmp_path / 'launch.sqlite3'
    ready_db(db_path)
    patch_env(monkeypatch, ready_env(db_path, LEMONSQUEEZY_API_KEY='secret-ls-key'))
    monkeypatch.setattr(checks, '_lemonsqueezy_get', lambda api_key: {'meta': {'test_mode': test_mode}})
    assert checks.launch_check() == expected
    output = capsys.readouterr().out
    assert ('FAIL Lemon Squeezy API key is in live mode' in output) is test_mode
    assert 'secret-ls-key' not in output


def test_launch_check_lemonsqueezy_mode_undetermined_fails(tmp_path, monkeypatch, capsys):
    db_path = tmp_path / 'launch.sqlite3'
    ready_db(db_path)
    patch_env(monkeypatch, ready_env(db_path, LEMONSQUEEZY_API_KEY='secret-ls-key'))
    monkeypatch.setattr(checks, '_lemonsqueezy_get', lambda api_key: (_ for _ in ()).throw(OSError('no network')))
    assert checks.launch_check() == 1
    output = capsys.readouterr().out
    assert 'FAIL Lemon Squeezy API key is in live mode' in output
    assert 'no network' not in output


def test_launch_check_never_prints_setting_values(tmp_path, monkeypatch, capsys):
    db_path = tmp_path / 'launch.sqlite3'
    ready_db(db_path, postal_address='')
    patch_env(monkeypatch, ready_env(db_path, OPERATOR_NAME='', OPERATOR_ADDRESS='',
                                      TELEGRAM_BOT_TOKEN='', TELEGRAM_CHAT_ID=''))
    checks.launch_check()
    output = capsys.readouterr().out
    assert 'telegram-token-do-not-print' not in output
    assert 'telegram-chat-do-not-print' not in output
    assert 'Alena K' not in output and '1 Main St' not in output

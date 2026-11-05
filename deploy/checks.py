"""Deployment checks. Never print exception details, response bodies or env values."""
import argparse
import os
import signal
from contextlib import closing
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import subprocess
import tempfile
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent.parent
# Production policy: keep required names here only (runtime has some dev defaults).
REQUIRED_ENV = frozenset('''
APP_BASE_URL SECRET_KEY DB_PATH ANTHROPIC_API_KEY ANTHROPIC_MODEL
LEMONSQUEEZY_API_KEY LEMONSQUEEZY_WEBHOOK_SECRET LEMONSQUEEZY_STORE_ID
LEMONSQUEEZY_VARIANT_STARTER LEMONSQUEEZY_VARIANT_PRO
SYSTEM_SMTP_HOST SYSTEM_SMTP_PORT SYSTEM_SMTP_USER SYSTEM_SMTP_PASSWORD SYSTEM_SMTP_FROM
'''.split())
OPTIONAL_ENV = frozenset({
    'LEGACY_ACCOUNT_SECURITY_ENCRYPTION_KEY', 'OPERATOR_NAME', 'OPERATOR_ADDRESS',
})
# Empty operator identity is a warning. It does not fail preflight.
WARN_IF_EMPTY = frozenset({'OPERATOR_NAME', 'OPERATOR_ADDRESS'})
SMOKE_REQUESTS = (
    ('/health', 'GET', (200,)), ('/', 'GET', (200,)),
    ('/login', 'GET', (200,)), ('/faq', 'GET', (200,)), ('/terms', 'GET', (200,)),
    ('/billing/webhook', 'POST', (400, 401)), ('/static/style.css', 'GET', (200,)),
    ('/static/htmx.min.js', 'GET', (200,)),
)
TABLES = ('businesses', 'users', 'people', 'messages', 'payments', 'runs', 'billing_events')


def env_values(text):
    # Match the application's file syntax; never execute/source shell contents.
    values = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        key, sep, value = line.partition('=')
        if sep and re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', key.strip()):
            values[key.strip()] = value.strip().strip("\"'")
    return values


def missing_env(actual):
    values = env_values(actual)
    return sorted(key for key in REQUIRED_ENV if not values.get(key, '').strip())


def check_environment(example, actual):
    values = env_values(actual)
    missing = missing_env(actual)
    for name in missing:
        print('MISSING ' + name)
    for name in sorted((env_values(example).keys() | OPTIONAL_ENV) - REQUIRED_ENV):
        if not values.get(name, '').strip():
            if name in WARN_IF_EMPTY:
                print('WARN ' + name + ' is empty')
            else:
                print('INFO optional not set: ' + name)
    token = bool(values.get('TELEGRAM_BOT_TOKEN', '').strip())
    chat = bool(values.get('TELEGRAM_CHAT_ID', '').strip())
    if token != chat:
        print('FAIL TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set together')
    return not missing and token == chat


def command(args):
    return subprocess.run(args, capture_output=True, text=True, timeout=30, check=True,
                          env={**os.environ, "LC_ALL": "C"}).stdout


def listeners_safe(output):
    # ss -H -lnt: State Recv-Q Send-Q Local-address:port Peer-address:port
    return all(len(fields) >= 5 and fields[3] == '127.0.0.1:8000'
               for line in output.splitlines() if (fields := line.split()))


def preflight():
    failures = []

    def check(name, fn):
        try:
            ok = fn()
        except (OSError, ValueError, subprocess.SubprocessError):
            ok = False
        print(('OK ' if ok else 'FAIL ') + name)
        if not ok:
            failures.append(name)

    def environment():
        return check_environment((ROOT / '.env.example').read_text(), Path('/opt/evorove/.env').read_text())

    check('environment', environment)
    memory = {}
    try:
        memory = {key: int(value.split()[0]) for key, value in
                  (line.split(':', 1) for line in Path('/proc/meminfo').read_text().splitlines())}
    except (OSError, ValueError):
        pass
    check('available RAM >= 128 MiB', lambda: memory.get('MemAvailable', 0) >= 128 * 1024)
    check('free swap >= 256 MiB', lambda: memory.get('SwapFree', 0) >= 256 * 1024)
    for path in ('/opt/evorove', '/var/lib/evorove'):
        check('free disk >= 1 GiB: ' + path, lambda path=path: shutil.disk_usage(path).free >= 1024**3)
    for service in ('cloudflared', 'litestream'):
        check(service + ' installed', lambda service=service: shutil.which(service) is not None)
        check(service + ' active', lambda service=service: command(['systemctl', 'is-active', service]).strip() == 'active')
    check('cloudflared token mode 600', lambda: stat.S_IMODE(
        Path('/etc/cloudflared/evorove.token').stat().st_mode) == 0o600)
    check('UFW active', lambda: re.search(r'^Status: active$', command(['ufw', 'status']), re.M) is not None)
    check('port 8000 loopback only (or stopped)', lambda: listeners_safe(command(['ss', '-H', '-lnt', 'sport = :8000'])))
    return int(bool(failures))


TRACE = re.compile(rb'Traceback \(most recent call last\)|Internal Server Error|File &quot;.*?, line \d+|File ".*?", line \d+', re.I)


def response_ok(status, body, allowed):
    return status in allowed and not TRACE.search(body)


def smoke(base_url):
    parsed = urlsplit(base_url)
    if parsed.scheme not in ('http', 'https') or not parsed.netloc or parsed.query or parsed.fragment or parsed.username or parsed.password:
        raise ValueError('Invalid base URL')
    failures = 0
    for path, method, allowed in SMOKE_REQUESTS:
        try:
            request = Request(base_url.rstrip('/') + path, data=b'{}' if method == 'POST' else None,
                              headers={'Content-Type': 'application/json'}, method=method)
            try:
                response = urlopen(request, timeout=20)
            except HTTPError as exc:
                response = exc
            with response:
                status, body = response.code, response.read()
            ok = bool(body) and response_ok(status, body, allowed)
        except (OSError, ValueError):
            ok = False
        print(('OK ' if ok else 'FAIL ') + method + ' ' + path)
        failures += not ok
    return int(bool(failures))


def readonly(path):
    return sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True, timeout=15)


def compare_databases(live, restored):
    equal = True
    with closing(readonly(restored)) as backup, closing(readonly(live)) as source:
        if backup.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
            print('FAIL backup integrity_check')
            return False
        print('OK backup integrity_check')
        source.execute('BEGIN')  # Consistent WAL snapshot across every table count.
        for table in TABLES:
            current = source.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0]
            saved = backup.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0]
            print(f'{"OK" if current == saved else "FAIL"} {table}: live={current} backup={saved}')
            equal &= current == saved
    return equal


def backup_check(db_path, config):
    # Private directory; restore target must not already exist. Cleanup includes WAL/SHM.
    with tempfile.TemporaryDirectory(prefix='evorove-backup-') as directory:
        restored = Path(directory) / 'restored.sqlite3'
        subprocess.run(['litestream', 'restore', '-config', config, '-o', str(restored), db_path],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True, timeout=600)
        return int(not compare_databases(db_path, restored))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('check', choices=('preflight', 'smoke', 'backup-check'))
    parser.add_argument('base_url', nargs='?')
    parser.add_argument('--db-path', default='/var/lib/evorove/evorove.sqlite3')
    parser.add_argument('--config', default='/etc/litestream.yml')
    args = parser.parse_args()
    # Ensure TemporaryDirectory is unwound when systemd stops the check.
    def terminate(signum, frame):
        raise SystemExit(128 + signum)
    signal.signal(signal.SIGTERM, terminate)
    try:
        if args.check == 'preflight':
            return preflight()
        if args.check == 'smoke':
            if not args.base_url:
                parser.error('smoke requires base_url')
            return smoke(args.base_url)
        return backup_check(args.db_path, args.config)
    except (OSError, ValueError, sqlite3.Error, subprocess.SubprocessError):
        print('FAIL ' + args.check + ' (check configuration, permissions and service availability)')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())

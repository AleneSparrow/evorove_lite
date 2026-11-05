import hashlib
import hmac
import json
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from app import auth, billing
from app.db import connect, migrate, now
from app.main import create_app
from app.settings import Settings


@pytest.fixture
def settings(tmp_path):
    return Settings({'SECRET_KEY': 'test-only-' * 6, 'DB_PATH': str(tmp_path / 'test.sqlite3'),
                     'APP_BASE_URL': 'https://testserver', 'LEMONSQUEEZY_WEBHOOK_SECRET': 'test-webhook',
                     'LEMONSQUEEZY_VARIANT_STARTER': '100', 'LEMONSQUEEZY_VARIANT_PRO': '200',
                     'LEMONSQUEEZY_STORE_ID': '1', 'LEMONSQUEEZY_API_KEY': 'test-api',
                     'BILLING_TEST_MODE_ALLOWED_EMAILS': 'owner@example.com'})


@pytest.fixture
def client(settings):
    with TestClient(create_app(settings, scheduler_enabled=False), base_url='https://testserver', follow_redirects=False) as client:
        yield client


def form(client, page, target=None, **fields):
    response = client.get(page)
    token = re.search('name="csrf" value="([^"]+)"', response.text)[1]
    return client.post(target or page, data={'csrf': token, **fields})


def signup(client, email='owner@example.com'):
    return form(client, '/signup', email=email, password='long password 123', business_name='Acme')


def business(settings):
    with connect(settings.db_path) as db:
        return dict(db.execute('SELECT * FROM businesses').fetchone())


def event(bid, test=False, stamp='2026-11-03T10:00:00Z', status='active'):
    return {'meta': {'event_name': 'subscription_created', 'custom_data': {'business_id': bid}, 'test_mode': test},
            'data': {'type': 'subscriptions', 'id': 'sub-1', 'attributes': {'variant_id': 100, 'customer_id': 7,
                     'status': status, 'updated_at': stamp, 'test_mode': test}}}


def deliver(client, settings, event):
    data = event.get('data')
    if isinstance(data, dict):
        data.setdefault('attributes', {}).setdefault('store_id', settings.get('LEMONSQUEEZY_STORE_ID'))
    payload = json.dumps(event).encode()
    signature = hmac.new(settings.get('LEMONSQUEEZY_WEBHOOK_SECRET').encode(), payload, hashlib.sha256).hexdigest()
    return client.post('/billing/webhook', content=payload, headers={'x-signature': signature})


def test_migrations_and_health(client, settings):
    assert client.get('/health').json() == {
        'status': 'ok',
        'scheduler': {kind: {'last_success_at': None}
                      for kind in ('search', 'send', 'poll', 'summary')},
        'stale': True,
    }
    migrate(settings.db_path)
    with connect(settings.db_path) as db:
        assert db.execute('PRAGMA journal_mode').fetchone()[0] == 'wal'
        assert db.execute('PRAGMA foreign_keys').fetchone()[0] == 1
        assert db.execute('SELECT count(*) FROM schema_migrations').fetchone()[0] == 5


def test_health_reports_last_success_and_search_freshness(client, settings):
    fresh = datetime.now(timezone.utc) - timedelta(hours=25)
    old = fresh - timedelta(days=2)
    with connect(settings.db_path) as db:
        db.execute("INSERT INTO runs(id,kind,started_at,finished_at,status) VALUES ('search-old','search',?,?,'ok')",
                   (old.isoformat(), old.isoformat()))
        db.execute("INSERT INTO runs(id,kind,started_at,finished_at,status) VALUES ('search-failed','search',?,?,'failed')",
                   (now(), now()))
        db.execute("INSERT INTO runs(id,kind,started_at,finished_at,status) VALUES ('search-fresh','search',?,?,'ok')",
                   (fresh.isoformat(), fresh.isoformat()))
        db.execute("INSERT INTO runs(id,kind,started_at,finished_at,status) VALUES ('poll-ok','poll',?,?,'ok')",
                   (old.isoformat(), old.isoformat()))
    health = client.get('/health').json()
    assert health['status'] == 'ok'
    assert health['stale'] is False
    assert health['scheduler']['search']['last_success_at'] == fresh.isoformat()
    assert health['scheduler']['poll']['last_success_at'] == old.isoformat()
    assert health['scheduler']['send']['last_success_at'] is None

    stale = datetime.now(timezone.utc) - timedelta(hours=27)
    with connect(settings.db_path) as db:
        db.execute("UPDATE runs SET finished_at=? WHERE id='search-fresh'", (stale.isoformat(),))
    assert client.get('/health').json()['stale'] is True


def test_signup_login_logout(client, settings):
    assert signup(client).status_code == 303
    assert client.get('/app').status_code == 200
    with connect(settings.db_path) as db:
        user = db.execute('SELECT * FROM users').fetchone()
        assert user['role'] == 'owner'
        assert user['password_hash'].startswith('$argon2id$')
        assert db.execute('SELECT count(*) FROM sessions').fetchone()[0] == 1
    response = form(client, '/app', '/logout')
    assert response.status_code == 303
    assert client.get('/app').status_code == 303
    response = form(client, '/login', email='OWNER@example.com', password='long password 123')
    assert response.status_code == 303
    assert 'Secure' in response.headers['set-cookie']
    assert 'HttpOnly' in response.headers['set-cookie']
    assert 'SameSite=lax' in response.headers['set-cookie']


def test_legacy_pbkdf2_login_upgrades_hash(client, settings):
    signup(client)
    password = 'long password 123'
    salt = bytes.fromhex('00112233445566778899aabbccddeeff')
    digest = hashlib.pbkdf2_hmac('sha256', password.encode(), salt, 390_000).hex()
    legacy = f'pbkdf2_sha256$390000${salt.hex()}${digest}'
    with connect(settings.db_path) as db:
        db.execute("UPDATE users SET password_hash=? WHERE email='owner@example.com'", (legacy,))

    assert form(client, '/app', '/logout').status_code == 303
    assert form(client, '/login', email='owner@example.com', password=password).status_code == 303
    with connect(settings.db_path) as db:
        upgraded = db.execute("SELECT password_hash FROM users WHERE email='owner@example.com'").fetchone()[0]
    assert upgraded.startswith('$argon2id$')
    assert upgraded != legacy


def test_legacy_pbkdf2_rejects_wrong_password_without_rehash(client, settings):
    signup(client)
    salt = bytes.fromhex('00112233445566778899aabbccddeeff')
    digest = hashlib.pbkdf2_hmac('sha256', b'long password 123', salt, 390_000).hex()
    legacy = f'pbkdf2_sha256$390000${salt.hex()}${digest}'
    with connect(settings.db_path) as db:
        db.execute("UPDATE users SET password_hash=? WHERE email='owner@example.com'", (legacy,))

    assert form(client, '/app', '/logout').status_code == 303
    assert form(client, '/login', email='owner@example.com', password='wrong password 456').status_code == 401
    with connect(settings.db_path) as db:
        assert db.execute("SELECT password_hash FROM users WHERE email='owner@example.com'").fetchone()[0] == legacy


def test_password_validation_csrf_and_duplicates(client, settings):
    assert form(client, '/signup', email='a@example.com', password='short', business_name='A').status_code == 400
    assert client.post('/signup', data={'email': 'x@example.com'}).status_code == 403
    assert signup(client).status_code == 303
    assert signup(client).status_code == 400
    with connect(settings.db_path) as db:
        assert db.execute('SELECT count(*) FROM businesses').fetchone()[0] == 1
    assert form(client, '/login', email='owner@example.com', password='incorrect').status_code == 401


def test_rate_limits_persist(client, settings):
    for _ in range(8):
        assert form(client, '/forgot-password', email='absent@example.com').status_code == 200
    assert form(client, '/forgot-password', email='absent@example.com').status_code == 429


def test_password_reset_end_to_end(client, settings, monkeypatch):
    sent = []
    monkeypatch.setattr(auth, 'send_reset', lambda s, email, token: sent.append((email, token)))
    signup(client)
    assert form(client, '/forgot-password', email='owner@example.com').status_code == 200
    token = sent[0][1]
    with connect(settings.db_path) as db:
        assert db.execute('SELECT token_hash FROM password_resets').fetchone()[0] != token
    assert form(client, '/reset?token=' + token, '/reset', token=token, password='new password 456').status_code == 303
    assert client.get('/app').status_code == 303
    assert form(client, '/reset?token=' + token, '/reset', token=token, password='new password 789').status_code == 400
    assert form(client, '/login', email='owner@example.com', password='long password 123').status_code == 401
    assert form(client, '/login', email='owner@example.com', password='new password 456').status_code == 303


@pytest.mark.parametrize('test,email,expected', [(False, 'owner@example.com', 'active'),
    (True, 'owner@example.com', 'active'), (True, 'stranger@example.com', 'incomplete')])
def test_billing_modes(client, settings, test, email, expected):
    signup(client, email)
    payload = event(business(settings)['id'], test)
    assert deliver(client, settings, payload).status_code == 200
    assert deliver(client, settings, payload).status_code == 200
    assert business(settings)['subscription_status'] == expected
    with connect(settings.db_path) as db:
        assert db.execute('SELECT count(*) FROM billing_events').fetchone()[0] == 1
        assert db.execute('SELECT count(*) FROM payments').fetchone()[0] == 0
        if test and expected == 'incomplete':
            assert db.execute("SELECT count(*) FROM log WHERE event='billing_test_ignored'").fetchone()[0] == 1


def test_webhook_rejects_a_non_ascii_signature_header_with_401_not_500(client):
    # A real HTTP header can carry any byte (obs-text); httpx's str-valued headers={}
    # only accepts ASCII, so the hostile value has to go in as raw latin-1 bytes.
    assert client.post('/billing/webhook', content=b'{}',
                       headers={'x-signature': 'bad-é-signature'.encode('latin-1')}).status_code == 401


def test_webhook_rejects_an_event_for_the_wrong_lemon_squeezy_store(client, settings):
    signup(client)
    bid = business(settings)['id']
    payload = event(bid)
    payload['data']['attributes']['store_id'] = 'not-our-store'
    assert deliver(client, settings, payload).status_code == 400
    assert business(settings)['subscription_status'] == 'incomplete'


def test_api_v1_billing_webhook_is_an_alias_for_the_same_handler(client, settings):
    signup(client)
    bid = business(settings)['id']
    payload = event(bid)
    payload['data']['attributes'].setdefault('store_id', settings.get('LEMONSQUEEZY_STORE_ID'))
    body = json.dumps(payload).encode()
    signature = hmac.new(settings.get('LEMONSQUEEZY_WEBHOOK_SECRET').encode(), body, hashlib.sha256).hexdigest()
    response = client.post('/api/v1/billing/webhook', content=body, headers={'x-signature': signature})
    assert response.status_code == 200
    assert business(settings)['subscription_status'] == 'active'


def test_login_rate_limit_counts_only_failed_attempts(client, settings):
    signup(client)
    for _ in range(10):
        assert form(client, '/login', email='owner@example.com', password='long password 123').status_code == 303
        assert form(client, '/app', '/logout').status_code == 303
    for _ in range(9):
        assert form(client, '/login', email='owner@example.com', password='wrong password 456').status_code == 401
    assert form(client, '/login', email='owner@example.com', password='wrong password 456').status_code == 429
    assert form(client, '/login', email='owner@example.com', password='long password 123').status_code == 429


def test_signature_and_stale_events(client, settings):
    signup(client)
    bid = business(settings)['id']
    assert client.post('/billing/webhook', content=b'{}', headers={'x-signature': 'bad'}).status_code == 401
    assert deliver(client, settings, event(bid, status='expired')).status_code == 200
    assert deliver(client, settings, event(bid, stamp='2026-11-02T10:00:00Z')).status_code == 200
    assert business(settings)['subscription_status'] == 'expired'
    assert deliver(client, settings, {'meta': {}}).status_code == 400


def test_checkout(client, settings, monkeypatch):
    signup(client)
    captured = []
    def post(url, **kwargs):
        captured.append(kwargs['json'])
        import httpx
        return httpx.Response(201, json={'data': {'attributes': {'url': 'https://shop.lemonsqueezy.com/checkout/test'}}}, request=httpx.Request('POST', url))
    monkeypatch.setattr(billing.httpx, 'post', post)
    response = form(client, '/app', '/billing/checkout/starter')
    assert response.status_code == 303
    assert response.headers['location'].startswith('https://shop.lemonsqueezy.com/')
    assert captured[0]['data']['attributes']['checkout_data']['custom']['business_id'] == business(settings)['id']
    assert form(client, '/app', '/billing/checkout/fake').status_code == 400


def test_payment_link_for_scopes_checkout_to_person():
    url = billing.payment_link_for(
        {'id': 'business-1', 'payment_link': 'https://store.example/checkout/buy?discount=SAVE'},
        'person-1',
    )
    query = parse_qs(urlparse(url).query)
    assert query == {
        'discount': ['SAVE'],
        'checkout[custom][business_id]': ['business-1'],
        'checkout[custom][person_id]': ['person-1'],
    }


def test_client_zero_is_always_entitled_without_a_subscription():
    # Evorove selling its own subscription (client 0) never goes through Lemon
    # Squeezy itself, so it has no real subscription_status/subscription_ends_at.
    row = {'id': 'biz-zero', 'subscription_status': 'incomplete', 'subscription_ends_at': None}
    assert not billing.entitled(row)
    assert billing.entitled(row, client_zero_id='biz-zero')
    assert not billing.entitled(row, client_zero_id='some-other-business')
    assert not billing.entitled(row, client_zero_id='')


@pytest.mark.parametrize('event_name,data_type,data_id', [
    ('subscription_payment_success', 'subscription-invoices', 'invoice-person-1'),
    ('order_created', 'orders', 'order-person-1'),
])
def test_payment_event_records_done_in_same_transaction(client, settings, monkeypatch,
                                                        event_name, data_type, data_id):
    signup(client)
    bid = business(settings)['id']
    with connect(settings.db_path) as db:
        pid = 'person-payment-1'
        db.execute('INSERT INTO people(id,business_id,email,pain,evidence_quote,source_url,stage,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)',
                   (pid, bid, 'buyer@example.com', 'Need', 'Quoted need',
                    'https://example.com/source', 'offer_made', now(), now()))
    if event_name.startswith('subscription_'):
        assert deliver(client, settings, event(bid)).status_code == 200

    calls = []
    def record_payment(business_id, person_id, source, *, db=None):
        assert db is not None
        assert db.execute('SELECT id FROM businesses WHERE id=?', (business_id,)).fetchone()
        calls.append((business_id, person_id, source))
        db.execute("UPDATE people SET stage='done',updated_at=? WHERE id=? AND business_id=?",
                   (now(), person_id, business_id))
    monkeypatch.setattr(billing.outreach, 'record_payment', record_payment)
    attributes = {'total': 4900, 'updated_at': '2026-11-03T11:00:00Z', 'test_mode': False}
    if data_type == 'subscription-invoices':
        attributes['subscription_id'] = 'sub-1'
    payment_event = {
        'meta': {'event_name': event_name, 'event_id': 'event-' + data_id,
                 'custom_data': {'business_id': bid, 'person_id': pid}, 'test_mode': False},
        'data': {'type': data_type, 'id': data_id, 'attributes': attributes},
    }
    assert deliver(client, settings, payment_event).status_code == 200
    assert deliver(client, settings, payment_event).status_code == 200
    assert calls == [(bid, pid, 'lemonsqueezy')]
    with connect(settings.db_path) as db:
        payment = db.execute('SELECT person_id,source FROM payments').fetchone()
        assert tuple(payment) == (pid, 'lemonsqueezy')
        assert db.execute('SELECT stage FROM people WHERE id=?', (pid,)).fetchone()[0] == 'done'


def test_test_payment_event_obeys_email_allowlist(client, settings, monkeypatch):
    signup(client, 'stranger@example.com')
    bid = business(settings)['id']
    called = []
    monkeypatch.setattr(billing.outreach, 'record_payment', lambda *args, **kwargs: called.append(args))
    payment_event = {
        'meta': {'event_name': 'order_created', 'event_id': 'test-order-event',
                 'custom_data': {'business_id': bid, 'person_id': 'unknown'}, 'test_mode': True},
        'data': {'type': 'orders', 'id': 'test-order', 'attributes': {
            'total': 100, 'created_at': '2026-11-03T11:00:00Z', 'test_mode': True}},
    }
    assert deliver(client, settings, payment_event).status_code == 200
    assert called == []
    with connect(settings.db_path) as db:
        assert db.execute('SELECT count(*) FROM payments').fetchone()[0] == 0


def test_staff_cannot_access_owner_pages(client, settings):
    signup(client)
    with connect(settings.db_path) as db:
        db.execute("UPDATE users SET role='staff'")
    assert client.get('/app').status_code == 403
    assert form(client, '/login', '/billing/checkout/starter').status_code == 403


def test_expired_session_and_reset(client, settings):
    signup(client)
    with connect(settings.db_path) as db:
        user = db.execute('SELECT id FROM users').fetchone()[0]
        db.execute("UPDATE sessions SET expires_at='2000-01-01T00:00:00+00:00'")
        db.execute('INSERT INTO password_resets VALUES (?,?,?,0)', (auth.digest('expired'), user, '2000-01-01T00:00:00+00:00'))
    assert client.get('/app').status_code == 303
    assert form(client, '/reset', token='expired', password='another password').status_code == 400


def test_invoice_payment_is_not_subscription_update(client, settings):
    signup(client)
    bid = business(settings)['id']
    assert deliver(client, settings, event(bid)).status_code == 200
    invoice = {'meta': {'event_name': 'subscription_payment_success'},
               'data': {'type': 'subscription-invoices', 'id': 'invoice-1', 'attributes': {
                   'subscription_id': 'sub-1', 'total': 4900, 'updated_at': '2026-11-03T11:00:00Z'}}}
    assert deliver(client, settings, invoice).status_code == 200
    invoice['meta']['event_name'] = 'subscription_payment_recovered'
    assert deliver(client, settings, invoice).status_code == 200
    with connect(settings.db_path) as db:
        assert db.execute('SELECT count(*) FROM payments').fetchone()[0] == 1
        assert db.execute('SELECT amount_cents FROM payments').fetchone()[0] == 4900


def test_test_mode_cannot_be_string(client, settings):
    signup(client, 'stranger@example.com')
    payload = event(business(settings)['id'])
    payload['meta']['test_mode'] = 'true'
    assert deliver(client, settings, payload).status_code == 400
    assert business(settings)['subscription_status'] == 'incomplete'


def test_smtp_uses_verified_starttls(settings, monkeypatch):
    captured = []
    settings.values.update({'SYSTEM_SMTP_HOST': 'smtp.example.com', 'SYSTEM_SMTP_FROM': 'support@example.com',
                            'SYSTEM_SMTP_USER': 'system', 'SYSTEM_SMTP_PASSWORD': 'test-password'})
    class SMTP:
        def __init__(self, host, port, timeout):
            assert (host, port, timeout) == ('smtp.example.com', 587, 15)
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def starttls(self, context):
            import ssl
            assert context.verify_mode == ssl.CERT_REQUIRED
            captured.append('tls')
        def login(self, user, password):
            assert captured == ['tls']
            captured.append('login')
        def send_message(self, message):
            assert captured == ['tls', 'login']
            assert 'https://testserver/reset?token=test-token' in message.get_content()
            captured.append('sent')
    monkeypatch.setattr(auth.smtplib, 'SMTP', SMTP)
    auth.send_reset(settings, 'owner@example.com', 'test-token')
    assert captured[-1] == 'sent'


def test_expired_subscription_can_be_replaced(client, settings):
    signup(client)
    bid = business(settings)['id']
    assert deliver(client, settings, event(bid, status='expired')).status_code == 200
    renewed = event(bid, stamp='2026-11-04T10:00:00Z')
    renewed['data']['id'] = 'sub-2'
    assert deliver(client, settings, renewed).status_code == 200
    assert business(settings)['subscription_status'] == 'active'
    assert business(settings)['ls_subscription_id'] == 'sub-2'
    old = event(bid, stamp='2026-11-05T10:00:00Z', status='expired')
    old['meta']['event_name'] = 'subscription_updated'
    assert deliver(client, settings, old).status_code == 200
    assert business(settings)['subscription_status'] == 'active'


def test_local_defaults_generate_persistent_secret(tmp_path, monkeypatch):
    from app.settings import load
    monkeypatch.chdir(tmp_path)
    for key in ('SECRET_KEY', 'APP_BASE_URL', 'DB_PATH'):
        monkeypatch.delenv(key, raising=False)
    (tmp_path / '.env').write_text('SECRET_KEY=\nAPP_BASE_URL=\nDB_PATH=\n')
    first = load()
    assert len(first.secret) >= 32
    assert first.secret == load().secret
    assert first.base_url == 'https://localhost:8000'
    assert first.db_path == 'evorove.sqlite3'
    assert (tmp_path / '.local/dev-secret').stat().st_mode & 0o777 == 0o600
    monkeypatch.setenv('APP_BASE_URL', 'https://evorove.example')
    with pytest.raises(ValueError, match='SECRET_KEY'):
        load()


def test_local_mailbox_password_reset(tmp_path, monkeypatch, settings):
    from email import message_from_string
    monkeypatch.chdir(tmp_path)
    settings.values['APP_BASE_URL'] = 'https://localhost:8000'
    migrate(settings.db_path)
    user = auth.signup(settings, 'dev@example.com', 'original password', 'Dev')
    auth.request_reset(settings, 'dev@example.com', '127.0.0.1')
    files = list((tmp_path / '.local/mailbox').glob('*.eml'))
    assert len(files) == 1
    assert files[0].stat().st_mode & 0o777 == 0o600
    message = message_from_string(files[0].read_text())
    body = message.get_payload(decode=True).decode()
    link = re.search(r'https://localhost:8000/reset\?token=\S+', body)[0]
    token = parse_qs(urlparse(link).query)['token'][0]
    auth.reset_password(settings, token, 'replacement password')
    assert auth.login(settings, 'dev@example.com', 'replacement password', '127.0.0.1') == user


def test_env_example_has_only_empty_values():
    from app.db import ROOT
    entries = (ROOT / '.env.example').read_text().splitlines()
    assert {'MARKET_SIGNALS=', 'PER_RUN_TOKEN_BUDGET=', 'EXTRA_ALLOWED_ORIGINS='} <= set(entries)
    assert len(entries) == 25
    assert all(line.endswith('=') and line.count('=') == 1 for line in entries)

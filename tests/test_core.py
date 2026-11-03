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
    with TestClient(create_app(settings), base_url='https://testserver', follow_redirects=False) as client:
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
    payload = json.dumps(event).encode()
    signature = hmac.new(settings.get('LEMONSQUEEZY_WEBHOOK_SECRET').encode(), payload, hashlib.sha256).hexdigest()
    return client.post('/billing/webhook', content=payload, headers={'x-signature': signature})


def test_migrations_and_health(client, settings):
    assert client.get('/health').json() == {'status': 'ok'}
    migrate(settings.db_path)
    with connect(settings.db_path) as db:
        assert db.execute('PRAGMA journal_mode').fetchone()[0] == 'wal'
        assert db.execute('PRAGMA foreign_keys').fetchone()[0] == 1
        assert db.execute('SELECT count(*) FROM schema_migrations').fetchone()[0] == 2


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

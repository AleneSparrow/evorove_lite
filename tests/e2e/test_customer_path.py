"""Customer journey across the core, search, outreach, billing, and board seams."""
import asyncio
import hashlib
import hmac
import inspect
import json
import re
from email.message import EmailMessage
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app import billing, outreach
from app.db import connect, now
from app.main import create_app
from app.settings import Settings


def submit(client, page, target=None, **fields):
    response = client.get(page)
    token = re.search('name="csrf" value="([^"]+)"', response.text)[1]
    return client.post(target or page, data={'csrf': token, **fields})


def webhook(client, settings, event):
    payload = json.dumps(event).encode()
    signature = hmac.new(settings.get('LEMONSQUEEZY_WEBHOOK_SECRET').encode(), payload,
                         hashlib.sha256).hexdigest()
    return client.post('/billing/webhook', content=payload, headers={'x-signature': signature})


def test_customer_path(monkeypatch, tmp_path):
    settings = Settings({
        'SECRET_KEY': 'customer-path-test-' * 4,
        'DB_PATH': str(tmp_path / 'customer-path.sqlite3'),
        'APP_BASE_URL': 'https://testserver',
        'LEMONSQUEEZY_WEBHOOK_SECRET': 'test-webhook',
        'LEMONSQUEEZY_VARIANT_STARTER': '100',
        'LEMONSQUEEZY_VARIANT_PRO': '200',
        'BILLING_TEST_MODE_ALLOWED_EMAILS': 'owner@example.com',
    })
    sent = []

    class FakeSMTP:
        def __init__(self, host, port, timeout):
            self.host, self.port, self.timeout = host, port, timeout
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return None
        def starttls(self, context):
            return None
        def login(self, user, password):
            assert (user, password) == ('sales@example.com', 'mail-password')
        def send_message(self, message):
            sent.append(message)

    class FakeIMAP:
        messages = {}
        def __init__(self, host, port, **kwargs):
            self.host, self.port = host, port
        def login(self, user, password):
            assert (user, password) == ('sales@example.com', 'mail-password')
        def select(self, mailbox, readonly=False):
            assert mailbox == 'INBOX' and readonly is True
            return 'OK', []
        def uid(self, command, *args):
            if command == 'SEARCH':
                return 'OK', [b' '.join(str(uid).encode() for uid in sorted(self.messages))]
            uid = int(args[0])
            return 'OK', [(b'BODY[]', self.messages[uid])]
        def logout(self):
            return 'BYE', []

    monkeypatch.setattr('smtplib.SMTP', FakeSMTP)
    monkeypatch.setattr('imaplib.IMAP4_SSL', FakeIMAP)

    with TestClient(create_app(settings, scheduler_enabled=False), base_url='https://testserver',
                    follow_redirects=False) as client:
        assert submit(client, '/signup', email='owner@example.com', password='long password 123',
                      business_name='Acme').status_code == 303
        with connect(settings.db_path) as db:
            bid = db.execute('SELECT id FROM businesses').fetchone()[0]

        activation = {
            'meta': {'event_name': 'subscription_created', 'event_id': 'subscription-created-e2e',
                     'custom_data': {'business_id': bid}, 'test_mode': True},
            'data': {'type': 'subscriptions', 'id': 'sub-e2e', 'attributes': {
                'variant_id': 100, 'customer_id': 7, 'status': 'active',
                'updated_at': '2026-11-03T10:00:00Z', 'test_mode': True}},
        }
        assert webhook(client, settings, activation).status_code == 200
        assert submit(
            client, '/app/settings', postal_address='12 Main Street, Austin, TX 78701',
            offer_text='Acme installs a documented lead follow-up workflow.',
            payment_link='https://store.example/checkout/acme', sender_name='Ana at Acme',
            smtp_host='smtp.example.com', smtp_port='587', smtp_user='sales@example.com',
            smtp_password='mail-password', imap_host='imap.example.com', imap_port='993',
            imap_user='sales@example.com', imap_password='mail-password', daily_cap='20',
            quiet_start='00:00', quiet_end='00:00', brief='US B2B service founders',
        ).status_code == 303

        candidates = [
            {'name': 'Alice Buyer', 'email': 'alice@example.com', 'company': 'Alice Co',
             'website': 'https://alice.example', 'role': 'Founder', 'us_state': 'TX',
             'segment': 'B2B services', 'pain': 'Lead follow-up takes too long',
             'evidence_quote': 'We are rebuilding our lead process.',
             'source_url': 'https://alice.example/about'},
            {'name': 'Bob Buyer', 'email': 'bob@example.com', 'company': 'Bob Co',
             'website': 'https://bob.example', 'role': 'Owner', 'us_state': 'CA',
             'segment': 'B2B services', 'pain': 'New enquiries wait for a response',
             'evidence_quote': 'We need to respond to enquiries faster.',
             'source_url': 'https://bob.example/about'},
        ]
        def fake_search(business_id, *, limit=20, dry_run=False):
            result = candidates[:limit]
            if not dry_run:
                with connect(settings.db_path) as db:
                    for candidate in result:
                        db.execute('INSERT OR IGNORE INTO people(id,business_id,name,email,company,website,role,us_state,segment,pain,evidence_quote,source_url,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                                   (str(uuid4()), business_id, candidate['name'], candidate['email'],
                                    candidate['company'], candidate['website'], candidate['role'],
                                    candidate['us_state'], candidate['segment'], candidate['pain'],
                                    candidate['evidence_quote'], candidate['source_url'], now(), now()))
            return result
        monkeypatch.setattr('app.scheduler.search.run_search', fake_search)
        assert submit(client, '/app', '/app/find-people').status_code == 303
        async def finish_search():
            await asyncio.gather(*list(client.app.state.scheduler.tasks))
        client.portal.call(finish_search)
        with connect(settings.db_path) as db:
            people = [dict(row) for row in db.execute('SELECT * FROM people ORDER BY email')]
        assert len(people) == 2

        # Current main has interface-only stubs. After feat/outreach is merged this
        # same test continues through real policy/transport code without a rewrite.
        if 'NotImplementedError' in inspect.getsource(outreach.send_first_letters):
            pytest.xfail('feat/outreach is not merged: first-letter and reply steps are interface stubs')

        outreach._SETTINGS = settings
        def fake_llm(system, user, *, schema=None, web_search=False, max_uses=5, purpose):
            if purpose == 'sales_analysis':
                return {'problem': 'Slow lead follow-up', 'desired_outcome': 'Faster qualified replies',
                        'objection': '', 'objection_cause': '', 'ready': True,
                        'hostile': False, 'requires_human': False}
            return {'body': 'That makes sense. I can help with the next step.'}
        monkeypatch.setattr(outreach, 'ask', fake_llm)

        assert outreach.send_first_letters(bid) == 2
        assert len(sent) == 2
        alice = next(person for person in people if person['email'] == 'alice@example.com')
        first = next(message for message in sent if message['To'] == 'alice@example.com')

        # Establish the IMAP cursor, then receive two customer messages. The
        # governed sequence is confirm_need followed by offer.
        assert outreach.poll_replies(bid) == 0
        for uid, text in ((1, 'Yes, slow follow-up is exactly the problem.'),
                          (2, 'Confirmed. Please send the offer and payment details.')):
            reply = EmailMessage()
            reply['From'] = 'Alice Buyer <alice@example.com>'
            reply['To'] = 'sales@example.com'
            reply['Subject'] = 'Re: ' + str(first['Subject'])
            reply['Message-ID'] = f'<reply-{uid}@example.com>'
            reply['In-Reply-To'] = str(first['Message-ID'])
            reply.set_content(text)
            FakeIMAP.messages[uid] = reply.as_bytes()
            assert outreach.poll_replies(bid) == 1

        with connect(settings.db_path) as db:
            assert db.execute('SELECT stage FROM people WHERE id=?', (alice['id'],)).fetchone()[0] == 'offer_made'
            assert db.execute("SELECT count(*) FROM messages WHERE person_id=? AND direction='in'", (alice['id'],)).fetchone()[0] == 2
            assert db.execute("SELECT count(*) FROM messages WHERE person_id=? AND move='offer'", (alice['id'],)).fetchone()[0] == 1

        payment = {
            'meta': {'event_name': 'order_created', 'event_id': 'order-paid-e2e',
                     'custom_data': {'business_id': bid, 'person_id': alice['id']}, 'test_mode': True},
            'data': {'type': 'orders', 'id': 'order-e2e', 'attributes': {
                'total': 4900, 'created_at': '2026-11-03T12:00:00Z', 'test_mode': True}},
        }
        assert webhook(client, settings, payment).status_code == 200
        with connect(settings.db_path) as db:
            assert db.execute('SELECT stage FROM people WHERE id=?', (alice['id'],)).fetchone()[0] == 'done'
            assert db.execute('SELECT count(*) FROM payments WHERE person_id=?', (alice['id'],)).fetchone()[0] == 1
        assert 'Done <span class="count">1</span>' in client.get('/app/columns').text

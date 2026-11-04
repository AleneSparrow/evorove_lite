import asyncio
import re
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from starlette.requests import Request

from app import auth, board, notify, scheduler
from app.db import connect, now
from app.main import create_app
from test_core import client, settings, signup, form, business


def add_person(settings, bid, stage='cold', name='Alice'):
    pid = str(uuid4())
    with connect(settings.db_path) as db:
        db.execute('INSERT INTO people(id,business_id,name,email,company,role,us_state,pain,evidence_quote,source_url,stage,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
                   (pid, bid, name, pid+'@example.com', 'Acme Labs', 'Founder', 'CA', 'Hiring is slow', 'We are hiring', 'https://example.com/team', stage, now(), now()))
    return pid


@pytest.mark.parametrize('peer,header,expected', [
    ('127.0.0.1', '203.0.113.7', '203.0.113.7'), ('::1', '2001:db8::8', '2001:db8::8'),
    ('198.51.100.2', '203.0.113.7', '198.51.100.2'), ('127.0.0.2', '203.0.113.7', '127.0.0.2'),
    ('127.0.0.1', 'invalid', '127.0.0.1'), ('127.0.0.1', '1.2.3.4, 5.6.7.8', '127.0.0.1')])
def test_cloudflare_ip_only_from_tunnel(peer, header, expected):
    request = Request({'type': 'http', 'client': (peer, 123), 'headers': [(b'cf-connecting-ip', header.encode())]})
    assert auth.client_ip(request) == expected


def reset_attempt(client, token, ip):
    page = client.get('/reset')
    csrf = re.search('name="csrf" value="([^"]+)"', page.text)[1]
    return client.post('/reset', data={'csrf': csrf, 'token': token, 'password': 'new password 123'}, headers={'CF-Connecting-IP': ip})


def test_reset_limit_is_per_token_and_ip(settings):
    with TestClient(create_app(settings, scheduler_enabled=False), base_url='https://testserver', client=('127.0.0.1', 123)) as client:
        for _ in range(8):
            assert reset_attempt(client, 'token-a', '203.0.113.1').status_code == 400
        assert reset_attempt(client, 'token-a', '203.0.113.2').status_code == 429
        # Another token is not blocked site-wide, including for the original IP.
        assert reset_attempt(client, 'token-b', '203.0.113.1').status_code == 400
        assert reset_attempt(client, 'token-c', '203.0.113.2').status_code == 400
        for number in range(21):
            assert reset_attempt(client, f'other-{number}', '203.0.113.1').status_code == 400
        assert reset_attempt(client, 'yet-another', '203.0.113.1').status_code == 429
        with connect(settings.db_path) as db:
            keys = [row[0] for row in db.execute('SELECT key FROM rate_limits')]
        assert auth.digest('reset-token:email:' + auth.digest('token-a')) in keys
        assert all('token-a' not in key for key in keys)


def test_board_person_timeline_and_tenant_isolation(client, settings):
    signup(client)
    bid = business(settings)['id']
    pid = add_person(settings, bid)
    page = client.get('/app')
    for title in ('Cold', 'In progress', 'Offer made', 'Done'):
        assert title in page.text
    detail = client.get('/app/people/' + pid)
    assert 'The engine has not written yet.' in detail.text
    assert 'We are hiring' in detail.text
    with connect(settings.db_path) as db:
        db.execute('INSERT INTO messages(id,person_id,direction,subject,body,created_at) VALUES (?,?,?,?,?,?)', ('m1', pid, 'out', 'Hello', '<script>alert(1)</script>', now()))
        db.execute('INSERT INTO messages(id,person_id,direction,body,created_at) VALUES (?,?,?,?,?)', ('m2', pid, 'in', 'Tell me more', now()))
    detail = client.get('/app/people/' + pid)
    assert '&lt;script&gt;' in detail.text and '<script>alert(1)</script>' not in detail.text
    assert detail.text.index('Hello') < detail.text.index('Tell me more')
    auth.signup(settings, 'second@example.com', 'another password', 'Second')
    with connect(settings.db_path) as db:
        other = db.execute('SELECT id FROM businesses WHERE id!=?', (bid,)).fetchone()[0]
    other_pid = add_person(settings, other)
    assert client.get('/app/people/' + other_pid).status_code == 404
    assert client.get('/app?person=' + other_pid).status_code == 404


def test_owner_can_confirm_manual_payment(client, settings, monkeypatch):
    signup(client)
    bid = business(settings)['id']
    pid = add_person(settings, bid, stage='offer_made')
    detail = client.get('/app/people/' + pid)
    assert 'Record payment' in detail.text
    assert 'I confirm this payment was received.' in detail.text
    assert form(client, '/app/people/' + pid, '/app/people/' + pid + '/payment').status_code == 400

    calls = []
    def record_payment(business_id, person_id, source, *, settings=None):
        calls.append((business_id, person_id, source))
        with connect(settings.db_path) as db:
            db.execute("UPDATE people SET stage='done',updated_at=? WHERE id=? AND business_id=?",
                       (now(), person_id, business_id))
    monkeypatch.setattr('app.main.outreach.record_payment', record_payment)
    response = form(client, '/app/people/' + pid, '/app/people/' + pid + '/payment', confirm='yes')
    assert response.status_code == 200
    assert 'Record payment' not in response.text
    assert calls == [(bid, pid, 'manual')]
    with connect(settings.db_path) as db:
        assert db.execute('SELECT stage FROM people WHERE id=?', (pid,)).fetchone()[0] == 'done'
        assert db.execute("SELECT count(*) FROM payments WHERE raw_event_id=?",
                          (f'manual:{bid}:{pid}',)).fetchone()[0] == 1


def test_settings_validation_and_no_password_echo(client, settings):
    signup(client)
    fields = {'postal_address': '12 Main Street, Austin TX', 'daily_cap': '20', 'quiet_start': '20:00', 'quiet_end': '08:00', 'outreach_paused': 'on'}
    assert form(client, '/app/settings', **fields).status_code == 303
    assert business(settings)['outreach_paused'] == 1
    for patch in ({'postal_address': 'short'}, {'daily_cap': '0'}, {'quiet_start': '25:00'}, {'payment_link': 'javascript:alert(1)'}, {'smtp_port': '65536'}):
        assert form(client, '/app/settings', **(fields | patch)).status_code == 400
    page = client.get('/app/settings')
    assert 'type="password" name="smtp_password" autocomplete="new-password" value=""' in page.text


def test_find_people_runs_in_background_and_surfaces_failure(client, settings, monkeypatch):
    signup(client)
    bid = business(settings)['id']
    assert form(client, '/app', '/app/find-people').status_code == 403
    with connect(settings.db_path) as db:
        db.execute("UPDATE businesses SET plan='pro',subscription_status='active',brief='US founders'")
    called = []
    monkeypatch.setattr(scheduler.search, 'run_search', lambda business_id: called.append(business_id) or [{}, {}])
    assert form(client, '/app', '/app/find-people').status_code == 303
    # Wait for the tracked task deterministically on the app loop.
    async def finish():
        await asyncio.gather(*list(client.app.state.scheduler.tasks))
    client.portal.call(finish)
    assert called == [bid]
    assert board.last_search(settings, bid)['found'] == 2
    def fail(business_id):
        raise RuntimeError('SECRET-MUST-NOT-LEAK')
    monkeypatch.setattr(scheduler.search, 'run_search', fail)
    assert form(client, '/app', '/app/find-people').status_code == 303
    client.portal.call(finish)
    status = client.get('/app/search-status')
    assert 'failed' in status.text and 'RuntimeError' in status.text
    assert 'SECRET-MUST-NOT-LEAK' not in status.text


def test_search_preview_is_dry_run_and_has_no_send_actions(client, settings, monkeypatch):
    signup(client)
    bid = business(settings)['id']
    with connect(settings.db_path) as db:
        db.execute("UPDATE businesses SET plan='pro',subscription_status='active',brief='US service founders'")
    calls = []
    candidates = [{
        'company': 'North Star LLC', 'website': 'https://north.example', 'us_state': 'TX',
        'name': 'Ana Doe', 'role': 'Founder', 'pain': 'Slow lead follow-up',
        'evidence_quote': 'We need a faster response process.',
        'source_url': 'https://north.example/about', 'segment': 'B2B services',
    }]
    def run_search(business_id, *, limit=20, dry_run=False):
        calls.append((business_id, limit, dry_run))
        return candidates
    monkeypatch.setattr('app.main.search.run_search', run_search)

    assert 'Preview 20 people' in client.get('/app/search-preview').text
    assert form(client, '/app/search-preview').status_code == 303
    async def finish():
        await asyncio.gather(*list(client.app.state.search_preview_tasks))
    client.portal.call(finish)
    page = client.get('/app/search-preview')
    assert calls == [(bid, 20, True)]
    for value in ('North Star LLC', 'Ana Doe', 'Slow lead follow-up',
                  'We need a faster response process.', 'B2B services'):
        assert value in page.text
    assert 'Send' not in page.text and 'Find people' not in page.text
    with connect(settings.db_path) as db:
        assert db.execute('SELECT count(*) FROM people').fetchone()[0] == 0


def test_search_preview_failure_hides_exception_message(client, settings, monkeypatch):
    signup(client)
    with connect(settings.db_path) as db:
        db.execute("UPDATE businesses SET plan='pro',subscription_status='active',brief='US founders'")
    def fail(*args, **kwargs):
        raise RuntimeError('SECRET-MUST-NOT-LEAK')
    monkeypatch.setattr('app.main.search.run_search', fail)
    assert form(client, '/app/search-preview').status_code == 303
    async def finish():
        await asyncio.gather(*list(client.app.state.search_preview_tasks))
    client.portal.call(finish)
    page = client.get('/app/search-preview')
    assert 'Preview failed (RuntimeError)' in page.text
    assert 'SECRET-MUST-NOT-LEAK' not in page.text


def test_public_pages_and_unsubscribe(client, settings):
    for url in ('/', '/faq', '/terms', '/privacy', '/dpa', '/subprocessors'):
        page = client.get(url)
        assert page.status_code == 200
        assert 'CS-1042' not in page.text
    assert 'Finding people in the open web is part of the Service.' in client.get('/terms').text
    signup(client)
    bid = business(settings)['id']
    pid = add_person(settings, bid)
    token = board.unsubscribe_token(settings, bid, pid)
    url = '/unsubscribe/' + token
    assert client.get(url).status_code == 200
    with connect(settings.db_path) as db:
        assert db.execute('SELECT count(*) FROM suppressions').fetchone()[0] == 0
    for _ in range(2):
        assert 'You are unsubscribed' in client.post(url, data={'List-Unsubscribe': 'One-Click'}).text
    with connect(settings.db_path) as db:
        assert db.execute('SELECT count(*) FROM suppressions').fetchone()[0] == 1
    assert client.post(url + 'forged').status_code == 404


def test_scheduler_due_times_dedup_and_failure(client, settings, monkeypatch):
    signup(client)
    bid = business(settings)['id']
    clock = datetime(2026, 11, 3, 13, 0, tzinfo=timezone.utc)
    jobs = scheduler.Scheduler(settings)
    scheduled = []
    monkeypatch.setattr(jobs, 'dispatch', lambda bid, kind, since=None: scheduled.append((bid, kind, since)))
    client.portal.call(jobs.tick, clock)
    assert scheduled == []  # unpaid and paused
    with connect(settings.db_path) as db:
        db.execute("UPDATE businesses SET plan='pro',subscription_status='active',outreach_paused=0")
    client.portal.call(jobs.tick, clock)
    assert [kind for _, kind, _ in scheduled] == ['search', 'send', 'poll']  # 08:00 Eastern, even though owner time is 20:00
    scheduled.clear()
    client.portal.call(jobs.tick, clock.replace(hour=2))
    assert [kind for _, kind, _ in scheduled] == ['send', 'poll']
    scheduled.clear()
    client.portal.call(jobs.tick, clock.replace(hour=6))
    assert [kind for _, kind, _ in scheduled] == ['poll']  # Quiet in every US zone
    jobs = scheduler.Scheduler(settings)
    run = jobs.claim(bid, 'search')
    assert jobs.claim(bid, 'search') is None
    alerts = []
    monkeypatch.setattr(notify, 'failure_alert', lambda *args: alerts.append(args))
    def fail(bid):
        raise ValueError('SECRET')
    monkeypatch.setattr(scheduler.search, 'run_search', fail)
    jobs.execute(run, bid, 'search')
    last = board.last_search(settings, bid)
    assert last['status'] == 'failed' and last['error'] == 'ValueError'
    assert alerts and 'SECRET' not in str(alerts)
    assert jobs.claim(bid, 'search', clock.replace(hour=0).isoformat()) is not None


def test_daily_summary_and_telegram_failures(client, settings, monkeypatch):
    signup(client)
    bid = business(settings)['id']
    pid = add_person(settings, bid)
    with connect(settings.db_path) as db:
        db.execute("INSERT INTO messages(id,person_id,direction,body,move,created_at) VALUES ('offer',?,'out','Offer','offer',?)", (pid, now()))
        db.execute("INSERT INTO messages(id,person_id,direction,body,created_at) VALUES ('reply',?,'in','Thanks',?)", (pid, now()))
        db.execute("INSERT INTO runs(id,business_id,kind,started_at,status,found) VALUES ('search',?,'search',?,'ok',4)", (bid, now()))
        db.execute("INSERT INTO payments(id,business_id,source,created_at) VALUES ('paid',?,'manual',?)", (bid, now()))
    sent = []
    monkeypatch.setattr(notify, 'telegram', lambda s, text: sent.append(text) or True)
    assert notify.daily_summary(settings)
    for part in ('Search ok, found 4', 'Letters: 1', 'Replies: 1', 'Offers: 1', 'Payments: 1', 'Errors: 0'):
        assert part in sent[0]


def test_summary_runs_at_nine_owner_time_and_once_per_day(client, settings, monkeypatch):
    settings.values.update({'TELEGRAM_BOT_TOKEN': 'fake', 'TELEGRAM_CHAT_ID': 'fake'})
    jobs = scheduler.Scheduler(settings)
    dispatched = []
    monkeypatch.setattr(jobs, 'dispatch', lambda *args: dispatched.append(args))
    client.portal.call(jobs.tick, datetime(2026, 11, 3, 1, 59, tzinfo=timezone.utc))
    assert dispatched == []
    client.portal.call(jobs.tick, datetime(2026, 11, 3, 2, 0, tzinfo=timezone.utc))
    assert dispatched == [(None, 'summary', '2026-11-02T17:00:00+00:00')]
    jobs = scheduler.Scheduler(settings)
    run = jobs.claim(None, 'summary', dispatched[0][2])
    monkeypatch.setattr(notify, 'daily_summary', lambda *args: True)
    jobs.execute(run, None, 'summary')
    assert jobs.claim(None, 'summary', dispatched[0][2]) is None


def test_telegram_failure_does_not_expose_token(client, settings, monkeypatch):
    settings.values.update({'TELEGRAM_BOT_TOKEN': 'SECRET-BOT', 'TELEGRAM_CHAT_ID': 'fake'})
    def fail(*args, **kwargs):
        raise RuntimeError('SECRET-BOT')
    monkeypatch.setattr(notify.httpx, 'post', fail)
    assert notify.telegram(settings, 'Summary') is False
    with connect(settings.db_path) as db:
        log = db.execute("SELECT detail FROM log WHERE event='telegram_failed'").fetchone()[0]
    assert 'SECRET-BOT' not in log


def test_search_run_context_and_interval_claims(client, settings, monkeypatch):
    from app.db import current_run_id
    from test_pricing import activate
    signup(client)
    activate(settings, 'pro')
    bid = business(settings)['id']
    jobs = scheduler.Scheduler(settings)
    run = jobs.claim(bid, 'search')
    contexts = []
    monkeypatch.setattr(scheduler.search, 'run_search', lambda bid: contexts.append(current_run_id.get()) or [])
    jobs.execute(run, bid, 'search')
    assert contexts == [run]
    assert current_run_id.get() is None
    since = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    assert jobs.claim(bid, 'search', since) is None


def test_every_owner_page_rejects_staff(client, settings):
    signup(client)
    pid = add_person(settings, business(settings)['id'])
    with connect(settings.db_path) as db:
        db.execute("UPDATE users SET role='staff'")
    for path in ('/app', '/app/settings', '/app/search-status', '/app/columns', '/app/people/' + pid):
        assert client.get(path).status_code == 403


def test_columns_refresh_counts_and_html_only(client, settings):
    signup(client)
    bid = business(settings)['id']
    pid = add_person(settings, bid, stage='offer_made')
    page = client.get('/app/columns', headers={'HX-Request': 'true'})
    assert page.status_code == 200
    assert 'id="board-columns"' in page.text
    assert '<html' not in page.text
    assert 'Offer made <span class="count">1</span>' in page.text
    assert 'Example Labs' not in page.text  # This fixture uses Acme Labs.
    assert 'Acme Labs' in page.text


def test_background_overlap_and_success_counts(client, settings, monkeypatch):
    from test_pricing import activate
    signup(client)
    activate(settings, 'pro')
    bid = business(settings)['id']
    jobs = scheduler.Scheduler(settings)
    async def check():
        import threading
        gate = threading.Event()
        entered = threading.Event()
        def find(bid):
            entered.set()
            gate.wait(timeout=5)
            return [{}]
        monkeypatch.setattr(scheduler.search, 'run_search', find)
        assert jobs.dispatch(bid, 'search') is True
        await asyncio.to_thread(entered.wait, 5)
        try:
            assert jobs.dispatch(bid, 'search') is False
        finally:
            gate.set()
        await asyncio.gather(*list(jobs.tasks))
    client.portal.call(check)
    assert board.last_search(settings, bid)['found'] == 1
    monkeypatch.setattr(scheduler.outreach, 'send_first_letters', lambda bid: 3)
    monkeypatch.setattr(scheduler.outreach, 'poll_replies', lambda bid: 2)
    for kind, column, expected in [('send', 'sent', 3), ('poll', 'found', 2)]:
        run = jobs.claim(bid, kind)
        jobs.execute(run, bid, kind)
        with connect(settings.db_path) as db:
            row = db.execute('SELECT * FROM runs WHERE id=?', (run,)).fetchone()
            assert row['status'] == 'ok' and row[column] == expected and row['finished_at']


def test_password_save_fails_closed_without_cipher(client, settings, monkeypatch):
    signup(client)
    def unavailable(*args):
        raise HTTPException(503, 'Mailbox password encryption is not installed')
    monkeypatch.setattr(board, 'encrypt_password', unavailable)
    fields = {'postal_address': '12 Main Street, Austin TX', 'daily_cap': '20',
              'quiet_start': '20:00', 'quiet_end': '08:00', 'smtp_password': 'NEVER-SAVE-PLAINTEXT'}
    response = form(client, '/app/settings', **fields)
    assert response.status_code == 503
    assert 'NEVER-SAVE-PLAINTEXT' not in response.text
    assert business(settings)['smtp_password_enc'] is None


def test_mailbox_cipher_roundtrip_nonce_and_integrity(settings):
    from cryptography.fernet import InvalidToken
    from app.settings import Settings, encrypt_password, decrypt_password
    secret = 'smtp-test-password-☃'
    first = encrypt_password(settings, secret)
    second = encrypt_password(settings, secret)
    assert first != second
    assert secret not in first
    assert decrypt_password(settings, first) == secret
    other = Settings(settings.values | {'SECRET_KEY': 'different-key-' * 4})
    with pytest.raises(InvalidToken):
        decrypt_password(other, first)
    tampered = first[:20] + ('A' if first[20] != 'A' else 'B') + first[21:]
    with pytest.raises(InvalidToken):
        decrypt_password(settings, tampered)


def test_mailbox_passwords_encrypted_preserved_and_not_echoed(client, settings):
    from app.settings import decrypt_password
    signup(client)
    fields = {'postal_address': '12 Main Street, Austin TX', 'daily_cap': '20',
              'quiet_start': '20:00', 'quiet_end': '08:00', 'outreach_paused': 'on',
              'smtp_password': 'SMTP-PRIVATE-PASSWORD', 'imap_password': 'IMAP-PRIVATE-PASSWORD'}
    assert form(client, '/app/settings', **fields).status_code == 303
    saved = business(settings)
    for protocol in ('smtp', 'imap'):
        ciphertext = saved[protocol + '_password_enc']
        assert ciphertext and fields[protocol + '_password'] not in ciphertext
        assert decrypt_password(settings, ciphertext) == fields[protocol + '_password']
        page = client.get('/app/settings').text
        assert ciphertext not in page and fields[protocol + '_password'] not in page
    # Empty fields keep both existing ciphertexts; updating one leaves the other intact.
    assert form(client, '/app/settings', **(fields | {'smtp_password': '', 'imap_password': ''})).status_code == 303
    kept = business(settings)
    assert kept['smtp_password_enc'] == saved['smtp_password_enc']
    assert kept['imap_password_enc'] == saved['imap_password_enc']
    assert form(client, '/app/settings', **(fields | {'smtp_password': 'REPLACEMENT', 'imap_password': ''})).status_code == 303
    replaced = business(settings)
    assert decrypt_password(settings, replaced['smtp_password_enc']) == 'REPLACEMENT'
    assert replaced['imap_password_enc'] == saved['imap_password_enc']
    with connect(settings.db_path) as db:
        logs = str([tuple(row) for row in db.execute('SELECT * FROM log')])
    assert 'SMTP-PRIVATE-PASSWORD' not in logs and 'IMAP-PRIVATE-PASSWORD' not in logs

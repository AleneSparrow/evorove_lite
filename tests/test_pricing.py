import asyncio
import re
from datetime import datetime, timezone

import pytest

from app import auth, billing, contacts, scheduler
from app.db import connect, now
from test_core import client, settings, signup, form, business


def activate(settings, plan='starter', status='active'):
    with connect(settings.db_path) as db:
        db.execute('UPDATE businesses SET plan=?,subscription_status=?,brief=?,outreach_paused=0', (plan, status, 'US founders with visible pain'))


@pytest.mark.parametrize('plan,status,allowed', [('starter','active',False), ('pro','active',True),
    ('pro','incomplete',False), ('pro','cancelled',False), ('pro','on_trial',False), (None,'active',False)])
def test_search_is_active_pro_only(client, settings, monkeypatch, plan, status, allowed):
    signup(client)
    activate(settings, plan, status)
    monkeypatch.setattr('app.main.search.run_search', lambda *args, **kwargs: [])
    page = client.get('/app')
    assert ('>Find people</button>' in page.text) == allowed
    if not allowed:
        assert 'Lead search is included in Pro' in page.text
        assert 'href="/app/billing"' in page.text
    for url in ('/app/search-preview', '/app/search-preview/status'):
        assert client.get(url).status_code == (200 if allowed else 403)
    for url in ('/app/find-people', '/app/search-preview'):
        assert form(client, '/app', url).status_code == (303 if allowed else 403)
    async def finish():
        await asyncio.gather(*list(client.app.state.scheduler.tasks), *list(client.app.state.search_preview_tasks))
    client.portal.call(finish)


@pytest.mark.parametrize('plan,expected', [('starter',['send','poll']), ('pro',['search','send','poll'])])
def test_scheduler_search_entitlement_keeps_email_jobs(client, settings, monkeypatch, plan, expected):
    signup(client)
    activate(settings, plan)
    jobs = scheduler.Scheduler(settings)
    calls = []
    monkeypatch.setattr(jobs, 'dispatch', lambda bid, kind, since=None: calls.append(kind))
    client.portal.call(jobs.tick, datetime(2026,11,3,13,0,tzinfo=timezone.utc))
    assert calls == expected


@pytest.mark.parametrize('plan', ['starter','pro'])
def test_add_one_uses_owner_evidence_and_cold_pipeline(client, settings, plan):
    signup(client)
    activate(settings, plan)
    response = form(client, '/app/contacts', email=' Alice@Example.com ', name='Alice',
                    company='Example', website='https://example.com', role='Owner', us_state='CA', note='Hiring follow-up is taking too long')
    assert response.status_code == 200 and 'Added: 1' in response.text
    with connect(settings.db_path) as db:
        row = dict(db.execute('SELECT * FROM people').fetchone())
        assert row['email'] == 'alice@example.com' and row['stage'] == 'cold'
        assert row['pain'] == row['evidence_quote'] == 'Hiring follow-up is taking too long'
        assert row['source_url'] == 'owner'
        assert db.execute('SELECT count(*) FROM messages').fetchone()[0] == 0
    assert 'Source: your note' in client.get('/app/people/' + row['id']).text
    assert 'Add contacts' in client.get('/app').text


def upload(client, text):
    csrf = re.search('name="csrf" value="([^"]+)"', client.get('/app/contacts').text)[1]
    return client.post('/app/contacts', data={'csrf':csrf}, files={'csv':('contacts.csv',text,'text/csv')})


def test_csv_report_validation_dedup_and_suppression(client, settings):
    signup(client)
    activate(settings)
    bid = business(settings)['id']
    with connect(settings.db_path) as db:
        db.execute('INSERT INTO suppressions VALUES (?,?,?,?)', (bid,'stop@example.com','unsubscribe',now()))
    text = '\ufeffemail,name,company,website,role,us_state,note\r\n'
    text += 'alice@example.com,Alice,"Example, Inc.",https://example.com,Founder,TX,"Hiring is slow, especially follow-up"\r\n'
    text += 'ALICE@EXAMPLE.COM,,,,,,Same contact appears twice\r\n'
    text += 'stop@example.com,,,,,,This person said stop\r\n'
    text += 'not-an-email,,,,,,Bad email is provided\r\n'
    text += 'empty@example.com,,,,,,\r\n'
    response = upload(client, text.encode('utf-8'))
    assert response.status_code == 200
    for value in ('Added: 1', 'Skipped: 4', 'Duplicate email','Suppressed email','Invalid email','Note is required'):
        assert value in response.text
    with connect(settings.db_path) as db:
        row = db.execute('SELECT company,pain FROM people').fetchone()
        assert tuple(row) == ('Example, Inc.', 'Hiring is slow, especially follow-up')
    assert 'Added: 0' in upload(client,text.encode()).text


def test_csv_limit_is_atomic_and_accepts_exactly_1000(client, settings):
    signup(client)
    activate(settings)
    rows = ['email,note'] + [f'person{n}@example.com,Real reason for reaching out' for n in range(1001)]
    assert upload(client, '\n'.join(rows)).status_code == 400
    with connect(settings.db_path) as db:
        assert db.execute('SELECT count(*) FROM people').fetchone()[0] == 0
    assert 'Added: 1000' in upload(client, '\n'.join(rows[:-1])).text


def test_contacts_are_tenant_scoped_and_owner_only(client, settings):
    signup(client)
    activate(settings)
    other_user = auth.signup(settings,'other@example.com','another long password','Other')
    with connect(settings.db_path) as db:
        other_bid = db.execute('SELECT business_id FROM users WHERE id=?',(other_user,)).fetchone()[0]
        db.execute('INSERT INTO suppressions VALUES (?,?,?,?)',(other_bid,'shared@example.com','unsubscribe',now()))
    assert 'Added: 1' in form(client,'/app/contacts',email='shared@example.com',note='A real owner note').text
    report = contacts.add(settings,other_bid,[{'email':'shared@example.com','note':'A real owner note'}])
    assert report['skipped'] == 1
    with connect(settings.db_path) as db:
        db.execute("UPDATE users SET role='staff'")
    assert client.get('/app/contacts').status_code == 403
    assert form(client,'/login','/app/contacts',email='x@example.com',note='A real owner note').status_code == 403


@pytest.mark.parametrize('text', ['name,note\nAlice,Real reason\n', 'email,email,note\na@b.com,a@b.com,Reason\n'])
def test_invalid_csv_headers_are_rejected(client, settings, text):
    signup(client)
    activate(settings)
    assert upload(client,text).status_code == 400


def test_contact_upload_rejects_missing_csrf_and_bad_utf8(client, settings):
    signup(client)
    activate(settings)
    assert client.post('/app/contacts',files={'csv':('x.csv',b'email,note\na@b.com,Reason')}).status_code == 403
    assert upload(client,b'email,note\na@b.com,\xff').status_code == 400


def test_portal_is_owner_scoped_and_validates_provider_url(client, settings, monkeypatch):
    signup(client)
    activate(settings)
    with connect(settings.db_path) as db:
        db.execute("UPDATE businesses SET ls_subscription_id='subscription-one'")
    import httpx
    urls = []
    def get(url, **kwargs):
        urls.append(url)
        return httpx.Response(200,json={'data':{'attributes':{'urls':{'customer_portal':'https://store.lemonsqueezy.com/billing/test'}}}},request=httpx.Request('GET',url))
    monkeypatch.setattr(billing.httpx,'get',get)
    response = form(client,'/app/billing','/billing/portal')
    assert response.status_code == 303 and response.headers['location'].startswith('https://store.lemonsqueezy.com/')
    assert urls == ['https://api.lemonsqueezy.com/v1/subscriptions/subscription-one']


def test_checkout_skips_trial(client, settings, monkeypatch):
    signup(client)
    import httpx
    payloads = []
    def post(url, **kwargs):
        payloads.append(kwargs['json'])
        return httpx.Response(201,json={'data':{'attributes':{'url':'https://store.lemonsqueezy.com/checkout/test'}}},request=httpx.Request('POST',url))
    monkeypatch.setattr(billing.httpx,'post',post)
    assert form(client,'/app','/billing/checkout/starter').status_code == 303
    assert payloads[0]['data']['attributes']['checkout_options']['skip_trial'] is True


def test_public_pricing_and_copy(client):
    for path in ('/','/faq'):
        text = client.get(path).text.lower()
        assert '$199' in text and '$499' in text and 'cancel anytime' in text
        for obsolete in ('seven-day','7-day','booked hour','calendar','booking','trained on sales books','already trained','upload photos'):
            assert obsolete not in text
    assert 'trial' not in client.get('/terms').text.lower()


def test_imported_starter_contact_follows_existing_first_letter_pipeline(client, settings):
    from app import outreach
    from app.settings import encrypt_password
    signup(client)
    activate(settings)
    bid = business(settings)['id']
    note = 'their team is losing time on manual follow up'
    assert 'Added: 1' in form(client,'/app/contacts',email='buyer@example.com',name='Buyer',us_state='NY',note=note).text
    with connect(settings.db_path) as db:
        db.execute('UPDATE businesses SET smtp_host=?,smtp_user=?,smtp_password_enc=?,postal_address=?,sender_name=?,quiet_start=?,quiet_end=? WHERE id=?',
                   ('smtp.example.com','sales@example.com',encrypt_password(settings,'test-mailbox-password'),
                    '123 Test Street, Austin TX','Acme Sales','20:00','08:00',bid))
    sent = []
    assert outreach.send_first_letters(bid,settings=settings,send=lambda target,message:sent.append(message),
                                       now_utc=datetime(2026,11,3,16,0,tzinfo=timezone.utc)) == 1
    assert note in sent[0].get_content()
    with connect(settings.db_path) as db:
        assert db.execute('SELECT stage FROM people').fetchone()[0] == 'in_progress'
        assert db.execute("SELECT count(*) FROM messages WHERE direction='out'").fetchone()[0] == 1


def test_contact_validation_reports_bad_website_and_empty_note(client, settings):
    signup(client)
    activate(settings)
    for patch, reason in [({'website':'http://[invalid'},'Website must be'), ({'note':''},'Note is required')]:
        response = form(client,'/app/contacts',**({'email':'safe@example.com','note':'Real reason to write'}|patch))
        assert response.status_code == 200 and 'Added: 0' in response.text and reason in response.text


def test_pro_daily_search_limit_excludes_owner_contacts(client, settings, monkeypatch):
    signup(client)
    activate(settings,'pro')
    bid = business(settings)['id']
    rows = [{'email':f'person{n}@example.com','note':'Real owner supplied pain'} for n in range(21)]
    contacts.add(settings,bid,rows)
    with connect(settings.db_path) as db:
        db.execute("UPDATE people SET source_url='https://example.com/evidence' WHERE email!='person20@example.com'")
    jobs = scheduler.Scheduler(settings)
    calls = []
    monkeypatch.setattr(scheduler.search,'run_search',lambda bid,**kwargs:calls.append(kwargs.get('limit',20)) or [])
    run = jobs.claim(bid,'search')
    jobs.execute(run,bid,'search')
    assert calls == []  # 20 found today; owner contact does not increase the search allowance.
    with connect(settings.db_path) as db:
        db.execute("UPDATE people SET source_url='owner' WHERE email='person19@example.com'")
    run = jobs.claim(bid,'search')
    jobs.execute(run,bid,'search')
    assert calls == [1]


def test_pro_search_limit_counts_the_owner_local_day_not_the_utc_day(client, settings, monkeypatch):
    """TIMEZONE_OWNER defaults to Asia/Ho_Chi_Minh (UTC+7): its day starts at 17:00 UTC."""
    signup(client)
    activate(settings, 'pro')
    bid = business(settings)['id']
    moment = datetime(2026, 11, 3, 18, 0, tzinfo=timezone.utc)  # 01:00 on 4 Nov for the owner

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return moment.astimezone(tz) if tz else moment.replace(tzinfo=None)

    monkeypatch.setattr(scheduler, 'datetime', Clock)
    calls = []
    monkeypatch.setattr(scheduler.search, 'run_search', lambda bid, **kwargs: calls.append(kwargs.get('limit', 20)) or [])

    def seed(created_at):
        with connect(settings.db_path) as db:
            db.execute('DELETE FROM people')
            for n in range(20):
                db.execute("INSERT INTO people(id,business_id,email,pain,evidence_quote,source_url,stage,created_at,updated_at) "
                           "VALUES (?,?,?,?,?,?,'cold',?,?)", (f'p{n}', bid, f'p{n}@example.com', 'pain', 'quote',
                                                                'https://example.com/evidence', created_at, created_at))

    def run_search_job():
        jobs = scheduler.Scheduler(settings)
        jobs.execute(jobs.claim(bid, 'search'), bid, 'search')

    seed('2026-11-03T10:00:00+00:00')  # UTC "today", but yesterday for the owner: a full allowance again
    run_search_job()
    assert calls == [20]
    seed('2026-11-03T17:30:00+00:00')  # after the owner's midnight: the allowance is spent
    run_search_job()
    assert calls == [20]


def test_pro_card_and_faq_promise_twenty_verified_leads_a_day(client):
    landing, faq = client.get('/').text, client.get('/faq').text
    assert 'Up to 20 verified new leads a day' in landing and 'around 20' not in landing
    assert 'up to 20 verified new leads a day' in faq and 'around 20' not in faq

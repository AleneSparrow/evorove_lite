from datetime import datetime, timezone

from fastapi.testclient import TestClient

from app.board import metrics
from app.db import connect, now
from app.main import create_app
from test_core import business, client, settings, signup

CURRENT = datetime(2026, 11, 3, 15, 0, tzinfo=timezone.utc)


def at(*parts):
    return datetime(*parts, tzinfo=timezone.utc).isoformat()


def add_person(db, bid, person_id, created):
    db.execute('INSERT INTO people (id,business_id,name,email,pain,evidence_quote,source_url,stage,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)',
               (person_id, bid, person_id, person_id + '@example.com', 'A real pain', 'A real quote', 'https://example.com', 'cold', created, created))


def add_message(db, person_id, message_id, direction, created, move=None):
    db.execute('INSERT INTO messages (id,person_id,direction,body,move,created_at) VALUES (?,?,?,?,?,?)',
               (message_id, person_id, direction, 'Hello', move, created))


def seed(settings):
    with connect(settings.db_path) as db:
        own = db.execute('SELECT id FROM businesses ORDER BY created_at').fetchone()[0]
        other = 'other-biz'
        db.execute('INSERT INTO businesses (id,name,created_at) VALUES (?,?,?)', (other, 'Other Co', now()))
        created = {
            'today-a': at(2026, 11, 3, 10), 'today-b': at(2026, 11, 3, 11),
            'week': at(2026, 10, 30, 12), 'month': at(2026, 10, 10, 12), 'old': at(2026, 9, 1, 12),
        }
        for person_id, created_at in created.items():
            add_person(db, own, person_id, created_at)
        add_person(db, other, 'foreign', at(2026, 11, 3, 10))
        add_message(db, 'today-a', 'm-greet', 'out', at(2026, 11, 3, 12), 'greet')
        add_message(db, 'today-a', 'm-reply', 'in', at(2026, 11, 3, 13))
        add_message(db, 'today-a', 'm-offer', 'out', at(2026, 11, 3, 14), 'OFFER')
        add_message(db, 'week', 'm-week-greet', 'out', at(2026, 10, 30, 13), 'greet')
        add_message(db, 'week', 'm-week-reply', 'in', at(2026, 10, 30, 14))
        add_message(db, 'month', 'm-month-greet', 'out', at(2026, 10, 10, 13), 'greet')
        add_message(db, 'today-b', 'm-later', 'out', at(2026, 11, 3, 12, 30), 'confirm_need')
        add_message(db, 'foreign', 'm-foreign', 'out', at(2026, 11, 3, 12), 'greet')
        db.execute('INSERT INTO payments (id,business_id,person_id,source,test_mode,created_at) VALUES (?,?,?,?,?,?)',
                   ('pay-live', own, 'today-a', 'manual', 0, at(2026, 11, 3, 16)))
        db.execute('INSERT INTO payments (id,business_id,person_id,source,test_mode,created_at) VALUES (?,?,?,?,?,?)',
                   ('pay-test', own, 'today-a', 'manual', 1, at(2026, 11, 3, 16, 1)))
        db.execute('INSERT INTO payments (id,business_id,person_id,source,test_mode,created_at) VALUES (?,?,?,?,?,?)',
                   ('pay-old', own, 'month', 'manual', 0, at(2026, 10, 10, 16)))
        db.execute('INSERT INTO payments (id,business_id,person_id,source,test_mode,created_at) VALUES (?,?,?,?,?,?)',
                   ('pay-foreign', other, 'foreign', 'manual', 0, at(2026, 11, 3, 16)))
        db.execute('INSERT INTO suppressions VALUES (?,?,?,?)', (own, 'today-a@example.com', 'unsubscribe', at(2026, 11, 3, 17)))
        db.execute('INSERT INTO suppressions VALUES (?,?,?,?)', (own, 'today-b@example.com', 'bounce', at(2026, 11, 3, 17)))
        db.execute('INSERT INTO suppressions VALUES (?,?,?,?)', (own, 'month@example.com', 'Unsubscribe', at(2026, 10, 10, 17)))
        db.execute('INSERT INTO suppressions VALUES (?,?,?,?)', (other, 'foreign@example.com', 'unsubscribe', at(2026, 11, 3, 17)))
        db.execute('INSERT INTO runs (id,business_id,kind,started_at,status,tokens_in,tokens_out) VALUES (?,?,?,?,?,?,?)',
                   ('run-search', own, 'search', at(2026, 11, 3, 9), 'ok', 10, 4))
        db.execute('INSERT INTO runs (id,business_id,kind,started_at,status,tokens_in,tokens_out) VALUES (?,?,?,?,?,?,?)',
                   ('run-send', own, 'send', at(2026, 11, 3, 18), 'ok', 3, 1))
        db.execute('INSERT INTO runs (id,business_id,kind,started_at,status,tokens_in,tokens_out) VALUES (?,?,?,?,?,?,?)',
                   ('run-old', own, 'search', at(2026, 10, 10, 9), 'ok', 100, 0))
        db.execute('INSERT INTO runs (id,business_id,kind,started_at,status,tokens_in,tokens_out) VALUES (?,?,?,?,?,?,?)',
                   ('run-foreign', other, 'search', at(2026, 11, 3, 9), 'ok', 99999, 0))
    return own


def column(report, label):
    index = report['columns'].index(label)
    return {row['label']: row['cells'][index] for row in report['rows']}


def test_funnel_counts_windows_and_ignores_other_tenants(settings):
    signup_business(settings)
    report = metrics(settings, seed(settings), CURRENT)
    today, week, month = (column(report, label) for label in ('Today', '7 days', '30 days'))
    assert [today[name]['count'] for name in ('Found', 'First letters', 'Replied', 'Reached an offer', 'Paid', 'Unsubscribed')] == [2, 1, 1, 1, 1, 1]
    assert [week[name]['count'] for name in ('Found', 'First letters', 'Replied', 'Reached an offer', 'Paid', 'Unsubscribed')] == [3, 2, 2, 1, 1, 1]
    assert [month[name]['count'] for name in ('Found', 'First letters', 'Replied', 'Reached an offer', 'Paid', 'Unsubscribed')] == [4, 3, 2, 1, 2, 2]
    assert [today['Tokens']['count'], today['Searches']['count']] == [18, 1]
    assert [week['Tokens']['count'], week['Searches']['count']] == [18, 1]
    assert [month['Tokens']['count'], month['Searches']['count']] == [118, 2]
    assert today['First letters']['rate'] == '50.0'
    assert week['First letters']['rate'] == '66.7'
    assert week['Reached an offer']['rate'] == '50.0'
    assert month['Paid']['rate'] == '200.0'
    assert today['Tokens']['rate'] is None and today['Found']['rate'] is None


def test_conversion_is_blank_when_the_previous_step_is_zero(settings):
    signup_business(settings)
    bid = business(settings)['id']
    with connect(settings.db_path) as db:
        add_person(db, bid, 'solo', at(2026, 11, 3, 10))
        add_message(db, 'solo', 'reply-only', 'in', at(2026, 11, 3, 11))
    cell = column(metrics(settings, bid, CURRENT), 'Today')['Replied']
    assert cell['count'] == 1 and cell['rate'] is None


def signup_business(settings):
    with TestClient(create_app(settings, scheduler_enabled=False), base_url='https://testserver', follow_redirects=False) as browser:
        signup(browser)


def test_metrics_page_shows_this_business_only(client, settings):
    signup(client)
    seed(settings)
    page = client.get('/app/metrics')
    assert page.status_code == 200
    assert 'What happened.' in page.text and 'First letters' in page.text and '50.0% of the previous step' in page.text
    assert '99999' not in page.text
    assert 'href="/app/metrics"' in client.get('/app').text
    with TestClient(create_app(settings, scheduler_enabled=False), base_url='https://testserver', follow_redirects=False) as anon:
        denied = anon.get('/app/metrics')
    assert denied.status_code == 303 and denied.headers['location'] == '/login'

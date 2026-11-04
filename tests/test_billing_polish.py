"""Billing UI, execution authorization, local scheduling and dependency regressions."""
from datetime import datetime, timezone
from importlib.metadata import version
from pathlib import Path

import pytest
from selectolax.lexbor import LexborHTMLParser

from app import scheduler
from app.db import connect
from test_core import client, settings, signup, business
from test_pricing import activate


@pytest.mark.parametrize('status,ends,access', [
    ('cancelled', '2099-01-01T00:00:00+00:00', True),
    ('cancelled', '2020-01-01T00:00:00+00:00', False),
    ('cancelled', None, False), ('on_trial', None, False), ('active', None, True),
])
def test_board_subscription_actions(client, settings, status, ends, access):
    signup(client)
    with connect(settings.db_path) as db:
        db.execute('UPDATE businesses SET subscription_status=?,subscription_ends_at=?', (status, ends))
    html = client.get('/app').text
    assert ('Start your subscription' in html) == (not access)
    assert ('/billing/checkout/' in html) == (not access)
    assert ('Subscription ends 2099-01-01' in html) == (access and status == 'cancelled')
    assert ('href="/billing/portal"' in html) == (access and status == 'cancelled')


@pytest.mark.parametrize('plan,status', [('starter', 'active'), ('pro', 'expired'), ('pro', 'on_trial')])
def test_execute_search_checks_access_itself(client, settings, monkeypatch, plan, status):
    signup(client)
    with connect(settings.db_path) as db:
        db.execute('UPDATE businesses SET plan=?,subscription_status=?', (plan, status))
    bid = business(settings)['id']
    jobs = scheduler.Scheduler(settings)
    run = jobs.claim(bid, 'search')
    monkeypatch.setattr(scheduler.search, 'run_search', lambda *a, **kw: pytest.fail('Search must be blocked'))
    jobs.execute(run, bid, 'search')
    with connect(settings.db_path) as db:
        assert db.execute('SELECT status FROM runs WHERE id=?', (run,)).fetchone()[0] == 'skipped'
        row = db.execute("SELECT detail FROM log WHERE event='search_skipped' AND business_id=?", (bid,)).fetchone()
        assert 'Pro with subscription access' in row[0]


@pytest.mark.parametrize('stamp,status,blocked', [
    ('2026-11-03T16:59:59+00:00', 'ok', False),
    ('2026-11-03T17:00:00+00:00', 'ok', True),
    ('2026-11-04T00:01:00+00:00', 'failed', False),
    ('2026-11-04T17:00:00+00:00', 'ok', False),
])
def test_scheduled_search_only_success_in_owner_day(client, settings, monkeypatch, stamp, status, blocked):
    signup(client)
    activate(settings, 'pro')
    bid = business(settings)['id']
    jobs = scheduler.Scheduler(settings)
    with connect(settings.db_path) as db:
        db.execute('INSERT INTO runs(id,business_id,kind,started_at,status) VALUES (?,?,?,?,?)',
                   ('old', bid, 'search', stamp, status))
    scheduled = []
    def dispatch(bid, kind, since=None):
        if kind == 'search':
            assert since == '2026-11-03T17:00:00+00:00'
            scheduled.append(jobs.claim(bid, kind, since))
    monkeypatch.setattr(jobs, 'dispatch', dispatch)
    client.portal.call(jobs.tick, datetime(2026, 11, 4, 13, tzinfo=timezone.utc))
    assert (scheduled[0] is None) == blocked


def test_readme_access_contract():
    text = Path('README.md').read_text()
    assert 'active/on_trial' not in text
    assert 'billing.entitled' in text and 'subscription_ends_at в будущем' in text


def test_direct_dependencies_pinned_and_lexbor_works():
    for line in Path('requirements.txt').read_text().splitlines():
        name, pinned = line.split('==')
        assert version(name) == pinned
    assert LexborHTMLParser('<p>Lexbor works</p>').css_first('p').text() == 'Lexbor works'

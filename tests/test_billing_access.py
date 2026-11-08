"""Regression coverage for paid-through cancellation and unsafe owner notes."""
import csv
import io
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app import billing, scheduler
from app.db import connect, migrate
from test_core import client, settings, signup, business, event, deliver, form
from test_pricing import activate, upload


@pytest.mark.parametrize('plan', ['starter', 'pro'])
@pytest.mark.parametrize('ends,allowed', [
    ('2099-01-01T07:00:00+07:00', True),
    ('2026-11-03T13:00:00Z', False),
    ('2020-01-01T00:00:00Z', False),
    (None, False), ('invalid', False), ('2099-01-01T00:00:00', False),
])
def test_cancelled_access_across_webhook_import_search_and_scheduler(client, settings, monkeypatch, plan, ends, allowed):
    signup(client)
    activate(settings, plan)
    snapshot = event(business(settings)['id'], status='cancelled')
    snapshot['meta']['event_name'] = 'subscription_cancelled'
    snapshot['data']['attributes'].update(ends_at=ends, variant_id=200 if plan == 'pro' else 100)
    assert deliver(client, settings, snapshot).status_code == 200
    row = business(settings)
    moment = datetime(2026, 11, 3, 13, tzinfo=timezone.utc)
    assert billing.entitled(row, moment) == allowed
    assert billing.can_search(row, moment) == (allowed and plan == 'pro')
    assert billing.can_search(row) == (allowed and plan == 'pro')
    assert row['subscription_ends_at'] == (billing.utc_date(ends).isoformat() if billing.utc_date(ends) else None)
    response = form(client, '/app/contacts', email='buyer@example.com', note='Manual follow up takes too long')
    assert response.status_code == (200 if allowed else 403)
    jobs = scheduler.Scheduler(settings)
    calls = []
    monkeypatch.setattr(jobs, 'dispatch', lambda bid, kind, since=None: calls.append(kind))
    client.portal.call(jobs.tick, moment)
    assert calls == ((['search'] if plan == 'pro' else []) + ['send', 'poll'] if allowed else [])
    assert client.get('/app/search-preview/status').status_code == (200 if allowed and plan == 'pro' else 403)


def test_scheduler_runs_send_and_search_for_client_zero_without_a_subscription(client, settings, monkeypatch):
    # Client 0 (Evorove selling its own subscription) never has a real Lemon
    # Squeezy subscription or a 'pro' plan of its own, but it must still run.
    signup(client)
    zero = business(settings)['id']
    settings.values['CLIENT_ZERO_BUSINESS_ID'] = zero
    with connect(settings.db_path) as db:
        db.execute("UPDATE businesses SET outreach_paused=0,brief='x' WHERE id=?", (zero,))
    moment = datetime(2026, 11, 3, 13, tzinfo=timezone.utc)
    jobs = scheduler.Scheduler(settings)
    calls = []
    monkeypatch.setattr(jobs, 'dispatch', lambda bid, kind, since=None: calls.append((bid, kind)))
    client.portal.call(jobs.tick, moment)
    assert calls == [(zero, 'search'), (zero, 'send'), (zero, 'poll')]


def test_scheduler_does_not_run_for_an_ordinary_business_without_a_subscription(client, settings, monkeypatch):
    signup(client)
    bid = business(settings)['id']
    settings.values['CLIENT_ZERO_BUSINESS_ID'] = 'some-other-business'
    with connect(settings.db_path) as db:
        db.execute("UPDATE businesses SET outreach_paused=0,brief='x' WHERE id=?", (bid,))
    moment = datetime(2026, 11, 3, 13, tzinfo=timezone.utc)
    jobs = scheduler.Scheduler(settings)
    calls = []
    monkeypatch.setattr(jobs, 'dispatch', lambda bid, kind, since=None: calls.append((bid, kind)))
    client.portal.call(jobs.tick, moment)
    assert calls == []


def test_cancelled_deadline_expires_without_expired_webhook_and_resume_clears_it(client, settings):
    signup(client)
    snapshot = event(business(settings)['id'], status='cancelled')
    snapshot['data']['attributes']['ends_at'] = '2026-11-04T00:00:00Z'
    assert deliver(client, settings, snapshot).status_code == 200
    row = business(settings)
    assert billing.entitled(row, datetime(2026, 11, 3, tzinfo=timezone.utc))
    assert not billing.entitled(row, datetime(2026, 11, 4, tzinfo=timezone.utc))
    snapshot['meta']['event_name'] = 'subscription_resumed'
    snapshot['data']['attributes'].update(status='active', updated_at='2026-11-03T11:00:00Z')
    assert deliver(client, settings, snapshot).status_code == 200
    row = business(settings)
    assert row['subscription_ends_at'] is None and billing.entitled(row)
    # A stale cancellation must not restore the old deadline.
    stale = event(row['id'], status='cancelled')
    stale['data']['attributes']['ends_at'] = '2099-01-01T00:00:00Z'
    deliver(client, settings, stale)
    assert business(settings)['subscription_ends_at'] is None


@pytest.mark.parametrize('status', ['paused', 'past_due', 'unpaid', 'expired', 'on_trial'])
def test_other_statuses_do_not_grant_access(client, settings, status):
    signup(client)
    row = business(settings) | {'subscription_status': status, 'subscription_ends_at': '2099-01-01T00:00:00Z'}
    assert not billing.entitled(row)


@pytest.mark.parametrize('mode', ['manual', 'csv'])
@pytest.mark.parametrize('note', ['Visit https://example.com', 'See www.example.com',
    'Email owner@example.com', 'Call +1 212 555 1234', 'Save 20% today',
    'Pay $100 today', 'Offer a discount today', 'We guarantee better sales'])
def test_unsafe_notes_are_skipped_without_inserting(client, settings, mode, note):
    signup(client)
    activate(settings)
    if mode == 'manual':
        response = form(client, '/app/contacts', email='buyer@example.com', note=note)
    else:
        stream = io.StringIO()
        csv.writer(stream).writerows([['email', 'note'], ['buyer@example.com', note],
                                    ['safe@example.com', 'Manual follow up takes too long']])
        response = upload(client, stream.getvalue())
    assert response.status_code == 200
    assert 'Skipped: 1' in response.text
    assert 'Note contains a link, price or discount — remove it' in response.text
    assert f'Added: {int(mode == "csv")}' in response.text
    with connect(settings.db_path) as db:
        assert db.execute("SELECT count(*) FROM people WHERE email='buyer@example.com'").fetchone()[0] == 0


def test_migration_preserves_existing_business_and_is_repeatable(client, settings):
    signup(client)
    row = business(settings)
    migrate(settings.db_path)
    assert business(settings) == row
    assert 'subscription_ends_at' in row and row['subscription_ends_at'] is None


def test_playwright_version_is_pinned():
    assert 'playwright==1.63.0' in Path('requirements-dev.txt').read_text().splitlines()

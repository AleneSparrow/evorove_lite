"""A cancelled subscription whose paid period is over can be replaced, even if
subscription_expired never arrived; one still inside its period cannot."""
import httpx
import pytest

from app import billing
from app.db import connect
from test_core import client, settings, signup, business, event, deliver, form

PAST, FUTURE = '2020-01-01T00:00:00+00:00', '2099-01-01T00:00:00+00:00'


def set_state(settings, status, ends, subscription='sub-old'):
    with connect(settings.db_path) as db:
        db.execute('UPDATE businesses SET plan=?,subscription_status=?,subscription_ends_at=?,ls_subscription_id=?',
                   ('pro', status, ends, subscription))


@pytest.mark.parametrize('status,ends,status_code', [
    ('cancelled', PAST, 303), ('cancelled', None, 303), ('expired', None, 303),
    ('cancelled', FUTURE, 409), ('active', None, 409), ('on_trial', None, 409),
])
def test_checkout_is_blocked_only_while_access_lasts(client, settings, monkeypatch, status, ends, status_code):
    signup(client)
    set_state(settings, status, ends)
    monkeypatch.setattr(billing.httpx, 'post', lambda url, **kw: httpx.Response(
        201, json={'data': {'attributes': {'url': 'https://store.lemonsqueezy.com/checkout/new'}}},
        request=httpx.Request('POST', url)))
    assert form(client, '/app', '/billing/checkout/starter').status_code == status_code


@pytest.mark.parametrize('ends,replaced', [(PAST, True), (FUTURE, False)])
def test_new_subscription_replaces_only_a_lapsed_cancelled_one(client, settings, ends, replaced):
    signup(client)
    set_state(settings, 'cancelled', ends)
    new = event(business(settings)['id'], stamp='2026-11-10T10:00:00Z')
    new['data']['id'] = 'sub-new'
    assert deliver(client, settings, new).status_code == 200
    row = business(settings)
    assert (row['ls_subscription_id'], row['subscription_status'], row['subscription_ends_at']) == (
        ('sub-new', 'active', None) if replaced else ('sub-old', 'cancelled', ends))

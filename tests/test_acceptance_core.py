import pytest

from app import auth, billing
from app.db import connect, now
from test_core import client, settings, signup, business, event, deliver, form
from test_app import add_person


def test_b1_same_origin_referrer_policy_and_reject_null_origin(client):
    page = client.get('/signup')
    assert page.headers['referrer-policy'] == 'same-origin'
    import re
    csrf = re.search('name="csrf" value="([^"]+)"', page.text)[1]
    fields = {'csrf': csrf, 'email': 'browser@example.com', 'password': 'test password 123', 'business_name': 'Browser'}
    assert client.post('/signup', data=fields, headers={'Origin': 'null'}).status_code == 403
    assert client.post('/signup', data=fields, headers={'Origin': 'https://other.example'}).status_code == 403
    assert client.post('/signup', data=fields, headers={'Origin': 'https://testserver'}).status_code == 303


def test_b2_csp_allows_only_self_and_lemon_squeezy_form_targets(client):
    policy = client.get('/signup').headers['content-security-policy']
    directives = dict(part.strip().split(' ', 1) for part in policy.split(';') if part.strip())
    assert directives['form-action'].split() == ["'self'", 'https://*.lemonsqueezy.com']
    assert directives['default-src'] == "'self'"


@pytest.mark.parametrize('business_subscription', [None, 'evorove-subscription'])
@pytest.mark.parametrize('initial_status', ['incomplete', 'active'])
@pytest.mark.parametrize('name,kind', [('order_created', 'orders'), ('subscription_payment_success', 'subscription-invoices')])
def test_b3_offer_payment_ignores_business_subscription(client, settings, initial_status, name, kind, business_subscription):
    signup(client)
    bid = business(settings)['id']
    pid = add_person(settings, bid, stage='offer_made')
    with connect(settings.db_path) as db:
        db.execute('UPDATE businesses SET plan=?,subscription_status=?,ls_subscription_id=?,ls_customer_id=? WHERE id=?',
                   ('pro', initial_status, business_subscription, 'evorove-customer', bid))
    before = business(settings)
    attributes = {'total': 12000, 'updated_at': now(), 'subscription_id': 'customers-own-subscription'}
    payload = {'meta': {'event_name': name, 'custom_data': {'business_id': bid, 'person_id': pid}},
               'data': {'type': kind, 'id': 'customer-payment', 'attributes': attributes}}
    assert deliver(client, settings, payload).status_code == 200
    assert deliver(client, settings, payload).status_code == 200
    assert business(settings) == before
    with connect(settings.db_path) as db:
        assert db.execute('SELECT stage FROM people WHERE id=?', (pid,)).fetchone()[0] == 'done'
        assert db.execute('SELECT count(*) FROM payments WHERE person_id=?', (pid,)).fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM log WHERE person_id=? AND event='payment_recorded'", (pid,)).fetchone()[0] == 1


@pytest.mark.parametrize('name', sorted(billing.EVENTS))
def test_b3_customer_subscription_snapshot_cannot_change_business(client, settings, name):
    signup(client)
    bid = business(settings)['id']
    pid = add_person(settings, bid, stage='offer_made')
    before = business(settings)
    payload = event(bid)
    payload['meta']['event_name'] = name
    payload['meta']['custom_data']['person_id'] = pid
    assert deliver(client, settings, payload).status_code == 200
    assert business(settings) == before
    with connect(settings.db_path) as db:
        assert db.execute('SELECT stage FROM people WHERE id=?', (pid,)).fetchone()[0] == 'offer_made'
        assert db.execute('SELECT count(*) FROM payments').fetchone()[0] == 0


def test_b3_customer_payment_cannot_credit_another_tenant(client, settings):
    signup(client)
    bid = business(settings)['id']
    auth.signup(settings, 'other@example.com', 'another long password', 'Other')
    with connect(settings.db_path) as db:
        other = db.execute('SELECT id FROM businesses WHERE id!=?', (bid,)).fetchone()[0]
    pid = add_person(settings, other, stage='offer_made')
    payload = {'meta': {'event_name': 'order_created', 'custom_data': {'business_id': bid, 'person_id': pid}},
               'data': {'type': 'orders', 'id': 'foreign-order', 'attributes': {'total': 100, 'updated_at': now()}}}
    assert deliver(client, settings, payload).status_code == 200
    with connect(settings.db_path) as db:
        assert db.execute('SELECT stage FROM people WHERE id=?', (pid,)).fetchone()[0] == 'offer_made'
        assert db.execute('SELECT count(*) FROM payments').fetchone()[0] == 0


@pytest.mark.parametrize('stage', ['cold', 'in_progress', 'offer_made', 'done', 'lost'])
def test_record_payment_available_only_after_offer(client, settings, stage):
    signup(client)
    pid = add_person(settings, business(settings)['id'], stage=stage)
    page = client.get('/app/people/' + pid)
    assert ('Record payment' in page.text) == (stage == 'offer_made')
    response = form(client, '/app', '/app/people/' + pid + '/payment', confirm='yes')
    assert response.status_code == (200 if stage in {'offer_made', 'done'} else 409)
    with connect(settings.db_path) as db:
        assert db.execute('SELECT count(*) FROM payments').fetchone()[0] == (1 if stage == 'offer_made' else 0)

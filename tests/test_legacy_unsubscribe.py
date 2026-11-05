import base64
import hashlib
import hmac
import json

import pytest

from app.db import connect
from test_core import client, settings, signup, business

KEY = 'synthetic-legacy-key-for-tests-only'


def old_token(bid, email, key=KEY):
    # Exact encoding/signature from old email_outreach_service.unsubscribe_token.
    body = base64.urlsafe_b64encode(json.dumps([bid, email.strip().casefold()]).encode()).decode().rstrip('=')
    return body + '.' + hmac.new(f'unsubscribe:{key}'.encode(), body.encode(), hashlib.sha256).hexdigest()[:32]


def test_valid_and_repeated_legacy_unsubscribe(client, settings):
    signup(client)
    bid = business(settings)['id']
    settings.values['LEGACY_ACCOUNT_SECURITY_ENCRYPTION_KEY'] = KEY
    with connect(settings.db_path) as db:
        db.execute('INSERT INTO legacy_business_map VALUES (?,?)', ('old-client-zero', bid))
    url = '/api/v1/public/unsubscribe/' + old_token('old-client-zero', 'ALICE@example.test')
    response = client.get(url)
    assert response.status_code == 200 and url in response.text
    with connect(settings.db_path) as db:
        assert db.execute('SELECT count(*) FROM suppressions').fetchone()[0] == 0
    for _ in range(2):
        response = client.post(url, data={'List-Unsubscribe': 'One-Click'})
        assert response.status_code == 200 and 'You are unsubscribed' in response.text
    with connect(settings.db_path) as db:
        rows = db.execute('SELECT business_id,email FROM suppressions').fetchall()
        assert [tuple(row) for row in rows] == [(bid, 'alice@example.test')]


@pytest.mark.parametrize('mode', ['broken', 'missing_key', 'wrong_key', 'unknown_business', 'unicode'])
@pytest.mark.parametrize('method', ['get', 'post'])
def test_unrecognized_legacy_link(client, settings, mode, method):
    settings.values['LEGACY_ACCOUNT_SECURITY_ENCRYPTION_KEY'] = '' if mode == 'missing_key' else KEY
    token = old_token('unknown', 'alice@example.test')
    if mode == 'broken':
        token = 'broken.token'
    elif mode == 'wrong_key':
        token = old_token('unknown', 'alice@example.test', 'other-key')
    elif mode == 'unicode':
        token = 'bad.неверно'
    response = getattr(client, method)('/api/v1/public/unsubscribe/' + token)
    assert response.status_code == 400
    assert 'Ссылка не распознана' in response.text and 'STOP' in response.text
    assert 'Traceback' not in response.text and KEY not in response.text


def test_legacy_key_is_optional_in_preflight(capsys):
    from deploy.checks import REQUIRED_ENV, OPTIONAL_ENV, check_environment
    name = 'LEGACY_ACCOUNT_SECURITY_ENCRYPTION_KEY'
    assert name in OPTIONAL_ENV and name not in REQUIRED_ENV
    assert check_environment(name + '=', '\n'.join(key + '=synthetic' for key in REQUIRED_ENV))
    assert 'INFO optional not set: ' + name in capsys.readouterr().out

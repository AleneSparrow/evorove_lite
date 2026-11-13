"""Password visibility and HTML bodies for failed form posts."""
import re
from pathlib import Path

from app import auth
from test_core import client, form, settings, signup

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / 'app' / 'templates'
INLINE_SCRIPT = re.compile(r'<script\b([^>]*)>', re.I)
EVENT_ATTRIBUTE = re.compile(r'\son[a-z]+\s*=', re.I)


def test_wrong_password_rerenders_the_login_page(client, caplog):
    email = 'owner@example.com'
    password = 'not-the-real-password'
    signup(client)
    response = form(client, '/login', email=email, password=password)
    assert response.status_code == 401
    assert 'text/html' in response.headers['content-type']
    assert 'Invalid email or password' in response.text
    assert 'role="alert"' in response.text
    assert f'value="{email}"' in response.text
    assert 'type="password"' in response.text
    assert password not in response.text
    assert '{"detail"' not in response.text
    assert 'type="button"' in response.text
    assert 'aria-label="Show password"' in response.text
    token = re.search(r'name="csrf" value="([^"]+)"', response.text)[1]
    again = client.post('/login', data={'csrf': token, 'email': email, 'password': password})
    assert again.status_code == 401
    assert 'Reload the page and try again' not in again.text
    assert password not in caplog.text
    assert email not in caplog.text


def test_quoted_email_is_escaped_on_the_login_error(client):
    response = form(client, '/login', email='a"<b>@example.com', password='long enough xx')
    assert response.status_code == 401
    assert 'value="a&#34;&lt;b&gt;@example.com"' in response.text
    assert '<b>' not in response.text
    assert 'long enough xx' not in response.text


def test_short_signup_password_is_html(client):
    response = form(
        client, '/signup', email='new@example.com', password='short',
        business_name='<script>alert(1)</script>')
    assert response.status_code == 400
    assert 'text/html' in response.headers['content-type']
    assert 'Password must contain 12–1024 characters' in response.text
    assert 'role="alert"' in response.text
    assert 'value="new@example.com"' in response.text
    assert '>short<' not in response.text and 'value="short"' not in response.text
    assert 'alert(1)' not in response.text
    assert '{"detail"' not in response.text


def test_login_lockout_is_html(client, settings):
    email = 'owner@example.com'
    signup(client)
    for _ in range(9):
        auth.record_login_failure(settings, 'testclient', email)
    response = form(client, '/login', email=email, password='wrong password 456')
    assert response.status_code == 429
    assert 'text/html' in response.headers['content-type']
    assert 'Too many attempts. Try again in 15 minutes' in response.text
    assert 'role="alert"' in response.text
    assert f'value="{email}"' in response.text
    assert 'wrong password 456' not in response.text
    assert '{"detail"' not in response.text


def test_webhook_and_health_stay_json_and_htmx_stays_a_fragment(client):
    webhook = client.post('/billing/webhook', content=b'{}', headers={'x-signature': 'nope'})
    assert webhook.status_code == 401
    assert webhook.headers['content-type'].startswith('application/json')
    assert webhook.json()['detail'] == 'Invalid signature'
    alias = client.post('/api/v1/billing/webhook', content=b'{}', headers={'x-signature': 'nope'})
    assert alias.status_code == 401
    assert alias.headers['content-type'].startswith('application/json')
    oversized = client.post('/billing/webhook', content=b'x' * 300000, headers={'x-signature': 'nope'})
    assert oversized.status_code == 413
    assert oversized.headers['content-type'].startswith('application/json')
    health = client.post('/health')
    assert health.status_code == 405
    assert health.headers['content-type'].startswith('application/json')
    fragment = client.get('/no-such-page', headers={'HX-Request': 'true'})
    assert fragment.status_code == 404
    assert '<h1>Page not found</h1>' in fragment.text
    assert '<html' not in fragment.text.lower()
    page = client.get('/login')
    token = re.search(r'name="csrf" value="([^"]+)"', page.text)[1]
    htmx = client.post(
        '/login', data={'csrf': token, 'email': 'a@example.com', 'password': 'long enough xx'},
        headers={'HX-Request': 'true'})
    assert htmx.status_code == 401
    assert '<html' not in htmx.text.lower()
    assert 'Invalid email or password' in htmx.text


def test_settings_and_contacts_errors_rerender_html(client):
    signup(client)
    settings_page = form(
        client, '/app/settings', postal_address='short', daily_cap='20',
        quiet_start='20:00', quiet_end='08:00', smtp_password='mailbox-secret-value',
        offer_text='UNIQUEOFFER123')
    assert settings_page.status_code == 400
    assert 'Postal address must contain 10–1000 characters' in settings_page.text
    assert 'role="alert"' in settings_page.text
    assert 'mailbox-secret-value' not in settings_page.text
    assert 'UNIQUEOFFER123' not in settings_page.text
    assert 'type="password" name="smtp_password" autocomplete="new-password" value=""' in settings_page.text
    contacts_page = form(
        client, '/app/contacts', email='person@example.com', note='A concrete pain for this person')
    assert contacts_page.status_code == 403
    assert 'An active Starter or Pro subscription is required' in contacts_page.text
    assert 'role="alert"' in contacts_page.text
    assert 'person@example.com' not in contacts_page.text
    assert 'A concrete pain for this person' not in contacts_page.text


def test_password_toggle_markup_has_no_inline_script_or_handler():
    login = (TEMPLATES / 'auth.html').read_text()
    settings_html = (TEMPLATES / 'settings.html').read_text()
    assert login.count('type="button"') == 1
    assert settings_html.count('type="button"') == 1
    assert 'aria-label="Show password"' in login
    assert 'aria-label="Show password"' in settings_html
    assert 'aria-pressed="false"' in login
    assert 'src="/static/password-toggle.js"' in login
    assert 'src="/static/password-toggle.js"' in settings_html
    for path in sorted(TEMPLATES.glob('*.html')):
        text = path.read_text()
        for attrs in INLINE_SCRIPT.findall(text):
            assert re.search(r'\bsrc\s*=', attrs), path
        assert EVENT_ATTRIBUTE.search(text) is None, f'{path.name} has an inline event handler'

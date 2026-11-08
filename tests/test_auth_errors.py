"""Auth failures reopen the night-theme form instead of returning JSON."""
import pytest

from test_core import client, form, settings, signup

SECRET = 'should-not-appear-in-html'


def assert_form(response, status, message):
    assert response.status_code == status
    assert response.headers['content-type'].startswith('text/html')
    assert 'class="form-error"' in response.text
    assert f'role="alert">{message}' in response.text
    assert '{"detail"' not in response.text
    assert SECRET not in response.text
    assert '/static/style.css' in response.text
    assert 'name="password"' not in response.text or 'value="' + SECRET not in response.text


@pytest.mark.parametrize('path,extra', [
    ('/login', {'email': 'ada@example.com'}),
    ('/signup', {'email': 'ada@example.com', 'business_name': 'Northwind'}),
    ('/forgot-password', {'email': 'ada@example.com'}),
    ('/reset', {'token': 'reset-token-value'}),
])
def test_csrf_reopens_the_auth_form(client, path, extra):
    response = client.post(path, data={'password': SECRET, **extra})
    assert_form(response, 403, 'Reload the page and try again')
    if 'email' in extra:
        assert f'value="{extra["email"]}"' in response.text
    if path == '/signup':
        assert 'value="Northwind"' in response.text
    if path == '/reset':
        assert 'value="reset-token-value"' in response.text
    assert 'type="password"' not in response.text or 'value="' + SECRET not in response.text


def test_login_keeps_the_email_and_drops_the_password(client):
    signup(client)
    response = form(client, '/login', email='owner@example.com', password=SECRET)
    assert_form(response, 401, 'Invalid email or password')
    assert 'value="owner@example.com"' in response.text
    assert 'Welcome back' in response.text


def test_invalid_email_is_kept(client):
    response = form(client, '/login', email='not-an-email', password=SECRET)
    assert_form(response, 400, 'Enter a valid email address')
    assert 'value="not-an-email"' in response.text


def test_email_is_escaped_in_the_form(client):
    response = form(client, '/login', email='<script>alert(1)</script>@x.com', password=SECRET)
    assert response.status_code == 401
    assert 'value="&lt;script&gt;alert(1)&lt;/script&gt;@x.com"' in response.text
    assert '<script>' not in response.text
    assert SECRET not in response.text


def test_signup_keeps_the_email_and_business_name(client):
    response = form(client, '/signup', email='ada@example.com', password='too-short', business_name='Northwind')
    assert response.status_code == 400
    assert 'Password must contain 12\u20131024 characters' in response.text
    assert 'value="ada@example.com"' in response.text
    assert 'value="Northwind"' in response.text
    assert 'too-short' not in response.text


def test_reset_keeps_the_token_and_drops_the_password(client):
    response = form(client, '/reset', token='expired-token', password='another password secret')
    assert_form(response, 400, 'Invalid or expired reset link')
    assert 'value="expired-token"' in response.text
    assert 'another password secret' not in response.text


def test_login_rate_limit_reopens_the_form(client):
    signup(client)
    for _ in range(9):
        assert form(client, '/login', email='owner@example.com', password=SECRET).status_code == 401
    blocked = form(client, '/login', email='owner@example.com', password=SECRET)
    assert_form(blocked, 429, 'Too many attempts. Try again in 15 minutes')
    assert 'value="owner@example.com"' in blocked.text


def test_forgot_password_rate_limit_reopens_the_form(client):
    for _ in range(8):
        assert form(client, '/forgot-password', email='absent@example.com').status_code == 200
    blocked = form(client, '/forgot-password', email='absent@example.com')
    assert_form(blocked, 429, 'Too many attempts. Try again in 15 minutes')
    assert 'value="absent@example.com"' in blocked.text


def test_quoted_token_cannot_break_out_of_the_attribute(client):
    response = form(client, '/reset', token='"><script>', password='another password secret')
    assert response.status_code == 400
    assert '<script>' not in response.text
    assert '&#34;&gt;&lt;script&gt;' in response.text


def test_other_forms_still_return_json(client):
    denied = client.post('/logout', data={'email': 'ada@example.com', 'password': SECRET})
    assert denied.status_code == 403
    assert denied.headers['content-type'].startswith('application/json')
    assert denied.json()['detail'] == 'Reload the page and try again'

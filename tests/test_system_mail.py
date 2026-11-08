"""System mail is multipart, branded, and free of external resources."""
import re
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path
from urllib.parse import urlparse

from app import auth, system_mail
from app.db import connect
from test_core import client, form, settings, signup

ROOT = Path(__file__).resolve().parents[1]
PREVIEW = ROOT / 'docs' / 'screenshots' / 'email-reset.html'
HTTP_URL = re.compile(r'https?://[^\s<>"\']+', re.I)


def parts(message):
    assert isinstance(message, EmailMessage)
    assert message.get_content_type() == 'multipart/alternative'
    plain = message.get_body(preferencelist=('plain',))
    html = message.get_body(preferencelist=('html',))
    assert plain.get_content_type() == 'text/plain'
    assert html.get_content_type() == 'text/html'
    return plain.get_content(), html.get_content()


def test_reset_mail_has_the_link_in_text_and_html_only(settings):
    settings.values['APP_BASE_URL'] = 'https://evorove.com'
    settings.values['OPERATOR_NAME'] = 'Ada & Co'
    message = system_mail.reset_message(
        settings, 'owner@example.com', 'a b', expires_minutes=auth.RESET_MINUTES)
    plain, html = parts(message)
    url = 'https://evorove.com/reset?token=a%20b'
    assert url in plain and url in html
    assert f'Link expires in {auth.RESET_MINUTES} minutes' in plain
    assert f'Link expires in {auth.RESET_MINUTES} minutes' in html
    assert 'If you didn\'t request this, ignore this email' in plain
    assert 'If you didn\'t request this, ignore this email' in html
    assert 'Reset your password' in plain and 'Reset your password' in html
    assert '— The Evorove team' in plain and '— The Evorove team' in html
    assert system_mail.BANNER in plain and system_mail.BANNER in html
    assert system_mail.PRODUCT_LINE in plain and html.count(system_mail.PRODUCT_LINE) == 1
    assert not re.search(r'\d', system_mail.PRODUCT_LINE)
    assert 'mailto:support@evorove.com' in html
    assert 'support@evorove.com' in plain
    assert 'Ada & Co' in plain
    assert 'Ada &amp; Co' in html
    assert '[operator name pending]' not in plain and '[operator name pending]' not in html
    assert len(html.encode()) < 30 * 1024
    lowered = html.lower()
    for banned in ('<img', '<script', '<link', 'url(', '@import', 'src='):
        assert banned not in lowered
    hosts = {urlparse(match).hostname for match in HTTP_URL.findall(plain + html)}
    assert hosts == {'evorove.com'}
    assert '<table' in lowered and f'href="{url}"' in html
    assert 'display:block' in html


def test_reset_mail_omits_the_operator_when_unset(settings):
    settings.values['OPERATOR_NAME'] = '   '
    plain, html = parts(system_mail.reset_message(
        settings, 'owner@example.com', 'preview', expires_minutes=auth.RESET_MINUTES))
    assert '[operator name pending]' not in plain and '[operator name pending]' not in html
    assert 'support@evorove.com' in plain and 'support@evorove.com' in html


def test_saved_reset_preview_matches_the_template():
    html = system_mail.render_email(
        'reset',
        action_url='https://evorove.com/reset?token=preview',
        expires_minutes=auth.RESET_MINUTES,
        operator_name='',
    )[1]
    assert PREVIEW.read_text() == html
    assert len(html.encode()) < 30 * 1024


def test_reset_email_minutes_match_the_token_lifetime(client, settings, monkeypatch):
    held = {}

    class SMTP:
        def __init__(self, host, port, timeout):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def starttls(self, context):
            pass

        def send_message(self, message):
            held['message'] = message

    settings.values.update({'SYSTEM_SMTP_HOST': 'smtp.example.com', 'SYSTEM_SMTP_FROM': 'support@evorove.com'})
    monkeypatch.setattr(auth.smtplib, 'SMTP', SMTP)
    signup(client)
    before = datetime.now(timezone.utc)
    assert form(client, '/forgot-password', email='owner@example.com').status_code == 200
    after = datetime.now(timezone.utc)
    plain, html = parts(held['message'])
    assert f'Link expires in {auth.RESET_MINUTES} minutes' in plain
    assert f'Link expires in {auth.RESET_MINUTES} minutes' in html
    with connect(settings.db_path) as db:
        expires = datetime.fromisoformat(db.execute('SELECT expires_at FROM password_resets').fetchone()[0])
    window = timedelta(minutes=auth.RESET_MINUTES)
    slack = timedelta(seconds=2)
    assert before + window - slack <= expires <= after + window + slack

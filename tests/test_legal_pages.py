"""Legal pages describe the current product and stay in English."""
from pathlib import Path

from test_core import client, settings

ROOT = Path(__file__).resolve().parents[1]
PAGES = ('/terms', '/privacy', '/dpa', '/subprocessors')
BANNED = (
    'preserved policy text',
    'business dna',
    'deterministic',
    'twilio',
    'sms',
    'two-factor',
    '2fa',
    'local storage',
    'railway',
    'postgresql',
    'openai',
    'privacy@evorove.com',
    'widget',
    'booking',
    'quote',
    'zoho',
)


def test_legal_pages_describe_the_current_product(client):
    pages = {path: client.get(path).text for path in PAGES}
    pages['/unsubscribe'] = (ROOT / 'app/templates/unsubscribe.html').read_text()
    for path, html in pages.items():
        if path != '/unsubscribe':
            assert client.get(path).status_code == 200
        lowered = html.lower()
        assert 'trial' not in lowered
        for phrase in BANNED:
            assert phrase not in lowered, (path, phrase)
        if path != '/unsubscribe':
            assert 'support@evorove.com' in lowered
    terms = pages['/terms']
    assert 'Evorove is operated by [operator name pending], an individual' in terms
    assert 'Address: [operator address pending].' in terms
    assert 'Finding people in the open web is part of the Service.' in terms
    assert 'Search results vary. The Service does not promise any number of leads, replies, meetings or sales.' in terms
    assert 'the subscription price shown at checkout' in terms
    assert 'href="/#pricing"' in terms
    assert '$199' not in terms and '$499' not in terms
    assert 'Refunds follow Lemon Squeezy' in terms
    assert 'aggregate liability is limited to the amount you paid for the subscription in the three months before the event' in terms
    assert 'governed by the laws of the State of Delaware' in terms
    assert 'state or federal courts located in Delaware' in terms
    assert 'until the end of the period already paid' in terms
    assert 'The AI writes each outreach email on your behalf.' in terms
    assert 'basis for emailing each person' in terms
    assert 'unsubscribe link' in terms.lower() and 'postal address' in terms.lower()
    privacy = pages['/privacy']
    assert 'Evorove is operated by [operator name pending], an individual' in privacy
    assert 'Address: [operator address pending].' in privacy
    lowered_privacy = privacy.lower()
    assert 'encrypted' in lowered_privacy and 'hashed' in lowered_privacy
    assert 'delete' in lowered_privacy and 'netherlands' in lowered_privacy
    assert 'Resend (United States)' in privacy
    assert 'Cloudflare Email Routing' in privacy
    assert 'password resets' in privacy
    assert 'do not include prospect names or email text' in privacy
    assert 'latest search status' in privacy and 'error type' in privacy
    assert 'The AI writes each outreach email on your behalf.' in privacy
    dpa = pages['/dpa']
    assert 'Evorove is operated by [operator name pending], an individual' in dpa
    assert 'Address: [operator address pending].' in dpa
    assert 'service provider' in dpa.lower()
    subprocessors = pages['/subprocessors']
    for name in ('Lemon Squeezy', 'Anthropic', 'Cloudflare', 'R2', 'JustHost', 'Netherlands', 'Telegram', 'Resend'):
        assert name in subprocessors
    assert 'System email delivery: password resets and account notifications' in subprocessors
    assert 'Cloudflare Email Routing forwards incoming email addressed to support@evorove.com' in subprocessors
    assert 'none has been chosen' not in subprocessors
    assert 'not a subprocessor we appoint' in subprocessors
    assert 'do not include prospect names or email text' in subprocessors.lower()


def test_operator_identity_comes_from_settings(client, settings):
    settings.values['OPERATOR_NAME'] = 'Ada Operator'
    settings.values['OPERATOR_ADDRESS'] = '1 Example Street'
    for path in ('/terms', '/privacy', '/dpa'):
        html = client.get(path).text
        assert 'Evorove is operated by Ada Operator, an individual' in html
        assert 'Address: 1 Example Street.' in html
        assert '[operator name pending]' not in html


def test_unsubscribe_page_is_english():
    text = (ROOT / 'app/templates/unsubscribe.html').read_text()
    assert 'Link not recognized' in text
    assert 'Reply to the email with STOP and we will remove you.' in text
    assert 'You are unsubscribed' in text
    assert 'Ссылка' not in text

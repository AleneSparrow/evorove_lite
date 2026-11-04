"""Public template branding and copy contract."""
import re
from pathlib import Path

from test_core import client, settings


def test_landing_brand_and_steps(client):
    html = client.get('/').text
    assert '<title>Evorove — Find. Close. Next.</title>' in html
    assert '<h1>FIND.<br>CLOSE.<br>NEXT.</h1>' in html
    assert '<span>Find · close · next</span>' in html
    assert 'An AI sales agent that writes the first email, handles the replies and sends your payment link. Every deal moves on one board.' in html
    for label, text in [
        ('01 FIND', 'Bring your contacts and the reason to write. On Pro, Evorove also finds US companies with visible pain and keeps the evidence on the card.'),
        ('02 CLOSE', 'Evorove writes the first email, answers replies, works objections and sends your approved offer and payment link. A confirmed payment moves the deal to Done.'),
        ('03 NEXT', 'The board keeps moving. Every conversation, every stage, in one place.'),
    ]:
        assert f'<span>{label}</span><p>{text}</p>' in html
    assert "Client 0 is Evorove itself — we sell our own subscription through the same board, same rules, before anyone else's." in html


def test_public_templates_without_trials_or_lead_counts(client):
    for route in ('/', '/faq', '/terms', '/signup', '/login'):
        html = client.get(route).text
        assert 'trial' not in html.lower()
        assert 'FIRST EMAIL.<br>REAL REPLIES.<br>PAID.' not in html
        assert not re.search(r'\b\d+\s+(?:verified\s+)?new\s+(?:leads|people)', html, re.I)
        assert 'Find · close · next' in html
    assert 'Search results vary. The Service does not promise any number of leads, replies, meetings or sales.' in client.get('/terms').text
    for template in Path('app/templates').glob('*.html'):
        if template.name in {'search_preview.html', 'settings.html'}:
            continue
        # Subscription status identifiers are internal logic, not public trial copy.
        text = re.sub(r'{%.*?%}|{{.*?}}', '', template.read_text(), flags=re.S)
        assert 'trial' not in text.lower(), template
        assert not re.search(r'\b\d+\s+(?:verified\s+)?new\s+(?:leads|people)', text, re.I), template

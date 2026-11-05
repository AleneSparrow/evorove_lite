"""Branded 404/500 pages, static maintenance page, and local frontend assets."""
import re
import traceback
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import create_app
from test_core import client, settings

ROOT = Path(__file__).resolve().parents[1]
ASSET_URL = re.compile(r"""(?:src|href)\s*=\s*['"]https?:|url\(\s*['"]?https?:""", re.I)
PROMISES = re.compile(r"\b(?:soon|shortly|minutes?|hours?|tomorrow|back in|we'll)\b", re.I)


def visible_text(html):
    text = re.sub(r'<style\b.*?</style>', ' ', html, flags=re.I | re.S)
    text = re.sub(r'<[^>]+>', ' ', text)
    return re.sub(r'\s+', ' ', text)


def test_missing_page_is_branded_html(client):
    missing = client.get('/no-such-page')
    assert missing.status_code == 404
    assert 'text/html' in missing.headers['content-type']
    assert '<h1>Page not found</h1>' in missing.text
    assert "This address doesn't match a page." in missing.text
    assert 'href="/"' in missing.text
    assert PROMISES.search(visible_text(missing.text)) is None
    assert missing.headers['content-security-policy'].startswith("default-src 'self'")
    fragment = client.get('/no-such-page', headers={'HX-Request': 'true'})
    assert fragment.status_code == 404
    assert '<h1>Page not found</h1>' in fragment.text
    assert '<html' not in fragment.text.lower()


def test_unhandled_error_is_a_calm_page_without_the_cause(settings, caplog):
    # ServerErrorMiddleware still re-raises after rendering, so the test client
    # must keep the response instead of surfacing the exception.
    app = create_app(settings, scheduler_enabled=False)

    private_value = 'smtp-password-should-not-leak'

    @app.get('/__error_probe')
    def error_probe():
        raise RuntimeError(private_value)

    with TestClient(app, base_url='https://testserver', follow_redirects=False, raise_server_exceptions=False) as browser:
        failed = browser.get('/__error_probe')
        assert failed.status_code == 500
        assert 'text/html' in failed.headers['content-type']
        assert '<h1>Something went wrong</h1>' in failed.text
        assert "This page didn't load." in failed.text
        assert 'href="/"' in failed.text
        assert 'smtp-password-should-not-leak' not in failed.text
        assert 'RuntimeError' not in failed.text
        assert 'Traceback' not in failed.text
        assert PROMISES.search(visible_text(failed.text)) is None
        assert failed.headers['content-security-policy'].startswith("default-src 'self'")
        denied = browser.post('/signup', data={'email': 'x@example.com'})
        assert denied.status_code == 403
        assert denied.headers['content-type'].startswith('application/json')

    records = [record for record in caplog.records if record.name == 'app.main']
    assert records and records[0].exc_info is not None
    assert any(frame.name == 'error_probe' for frame in traceback.extract_tb(records[0].exc_info[2]))
    assert 'smtp-password-should-not-leak' not in caplog.text
    assert 'Unhandled RuntimeError' in caplog.text


def test_unhandled_error_does_not_reach_the_asgi_server(settings, caplog):
    # ServerErrorMiddleware's re-raise exists so the ASGI server (uvicorn) can log
    # the exception, which would print its original, unredacted message. without_
    # reraise() wraps the whole app to drop that re-raise before it gets there.
    # raise_server_exceptions defaults to True here (unlike the test above): if the
    # wrapper failed to suppress the re-raise, this request would itself raise it.
    from app.main import without_reraise

    app = create_app(settings, scheduler_enabled=False)

    @app.get('/__error_probe_asgi')
    def error_probe_asgi():
        raise RuntimeError('should-never-reach-the-asgi-server-log')

    with TestClient(without_reraise(app), base_url='https://testserver', follow_redirects=False) as browser:
        failed = browser.get('/__error_probe_asgi')

    assert failed.status_code == 500
    assert '<h1>Something went wrong</h1>' in failed.text
    assert 'should-never-reach-the-asgi-server-log' not in failed.text
    assert 'Unhandled RuntimeError' in caplog.text


def test_maintenance_page_is_static_and_makes_no_time_promise(client):
    path = ROOT / 'app/static/maintenance.html'
    text = path.read_text()
    assert '{%' not in text and '{{' not in text
    assert 'href="/"' in text
    assert 'Down for maintenance' in text
    assert 'The site is unavailable right now.' in text
    assert '/static/style.css' in text
    assert 'htmx' not in text.lower()
    assert not ASSET_URL.search(text)
    assert PROMISES.search(visible_text(text)) is None
    served = client.get('/static/maintenance.html')
    assert served.status_code == 200
    assert 'text/html' in served.headers['content-type']
    assert 'Down for maintenance' in served.text


def test_frontend_assets_stay_on_this_origin(client):
    for folder in (ROOT / 'app/templates', ROOT / 'app/static'):
        for file in folder.rglob('*'):
            if file.suffix not in {'.html', '.css'} or not file.is_file():
                continue
            match = ASSET_URL.search(file.read_text(errors='ignore'))
            assert match is None, f'{file.relative_to(ROOT)} -> {match.group(0)}'
    base = (ROOT / 'app/templates/base.html').read_text()
    assert 'src="/static/htmx.min.js"' in base
    assert 'href="/static/style.css"' in base
    css = (ROOT / 'app/static/style.css').read_text()
    assert '/static/fonts/barlow-condensed-700.woff2' in css
    assert '/static/fonts/inter-400.woff2' in css
    assert client.get('/static/htmx.min.js').status_code == 200
    assert client.get('/static/fonts/inter-400.woff2').status_code == 200
    assert '2.0.11' in (ROOT / 'app/static/htmx.min.js').read_text(errors='ignore')

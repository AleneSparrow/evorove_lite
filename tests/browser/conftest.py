"""Real HTTPS uvicorn and Chromium, isolated from owner's DB and external services."""
import socket
import select
import socketserver
from urllib.parse import urlparse
import subprocess
import threading
import time

import httpx
import pytest
import uvicorn
from playwright.sync_api import sync_playwright
from fastapi.responses import HTMLResponse

from app import billing
from app.main import create_app
from app.settings import Settings


@pytest.fixture(scope='session')
def chromium():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        yield browser
        browser.close()


@pytest.fixture
def live_app(tmp_path, monkeypatch):
    certificate, key = tmp_path / 'cert.pem', tmp_path / 'key.pem'
    subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes',
                    '-keyout', str(key), '-out', str(certificate), '-days', '1', '-subj', '/CN=localhost'],
                   check=True, capture_output=True)
    sock = socket.socket()
    sock.bind(('127.0.0.1', 0))
    base = f'https://127.0.0.1:{sock.getsockname()[1]}'
    settings = Settings({'SECRET_KEY': 'browser-test-only-' * 4, 'DB_PATH': str(tmp_path / 'browser.sqlite3'),
                         'APP_BASE_URL': base, 'LEMONSQUEEZY_API_KEY': 'fake-browser-api',
                         'LEMONSQUEEZY_STORE_ID': '1', 'LEMONSQUEEZY_VARIANT_STARTER': '100'})
    calls = []
    def checkout_post(url, **kwargs):
        assert url == 'https://api.lemonsqueezy.com/v1/checkouts'
        calls.append(kwargs['json'])
        return httpx.Response(201, json={'data': {'attributes': {
            'url': 'https://sandbox.lemonsqueezy.com/checkout/acceptance'}}}, request=httpx.Request('POST', url))
    monkeypatch.setattr(billing.httpx, 'post', checkout_post)
    app = create_app(settings, scheduler_enabled=False)
    @app.get('/checkout/acceptance')
    def fake_checkout_destination():
        return HTMLResponse('<h1>Test checkout destination</h1>')
    server = uvicorn.Server(uvicorn.Config(app,
                            ssl_keyfile=str(key), ssl_certfile=str(certificate), log_level='error',
                            access_log=False, proxy_headers=False, loop='asyncio'))
    thread = threading.Thread(target=server.run, kwargs={'sockets': [sock]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 15
        while not server.started:
            if not thread.is_alive() or time.monotonic() >= deadline:
                raise RuntimeError('Browser test server failed to start')
            time.sleep(.02)
        yield base, settings, calls
    finally:
        server.should_exit = True
        thread.join(timeout=15)
        sock.close()
        assert not thread.is_alive(), 'Browser test server did not stop'


@pytest.fixture
def checkout_proxy(live_app):
    # Route only the synthetic checkout host to our local TLS server. Chromium
    # still sees the genuine external origin and enforces CSP across the 303.
    # A proxy is necessary because Playwright routing skips redirected requests.
    port = urlparse(live_app[0]).port
    class Tunnel(socketserver.StreamRequestHandler):
        def handle(self):
            first = self.rfile.readline(4096)
            for _ in range(100):
                if self.rfile.readline(4096) in (b'\r\n', b''):
                    break
            if first.split()[:2] != [b'CONNECT', b'sandbox.lemonsqueezy.com:443']:
                self.wfile.write(b'HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n')
                return
            with socket.create_connection(('127.0.0.1', port), timeout=5) as upstream:
                self.wfile.write(b'HTTP/1.1 200 Connection Established\r\n\r\n')
                self.wfile.flush()
                upstream.settimeout(None)
                sockets = [self.connection, upstream]
                while True:
                    ready, _, _ = select.select(sockets, [], [], 5)
                    if not ready:
                        return
                    for source in ready:
                        data = source.recv(65536)
                        if not data:
                            return
                        destination = upstream if source is self.connection else self.connection
                        destination.sendall(data)
    class Proxy(socketserver.ThreadingTCPServer):
        daemon_threads = True
    with Proxy(('127.0.0.1', 0), Tunnel) as proxy:
        thread = threading.Thread(target=proxy.serve_forever, kwargs={'poll_interval': .05}, daemon=True)
        thread.start()
        try:
            yield f'http://127.0.0.1:{proxy.server_address[1]}'
        finally:
            proxy.shutdown()
            thread.join(timeout=5)


@pytest.fixture
def page(chromium, live_app, checkout_proxy):
    # Only a generated test certificate; never changes the user's browser trust store.
    context = chromium.new_context(ignore_https_errors=True, service_workers='block',
                                   proxy={'server': checkout_proxy, 'bypass': '127.0.0.1,localhost'})
    page = context.new_page()
    yield page
    context.close()

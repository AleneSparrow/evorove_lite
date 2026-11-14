import asyncio
import logging
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

from fastapi import FastAPI, Request, HTTPException
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from itsdangerous import URLSafeTimedSerializer, BadSignature
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import auth, billing, board, contacts, outreach, search
from .scheduler import Scheduler
from .db import ROOT, connect, migrate, now
from .settings import load

log = logging.getLogger(__name__)


def log_redacted(message, exc):
    """Keep the stack location in the process log, but an exception's own
    message may carry provider data or secrets, so never pass it through."""
    safe_error = RuntimeError("Exception details redacted")
    log.error(message, type(exc).__name__, exc_info=(RuntimeError, safe_error, exc.__traceback__))


SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "same-origin",
    "Strict-Transport-Security": "max-age=31536000",
    "Permissions-Policy": "geolocation=(), microphone=(), camera=(), payment=(), usb=()",
    "Content-Security-Policy": "default-src 'self'; frame-ancestors 'none'; form-action 'self' https://*.lemonsqueezy.com",
}


def static_cache_control(path):
    """no-store stays the rule for HTML/API; a cacheable path (favicon, a
    static asset) gets a real Cache-Control instead, so fonts and CSS are
    not re-downloaded on every page load."""
    if path == "/favicon.ico":
        return "public, max-age=604800"
    if not path.startswith("/static/"):
        return "no-store"
    if path == "/static/style.css":
        return "public, max-age=3600"
    return "public, max-age=31536000, immutable"


def canonical_host(settings):
    return urlparse(settings.base_url).hostname


def allowed_origins(settings):
    extra = {origin.strip().rstrip("/") for origin in settings.get("EXTRA_ALLOWED_ORIGINS", "").split(",") if origin.strip()}
    return {settings.base_url, *extra}


def without_reraise(asgi_app):
    """Starlette's ServerErrorMiddleware re-raises every unhandled exception after
    sending the response our own exception_handler(Exception) already built, purely
    so the ASGI server can log it -- which would print the exception's own,
    unredacted message straight to the process log. That handler already wrote a
    safe, redacted line and the response is already sent by the time this re-raise
    happens, so drop it here, outside everything Starlette controls."""
    async def wrapped(scope, receive, send):
        try:
            await asgi_app(scope, receive, send)
        except Exception:
            if scope["type"] != "http":
                raise
    return wrapped


def create_app(settings=None, *, scheduler_enabled=True):
    @asynccontextmanager
    async def lifespan(app):
        app.state.settings = settings or load()
        migrate(app.state.settings.db_path)
        app.state.scheduler = Scheduler(app.state.settings)
        app.state.search_previews = {}
        app.state.search_preview_tasks = set()
        if scheduler_enabled:
            app.state.scheduler.start()
        try:
            yield
        finally:
            await app.state.scheduler.stop()
            if app.state.search_preview_tasks:
                await asyncio.gather(*app.state.search_preview_tasks, return_exceptions=True)

    app = FastAPI(lifespan=lifespan)
    templates = Jinja2Templates(directory=str(ROOT / "app/templates"))
    templates.env.filters["relative_time"] = board.relative_time
    templates.env.filters["funnel_summary"] = board.funnel_summary
    app.mount("/static", StaticFiles(directory=ROOT / "app/static"), name="static")

    def apply_security_headers(response, cache_control="no-store"):
        response.headers.update({**SECURITY_HEADERS, "Cache-Control": cache_control})
        return response

    def error_page(request, status):
        # htmx swaps the response into a board pane, so it gets the panel only.
        template = "error_fragment.html" if request.headers.get("hx-request") == "true" else f"{status}.html"
        response = templates.TemplateResponse(request=request, name=template, context={"code": status}, status_code=status)
        return apply_security_headers(response)

    @app.exception_handler(StarletteHTTPException)
    async def http_errors(request: Request, exc: StarletteHTTPException):
        if exc.status_code == 404:
            return error_page(request, 404)
        if exc.status_code == 500:
            return error_page(request, 500)
        response = await http_exception_handler(request, exc)
        return apply_security_headers(response)

    @app.exception_handler(Exception)
    async def unhandled_error(request: Request, exc: Exception):
        log_redacted("Unhandled %s", exc)
        return error_page(request, 500)

    @app.middleware("http")
    async def security_headers(request, call_next):
        settings_ = request.app.state.settings
        host = canonical_host(settings_)
        if host and request.url.hostname == "www." + host:
            target = settings_.base_url + request.url.path
            if request.url.query:
                target += "?" + request.url.query
            return apply_security_headers(RedirectResponse(target, status_code=301))
        return apply_security_headers(await call_next(request), static_cache_control(request.url.path))

    def render(request, template, **context):
        s = request.app.state.settings
        nonce = request.cookies.get("csrf") or secrets.token_urlsafe(24)
        csrf = URLSafeTimedSerializer(s.secret, salt="csrf").dumps(nonce)
        response = templates.TemplateResponse(request=request, name=template, context={
            "csrf": csrf, **context,
            "operator_name": s.operator_name, "operator_address": s.operator_address})
        response.set_cookie("csrf", nonce, secure=True, httponly=True, samesite="lax", max_age=3600)
        return response

    async def form(request, *, upload=False):
        content_type = request.headers.get('content-type', '')
        multipart = upload and content_type.split(';')[0] == 'multipart/form-data'
        if not multipart and content_type.split(';')[0] != 'application/x-www-form-urlencoded':
            raise HTTPException(415, 'Use a form or CSV upload')
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > (5_000_000 if upload else 16384):
                raise HTTPException(413, 'Form is too large')
        fields = contacts.multipart_fields(content_type, bytes(body)) if multipart else {
            key: values[0] for key, values in parse_qs(body.decode(), keep_blank_values=True).items()}
        try:
            nonce = URLSafeTimedSerializer(request.app.state.settings.secret, salt="csrf").loads(fields.get("csrf", ""), max_age=3600)
            if not secrets.compare_digest(nonce, request.cookies.get("csrf", "")):
                raise BadSignature("CSRF")
        except BadSignature:
            raise HTTPException(403, "Reload the page and try again") from None
        origin = request.headers.get("origin")
        if origin == "null" or (origin and origin.rstrip("/") not in allowed_origins(request.app.state.settings)):
            raise HTTPException(403, "Invalid origin")
        return fields

    @app.get("/favicon.ico")
    def favicon():
        # Browsers and bots request this at the root regardless of any <link>.
        return FileResponse(ROOT / "app/static/favicon.ico", media_type="image/x-icon")

    @app.get("/health")
    def health(request: Request):
        with connect(request.app.state.settings.db_path) as db:
            db.execute("SELECT 1").fetchone()
            rows = {row['kind']: row['last_success_at'] for row in db.execute(
                "SELECT kind,max(finished_at) AS last_success_at FROM runs "
                "WHERE status='ok' GROUP BY kind")}
        scheduler = {kind: {'last_success_at': rows.get(kind)}
                     for kind in ('search', 'send', 'poll', 'summary')}
        last_search = rows.get('search')
        stale = True
        if last_search:
            try:
                stamp = datetime.fromisoformat(last_search.replace('Z', '+00:00'))
                if stamp.tzinfo is not None:
                    stale = datetime.now(timezone.utc) - stamp.astimezone(timezone.utc) > timedelta(hours=26)
            except (TypeError, ValueError):
                pass
        return {"status": "ok", "scheduler": scheduler, "stale": stale}

    @app.get("/")
    def home(request: Request):
        return render(request, "landing.html")

    @app.get("/signup")
    @app.get("/login")
    @app.get("/forgot-password")
    @app.get("/reset")
    def auth_page(request: Request):
        return render(request, "auth.html", mode=request.url.path.strip("/"), token=request.query_params.get("token", ""))

    @app.post("/signup")
    async def signup(request: Request):
        fields = await form(request)
        s = request.app.state.settings
        await run_in_threadpool(auth.rate_limit, s, auth.client_ip(request), fields.get("email", "").lower(), "signup")
        user_id = await run_in_threadpool(auth.signup, s, fields.get("email", ""), fields.get("password", ""), fields.get("business_name", ""))
        response = RedirectResponse("/app", status_code=303)
        auth.start_session(s, user_id, response)
        return response

    @app.post("/login")
    async def login(request: Request):
        fields = await form(request)
        s = request.app.state.settings
        password = fields.get("password", "")
        if len(password) > 1024:
            raise HTTPException(400, "Password is too long")
        user_id = await run_in_threadpool(auth.login, s, fields.get("email", ""), password, auth.client_ip(request))
        response = RedirectResponse("/app", status_code=303)
        auth.start_session(s, user_id, response)
        return response

    @app.post("/logout")
    async def logout(request: Request):
        await form(request)
        s = request.app.state.settings
        with connect(s.db_path) as db:
            db.execute("DELETE FROM sessions WHERE token_hash=?", (auth.session_hash(s, request),))
        response = RedirectResponse("/login", status_code=303)
        response.delete_cookie(auth.COOKIE, secure=True, httponly=True, samesite="lax")
        return response

    @app.post("/forgot-password")
    async def forgot(request: Request):
        fields = await form(request)
        await run_in_threadpool(auth.request_reset, request.app.state.settings, fields.get("email", ""), auth.client_ip(request))
        return render(request, "notice.html", message="If an account exists, a reset email has been sent.")

    @app.post("/reset")
    async def reset(request: Request):
        fields = await form(request)
        s = request.app.state.settings
        auth.rate_limit(s, auth.client_ip(request), auth.digest(fields.get("token", "")), "reset-token")
        await run_in_threadpool(auth.reset_password, s, fields.get("token", ""), fields.get("password", ""))
        return RedirectResponse("/login", status_code=303)

    @app.get("/app")
    def account(request: Request):
        s = request.app.state.settings
        user = auth.current_user(s, request)
        bid = user['business_id']
        selected = request.query_params.get('person')
        business = board.business(s, bid)
        return render(request, 'board.html', business=business, columns=board.columns(s, bid),
                      entitled=billing.entitled(business, settings=s),
                      last=board.last_search(s, bid), can_search=billing.can_search(business, settings=s), person=board.person(s, bid, selected) if selected else None)

    @app.get('/app/people/{person_id}')
    def person_detail(request: Request, person_id: str):
        s = request.app.state.settings
        user = auth.current_user(s, request)
        return render(request, 'person.html', person=board.person(s, user['business_id'], person_id))

    @app.post('/app/people/{person_id}/payment')
    async def record_manual_payment(request: Request, person_id: str):
        fields = await form(request)
        if fields.get('confirm') != 'yes':
            raise HTTPException(400, 'Confirm that payment was received')
        s = request.app.state.settings
        user = auth.current_user(s, request)
        person = board.person(s, user['business_id'], person_id)
        if person['stage'] not in {'offer_made', 'done'}:
            raise HTTPException(409, 'Payment can only be recorded after an offer')
        if person['stage'] == 'done':
            return render(request, 'person.html', person=person)
        if person['stage'] == 'offer_made':
            try:
                await run_in_threadpool(outreach.record_payment, user['business_id'], person_id, 'manual', settings=s)
            except ValueError:
                raise HTTPException(404, 'Person not found') from None
        with connect(s.db_path) as db:
            db.execute("INSERT OR IGNORE INTO payments(id,business_id,person_id,source,event_name,test_mode,raw_event_id,created_at) VALUES (?,?,?,'manual','manual_payment',0,?,?)",
                       (str(secrets.token_urlsafe(18)), user['business_id'], person_id,
                        f"manual:{user['business_id']}:{person_id}", now()))
        return render(request, 'person.html', person=board.person(s, user['business_id'], person_id))

    @app.get('/app/metrics')
    def metrics_page(request: Request):
        s = request.app.state.settings
        user = auth.current_user(s, request)
        bid = user['business_id']
        return render(request, 'metrics.html', business=board.business(s, bid), **board.metrics(s, bid))

    @app.get('/app/columns')
    def board_columns(request: Request):
        s = request.app.state.settings
        user = auth.current_user(s, request)
        return render(request, 'columns.html', columns=board.columns(s, user['business_id']))

    @app.get('/app/search-status')
    def search_status(request: Request):
        s = request.app.state.settings
        user = auth.current_user(s, request)
        return render(request, 'search_status.html', last=board.last_search(s, user['business_id']))

    @app.post('/app/find-people')
    async def find_people(request: Request):
        await form(request)
        s = request.app.state.settings
        user = auth.current_user(s, request)
        business = board.business(s, user['business_id'])
        billing.require_search(business, settings=s)
        if not business['brief']:
            raise HTTPException(400, 'Add a search brief in Settings first')
        request.app.state.scheduler.dispatch(user['business_id'], 'search')
        if request.headers.get('hx-request') == 'true':
            return render(request, 'search_status.html', last=board.last_search(s, user['business_id']))
        return RedirectResponse('/app', status_code=303)

    @app.get('/app/search-preview')
    def search_preview(request: Request):
        s = request.app.state.settings
        user = auth.current_user(s, request)
        billing.require_search(board.business(s, user['business_id']), settings=s)
        return render(request, 'search_preview.html',
                      preview=request.app.state.search_previews.get(user['business_id']))

    @app.get('/app/search-preview/status')
    def search_preview_status(request: Request):
        s = request.app.state.settings
        user = auth.current_user(s, request)
        billing.require_search(board.business(s, user['business_id']), settings=s)
        return render(request, 'search_preview_results.html',
                      preview=request.app.state.search_previews.get(user['business_id']))

    @app.post('/app/search-preview')
    async def start_search_preview(request: Request):
        await form(request)
        s = request.app.state.settings
        user = auth.current_user(s, request)
        bid = user['business_id']
        business = board.business(s, bid)
        billing.require_search(business, settings=s)
        if not business['brief']:
            raise HTTPException(400, 'Add a search brief in Settings first')
        current = request.app.state.search_previews.get(bid)
        if not current or current.get('status') != 'running':
            request.app.state.search_previews[bid] = {'status': 'running', 'people': []}

            async def execute_preview():
                try:
                    people = await asyncio.to_thread(search.run_search, bid, limit=20, dry_run=True)
                    request.app.state.search_previews[bid] = {'status': 'ok', 'people': people}
                except Exception as exc:
                    log_redacted("Search preview failed: %s", exc)
                    request.app.state.search_previews[bid] = {'status': 'failed', 'people': []}

            task = asyncio.create_task(execute_preview())
            request.app.state.search_preview_tasks.add(task)
            task.add_done_callback(request.app.state.search_preview_tasks.discard)
        if request.headers.get('hx-request') == 'true':
            return render(request, 'search_preview_results.html',
                          preview=request.app.state.search_previews.get(bid))
        return RedirectResponse('/app/search-preview', status_code=303)

    @app.get('/app/contacts')
    def contacts_page(request: Request):
        s = request.app.state.settings
        user = auth.current_user(s, request)
        return render(request, 'contacts.html', report=None, columns=contacts.COLUMNS)

    @app.post('/app/contacts')
    async def add_contacts(request: Request):
        fields = await form(request, upload=True)
        s = request.app.state.settings
        user = auth.current_user(s, request)
        business = board.business(s, user['business_id'])
        if business['plan'] not in {'starter', 'pro'} or not billing.entitled(business, settings=s):
            raise HTTPException(403, 'An active Starter or Pro subscription is required')
        rows = contacts.csv_rows(fields['csv']) if 'csv' in fields else [fields]
        report = await run_in_threadpool(contacts.add, s, user['business_id'], rows)
        return render(request, 'contacts.html', report=report, columns=contacts.COLUMNS)

    @app.get('/app/billing')
    def billing_page(request: Request):
        s = request.app.state.settings
        user = auth.current_user(s, request)
        return render(request, 'billing.html', business=board.business(s, user['business_id']))

    @app.post('/billing/portal')
    async def billing_portal(request: Request):
        await form(request)
        s = request.app.state.settings
        user = auth.current_user(s, request)
        url = await run_in_threadpool(billing.portal, s, user)
        return RedirectResponse(url, status_code=303)

    @app.get('/app/settings')
    def settings_page(request: Request):
        s = request.app.state.settings
        user = auth.current_user(s, request)
        return render(request, 'settings.html', business=board.business(s, user['business_id']))

    @app.post('/app/settings')
    async def update_settings(request: Request):
        fields = await form(request)
        s = request.app.state.settings
        user = auth.current_user(s, request)
        await run_in_threadpool(board.save_settings, s, user['business_id'], fields)
        return RedirectResponse('/app/settings?saved=1', status_code=303)

    @app.get('/faq')
    @app.get('/terms')
    @app.get('/privacy')
    @app.get('/dpa')
    @app.get('/subprocessors')
    def public_page(request: Request):
        return render(request, request.url.path.strip('/') + '.html')

    @app.get('/api/v1/public/unsubscribe/{token}')
    @app.post('/api/v1/public/unsubscribe/{token}')
    def legacy_unsubscribe_result(request: Request, token: str):
        from app.legacy_unsubscribe import unsubscribe as legacy_unsubscribe
        done = request.method == 'POST'
        email = legacy_unsubscribe(request.app.state.settings, token, apply=done)
        response = render(request, 'unsubscribe.html', email=email, token=token, done=done,
                          unrecognized=email is None, form_action=request.url.path)
        if email is None:
            response.status_code = 400
        return response

    @app.get('/unsubscribe/{token}')
    def unsubscribe_page(request: Request, token: str):
        email = board.unsubscribe(request.app.state.settings, token)
        return render(request, 'unsubscribe.html', email=email, token=token, done=False)

    @app.post('/unsubscribe/{token}')
    async def unsubscribe(request: Request, token: str):
        # The signed bearer token authorizes RFC 8058 one-click POST without a session.
        email = board.unsubscribe(request.app.state.settings, token, apply=True)
        return render(request, 'unsubscribe.html', email=email, token=token, done=True)

    @app.post("/billing/checkout/{plan}")
    async def checkout(request: Request, plan: str):
        await form(request)
        s = request.app.state.settings
        user = auth.current_user(s, request)
        url = await run_in_threadpool(billing.checkout, s, user, plan)
        return RedirectResponse(url, status_code=303)

    @app.post("/billing/webhook")
    @app.post("/api/v1/billing/webhook")
    async def webhook(request: Request):
        payload = await request.body()
        if len(payload) > 262144:
            raise HTTPException(413, "Event is too large")
        await run_in_threadpool(billing.webhook, request.app.state.settings, payload, request.headers.get("x-signature"))
        return {"ok": True}

    return app


app = without_reraise(create_app())

import secrets
from contextlib import asynccontextmanager
from urllib.parse import parse_qs

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from itsdangerous import URLSafeTimedSerializer, BadSignature
from starlette.concurrency import run_in_threadpool

from . import auth, billing, board
from .scheduler import Scheduler
from .db import ROOT, connect, migrate
from .settings import load


def create_app(settings=None, *, scheduler_enabled=True):
    @asynccontextmanager
    async def lifespan(app):
        app.state.settings = settings or load()
        migrate(app.state.settings.db_path)
        app.state.scheduler = Scheduler(app.state.settings)
        if scheduler_enabled:
            app.state.scheduler.start()
        try:
            yield
        finally:
            await app.state.scheduler.stop()

    app = FastAPI(lifespan=lifespan)
    templates = Jinja2Templates(directory=str(ROOT / "app/templates"))
    app.mount("/static", StaticFiles(directory=ROOT / "app/static"), name="static")

    @app.middleware("http")
    async def security_headers(request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        response.headers["Content-Security-Policy"] = "default-src 'self'; frame-ancestors 'none'; form-action 'self'"
        return response

    def render(request, template, **context):
        s = request.app.state.settings
        nonce = request.cookies.get("csrf") or secrets.token_urlsafe(24)
        csrf = URLSafeTimedSerializer(s.secret, salt="csrf").dumps(nonce)
        response = templates.TemplateResponse(request=request, name=template, context={"csrf": csrf, **context})
        response.set_cookie("csrf", nonce, secure=True, httponly=True, samesite="lax", max_age=3600)
        return response

    async def form(request):
        if request.headers.get("content-type", "").split(";")[0] != "application/x-www-form-urlencoded":
            raise HTTPException(415, "Use a URL encoded form")
        body = await request.body()
        if len(body) > 16384:
            raise HTTPException(413, "Form is too large")
        fields = {key: values[0] for key, values in parse_qs(body.decode(), keep_blank_values=True).items()}
        try:
            nonce = URLSafeTimedSerializer(request.app.state.settings.secret, salt="csrf").loads(fields.get("csrf", ""), max_age=3600)
            if not secrets.compare_digest(nonce, request.cookies.get("csrf", "")):
                raise BadSignature("CSRF")
        except BadSignature:
            raise HTTPException(403, "Reload the page and try again") from None
        origin = request.headers.get("origin")
        if origin and origin.rstrip("/") != request.app.state.settings.base_url:
            raise HTTPException(403, "Invalid origin")
        return fields

    @app.get("/health")
    def health(request: Request):
        with connect(request.app.state.settings.db_path) as db:
            db.execute("SELECT 1").fetchone()
        return {"status": "ok"}

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
        return render(request, 'board.html', business=board.business(s, bid), columns=board.columns(s, bid),
                      last=board.last_search(s, bid), person=board.person(s, bid, selected) if selected else None)

    @app.get('/app/people/{person_id}')
    def person_detail(request: Request, person_id: str):
        s = request.app.state.settings
        user = auth.current_user(s, request)
        return render(request, 'person.html', person=board.person(s, user['business_id'], person_id))

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
        if business['subscription_status'] not in {'active', 'on_trial'}:
            raise HTTPException(403, 'An active subscription is required')
        if not business['brief']:
            raise HTTPException(400, 'Add a search brief in Settings first')
        request.app.state.scheduler.dispatch(user['business_id'], 'search')
        if request.headers.get('hx-request') == 'true':
            return render(request, 'search_status.html', last=board.last_search(s, user['business_id']))
        return RedirectResponse('/app', status_code=303)

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
    async def webhook(request: Request):
        payload = await request.body()
        if len(payload) > 262144:
            raise HTTPException(413, "Event is too large")
        await run_in_threadpool(billing.webhook, request.app.state.settings, payload, request.headers.get("x-signature"))
        return {"ok": True}

    return app


app = create_app()

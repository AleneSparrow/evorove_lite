import secrets
from contextlib import asynccontextmanager
from urllib.parse import parse_qs

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from itsdangerous import URLSafeTimedSerializer, BadSignature
from starlette.concurrency import run_in_threadpool

from . import auth, billing
from .db import ROOT, connect, migrate
from .settings import load


def create_app(settings=None):
    @asynccontextmanager
    async def lifespan(app):
        app.state.settings = settings or load()
        migrate(app.state.settings.db_path)
        yield

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
    def home():
        return RedirectResponse("/login", status_code=303)

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
        await run_in_threadpool(auth.rate_limit, s, request.client.host, fields.get("email", "").lower(), "signup")
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
        user_id = await run_in_threadpool(auth.login, s, fields.get("email", ""), password, request.client.host)
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
        await run_in_threadpool(auth.request_reset, request.app.state.settings, fields.get("email", ""), request.client.host)
        return render(request, "notice.html", message="If an account exists, a reset email has been sent.")

    @app.post("/reset")
    async def reset(request: Request):
        fields = await form(request)
        s = request.app.state.settings
        auth.rate_limit(s, request.client.host, "reset-token", "reset-token")
        await run_in_threadpool(auth.reset_password, s, fields.get("token", ""), fields.get("password", ""))
        return RedirectResponse("/login", status_code=303)

    @app.get("/app")
    def account(request: Request):
        s = request.app.state.settings
        user = auth.current_user(s, request)
        with connect(s.db_path) as db:
            business = dict(db.execute("SELECT * FROM businesses WHERE id=?", (user["business_id"],)).fetchone())
        return render(request, "account.html", business=business)

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

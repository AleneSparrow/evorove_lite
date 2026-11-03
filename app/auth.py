import hashlib
import hmac
import ipaddress
import os
from pathlib import Path
from urllib.parse import urlparse
import re
import secrets
import smtplib
import sqlite3
import ssl
import time
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from uuid import uuid4

from argon2 import PasswordHasher
from argon2.exceptions import VerificationError, InvalidHashError
from fastapi import HTTPException
from itsdangerous import URLSafeTimedSerializer, BadSignature

from .db import connect, now

HASHER = PasswordHasher()
COOKIE = "evorove_session"
LEGACY_PBKDF2_PREFIX = "pbkdf2_sha256"


def client_ip(request):
    peer = request.client.host if request.client else "unknown"
    if peer in {"127.0.0.1", "::1"}:
        forwarded = request.headers.get("cf-connecting-ip", "")
        try:
            return str(ipaddress.ip_address(forwarded))
        except ValueError:
            pass
    return peer


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def email_address(value):
    value = value.strip().lower()
    if len(value) > 254 or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", value):
        raise HTTPException(400, "Enter a valid email address")
    return value


def password_rule(password):
    if not 12 <= len(password) <= 1024:
        raise HTTPException(400, "Password must contain 12–1024 characters")


def verify_legacy_password(password, stored):
    """Verify the one PBKDF2 format emitted by the retired application."""
    try:
        label, iterations_text, salt_hex, expected_hex = stored.split("$")
        if label != LEGACY_PBKDF2_PREFIX:
            return False
        iterations = int(iterations_text)
        if not 100_000 <= iterations <= 2_000_000:
            return False
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(expected_hex)
        if not 8 <= len(salt) <= 64 or len(expected) != hashlib.sha256().digest_size:
            return False
    except (AttributeError, TypeError, ValueError):
        return False
    candidate = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(candidate, expected)


def rate_limit(settings, ip, email, kind):
    stamp = int(time.time())
    keys = [digest(f"{kind}:ip:{ip}"), digest(f"{kind}:email:{email}")]
    limited = False
    with connect(settings.db_path) as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute("DELETE FROM rate_limits WHERE window_start < ?", (stamp - 900,))
        for key, cap in zip(keys, (30, 8)):
            db.execute("INSERT INTO rate_limits VALUES (?,1,?) ON CONFLICT(key) DO UPDATE SET attempts=attempts+1", (key, stamp))
            limited |= db.execute("SELECT attempts FROM rate_limits WHERE key=?", (key,)).fetchone()[0] > cap
    if limited:
        raise HTTPException(429, "Too many attempts. Try again in 15 minutes")


def signup(settings, email, password, name):
    email = email_address(email)
    password_rule(password)
    if not name.strip() or len(name) > 200:
        raise HTTPException(400, "Enter a business name (up to 200 characters)")
    business, user = str(uuid4()), str(uuid4())
    password_hash = HASHER.hash(password)
    try:
        with connect(settings.db_path) as db:
            db.execute("INSERT INTO businesses(id,name,created_at) VALUES (?,?,?)", (business, name.strip(), now()))
            db.execute("INSERT INTO users VALUES (?,?,?,?,?,?)", (user, email, password_hash, business, "owner", now()))
    except sqlite3.IntegrityError:
        raise HTTPException(400, "Unable to create account with these details") from None
    return user


def login(settings, email, password, ip):
    email = email_address(email)
    rate_limit(settings, ip, email, "login")
    with connect(settings.db_path) as db:
        user = db.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
    # Equal Argon2 work for an unknown email; never reveal whether an account exists.
    stored = user["password_hash"] if user else HASHER.hash(secrets.token_urlsafe(24))
    legacy = stored.startswith(LEGACY_PBKDF2_PREFIX + "$")
    if legacy:
        valid = verify_legacy_password(password, stored)
    else:
        try:
            valid = HASHER.verify(stored, password)
        except (VerificationError, InvalidHashError):
            valid = False
    if not user or not valid:
        raise HTTPException(401, "Invalid email or password")
    if legacy:
        upgraded = HASHER.hash(password)
        with connect(settings.db_path) as db:
            db.execute(
                "UPDATE users SET password_hash=? WHERE id=? AND password_hash=?",
                (upgraded, user["id"], stored),
            )
    return user["id"]


def serializer(settings):
    return URLSafeTimedSerializer(settings.secret, salt="session")


def start_session(settings, user_id, response):
    token = secrets.token_urlsafe(32)
    with connect(settings.db_path) as db:
        db.execute("DELETE FROM sessions WHERE expires_at<=?", (now(),))
        db.execute("INSERT INTO sessions VALUES (?,?,?)", (digest(token), user_id, (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()))
    response.set_cookie(COOKIE, serializer(settings).dumps(token), max_age=604800, httponly=True, secure=True, samesite="lax")


def session_hash(settings, request):
    try:
        return digest(serializer(settings).loads(request.cookies.get(COOKIE, ""), max_age=604800))
    except BadSignature:
        return ""


def current_user(settings, request):
    with connect(settings.db_path) as db:
        user = db.execute("SELECT users.* FROM users JOIN sessions ON users.id=sessions.user_id WHERE sessions.token_hash=? AND sessions.expires_at>?", (session_hash(settings, request), now())).fetchone()
    if not user:
        raise HTTPException(303, headers={"Location": "/login"})
    if user["role"] != "owner":
        raise HTTPException(403, "Owner access required")
    return dict(user)


def send_reset(settings, recipient, token):
    host = settings.get("SYSTEM_SMTP_HOST")
    message = EmailMessage()
    message["From"] = settings.get("SYSTEM_SMTP_FROM", "dev@localhost")
    message["To"] = recipient
    message["Subject"] = "Reset your Evorove password"
    message.set_content(f"Reset your password within 30 minutes:\n{settings.base_url}/reset?token={token}\n")
    if not host:
        if urlparse(settings.base_url).hostname not in {"localhost", "127.0.0.1"}:
            raise RuntimeError("System SMTP is not configured")
        mailbox = Path(".local/mailbox")
        mailbox.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = mailbox / (str(uuid4()) + ".eml")
        with open(path, "x", opener=lambda path, flags: os.open(path, flags, 0o600)) as file:
            file.write(message.as_string())
        return
    with smtplib.SMTP(host, int(settings.get("SYSTEM_SMTP_PORT", "587")), timeout=15) as smtp:
        smtp.starttls(context=ssl.create_default_context())
        if settings.get("SYSTEM_SMTP_USER"):
            smtp.login(settings.get("SYSTEM_SMTP_USER"), settings.get("SYSTEM_SMTP_PASSWORD"))
        smtp.send_message(message)


def request_reset(settings, email, ip):
    email = email_address(email)
    rate_limit(settings, ip, email, "reset")
    token = secrets.token_urlsafe(32)
    with connect(settings.db_path) as db:
        user = db.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()
        if user:
            db.execute("INSERT INTO password_resets VALUES (?,?,?,0)", (digest(token), user[0], (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat()))
    if user:
        try:
            send_reset(settings, email, token)
        except Exception:
            with connect(settings.db_path) as db:
                db.execute("UPDATE password_resets SET used=1 WHERE token_hash=?", (digest(token),))
                db.execute("INSERT INTO log(event,detail,created_at) VALUES ('reset_mail_failed','System SMTP delivery failed',?)", (now(),))


def reset_password(settings, token, password):
    password_rule(password)
    hashed = HASHER.hash(password)
    with connect(settings.db_path) as db:
        db.execute("BEGIN IMMEDIATE")
        reset = db.execute("SELECT * FROM password_resets WHERE token_hash=? AND used=0 AND expires_at>?", (digest(token), now())).fetchone()
        if not reset:
            raise HTTPException(400, "Invalid or expired reset link")
        db.execute("UPDATE users SET password_hash=? WHERE id=?", (hashed, reset["user_id"]))
        db.execute("UPDATE password_resets SET used=1 WHERE user_id=?", (reset["user_id"],))
        db.execute("DELETE FROM sessions WHERE user_id=?", (reset["user_id"],))

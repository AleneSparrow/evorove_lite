"""One prospect, one thread: email and chat together. No sales logic here."""
import hmac
from datetime import datetime, timedelta, timezone
from urllib.parse import quote
from uuid import uuid4

from fastapi import HTTPException
from itsdangerous import BadSignature, URLSafeTimedSerializer

from . import outreach
from .db import connect, now

TOKEN_SALT = "evorove:chat:v1"
FORM_SALT = "evorove:chat-form:v1"
TOKEN_MAX_AGE = 30 * 24 * 60 * 60
PERSON_GAP = timedelta(seconds=5)
PERSON_HOUR = 30
BUSINESS_HOUR = 120
CLOSED = "Understood. I'll leave it there."
PAUSED = "We'll reply by email shortly."
WAIT = "Please wait a moment before sending another message."
LENGTH = "Write a message between 1 and 2000 characters."


def reply_fn(business, person, message_row):
    """Sales brain lives in outreach. Tests replace this function."""
    fn = getattr(outreach, "next_reply", None)
    if fn is None:
        raise RuntimeError("chat reply is not available")
    return fn(business, person, message_row)


def _serializer(settings, salt):
    return URLSafeTimedSerializer(settings.secret, salt=salt)


def token_for(settings, business_id, person_id):
    return _serializer(settings, TOKEN_SALT).dumps({"business_id": business_id, "person_id": person_id})


def person_for(settings, token):
    try:
        data = _serializer(settings, TOKEN_SALT).loads(token, max_age=TOKEN_MAX_AGE)
    except BadSignature:
        raise HTTPException(404, "Page not found") from None
    if not isinstance(data, dict):
        raise HTTPException(404, "Page not found")
    business_id, person_id = data.get("business_id"), data.get("person_id")
    if not isinstance(business_id, str) or not isinstance(person_id, str):
        raise HTTPException(404, "Page not found")
    with connect(settings.db_path) as db:
        business = db.execute("SELECT * FROM businesses WHERE id=?", (business_id,)).fetchone()
        person = db.execute("SELECT * FROM people WHERE id=? AND business_id=?", (person_id, business_id)).fetchone()
    if not business or not person:
        raise HTTPException(404, "Page not found")
    return dict(business), dict(person)


def _form_token(settings, token):
    return _serializer(settings, FORM_SALT).dumps(token)


def _check_form(settings, token, value):
    try:
        signed = _serializer(settings, FORM_SALT).loads(value or "", max_age=TOKEN_MAX_AGE)
    except BadSignature:
        raise HTTPException(403, "Reload the page and try again") from None
    if not isinstance(signed, str) or not hmac.compare_digest(signed.encode(), token.encode()):
        raise HTTPException(403, "Reload the page and try again")


def _load(settings, business_id, person_id):
    with connect(settings.db_path) as db:
        business = db.execute("SELECT * FROM businesses WHERE id=?", (business_id,)).fetchone()
        person = db.execute("SELECT * FROM people WHERE id=? AND business_id=?", (person_id, business_id)).fetchone()
    return dict(business), dict(person)


def _messages(settings, person_id):
    with connect(settings.db_path) as db:
        rows = db.execute(
            "SELECT direction, body, channel FROM messages WHERE person_id=? ORDER BY created_at, id",
            (person_id,)).fetchall()
    return [dict(row) for row in rows]


def _limited(settings, business_id, person_id):
    moment = datetime.now(timezone.utc)
    hour = (moment - timedelta(hours=1)).isoformat()
    with connect(settings.db_path) as db:
        last = db.execute(
            "SELECT created_at FROM messages WHERE person_id=? AND direction='in' AND channel='chat' "
            "ORDER BY created_at DESC LIMIT 1", (person_id,)).fetchone()
        if last:
            seen = datetime.fromisoformat(last[0])
            if seen.tzinfo is None:
                seen = seen.replace(tzinfo=timezone.utc)
            if moment - seen < PERSON_GAP:
                return True
        person_count = db.execute(
            "SELECT COUNT(*) FROM messages WHERE person_id=? AND direction='in' AND channel='chat' AND created_at>=?",
            (person_id, hour)).fetchone()[0]
        if person_count >= PERSON_HOUR:
            return True
        business_count = db.execute(
            "SELECT COUNT(*) FROM messages m JOIN people p ON p.id=m.person_id "
            "WHERE p.business_id=? AND m.direction='in' AND m.channel='chat' AND m.created_at>=?",
            (business_id, hour)).fetchone()[0]
    return business_count >= BUSINESS_HOUR


def _insert(db, person_id, direction, body, move):
    message_id = str(uuid4())
    stamp = now()
    db.execute(
        "INSERT INTO messages (id,person_id,direction,body,move,created_at,channel,reply_processed) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (message_id, person_id, direction, body, move, stamp, "chat", 1))
    return {"id": message_id, "person_id": person_id, "direction": direction, "body": body,
            "move": move, "created_at": stamp, "channel": "chat", "reply_processed": 1}


def _view(settings, token, business, person, notice=""):
    stage = person["stage"]
    paused = int(business["outreach_paused"] or 0) == 1
    closed = stage in {"done", "lost"}
    if not notice:
        if stage == "lost":
            notice = CLOSED
        elif paused:
            notice = PAUSED
    compose = not closed and not paused
    sender = (business["sender_name"] or business["name"] or "Messages").strip() or "Messages"
    return {"sender_name": sender, "messages": _messages(settings, person["id"]), "notice": notice,
            "compose": compose, "action": "/c/" + quote(token, safe="") + "/messages",
            "sig": _form_token(settings, token) if compose else ""}


def page(settings, token, business, person):
    business, person = _load(settings, business["id"], person["id"])
    return 200, _view(settings, token, business, person)


def accept(settings, token, business, person, fields):
    _check_form(settings, token, fields.get("sig", ""))
    text = str(fields.get("text") or "").strip()
    if not 1 <= len(text) <= 2000:
        return 400, _view(settings, token, business, person, LENGTH)
    business, person = _load(settings, business["id"], person["id"])
    if person["stage"] in {"done", "lost"}:
        return 200, _view(settings, token, business, person)
    hostile = outreach.HOSTILE.search(text[:500])
    if outreach.is_opt_out(text) or hostile:
        with connect(settings.db_path) as db:
            _insert(db, person["id"], "in", text, None)
        reason = "chat_hostile" if hostile and not outreach.is_opt_out(text) else "chat_opt_out"
        outreach.stop_contact(settings, business["id"], person, reason)
        business, person = _load(settings, business["id"], person["id"])
        return 200, _view(settings, token, business, person)
    if _limited(settings, business["id"], person["id"]):
        return 429, _view(settings, token, business, person, WAIT)
    if int(business["outreach_paused"] or 0) == 1:
        with connect(settings.db_path) as db:
            _insert(db, person["id"], "in", text, None)
        business, person = _load(settings, business["id"], person["id"])
        return 200, _view(settings, token, business, person)
    with connect(settings.db_path) as db:
        inbound = _insert(db, person["id"], "in", text, None)
    move, body = reply_fn(business, person, inbound)
    reply = " ".join(str(body or "").split())
    if not reply:
        raise RuntimeError("empty chat reply")
    label = str(move or "").strip()[:64] or None
    with connect(settings.db_path) as db:
        _insert(db, person["id"], "out", reply, label)
    business, person = _load(settings, business["id"], person["id"])
    return 200, _view(settings, token, business, person)

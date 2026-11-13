import re
from datetime import datetime, timedelta, timezone
from html import unescape
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import chat, outreach
from app.db import ROOT, connect, migrate, now
from app.main import create_app
from app.settings import Settings

PAIN = "needle-pain-7f3c"
EMAIL = "prospect@harbor.example"
MOVE = "needle-move-9aa1"


@pytest.fixture
def settings(tmp_path):
    return Settings({"SECRET_KEY": "test-only-" * 6, "DB_PATH": str(tmp_path / "test.sqlite3"),
                     "APP_BASE_URL": "https://testserver"})


@pytest.fixture
def client(settings):
    with TestClient(create_app(settings, scheduler_enabled=False), base_url="https://testserver",
                    follow_redirects=False) as client:
        yield client


def signup(client, email="owner@example.com", business_name="Harbor and Co"):
    token = re.search('name="csrf" value="([^"]+)"', client.get("/signup").text)[1]
    return client.post("/signup", data={"csrf": token, "email": email, "password": "long password 123",
                                        "business_name": business_name})


def business_row(settings):
    with connect(settings.db_path) as db:
        return dict(db.execute("SELECT * FROM businesses").fetchone())


def add_person(settings, business_id, person_id="p1", email=EMAIL, stage="in_progress"):
    stamp = now()
    with connect(settings.db_path) as db:
        db.execute(
            "INSERT INTO people(id,business_id,name,email,pain,evidence_quote,source_url,stage,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (person_id, business_id, "Sam Rivera", email, PAIN, "evidence quote", "https://harbor.example",
             stage, stamp, stamp))


def add_message(settings, person_id, direction, body, channel="email", move=None, stamp=None, processed=0):
    stamp = stamp or now()
    message_id = "m-" + direction + "-" + channel + "-" + str(abs(hash(body)))[:8]
    with connect(settings.db_path) as db:
        db.execute(
            "INSERT INTO messages(id,person_id,direction,body,move,created_at,channel,reply_processed) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (message_id, person_id, direction, body, move, stamp, channel, processed))
    return message_id


def open_chat(settings, client, paused=0):
    assert signup(client).status_code == 303
    business = business_row(settings)
    with connect(settings.db_path) as db:
        db.execute("UPDATE businesses SET sender_name=?, outreach_paused=? WHERE id=?",
                   ("Maya Chen", paused, business["id"]))
    add_person(settings, business["id"])
    business = business_row(settings)
    with connect(settings.db_path) as db:
        person = dict(db.execute("SELECT * FROM people WHERE id='p1'").fetchone())
    token = chat.token_for(settings, business["id"], person["id"])
    return business, person, token


def messages(settings, person_id="p1"):
    with connect(settings.db_path) as db:
        return [dict(row) for row in db.execute(
            "SELECT * FROM messages WHERE person_id=? ORDER BY created_at, id", (person_id,))]


def test_valid_token_shows_the_thread_and_hides_private_fields(client, settings):
    business, person, token = open_chat(settings, client)
    add_message(settings, person["id"], "out", "We help shops catch the calls they miss.", move=MOVE)
    add_message(settings, person["id"], "in", "What does one location cost?")
    page = client.get("/c/" + token)
    assert page.status_code == 200
    assert "Maya Chen" in page.text
    assert "We help shops catch the calls they miss." in page.text
    assert "What does one location cost?" in page.text
    assert "Email" in page.text
    assert "Powered by Evorove" in page.text
    assert EMAIL not in page.text
    assert PAIN not in page.text
    assert MOVE not in page.text
    assert "Traceback" not in page.text
    assert 'name="sig"' in page.text
    assert 'name="text"' in page.text


def test_bad_tokens_are_a_plain_404(client, settings, monkeypatch):
    business, person, token = open_chat(settings, client)
    add_message(settings, person["id"], "out", "Only this thread.")
    other = signup(client, email="other@example.com", business_name="Other Co")
    assert other.status_code == 303
    with connect(settings.db_path) as db:
        other_id = db.execute("SELECT id FROM businesses WHERE name='Other Co'").fetchone()[0]
    tampered = token[:-1] + ("A" if token[-1] != "A" else "B")
    mismatched = chat.token_for(settings, other_id, person["id"])
    monkeypatch.setattr(chat, "TOKEN_MAX_AGE", -1)
    expired = chat.token_for(settings, business["id"], person["id"])
    for bad in (tampered, mismatched, expired, "not-a-token"):
        response = client.get("/c/" + bad)
        assert response.status_code == 404
        assert "Page not found" in response.text
        assert "Traceback" not in response.text
        assert "Only this thread." not in response.text


def test_post_stores_chat_and_returns_the_fragment(client, settings, monkeypatch):
    business, person, token = open_chat(settings, client)
    calls = []

    def fake_reply(biz, who, message_row):
        calls.append((biz["id"], who["id"], message_row["body"], message_row["channel"]))
        return ("ask_discovery", "Yes. One location, and you can stop when the book is full.")

    monkeypatch.setattr(chat, "reply_fn", fake_reply)
    page = client.get("/c/" + token)
    sig = re.search('name="sig" value="([^"]+)"', page.text)[1]
    response = client.post("/c/" + token + "/messages", data={"sig": sig, "text": "  Can you start with one location?  "},
                           headers={"HX-Request": "true"})
    assert response.status_code == 200
    assert "<!doctype" not in response.text.lower()
    assert "Can you start with one location?" in response.text
    assert "Yes. One location, and you can stop when the book is full." in response.text
    assert calls == [(business["id"], person["id"], "Can you start with one location?", "chat")]
    rows = messages(settings)
    inbound, outbound = rows
    assert inbound["direction"] == "in" and inbound["channel"] == "chat" and inbound["reply_processed"] == 1
    assert outbound["direction"] == "out" and outbound["channel"] == "chat"
    assert outbound["move"] == "ask_discovery"
    assert outbound["body"].startswith("Yes. One location")
    with connect(settings.db_path) as db:
        pending = outreach.pending_replies(db, business["id"])
    assert pending == []


def test_pending_replies_still_sees_a_real_email(client, settings):
    business, person, token = open_chat(settings, client)
    add_message(settings, person["id"], "in", "Please call me tomorrow.", channel="chat", processed=1)
    email_id = add_message(settings, person["id"], "in", "Replying from my inbox.", channel="email", processed=0)
    with connect(settings.db_path) as db:
        pending = outreach.pending_replies(db, business["id"])
    assert [row["id"] for row in pending] == [email_id]
    assert client.get("/c/" + token).status_code == 200


def test_opt_out_stops_contact_and_hides_the_box(client, settings, monkeypatch):
    business, person, token = open_chat(settings, client)
    monkeypatch.setattr(chat, "reply_fn", lambda *args: (_ for _ in ()).throw(AssertionError("brain")))
    page = client.get("/c/" + token)
    sig = re.search('name="sig" value="([^"]+)"', page.text)[1]
    response = client.post("/c/" + token + "/messages", data={"sig": sig, "text": "Please unsubscribe me"})
    assert response.status_code == 200
    assert "Understood. I'll leave it there." in unescape(response.text)
    assert 'name="text"' not in response.text
    with connect(settings.db_path) as db:
        stage = db.execute("SELECT stage FROM people WHERE id=?", (person["id"],)).fetchone()[0]
        suppressed = db.execute("SELECT reason FROM suppressions WHERE business_id=? AND email=?",
                                (business["id"], EMAIL)).fetchone()
    assert stage == "lost"
    assert suppressed[0] == "chat_opt_out"
    rows = messages(settings)
    assert len(rows) == 1 and rows[0]["direction"] == "in" and rows[0]["reply_processed"] == 1


@pytest.mark.parametrize("stage", ["done", "lost"])
def test_closed_stage_is_read_only(client, settings, monkeypatch, stage):
    _, person, token = open_chat(settings, client)
    with connect(settings.db_path) as db:
        db.execute("UPDATE people SET stage=? WHERE id=?", (stage, person["id"]))
    monkeypatch.setattr(chat, "reply_fn", lambda *args: (_ for _ in ()).throw(AssertionError("brain")))
    page = client.get("/c/" + token)
    assert page.status_code == 200
    assert 'name="text"' not in page.text
    response = client.post("/c/" + token + "/messages",
                           data={"sig": chat._form_token(settings, token), "text": "hello again"})
    assert response.status_code == 200
    assert "hello again" not in response.text
    assert messages(settings) == []


def test_paused_business_does_not_call_the_brain(client, settings, monkeypatch):
    _, _, token = open_chat(settings, client, paused=1)
    monkeypatch.setattr(chat, "reply_fn", lambda *args: (_ for _ in ()).throw(AssertionError("brain")))
    page = client.get("/c/" + token)
    assert "We'll reply by email shortly." in unescape(page.text)
    assert 'name="text"' not in page.text
    sig = chat._form_token(settings, token)
    response = client.post("/c/" + token + "/messages", data={"sig": sig, "text": "Are you there?"})
    assert response.status_code == 200
    assert unescape(response.text).count("We'll reply by email shortly.") == 1
    assert 'name="text"' not in response.text
    rows = messages(settings)
    assert len(rows) == 1 and rows[0]["direction"] == "in" and rows[0]["channel"] == "chat"


def test_rate_limit_returns_429_without_the_brain(client, settings, monkeypatch):
    business, person, token = open_chat(settings, client)
    monkeypatch.setattr(chat, "reply_fn", lambda *args: (_ for _ in ()).throw(AssertionError("brain")))
    page = client.get("/c/" + token)
    sig = re.search('name="sig" value="([^"]+)"', page.text)[1]
    recent = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    add_message(settings, person["id"], "in", "just now", channel="chat", stamp=recent, processed=1)
    gap = client.post("/c/" + token + "/messages", data={"sig": sig, "text": "another"})
    assert gap.status_code == 429
    assert "Please wait a moment before sending another message." in gap.text
    assert len(messages(settings)) == 1

    with connect(settings.db_path) as db:
        db.execute("DELETE FROM messages")
    earlier = (datetime.now(timezone.utc) - timedelta(seconds=30)).isoformat()
    add_message(settings, person["id"], "in", "earlier", channel="chat", stamp=earlier, processed=1)
    monkeypatch.setattr(chat, "PERSON_HOUR", 1)
    hour = client.post("/c/" + token + "/messages", data={"sig": sig, "text": "over the hour"})
    assert hour.status_code == 429
    assert len(messages(settings)) == 1

    with connect(settings.db_path) as db:
        db.execute("DELETE FROM messages")
    add_message(settings, person["id"], "in", "earlier", channel="chat", stamp=earlier, processed=1)
    monkeypatch.setattr(chat, "PERSON_HOUR", 30)
    monkeypatch.setattr(chat, "BUSINESS_HOUR", 1)
    business_cap = client.post("/c/" + token + "/messages", data={"sig": sig, "text": "over the shop"})
    assert business_cap.status_code == 429
    assert len(messages(settings)) == 1
    assert business["id"]


def test_form_signature_and_origin(client, settings):
    _, _, token = open_chat(settings, client)
    missing = client.post("/c/" + token + "/messages", data={"text": "hello"})
    assert missing.status_code == 403
    wrong = client.post("/c/" + token + "/messages", data={"sig": "not-signed", "text": "hello"})
    assert wrong.status_code == 403
    page = client.get("/c/" + token)
    sig = re.search('name="sig" value="([^"]+)"', page.text)[1]
    evil = client.post("/c/" + token + "/messages", data={"sig": sig, "text": "hello"},
                       headers={"Origin": "https://evil.example"})
    assert evil.status_code == 403
    null = client.post("/c/" + token + "/messages", data={"sig": sig, "text": "hello"},
                       headers={"Origin": "null"})
    assert null.status_code == 403
    assert messages(settings) == []


def test_templates_have_no_inline_script_or_handlers():
    for name in ("chat.html", "chat_messages.html"):
        text = (ROOT / "app/templates" / name).read_text()
        assert "<style" not in text
        assert " style=" not in text
        assert re.search(r"<script(?![^>]*\bsrc=)", text) is None
        assert re.search(r"\son[a-zA-Z]+=", text) is None


def test_migration_007_keeps_existing_messages(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    files = sorted((ROOT / "migrations").glob("*.sql"))
    assert files[-1].name == "007_chat_channel.sql"
    with connect(path) as db:
        db.execute("CREATE TABLE IF NOT EXISTS schema_migrations (name TEXT PRIMARY KEY)")
        for file in files[:-1]:
            db.executescript(file.read_text())
            db.execute("INSERT INTO schema_migrations(name) VALUES (?)", (file.name,))
        stamp = now()
        db.execute("INSERT INTO businesses(id,name,created_at) VALUES ('b1','Northwind',?)", (stamp,))
        db.execute(
            "INSERT INTO people(id,business_id,email,pain,evidence_quote,source_url,created_at,updated_at) "
            "VALUES ('p1','b1','a@b.example','pain','quote','https://a.example',?,?)", (stamp, stamp))
        db.execute(
            "INSERT INTO messages(id,person_id,direction,body,created_at) VALUES ('m1','p1','out','Earlier email',?)",
            (stamp,))
    migrate(path)
    with connect(path) as db:
        row = dict(db.execute("SELECT body, channel, reply_processed FROM messages WHERE id='m1'").fetchone())
        applied = [name for (name,) in db.execute("SELECT name FROM schema_migrations ORDER BY name")]
    assert row == {"body": "Earlier email", "channel": "email", "reply_processed": 0}
    assert applied[-1] == "007_chat_channel.sql"
    assert Path(files[-1]).read_text().count("COMMIT") == 0

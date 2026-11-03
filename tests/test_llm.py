from uuid import uuid4

import httpx
import pytest

from app import llm
from app.db import connect, current_run_id, migrate, now

SCHEMA = {"type": "object", "additionalProperties": False, "required": ["ok"],
          "properties": {"ok": {"type": "boolean"}}}


class Usage:
    input_tokens = 10
    output_tokens = 4
    cache_read_input_tokens = 0
    cache_creation_input_tokens = 0


class Messages:
    def __init__(self, text='{"ok": true}', fail=None):
        self.text = text
        self.fail = fail
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.fail and len(self.calls) <= self.fail[0]:
            raise self.fail[1]
        block = type("Block", (), {"type": "text", "text": self.text})()
        return type("Message", (), {"content": [block], "usage": Usage()})()


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "llm.sqlite3"
    monkeypatch.setenv("DB_PATH", str(path))
    monkeypatch.setenv("SECRET_KEY", "test-only-" * 6)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("ANTHROPIC_MODEL", "claude-test")
    monkeypatch.setenv("DAILY_TOKEN_BUDGET", "400000")
    monkeypatch.setenv("PER_RUN_TOKEN_BUDGET", "150000")
    migrate(path)
    return path


def patch_anthropic(monkeypatch, messages):
    class FakeAnthropic:
        def __init__(self, api_key, max_retries=0, timeout=60):
            assert api_key == "test-key"
            self.messages = messages

    monkeypatch.setattr("anthropic.Anthropic", FakeAnthropic)
    monkeypatch.setattr(llm, "_sleep", lambda seconds: None)


def status_error(cls, code):
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    return cls("limited", response=httpx.Response(code, request=request), body=None)


def open_run(path, tokens_in=0):
    run_id = str(uuid4())
    with connect(path) as database:
        database.execute("INSERT INTO runs (id,business_id,kind,started_at,status,tokens_in,tokens_out) VALUES (?,?,?,?,?,?,?)",
                         (run_id, None, "search", now(), "running", tokens_in, 0))
    return run_id


def test_schema_and_fenced_json(db, monkeypatch):
    messages = Messages('```json\n{"ok": true}\n```')
    patch_anthropic(monkeypatch, messages)
    run_id = open_run(db)
    token = current_run_id.set(run_id)
    try:
        assert llm.ask("system", "user", schema=SCHEMA, purpose="offer_reading") == {"ok": True}
    finally:
        current_run_id.reset(token)
    with connect(db) as database:
        row = dict(database.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone())
    assert row["tokens_in"] == 10 and row["tokens_out"] == 4
    assert messages.calls[0]["model"] == "claude-test"
    assert messages.calls[0]["output_config"]["format"]["schema"] == SCHEMA


def test_schema_mismatch_is_rejected_after_the_tokens_are_counted(db, monkeypatch):
    messages = Messages('{"extra": 1}')
    patch_anthropic(monkeypatch, messages)
    run_id = open_run(db)
    token = current_run_id.set(run_id)
    try:
        with pytest.raises(llm.LLMRejected):
            llm.ask("system", "user", schema=SCHEMA, purpose="offer_reading")
    finally:
        current_run_id.reset(token)
    with connect(db) as database:
        row = database.execute("SELECT tokens_in FROM runs WHERE id=?", (run_id,)).fetchone()
    assert row["tokens_in"] == 10


def test_rate_limit_retries_and_a_client_error_does_not(db, monkeypatch):
    import anthropic
    slept = []
    messages = Messages(fail=(1, status_error(anthropic.RateLimitError, 429)))
    patch_anthropic(monkeypatch, messages)
    monkeypatch.setattr(llm, "_sleep", slept.append)
    assert llm.ask("system", "user", purpose="web_search") == {"ok": True}
    assert len(messages.calls) == 2 and slept == [0.5]
    denied = Messages(fail=(1, status_error(anthropic.BadRequestError, 400)))
    patch_anthropic(monkeypatch, denied)
    with pytest.raises(anthropic.BadRequestError):
        llm.ask("system", "user", purpose="web_search")
    assert len(denied.calls) == 1


def test_web_search_tool_and_corpus_exclude_model_text(db, monkeypatch):
    class ToolMessages(Messages):
        def create(self, **kwargs):
            self.calls.append(kwargs)
            text = type("Block", (), {"type": "text", "text": '{"ok": true, "note": "only-in-json"}'})()
            result = type("Result", (), {"title": "Acme Plumbing", "url": "https://acme.example"})()
            tool = type("Tool", (), {"type": "web_search_tool_result", "content": [result]})()
            return type("Message", (), {"content": [tool, text], "usage": Usage()})()

    messages = ToolMessages()
    patch_anthropic(monkeypatch, messages)
    noted = {"type": "object", "additionalProperties": False, "required": ["ok"],
             "properties": {"ok": {"type": "boolean"}, "note": {"type": "string"}}}
    assert llm.ask("system", "user", schema=noted, web_search=True, max_uses=3, purpose="web_search")["ok"] is True
    tool = messages.calls[0]["tools"][0]
    assert tool["type"] == "web_search_20250305" and tool["name"] == "web_search" and tool["max_uses"] == 3
    assert tool["user_location"]["country"] == "US"
    assert "Acme Plumbing" in llm.source_text.get() and "only-in-json" not in llm.source_text.get()


def test_daily_and_per_run_budgets_refuse_before_the_call(db, monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("Anthropic was called")

    monkeypatch.setattr("anthropic.Anthropic", boom)
    monkeypatch.setenv("DAILY_TOKEN_BUDGET", "0")
    with pytest.raises(llm.BudgetExhausted):
        llm.ask("system", "user", purpose="offer_reading")
    monkeypatch.setenv("DAILY_TOKEN_BUDGET", "400000")
    run_id = open_run(db, tokens_in=150_000)
    token = current_run_id.set(run_id)
    try:
        with pytest.raises(llm.BudgetExhausted):
            llm.ask("system", "user", purpose="offer_reading")
    finally:
        current_run_id.reset(token)


def test_missing_model_does_not_call_anthropic(db, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_MODEL", "")

    def boom(*args, **kwargs):
        raise AssertionError("Anthropic was called")

    monkeypatch.setattr("anthropic.Anthropic", boom)
    with pytest.raises(llm.LLMRejected, match="ANTHROPIC_MODEL"):
        llm.ask("system", "user", purpose="offer_reading")


def test_orphan_call_records_its_own_run(db, monkeypatch):
    assert current_run_id.get() is None
    patch_anthropic(monkeypatch, Messages())
    llm.ask("system", "user", purpose="offer_reading")
    with connect(db) as database:
        row = dict(database.execute("SELECT * FROM runs").fetchone())
    assert row["kind"] == "offer_reading" and row["status"] == "ok"
    assert row["tokens_in"] == 10 and row["tokens_out"] == 4

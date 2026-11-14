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


def _block(kind, **attrs):
    return type("Block", (), {"type": kind, **attrs})()


def _response(content, stop_reason=None, usage=None):
    return type("Message", (), {"content": content, "usage": usage or Usage(), "stop_reason": stop_reason})()


class Scripted:
    """A fake Anthropic Messages client that returns one prebuilt response
    per call, in order -- for scenarios a single canned reply can't cover
    (pause_turn continuations, a max_tokens retry)."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self.responses.pop(0)


def test_json_after_a_pre_search_text_block_is_still_parsed(db, monkeypatch):
    content = [
        _block("text", text="Let me search the web for this."),
        _block("web_search_tool_result", content=[_block("", title="Acme", url="https://acme.example")]),
        _block("text", text='{"ok": true}'),
    ]
    messages = Scripted([_response(content, stop_reason="end_turn")])
    patch_anthropic(monkeypatch, messages)
    assert llm.ask("system", "user", schema=SCHEMA, web_search=True, purpose="web_search") == {"ok": True}
    assert "Acme" in llm.source_text.get()


def test_pause_turn_continues_the_conversation_up_to_three_times(db, monkeypatch):
    paused = _response([_block("text", text="still thinking...")], stop_reason="pause_turn")
    final = _response([_block("text", text='{"ok": true}')], stop_reason="end_turn")
    messages = Scripted([paused, final])
    patch_anthropic(monkeypatch, messages)
    assert llm.ask("system", "user", schema=SCHEMA, purpose="web_search") == {"ok": True}
    assert len(messages.calls) == 2
    assert messages.calls[1]["messages"][-1] == {"role": "assistant", "content": paused.content}


def test_max_tokens_bumps_once_for_web_search_then_rejects(db, monkeypatch):
    truncated = _response([_block("text", text='{"ok": tr')], stop_reason="max_tokens")
    truncated_again = _response([_block("text", text='{"ok": tr')], stop_reason="max_tokens")
    messages = Scripted([truncated, truncated_again])
    patch_anthropic(monkeypatch, messages)
    with pytest.raises(llm.LLMRejected, match="max_tokens"):
        llm.ask("system", "user", schema=SCHEMA, web_search=True, purpose="web_search")
    assert len(messages.calls) == 2
    assert messages.calls[0]["max_tokens"] == 4096
    assert messages.calls[1]["max_tokens"] == llm.MAX_TOKENS_WEB_SEARCH


def test_null_in_a_string_field_is_coerced_to_empty_string(db, monkeypatch):
    schema = {"type": "object", "additionalProperties": False, "required": ["ok", "note"],
              "properties": {"ok": {"type": "boolean"}, "note": {"type": "string"}}}
    messages = Messages('{"ok": true, "note": null}')
    patch_anthropic(monkeypatch, messages)
    assert llm.ask("system", "user", schema=schema, purpose="offer_reading") == {"ok": True, "note": ""}


def _array_nodes(schema):
    """Every array-type node anywhere in a JSON schema tree: its own items,
    recursively, and every property of every object along the way."""
    if not isinstance(schema, dict):
        return
    if schema.get("type") == "array":
        yield schema
        yield from _array_nodes(schema.get("items"))
    for value in (schema.get("properties") or {}).values():
        yield from _array_nodes(value)


def test_no_schema_sent_to_the_model_sets_minitems_above_one():
    """Anthropic's structured outputs only accepts minItems 0 or 1 on an array
    -- anything higher is a 400 on every call (the search-preview outage this
    guards against). A count like "2 to 3 segments" belongs to Python, after
    the response, never in the schema handed to output_config."""
    import app.outreach as outreach
    import app.search as search
    checked = 0
    for module in (search, outreach):
        for name in dir(module):
            if not name.endswith("_SCHEMA"):
                continue
            schema = getattr(module, name)
            if not isinstance(schema, dict):
                continue
            for node in _array_nodes(schema):
                checked += 1
                assert node.get("minItems", 0) <= 1, f"{module.__name__}.{name} sets minItems > 1"
    assert checked > 0  # the scan actually found at least one array schema to check

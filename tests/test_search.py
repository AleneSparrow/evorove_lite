import json
from uuid import uuid4

import pytest

from app import board, search
from app.db import connect, migrate, now

BRIEF = (
    "Northwind sells field-service scheduling to US plumbing companies.\n"
    "Owners of plumbing companies still book jobs by phone."
)
PAGE = """<html><body>
<p>Jane Alvarez is the founder of Acme Plumbing.</p>
<p>Email jane.alvarez@acme.example for the owner.</p>
<p>The owner still answers every new inquiry herself.</p>
</body></html>"""
OFFER = {
    "product_quote": "field-service scheduling", "product_source": "brief",
    "price_quote": "", "price_source": "", "place_quote": "", "place_source": "",
    "promotion_quote": "", "promotion_source": "",
    "segments": [
        {"label": "US plumbing company owners",
         "evidence_quote": "Owners of plumbing companies still book jobs by phone.",
         "source_name": "brief", "channel": "business_listing"},
        {"label": "Field service owners", "evidence_quote": "field-service scheduling",
         "source_name": "brief", "channel": "industry_sites"},
    ],
}
CANDIDATE = {
    "company": "Acme Plumbing", "website": "https://acme.example", "us_state": "OH",
    "segment": "US plumbing company owners",
    "evidence_quote": "Owners of plumbing companies still book jobs by phone.",
    "source_url": "https://acme.example",
}
PERSON = {
    "name": "Jane Alvarez", "role": "founder", "email": "jane.alvarez@acme.example",
    "pain": "The owner still answers every new inquiry herself.",
    "evidence_quote": "The owner still answers every new inquiry herself.",
}


class Page:
    def __init__(self, url, html=PAGE):
        self.url = url
        self.content = html.encode()
        self.headers = {"content-type": "text/html; charset=utf-8"}

    def raise_for_status(self):
        return None


class Messages:
    def __init__(self, person=None, candidates=None, offer=None):
        self.person = PERSON if person is None else person
        self.candidates = [CANDIDATE] if candidates is None else candidates
        self.offer = OFFER if offer is None else offer
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        system = kwargs["system"]
        if system.startswith("Read the business materials"):
            body = self.offer
        elif system.startswith("Find US small"):
            body = {"candidates": self.candidates}
        elif system.startswith("Read one company site"):
            body = self.person
        else:
            raise AssertionError(system[:80])
        usage = type("Usage", (), {"input_tokens": 10, "output_tokens": 4,
                                    "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0})()
        block = type("Block", (), {"type": "text", "text": json.dumps(body)})()
        return type("Message", (), {"content": [block], "usage": usage})()


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "search.sqlite3"
    monkeypatch.setenv("DB_PATH", str(path))
    monkeypatch.setenv("SECRET_KEY", "test-only-" * 6)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("ANTHROPIC_MODEL", "claude-test")
    monkeypatch.setenv("DAILY_TOKEN_BUDGET", "400000")
    monkeypatch.setenv("PER_RUN_TOKEN_BUDGET", "150000")
    monkeypatch.delenv("MARKET_SIGNALS", raising=False)
    monkeypatch.delenv("CLIENT_ZERO_BUSINESS_ID", raising=False)
    migrate(path)
    return path


def business(path, brief=BRIEF, bid=None):
    bid = bid or str(uuid4())
    with connect(path) as database:
        database.execute("INSERT INTO businesses (id,name,created_at,brief) VALUES (?,?,?,?)", (bid, "Northwind", now(), brief))
    return bid


def install(monkeypatch, messages, html=PAGE):
    fetched = []

    def fake_get(url, **kwargs):
        fetched.append(url)
        return Page(url, html)

    class FakeAnthropic:
        def __init__(self, api_key, max_retries=0, timeout=60):
            self.messages = messages

    monkeypatch.setattr("anthropic.Anthropic", FakeAnthropic)
    monkeypatch.setattr("httpx.get", fake_get)
    return fetched


def people(path):
    with connect(path) as database:
        return [dict(row) for row in database.execute("SELECT * FROM people ORDER BY email")]


def search_run(path):
    with connect(path) as database:
        return dict(database.execute("SELECT * FROM runs WHERE kind='search'").fetchone())


def test_grounded_person_is_inserted_cold(db, monkeypatch):
    messages = Messages()
    install(monkeypatch, messages)
    bid = business(db)
    found = search.run_search(bid)
    assert len(found) == 1
    assert found[0]["email"] == "jane.alvarez@acme.example"
    assert found[0]["us_state"] == "OH"
    row = people(db)[0]
    assert row["stage"] == "cold" and row["business_id"] == bid
    assert row["evidence_quote"] == PERSON["evidence_quote"]
    assert row["evidence_quote"] in PAGE
    assert search_run(db)["status"] == "ok" and search_run(db)["found"] == 1
    assert search_run(db)["tokens_in"] == 30 and search_run(db)["tokens_out"] == 12
    search_call = next(call for call in messages.calls if call["system"].startswith("Find US small"))
    assert search_call["tools"][0]["max_uses"] == 5
    assert search_call["tools"][0]["type"] == "web_search_20250305"
    assert "US plumbing company owners" in search_call["messages"][0]["content"]
    assert all(not call["system"].startswith("Decide whether this market news") for call in messages.calls)


def test_quote_not_in_the_page_is_dropped(db, monkeypatch):
    person = dict(PERSON, evidence_quote="Invented pain that never appears.")
    install(monkeypatch, Messages(person=person))
    search.run_search(business(db))
    assert people(db) == []
    assert search_run(db)["status"] == "ok" and search_run(db)["found"] == 0


def test_missing_email_is_dropped(db, monkeypatch):
    html = "<html><body><p>Jane Alvarez is the founder of Acme Plumbing.</p><p>The owner still answers every new inquiry herself.</p></body></html>"
    install(monkeypatch, Messages(), html)
    search.run_search(business(db))
    assert people(db) == []


def test_non_us_company_is_dropped_without_a_fetch(db, monkeypatch):
    fetched = install(monkeypatch, Messages(candidates=[dict(CANDIDATE, us_state="Ontario")]))
    search.run_search(business(db))
    assert people(db) == [] and fetched == []


def test_suppressed_email_is_skipped(db, monkeypatch):
    install(monkeypatch, Messages())
    bid = business(db)
    with connect(db) as database:
        database.execute("INSERT INTO suppressions VALUES (?,?,?,?)", (bid, "jane.alvarez@acme.example", "unsubscribe", now()))
    assert search.run_search(bid) == []
    assert people(db) == []


def test_duplicate_email_is_skipped(db, monkeypatch):
    install(monkeypatch, Messages())
    bid = business(db)
    with connect(db) as database:
        database.execute("INSERT INTO people (id,business_id,name,email,pain,evidence_quote,source_url,stage,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                         ("existing", bid, "Existing", "jane.alvarez@acme.example", "Old pain", "Old quote", "https://acme.example", "cold", now(), now()))
    assert search.run_search(bid) == []
    assert [row["name"] for row in people(db)] == ["Existing"]


def test_same_email_on_another_business_is_kept(db, monkeypatch):
    install(monkeypatch, Messages())
    bid = business(db)
    other = business(db, bid=str(uuid4()))
    with connect(db) as database:
        database.execute("INSERT INTO people (id,business_id,name,email,pain,evidence_quote,source_url,stage,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                         ("other", other, "Other", "jane.alvarez@acme.example", "Old pain", "Old quote", "https://acme.example", "cold", now(), now()))
    assert len(search.run_search(bid)) == 1
    assert len(people(db)) == 2


def test_dry_run_writes_only_the_run_and_renders_markdown(db, monkeypatch):
    install(monkeypatch, Messages())
    bid = business(db)
    found = search.run_search(bid, dry_run=True)
    assert len(found) == 1 and people(db) == []
    with connect(db) as database:
        assert database.execute("SELECT COUNT(*) FROM log").fetchone()[0] == 0
        assert database.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0
    row = search_run(db)
    assert row["status"] == "ok" and row["found"] == 1
    assert "Jane Alvarez" in search.last_report and "jane.alvarez@acme.example" in search.last_report
    assert search.last_report == search.markdown_table(found)


def test_budget_already_spent_stops_clean(db, monkeypatch):
    monkeypatch.setenv("DAILY_TOKEN_BUDGET", "0")

    def boom(*args, **kwargs):
        raise AssertionError("Anthropic was called")

    monkeypatch.setattr("anthropic.Anthropic", boom)
    assert search.run_search(business(db)) == []
    row = search_run(db)
    assert row["status"] == "budget_stopped" and row["found"] == 0 and row["error"] is None
    assert people(db) == []


def test_budget_stops_after_a_partial_run(db, monkeypatch):
    monkeypatch.setenv("DAILY_TOKEN_BUDGET", "14")
    messages = Messages()
    install(monkeypatch, messages)
    assert search.run_search(business(db)) == []
    assert len(messages.calls) == 1
    assert search_run(db)["status"] == "budget_stopped"
    assert people(db) == []


def test_pain_words_missing_from_the_quote_are_dropped(db, monkeypatch):
    person = dict(PERSON, pain="The owner still misses weekend bookings.")
    install(monkeypatch, Messages(person=person))
    assert search.run_search(business(db)) == []
    assert people(db) == []


def test_pain_that_reuses_quote_words_is_kept(db, monkeypatch):
    person = dict(PERSON, pain="The owner answers every inquiry.")
    install(monkeypatch, Messages(person=person))
    found = search.run_search(business(db))
    assert len(found) == 1
    assert found[0]["pain"] == "The owner answers every inquiry."
    assert found[0]["evidence_quote"] == PERSON["evidence_quote"]


def test_model_abstains_and_no_person_is_saved(db, monkeypatch):
    empty = {"name": "", "role": "", "email": "", "pain": "", "evidence_quote": ""}
    install(monkeypatch, Messages(person=empty))
    assert search.run_search(business(db)) == []
    assert people(db) == []


def test_rejected_person_read_does_not_invent_one(db, monkeypatch):
    real = search.ask

    def wrapped(*args, **kwargs):
        if kwargs.get("purpose") == "decision_maker":
            raise search.LLMRejected("no person")
        return real(*args, **kwargs)

    monkeypatch.setattr(search, "ask", wrapped)
    install(monkeypatch, Messages())
    assert search.run_search(business(db)) == []
    assert people(db) == []


BETA_PAGE = """<html><body>
<p>Beta IT Services
Mark Stone is the owner.</p>
<p>Email mark.stone@beta.example for the owner.</p>
<p>The owner still answers every new inquiry herself.</p>
</body></html>"""


def beta_person(name):
    person = {
        "name": name, "role": "owner", "email": "mark.stone@beta.example",
        "pain": "The owner still answers every new inquiry herself.",
        "evidence_quote": "The owner still answers every new inquiry herself.",
    }
    candidate = dict(CANDIDATE, company="Beta IT Services", website="https://beta.example")
    return person, candidate


def test_newline_company_name_is_cleared_and_the_person_stays(db, monkeypatch):
    person, candidate = beta_person("Beta IT Services\nMark Stone")
    install(monkeypatch, Messages(person=person, candidates=[candidate]), BETA_PAGE)
    found = search.run_search(business(db))
    assert len(found) == 1
    assert found[0]["name"] == ""
    assert found[0]["email"] == "mark.stone@beta.example"
    assert found[0]["role"].lower() == "owner"
    assert people(db)[0]["name"] == ""


def test_company_word_used_as_a_name_is_cleared(db, monkeypatch):
    person, candidate = beta_person("Services")
    install(monkeypatch, Messages(person=person, candidates=[candidate]), BETA_PAGE)
    found = search.run_search(business(db))
    assert len(found) == 1 and found[0]["name"] == ""
    assert found[0]["role"].lower() == "owner"


def test_clean_name_without_company_words_is_kept(db, monkeypatch):
    person, candidate = beta_person("Mark Stone")
    install(monkeypatch, Messages(person=person, candidates=[candidate]), BETA_PAGE)
    found = search.run_search(business(db))
    assert found[0]["name"] == "Mark Stone"


# --- the small-business email relaxations (docs/agent-prompts/16) ---------

def test_generic_email_on_the_companys_own_domain_is_accepted(db, monkeypatch):
    person = dict(PERSON, email="info@acme.example")
    page = PAGE.replace("jane.alvarez@acme.example", "info@acme.example")
    install(monkeypatch, Messages(person=person), page)
    found = search.run_search(business(db))
    assert len(found) == 1 and found[0]["email"] == "info@acme.example"
    assert people(db)[0]["email_kind"] == "company"


def test_generic_email_on_another_domain_is_rejected(db, monkeypatch):
    person = dict(PERSON, email="info@othercompany.com")
    page = PAGE.replace("jane.alvarez@acme.example", "info@othercompany.com")
    install(monkeypatch, Messages(person=person), page)
    assert search.run_search(business(db)) == []
    assert people(db) == []


def test_free_mail_written_on_the_companys_own_site_is_accepted(db, monkeypatch):
    person = dict(PERSON, email="jane.alvarez@gmail.com")
    page = PAGE.replace("jane.alvarez@acme.example", "jane.alvarez@gmail.com")
    install(monkeypatch, Messages(person=person), page)
    found = search.run_search(business(db))
    assert len(found) == 1 and found[0]["email"] == "jane.alvarez@gmail.com"
    assert people(db)[0]["email_kind"] == "personal_free"


def test_free_mail_not_written_on_the_page_is_rejected(db, monkeypatch):
    person = dict(PERSON, email="jane.alvarez@gmail.com")
    install(monkeypatch, Messages(person=person), PAGE)  # PAGE only has the acme.example address
    assert search.run_search(business(db)) == []
    assert people(db) == []


def test_email_kind_direct_cases():
    assert search._email_kind("jane@acme.example", "https://acme.example") == ""
    assert search._email_kind("info@acme.example", "https://acme.example") == "company"
    assert search._email_kind("jane@gmail.com", "https://acme.example") == "personal_free"
    assert search._email_kind("info@othercompany.com", "https://acme.example") is None
    assert search._email_kind("jane@othercompany.com", "https://acme.example") is None


def test_pain_grounded_accepts_60_percent_of_keywords_and_rejects_40_percent():
    pain = "alpha beta gamma delta epsilon"
    assert search._pain_grounded(pain, "The report mentions alpha, beta and gamma in the notes.") is True
    assert search._pain_grounded(pain, "The report mentions alpha and beta in the notes.") is False


def test_pain_grounded_requires_every_keyword_below_two():
    assert search._pain_grounded("alpha", "nothing matches here") is False
    assert search._pain_grounded("alpha", "the word alpha is right here") is True


def test_role_found_near_the_name_on_the_about_page_is_accepted():
    pages = [("https://acme.example/about", "Meet the team. Jane Alvarez is the proud owner of this shop.")]
    assert search._role_from_pages(pages, "Jane Alvarez") is not None


def test_role_on_a_page_that_is_not_about_or_team_is_not_used():
    pages = [("https://acme.example/contact", "Jane Alvarez is the owner.")]
    assert search._role_from_pages(pages, "Jane Alvarez") is None


# --- the search query (docs/agent-prompts/16, "any seller" update) --------

def test_queries_have_no_small_business_wording():
    segments = [{"label": "US plumbing company owners", "channel": "industry_sites"}]
    for query in search._queries(segments):
        assert "small business" not in query.lower()


def test_queries_add_the_role_phrase_for_a_business_segment():
    segments = [{"label": "US plumbing company owners", "channel": "industry_sites"}]
    [query] = search._queries(segments)
    assert query == 'US plumbing company owners owner OR founder OR "head of sales" United States'


def test_queries_drop_the_role_phrase_for_an_individual_seller_segment():
    segments = [{"label": "Independent wedding photographers", "channel": "forums"}]
    [query] = search._queries(segments)
    assert query == "Independent wedding photographers forum United States"
    assert "owner" not in query and "founder" not in query


@pytest.mark.parametrize("channel,expected", [
    ("forums", "Freelance plumbers forum United States"),
    ("news", "Freelance plumbers news United States"),
    ("business_listing", "Freelance plumbers business directory United States"),
    ("industry_sites", "Freelance plumbers United States"),
])
def test_queries_use_a_one_word_signal_per_channel(channel, expected):
    segments = [{"label": "Freelance plumbers", "channel": channel}]
    assert search._queries(segments) == [expected]


# --- the funnel (docs/agent-prompts/16) -------------------------------------

def test_funnel_is_recorded_on_the_run_and_counts_duplicates(db, monkeypatch):
    # Same website and email found twice (messy web-search results do this):
    # the second is a same-run duplicate, not a second distinct company.
    second = dict(CANDIDATE, company="Acme Plumbing LLC")
    install(monkeypatch, Messages(candidates=[CANDIDATE, second]))
    search.run_search(business(db))
    with connect(db) as database:
        row = dict(database.execute("SELECT funnel FROM runs WHERE kind='search'").fetchone())
    funnel = json.loads(row["funnel"])
    assert funnel["returned"] == 2
    assert funnel["accepted"] == 1
    assert funnel["duplicate_or_suppressed"] == 1


def test_funnel_summary_picks_the_most_common_reason():
    funnel_json = json.dumps({"returned": 23, "no_role": 2, "email_other_domain": 15, "accepted": 0})
    checked, reason = board.funnel_summary(funnel_json)
    assert checked == 23
    assert reason == search.FUNNEL_REASONS["email_other_domain"]


def test_funnel_summary_handles_a_missing_or_empty_funnel():
    assert board.funnel_summary(None) == (0, '')
    assert board.funnel_summary('{}') == (0, '')


def test_client_zero_seeds_the_owner_brief(db, monkeypatch):
    offer = {
        "product_quote": "finds likely buyers on the open web", "product_source": "service.txt",
        "price_quote": "$199/month", "price_source": "service.txt",
        "place_quote": "", "place_source": "", "promotion_quote": "", "promotion_source": "",
        "segments": [
            {"label": "US B2B businesses with high-value deals",
             "evidence_quote": "US B2B businesses with high-value deals that want to replace or avoid a sales team.",
             "source_name": "service.txt", "channel": "industry_sites"},
            {"label": "Owners who want a sales conversation carried through closing",
             "evidence_quote": "carries the sales conversation through closing.",
             "source_name": "service.txt", "channel": "business_listing"},
        ],
    }
    messages = Messages(offer=offer, candidates=[])
    install(monkeypatch, messages)
    bid = business(db, brief=None, bid="client-zero")
    monkeypatch.setenv("CLIENT_ZERO_BUSINESS_ID", bid)
    assert search.run_search(bid) == []
    offer_user = messages.calls[0]["messages"][0]["content"]
    assert "SOURCE NAME: service.txt" in offer_user
    assert "Starter is $199/month" in offer_user and "Pro is $499/month" in offer_user
    assert "7-day free trial" not in offer_user
    assert search_run(db)["status"] == "ok"


def test_missing_brief_fails_closed(db, monkeypatch):
    install(monkeypatch, Messages())
    with pytest.raises(search.BriefMissing):
        search.run_search(business(db, brief=None))
    assert search_run(db)["status"] == "failed" and search_run(db)["error"] == "BriefMissing"
    assert people(db) == []


def test_ungrounded_segments_do_not_search(db, monkeypatch):
    offer = json.loads(json.dumps(OFFER))
    offer["segments"][0]["evidence_quote"] = "not in the brief"
    offer["segments"][1]["evidence_quote"] = "also missing"
    messages = Messages(offer=offer)
    install(monkeypatch, messages)
    with pytest.raises(search.OfferRejected):
        search.run_search(business(db))
    assert len(messages.calls) == 1
    assert search_run(db)["error"] == "OfferRejected"
    assert people(db) == []


# --- the 2-3 segment count, enforced in Python now that the schema sent to
# the model carries no minItems/maxItems (see app/search.py:OFFER_SCHEMA) ----

def test_more_than_three_grounded_segments_are_truncated_to_three(monkeypatch):
    body = (
        "Owners of plumbing companies still book jobs by phone. "
        "Field service owners rely on paper schedules for everything. "
        "Rural contractors miss calls during drive time every day. "
        "Small shops still use a shared paper calendar for bookings. "
        "This covers field-service scheduling for small teams."
    )
    offer = {
        "product_quote": "field-service scheduling", "product_source": "brief",
        "price_quote": "", "price_source": "", "place_quote": "", "place_source": "",
        "promotion_quote": "", "promotion_source": "",
        "segments": [
            {"label": "US plumbing company owners",
             "evidence_quote": "Owners of plumbing companies still book jobs by phone.",
             "source_name": "brief", "channel": "business_listing"},
            {"label": "Field service owners",
             "evidence_quote": "Field service owners rely on paper schedules for everything.",
             "source_name": "brief", "channel": "industry_sites"},
            {"label": "Rural contractors",
             "evidence_quote": "Rural contractors miss calls during drive time every day.",
             "source_name": "brief", "channel": "forums"},
            {"label": "Small shops",
             "evidence_quote": "Small shops still use a shared paper calendar for bookings.",
             "source_name": "brief", "channel": "news"},
        ],
    }
    monkeypatch.setattr(search, "ask", lambda *a, **k: offer)
    facts, segments, forbidden = search._read_offer([("brief", body)])
    assert facts["product"] == "field-service scheduling"
    assert len(segments) == 3
    assert [item["label"] for item in segments] == [
        "US plumbing company owners", "Field service owners", "Rural contractors"]


def test_fewer_than_two_grounded_segments_is_a_clear_rejection(monkeypatch):
    offer = {
        "product_quote": "field-service scheduling", "product_source": "brief",
        "price_quote": "", "price_source": "", "place_quote": "", "place_source": "",
        "promotion_quote": "", "promotion_source": "",
        "segments": [
            {"label": "US plumbing company owners",
             "evidence_quote": "Owners of plumbing companies still book jobs by phone.",
             "source_name": "brief", "channel": "business_listing"},
        ],
    }
    monkeypatch.setattr(search, "ask", lambda *a, **k: offer)
    body = "Owners of plumbing companies still book jobs by phone. This covers field-service scheduling."
    with pytest.raises(search.OfferRejected):
        search._read_offer([("brief", body)])

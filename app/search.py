"""Cycle 1: brief to a grounded Cold person. One web-search pass, then the company's own site."""
import ipaddress
import json
import logging
import math
import re
import sqlite3
from collections import Counter
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

from .db import connect, current_run_id, now
from .llm import BudgetExhausted, LLMRejected, ask, source_text
from .settings import load

log = logging.getLogger(__name__)
last_report = ""
CHANNELS = {"forums", "industry_sites", "news", "business_listing"}
# Copied from evorove_lead/owner-materials/service.txt (25 Oct 2026). Client 0 only.
CLIENT_ZERO_BRIEF = (
    "Evorove finds likely buyers on the open web and carries the sales conversation through closing.\n"
    "It is for US B2B businesses with high-value deals that want to replace or avoid a sales team.\n"
    "Evorove checks public fit, keeps a grounded reason, and shows the work on a CRM board.\n"
    "Starter is $199/month: a sales agent for the customer's own contacts.\n"
    "Pro is $499/month: Starter, plus a search for leads in the United States.\n"
    "There is no free trial."
)
_STATE_LINES = """
AL Alabama / AK Alaska / AZ Arizona / AR Arkansas / CA California / CO Colorado / CT Connecticut
DE Delaware / DC District of Columbia / FL Florida / GA Georgia / HI Hawaii / ID Idaho / IL Illinois
IN Indiana / IA Iowa / KS Kansas / KY Kentucky / LA Louisiana / ME Maine / MD Maryland
MA Massachusetts / MI Michigan / MN Minnesota / MS Mississippi / MO Missouri / MT Montana
NE Nebraska / NV Nevada / NH New Hampshire / NJ New Jersey / NM New Mexico / NY New York
NC North Carolina / ND North Dakota / OH Ohio / OK Oklahoma / OR Oregon / PA Pennsylvania
RI Rhode Island / SC South Carolina / SD South Dakota / TN Tennessee / TX Texas / UT Utah
VT Vermont / VA Virginia / WA Washington / WV West Virginia / WI Wisconsin / WY Wyoming
"""
STATES = {}
STATE_NAMES = {}
for _piece in _STATE_LINES.split("/"):
    _code, _name = _piece.split(None, 1)
    STATES[_code] = _name.strip()
    STATE_NAMES[_name.strip().casefold()] = _code
GENERIC = set("info support sales contact hello office admin team enquiries inquiries help general noreply no-reply webmaster".split())
FREE_MAIL = {"gmail.com", "googlemail.com", "yahoo.com", "outlook.com", "hotmail.com", "live.com", "icloud.com", "aol.com", "proton.me", "protonmail.com"}
ROLE_RE = re.compile(r"\b(co-founder|cofounder|founder|co-owner|owner|ceo|president|managing partner|proprietor|principal|director|head of sales|vp of sales|vice president of sales|sales director|chief revenue officer)\b", re.I)
COMMERCIAL_RE = re.compile(r"\$\d|\b(?:discount|guarantee|warranty|refund)\b", re.I)
LEADING = set("our the my your this a an meet read contact about get learn".split())
NAME_STOP = {"contact us", "about us", "our team", "read more", "learn more", "privacy policy"}
PAIN_STOP = set("""
a an the of to and or for in on at by from with as into over under about after before
between within without during per via than then so if but nor not no
is are was were be been being am
it its this that these those
he she they them his her their our your my we you i me him
herself himself itself themselves yourself ourselves
do does did doing have has had having
will would can could should may might must
still every each all any some such very just also too already often always never
here there when where who whom whose which what how why
""".split())
PAGE_PATHS = ("", "/about", "/team", "/contact", "/about-us", "/contact-us", "/our-team", "/meet-the-team")
# Only these look like a page that would actually say who owns the place --
# used by _role_from_pages, not by the general page fetch above.
ABOUT_TEAM_SUFFIXES = ("/about", "/team", "/about-us", "/our-team", "/meet-the-team")

# Every way a candidate can fall out of the funnel, in a sentence a human asked
# for -- never the key itself. "returned" and "accepted" are not rejections and
# have no phrase. Order does not matter: the most frequent one wins.
FUNNEL_REASONS = {
    "no_state": "none were in a US state we could identify",
    "no_website": "none had a usable website",
    "same_as_us": "none were a different company from yours",
    "bad_segment": "none matched one of your segments",
    "site_unreachable": "none had a site we could reach",
    "llm_no_person": "none had a decision-maker we could read",
    "no_quote": "none had a usable quote",
    "quote_not_on_page": "none had evidence we could find on their own site",
    "email_not_on_page": "none had an email we could find on their own site",
    "email_generic_or_free": "none had an email we could trust",
    "email_other_domain": "none had a public owner email",
    "no_role": "none had an owner or founder we could identify",
    "pain_not_grounded": "none had a pain we could tie to the evidence",
    "pain_invented": "none had a pain the evidence actually supports",
    "duplicate_or_suppressed": "none were new -- already on your board or suppressed",
}


class OfferRejected(Exception):
    """The brief did not produce two grounded US B2B segments."""


class BriefMissing(Exception):
    """This business has no brief, offer, or client-0 seed."""


def _prompt(name):
    return (Path(__file__).parent / "prompts" / name).read_text().strip()


def _string_schema(fields):
    return {"type": "object", "additionalProperties": False, "required": list(fields),
            "properties": {field: {"type": "string"} for field in fields}}


OFFER_SCHEMA = {"type": "object", "additionalProperties": False, "required": [
    "product_quote", "product_source", "price_quote", "price_source", "place_quote", "place_source",
    "promotion_quote", "promotion_source", "segments"], "properties": {
        **{field: {"type": "string"} for field in (
            "product_quote", "product_source", "price_quote", "price_source", "place_quote", "place_source",
            "promotion_quote", "promotion_source")},
        # No minItems/maxItems here: Anthropic's structured outputs only accepts
        # 0 or 1 on an array, and rejects the whole request (400) otherwise. The
        # 2-3 count is enforced in Python, after the response -- see _read_offer.
        "segments": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["label", "evidence_quote", "source_name", "channel"],
            "properties": {"label": {"type": "string"}, "evidence_quote": {"type": "string"},
                           "source_name": {"type": "string"}, "channel": {"type": "string"}}}}}}
PERSON_SCHEMA = _string_schema(("name", "role", "email", "pain", "evidence_quote"))
SEARCH_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["candidates"], "properties": {
    "candidates": {"type": "array", "items": _string_schema(
        ("company", "website", "us_state", "segment", "evidence_quote", "source_url"))}}}
SIGNAL_SCHEMA = {"type": "object", "additionalProperties": False,
                 "required": ["relevant", "segment_label", "channel", "evidence_quote"], "properties": {
                     "relevant": {"type": "boolean"}, "segment_label": {"type": "string"},
                     "channel": {"type": "string"}, "evidence_quote": {"type": "string"}}}


def _host(url):
    return (urlsplit(url).hostname or "").lower().removeprefix("www.")


def _blocked(hostname):
    host = hostname.lower().rstrip(".")
    if host in {"localhost", "metadata.google.internal"} or host.endswith((".local", ".internal")):
        return True
    try:
        return not ipaddress.ip_address(host).is_global
    except ValueError:
        return False


def _site(url):
    parts = urlsplit(str(url or "").strip())
    if parts.scheme != "https" or not parts.hostname or parts.username or _blocked(parts.hostname):
        return ""
    return "https://" + parts.hostname.lower()


def _us_state(value):
    text = str(value or "").strip()
    if text.upper() in STATES:
        return text.upper()
    return STATE_NAMES.get(text.casefold(), "")


def _quote(quote, body):
    text = str(quote or "").strip()
    if not text or len(text) > 800 or text not in (body or ""):
        return ""
    letters = [char for char in text if char.isalpha()]
    if "?" in text or (letters and sum(char.isupper() for char in letters) / len(letters) > 0.8):
        return ""
    return text


def _invented(text, source):
    folded = (source or "").casefold()
    return any(match.group(0).casefold() not in folded for match in COMMERCIAL_RE.finditer(text or ""))


def _one_sentence(value):
    text = " ".join(str(value or "").split())
    if not text or len(text) > 240 or text.count(".") > 1 or "?" in text:
        return ""
    return text


def _company_words(company):
    return {word.casefold() for word in re.findall(r"[A-Za-z]{2,}", company or "")}


def _person_name(value, company):
    """One to three capitalized words, with none taken from the company name."""
    text = str(value or "")
    if "\n" in text or "\r" in text:
        return ""
    words = text.split()
    while words and words[0].casefold() in LEADING:
        words.pop(0)
    if not 1 <= len(words) <= 3 or any(not re.fullmatch(r"[A-Z][a-z]+(?:-[A-Z][a-z]+)?", word) for word in words):
        return ""
    if any(word.casefold() in _company_words(company) for word in words):
        return ""
    name = " ".join(words)
    return "" if name.casefold() in NAME_STOP else name


def _pain_grounded(pain, quote):
    """Nouns and verbs left after the stop list must mostly occur in the
    evidence quote: at least 60%, and all of them below 2 keywords (too few
    to apply a percentage to honestly)."""
    keywords = [word.casefold() for word in re.findall(r"[A-Za-z]+", pain) if len(word) > 1 and word.casefold() not in PAIN_STOP]
    if not keywords:
        return False
    folded = quote.casefold()
    matched = sum(1 for word in keywords if re.search(r"\b" + re.escape(word) + r"\b", folded))
    if len(keywords) < 2:
        return matched == len(keywords)
    return matched >= max(2, math.ceil(len(keywords) * 0.6))


def _email_kind(email, website):
    """None when the domain disqualifies the email outright. Otherwise the
    email_kind to store: "" for the classic owner@company-domain address,
    "company" for a generic mailbox (info@, hello@, ...) on the company's
    own domain, "personal_free" for a personal address at a free provider --
    a small business's owner using their own gmail is normal, and the caller
    has already confirmed the address is written on the company's own site
    before asking here."""
    local, _, domain = email.partition("@")
    domain = domain.lower().removeprefix("www.")
    host = _host(website)
    generic = local in GENERIC
    if host and (domain == host or domain.endswith("." + host)):
        return "company" if generic else ""
    if domain in FREE_MAIL and not generic:
        return "personal_free"
    return None


def _role_from_pages(pages, name):
    """When the model's own `role` text names no title, fall back to the
    /about or /team page: a role keyword written near the person's own name
    is accepted the same as if the model had named it."""
    if not name:
        return None
    for url, text in pages:
        if not url.endswith(ABOUT_TEAM_SUFFIXES):
            continue
        for match in re.finditer(re.escape(name), text, re.I):
            window = text[max(0, match.start() - 120):match.end() + 120]
            found = ROLE_RE.search(window)
            if found:
                return found.group(0)
    return None


def materials_for(business, settings):
    rows = []
    if (business["brief"] or "").strip():
        rows.append(("brief", business["brief"].strip()))
    if (business["offer_text"] or "").strip():
        rows.append(("offer_text", business["offer_text"].strip()))
    if settings.get("CLIENT_ZERO_BUSINESS_ID") == business["id"]:
        rows.append(("service.txt", CLIENT_ZERO_BRIEF))
    return rows


def _read_offer(rows):
    payload = ask(_prompt("search_offer.md"), "\n\n".join(f"SOURCE NAME: {name}\n{body}" for name, body in rows),
                  schema=OFFER_SCHEMA, purpose="offer_reading")
    by_name = dict(rows)
    facts = {}
    for key in ("product", "price", "place", "promotion"):
        facts[key] = _quote(payload.get(key + "_quote"), by_name.get(str(payload.get(key + "_source") or "").strip(), ""))
    segments = []
    for item in payload.get("segments") or []:
        body = by_name.get(str(item.get("source_name") or "").strip())
        quote = str(item.get("evidence_quote") or "").strip()
        label = " ".join(str(item.get("label") or "").split())
        channel = str(item.get("channel") or "").strip()
        if body and label and len(label) <= 255 and channel in CHANNELS and 0 < len(quote) <= 800 and quote in body and not _invented(label, body):
            segments.append({"label": label, "evidence_quote": quote, "source_name": item["source_name"].strip(), "channel": channel})
        if len(segments) == 3:
            break
    if not facts["product"] or len(segments) < 2:
        raise OfferRejected("fewer than 2 grounded segments or no product quote")
    corpus = "\n".join(body for _, body in rows)
    forbidden = []
    if not facts["price"]:
        forbidden.append("price")
    if not re.search(r"\b(?:discount|refund)\b", corpus, re.I):
        forbidden.append("discount")
    if not re.search(r"\b(?:guarantee|warranty)\b", corpus, re.I):
        forbidden.append("guarantee")
    if not re.search(r"\b(?:legal|licensed|insured)\b", corpus, re.I):
        forbidden.append("legal_claim")
    return facts, segments, forbidden


def _signal_segment(segments, rows):
    if len(segments) >= 3:
        return segments
    try:
        payload = ask(_prompt("search_signal.md"), "Materials:\n" + "\n\n".join(body for _, body in rows),
                      schema=SIGNAL_SCHEMA, web_search=True, max_uses=2, purpose="market_signal")
    except LLMRejected:
        return segments
    quote = str(payload.get("evidence_quote") or "").strip()
    label = " ".join(str(payload.get("segment_label") or "").split())
    channel = str(payload.get("channel") or "").strip()
    if payload.get("relevant") is not True or channel not in CHANNELS or not label or len(label) > 255:
        return segments
    if not quote or len(quote) > 800 or quote not in source_text.get() or _invented(label, quote):
        return segments
    return segments + [{"label": label, "evidence_quote": quote, "source_name": "market", "channel": channel}]


def _queries(segments):
    lines = []
    for segment in segments:
        label = segment["label"]
        lines.append(f'{label} owner OR founder OR "head of sales" small business United States')
        extra = {"forums": "forum", "news": "news"}.get(segment["channel"], "")
        lines.append((f"{label} in the United States" if segment["channel"] == "business_listing" else f"{label} {extra} United States").strip())
    return lines


def fetch_text(url):
    import httpx
    from selectolax.lexbor import LexborHTMLParser
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname or _blocked(parts.hostname):
        return ""
    try:
        response = httpx.get(url, timeout=10, follow_redirects=True, headers={"User-Agent": "EvoroveLite/1.0"})
        response.raise_for_status()
    except httpx.HTTPError:
        return ""
    final = urlsplit(str(response.url))
    if final.scheme != "https" or not final.hostname or _blocked(final.hostname) or _host(final.geturl()) != _host(url):
        return ""
    kind = response.headers.get("content-type", "")
    if kind and "html" not in kind and "text/plain" not in kind:
        return ""
    tree = LexborHTMLParser(response.content[:500_000].decode("utf-8", "replace"))
    for node in tree.css("script, style, noscript"):
        node.decompose()
    return tree.body.text(separator="\n") if tree.body is not None else ""


def _pages(website):
    pages, total = [], 0
    for path in PAGE_PATHS:
        text = fetch_text(website + path)
        if not text:
            continue
        pages.append((website + path if path else website, text))
        total += len(text)
        if total >= 6000:
            break
    return pages


def _accept_person(parsed, pages, website, corpus, listed_url, company, funnel):
    quote = str(parsed.get("evidence_quote") or "").strip()
    if not quote or len(quote) > 800:
        funnel["no_quote"] += 1
        return None
    page = "\n".join(text for _, text in pages)
    source_url = next((url for url, text in pages if quote in text), "")
    if not source_url and quote in corpus:
        source_url = _site(listed_url)
    if not source_url:
        funnel["quote_not_on_page"] += 1
        return None
    email = str(parsed.get("email") or "").strip().lower().strip(".")
    if not email or email not in page.lower():
        funnel["email_not_on_page"] += 1
        return None
    kind = _email_kind(email, website)
    if kind is None:
        local, _, domain = email.partition("@")
        domain = domain.lower().removeprefix("www.")
        funnel["email_generic_or_free" if local in GENERIC or domain in FREE_MAIL else "email_other_domain"] += 1
        return None
    name = _person_name(parsed.get("name"), company)
    role_match = ROLE_RE.search(str(parsed.get("role") or ""))
    role = role_match.group(0) if role_match else _role_from_pages(pages, name)
    if not role:
        funnel["no_role"] += 1
        return None
    pain = _one_sentence(parsed.get("pain"))
    if not pain or not _pain_grounded(pain, quote):
        funnel["pain_not_grounded"] += 1
        return None
    if _invented(pain, page + "\n" + corpus):
        funnel["pain_invented"] += 1
        return None
    return {"name": name, "role": role, "email": email, "email_kind": kind, "pain": pain,
            "evidence_quote": quote, "source_url": source_url}


def _person(company, pages, website, corpus, forbidden, listed_url, funnel):
    remaining, blocks = 6000, []
    for url, text in pages:
        chunk = text[:remaining]
        if not chunk:
            break
        blocks.append(f"PAGE {url}\n{chunk}")
        remaining -= len(chunk)
    user = f"Company: {company}\nDo not invent or promise: {', '.join(forbidden)}.\n\n" + "\n\n".join(blocks)
    try:
        parsed = ask(_prompt("search_people.md"), user, schema=PERSON_SCHEMA, purpose="decision_maker")
    except LLMRejected:
        funnel["llm_no_person"] += 1
        return None
    return _accept_person(parsed, pages, website, corpus, listed_url, company, funnel)


def _candidate(business, item, segments, corpus, forbidden, funnel):
    state = _us_state(item.get("us_state"))
    website = _site(item.get("website"))
    company = " ".join(str(item.get("company") or "").split())
    labels = {segment["label"].casefold(): segment["label"] for segment in segments}
    segment = labels.get(str(item.get("segment") or "").strip().casefold())
    if not state:
        funnel["no_state"] += 1
        return None
    if not website:
        funnel["no_website"] += 1
        return None
    # An empty company name is as unusable as one that is plainly ours: either
    # way this is not a distinct real company to write to.
    if not company or company.casefold() == (business["name"] or "").casefold():
        funnel["same_as_us"] += 1
        return None
    if not segment:
        funnel["bad_segment"] += 1
        return None
    pages = _pages(website)
    if not pages:
        funnel["site_unreachable"] += 1
        return None
    person = _person(company, pages, website, corpus, forbidden, item.get("source_url"), funnel)
    if not person:
        return None  # _person/_accept_person already recorded the specific reason
    person.update({"company": company, "website": website, "us_state": state, "segment": segment})
    return person


def _kept(settings, business_id, person, dry_run):
    email = person["email"]
    with connect(settings.db_path) as db:
        if db.execute("SELECT 1 FROM suppressions WHERE business_id=? AND lower(email)=?", (business_id, email)).fetchone():
            return False
        if db.execute("SELECT 1 FROM people WHERE business_id=? AND lower(email)=?", (business_id, email)).fetchone():
            return False
        if dry_run:
            return True
        stamp = now()
        try:
            db.execute("INSERT INTO people (id,business_id,name,role,email,email_kind,company,website,us_state,segment,pain,evidence_quote,source_url,stage,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (str(uuid4()), business_id, person["name"], person["role"], email, person.get("email_kind") or "",
                        person["company"], person["website"], person["us_state"], person["segment"], person["pain"],
                        person["evidence_quote"], person["source_url"], "cold", stamp, stamp))
        except sqlite3.IntegrityError:
            return False
    return True


def markdown_table(rows):
    header = "| Company | Person | Role | Email | State | Segment | Pain | Source |"
    lines = [header, "| --- | --- | --- | --- | --- | --- | --- | --- |"]
    for row in rows:
        cells = [str(row.get(key) or "").replace("|", "/").replace("\n", " ")
                 for key in ("company", "name", "role", "email", "us_state", "segment", "pain", "source_url")]
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def _finish(settings, run_id, status, found, error, funnel):
    with connect(settings.db_path) as db:
        db.execute("UPDATE runs SET status=?, found=?, error=?, funnel=?, finished_at=? WHERE id=?",
                   (status, found, error, json.dumps(funnel), now(), run_id))


def _open_run(settings, business_id):
    run_id = str(uuid4())
    with connect(settings.db_path) as db:
        db.execute("INSERT INTO runs (id,business_id,kind,started_at,status) VALUES (?,?,?,?,?)", (run_id, business_id, "search", now(), "running"))
    return run_id


def _collect(settings, business, limit, dry_run):
    accepted = []
    funnel = Counter()
    try:
        rows = materials_for(business, settings)
        if not rows:
            raise BriefMissing("add a search brief first")
        facts, segments, forbidden = _read_offer(rows)
        if settings.get("MARKET_SIGNALS", "").strip().lower() in {"1", "true", "yes", "on"}:
            segments = _signal_segment(segments, rows)
        user = (f"What we sell, copied from the materials: {facts['product']}\n"
                f"Do not invent or promise: {', '.join(forbidden)}.\n"
                f"Return at most {limit} companies.\nQueries:\n" + "\n".join(f"- {query}" for query in _queries(segments)))
        payload = ask(_prompt("search_queries.md"), user, schema=SEARCH_SCHEMA, web_search=True, max_uses=5, purpose="web_search")
        corpus = source_text.get()
        seen = set()
        for item in list(payload.get("candidates") or [])[:limit * 3]:
            if len(accepted) >= limit:
                break
            funnel["returned"] += 1
            if not isinstance(item, dict):
                continue
            person = _candidate(business, item, segments, corpus, forbidden, funnel)
            if not person:
                continue  # the specific reason is already in funnel
            if person["email"] in seen or not _kept(settings, business["id"], person, dry_run):
                funnel["duplicate_or_suppressed"] += 1
                continue
            seen.add(person["email"])
            accepted.append(person)
            funnel["accepted"] += 1
    except BudgetExhausted:
        return accepted, True, funnel
    return accepted, False, funnel


def run_search(business_id: str, *, limit: int = 20, dry_run: bool = False) -> list[dict]:
    global last_report
    settings = load()
    with connect(settings.db_path) as db:
        business = db.execute("SELECT * FROM businesses WHERE id=?", (business_id,)).fetchone()
    if business is None:
        raise LookupError("business not found")
    business = dict(business)
    owned = current_run_id.get() is None
    token = None
    run_id = current_run_id.get()
    if owned:
        run_id = _open_run(settings, business_id)
        token = current_run_id.set(run_id)
    accepted, funnel = [], Counter()
    try:
        accepted, stopped, funnel = _collect(settings, business, limit, dry_run) if limit >= 1 else ([], False, Counter())
        if stopped:
            if owned:
                _finish(settings, run_id, "budget_stopped", len(accepted), None, funnel)
            else:
                with connect(settings.db_path) as db:
                    db.execute("UPDATE runs SET status='budget_stopped', found=?, funnel=? WHERE id=?",
                               (len(accepted), json.dumps(funnel), run_id))
            return accepted
        if owned:
            _finish(settings, run_id, "ok", len(accepted), None, funnel)
        return accepted
    except Exception as exc:
        if owned:
            _finish(settings, run_id, "failed", len(accepted), type(exc).__name__, funnel)
        raise
    finally:
        if dry_run:
            last_report = markdown_table(accepted)
        if funnel:
            log.info("search funnel run=%s %s", run_id, dict(funnel))
        if token is not None:
            current_run_id.reset(token)

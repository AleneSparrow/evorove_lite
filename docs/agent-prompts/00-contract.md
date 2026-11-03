# evorove_lite — общий контракт (читают все агенты первым)

Owner: Alena. Talk to her in Russian. Code, UI and every customer-facing string in English. Market: US B2B. Client 0 = Evorove selling its own subscription.

## Why
The current system (repos evorove, evorove-crm, evorove_lead) is ~65k lines, 3 services talking over HTTP, 3 Postgres DBs, SearxNG, a React SPA, SMS/WhatsApp/calendar/quotes/LoRA. It broke silently for a week because one service lost another's address. We are rebuilding the SAME customer-facing behaviour as one small program. The old repos stay as read-only reference: copy proven logic (prompts, policy, guards), do not copy architecture.

## Hard limits
- One process: Python 3.12, FastAPI + Jinja2 templates (+ htmx from a vendored file, no CDN, no Node build). One `uvicorn` worker.
- One database: SQLite file in WAL mode via stdlib `sqlite3` (thin helper module, no ORM). Migrations = numbered `.sql` files applied at startup.
- Scheduler inside the process (a small asyncio loop or APScheduler). No cron containers, no queues, no Redis, no Docker required.
- Mail: stdlib `smtplib` (587 STARTTLS) and `imaplib`. LLM: official `anthropic` SDK; web search via Claude's server-side web search tool — no SearxNG, no scraping libraries beyond `httpx` + `selectolax` for fetching a company's own page when Claude needs it.
- Dependencies: fastapi, uvicorn, jinja2, anthropic, httpx, selectolax, itsdangerous (sessions), argon2-cffi (passwords), pytest. Anything else needs the owner's yes.
- Own code target ≤ 3,000 lines (tests excluded). RAM ≤ 150 MB idle. If you exceed, say why.
- Never print, log or commit secrets. Settings come from one `.env` the owner fills. No git push / merge without the owner's yes.
- AI output is untrusted: validate enums, required fields, and that every quoted evidence string is a verbatim substring of the fetched source. Never invent price, discount, guarantee, slot, gender or region.

## Repo
https://github.com/AleneSparrow/evorove_lite (empty, created by the owner on 3 Nov 2026). Local path: /Users/alenakulish/dev/evorove_lite. Agent 1 copies this contract into the new repo as docs/CONTRACT.md in its first commit, so later agents read it from there.

## Repo layout
```
app/
  main.py          # FastAPI app, routes, startup (migrations, scheduler)
  db.py            # sqlite helpers + migrations runner
  settings.py      # .env -> dataclass
  auth.py          # signup/login/reset, sessions, roles (owner/staff)
  billing.py       # Lemon Squeezy checkout + webhook (+ test-mode allowlist)
  board.py         # board queries + person timeline
  search.py        # cycle 1: brief -> Claude web search -> grounded people -> Cold
  outreach.py      # cycle 2: first letter, IMAP replies, sales moves, offer, Done
  guards.py        # unsubscribe, postal address, suppression, quiet hours, daily cap
  llm.py           # one place that calls Anthropic, counts tokens, enforces budgets
  notify.py        # Telegram daily summary + failure alerts
  scheduler.py
  templates/       # landing, faq, legal, auth, board, settings, unsubscribe
  static/          # css, htmx.min.js, images
migrations/001_init.sql ...
tests/
scripts/import_from_railway.py   # step 2
```

## Database (migrations/001_init.sql) — the only shared truth
```sql
CREATE TABLE businesses (id TEXT PRIMARY KEY, name TEXT NOT NULL, created_at TEXT NOT NULL,
  plan TEXT, subscription_status TEXT NOT NULL DEFAULT 'incomplete', ls_customer_id TEXT, ls_subscription_id TEXT,
  offer_text TEXT, payment_link TEXT, postal_address TEXT, sender_name TEXT,
  smtp_host TEXT, smtp_port INTEGER, smtp_user TEXT, smtp_password_enc TEXT,
  imap_host TEXT, imap_port INTEGER, imap_user TEXT, imap_password_enc TEXT,
  daily_cap INTEGER NOT NULL DEFAULT 20, quiet_start TEXT DEFAULT '20:00', quiet_end TEXT DEFAULT '08:00',
  outreach_paused INTEGER NOT NULL DEFAULT 1, brief TEXT);
CREATE TABLE users (id TEXT PRIMARY KEY, email TEXT UNIQUE NOT NULL, password_hash TEXT NOT NULL,
  business_id TEXT REFERENCES businesses(id), role TEXT NOT NULL DEFAULT 'owner', created_at TEXT NOT NULL);
CREATE TABLE sessions (token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id), expires_at TEXT NOT NULL);
CREATE TABLE password_resets (token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL, expires_at TEXT NOT NULL, used INTEGER DEFAULT 0);
CREATE TABLE people (id TEXT PRIMARY KEY, business_id TEXT NOT NULL REFERENCES businesses(id),
  name TEXT, role TEXT, email TEXT NOT NULL, company TEXT, website TEXT, us_state TEXT, segment TEXT,
  pain TEXT NOT NULL, evidence_quote TEXT NOT NULL, source_url TEXT NOT NULL,
  stage TEXT NOT NULL CHECK (stage IN ('cold','in_progress','offer_made','done','lost')) DEFAULT 'cold',
  last_move TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  UNIQUE (business_id, email));
CREATE TABLE messages (id TEXT PRIMARY KEY, person_id TEXT NOT NULL REFERENCES people(id),
  direction TEXT NOT NULL CHECK (direction IN ('out','in')), subject TEXT, body TEXT NOT NULL,
  smtp_message_id TEXT, in_reply_to TEXT, move TEXT, created_at TEXT NOT NULL);
CREATE TABLE suppressions (business_id TEXT NOT NULL, email TEXT NOT NULL, reason TEXT NOT NULL, created_at TEXT NOT NULL,
  PRIMARY KEY (business_id, email));
CREATE TABLE payments (id TEXT PRIMARY KEY, business_id TEXT, person_id TEXT, source TEXT NOT NULL,
  event_name TEXT, test_mode INTEGER NOT NULL DEFAULT 0, amount_cents INTEGER, raw_event_id TEXT UNIQUE, created_at TEXT NOT NULL);
CREATE TABLE runs (id TEXT PRIMARY KEY, business_id TEXT, kind TEXT NOT NULL, started_at TEXT NOT NULL, finished_at TEXT,
  status TEXT, found INTEGER DEFAULT 0, sent INTEGER DEFAULT 0, tokens_in INTEGER DEFAULT 0, tokens_out INTEGER DEFAULT 0, error TEXT);
CREATE TABLE log (id INTEGER PRIMARY KEY AUTOINCREMENT, business_id TEXT, person_id TEXT, event TEXT NOT NULL, detail TEXT, created_at TEXT NOT NULL);
```
Changing this schema = new numbered migration + tell the other agents in the PR description.

## Module interfaces (stable — implement exactly these names)
```python
# search.py
def run_search(business_id: str, *, limit: int = 20, dry_run: bool = False) -> list[dict]: ...
# returns grounded candidates; when not dry_run inserts them into people (stage='cold'), dedup by (business_id,email), skips suppressed.

# outreach.py
def send_first_letters(business_id: str) -> int: ...          # cold people without an 'out' message, respecting guards
def poll_replies(business_id: str) -> int: ...                # IMAP -> messages(in) -> next move -> reply
def record_payment(business_id: str, person_id: str, source: str) -> None: ...  # -> stage 'done'

# guards.py
def can_send(business_id: str, email: str, now_utc) -> tuple[bool, str]: ...    # reason when False
def footer(business_id: str, person_id: str) -> str: ...                        # unsubscribe link + postal address

# llm.py
def ask(system: str, user: str, *, schema: dict | None = None, web_search: bool = False, max_uses: int = 5, purpose: str) -> dict: ...
# single entry to Anthropic; logs tokens to runs; refuses when daily token budget (DAILY_TOKEN_BUDGET, default 400000) is spent.
```

## Settings (.env names)
APP_BASE_URL, SECRET_KEY, DB_PATH, ANTHROPIC_API_KEY, ANTHROPIC_MODEL, DAILY_TOKEN_BUDGET, LEMONSQUEEZY_API_KEY, LEMONSQUEEZY_WEBHOOK_SECRET, LEMONSQUEEZY_STORE_ID, LEMONSQUEEZY_VARIANT_STARTER, LEMONSQUEEZY_VARIANT_PRO, BILLING_TEST_MODE_ALLOWED_EMAILS, SYSTEM_SMTP_HOST/PORT/USER/PASSWORD/FROM (password reset + summaries), TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, CLIENT_ZERO_BUSINESS_ID, TIMEZONE_OWNER=Asia/Ho_Chi_Minh.

## Reference code to read (copy logic, not structure)
- First letter / pain-first: evorove `src/persistence/outbound_first_touch.py`, `src/persistence/outreach_service.py` (`draft_first_email`), commits of 26 Oct 2026 "Pain first, then touch".
- Sales moves: evorove `src/engine/sales_policy.py`, `src/ai/sales_prompts.py`, `src/ai/sales_response_prompts.py`, `src/ai/sales_models.py`, `src/engine/sales_response_validator.py`.
- Guards: evorove `src/persistence/email_outreach_service.py`, `src/persistence/email_inbox_service.py`, unsubscribe route `src/api/routes/unsubscribe.py`.
- Billing: evorove `src/persistence/billing_service.py`, `src/persistence/lemonsqueezy_client.py`.
- Search: evorove_lead `marketing_analysis.py`, `decision_maker_llm.py`, `market_signal_llm.py`, `selection.py`, `client_zero.py`, `owner-materials/`, `docs/cycle-1-contract.md`.
- Site copy: evorove `web/app/src/content/` (legal.ts, faq.ts), `web/app/src/pages/Landing.tsx`.

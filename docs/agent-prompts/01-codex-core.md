# Агент 1 — Codex: каркас, вход, оплата, доска, сайт

Запускать первым. Агенты 2 и 3 стартуют, когда каркас (п. 1–3) смёржен владельцем.

```text
Read docs/agent-prompts/axe/00-contract.md in the evorove repo first and follow it exactly. You work in the new repo evorove_lite (the owner created it empty). The old repos are read-only reference.

Your scope: app/main.py, db.py, settings.py, auth.py, billing.py, board.py, notify.py, scheduler.py, templates/, static/, migrations/001_init.sql, tests for these. Do NOT write search.py or outreach.py logic — create them as stubs with the exact interface signatures from the contract that raise NotImplementedError, so the other agents fill them.

1. Skeleton: layout from the contract, db.py with WAL + foreign_keys ON + migrations runner, settings.py, /health, pytest wired. A Makefile or justfile with: run, test, lint. README in Russian for the owner: how to run locally in 3 commands.
2. Auth: signup (creates business + owner user), login, logout, password reset by email (system SMTP), argon2 hashes, signed session cookie (HttpOnly, Secure, SameSite=Lax), rate limit on login/reset by IP+email in SQLite. Same password rule as old app (>= 12 chars).
3. Billing: Lemon Squeezy checkout link per plan with custom_data.business_id; webhook with signature check, idempotent by event id; meta.test_mode=true activates a plan ONLY if the business owner's email is in BILLING_TEST_MODE_ALLOWED_EMAILS, otherwise logged and ignored (200). Tests for: live event, allowed test event, stranger test event stays 'incomplete'.
   Done-criterion for steps 1-3: owner can sign up locally, log in, reset password (mail captured in a dev mailbox / console), pay in test mode with an allowlisted email. Ask the owner to merge before continuing.
4. Board (owner-only pages, server-rendered + htmx): four columns Cold / In progress / Offer made / Done with counts; click a card -> right pane: company, person, role, state, pain, evidence quote with source link, full message timeline. Cold person with no messages shows "The engine has not written yet." No discard/pause/takeover buttons. A "Find people" button calls search.run_search in a background task and shows the last run status. Settings page: offer text, payment link, postal address (required, >= 10 chars), sender name, SMTP/IMAP fields (passwords encrypted at rest with SECRET_KEY-derived key, never shown back), daily cap, quiet hours, outreach paused toggle, brief text.
5. Public site: port Landing, FAQ, Terms, Privacy, DPA, Subprocessors from the old SPA as static Jinja templates (English copy as-is, minus the removed fake CS-1042 log; legal must say finding people IS part of the service). Unsubscribe page (one click, adds to suppressions, confirms).
6. notify.py: Telegram message helper; daily summary at 09:00 Asia/Ho_Chi_Minh: last search run (ok/failed, found), letters sent, replies, offers, payments, errors in 24h; immediate alert when any scheduled job fails.
7. scheduler.py: in-process jobs — search daily 13:00 UTC per paid business with outreach not paused; send_first_letters every 15 min inside allowed hours; poll_replies every 5 min; daily summary. Each job writes a row to runs and never crashes the process.
8. Measure: RSS idle after 10 min; line count of app/ (excluding templates/static). Report both.

Before each item say which files you touch. After each item: what is done, what the owner must type into .env. Run tests. No push without the owner's yes.
```

# Агент — Claude Code CLI: блокеры запуска в коде

Идёт параллельно с `05-search-signals.md` (поиск, его делает другой агент).
Владелец: Алёна. Отчёт — на русском. Код и строки — на английском.

```text
Read docs/CONTRACT.md first and follow it exactly. Repo: evorove_lite.
Your scope ONLY: deploy/checks.py, Makefile, app/templates/terms.html,
the reply section of app/outreach.py (handle_reply, attempt_reply, pending_replies,
answer_pending_replies), and tests for these.
Another agent is rewriting app/search.py, app/prompts/search_*.md and may touch
app/llm.py right now. Do not open those files for editing. Do not edit guards.py,
billing.py, scheduler.py or migrations. No new dependency.
No push or merge: the owner does both. Before editing, list the files you touch and
wait for the owner's yes.

Goal: the product goes live next week. Close the three things in code that would
embarrass or block a live launch. Keep every change small.

## 1. `make launch-check` — refuse to launch with placeholders

Today `make preflight` only WARNs when OPERATOR_NAME / OPERATOR_ADDRESS are empty,
so the public Terms, Privacy and DPA show "[operator name pending]". Add a separate
target `launch-check` (new function in deploy/checks.py, reuse what is there).
It prints only OK / FAIL lines with fixed names, never a value, a response body or an
exception text, and exits non-zero on any FAIL:

- OPERATOR_NAME and OPERATOR_ADDRESS are set
- the templates terms.html, privacy.html, dpa.html contain no "pending" and no
  "will be added" after rendering with the real settings
- TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are set, and `runs` has a summary with
  status 'ok' in the last 26 hours
- for the client-zero business (CLIENT_ZERO_BUSINESS_ID): postal_address has at least
  10 characters; SMTP and IMAP host, user and encrypted password are present;
  `brief` is not empty
- BILLING_TEST_MODE_ALLOWED_EMAILS: print INFO (not FAIL) when it is non-empty, so
  the owner remembers test payments are still honoured for those accounts
- Lemon Squeezy key mode: only if the Lemon Squeezy API documents a field that says
  whether the key is a test key. Read their docs; do not guess a field name. If it is
  not documented, skip this check and say so in the report.

Do not change `preflight` behaviour: it runs before the app starts and must keep
passing on a server that is not launched yet.

## 2. Terms, section 15 (governing law and venue)

app/templates/terms.html, section 15 is a stub: "This section will be added."
ASK the owner which governing law and which venue she chooses. Do not pick one, do not
suggest one as a default, and do not write the section until she answers. Then write
two or three plain sentences in the style of the rest of the page, update the
"Last updated" date, and keep the existing legal-page tests green.
No promise, number or result claim anywhere.

## 3. Replies (two open findings from docs/ACCEPTANCE_2.md, item 5 b and c)

Item 5 a (answering old imported threads) is already closed by migration 006 and
`pending_replies`; verify that with a test if none exists, change nothing there.

b) Two unanswered inbound messages from one person currently produce two replies in a
   row. Wanted: one reply per person per poll, written against the newest inbound
   message, with the earlier unanswered ones from that person marked handled
   (`reply_processed=1`) in the same step so they are never answered later.
   Opt-out and hostile handling must still run on EVERY inbound message.
c) A reply blocked by a guard writes a `reply_blocked` log row on every poll (every
   5 minutes). Wanted: at most one row per person and reason per owner-local day.

## Tests (no network)

- launch-check: each condition above failing alone -> FAIL line and non-zero exit;
  all satisfied -> zero exit; output contains no setting values
- preflight unchanged
- two inbound messages, one person -> exactly one outbound reply, both inbound rows
  end up handled; an opt-out in the OLDER of the two still stops contact
- blocked reply polled 10 times in one day -> one reply_blocked row
- imported history is not answered on the first poll
`make test` and `make lint` stay green.

## Report to the owner (in Russian)

Files touched; lines before and after (app/*.py is 3,035 against a 3,000 target, do
not grow it: remove what this task makes dead); the exact command to run
`make launch-check` on the server; anything you skipped and why.
```

# Агент — поиск лидов «от сигнала к компании» (цикл 1, вторая версия)

Заменяет логику из `02-grok-search.md`. Интерфейс `run_search` и схема БД не меняются.
Владелец: Алёна. Отчёт — на русском. Код, промпты и строки — на английском.

```text
Read docs/CONTRACT.md first and follow it exactly. Repo: evorove_lite.
Your scope ONLY: app/search.py, app/prompts/search_*.md, tests/test_search.py.
Touch app/llm.py only if step 3 below cannot be done without it, and say so first.
Do not edit outreach.py, guards.py, scheduler.py, billing.py, templates, or migrations.
No schema change. No new dependency. No push or merge: the owner does both.
Before editing, say which files you touch.

## Why this task exists

The current search finds companies by segment and then reads the company's own
home/about/team/contact pages to write the "pain". Companies do not describe their
problems on their own site, so the stored pain is a restatement of the About page and
the "pain first, then touch" letter has nothing real to stand on. Every live run so far
returned leads the owner judged irrelevant.

Second cause: llm.py keeps only the TITLE and URL of each web search result in
source_text. The page text of a search result is never available, so a quote "copied
from a search result" can only ever match a title. Any evidence that matters must come
from a page this module fetches itself.

New rule: find the SIGNAL first, then the company, then the person.
A signal is public, dated behaviour that shows the buyer has the problem now.
What a company says about itself is marketing and is not a signal.

## Pipeline

1. Offer reading (keep `_read_offer`). Add to the offer output 1-3 `signals`: each has
   a `label`, a `kind` from a fixed enum, and an `evidence_quote` that is a verbatim
   substring of the owner's materials, same grounding rule as segments.
   Enum for `kind`, first version: `hiring`. Leave room to add kinds later; do not
   build the others now.
   Client 0 seed (add next to CLIENT_ZERO_BRIEF, client 0 only): US B2B service
   companies, roughly 2-50 people, that are hiring their first or second sales role
   (SDR, BDR, account executive, business development, head of sales). This segment is
   the owner's working hypothesis, not a fact; keep it in one constant so she can
   change it.

2. Signal search. One `ask(..., web_search=True)` per signal. Queries target the job
   posting itself, not lists of companies. Return candidates:
   company, website, us_state, signal_url, signal_kind.
   Drop directories, staffing agencies, "top N companies" articles, and the seller.

3. Fetch and ground the signal. Fetch `signal_url` with the existing `fetch_text`
   (https only, existing private-address guards, size cap, no new library).
   Allowed hosts: the company's own domain, or one of
   boards.greenhouse.io, job-boards.greenhouse.io, jobs.lever.co, jobs.ashbyhq.com,
   apply.workable.com. Anything else is dropped with reason `signal_host`.
   The owner has approved fetching these job-board hosts as an extension of the
   contract's "company's own page" rule; do not widen the list.
   `evidence_quote` must be a verbatim substring of the FETCHED signal page.
   The company name must appear in that page, or the page must be on the company's
   own domain. Otherwise drop with reason `signal_not_theirs`.

4. Person. Keep the existing decision-maker step on the company's own site, with the
   same email rules (own domain, on the page, no shared inbox, no free mail).
   Widen where it looks: follow up to 6 same-host links found on the home page whose
   path or link text matches about, team, leadership, people, founders, contact.
   Keep the 6,000-character budget per company.
   The role string returned by the model must appear in the fetched site text.

5. Pain. One sentence, built from the SIGNAL quote, under the existing
   `_pain_grounded` rule (every noun and verb already occurs in the quote) and
   `_invented` rule. `source_url` = signal_url. `evidence_quote` = the signal quote.
   `segment` = the signal label. Nothing else about the stored row changes.

6. Funnel report. Count every candidate by outcome: accepted, or the first reason it
   was dropped (not_us, no_site, signal_host, signal_fetch_failed, quote_not_found,
   signal_not_theirs, no_person_email, role_not_on_site, pain_not_grounded, suppressed,
   duplicate). Write one `log` row per run: event='search_funnel', detail=JSON of
   counts, no names or emails. In dry_run append the same counts under the Markdown
   table. This report is the main deliverable: the owner needs to see where
   candidates are lost before anything is tuned further.

Budgets: keep max_uses <= 5 per ask and the existing per-run and daily token budgets.
Size: app/search.py <= 600 lines. app/*.py is already 3,035 lines against a 3,000
target; report the new total and delete the code this task makes dead.

## Tests (no network: stub Anthropic and httpx)

- signal quote present in the fetched signal page -> accepted as cold, source_url is
  the signal URL
- quote only in the search-result title, not in the fetched page -> dropped
- signal host not on the allow-list -> dropped, reason signal_host
- signal page does not name the company and is not on its domain -> dropped
- pain uses a word that is not in the signal quote -> dropped
- no personal email on the company site -> dropped, reason no_person_email
- non-US, suppressed, duplicate -> unchanged behaviour
- dry_run writes nothing but the runs row and returns the funnel counts
- budget exhausted -> status budget_stopped, clean stop
- funnel log row contains counts only
Existing tests must stay green: `make test`, `make lint`.

## Do not

- Do not guess or construct an email address. An address that is not on the page does
  not exist for this module.
- Do not loosen a grounding check to raise the count. A run that returns 3 grounded
  people is a better result than 20 ungrounded ones; say so in the report.
- Do not add a promise, number of leads, or result claim to any string.

## Report to the owner (in Russian)

1. Files touched, line counts before and after.
2. Read-only check: open the first-letter code in app/outreach.py and say whether a
   pain built from a hiring signal produces a sensible first letter. Quote one example
   draft built from a stubbed row. Do not edit outreach.py; describe what would need
   to change, if anything.
3. Cost estimate for one run (searches + tokens).
4. Ask the owner's yes before the first LIVE dry run. After it: the Markdown table,
   the funnel counts, and which drop reason removed the most candidates.
```

# Агент 2 — Grok: поиск лидов (цикл 1) в одном модуле

Стартует после того, как каркас агента 1 (п. 1–3) смёржен.

```text
Read docs/agent-prompts/axe/00-contract.md in the evorove repo first and follow it exactly. Repo: evorove_lite. Your scope ONLY: app/search.py, app/llm.py, tests/test_search.py, tests/test_llm.py, and prompt text files under app/prompts/search_*.md. Do not edit other modules; if you need a schema change, write a new migration and say so.

Goal: replace evorove_lead (7k lines + SearxNG + its own Postgres warehouse) with one module of <= 600 lines that keeps the behaviour that matters:
- Input: the business brief (businesses.brief) + offer_text + owner materials. For client 0 seed the brief from evorove_lead/owner-materials/service.txt.
- 1) Offer reading: what we sell, to whom, what we must not promise (port marketing_analysis.py logic: 4P + 2-3 US B2B segments, verbatim quotes only).
- 2) Search: Claude web search tool (server-side) with max_uses budget, queries built per segment; target US small/mid B2B service companies whose owner or head of sales has a business email on the company's own site.
- 3) Selection per candidate: company, website, US state, person name + role (decision maker, port decision_maker_llm.py rules), email found ON the company's own site (fetch with httpx, parse with selectolax), pain = one sentence, evidence_quote = verbatim substring of the fetched page or search result text, source_url, segment. Drop anyone without a grounded reason, without an email, outside the US, or already suppressed / already in people.
- 4) Market signals (old track 4): optional flag, off by default.
- dry_run=True returns the list and writes NOTHING except a runs row; also renders a Markdown table for the owner.
- llm.py: the single Anthropic entry point from the contract: JSON-schema-validated outputs, retries with backoff, per-run and daily token budget, tokens written to runs. Model from ANTHROPIC_MODEL.
Tests (no network: stub Anthropic + httpx): grounded person -> inserted as cold; quote not a substring -> dropped; no email -> dropped; non-US -> dropped; suppressed -> skipped; duplicate email -> skipped; dry_run writes nothing; budget exhausted -> run status 'budget_stopped', clean stop.
Report: line count, and a cost estimate per run of 20 people (tokens + searches) from a single live dry run the owner approves first.
Before editing say which files you touch. No push without the owner's yes.
```

# Агент 3 — Cursor: письма и продажа (цикл 2) + защиты рассылки

Стартует после того, как каркас агента 1 (п. 1–3) смёржен. Может идти параллельно с агентом 2.

```text
Read docs/agent-prompts/axe/00-contract.md in the evorove repo first and follow it exactly. Repo: evorove_lite. Your scope ONLY: app/outreach.py, app/guards.py, app/prompts/sales_*.md, tests/test_outreach.py, tests/test_guards.py. Use app/llm.py from agent 2 if merged; until then call through a thin local function with the same signature so the merge is trivial.

Goal: replace the ~45k-line engine's outreach/sales path with <= 900 lines that behave the same for the customer:
1. guards.py: can_send() checks in order — outreach_paused, mailbox configured, postal_address set, APP_BASE_URL set, suppressed, quiet hours in the RECIPIENT's US time zone (state -> tz table; port src/domain/us_postal_timezones.py / customer_timezone.py), daily cap (count of 'out' messages today for the business), bounce/complaint history. footer(): unsubscribe link (signed token, one click) + postal address + sender name. Port the rules from email_outreach_service.py and the unsubscribe route.
2. First letter (send_first_letters): "pain first, then touch" exactly as in the 26 Oct 2026 commits — the opening sentence names the observed pain and ends with one full stop; then one line connecting to the offer; one soft ask; no price, discount, guarantee, slot, or fact without a source; plain text; subject <= 60 chars; footer appended. Stage cold -> in_progress on successful SMTP send; message stored with smtp Message-ID.
3. Replies (poll_replies): IMAP fetch UNSEEN since last uid; match by In-Reply-To/References to our Message-ID, else by sender email; store as 'in'; detect STOP/unsubscribe/hostile -> suppress + stage 'lost', no reply. Otherwise choose the next move with the ported SalesPolicyEngine rules (src/engine/sales_policy.py: moves enum, discovery vs offer readiness, objections) and let Claude phrase it via sales_response prompts; validate with the ported sales_response_validator rules. One reply per inbound message, threaded (In-Reply-To/References), same guards.
4. Offer: when the policy reaches OFFER, the letter uses businesses.offer_text and payment_link; stage -> offer_made. Never mark done from a link alone.
5. record_payment(): stage -> done, log row. Called by billing.py when a Lemon Squeezy order/subscription event carries custom_data.person_id, and by an owner-only "Record payment" action on the card (manual fallback).
6. SMS: none. A person with a phone and no email is never contacted.
Tests (no network: fake SMTP/IMAP + stub LLM): letter starts with the pain sentence and contains no price; footer present; each guard blocks with the right reason; cap 20 enforced; quiet hours by recipient state; reply continues the same person (no duplicate person); STOP suppresses and never replies; offer -> offer_made; payment -> done; link alone does not make done.
Report line count. Before editing say which files you touch. No push without the owner's yes.
```

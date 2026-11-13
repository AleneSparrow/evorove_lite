# Агент — клиент 0: подписка нового клиента закрывает карточку в Done

Владелец: Алёна. Отчёт — на русском. Код и строки — на английском.
Не пересекается с 05 (поиск), 06 (блокеры запуска), 07 (формы входа).

```text
Read docs/CONTRACT.md first and follow it exactly. Repo: evorove_lite.
Your scope ONLY: app/billing.py (the webhook path for subscription events),
and tests. Read app/outreach.py (record_payment) and app/board.py but do not edit
them: other agents work in outreach.py now. No schema change unless unavoidable;
if it is, a new numbered migration and say so first.
No new dependency. No push or merge: the owner does both.
Before editing, list the files you touch and wait for the owner's yes.

## Why

Evorove's core claim is that it sells itself (client 0 = Evorove, business id in
CLIENT_ZERO_BUSINESS_ID). When client 0 makes an offer, the person must end up
with a working Evorove account AND client 0's card must move to Done, because
that self-sale is the proof the product works.

Today neither payment link closes that loop:
- a Lemon Squeezy product link with person_id: the card goes to Done, but the
  buyer gets no Evorove account (a person_id payment never touches a
  subscription);
- https://evorove.com/signup: the buyer signs up and subscribes in the app, but
  nothing connects that new subscription to the person on client 0's board.

The owner will set client 0's payment link to https://evorove.com/signup.

## Wanted

When a subscription webhook makes a business entitled (status becomes 'active'
for the first time for that business, real mode, not test_mode, or test_mode
for an email in BILLING_TEST_MODE_ALLOWED_EMAILS), look up that business's owner
email. If client 0 has a person with the same email (case-insensitive) at stage
'offer_made' or 'in_progress', call outreach.record_payment(client0_id,
person_id, 'evorove_subscription', db=db) inside the same transaction, and
insert one payments row for client 0 with source 'evorove_subscription', the
amount from the event when present, and a raw_event_id that makes a repeat
webhook a no-op.

Rules:
- Never for the client 0 business itself.
- Only on the first transition to active; renewals, updates and resumes do not
  touch client 0's board again.
- A person at 'cold', 'done' or 'lost' is left alone.
- Tenant isolation: nothing from client 0's people is shown to the new business,
  and nothing about the new business is written to client 0 except the stage
  change, the log row record_payment already writes, and the payments row.
- Do not log the email.

## Tests (no network)

- new business subscribes, owner email matches a client-0 person at offer_made ->
  that person is done, one payments row with source evorove_subscription
- same webhook delivered twice -> still one payments row, no second log row
- renewal / subscription_updated on an already active business -> no change
- email matches a person at cold or lost -> no change
- no CLIENT_ZERO_BUSINESS_ID set -> no change, no error
- test_mode event for an email not on the allow-list -> ignored as today
- existing tests stay green: `make test`, `make lint`

## Report to the owner (in Russian)

Files touched, lines before and after (app/*.py is over its 3,000-line target:
keep it small), and one sentence the owner can say to herself to check it live:
which test account to sign up with and what she should see on the board.
```

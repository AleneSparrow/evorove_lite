# Приёмка шага 1 — любой агент, который НЕ писал код (например, Claude Code)

```text
Read docs/roadmap-axe-2026-11.md and docs/agent-prompts/axe/00-contract.md. Repo evorove_lite, all three agents' work merged on a branch. You did not write this code: review it as an outsider.
1. Run the full test suite; report failures.
2. Walk the customer path locally end to end with fakes for SMTP/IMAP and a stubbed LLM: signup -> test-mode payment with allowlisted email -> settings -> Find people (dry run, then real with stub) -> first letter -> reply -> next move -> offer -> payment -> Done. Screenshot each board state.
3. Check every item of "Что должно работать точно так же" in the roadmap: present / missing / different. Table.
4. Check the hard limits: dependency list, own-code line count, RSS idle, no secrets in repo or logs, AI-output validation present.
5. Security pass: session cookie flags, CSRF on POST forms, webhook signature, unsubscribe token signed, password reset token single-use and expiring, SQL only parameterized.
Output a short report in Russian for the owner: ready / not ready, and the exact list of fixes.
```

# Агент — формы входа: показ пароля и нормальные ошибки

Владелец: Алёна. Отчёт — на русском. Код и строки интерфейса — на английском.
Идёт параллельно с 05 (поиск) и 06 (блокеры запуска): с ними не пересекается.

```text
Read docs/CONTRACT.md first and follow it exactly. Repo: evorove_lite.
Your scope ONLY: app/templates/auth.html, app/templates/settings.html (password
fields only), app/static/style.css, one new file app/static/password-toggle.js,
the HTTP error handling in app/main.py (http_errors and the form-route error path),
and tests.
Do not touch app/search.py, app/prompts/, app/llm.py, app/outreach.py (other agents
work there now), guards.py, billing.py, scheduler.py, migrations.
No new dependency. No push or merge: the owner does both.
Before editing, list the files you touch and wait for the owner's yes.

## Problem 1: no way to see the password

Every password field (login, signup, reset, and the SMTP/IMAP password fields on
/app/settings) gets an eye button inside the field that switches between hidden and
shown.

- The site sends `Content-Security-Policy: default-src 'self'`. Inline scripts and
  inline event handlers (onclick=...) are blocked. Put the logic in
  app/static/password-toggle.js, load it with <script src defer>, and do not weaken
  the CSP.
- The button is a real <button type="button">, keyboard reachable, with
  aria-label "Show password" / "Hide password" and aria-pressed. It must never
  submit the form.
- Without JavaScript the field still works as a normal password field (the button
  can stay hidden until the script runs).
- Use the existing design: dark background, cream text, orange accent, the
  fonts already in style.css. Eye icon as inline SVG in the template, no icon
  library, no external file from a CDN.
- On /app/settings the saved password is never displayed; the eye only reveals
  what the owner is typing now.

## Problem 2: a wrong password shows raw JSON on a black page

Today a failed POST from an HTML form (for example /login with a wrong password)
returns FastAPI's JSON body, so the user sees {"detail":"Invalid email or password"}
as a line of text. Wanted: the same page re-rendered with a clear message above the
form, in the site design.

- Applies to the HTML form routes: /login, /signup, /forgot-password, /reset, and
  POST /app/settings, /app/contacts. Status codes stay as they are (400, 401, 403,
  413, 415, 429), only the body becomes the rendered page.
- The email the user typed stays filled in. The password field is always empty
  after an error. No other submitted value is echoed back unescaped.
- Message texts: reuse the existing HTTPException messages, they are already in
  plain English. Wrong login stays generic: "Invalid email or password" (never
  reveal whether the email exists). 429 shows the existing "Try again in 15
  minutes" text.
- The error block uses role="alert" so screen readers announce it, and the
  styling matches the site (no default browser red box).
- Keep the CSRF behaviour: the re-rendered form gets a fresh valid token.
- API-like routes keep their JSON: /billing/webhook, /api/v1/billing/webhook,
  /health, and htmx requests (hx-request: true) keep the current error fragment.
- 404 and 500 pages stay exactly as they are now.
- Do not log the submitted password or email anywhere.

## Tests (no network)

- login with a wrong password -> 401, HTML page with the message, email field
  keeps the typed email, password field empty, no JSON in the body
- signup with a short password -> 400, HTML with the message
- 429 on login -> HTML with the "15 minutes" text
- webhook and /health errors are still JSON; htmx error is still the fragment
- password field markup has the toggle button with type="button" and an
  aria-label; no inline script or on* attribute anywhere in templates
  (add a test that greps the templates for "<script>" without src and for " on"
  event attributes)
- existing tests stay green: `make test`, `make lint`; if the browser tests run in
  your environment, add one that clicks the eye and checks the input type changes

## Report to the owner (in Russian)

Files touched, lines before and after (app/*.py is over its 3,000-line target:
keep the Python change small, the toggle is JS and CSS), two screenshots: the login
page with the eye and the login page after a wrong password, desktop and phone.
```

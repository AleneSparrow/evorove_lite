# Billing access fix

Ветка `fix/billing-access` создана после `git fetch` от `origin/fix/acceptance-2`.

- Миграция `004_subscription_ends_at.sql` добавляет nullable TEXT-колонку
  `businesses.subscription_ends_at`. Существующие строки сохраняются; импортёр не меняется.
- Webhook сохраняет aware `attributes.ends_at` в UTC вместе со статусом в одной
  транзакции. Невалидная/naive дата даёт NULL; active/resume очищает срок.
- Общая `billing.entitled` допускает active либо cancelled строго до ends_at.
  Планировщик send/poll/search, импорт контактов и can_search/require_search
  используют эту проверку. После ends_at доступ закрыт без subscription_expired.
  on_trial доступа не даёт, согласно заданному условию active/cancelled.
- CSV и ручное добавление вызывают существующий `outreach.validate(note, GREET, '')`.
  Небезопасная заметка отображается в Skipped с причиной и не создаёт контакт.
- Playwright закреплён на 1.63.0. Для запуска установлен совместимый Chromium.
- `search.py`, `llm.py`, `outreach.py`, `guards.py` не менялись.

Проверки: 36 новых тестовых случаев; при подстановке прежних исходников
35 падают; отдельный миграционный тест также падает без новой миграции.
`make test`: **259 passed** (223 + 36). `make lint`: чисто.
`make test-browser`: **6 passed**. `git diff --check`: чисто.

Открыто из ACCEPTANCE_2: Terms и CLIENT_ZERO_BRIEF, обработка старых/двойных
ответов и повторное логирование reply_blocked, подпись Found, staging-проверка
реальных сервисов и замер RSS. В этой задаче они не менялись.
У ранее cancelled-подписок без сохранённого ends_at доступ закрыт до получения
валидного снимка webhook; срок не восстанавливается из предположений.
Push и деплой не выполнялись; .env и секреты не читались и не выводились.

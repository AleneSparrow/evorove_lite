# Исправления приёмки core

Ветка: `fix/acceptance-core` от `main`. Миграции и схема не менялись.

## B1 — Origin браузерных форм

Referrer-Policy заменена на `same-origin`. `Origin: null` явно запрещён; чужой
Origin тоже возвращает 403. Проверка CSRF и Secure/HttpOnly cookie сохранена.
Регистрация, выход/вход и сохранение настроек проходят в Chromium по HTTPS,
причём тесты проверяют Origin фактических POST-запросов.

Тест: `tests/test_acceptance_core.py::test_b1_same_origin_referrer_policy_and_reject_null_origin`;
браузерные `test_signup_in_chromium`, `test_login_in_chromium`, `test_settings_form_in_chromium`.

## B2 — переход на checkout

CSP `form-action` разрешает только `'self' https://*.lemonsqueezy.com`.
Остальные директивы CSP сохранены. Тест кликает Choose Starter, получает настоящий
303 от приложения и проверяет итоговый URL/страницу без нарушения CSP.

Для изоляции тестов API checkout замокирован. Тестовый CONNECT-прокси разрешает
только `sandbox.lemonsqueezy.com:443` и направляет его в локальный временный TLS-сервер;
другие внешние назначения запрещены. Это сохраняет в Chromium реальный внешний
origin и проверку CSP после 303, обходя ограничение Playwright route на цепочках
редиректов. Реальных checkout/платежей у провайдера нет, production-настройки не меняются.

Тест: `test_b2_csp_allows_only_self_and_lemon_squeezy_form_targets`;
браузерный `test_choose_starter_follows_external_303_without_csp_block`.

## B3 — оплата оффера клиента отдельно от подписки бизнеса

События с custom_data.person_id обрабатываются в отдельной ветке до обновления
подписки бизнеса. `order_created` и `subscription_payment_success` проверяют карточку
внутри business_id, записывают оплату и вызывают существующий outreach.record_payment
в той же SQLite-транзакции. Совпадение ls_subscription_id бизнеса не требуется;
поле может быть NULL или указывать на другую подписку. Повторы не дублируют оплату.
Другие события с person_id не изменяют тариф и не переводят карточку в Done.
plan, subscription_status, ls_customer_id и ls_subscription_id бизнеса сохраняются.
Сохранены проверка подписи, test-mode allowlist и идемпотентность webhook.
Событие с person_id не может использовать fallback на подписку другого бизнеса.

Тесты `test_b3_*` покрывают оба успешных события, два статуса бизнеса,
отсутствующий/несовпадающий ls_subscription_id, все subscription snapshots,
повтор доставки и попытку зачислить оплату карточке чужого бизнеса.

## Остальные изменения

- Record payment показывается только для offer_made. Сервер отклоняет подтверждение
  для cold/in_progress/lost с 409; повтор для Done не создаёт новую оплату.
  Передаётся текущий Settings в record_payment, чтобы использовать ту же базу.
  Тест `test_record_payment_available_only_after_offer` проверяет все пять стадий.
- README отражает реализованные поиск и рассылку, удалена ошибочная оценка idle RSS;
  указан ориентир 70–90 MB из приёмки, без заявления о новом замере в этой ветке.
- В .env.example добавлены пустые MARKET_SIGNALS и PER_RUN_TOKEN_BUDGET;
  тест проверяет наличие новых имён и отсутствие значений во всех 23 переменных.
- `requirements-dev.txt` содержит Playwright; runtime requirements не менялись.
  `make test` не собирает tests/browser. `make test-browser` запускает браузерный
  набор отдельно. Тесты используют временные SQLite/сертификаты и выключенный scheduler.
- search.py, llm.py, outreach.py, guards.py не изменялись. Push не выполнялся.

## Проверка

- `make test` — 164 passed.
- `make test-browser` — 4 passed, Chromium 156 (Playwright), headless, настоящий HTTPS.
- `make lint`, `pip check`, `git diff --check` — успешно.

Запуск браузерного набора: см. README, раздел «Браузерная приёмка».

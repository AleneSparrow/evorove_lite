# evorove_lite

Один процесс FastAPI, SQLite WAL, серверные Jinja-шаблоны. Python **3.12**.
Сейчас реализован первый этап задания: каркас, авторизация, Lemon Squeezy.
Доска, публичный сайт и планировщик — следующий этап после merge владельцем.
Поиск и рассылка пока явно выбрасывают `NotImplementedError`.

## Локальный запуск: три команды

Из корня проекта:

```sh
sh scripts/setup.sh
${EDITOR:-vi} .env
make run
```

Нужны Python 3.12 и OpenSSL с поддержкой `-addext`. Если Python не в PATH,
перед первой командой укажите `PYTHON_BIN=/absolute/path/to/python3.12`.
В среде Codex можно использовать
`PYTHON_BIN=/Users/alenakulish/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3 sh scripts/setup.sh`.
Скрипт создаёт `.venv`, случайный `SECRET_KEY`, `.env` и локальный сертификат.
Откройте **https://localhost:8000** и примите локальный самоподписанный сертификат.
Cookie всегда Secure/HttpOnly/SameSite=Lax; через обычный HTTP вход не работает.
`/health` проверяет доступ к базе. Uvicorn работает одним worker, без access log,
чтобы query-параметры с токенами сброса не попадали в журнал.
В production используйте доверенный HTTPS-сертификат и соответствующий `APP_BASE_URL`.
Не включайте логирование URL с query string на reverse proxy.

## Что заполнить в .env

- Каркас: `APP_BASE_URL`, `DB_PATH`, случайный `SECRET_KEY` (скрипт создаёт его).
  Не меняйте ключ без необходимости: это аннулирует подписанные cookie.
- Авторизация: `SYSTEM_SMTP_HOST`, `SYSTEM_SMTP_PORT=587`, `SYSTEM_SMTP_USER`,
  `SYSTEM_SMTP_PASSWORD`, `SYSTEM_SMTP_FROM`. SMTP должен поддерживать STARTTLS
  с доверенным сертификатом. Для dev mailbox используйте SMTP-песочницу с STARTTLS.
  Ссылки сброса и пароли никогда не выводятся в консоль. При сбое SMTP пользователь
  видит нейтральный ответ, в таблице `log` появляется `reset_mail_failed` без секретов.
- Оплата: `LEMONSQUEEZY_API_KEY`, `LEMONSQUEEZY_WEBHOOK_SECRET`,
  `LEMONSQUEEZY_STORE_ID`, `LEMONSQUEEZY_VARIANT_STARTER`,
  `LEMONSQUEEZY_VARIANT_PRO`, `BILLING_TEST_MODE_ALLOWED_EMAILS` (список через запятую).
  Для проверки используйте test API key и ID тестовых вариантов.
- Остальные поля из `.env.example` предназначены для следующих этапов.

В Lemon Squeezy настройте webhook `https://<домен>/billing/webhook` на события
`subscription_created`, `subscription_updated`, `subscription_cancelled`,
`subscription_resumed`, `subscription_expired`, `subscription_paused`, `subscription_unpaused`,
`subscription_payment_success`, `subscription_payment_recovered`, `subscription_payment_failed`.
Обновления подписки управляют доступом; успешные invoice-события отдельно записывают
сумму платежа в `payments` и не дублируются по ID invoice.
Для локальной проверки нужен доступный HTTPS-адрес; `APP_BASE_URL` должен совпадать с ним.
Регистрация → выбор плана → hosted checkout → подписанный webhook → статус подписки.
Переход обратно из checkout сам по себе не активирует подписку.
Тестовое событие чужого владельца отвечает 200, записывает `billing_test_ignored`
и оставляет бизнес неактивированным. Подпись, дубли и устаревшие события проверяются.
При отсутствии отдельного event ID используется SHA-256 исходного тела: ID объекта
подписки не является ID доставки.

Контракты интеграции сверены с официальными страницами
[Create a Checkout](https://docs.lemonsqueezy.com/api/checkouts/create-checkout) и
[Sync With Webhooks](https://docs.lemonsqueezy.com/guides/developer-guide/webhooks).

## Проверка

```sh
make test
make lint
```

`lint` — проверка синтаксиса и пробелов без дополнительных зависимостей.
Тесты используют временные SQLite-файлы, HTTPS TestClient, перехват SMTP и mock checkout;
реальные письма, платежи и обращения к Lemon Squeezy не выполняются.
Для ручной приёмки: зарегистрироваться, выйти/войти, запросить и применить ссылку сброса,
оплатить тестовый план с allowlisted email и дождаться webhook.
Без заполненных SMTP/LS проверить реальную доставку и оплату невозможно.

## База и совместимость

`001_init.sql` буквально соответствует `docs/CONTRACT.md`.
`002_core_security.sql` добавляет SQLite rate limit и журнал идемпотентности webhook;
это нужно учесть следующим агентам. Миграции запускаются при старте приложения,
применение SQL и запись версии атомарны. Соединения включают foreign_keys и закрываются.
Все времена хранятся в UTC. Настройки и секреты не включены в git.
Старые проекты не изменяются.

Перед продолжением пунктов 4–8 требуется merge владельцем согласно
`docs/agent-prompts/01-codex-core.md`, пункт 3. Push и merge автоматически не выполняются.

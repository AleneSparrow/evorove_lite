# evorove_lite

Один процесс FastAPI, SQLite WAL, серверные Jinja-шаблоны. Python **3.12**.
Реализованы каркас, авторизация, Lemon Squeezy, доска, настройки, публичный сайт,
отписка, Telegram-сводка и встроенный планировщик.
Поиск и рассылка реализованы: Claude ищет людей с проверяемыми источниками,
первые письма и IMAP-ответы проходят через guards и политику продаж.

## Локальный запуск: три команды

Из корня проекта:

```sh
sh scripts/setup.sh
make test
make run
```

Нужны Python 3.12 и OpenSSL с поддержкой `-addext`. Если Python не в PATH,
перед первой командой укажите `PYTHON_BIN=/absolute/path/to/python3.12`.
В среде Codex можно использовать
`PYTHON_BIN=/Users/alenakulish/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3 sh scripts/setup.sh`.
Скрипт создаёт `.venv` и локальный сертификат. `.env` не нужен для dev-запуска
и автоматически не создаётся. При первом запуске приложение сохраняет случайный
`SECRET_KEY` в `.local/dev-secret` с правами 0600 и переиспользует его далее.
Все переменные в `.env.example` оставлены пустыми.
Откройте **https://localhost:8000** и примите локальный самоподписанный сертификат.
Cookie всегда Secure/HttpOnly/SameSite=Lax; через обычный HTTP вход не работает.
`/health` проверяет доступ к базе. Uvicorn работает одним worker, без access log,
чтобы query-параметры с токенами сброса не попадали в журнал.
В production используйте доверенный HTTPS-сертификат и соответствующий `APP_BASE_URL`.
Не включайте логирование URL с query string на reverse proxy.

## Локальный фейковый почтовый ящик

При `APP_BASE_URL=https://localhost:8000` (по умолчанию) и пустом `SYSTEM_SMTP_HOST`
письма сброса сохраняются в `.local/mailbox/*.eml` с правами 0600. Откройте последний
файл в редакторе/почтовой программе, перейдите по ссылке и задайте новый пароль.
Пароли и ссылки не печатаются в консоль. Файлы писем и dev-ключ исключены из git.
Этот режим доступен только для `localhost`/`127.0.0.1`; для внешнего адреса нужны
явный `SECRET_KEY` и настоящий SMTP. Не используйте localhost-режим в production.

## Что заполнить в .env позже

Скопируйте `.env.example` в `.env`, когда будете подключать реальные сервисы.
Пустые настройки не мешают локальным регистрации, входу и сбросу пароля.
Для checkout без настроенных LS возвращается понятная ошибка 503;
webhook и checkout проверяются в тестах с фиктивными значениями.

- Каркас: `APP_BASE_URL`, `DB_PATH`, случайный `SECRET_KEY` (в dev приложение создаёт его автоматически).
  Не меняйте ключ без необходимости: это аннулирует подписанные cookie.
- Авторизация: `SYSTEM_SMTP_HOST`, `SYSTEM_SMTP_PORT=587`, `SYSTEM_SMTP_USER`,
  `SYSTEM_SMTP_PASSWORD`, `SYSTEM_SMTP_FROM`. SMTP должен поддерживать STARTTLS
  с доверенным сертификатом. Для локальной работы можно оставить SMTP пустым.
  Ссылки сброса и пароли никогда не выводятся в консоль. При сбое SMTP пользователь
  видит нейтральный ответ, в таблице `log` появляется `reset_mail_failed` без секретов.
- Оплата: `LEMONSQUEEZY_API_KEY`, `LEMONSQUEEZY_WEBHOOK_SECRET`,
  `LEMONSQUEEZY_STORE_ID`, `LEMONSQUEEZY_VARIANT_STARTER`,
  `LEMONSQUEEZY_VARIANT_PRO`, `BILLING_TEST_MODE_ALLOWED_EMAILS` (список через запятую).
  Для проверки используйте test API key и ID тестовых вариантов.
- Уведомления: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`,
  `TIMEZONE_OWNER=Asia/Ho_Chi_Minh` (это значение по умолчанию). Пустые Telegram-поля
  отключают доставку и ежедневную задачу сводки; тесты перехватывают Telegram.
- Поиск и LLM: `ANTHROPIC_API_KEY`, `ANTHROPIC_MODEL`, `DAILY_TOKEN_BUDGET`,
  `PER_RUN_TOKEN_BUDGET` (лимит токенов на запуск), `MARKET_SIGNALS` (опциональный
  поиск рыночных сигналов; включение: `true`). Реальных значений для тестов не нужно.

В Lemon Squeezy настройте webhook `https://<домен>/billing/webhook` на события
`subscription_created`, `subscription_updated`, `subscription_cancelled`,
`subscription_resumed`, `subscription_expired`, `subscription_paused`, `subscription_unpaused`,
`subscription_payment_success`, `subscription_payment_recovered`, `subscription_payment_failed`,
`order_created`.
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
Локальный сброс можно полностью проверить через файловый mailbox без SMTP.
Реальную доставку SMTP и реальную оплату проверяйте после заполнения настроек.

## База и совместимость

`001_init.sql` буквально соответствует `docs/CONTRACT.md`.
`002_core_security.sql` добавляет SQLite rate limit и журнал идемпотентности webhook;
это нужно учесть следующим агентам. Миграции запускаются при старте приложения,
применение SQL и запись версии атомарны. Соединения включают foreign_keys и закрываются.
Все времена хранятся в UTC. Настройки и секреты не включены в git.
Старые проекты не изменяются.

## Доска и настройки

`/app` — четыре колонки, карточки с полным таймлайном в правой панели.
`/app/settings` — оффер, платёжная ссылка, почтовый адрес, brief, SMTP/IMAP,
лимит и тихие часы, пауза рассылки. Доступ только владельцу своего бизнеса.
Пароли почты шифруются Fernet с отдельным ключом, полученным из SECRET_KEY.
Зависимость `cryptography` согласована владельцем и включена в requirements.txt. Смена ключа требует повторного
ввода почтовых паролей. Пустое поле пароля сохраняет предыдущее значение.
Кнопка Find people требует активной подписки и brief, запускает поиск в фоне;
статус/ошибка видны на доске. Поиск и рассылка используют рабочие реализации.
Ручной поиск возможен при паузе рассылки; Search preview выполняет dry-run.

## Планировщик и Tunnel

Один asyncio-цикл работает внутри uvicorn: поиск в 13:00 UTC, первые письма каждые
15 минут вне тихих часов, IMAP каждые 5 минут, сводка в 09:00 TIMEZONE_OWNER.
После позднего старта дневные задачи догоняются один раз. Автоматические задания
применяются к active/on_trial бизнесам с outreach_paused=0. Внешние вызовы работают
в потоках этого же процесса. Запуски и ошибки записываются в runs; сбой не останавливает
следующие задания. Тесты не отправляют реальных уведомлений.

Uvicorn запускается с `--no-proxy-headers`, чтобы сохранить исходный TCP peer.
CF-Connecting-IP используется для rate limit только от 127.0.0.1/::1; заголовок от
любого другого адреса игнорируется. Порт приложения должен быть закрыт извне.

## Измерения и передача другим агентам

`python scripts/measure_idle.py` измеряет RSS одного worker через 600 секунд простоя,
с включённым scheduler и пустой тестовой SQLite; результат — `.local/idle-measurement.json`.
Подробности интеграции и новые таблицы `rate_limits`/`billing_events` из
`002_core_security.sql` описаны в [CORE_HANDOFF.md](docs/CORE_HANDOFF.md).
Публичные тексты перенесены из SPA; устаревшие описания возможностей и инфраструктуры
помечены заметкой о текущей версии и требуют обновления перед production-переездом.
Push и merge автоматически не выполняются.

По результатам приёмки idle RSS составляет примерно 70–90 MB.
Для нового замера используйте scripts/measure_idle.py; ранее указанная заниженная
цифра удалена. Пиковая память поиска/LLM требует отдельного измерения.


## Браузерная приёмка (отдельно от make test)

```sh
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m playwright install chromium
make test-browser
```

Playwright используется только в dev-зависимостях. Тесты запускают настоящий
Chromium и временный HTTPS uvicorn с отдельной SQLite. Проверяются регистрация,
выход/вход, отправка формы настроек и Choose Starter с 303-переходом на разрешённый
Lemon Squeezy host. API checkout замокирован; тестовый CONNECT-прокси направляет внешний checkout-host
на локальную HTTPS-страницу, сохраняя проверку CSP в Chromium;
реальных регистраций у провайдера, платежей и писем нет. Сертификат генерируется
только для тестового сервера, системное хранилище доверия не меняется.

Referrer-Policy — same-origin; Origin: null и чужой Origin для форм запрещены.
CSP form-action разрешает только self и https://*.lemonsqueezy.com.

События с custom_data.person_id относятся к оплате оффера клиента: order_created
и subscription_payment_success записывают оплату и переводят своего человека в Done,
не меняя plan/subscription_status/ls_subscription_id бизнеса и не требуя совпадения
его подписки с подпиской клиента. Остальные события с person_id не меняют тариф.
Ручная кнопка Record payment доступна только на Offer made; повтор подтверждения
уже Done не создаёт новую оплату.

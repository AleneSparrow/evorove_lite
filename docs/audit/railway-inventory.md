# Опись перед переездом `evorove.com` и `api.evorove.com` с Railway на VPS (`evorove_lite`)

Локальные репозитории старого стека найдены в `~/dev`:
- `~/dev/evorove` (`AleneSparrow/Evorove`) — сервис Railway `ai-business-process-engine` (обслуживает `evorove.com` и `api.evorove.com`), а также Cloudflare Worker `evorove-web` (`web/app/wrangler.web.toml`).
- `~/dev/evorove-crm` (`AleneSparrow/Evorove_CRM`) — сервис Railway `evorove-crm`.
- `~/dev/evorove_lead` (`AleneSparrow/evorove_lead`) — сервис Railway `evorove-lead` + `searxng`.
- `~/dev/ev-gemini` (`AleneSparrow/evorove_lite`) — новый монолитный сервис на VPS (FastAPI + SQLite + Cloudflare Tunnel).

---

## 1. Внешние вызовы на `evorove.com` и `api.evorove.com`

| Источник вызова | Куда обращается сейчас | Конфигурация / переменные (только имена) | Что произойдёт при переезде на `evorove_lite` |
|---|---|---|---|
| **Cloudflare Worker (`evorove-web`)** (`web/app/wrangler.web.toml`, `worker.js`) | Привязан как Custom Domain к `evorove.com` и `www.evorove.com`; проксирует `/api/*` и `/widget/*` на `API_ORIGIN` (`ai-business-process-engine-production.up.railway.app`) | `API_ORIGIN` | **Конфликт роутинга**, если Worker активен в Cloudflare: перед переключением на Cloudflare Tunnel (`http://localhost:8000`) нужно отвязать Custom Domains от Worker `evorove-web` / Pages `evorove`. |
| **Встраиваемый чат-виджет на чужих сайтах** (`web/widget/widget.js`, `widget.css`) | `<script src="https://evorove.com/widget/widget.js" data-business-id="...">` и запросы к `/api/v1/public/businesses/{business_id}/*` | `PUBLIC_CHAT_RATE_LIMIT_REQUESTS`, `PUBLIC_CHAT_RATE_LIMIT_WINDOW_SECONDS`, `PUBLIC_CONVERSATION_TOKEN_TTL_HOURS`, `PUBLIC_CHAT_MESSAGE_MAX_CHARS`, `CORS_ALLOWED_ORIGINS` | **Нигде не встроен в бою**: в коде лендинга `evorove.com` (`web/app`) виджет не подключён (есть только в локальном `web/widget/demo.html`), а внешним клиентам он не ставился (в системе только Клиент 0). Удаление `/widget/*` и `/api/v1/public/*` в `evorove_lite` ничего живого не сломает. |
| **Webhook Lemon Squeezy** | `POST https://api.evorove.com/api/v1/billing/webhook` (заголовок `X-Signature`) | `LEMONSQUEEZY_WEBHOOK_SECRET`, `LEMONSQUEEZY_API_KEY`, `LEMONSQUEEZY_STORE_ID`, `LEMONSQUEEZY_VARIANT_STARTER`, `LEMONSQUEEZY_VARIANT_PRO` | **Сломается без смены URL в Lemon Squeezy**: в `evorove_lite` роут изменён на `POST https://evorove.com/billing/webhook`. Нужно обновить Webhook URL в дашборде Lemon Squeezy в момент переключения. |
| **Webhook Twilio (входящие SMS / WhatsApp)** | `POST https://api.evorove.com/api/v1/public/sms/inbound` (заголовок `X-Twilio-Signature`) | `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `EVOROVE_WHATSAPP_FROM`, `PUBLIC_API_BASE_URL` | **Для Клиента 0 не использовался** (работа шла только через email). В августе 2026 на этапе прототипа Flywheel подключался тестовый номер Twilio (`docs/pilot-acceptance-pack.md`). В `evorove_lite` SMS/WhatsApp убраны (TCPA). |
| **Ссылки отписки в ранее отправленных письмах** | `GET/POST https://api.evorove.com/api/v1/public/unsubscribe/{token}` | `PUBLIC_API_BASE_URL`, `ACCOUNT_SECURITY_ENCRYPTION_KEY` | **Старые ссылки в уже отправленных письмах перестанут работать**:1) они ведут на поддомен `api.evorove.com` и путь `/api/v1/public/unsubscribe/{token}` (в `evorove_lite` — `https://evorove.com/unsubscribe/{token}`); 2) старый токен зашифрован через `ACCOUNT_SECURITY_ENCRYPTION_KEY` (`[business_id, email]`), а новый подписан через `SECRET_KEY` (`[business_id, person_id]`). Отписка ответом на письмо (`no` / `stop` / `unsubscribe`) по IMAP продолжит работать. |
| **Google Calendar OAuth callback** | `https://evorove.com/app/settings?tab=calendar` → `POST /api/v1/businesses/{id}/calendar/callback` | `GOOGLE_CALENDAR_CLIENT_ID`, `GOOGLE_CALENDAR_CLIENT_SECRET`, `PUBLIC_APP_BASE_URL` | **Отсутствует в `evorove_lite`** (встречи убраны, закрытие сделок идёт через ссылку на оплату или ручное подтверждение). |
| **Railway cron `evorove-sweep-cron`** | Каждые 15 мин вызывает `POST https://api.evorove.com/api/v1/internal/integrations/deliver`, `/follow-up/run`, `/commercial/expire` и `evorove-crm` (`/lead-searches/run-due`) | `EVOROVE_API_URL`, `CRM_API_URL`, `INTERNAL_TASK_SECRET` | **Не нужен, заменён встроенным `Scheduler`** (`app/scheduler.py`). Крон нужно выключить на Railway перед миграцией. |
| **GitHub Actions `.github/workflows/scheduled-tasks.yml`** (в репо `AleneSparrow/Evorove`) | Каждые 10 мин / 1 час / 1 день бьёт в `EVOROVE_API_URL` (`api.evorove.com`) и `CRM_API_URL` по `/api/v1/internal/*` | GitHub Secrets: `EVOROVE_API_URL`, `CRM_API_URL`, `INTERNAL_TASK_SECRET` | **Начнёт падать каждые 10 минут** и слать уведомления об ошибках GitHub Actions, если не отключить workflow в репозитории `AleneSparrow/Evorove`. |
| **Railway cron `evorove-lead-market-signals-cron`** | Раз в сутки (`0 13 * * *`) вызывает `POST /api/v1/internal/market-signals/run-due` на `evorove-lead` | `EVOROVE_LEAD_URL`, `INTERNAL_TASK_SECRET` | **Не нужен**, в `evorove_lite` сигналы рынка встроены в `app/search.py` (включаются через `MARKET_SIGNALS=1`). |
| **Вызовы из `evorove-crm` в `ai-business-process-engine`** | `POST https://api.evorove.com/api/v1/internal/businesses/{business_id}/lead-commands` | `EVOROVE_BASE_URL`, `INTERNAL_TASK_SECRET` | **Не нужны**, CRM и Engine объединены в один процесс `evorove_lite`. |
| **Вызовы из `evorove-lead` в `evorove-crm`** | `POST http://evorove-crm.railway.internal:8080/api/v1/internal/businesses/{business_id}/lead-touches` | `CRM_BASE_URL`, `INTERNAL_TASK_SECRET` | **Не нужны**, поиск лидов (`app/search.py`) пишет напрямую в SQLite таблицу `people`. |
| **Вызовы из `n8n` (и исходящие CRM webhooks в `n8n`)** | Возможные входящие вызовы из `n8n` на `api.evorove.com` (`/api/v1/businesses/{id}/messages`, `/api/v1/internal/*`) или исходящие вебхуки из таблицы `crm_webhook_connections` в `n8n` | `INTERNAL_TASK_SECRET`, URL вебхуков в `crm_webhook_connections` | В `evorove_lite` **нет API для внешних автоматизаций `n8n`** и нет исходящих CRM-вебхуков. `n8n` остаётся на Railway, но нужно проверить в UI `n8n`, нет ли активных воркфлоу, стучащихся в `evorove.com` / `api.evorove.com`. |

---

## 2. Внешние эндпоинты старого API и их аналоги в `evorove_lite`

| Старый эндпоинт (`ai-business-process-engine` / `evorove-crm` / `evorove-lead`) | Кто вызывал | Аналог в `evorove_lite` (`app/main.py`) | Статус / Что менять |
|---|---|---|---|
| `GET /health` | Мониторинг, Railway healthcheck | `GET /health` | Полный аналог. |
| `POST /api/v1/billing/webhook` | Lemon Squeezy | `POST /billing/webhook` | **Меняется путь и домен** (`api.evorove.com/api/v1/billing/webhook` → `evorove.com/billing/webhook`). |
| `GET /api/v1/public/unsubscribe/{token}`<br>`POST /api/v1/public/unsubscribe/{token}` | Получатели писем (браузер и почтовые клиенты RFC 8058 One-Click) | `GET /unsubscribe/{token}`<br>`POST /unsubscribe/{token}` | **Меняется путь и формат токена**. Новые письма работают штатно; старые ссылки из прошлых писем не откроются без редиректа/совместимости. |
| `GET /widget/widget.js`, `GET /widget/widget.css`<br>`GET /api/v1/public/businesses/{id}/chat-config`<br>`POST /api/v1/public/businesses/{id}/conversations`<br>`GET/POST .../conversations/{token}[/messages|/commercial]` | Внешние сайты с виджетом чата | **Нет аналога** | Виджет чата вырезан из продукта. |
| `POST /api/v1/public/sms/inbound` | Twilio (SMS / WhatsApp) | **Нет аналога** | Канал SMS/WhatsApp вырезан из продукта. |
| `POST /api/v1/auth/register`, `/login`, `/logout`, `/forgot-password`, `/reset-password` | React SPA (`evorove.com`) | `GET/POST /signup`, `/login`, `POST /logout`, `GET/POST /forgot`, `/reset/{token}` | Заменено серверными HTML-страницами (Jinja2 + htmx). |
| `GET/PUT /api/v1/businesses/{id}/board*`<br>`GET/POST /api/v1/businesses/{id}/email-outreach*`<br>`GET/PUT /api/v1/businesses/{id}/dna`, `/email-connection`, `/brief` | React SPA (`evorove.com`) | `GET /app`, `GET /app/people/{id}`, `POST /app/people/{id}/paid`, `/stage`, `/note`<br>`GET/POST /app/settings`, `/app/settings/mailbox`, `/app/settings/pause`, `/app/settings/run-now` | Заменено серверным UI доски и настроек. |
| `POST /api/v1/internal/integrations/deliver`<br>`POST /api/v1/internal/follow-up/run`<br>`POST /api/v1/internal/commercial/expire`<br>`POST /api/v1/internal/lead-searches/run-due`<br>`POST /api/v1/internal/market-signals/run-due` | Кроны Railway (`evorove-sweep-cron`, `evorove-lead-market-signals-cron`) и GitHub Actions | **Нет HTTP-эндпоинтов** (внутрипроцессный `Scheduler` в `app/scheduler.py` + кнопка `POST /app/settings/run-now`) | Внешние кроны отключить. |

---

## 3. Данные в трёх старых Postgres и план переноса в SQLite (`evorove_lite`)

В репозитории уже есть скрипт `scripts/import_from_railway.py` (читает `OLD_ENGINE_DATABASE_URL`, `OLD_CRM_DATABASE_URL`, `OLD_LEAD_DATABASE_URL` в режиме read-only для бизнеса `CLIENT_ZERO_BUSINESS_ID`). Ниже — полная ревизия всех таблиц трёх БД и найденные пробелы в скрипте.

### 3.1. База Engine (`ai-business-process-engine` — 29 таблиц)

| Таблица в старом Postgres | Что внутри | Куда в SQLite (`evorove_lite`) | Есть в `import_from_railway.py`? | Решение |
|---|---|---|---|---|
| `businesses` | Профиль бизнеса, тариф `plan`, `subscription_status`, `lemonsqueezy_customer_id`, `lemonsqueezy_subscription_id`, `current_period_end` | `businesses` (`id, name, plan, subscription_status, ls_customer_id, ls_subscription_id, subscription_ends_at`) | **Частично**: колонка `current_period_end` **не переносится** в `subscription_ends_at` (критично при статусе `cancelled`) | **Переносим** (доработать перенос `current_period_end` → `subscription_ends_at`) |
| `business_dna` | `offer_summary`, `stripe_payment_link` | `businesses` (`offer_text`, `payment_link`) | Да | **Переносим** |
| `email_connections` | Почтовый ящик бизнеса: `from_address`, `from_name`, `postal_address`, `smtp_host`, `smtp_port`, `smtp_username`, `password_encrypted`, `imap_host`, `imap_port`, `imap_last_uid`, `daily_limit` | `businesses` (`sender_name`, `postal_address`, `daily_cap`, `smtp_host`, `smtp_port`, `smtp_user`, `imap_host`, `imap_port`, `imap_user`, `imap_last_uid`) | **Частично**: переносятся только `from_name`, `postal_address`, `daily_limit`. Поля `smtp_host/port/username`, `imap_host/port` и **`imap_last_uid` НЕ переносятся**; пароль зашифрован другим ключом (`ACCOUNT_SECURITY_ENCRYPTION_KEY`) | **Переносим** (хосты/порты/логин/`imap_last_uid` перенести или ввести заново в `/app/settings`; пароль ящика ввести заново в UI) |
| `staff_users` + `business_memberships` | Пользователи (`email`, `password_hash` PBKDF2) и роли (`owner` / `staff`) | `users` (`id, email, password_hash, business_id, role`) | Да (`app/auth.py` понимает старый `pbkdf2_sha256$...` и при первом входе обновляет до Argon2) | **Переносим** |
| `email_suppressions` | Отписавшиеся и bounce-адреса (`business_id, email, reason`) | `suppressions` | Да | **Переносим обязательно** (защита от повторной отправки CAN-SPAM) |
| `conversations` + `conversation_messages` | Полные тексты всех входящих/исходящих писем и чатов, `external_message_id`, метаданные | `messages` (`id, person_id, direction, subject, body, smtp_message_id, in_reply_to, move, created_at`) | **Нет** (сейчас скрипт берёт только обрезанный `summary` из `crm.board_touches` без `subject` и `smtp_message_id`) | **Переносим (решение Алёны)**: доработать скрипт, чтобы переносить полные тексты писем из `engine.conversation_messages` вместе с `subject` и `smtp_message_id` (для контекста ИИ и продолжения цепочки `In-Reply-To`) |
| `leads` + `outreach_prospects` | Лиды и проспекты в Engine (включая тех, кто не попал на CRM-доску) | `people` (только для `CLIENT_ZERO_BUSINESS_ID`) | **Нет** (скрипт читает только `crm.board_people` с непустым `email`) | **Переносим после оценки в `--dry-run` (решение Алёны)**: переносим только Клиента 0; контакты с email, не попавшие на доску, считаем в `--dry-run` и забираем |
| `billing_webhook_events` | Сырые события вебхуков Lemon Squeezy | `payments` | Нет | **Не переносим** (достаточно текущего статуса подписки в `businesses`) |
| `marketing_assets`, `marketing_guidance` | Загруженные маркетинговые материалы и правила | `businesses.brief` | Нет (берётся из `lead.briefs` или `CLIENT_ZERO_BRIEF`) | **Не переносим** |
| `staff_sessions`, `password_reset_tokens`, `login_throttles` | Старые сессии, токены сброса пароля, лимиты входа | — | Нет | **Не переносим** |
| `appointments`, `calendar_connections`, `quotes`, `contracts`, `payment_records`, `follow_up_tasks`, `escalations`, `crm_webhook_connections`, `integration_deliveries`, `message_delivery_events`, `audit_events`, `lora_adapters`, `training_runs` | Встречи, календарь, КП/контракты, задачи фоллоу-апа, эскалации, вебхуки, аудит, LoRA | — | Нет (функции убраны в `evorove_lite`) | **Не переносим** |

### 3.2. База CRM (`evorove-crm` — 24 таблицы)

| Таблица в старом Postgres | Что внутри | Куда в SQLite (`evorove_lite`) | Есть в `import_from_railway.py`? | Решение |
|---|---|---|---|---|
| `board_people` | Карточки людей на доске (`name, role, company, website, email, phone, summary, stage`) | `people` (`cold`→`cold`, `in_work`→`in_progress`, `offer_sent`→`offer_made`, `done`→`done`, `discarded`→`lost`) | Да (контакты без `email` пропускаются, так как в `evorove_lite` email обязателен) | **Переносим** |
| `board_touches` | События по человеку: `kind='assembled'` (источник `source_url`, цитата `evidence_quote`, боль `pain`, штат `us_state`) и `kind='message'` (сводка сообщения до 500 симв.) | `people` (поля `pain, evidence_quote, source_url, us_state, segment`) + `messages` | Да | **Переносим** |
| `board_notes` | Ручные заметки владельца по карточкам на доске | `log` (`event='note'`, `detail=body`) | **Нет** (в `import_from_railway.py` таблица `board_notes` не читается) | **Переносим (решение Алёны)**: доработать скрипт, чтобы перенести ручные заметки из `crm.board_notes` в таблицу `log` |
| `lead_search_configs` | Настройки регулярного поиска в CRM | `businesses.brief` / `outreach_paused` | Нет (`outreach_paused` при импорте ставится в `1` для безопасного старта) | **Не переносим** |
| Остальные 20 таблиц (`businesses`, `users`, `sessions`, `leads`, `deals`, `contacts`, `companies`, `activities`, `tasks`, `notes`, `pipelines`, `stages`, `tags`, `lead_tags`, `webhook_endpoints`, `webhook_deliveries`, `import_jobs`, `audit_logs`, `cycle_commands`, `hot_lead_intakes`) | Дубликаты таблиц из старого шаблона CRM и очередь команд цикла | — | Нет | **Не переносим** |

### 3.3. База Lead (`evorove-lead` — 25 таблиц)

| Таблица в старом Postgres | Что внутри | Куда в SQLite (`evorove_lite`) | Есть в `import_from_railway.py`? | Решение |
|---|---|---|---|---|
| `briefs` | Поисковый бриф бизнеса (`payload`: `what_we_sell`, `ideal_customer`, `ObservedPain` и др.) | `businesses.brief` (JSON) | Да | **Переносим** |
| `leads`, `contacts`, `companies`, `evidence_items`, `pain_signals`, `observed_pains`, `research_evidence` | Сырые результаты прошлых поисков и доказательства болей (вкл. таблицу `candidates` в ревизии 0008) | `people` (только для `CLIENT_ZERO_BUSINESS_ID`) | **Нет** (скрипт читает только `crm.board_people`) | **Переносим после оценки в `--dry-run` (решение Алёны)**: в `--dry-run` вывести число найденных контактов Клиента 0 с email/доказательством боли, не попавших на доску CRM, и забрать их |
| Остальные 17 таблиц (`search_runs`, `search_queries`, `search_hypotheses`, `hypothesis_outcomes`, `source_documents`, `extractions`, `verifications`, `market_signal_configs`, `market_signal_runs`, `crm_touch_deliveries`, `suppression_entries`, `domain_cache`, `url_cache`, `llm_calls`, `job_locks`, `schema_migrations`, `audit_events`) | Служебные таблицы поисковых прогонов, кэша и доставки в CRM | — | Нет | **Не переносим** |

---

## 4. Почта (SMTP / IMAP) и зависимые DNS-записи

### 4.1. Откуда уходят и как принимаются письма

| Поток писем | Как было на Railway (`ai-business-process-engine`) | Как стало в `evorove_lite` (VPS) | Что изменилось / Риски |
|---|---|---|---|
| **Системная почта** (сброс пароля `/forgot`) | Переменные окружения `SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD`, `SMTP_FROM_EMAIL`, `SMTP_USE_TLS` | Переменные окружения **`SYSTEM_SMTP_HOST`**, **`SYSTEM_SMTP_PORT`**, **`SYSTEM_SMTP_USER`**, **`SYSTEM_SMTP_PASSWORD`**, **`SYSTEM_SMTP_FROM`** (`app/auth.py`, `smtplib.SMTP` + `starttls()`) | **Переименованы все переменные `SMTP_*` → `SYSTEM_SMTP_*`**. Если в `/opt/evorove/.env` оставить старые имена, сброс пароля упадёт с `RuntimeError("System SMTP is not configured")`. Ежедневные сводки теперь уходят в Telegram (`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`), а не по почте. |
| **Клиентский аутрич (исходящие письма лидам)** | Таблица `email_connections`: поддерживались `smtp_security='starttls'` (587) и `'ssl'` (465), отдельный `from_address` и `smtp_username`. Заголовки `Message-ID` и `List-Unsubscribe` на `PUBLIC_API_BASE_URL` (`api.evorove.com`) | Таблица `businesses`: `smtp_host`, `smtp_port` (по умолчанию `587`), `smtp_user`, `smtp_password_enc`. В `app/outreach.py` используется **только `smtplib.SMTP` + `starttls()`** (порт 587). Адрес `From:` и домен `Message-ID` берутся из `smtp_user`. `List-Unsubscribe` ведёт на `APP_BASE_URL` (`https://evorove.com/unsubscribe/{token}`) | 1) **Порт 465 (`SMTP_SSL`) не поддерживается** в `evorove_lite` — нужен порт `587` (`STARTTLS`).<br>2) `smtp_user` должен быть полным email-адресом (`user@domain`), так как он же подставляется в `From:` и `Message-ID`.<br>3) Отправка идёт через внешний SMTP-провайдер по порту 587 (проверить, что хостер VPS не блокирует исходящий TCP 587). |
| **Клиентский аутрич (входящие ответы IMAP)** | Таблица `email_connections`: `imap_host`, `imap_port` (993 SSL), `imap_last_uid`, логин из `smtp_username` | Таблица `businesses`: `imap_host`, `imap_port` (993 SSL), `imap_user`, `imap_password_enc`, `imap_last_uid`, `imap_uidvalidity` (`app/outreach.py`) | **Критичный момент при переключении**: если `imap_last_uid` в SQLite равен `NULL` (а `import_from_railway.py` его сейчас не переносит), то первый запуск `poll_replies()` в `evorove_lite` считает текущий максимальный UID в ящике как базовый (`imap_last_uid = newest`) и **не обработает письма, пришедшие во время окна миграции**. Нужно либо перенести `imap_last_uid` при импорте, либо обработать входящие до остановки старого сервиса. |

### 4.2. Какие DNS-записи зависят от почты и переезда домена

| DNS-запись | Домен / хост | От чего зависит и нужно ли менять при переезде |
|---|---|---|
| **`MX`** | `evorove.com` (и/или отдельный аутрич-домен) | Принимает входящую почту на серверах почтового провайдера (куда `evorove_lite` ходит по IMAP 993). **НЕ ТРОГАТЬ** при переключении `evorove.com` на Cloudflare Tunnel. |
| **`TXT` (SPF)** | `evorove.com` (и аутрич-домен) | Авторизует SMTP-сервер провайдера (`SYSTEM_SMTP_HOST` и `businesses.smtp_host`), а не IP-адрес Railway или VPS. **Не менять**, если почтовый провайдер остаётся прежним (проверить только, нет ли в SPF привязки к старому IP/хосту). |
| **`TXT` / `CNAME` (DKIM)** | `<selector>._domainkey.evorove.com` | Ключ подписи писем на стороне SMTP-провайдера. **НЕ ТРОГАТЬ**. |
| **`TXT` (DMARC)** | `_dmarc.evorove.com` | Политика проверки SPF/DKIM. **НЕ ТРОГАТЬ**. |
| **`CNAME` / `A` (`evorove.com`, `www.evorove.com`)** | `evorove.com`, `www` | Сейчас смотрят на Railway (`ai-business-process-engine`) или Cloudflare Worker `evorove-web`. При переезде переключаются на Cloudflare Tunnel (`<tunnel-id>.cfargotunnel.com`), который ведёт в `http://localhost:8000` на VPS. |
| **`CNAME` / `A` (`api.evorove.com`)** | `api.evorove.com` | Сейчас смотрит на Railway (`ai-business-process-engine`). От него зависят **старые ссылки отписки в уже отправленных письмах** и (если не обновлён) вебхук Lemon Squeezy. Рекомендуется также завести `api.evorove.com` в Cloudflare Tunnel (или настроить Cloudflare Redirect Rule с `api.evorove.com/api/v1/public/unsubscribe/*` и `/api/v1/billing/webhook`). |

---

## 5. Что можно выключить на Railway после переезда, а что оставить

> **Правило:** сервис **`n8n`** (и его persistent volume на 5 ГБ) остаётся на Railway — **его НЕ трогаем**.

| Сервис на Railway / GitHub | Действие | Когда и в каком порядке выключать |
|---|---|---|
| **`n8n`** (+ его volume 5 ГБ) | **ОСТАВИТЬ РАБОТАТЬ (НЕ ТРОГАТЬ)** | Остаётся на Railway постоянно. Алёна подтвердила: `n8n` хранит всё в своём файле на отдельном 5 ГБ диске Railway и ни к одной из трёх баз Postgres не подключён. Воркфлоу внутри `n8n` на предмет вызовов `api.evorove.com` Алёна проверит перед переключением. |
| **`evorove-sweep-cron`** (Railway) + **`.github/workflows/scheduled-tasks.yml`** (GitHub Actions в `AleneSparrow/Evorove`) | **Выключить ПЕРВЫМИ** | **Шаг 1 (до финального дампинга БД и импорта)** — чтобы кроны не отправляли письма и не меняли состояния сделок во время переключения. |
| **`evorove-lead-market-signals-cron`** (Railway) | **Выключить ПЕРВЫМ** | **Шаг 1 (до финального дампинга БД и импорта)**. |
| **`ai-business-process-engine`** (`AleneSparrow/Evorove`) | **Остановить после переключения DNS/Tunnel** | **Шаг 2**: после импорта данных в SQLite, обновления URL вебхука в Lemon Squeezy и переключения `evorove.com` на Cloudflare Tunnel. |
| **`evorove-crm`** (`AleneSparrow/Evorove_CRM`) | **Остановить после переключения** | **Шаг 2**: одновременно с остановкой `ai-business-process-engine`. |
| **`evorove-lead`** (`AleneSparrow/evorove_lead`) | **Остановить после переключения** | **Шаг 2**: одновременно с остановкой `ai-business-process-engine`. |
| **`searxng`** | **Остановить и удалить** | **Шаг 2**: больше не используется (`evorove_lite` ищет через серверный web search инструмент Claude API, `searxng` только потребляет RAM на Railway). |
| **Три базы PostgreSQL** (`Postgres`, `Postgres-9WEU`, `Postgres-EY-2`) | **Сначала оставить в стопе (7–14 дней), затем удалить** | **Шаг 3**: перед остановкой снять полные `pg_dump` всех трёх БД в надёжное место и проверить, что `n8n` не подключён ни к одной из них. После 1–2 недель стабильной работы `evorove_lite` и репликации Litestream → R2 удалить инстансы Postgres. |

# Core: описание изменений для агентов 2 и 3

## Схема

`001_init.sql` сохранена без изменений относительно CONTRACT.md.
Уже существующая `002_core_security.sql` добавляет:

- `rate_limits(key TEXT PRIMARY KEY, attempts INTEGER NOT NULL, window_start INTEGER NOT NULL)`:
  счётчики попыток с окном 900 секунд. `key` — SHA-256 ключа IP или identity.
  Для login/reset identity — email; для применения сброса — SHA-256 bearer-токена.
  IP ограничен отдельно от identity. Сырые IP/email/токены в этой таблице не хранятся.
- `billing_events(event_id TEXT PRIMARY KEY, business_id TEXT, event_at TEXT NOT NULL,
  received_at TEXT NOT NULL)` и индекс `billing_events_business(business_id,event_at)`:
  идемпотентность подписанных webhook. При отсутствии отдельного event ID используется
  SHA-256 исходного тела. ID подписки/объекта не является ID события.

Пункты 4–8 не вводят дополнительных таблиц. Все агенты должны запускать обе миграции.
Даты — timezone-aware UTC ISO 8601; статусы `runs`: `running`, `ok`, `failed`.

## Интеграция поиска и рассылки

`search.py` и `outreach.py` по-прежнему являются заглушками с точными сигнатурами
контракта. Их реализацию core не подменяет. Ошибка заглушки видна в статусе запуска.

`Scheduler` записывает строку в `runs` до вызова функции и завершает её после вызова.
`app.db.current_run_id.get()` возвращает ID этой строки внутри потока задания.
Агент 2 может относить токены `llm.ask` к ней, не создавая дублирующий запуск.
При вызове вне scheduler контекст равен `None`; прямые dry-run/CLI вызовы могут
создавать собственную строку. `found` — длина результата поиска, `sent` — результат
`send_first_letters`; результат `poll_replies` попадает в `found` запуска `poll`.
Сводка считает письма/ответы по `messages`, а не суммированием `runs`.

Поиск по кнопке требует подписку `active`/`on_trial` и заполненный brief. Ручной
поиск разрешён при paused: это позволяет выполнить поиск без отправки писем.
Одновременный ручной/плановый поиск одного бизнеса блокируется строкой running.
После рестарта незавершённые строки становятся failed/ProcessInterrupted.

Scheduler запускает поиск после 13:00 UTC один раз за UTC-день (догоняет при
рестарте), отправку каждые 15 минут, IMAP каждые 5 минут. Автоматические задания
работают для active/on_trial при outreach_paused=0. Фильтр отправки в scheduler
открывает задание, если quiet_start/quiet_end позволяют отправку хотя бы в одном
из основных часовых поясов США (включая Arizona/Alaska/Hawaii), без привязки к
вьетнамскому времени владельца. **guards агента 3 обязаны проверять часовой пояс
каждого получателя** и все остальные защиты непосредственно перед SMTP.
Не обходить guards из-за общей проверки scheduler.

## Настройки и отписка

`app.settings.encrypt_password(settings, plaintext)` и
`decrypt_password(settings, ciphertext)` — общие функции для SMTP/IMAP паролей.
Fernet использует отдельный HMAC-SHA256-derived ключ из SECRET_KEY.
**Статус зависимости:** владелец явно согласовала cryptography 3 ноября 2026;
пакет включён в requirements.txt и установлен. Пароли сохраняются только шифротекстом.
При некорректной установке без cryptography сохранение пароля fail-closed (503).
Пустое парольное поле сохраняет старое значение. UI не читает обратно шифротекст.
При смене SECRET_KEY необходимо повторно ввести пароли почтовых ящиков.

Агент 3: footer должен использовать
`app.board.unsubscribe_token(settings, business_id, person_id)` и URL
`{APP_BASE_URL}/unsubscribe/{token}`. GET показывает форму; POST добавляет
`(business_id,lower(email),'unsubscribe',created_at)` в suppressions без дублей.
Тот же POST поддерживает RFC 8058 (`List-Unsubscribe=One-Click`) без cookie/CSRF:
авторизация — подписанный токен. Никакой SMTP из маршрута отписки не вызывается.

Для сводки число офферов считается по исходящим messages с move='offer'
(без учёта регистра); агент 3 должен сохранять этот move при отправке оффера.
Оплаты считаются по payments за 24 часа без test_mode. Задача ручного record_payment
и привязка custom_data.person_id остаются на согласование с агентом рассылки;
core не переводит карточку в Done от одного перехода по ссылке.

## Tunnel и эксплуатация

Uvicorn запускается с `--no-proxy-headers`: X-Forwarded-For не должен подменить
адрес TCP peer до проверки доверия. `auth.client_ip` принимает CF-Connecting-IP
только от peer `127.0.0.1`/`::1`, проверяет формат одиночного IP; иначе возвращает peer.
Это предполагает, что на loopback к приложению подключается доверенный cloudflared.
Слушать 127.0.0.1, не открывать порт приложения наружу.

Telegram включается только при наличии TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID.
Сводка — 09:00 в TIMEZONE_OWNER (по умолчанию Asia/Ho_Chi_Minh), один раз за местный
день; при позднем старте догоняет. Ошибки job и Telegram пишутся без текста
исключений, bearer-токенов, паролей, URL провайдера и тела писем.

Публичные legal/FAQ перенесены из старого SPA с исходным текстом и заметкой о v1.
Описание старых SMS/calendar/hosting/subprocessors нужно обновить перед production
переездом; это не утверждение о новом production-развёртывании.

## Верификация

Сценарии покрывают tenant isolation, owner-only страницы, XSS в timeline,
обе правки rate limit, подпись/повторы webhook, фоновые запуски и их ошибки,
расписание и тишину, сводку, отписку без мутаций на GET.
Замер: `python scripts/measure_idle.py` — worker Python 3.12, один процесс,
включённый scheduler, пустая SQLite, 600 секунд простоя; отчёт в .local (без git).

## Vendored htmx

`app/static/htmx.min.js` — htmx 2.0.11, лицензия Zero-Clause BSD рядом в
`htmx.LICENSE`. Источник: https://cdn.jsdelivr.net/npm/htmx.org@2.0.11/dist/htmx.min.js.
При загрузке SHA-384 сверена с официальной документацией https://htmx.org/docs/:
`2OatzQy1H+Zd/IIrjr1TcuDGqLXeHhbooAyJY1KdQMKnr4LZ22k31GBLdYKHmVjg`.
Runtime использует только локальный файл; CDN или Node build не нужны.
CSP остаётся self-only; htmx allowEval=false, includeIndicatorStyles=false,
historyCacheSize=0 (переписка не кешируется в localStorage).

## Результат замера (3 ноября 2026)

- Python 3.12.14, один uvicorn worker; scheduler включён, пустая тестовая SQLite,
  Telegram выключен. После проверки /health входящих запросов не было.
- Простой: 600.04 секунды (13:42:24–13:52:24 UTC).
- RSS по `ps -o rss`: **7 936 KiB = 7.75 MiB** на этой macOS-машине.
  Это RSS простоя, не пиковая память при Argon2/LLM/поиске или рассылке.
- `app/**/*.py`, без templates/static и тестов: **919 строк**.
- `make test`: **39 passed**; `make lint`, разбор всех Jinja-шаблонов и
  проверка точного соответствия 001_init.sql контракту — успешно.

## Завершение шифрования (после согласия владельца)

cryptography добавлена в зависимости; полный набор — **41 passed**, lint и pip check
успешны. Дополнительно проверены: round-trip Unicode-пароля, разные ciphertext для
одного plaintext, отказ при неверном ключе и повреждённом токене, оба SMTP/IMAP-пароля
в SQLite, отсутствие plaintext/ciphertext в HTML и секретов в log, сохранение старого
шифротекста при пустом поле, независимая замена одного пароля.

Приведённый выше десятиминутный RSS — измерение базового idle worker до подключения
cryptography. Импорт Fernet ленивый: происходит при первом encrypt/decrypt, а не при
старте приложения. Новый замер после использования шифрования не выполнялся.

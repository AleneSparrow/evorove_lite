# Переезд evorove.com с Railway на свой сервер

Эта инструкция — продолжение `docs/DEPLOY_RU.md` (там — как поднять сервер и
systemd-юниты) и шага 2 из `docs/ROADMAP.md` (переезд данных и домена). Здесь —
именно день переключения `evorove.com` и всё, что нужно сделать до него и
после.

**Что переезжает:** сайт и API Railway-сервиса `ai-business-process-engine`
(движок + `api.evorove.com`), вместе с ним — данные бизнеса клиента 0. Старые
`evorove-crm`, `evorove-lead`, `searxng` и их Postgres в `evorove_lite` не
нужны — один процесс делает всё сам. **n8n и остальные проекты на Railway
переезд не затрагивает** — их не трогаем вообще.

**`evorove.com` и `api.evorove.com` переезжают оба.** Новому приложению
`api.evorove.com` не нужен как отдельный сервис — весь функционал (сайт,
доска, вебхук Lemon Squeezy) живёт на одном хосте `evorove.com`. Но
`api.evorove.com` всё равно нужно поднять на новом сервере минимум на 30 дней:
на него ведут ссылки отписки в уже отправленных письмах
(`api.evorove.com/api/v1/public/unsubscribe/{token}`), и по CAN-SPAM они
обязаны продолжать работать (раздел 1 и 4).

**Новый сервер:** `212.192.20.133`, Ubuntu 24.04, 1 ГБ RAM. **Порт 443 на нём
уже занят VPN** — это не проблема: Cloudflare Tunnel (`evorove`) сам идёт
наружу и не просит открыть 443 или 80 на сервере. Приложение слушает только
`127.0.0.1:8000`, `deploy/setup.sh` открывает в UFW только SSH (см.
`docs/DEPLOY_RU.md`, раздел 2). Снаружи сайт виден только через туннель.

> Источник данных для разделов 1–3 — `docs/audit/railway-inventory.md`
> (полная опись всех вызовов, эндпоинтов и таблиц трёх старых Postgres).
> Здесь — выжимка и чек-лист; за полной таблицей по каждой базе смотрите
> сам файл описи.

---

## 1. Подготовка до дня переезда

Каждый пункт — подготовить заранее, пока Railway ещё работает и ничего не
торопит. Значения секретов везде вводит сама Алёна — в этом файле их нет.

- [ ] **R2-бакет и токен для бэкапов.**
  Где взять: Cloudflare Dashboard → R2 → Create bucket → `evorove-backups`;
  затем R2 → Manage API tokens → Create API token → доступ только к этому
  бакету, права Object Read & Write. R2 даст три значения: Access Key ID,
  Secret Access Key и S3-endpoint вида `https://ACCOUNT_ID.r2.cloudflarestorage.com`.
  Куда вписать: на сервере, в `/etc/evorove/litestream.env` —
  `LITESTREAM_R2_BUCKET`, `LITESTREAM_R2_ENDPOINT`,
  `LITESTREAM_R2_ACCESS_KEY_ID`, `LITESTREAM_R2_SECRET_ACCESS_KEY`
  (подробности и команда проверки — `docs/DEPLOY_RU.md`, раздел 5).

- [ ] **Почта Zoho — два отдельных ящика. DNS-записи MX, SPF, DKIM, `_dmarc`
  не трогаем.** Решено: один ящик только для системных писем сайта, другой
  только для рассылки — так если письма рассылки начнут попадать в спам,
  репутация системного ящика (сброс пароля, чеки) не страдает.
  `MX`/`TXT` SPF/`TXT`или`CNAME` DKIM/`TXT` `_dmarc.evorove.com` — это
  настройки почтового провайдера (Zoho), не хостинга сайта; они не зависят от
  того, Railway или VPS отдаёт `evorove.com`, и их **не меняем и не создаём
  заново** при переезде (см. опись, раздел 4.2). Если оба ящика — на уже
  подтверждённом в Zoho домене `evorove.com`, добавление второго ящика само
  по себе новых DNS-записей не требует (SPF/DMARC — на весь домен, DKIM-
  селектор в Zoho обычно тоже один на домен). Если при создании второго
  ящика Zoho всё же покажет свою, новую DNS-запись — это неожиданно; прежде
  чем её добавлять, стоит свериться со службой поддержки Zoho, а не просто
  вписать в Cloudflare DNS.
  Где взять сами ящики: Zoho Mail → завести оба почтовых ящика на домене
  `evorove.com` (например `hello@evorove.com` — системный,
  `sales@evorove.com` — рассылка; точные имена решает Алёна).
  Затем в Zoho для **каждого** из двух ящиков отдельно: Security → App
  Passwords → создать пароль приложения для SMTP/IMAP (587/993) — это не
  пароль от самого ящика Zoho. Куда вписать пароль приложения:
  - системный ящик (сброс пароля, ежедневная сводка) — на сервере в
    `/opt/evorove/.env`: `SYSTEM_SMTP_HOST`, `SYSTEM_SMTP_PORT`,
    `SYSTEM_SMTP_USER`, `SYSTEM_SMTP_PASSWORD`, `SYSTEM_SMTP_FROM`;
  - ящик рассылки клиента 0 (первые письма, ответы) — **не в `.env`**, а в
    браузере, после первого входа, на странице `/app/settings`, блоки «SMTP»
    и «IMAP» (Host/Port/Username/Password). Это сознательно не переносится
    скриптом импорта (раздел 3) — впишите заново.

- [ ] **API-ключ Lemon Squeezy.**
  Где взять: Lemon Squeezy Dashboard → Settings → API → создать API key (или
  использовать тот же, что уже привязан к магазину); там же Settings → Stores
  — Store ID; Products → нужный продукт → Variants — ID вариантов Starter и
  Pro. Куда вписать: `/opt/evorove/.env` — `LEMONSQUEEZY_API_KEY`,
  `LEMONSQUEEZY_STORE_ID`, `LEMONSQUEEZY_VARIANT_STARTER`,
  `LEMONSQUEEZY_VARIANT_PRO`. Про сам вебхук и его секрет — в разделе 5
  (меняется URL, когда сайт уже работает с нового адреса).

- [ ] **Токен туннеля Cloudflare.**
  Где взять: Cloudflare Zero Trust → Networks → Tunnels → создать (или
  использовать уже готовый) тоннель `evorove` → Install connector → токен
  показывается один раз. Куда вписать: на сервере, одной строкой, в файл
  `/etc/cloudflared/evorove.token` (создаётся `setup.sh`), права `0640`,
  владелец `root:cloudflared` — см. `docs/DEPLOY_RU.md`, раздел 4 и
  `deploy/cloudflared/README_RU.md`. Публичные хостнеймы для `evorove.com` и
  `api.evorove.com` в этом туннеле пока **не добавляйте** — сначала
  используем временный (раздел 4).

- [ ] **Старый ключ `ACCOUNT_SECURITY_ENCRYPTION_KEY` — для старых ссылок
  отписки.** Нужен, чтобы `api.evorove.com` на новом сервере мог ещё
  понимать токены отписки из уже отправленных писем (раздел 4). Где взять:
  Railway → проект → сервис `ai-business-process-engine` → Variables →
  `ACCOUNT_SECURITY_ENCRYPTION_KEY` — **Алёна копирует значение сама, не
  через агентов**. Куда вписать: `/opt/evorove/.env`, под **новым** именем
  `LEGACY_ACCOUNT_SECURITY_ENCRYPTION_KEY` (не поверх `SECRET_KEY` — это
  разные ключи для разных схем токена).
  ⚠️ На момент написания этого файла в `evorove_lite` ещё нет кода, который
  читает `LEGACY_ACCOUNT_SECURITY_ENCRYPTION_KEY` и отвечает на
  `GET/POST /api/v1/public/unsubscribe/{token}` в старом формате — это
  отдельная задача разработки, её нужно закрыть до дня переезда (раздел 4),
  иначе сам ключ просто не на что опереться.

---

## 2. Бэкап старых баз Railway перед переездом

Это страховка на случай, если что-то в переносе данных (раздел 3) пойдёт не
так — Railway всё равно работает и его не трогаем минимум 7 дней (раздел 6),
но свежий дамп должен лежать отдельно от Railway.

- [ ] **Сначала выключить кроны на Railway** — `evorove-sweep-cron` и
      `evorove-lead-market-signals-cron` (Railway → соответствующий
      сервис/крон → Stop/Pause). Это нужно сделать **до** снятия дампа, иначе
      кроны продолжат менять данные (слать письма, двигать сделки) уже после
      того, как дамп снят, и новые данные потеряются при импорте.
- [ ] В Railway откройте каждую Postgres-базу трёх старых сервисов (engine,
      crm, lead) и возьмите временную **read-only** строку подключения (или
      выполните `railway run pg_dump ...` прямо из CLI, если он настроен).
- [ ] Снимите дамп каждой базы отдельно:
  ```bash
  pg_dump "$OLD_ENGINE_DATABASE_URL" --format=custom --file=engine-$(date -u +%Y%m%dT%H%M%SZ).dump
  pg_dump "$OLD_CRM_DATABASE_URL"    --format=custom --file=crm-$(date -u +%Y%m%dT%H%M%SZ).dump
  pg_dump "$OLD_LEAD_DATABASE_URL"   --format=custom --file=lead-$(date -u +%Y%m%dT%H%M%SZ).dump
  ```
  Строки подключения — переменные окружения в своём терминале, не аргументы
  команды (не попадут в историю шелла построчно) и не в этот файл.
- [ ] Храните три `.dump`-файла **не на новом сервере и не в Git** — например,
      в том же R2-бакете `evorove-backups` под отдельным префиксом
      `railway-dumps/` (`aws s3 cp ... --endpoint-url ...` с тем же R2-токеном
      из раздела 1), либо в менеджере паролей/зашифрованном архиве у Алёны.
      Это разовый архив на случай отказа, не часть обычных бэкапов Litestream.

---

## 3. Перенос данных

Полная опись всех таблиц трёх старых Postgres — `docs/audit/railway-inventory.md`,
раздел 3. Здесь — что из неё переносит сегодняшний
`scripts/import_from_railway.py` (имя в задаче — `import_railway.py`, в
репозитории файл называется чуть иначе, это один и тот же скрипт), что он
**не** переносит и что по каждому пробелу решает Алёна.

**Переносит сегодня:**

| Таблица в новой SQLite | Источник на Railway | Пробел по описи |
| --- | --- | --- |
| `businesses` (1 строка) | `businesses` + активная `business_dna` (engine), опционально `briefs` (lead) | `current_period_end` **не** попадает в `subscription_ends_at` (см. ниже — блокер) |
| `users` | `staff_users` этого бизнеса (engine); старый `pbkdf2_sha256$...` хеш пароля `app/auth.py` понимает и обновит до Argon2 при первом входе | — |
| `suppressions` | `email_suppressions` (engine) | — |
| `people` | `board_people` (crm) — только строки с email | контакты без email из `board_people` пропускаются (в `evorove_lite` email обязателен) |
| `people.pain/evidence_quote/source_url/us_state` + `messages` | `board_touches` (crm): `kind='assembled'` → поля карточки, `kind='message'` → сообщение (`summary`, обрезан до 500 символов) | полный текст писем и `subject` лежат в `conversations`/`conversation_messages` (engine), скрипт их не читает — решает Алёна (см. ниже) |

**Не переносит вообще** (ввести/решить отдельно):
- `smtp_host`/`smtp_user`/`smtp_password`, `imap_host`/`imap_user`/
  `imap_password` — старые учётные данные зашифрованы под
  `ACCOUNT_SECURITY_ENCRYPTION_KEY` и относятся к старой почте; для новой
  Zoho вводятся заново на `/app/settings` (раздел 1), порт SMTP — **587**
  (поле уже стоит на 587 по умолчанию в форме настроек);
- `imap_last_uid` — **блокер, см. ниже**;
- `board_notes` (ручные заметки владельца на карточках, crm) — **решает
  Алёна**: если есть важные заметки, их нужно будет добавить в `log` отдельно
  (скрипт сегодня это не делает);
- `engine.leads`/`outreach_prospects` и `lead.leads`/`contacts` (контакты, не
  дошедшие до доски CRM из-за старого сбоя связки сервисов или просто ещё не
  принятые) — **решает Алёна**: искать их заново циклом 1 в `evorove_lite`
  или всё же импортировать. Если решение — импортировать, скрипту потребуется
  **новый флаг `--include-unboarded-leads`** (сегодня **не существует** —
  отдельная задача разработки, не входит в этот документ);
- `billing_webhook_events`, `marketing_assets`/`marketing_guidance`,
  `staff_sessions`/`password_reset_tokens`/`login_throttles`, всё про
  встречи/календарь/КП/эскалации/LoRA, дубликаты CRM-таблиц (`leads`,
  `deals`, `contacts`, …) и служебные таблицы `evorove-lead` (кэш, прогоны
  поиска, `llm_calls` и т.п.) — не нужны продукту `evorove_lite`, не
  переносим.

**Блокеры — нужно закрыть до `--apply` в день переезда:**
- [ ] **`subscription_ends_at` не переносится.** Скрипт не копирует
      `businesses.current_period_end` в новую колонку
      `businesses.subscription_ends_at`. Это критично для тех, кто отменил
      подписку, но у кого оплаченный период ещё не закончился — без этого
      поля они могут потерять доступ раньше срока. Нужно доработать
      `apply_bundle`/`build_bundle` в скрипте, прежде чем запускать `--apply`
      на настоящих данных.
- [ ] **`imap_last_uid` не переносится.** Без него первый `poll_replies()` в
      `evorove_lite` возьмёт текущий максимальный UID почтового ящика за
      точку отсчёта и пропустит все ответы, пришедшие в окно самого переезда.
      Решение: доработать скрипт, чтобы `imap_last_uid` (и начальный
      `imap_uidvalidity`) переносился из `email_connections`. Пока это не
      сделано — не снимать финальный дамп/импорт, пока не обработаны (или не
      поставлены на паузу, см. ниже) все входящие в старом ящике.

**Перед дампом и импортом — поставить рассылку на паузу в старом
приложении** (Railway, UI `ai-business-process-engine`/CRM): это остановит
появление новых исходящих/входящих писем и сделок после снятия снимка
данных, пока `imap_last_uid` не перенесён надёжно.

Как устроен сам скрипт (важно для безопасности — не меняется этим пунктом):
строки подключения к трём старым Postgres читает из переменных окружения
`OLD_ENGINE_DATABASE_URL`, `OLD_CRM_DATABASE_URL`, `OLD_LEAD_DATABASE_URL`, а
какой бизнес переносить — из `CLIENT_ZERO_BUSINESS_ID`; сам ничего из
секретов не печатает, в консоль идут только количества строк; каждое
Postgres-соединение открывается заведомо read-only
(`SET TRANSACTION READ ONLY`) — испортить исходные базы на Railway он не
может физически; по умолчанию (без флагов) работает как `--dry-run`:
подключается, читает, печатает количества, ничего не пишет; только с явным
`--apply` и переменной `DB_PATH` применяет миграции и записывает данные в
новую SQLite (вставки идемпотентны — повторный запуск с `--apply` не плодит
дубликаты).

Новый бизнес после импорта всегда остаётся с рассылкой **на паузе**
(`outreach_paused`, по умолчанию включена) — это намеренно, чтобы ничего не
ушло настоящим людям раньше смотра (раздел 4).

Порядок на сервере (после того, как `deploy/setup.sh` уже выполнен и
`/opt/evorove/.env` заполнен — разделы 1–3 `docs/DEPLOY_RU.md`, и после того,
как оба блокера выше закрыты):

```bash
sudo -u evorove /opt/evorove/.venv/bin/pip install \
  --requirement /opt/evorove/requirements-import.txt
```

- [ ] Сухой прогон (ничего не меняет, только печатает количества):
  ```bash
  sudo -u evorove env \
    OLD_ENGINE_DATABASE_URL='...' OLD_CRM_DATABASE_URL='...' OLD_LEAD_DATABASE_URL='...' \
    CLIENT_ZERO_BUSINESS_ID='...' \
    /opt/evorove/.venv/bin/python /opt/evorove/scripts/import_from_railway.py --dry-run
  ```
  Значения после `=` подставляет Алёна сама в момент запуска, не в файл.
- [ ] Сверьте напечатанные количества строк с тем, что ожидаете увидеть
      (сколько карточек людей, сообщений и т.д. сейчас в CRM на Railway).
- [ ] Только если числа похожи на правду — тот же вызов с `--apply` и
      `DB_PATH=/var/lib/evorove/evorove.sqlite3`:
  ```bash
  sudo -u evorove env \
    OLD_ENGINE_DATABASE_URL='...' OLD_CRM_DATABASE_URL='...' OLD_LEAD_DATABASE_URL='...' \
    CLIENT_ZERO_BUSINESS_ID='...' DB_PATH=/var/lib/evorove/evorove.sqlite3 \
    /opt/evorove/.venv/bin/python /opt/evorove/scripts/import_from_railway.py --apply
  ```
- [ ] Повторный `--dry-run` после импорта — числа не должны расти при ещё
      одном случайном запуске (вставки с `ON CONFLICT` обновляют, а не
      дублируют).

---

## 4. День переезда по шагам

- [ ] **Preflight.** `evorove` на новом сервере пока не запущен. `.env`
      (включая `LEGACY_ACCOUNT_SECURITY_ENCRYPTION_KEY`, раздел 1),
      `/etc/evorove/litestream.env`, `/etc/cloudflared/evorove.token`
      заполнены. Данные перенесены и проверены, оба блокера раздела 3
      (`subscription_ends_at`, `imap_last_uid`) закрыты. Свежий бэкап старых
      Postgres лежит отдельно (раздел 2). Исходящий 587 открыт (если не
      проверяли при заказе сервера — `nc -vz smtp.zoho.com 587`). Railway
      продолжает отвечать на `evorove.com`/`api.evorove.com` — их не трогаем
      до самого переключения DNS.
  - [ ] **Отключить GitHub Actions `.github/workflows/scheduled-tasks.yml`**
        в репозитории `AleneSparrow/Evorove` (GitHub → Actions → этот
        workflow → Disable workflow). Он каждые 10 минут/час/день бьёт в
        `api.evorove.com`/CRM и после переключения начнёт падать с
        уведомлениями об ошибках, если его не выключить заранее.
  - [ ] **Проверить Cloudflare Worker `evorove-web`** (из репозитория
        `evorove`, `web/app/wrangler.web.toml`) — убедиться, что у него (или
        у Pages-проекта `evorove`) **не привязаны** Custom Domains
        `evorove.com`/`www.evorove.com`. Если привязаны — отвязать их в
        Cloudflare Dashboard → Workers & Pages → `evorove-web` → Custom
        Domains, иначе Worker продолжит перехватывать `/api/*`/`/widget/*` и
        будет конфликтовать с Public Hostname тоннеля на том же имени.
- [ ] **Запуск.** `sudo systemctl start evorove`, затем `litestream`, затем
      `cloudflared` — команды и проверка `/health` в `docs/DEPLOY_RU.md`,
      раздел 6 и раздел 10.
- [ ] **Смоук на временном адресе туннеля — до переключения evorove.com.** В
      Cloudflare Zero Trust → тоннель `evorove` → Public Hostname → добавьте
      временный поддомен, например `cutover-check.evorove.com`, service
      `http://localhost:8000` (это создаст свою DNS-запись, `evorove.com` при
      этом не трогается). Откройте этот адрес в браузере и проверьте:
  - [ ] главная страница и `/faq`, `/terms`, `/privacy` открываются;
  - [ ] вход под перенесённым аккаунтом владельца работает, доска показывает
        перенесённые карточки в нужных колонках;
  - [ ] `/app/settings` — впишите SMTP/IMAP для Zoho (раздел 1), сохраните;
  - [ ] на доске написано «Outreach paused» — рассылка не ушла сама;
  - [ ] `curl --fail https://cutover-check.evorove.com/health` отвечает.
  Хостнейм `api.evorove.com` в этом смоуке отдельно не нужен — его проверяем
  уже после переключения (следующий пункт), на реальном старом токене
  отписки.
- [ ] **Переключение `evorove.com` и `api.evorove.com` — вместе, одним
      шагом.** В `evorove_lite` весь функционал (сайт, доска, вебхук Lemon
      Squeezy) живёт на одном хосте `evorove.com` — отдельный сервис под
      `api.evorove.com` новому приложению не нужен. Но сам хостнейм
      `api.evorove.com` нужен ещё минимум 30 дней ради старых ссылок отписки
      (см. врезку в начале файла и раздел 1) — на нём должен отвечать тот же
      `evorove_lite`, со старым путём `/api/v1/public/unsubscribe/{token}`.
      Поэтому в тоннеле `evorove` добавьте **два** Public Hostname на один и
      тот же `http://127.0.0.1:8000`:
  - [ ] `evorove.com` (и `www.evorove.com`, если он используется);
  - [ ] `api.evorove.com`.
      Если в Cloudflare DNS для этих имён уже есть старые записи на Railway —
      замените их (Cloudflare предупредит о конфликте записи, когда вы
      добавляете тот же хостнейм в тоннеле). Подождите несколько минут на
      прогрев DNS/кеша, затем откройте `https://evorove.com` и повторите те
      же проверки, что в смоуке выше, а на `https://api.evorove.com` — откройте
      настоящую ссылку отписки из любого старого письма и убедитесь, что
      страница отписки открывается (а не 404). Временный
      `cutover-check.evorove.com` можно удалить сразу после этого или
      оставить ещё на неделю — не мешает.
  - [ ] В Railway: Settings сервиса `ai-business-process-engine` → Domains →
        удалите custom domain `evorove.com` и `api.evorove.com`. Сам сервис и
        его переменные не трогаем — только отвязываем домены, чтобы Railway
        не продолжал держать сертификат на уже неиспользуемый адрес. Это
        обратимо — в разделе 6 их можно привязать заново.

---

## 5. После переключения

- [ ] **Сменить URL вебхука в Lemon Squeezy.** Lemon Squeezy Dashboard →
      Settings → Webhooks → существующий вебхук → изменить URL на
      `https://evorove.com/billing/webhook` (раньше он указывал на старый
      `api.evorove.com`). Secret вебхука (поле «Signing secret») остаётся тем
      же — он уже должен быть в `/opt/evorove/.env` как
      `LEMONSQUEEZY_WEBHOOK_SECRET` (раздел 1). Если меняете и секрет —
      впишите новое значение туда же.
- [ ] **Проверить тестовую оплату в test mode.** В Lemon Squeezy включите Test
      mode (переключатель в дашборде). На сайте добавьте e-mail владельца
      (под которым вы залогинены) в `BILLING_TEST_MODE_ALLOWED_EMAILS` в
      `.env`, если его там ещё нет, перезапустите `evorove`. Затем на
      `/app/billing` нажмите «Subscribe to Starter» (или Pro), пройдите
      тестовый чекаут тестовой картой Lemon Squeezy. Проверьте: пришёл вебхук
      (вид в `journalctl -u evorove`), план и статус подписки на `/app/billing`
      обновились. Выключите Test mode обратно, когда закончите.
- [ ] **Проверить уходящие письма.** Два независимых канала:
  - системные (сброс пароля) — откройте `/forgot-password`, запросите сброс
    на свою почту, письмо должно прийти через Zoho (`SYSTEM_SMTP_*`);
  - рассылка клиента 0 — **не снимайте Outreach paused ради этой проверки**
    на настоящих людях. Сначала отправьте тестовое письмо себе: на
    `/app/contacts` добавьте свой собственный e-mail как контакт с обяза-
    тельной note, снимите пометку «Outreach paused» на 2–3 минуты, посмотрите,
    что письмо дошло и выглядит нормально, затем поставьте паузу обратно, пока
    не готовы вести реальную рассылку.
- [ ] **Бэкап и восстановление.** Убедитесь, что Litestream реально пишет в
      R2 и что восстановление работает — команды `docs/DEPLOY_RU.md`,
      разделы 5 (проверка записи) и 8 (восстановление, тренировочно можно не
      делать прямо в день переезда — но хотя бы команду из раздела 5
      выполнить и увидеть файл в R2 нужно сегодня же).

---

## 6. Откат за 5 минут

Railway ничего не выключали (раздел 7), поэтому откат — это просто вернуть
DNS обратно, а не поднимать старое с нуля:

- [ ] В Railway: Settings сервиса `ai-business-process-engine` → Domains →
      снова привяжите custom domain `evorove.com` **и** `api.evorove.com`
      (оба отвязывали вместе в разделе 4). Railway покажет те же инструкции
      по DNS, что и раньше.
- [ ] В Cloudflare: либо удалите оба Public Hostname (`evorove.com` и
      `api.evorove.com`) в тоннеле `evorove` (тогда освободившиеся имена
      снова можно направить на Railway так, как просит Railway выше), либо,
      если Railway просит CNAME на конкретный адрес — добавьте/верните эти
      DNS-записи в Cloudflare DNS для обоих хостов (они будут приоритетнее
      записи от тоннеля, если тоннель остался добавленным, но запись в DNS
      правится отдельно от Zero Trust — в Cloudflare Dashboard →
      evorove.com → DNS). MX/SPF/DKIM/`_dmarc` при этом не трогаем — они не
      связаны с тем, где отвечает сайт.
- [ ] На новом сервере ничего выключать необязательно — он просто перестанет
      получать трафик по `evorove.com`/`api.evorove.com`, пока DNS не
      прогреется (обычно несколько минут, Cloudflare proxy меняется почти
      сразу).
- [ ] Проверьте `https://evorove.com` — должен снова открыться старый Railway-
      сайт.
- [ ] Вебхук Lemon Squeezy придётся на время откатить обратно на старый URL
      (раздел 5) — иначе события подписки будут уходить в недоступный `/billing/webhook`
      нового сервера, пока Railway не восстановлен как основной.

**Railway не выключаем минимум 7 дней** после переключения — это и есть план
отката; раньше срока ничего из раздела 7 не делаем.

---

## 7. Что выключить на Railway через неделю, а что оставить

Через 7 стабильных дней на новом сервере (`docs/ROADMAP.md`: «evorove.com
работает с нового сервера неделю без падений»):

Кроны `evorove-sweep-cron` и `evorove-lead-market-signals-cron` (их работу
теперь делает внутренний планировщик `evorove_lite`, выключены в разделе 2)
и GitHub Actions `scheduled-tasks.yml` (выключен в разделе 4) уже не
работают — здесь речь про то, что можно удалить полностью, и в каком
порядке:

- [ ] **Шаг 1 — сначала сервисы:** `ai-business-process-engine`
      (движок/сайт/api — полностью заменён `evorove_lite`), `evorove-crm`,
      `evorove-lead`, `searxng`.
- [ ] **Шаг 2 — перед удалением трёх Postgres-баз (`Postgres`,
      `Postgres-9WEU`, `Postgres-EY-2`) — проверить `n8n`.** В переменных
      окружения сервиса `n8n` на Railway (`DB_TYPE` и соседние) убедиться,
      что `n8n` хранит свою базу на собственном volume (SQLite), а **не** в
      одном из этих трёх Postgres. Только после этой проверки и свежего
      `pg_dump` (раздел 2, если не снимали отдельно прямо перед удалением) —
      можно удалять все три базы.

**Остаётся на Railway без изменений:**
- [ ] **n8n** (и его persistent volume) — не трогаем вообще, это отдельная
      задача владельца;
- [ ] любые другие проекты/сервисы в этом Railway-аккаунте, не входящие в
      список выше.

Выключает сама Алёна, когда решит, что неделя прошла спокойно — агент этого
не делает.

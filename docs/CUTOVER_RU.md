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

- [x] **Системная почта сайта уже настроена: Resend + Cloudflare Email Routing.**
  Resend отправляет системные письма (сброс пароля, уведомления).
  Cloudflare Email Routing пересылает входящие на `support@evorove.com`.
  Настройки `SYSTEM_SMTP_*` уже заданы; повторно настраивать провайдера
  при переезде не нужно. Холодная рассылка использует отдельный домен и
  Google Workspace — раздел «Новый ящик рассылки» ниже.

  **При переключении сайта и при откате нельзя менять или удалять эти
  действующие записи в DNS-зоне `evorove.com`:**
  - SPF (TXT): `v=spf1 include:_spf.mx.cloudflare.net ~all`;
  - MX Cloudflare Email Routing;
  - DKIM Resend: `resend._domainkey`;
  - DKIM Cloudflare: `cf2024-1._domainkey`.

  Эти записи обеспечивают работающую системную почту и пересылку поддержки.
  Изменение адреса сайта на Railway/VPS их не затрагивает. Существующую
  запись `_dmarc.evorove.com` также сохраняем без изменений.

- [ ] **Новый ящик рассылки** — отдельная подготовка, раздел «Новый ящик
  рассылки» ниже: нужен минимум за 2–3 недели до первого настоящего холодного
  письма (прогрев), не в последний день.

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
  `/etc/cloudflared/evorove.token` (создаётся `setup.sh`), права `0600`,
  владелец `cloudflared:cloudflared` — см. `docs/DEPLOY_RU.md`, раздел 4 и
  `deploy/cloudflared/README_RU.md`. Публичные хостнеймы для `evorove.com` и
  `api.evorove.com` в этом туннеле пока **не добавляйте** — сначала
  используем временный (раздел 4).

- [ ] **Старый ключ `ACCOUNT_SECURITY_ENCRYPTION_KEY` — для старых ссылок
  отписки.** Нужен, чтобы `api.evorove.com` на новом сервере мог ещё
  понимать токены отписки из уже отправленных писем (раздел 4; маршрут
  `GET/POST /api/v1/public/unsubscribe/{token}` в `evorove_lite` уже есть,
  `app/legacy_unsubscribe.py`). Где взять: Railway → проект → сервис
  `ai-business-process-engine` → Variables → `ACCOUNT_SECURITY_ENCRYPTION_KEY`
  — **Алёна копирует значение сама, не через агентов**. Куда вписать:
  `/opt/evorove/.env`, под **новым** именем
  `LEGACY_ACCOUNT_SECURITY_ENCRYPTION_KEY` (не поверх `SECRET_KEY` — это
  разные ключи для разных схем токена).

- [ ] **Своя копия `SECRET_KEY` и Fernet-ключа (= сам `SECRET_KEY`, из него
  выводится ключ шифрования почтовых паролей) — у Алёны отдельно от
  сервера**, в менеджере паролей, не только в `/opt/evorove/.env`. Без этого
  значения бэкап из R2 восстановит базу, но **не расшифрует** сохранённые
  `smtp_password_enc`/`imap_password_enc` в `businesses` — без своей копии
  ключа восстановление после потери сервера означает заново вводить пароли
  почты, а не просто поднять файл из R2.

---

## 2. Бэкап старых баз Railway перед переездом

Это страховка на случай, если что-то в переносе данных (раздел 3) пойдёт не
так — Railway всё равно работает и его не трогаем минимум 7 дней (раздел 6),
но свежий дамп должен лежать отдельно от Railway.

- [ ] **Сначала выключить кроны на Railway** — `evorove-sweep-cron`,
      `evorove-lead-market-signals-cron` **и** GitHub Actions
      `.github/workflows/scheduled-tasks.yml` в `AleneSparrow/Evorove`
      (GitHub → Actions → этот workflow → Disable workflow) — вместе, одним
      шагом, **до** снятия дампа: иначе что-то одно продолжит менять данные
      (слать письма, двигать сделки) уже после того, как дамп снят, и эти
      изменения потеряются при импорте. Перед тем как снимать дамп,
      убедитесь, что нет уже запущенного прогона ни одного из них (Railway →
      Deployments/Logs по каждому крону, GitHub → Actions → этот workflow →
      нет жёлтого «in progress» запуска) — остановленный, но ещё
      выполняющийся прогон может дописать данные после самого выключения.
- [ ] **Поставить рассылку на паузу в старом приложении** (владелец бизнеса
      клиента 0 → настройки email-рассылки в UI Railway-версии): без этого
      шага между остановкой кронов/Actions и снятием дампа ответы могут
      продолжить приходить, а первые письма — продолжить уходить вручную
      через UI.
- [ ] В Railway откройте каждую Postgres-базу трёх старых сервисов (engine,
      crm, lead) и возьмите временную **read-only** строку подключения (или
      выполните `railway run pg_dump ...` прямо из CLI, если он настроен).
- [ ] Введите три строки подключения в своём терминале через `read -s` (не
      видны на экране, не остаются в истории шелла построчно) и `export` —
      **не** как аргументы команды и не в этот файл:
  ```bash
  read -rs OLD_ENGINE_DATABASE_URL; export OLD_ENGINE_DATABASE_URL; echo
  read -rs OLD_CRM_DATABASE_URL; export OLD_CRM_DATABASE_URL; echo
  read -rs OLD_LEAD_DATABASE_URL; export OLD_LEAD_DATABASE_URL; echo
  ```
- [ ] Снимите дамп каждой базы отдельно, в той же сессии:
  ```bash
  pg_dump "$OLD_ENGINE_DATABASE_URL" --format=custom --file=engine-$(date -u +%Y%m%dT%H%M%SZ).dump
  pg_dump "$OLD_CRM_DATABASE_URL"    --format=custom --file=crm-$(date -u +%Y%m%dT%H%M%SZ).dump
  pg_dump "$OLD_LEAD_DATABASE_URL"   --format=custom --file=lead-$(date -u +%Y%m%dT%H%M%SZ).dump
  ```
  Эти же три переменные нужны ещё раз в разделе 3 (перенос данных) — не
  делайте `unset` сразу, сделайте его один раз после того, как раздел 3
  полностью закончен (команда — в конце раздела 3).
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

| Таблица в новой SQLite | Источник на Railway | Примечание |
| --- | --- | --- |
| `businesses`, включая `subscription_ends_at` (1 строка) | `businesses` + активная `business_dna` (engine), опционально `briefs` (lead) | — |
| `businesses.smtp_host/smtp_port/smtp_user`, `imap_host/imap_port/imap_user` | `email_connections` (engine) | **Для этого переезда не используем — см. «Новый ящик рассылки» ниже**: ящик меняется на новый домен, старые host/user не подходят. Пароли (`smtp_password_enc`) не переносятся в любом случае — другой ключ шифрования. |
| `businesses.imap_last_uid`/`imap_uidvalidity` | `email_connections` (engine) | **По умолчанию не переносятся вообще** (оба пишутся `NULL`) — курсор IMAP сбрасывается, не наследуется от старого ящика. Флаг `--keep-imap-cursor` существует, но **не используем его** в этом переезде: ящик новый, курсор старого к нему не относится. |
| `users` | `staff_users` этого бизнеса (engine); старый `pbkdf2_sha256$...` хеш пароля `app/auth.py` понимает и обновит до Argon2 при первом входе | — |
| `suppressions` | `email_suppressions` (engine) | — |
| `people` | `board_people` (crm, только строки с email) + непринятые лиды `engine.leads`/`lead.leads` при `--include-unboarded-leads` | контакты без email пропускаются (в `evorove_lite` email обязателен); без флага непринятые лиды не переносятся — **решает Алёна**, нужны ли они, или их проще найти заново циклом 1 |
| `messages` (полный текст + `subject` + `smtp_message_id` для трединга) | `conversation_messages` (engine) в приоритете; `board_touches` (crm, обрезанная до 500 символов сводка) добавляется только там, где полного сообщения нет | — |
| `people.pain/evidence_quote/source_url/us_state` | `board_touches` (crm), `kind='assembled'` | — |
| `log` (заметки владельца, `event='note'`) | `board_notes` (crm) | — |

**Не переносит вообще** (ввести/решить отдельно):
- паролей `smtp_password_enc`/`imap_password_enc` — другой ключ шифрования,
  вводятся заново на `/app/settings` (раздел 1 и «Новый ящик рассылки»
  ниже), порт SMTP — **587** (поле уже стоит на 587 по умолчанию в форме
  настроек);
- `imap_last_uid`/`imap_uidvalidity` — намеренно, см. таблицу выше;
- `billing_webhook_events`, `marketing_assets`/`marketing_guidance`,
  `staff_sessions`/`password_reset_tokens`/`login_throttles`, всё про
  встречи/календарь/КП/эскалации/LoRA, дубликаты CRM-таблиц (`leads`,
  `deals`, `contacts`, …) и служебные таблицы `evorove-lead` (кэш, прогоны
  поиска, `llm_calls` и т.п.) — не нужны продукту `evorove_lite`, не
  переносим.

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
новую SQLite.

**Полный импорт (`--apply`) выполняется один раз — и это теперь проверяется
кодом, не только дисциплиной.** Если в базе уже есть хоть один прогон
(`runs`) или ещё не отмеченное импортом сообщение — повторный `--apply` без
`--force-reimport` **отказывает** (`Import refused: live database; use
--force-reimport to preserve existing rows`) и ничего не пишет. С явным
`--force-reimport` импорт выполняется, но существующие строки (бизнес,
пользователи, стоп-лист, карточки, сообщения) **не перезаписываются** —
вставки идут через `ON CONFLICT DO NOTHING`, так что то, что Алёна уже
настроила руками на `/app/settings` после первого импорта, не затирается.
Повторный прогон без `--force-reimport` ради проверки всё равно не нужен —
`--dry-run` безопасен для повторов сколько угодно раз.

Новый бизнес после импорта всегда остаётся с рассылкой **на паузе**
(`outreach_paused`, по умолчанию включена) — это намеренно, и после импорта
эту паузу **не снимаем** до конца прогрева нового ящика (раздел «Новый ящик
рассылки» ниже), что бы ни показали проверки разделов 4 и 5.

Порядок на сервере (после того, как `deploy/setup.sh` уже выполнен и
`/opt/evorove/.env` заполнен — разделы 1–3 `docs/DEPLOY_RU.md`; три
`OLD_*_DATABASE_URL` уже экспортированы в этой же сессии — раздел 2):

```bash
sudo -u evorove /opt/evorove/.venv/bin/pip install \
  --requirement /opt/evorove/requirements-import.txt
```

- [ ] Сухой прогон (ничего не меняет, только печатает количества):
  ```bash
  read -rs CLIENT_ZERO_BUSINESS_ID; export CLIENT_ZERO_BUSINESS_ID; echo
  sudo -u evorove --preserve-env=OLD_ENGINE_DATABASE_URL,OLD_CRM_DATABASE_URL,OLD_LEAD_DATABASE_URL,CLIENT_ZERO_BUSINESS_ID \
    /opt/evorove/.venv/bin/python /opt/evorove/scripts/import_from_railway.py --dry-run
  ```
  Значения вводятся через `read -s` в этой же сессии (разделы 2 и выше), не
  как аргументы команды и не в этот файл.
- [ ] Сверьте напечатанные количества строк с тем, что ожидаете увидеть
      (сколько карточек людей, сообщений и т.д. сейчас в CRM на Railway).
- [ ] **Повторный top-up только стоп-листа, сразу после того как Railway
      перестал отвечать на `evorove.com` (DNS переключён, раздел 4), и до
      того как где-либо снята пауза рассылки на новом сервере** (не раньше —
      пока DNS ещё ведёт на Railway, там могут появиться новые отписки по
      уже отправленным письмам; не позже — чтобы между этим top-up'ом и
      первым настоящим письмом с нового ящика рассылки не было окна без
      актуального стоп-листа):
  ```bash
  sudo -u evorove --preserve-env=OLD_ENGINE_DATABASE_URL,OLD_CRM_DATABASE_URL,OLD_LEAD_DATABASE_URL,CLIENT_ZERO_BUSINESS_ID \
    DB_PATH=/var/lib/evorove/evorove.sqlite3 \
    /opt/evorove/.venv/bin/python /opt/evorove/scripts/import_from_railway.py --suppressions-only --apply
  ```
- [ ] Только если числа из `--dry-run` похожи на правду — полный `--apply`,
      один раз:
  ```bash
  sudo -u evorove --preserve-env=OLD_ENGINE_DATABASE_URL,OLD_CRM_DATABASE_URL,OLD_LEAD_DATABASE_URL,CLIENT_ZERO_BUSINESS_ID \
    DB_PATH=/var/lib/evorove/evorove.sqlite3 \
    /opt/evorove/.venv/bin/python /opt/evorove/scripts/import_from_railway.py --apply
  ```
- [ ] Повторный `--dry-run` после импорта — числа не должны расти при ещё
      одном случайном запуске. Курсор IMAP не требует отдельной проверки —
      без `--keep-imap-cursor` (мы его не передаём) `imap_last_uid`/
      `imap_uidvalidity` уже `NULL` по умолчанию.
- [ ] Когда раздел 3 полностью закончен — `unset` три переменные подключения
      и `CLIENT_ZERO_BUSINESS_ID` в этой же сессии:
  ```bash
  unset OLD_ENGINE_DATABASE_URL OLD_CRM_DATABASE_URL OLD_LEAD_DATABASE_URL CLIENT_ZERO_BUSINESS_ID
  ```

---

## 4. День переезда по шагам

- [ ] **Preflight.** `evorove` на новом сервере пока не запущен. `.env`
      (включая `LEGACY_ACCOUNT_SECURITY_ENCRYPTION_KEY`, раздел 1),
      `/etc/evorove/litestream.env`, `/etc/cloudflared/evorove.token`
      заполнены. Данные перенесены и проверены (раздел 3). Свежий бэкап старых
      Postgres лежит отдельно (раздел 2). Исходящий 587 открыт (если не
      проверяли при заказе сервера — `nc -vz smtp.resend.com 587`). GitHub
      Actions и кроны Railway уже выключены (раздел 2) — здесь их повторно не
      трогаем. Railway продолжает отвечать на `evorove.com`/`api.evorove.com`
      — их не трогаем до самого переключения DNS.
  - [ ] **Проверить Cloudflare Worker `evorove-web`** (из репозитория
        `evorove`, `web/app/wrangler.web.toml`) — убедиться, что у него (или
        у Pages-проекта `evorove`) **не привязаны** Custom Domains
        `evorove.com`/`www.evorove.com`. Если привязаны — отвязать их в
        Cloudflare Dashboard → Workers & Pages → `evorove-web` → Custom
        Domains, иначе Worker продолжит перехватывать `/api/*`/`/widget/*` и
        будет конфликтовать с Public Hostname тоннеля на том же имени.
- [ ] **Запуск** (порядок — как в `docs/DEPLOY_RU.md`, раздел 6: сначала то,
      что переживает временное отсутствие приложения):
  ```bash
  sudo systemctl start litestream cloudflared
  sudo make -C /opt/evorove preflight
  sudo systemctl start evorove
  ```
- [ ] **На время смоука — временный хост в `EXTRA_ALLOWED_ORIGINS`.**
      Добавьте `https://cutover-check.evorove.com` в `/opt/evorove/.env`
      (`EXTRA_ALLOWED_ORIGINS=https://cutover-check.evorove.com`),
      `sudo systemctl restart evorove` — иначе формы (вход, settings) на
      временном хосте будут получать 403 Invalid origin. После раздела 4
      можно убрать это значение и перезапустить `evorove` снова — постоянно
      оно не нужно.
- [ ] **Смоук на временном адресе туннеля — до переключения evorove.com.** В
      Cloudflare Zero Trust → тоннель `evorove` → Public Hostname → добавьте
      временный поддомен `cutover-check.evorove.com`, service
      `http://localhost:8000` (это создаст свою DNS-запись, `evorove.com` при
      этом не трогается). Откройте этот адрес в браузере и проверьте:
  - [ ] `make -C /opt/evorove smoke BASE_URL=https://cutover-check.evorove.com`
        зелёный (`docs/DEPLOY_RU.md`, раздел 11);
  - [ ] главная страница и `/faq`, `/terms`, `/privacy` открываются;
  - [ ] вход под перенесённым аккаунтом владельца работает (форма входа — это
        и есть проверка `EXTRA_ALLOWED_ORIGINS` выше), доска показывает
        перенесённые карточки в нужных колонках;
  - [ ] на доске написано «Outreach paused» — рассылка не ушла сама.
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
  - [ ] **Теперь, пока Railway уже не отвечает на `evorove.com` — top-up
        стоп-листа** из раздела 3 (`--suppressions-only --apply`). Пауза
        рассылки на новом сервере при этом остаётся включённой — её не
        снимаем ни сейчас, ни в разделе 5 (снимается только после прогрева
        нового ящика, раздел «Новый ящик рассылки»).

---

## 5. После переключения

- [ ] **Сменить URL вебхука в Lemon Squeezy.** Lemon Squeezy Dashboard →
      Settings → Webhooks → существующий вебхук → изменить URL на
      `https://evorove.com/billing/webhook` (раньше он указывал на старый
      `api.evorove.com`). Secret вебхука (поле «Signing secret») остаётся тем
      же — он уже должен быть в `/opt/evorove/.env` как
      `LEMONSQUEEZY_WEBHOOK_SECRET` (раздел 1). Если меняете и секрет —
      впишите новое значение туда же.
- [ ] **Проверить тестовую оплату в test mode — не на бизнесе клиента 0.**
      Бизнес с `id = CLIENT_ZERO_BUSINESS_ID` теперь получает доступ
      автоматически (`billing.entitled(..., client_zero_id=...)`, без
      подписки вообще) — тестировать на нём Lemon Squeezy нет смысла, он
      всегда «entitled» независимо от результата теста. Заведите отдельный
      тестовый аккаунт (`/signup` с любым другим email). В Lemon Squeezy
      включите Test mode (переключатель в дашборде), добавьте email этого
      тестового аккаунта в `BILLING_TEST_MODE_ALLOWED_EMAILS` в `.env`,
      перезапустите `evorove`. На `/app/billing` этого тестового аккаунта
      нажмите «Subscribe to Starter» (или Pro), пройдите тестовый чекаут
      тестовой картой Lemon Squeezy. Проверьте: пришёл вебхук (вид в
      `journalctl -u evorove`), план и статус подписки на `/app/billing`
      обновились. Выключите Test mode обратно, когда закончите; тестовый
      аккаунт можно оставить или удалить — он не связан с клиентом 0.
- [ ] **Проверить системную почту.** Откройте `/forgot-password`, запросите
      сброс на свою почту, письмо должно прийти через Resend (`SYSTEM_SMTP_*`).
      Про рассылку клиента 0 — отдельно, раздел «Новый ящик рассылки»: до
      конца прогрева `Outreach paused` не снимаем вообще, в том числе ради
      самопроверки.
- [ ] **Бэкап и восстановление.** Убедитесь, что Litestream реально пишет в
      R2 и что восстановление работает — команды `docs/DEPLOY_RU.md`,
      разделы 5 (проверка записи) и 8 (восстановление, тренировочно можно не
      делать прямо в день переезда — но хотя бы команду из раздела 5
      выполнить и увидеть файл в R2 нужно сегодня же).

---

## 6. Откат

**Честно про «5 минут»: это только про DNS, и только пока рассылка клиента 0
ещё на паузе.** Сам возврат `evorove.com`/`api.evorove.com` на Railway — и
правда дело нескольких минут (DNS/прокси Cloudflare прогревается быстро).
Но если к моменту отката пауза уже снята и новый ящик рассылки реально
отправляет письма (раздел «Новый ящик рассылки»), откат — это ещё и шаги
ниже про стоп-лист и кроны, которые быстрыми не являются; не обещайте «5
минут» в этом случае.

Railway ничего не выключали (раздел 7), поэтому откат не поднимает старое с
нуля:

- [ ] **Первым шагом — пауза рассылки на новом сервере** (`/app/settings`,
      «Outreach paused»), даже если по плану она должна быть уже включена:
      откат может случиться позже, чем ожидалось, когда прогрев уже кончился
      и реальная рассылка идёт.
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
      evorove.com → DNS). Действующие MX Cloudflare, SPF
      `v=spf1 include:_spf.mx.cloudflare.net ~all`, DKIM `resend._domainkey`
      и `cf2024-1._domainkey`, а также `_dmarc` нельзя менять или удалять
      при откате: они обеспечивают системную почту (раздел 1).
- [ ] На новом сервере ничего выключать необязательно — он просто перестанет
      получать трафик по `evorove.com`/`api.evorove.com`, пока DNS не
      прогреется (обычно несколько минут, Cloudflare proxy меняется почти
      сразу).
- [ ] Проверьте `https://evorove.com` — должен снова открыться старый Railway-
      сайт.
- [ ] **Вернуть кроны и GitHub Actions на Railway** — `evorove-sweep-cron`,
      `evorove-lead-market-signals-cron` (Resume/Start) и
      `.github/workflows/scheduled-tasks.yml` (GitHub → Actions → Enable
      workflow) — иначе старое приложение снова отвечает на `evorove.com`, но
      без своих фоновых задач.
- [ ] **Вебхук Lemon Squeezy** — откатить URL обратно на старый
      (`https://api.evorove.com/api/v1/billing/webhook`, раздел 5) — иначе
      события подписки будут уходить в недоступный `/billing/webhook` нового
      сервера, пока Railway не восстановлен как основной.
- [ ] **Перенести стоп-лист обратно, если новая рассылка уже работала.** Это
      **не автоматизировано** — `scripts/import_from_railway.py` читает из
      Postgres и пишет в SQLite, в обратную сторону он не работает. Если
      пауза была снята и кто-то успел отписаться уже на новом сервере:
      выгрузите новые строки `suppressions` (`created_at` новее момента
      переключения раздела 4) из SQLite как CSV и вручную добавьте их в
      `email_suppressions` старой Postgres-базы Railway (через её консоль
      или `psql`), прежде чем старое приложение продолжит слать письма. Если
      рассылка всё это время была на паузе — этот шаг не нужен, список не
      менялся.

**Railway не выключаем минимум 7 дней** после переключения — это и есть план
отката; раньше срока ничего из раздела 7 не делаем.

---

## 7. Что выключить на Railway через неделю, а что оставить

Через 7 стабильных дней на новом сервере (`docs/ROADMAP.md`: «evorove.com
работает с нового сервера неделю без падений»):

Кроны `evorove-sweep-cron`, `evorove-lead-market-signals-cron` (их работу
теперь делает внутренний планировщик `evorove_lite`) и GitHub Actions
`scheduled-tasks.yml` уже выключены в разделе 2 — здесь речь про то, что
можно удалить полностью, и в каком порядке:

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

---

## Новый ящик рассылки

Решено: прежнего ящика рассылки больше нет, курсор IMAP из старой почты не
переносим вообще (раздел 3). Рассылка клиента 0 (первые письма, ответы)
переезжает на **новый, отдельный от `evorove.com` домен**, на **Google
Workspace**. Системная почта сайта уже работает через Resend и Cloudflare
Email Routing (раздел 1). Разделение на два домена и двух провайдеров —
осознанная изоляция репутации: что бы ни случилось с доставляемостью холодных писем, системные
письма сайта (сброс пароля, сводки) на это не завязаны.

Готовить начинаем заранее — минимум за 2–3 недели до дня переезда, прогрев
не делается в последний день:

- [ ] **Купить/выбрать домен рассылки** (не `evorove.com` и не его
      поддомен) — Алёна сама. Короткий, явно узнаваемый как Evorove, но
      технически отдельная DNS-зона — чтобы проблема с репутацией рассылки
      никогда не задела основной домен.
- [ ] **Google Workspace на этом домене.** Завести Workspace, подтвердить
      владение доменом (TXT-запись, которую даст Google), создать почтовый
      ящик для рассылки (например `sales@<домен-рассылки>`).
- [ ] **DNS-записи — в зоне нового домена**, не в зоне `evorove.com` (если
      домен не переведён в тот же Cloudflare-аккаунт — решает Алёна, где он
      управляется):
  - [ ] `MX` на Google (записи даст Google Workspace при настройке);
  - [ ] `TXT` SPF: `v=spf1 include:_spf.google.com ~all`;
  - [ ] `TXT`/`CNAME` DKIM — ключ и запись даст Google Admin Console
        (Apps → Google Workspace → Gmail → Authenticate email);
  - [ ] `TXT` DMARC на `_dmarc.<домен-рассылки>` — на старте мягкая политика
        для наблюдения, например
        `v=DMARC1; p=none; rua=mailto:<адрес-для-отчётов>`; ужесточать
        (`p=quarantine`, затем `p=reject`) только после прогрева, когда отчёты
        не показывают чужих/сломанных отправок.
- [ ] **Пароль приложения.** В Google Workspace (нужна двухфакторка на
      аккаунте) → Security → App passwords → пароль для почты. Это не
      пароль от самого аккаунта Google.
- [ ] **Куда вписать** — **не в `.env`**, а в браузере, после первого входа,
      на `/app/settings`, блоки «SMTP» и «IMAP»: host `smtp.gmail.com`, порт
      `587`, host `imap.gmail.com`, порт `993`, логин — полный адрес почты,
      пароль — пароль приложения из предыдущего пункта.
- [ ] **Проверка на пустой базе — до импорта клиента 0 (раздел 3), не
      после.** На свежем тестовом аккаунте (не клиент 0) впишите те же SMTP/
      IMAP на `/app/settings`, добавьте себя как единственный контакт на
      `/app/contacts`, снимите Outreach paused на пару минут, убедитесь, что
      письмо реально ушло и выглядит нормально, поставьте Outreach paused
      обратно. Цель — убедиться, что сама почта работает, прежде чем на неё
      будет полагаться что-либо с настоящими данными клиента 0.
- [ ] **Прогрев 2–3 недели до первых настоящих холодных писем.** Все эти дни
      `daily_cap` клиента 0 на `/app/settings` — **10**, не стандартные 20:
      малый, стабильно растущий объём снижает риск попадания в спам у новых
      получателей на новом домене. `Outreach paused` у клиента 0 остаётся
      включённым до конца прогрева (разделы 4–6) — единственная переписка
      эти недели — самопроверки, как в пункте выше, не настоящая рассылка.
      Поднимать `daily_cap` выше 10 — только после прогрева и только
      постепенно, ориентируясь на отсутствие bounce/жалоб (раздел 9 и 11
      `docs/DEPLOY_RU.md` — журналы и метрики).
- [ ] Только когда прогрев завершён и Алёна явно говорит «да» — снять
      `Outreach paused` у клиента 0 для настоящей рассылки. До этого момента
      ни один из предыдущих разделов (4, 5, 6) этот флаг не трогает.

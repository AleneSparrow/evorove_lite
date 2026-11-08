# Развёртывание Evorove на Ubuntu 24.04

Эта инструкция соответствует шагу 2 из `docs/ROADMAP.md`: один сервер Ubuntu
24.04 с 1 ГБ RAM, один процесс приложения, SQLite, Cloudflare Tunnel и
непрерывная репликация базы в Cloudflare R2. Docker не используется.

## 1. Подготовка Cloudflare

1. Добавьте домен в Cloudflare.
2. В R2 создайте отдельный bucket для Evorove.
3. Создайте R2 API token с правами Object Read & Write только для этого bucket.
   Сохраните Access Key ID, Secret Access Key и S3 endpoint вида
   `https://ACCOUNT_ID.r2.cloudflarestorage.com`.
4. В Zero Trust → Networks → Tunnels создайте remotely-managed tunnel.
5. Добавьте Public Hostname и направьте его в `http://localhost:8000`.
6. Сохраните tunnel token. Он будет записан в файл на сервере, а не в unit или
   репозиторий.

## 2. Подготовка сервера

Подключитесь по SSH. Если SSH работает не на порту 22, далее передайте реальный
порт через `SSH_PORT`, иначе firewall может закрыть доступ.

Репозиторий должен находиться именно в `/opt/evorove`, потому что этот путь
зафиксирован в systemd unit:

```bash
sudo apt-get update
sudo apt-get install -y git ca-certificates
sudo git clone --branch main YOUR_REPOSITORY_URL /opt/evorove
cd /opt/evorove
sudo SSH_PORT=22 ./deploy/setup.sh
```

`setup.sh` можно запускать повторно. Он:

- устанавливает системные пакеты, Python 3.12, `cloudflared` и Litestream;
- создаёт системных пользователей `evorove` и `cloudflared`;
- создаёт venv и устанавливает только зависимости из существующего
  `requirements.txt`;
- создаёт и включает swap-файл 2 ГБ;
- устанавливает и включает systemd units, но не запускает их;
- включает UFW с запретом входящих соединений, кроме указанного SSH-порта;
- не перезаписывает существующие файлы с настройками и секретами.

После выполнения проверьте firewall до завершения текущей SSH-сессии:

```bash
sudo ufw status verbose
```

На чистом сервере в списке входящих разрешений должен быть только SSH. Порт
8000 не открывается наружу: к нему по loopback подключается `cloudflared`.

## 3. Настройки приложения

Откройте созданный файл:

```bash
sudoedit /opt/evorove/.env
```

Заполните в нём production-переменные приложения из `docs/CONTRACT.md`. Для
базы обязательно укажите:

```text
DB_PATH=/var/lib/evorove/evorove.sqlite3
```

Не добавляйте этот файл в Git. Проверьте права:

```bash
sudo chown root:evorove /opt/evorove/.env
sudo chmod 0640 /opt/evorove/.env
```

## 4. Cloudflare Tunnel

Запишите полученный tunnel token одной строкой:

```bash
sudoedit /etc/cloudflared/evorove.token
sudo chown cloudflared:cloudflared /etc/cloudflared/evorove.token
sudo chmod 0600 /etc/cloudflared/evorove.token
```

Токен является секретом. Не передавайте его аргументом команды и не сохраняйте
в unit. Дополнительная краткая инструкция находится в
`deploy/cloudflared/README_RU.md`.

## 5. Репликация SQLite в R2

Откройте файл переменных Litestream:

```bash
sudoedit /etc/evorove/litestream.env
```

Заполните его без реальных значений в репозитории:

```text
LITESTREAM_R2_BUCKET=your-private-bucket
LITESTREAM_R2_ENDPOINT=https://ACCOUNT_ID.r2.cloudflarestorage.com
LITESTREAM_R2_ACCESS_KEY_ID=replace-me
LITESTREAM_R2_SECRET_ACCESS_KEY=replace-me
```

Затем восстановите требуемые права и проверьте, как Litestream прочитал
конфигурацию:

```bash
sudo chown root:evorove /etc/evorove/litestream.env
sudo chmod 0640 /etc/evorove/litestream.env
sudo systemd-run --wait --pipe --collect \
  --property=User=evorove \
  --property=Group=evorove \
  --property=EnvironmentFile=/etc/evorove/litestream.env \
  /usr/bin/litestream databases -config /etc/litestream.yml
```

## 6. Первый запуск и проверка

Запускайте компоненты по порядку (подробнее о проверках — раздел 11):

```bash
sudo systemctl start litestream cloudflared
sudo make -C /opt/evorove preflight
sudo systemctl start evorove
make -C /opt/evorove smoke BASE_URL=https://YOUR_DOMAIN
```

До старта приложения tunnel может временно возвращать 502, а Litestream —
ожидать появления файла базы. Сначала убедитесь, что оба сервиса активны.

Проверьте состояния и последние журналы:

```bash
sudo systemctl --no-pager --full status evorove litestream cloudflared
sudo journalctl -u evorove -u litestream -u cloudflared -n 100 --no-pager
sudo systemctl show evorove -p MemoryCurrent -p MemoryMax
```

`MemoryMax` должен быть `314572800` байт. Внешний URL должен отвечать через
Cloudflare, при этом прямого входящего доступа к порту 8000 быть не должно.

## 7. Обычное обновление

Владелец проекта выполняет push. На сервере обновление выглядит так:

```bash
sudo -u evorove git -C /opt/evorove pull --ff-only
sudo -u evorove /opt/evorove/.venv/bin/pip install \
  --requirement /opt/evorove/requirements.txt
sudo make -C /opt/evorove preflight
sudo systemctl restart evorove
make -C /opt/evorove smoke BASE_URL=https://YOUR_DOMAIN
```

Если менялись файлы в `deploy/`, сначала повторно выполните
`sudo /opt/evorove/deploy/setup.sh`, проверьте конфигурацию и только потом
перезапускайте затронутые units.

## 8. Восстановление SQLite из R2

Восстановление требует остановки приложения и Litestream. Оно не перезаписывает
старую базу: существующие файлы сначала переносятся в каталог с временной меткой.

```bash
sudo systemctl stop cloudflared evorove litestream
RESTORE_BACKUP=/var/lib/evorove/pre-restore-$(date -u +%Y%m%dT%H%M%SZ)
sudo install -d -o evorove -g evorove -m 0700 "$RESTORE_BACKUP"
for file in \
  /var/lib/evorove/evorove.sqlite3 \
  /var/lib/evorove/evorove.sqlite3-wal \
  /var/lib/evorove/evorove.sqlite3-shm \
  /var/lib/evorove/evorove.sqlite3-journal; do
  if sudo test -e "$file"; then
    sudo mv "$file" "$RESTORE_BACKUP/"
  fi
done
sudo systemd-run --wait --pipe --collect \
  --unit=evorove-litestream-restore \
  --property=User=evorove \
  --property=Group=evorove \
  --property=EnvironmentFile=/etc/evorove/litestream.env \
  /usr/bin/litestream restore -config /etc/litestream.yml \
  -integrity-check full /var/lib/evorove/evorove.sqlite3
sudo systemctl start evorove
curl --fail --silent --show-error http://127.0.0.1:8000/health
sudo systemctl start litestream cloudflared
```

Если restore завершился с ошибкой, не запускайте приложение: изучите журнал
временного unit командой `sudo journalctl -u evorove-litestream-restore` и при
необходимости верните сохранённые файлы из `$RESTORE_BACKUP` при остановленных
`evorove` и `litestream`.

## 9. Диагностика

```bash
sudo systemctl is-enabled evorove litestream cloudflared
sudo systemctl is-active evorove litestream cloudflared
sudo journalctl -u evorove --since today
sudo journalctl -u litestream --since today
sudo journalctl -u cloudflared --since today
sudo ss -lntp
free -h
```

Ожидается только один uvicorn worker, слушающий `127.0.0.1:8000`. Если процесс
превысит лимит памяти 300 МБ, systemd завершит его, а политика `Restart=on-failure`
запустит его снова.

## 10. Чек-лист для Алёны: что вводить и куда (коротко)

**Секреты — только имена переменных, значения берутся из своих источников
(Cloudflare, Lemon Squeezy, Anthropic, Telegram), не из этого файла:**

- `/opt/evorove/.env` (создаётся `setup.sh` из `.env.example`, редактировать
  через `sudoedit`, раздел 3): `APP_BASE_URL`, `SECRET_KEY`, `DB_PATH` —
  обязательны для запуска; далее `ANTHROPIC_API_KEY`, `ANTHROPIC_MODEL`,
  `DAILY_TOKEN_BUDGET`, `PER_RUN_TOKEN_BUDGET`, `LEMONSQUEEZY_API_KEY`,
  `LEMONSQUEEZY_WEBHOOK_SECRET`, `LEMONSQUEEZY_STORE_ID`,
  `LEMONSQUEEZY_VARIANT_STARTER`, `LEMONSQUEEZY_VARIANT_PRO`,
  `BILLING_TEST_MODE_ALLOWED_EMAILS`, `SYSTEM_SMTP_HOST`, `SYSTEM_SMTP_PORT`,
  `SYSTEM_SMTP_USER`, `SYSTEM_SMTP_PASSWORD`, `SYSTEM_SMTP_FROM`,
  `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `CLIENT_ZERO_BUSINESS_ID`,
  `TIMEZONE_OWNER`, `MARKET_SIGNALS`; необязательные
  `LEGACY_ACCOUNT_SECURITY_ENCRYPTION_KEY` и `EXTRA_ALLOWED_ORIGINS` нужны
  только на переезде (`docs/CUTOVER_RU.md`) — полный и актуальный список
  всегда в `.env.example`; что каждая означает — в `docs/CONTRACT.md`.
- `/etc/evorove/litestream.env` (раздел 5): `LITESTREAM_R2_BUCKET`,
  `LITESTREAM_R2_ENDPOINT`, `LITESTREAM_R2_ACCESS_KEY_ID`,
  `LITESTREAM_R2_SECRET_ACCESS_KEY`.
- `/etc/cloudflared/evorove.token` (раздел 4): не переменная, а файл из одной
  строки — tunnel token целиком.

**Команда запуска** (после того, как все файлы выше заполнены и
`deploy/setup.sh` уже выполнен без ошибок — раздел 2 и 6; порядок именно
такой — `litestream`/`cloudflared` переживают временные 502/ожидание файла
базы, `preflight` и сам `evorove` идут только после них):

```bash
sudo systemctl start litestream cloudflared
sudo make -C /opt/evorove preflight
sudo systemctl start evorove
make -C /opt/evorove smoke BASE_URL=https://YOUR_DOMAIN
```

**Проверка `/health`** (должно вернуть успешный ответ до и после каждого
перезапуска):

```bash
curl --fail --silent --show-error http://127.0.0.1:8000/health
```

**Бэкап в R2 и логи:**

```bash
# Litestream реально пишет в R2 (раздел 5):
sudo systemd-run --wait --pipe --collect --property=User=evorove \
  --property=Group=evorove --property=EnvironmentFile=/etc/evorove/litestream.env \
  /usr/bin/litestream databases -config /etc/litestream.yml

# Последние логи всех трёх сервисов (раздел 9):
sudo journalctl -u evorove -u litestream -u cloudflared -n 100 --no-pager
```

**Как откатиться**, если новый деплой сломан:

```bash
# 1. Откат кода на предыдущий коммит/тег:
sudo systemctl stop evorove
sudo -u evorove git -C /opt/evorove checkout <предыдущий-коммит-или-тег>
sudo -u evorove /opt/evorove/.venv/bin/pip install \
  --requirement /opt/evorove/requirements.txt
sudo systemctl start evorove
curl --fail --silent --show-error http://127.0.0.1:8000/health

# 2. Если дело не в коде, а в самой базе — восстановить её из R2 (раздел 8),
#    это отдельная операция и требует останова evorove и litestream.
```

Если `/health` не отвечает и после откат кода не помог, не трогайте базу —
соберите `journalctl` (команда выше) и восстановление из R2 делайте только по
инструкции в разделе 8, с резервной копией старых файлов.

## 11. Автоматические проверки деплоя

Скрипты запускаются из `/opt/evorove`, возвращают 0 при успехе и ненулевой код
при ошибке. Нужен Python 3; shellcheck нужен только на машине разработки.
Скрипты не печатают значения переменных, тела HTTP-ответов и вывод Litestream.
`.env` не исполняется как shell-код. Проверка присутствия читает его только на
сервере; обязательный список production-переменных хранится в `REQUIRED_ENV` в
`deploy/checks.py`. Остальные имена из `.env.example` необязательны: пустые
дают `INFO optional not set: ИМЯ`, без ошибки. Telegram можно отключить,
оставив оба поля пустыми; только одно заполненное поле — ошибка.
Список намеренно строже runtime-defaults: `APP_BASE_URL` и `DB_PATH` имеют
локальные defaults, `SECRET_KEY` может генерироваться локально;
`SYSTEM_SMTP_PORT` имеет default 587, `SYSTEM_SMTP_FROM` — dev@localhost,
а SMTP-аутентификация выполняется только при заданном `SYSTEM_SMTP_USER`.
Для production все эти значения остаются обязательными по политике деплоя.
Проверяется только непустое значение, а не валидность или доступность ключей. Подставлять фиктивные значения ради прохождения проверки не следует.

### Перед стартом или перезапуском приложения

На первом запуске сначала запустите зависимости. Перед первым стартом и
каждым обновлением приложения:

```bash
sudo systemctl start litestream cloudflared
sudo make -C /opt/evorove preflight
sudo systemctl restart evorove
```

`preflight.sh` проверяет доступную RAM ≥128 МиБ, свободный swap ≥256 МиБ,
свободное место ≥1 ГиБ на файловых системах кода и базы, наличие бинарников и
активность обоих сервисов, режим токена 0600, активность UFW и адреса TCP-порта
8000. При остановленном приложении отсутствие слушателя допустимо; каждый
существующий слушатель обязан иметь адрес строго `127.0.0.1:8000`.
Проверка запускается вручную с root-правами и не является `ExecStartPre`:
это позволяет избежать зависимости старта приложения от сервисов, которые
сами запускаются после него. Владельцем токена должен быть `cloudflared`,
чтобы сервис мог читать файл с режимом 0600 (см. раздел 4).

### После старта и каждого обновления

```bash
make -C /opt/evorove smoke BASE_URL=https://YOUR_DOMAIN
# Для проверки приложения напрямую на сервере:
/opt/evorove/deploy/smoke.sh http://127.0.0.1:8000
```

Проверяются `/health`, `/`, `/login`, `/faq`, `/terms`, CSS и JS, а также POST
`/billing/webhook` без подписи: допустимы только 400/401. Код 503 означает,
что billing не настроен, и тоже считается ошибкой. Каждый ответ проверяется
на признаки traceback/внутренней ошибки. Проверка не создаёт платежей.

### Проверка восстановления бэкапа

Запускайте после первого появления реплики в R2, после изменения настройки
репликации и регулярно, например раз в неделю. Цель Makefile сама запускает
скрипт через `systemd-run --wait --pipe --collect` с `User=evorove`,
`Group=evorove` и `EnvironmentFile=/etc/evorove/litestream.env`, как для самого
Litestream. Запускайте её с root-правами; ключи R2 не нужно передавать вручную:

```bash
sudo make -C /opt/evorove backup-check
```

`backup-check.sh` восстанавливает последнюю доступную копию в закрытый
временный каталог, выполняет `PRAGMA integrity_check` и сравнивает число строк
в `businesses`, `users`, `people`, `messages`, `payments`, `runs`, `billing_events`
с согласованным снимком рабочей SQLite-базы в режиме чтения, с учётом WAL.
Временный каталог удаляется и при обычной ошибке восстановления или проверки.
Рабочая база не заменяется. Принудительное завершение процесса SIGKILL может
оставить временный каталог `evorove-backup-*` для ручного удаления.

Расхождение считается ошибкой и выводит только имя таблицы и два числа.
На работающем сайте причиной может быть задержка репликации или новые записи:
повторите проверку в спокойное время, а для строгого сравнения остановите только
`evorove`, дождитесь отправки оставшегося WAL через работающий Litestream и
повторите проверку. Совпадение числа строк не доказывает равенство содержимого.
Для другой установки доступны `--db-path PATH` и `--config PATH`.

Локальные проверки перед коммитом: `make test` и `make shellcheck`.
Синтаксис restore соответствует [документации Litestream](https://litestream.io/reference/restore/).

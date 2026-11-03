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
sudo git clone YOUR_REPOSITORY_URL /opt/evorove
cd /opt/evorove
sudo git switch feat/deploy
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
sudo chown root:cloudflared /etc/cloudflared/evorove.token
sudo chmod 0640 /etc/cloudflared/evorove.token
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

Запускайте компоненты по порядку:

```bash
sudo systemctl start evorove
curl --fail --silent --show-error http://127.0.0.1:8000/health
sudo systemctl start litestream
sudo systemctl start cloudflared
```

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
sudo systemctl restart evorove
curl --fail --silent --show-error http://127.0.0.1:8000/health
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

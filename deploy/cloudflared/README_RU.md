# Cloudflare Tunnel для Evorove

Используется remotely-managed tunnel. Его конфигурация маршрута хранится в
Cloudflare, а на сервере хранится только токен в отдельном файле.

1. В Cloudflare Zero Trust создайте tunnel и добавьте Public Hostname с service
   `http://localhost:8000`.
2. На сервере откройте файл токена командой `sudoedit
   /etc/cloudflared/evorove.token` и вставьте только сам токен, одной строкой.
3. Ограничьте доступ к файлу:

   ```bash
   sudo chown cloudflared:cloudflared /etc/cloudflared/evorove.token
   sudo chmod 0600 /etc/cloudflared/evorove.token
   ```

4. Включите tunnel до запуска самого приложения (см. порядок в
   `docs/DEPLOY_RU.md`, раздел 6: `litestream`/`cloudflared` переживают
   временные 502, пока `evorove` ещё не поднялся):

   ```bash
   sudo systemctl enable --now cloudflared
   sudo systemctl status cloudflared --no-pager
   ```

Токен нельзя добавлять в Git, unit или командную строку. Для его замены
остановите unit, замените содержимое файла и снова запустите unit.

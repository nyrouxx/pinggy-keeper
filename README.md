# pinggy-keeper

Обновляет AAAA-запись FreeDNS для клиента WG Tunnel. Официальный клиент Pinggy
открывает UDP-туннель до существующего сервера WireGuard. Keeper опрашивает
`GET /urls` в Pinggy Web Debugger, разрешает публичное имя туннеля в IPv4 и
публикует адрес вместе с портом в формате IP4P. [Pinggy API](https://pinggy.io/docs/api/web_debugger_api/) · [IP4P в WG Tunnel](https://www.wgtunnel.com/docs/guides/ip4p-self-hosting/)

Например, `203.0.113.8:51820` кодируется в `2001::ca6c:cb00:7108`.
При изменении только порта запись тоже обновится. При временном сбое keeper
повторит запрос и опубликует адрес, когда оба сервиса ответят.

## Настройка

1. В FreeDNS создайте отдельную запись типа **AAAA** для WireGuard и включите
   динамическое обновление в интерфейсе v2. Из ссылки `/u/<token>/` возьмите
   токен этой записи. Если используете связанные обновления FreeDNS, выделите
   записи отдельный начальный адрес или отключите связанные обновления.
   [FreeDNS Dynamic DNS v2](https://freedns.afraid.org/dynamic/v2/) · [Документация FreeDNS](https://freedns.afraid.org/guide/dd-wrt-v2/)
2. Возьмите токен туннеля в аккаунте Pinggy. Выберите сервер, указанный в
   команде подключения в панели Pinggy. [Панель Pinggy](https://dashboard.pinggy.io/)
3. Скопируйте шаблон и заполните `PINGGY_TOKEN` и `FREEDNS_TOKEN` в файле `.env`.

   ```sh
   cp .env.example .env
   chmod 600 .env
   ```

4. При необходимости задайте `WIREGUARD_HOST` и `WIREGUARD_PORT`. По умолчанию
   используется UDP-порт `51820` на хосте Docker. Для сервера на другом устройстве
   укажите его LAN-адрес, доступный из контейнера. `PINGGY_SERVER` по умолчанию
   равен `free.pinggy.io`, а `POLL_INTERVAL_SECONDS` равен `10`.
5. Запустите оба контейнера из каталога проекта.

   ```sh
   docker compose up -d --build
   docker compose logs -f keeper
   ```

После первого успешного ответа FreeDNS в журнале появится `FreeDNS AAAA` с
опубликованным значением. Его можно проверить командой `dig +short AAAA wg.example.com`,
подставив имя своей записи. Скорость обновления у клиента зависит от кэша DNS.

## WG Tunnel

В существующем `[Peer]` используйте домен своей AAAA-записи.

```ini
Endpoint = wg.example.com:0
```

WG Tunnel подставляет порт из IP4P. Включите **Dynamic DNS Recovery** в настройках
туннеля для повторного разрешения имени при потере связи. [Настройки WG Tunnel](https://wgtunnel.com/docs/common/tunnels/#dynamic-dns-recovery)

## Проверки

Для локальной проверки кодирования и смены endpoint запустите `node --test`
с Node.js 24. Тест подставляет ответ FreeDNS и использует локальный HTTP-сервер
вместо Pinggy.

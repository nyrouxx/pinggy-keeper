# pinggy-keeper

Python-сервис открывает UDP-туннель через официальный Pinggy SDK, читает
публичный адрес из `tunnel.urls` и публикует IPv4 и порт в AAAA-записи FreeDNS
в формате IP4P. [Pinggy Python SDK](https://github.com/Pinggy-io/sdk-python/blob/main/API_DOC.md) · [IP4P в WG Tunnel](https://www.wgtunnel.com/docs/guides/ip4p-self-hosting/)

Например, `203.0.113.8:51820` кодируется в `2001::ca6c:cb00:7108`.
При изменении только порта запись тоже обновится. После обрыва keeper открывает
туннель заново, а при временной ошибке FreeDNS повторяет обновление.

## Локальный порт

Запускайте Compose на том Linux-хосте, где доступен UDP-порт WireGuard.
`network_mode: host` даёт контейнеру доступ к `127.0.0.1` этого хоста. В `.env`
задайте только `WIREGUARD_PORT` для локального проброса, например
`WIREGUARD_PORT=51820`. Pinggy сам выдаёт публичный порт, который попадает в IP4P.
[Режим сети host в Docker](https://docs.docker.com/engine/network/drivers/host/)

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

4. Укажите ваш локальный `WIREGUARD_PORT`. По умолчанию используется `51820`.
   `PINGGY_SERVER` и `POLL_INTERVAL_SECONDS` задаются по необходимости, их значения
   по умолчанию `free.pinggy.io` и `10`.
5. Запустите контейнер из каталога проекта.

   ```sh
   docker compose up -d --build
   docker compose logs -f keeper
   ```

При переходе с предыдущей версии запустите `docker compose up -d --build --remove-orphans`
в том же Compose-проекте. Команда удалит прежний отдельный контейнер Pinggy.

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

Локальные тесты проверяют адрес назначения, кодирование IP4P, смену IPv4 и порта
и повтор после ошибки FreeDNS. Запуск с Python 3.13 и тестовыми ответами сервисов.

```sh
python -m unittest
```

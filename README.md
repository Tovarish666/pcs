# PCS — Proxy Control Service 5

Один софт для фермы мобильных прокси. Одна логика, одна версия зависимостей
(sing-box 1.14.2), одна схема маршрутизации.

| часть | что делает |
|---|---|
| **pcs** | хаб на хосте Proxmox: создаёт ВМ Ubuntu 24.04, ставит софт, DNS, доступы, обновления; меню в терминале и веб-панель на :666 |
| **proxyveth** | из прокси ген1 (`host:port:login:pass`) — сетевой интерфейс. Режим `usb` — модем выглядит как настоящий USB Huawei E3372h (до ~40 на сервер). Режим `gw` — шлюз без USB и без такого предела |
| **modlink** | из любого интерфейса — прокси (HTTP и SOCKS5 с логином), смена IP по ссылке и таймеру; у каждой строки галочка «защита» (ЛК операторов, опасные порты, веб модема); на хост ставится сам |
| **hivelink** | драйвер настоящих модемов Huawei/Vodafone; на хост ставится сам |

## Установка — хост Proxmox VE или Ubuntu 24.04+ (VPS)

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/Tovarish666/pcs/main/install.sh)
```

Появятся команда `pcs` (без аргументов — меню), веб-панель на порту 666 (логин и пароль
спросит установщик), а на хосте — hivelink и modlink: настоящие модемы, воткнутые в хост,
становятся прокси (`pcs modlink from-ifaces --add --host && pcs modlink apply --host`), а
из них — виртуальные модемы в ВМ этого же хоста (`modlink export --table`, docs/PCS.md §9.6).

На **Ubuntu (VPS)** тот же скрипт ставит модемы прямо на машину: спросит режим proxyveth
(`usb` — для mobileproxy.space) и нужен ли mp.space; ВМ там не создаются. Чтобы управлять
VPS с хоста Proxmox — там «+ Присоединить хост» (адрес, логин и пароль панели VPS) или
`pcs node add IP` ([docs/PCS.md §19](docs/PCS.md)).

## Первый сервер

```bash
pcs server create --mode usb --sheet 'https://docs.google.com/spreadsheets/d/ID/edit#gid=0' --mpspace
```

Таблица — колонки `n`, `real`, `proxy` (и по желанию `enabled`). Дальше: `pcs proxyveth status`,
`pcs modlink from-ifaces --add && pcs modlink apply`, `pcs` — меню.

## Документация

- [`docs/PCS.md`](docs/PCS.md) — полная: устройство, все команды, файлы, форматы, HTTP API панели,
  решения и грабли, состояние и следующий этап (связь хостов через API).
- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — короткий контракт между частями.

Проверки без root и без ВМ: `bash tests/run-all.sh`.

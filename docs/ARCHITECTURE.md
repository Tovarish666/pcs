# PCS 5 — устройство и договорённости

Это контракт для всех, кто пишет PCS. Здесь записано, кто за что отвечает и как части
говорят друг с другом. Если чего-то здесь нет — спросить ведущего, а не придумать
своё. Если нужно поменять договорённость — тоже через ведущего.

## 1. Кто есть кто

- **PCS (ProxyControlService)** — пульт и хаб. Живёт на хосте Proxmox. Создаёт ВМ
  Ubuntu 24.04 (серверы) и ставит на них всё нужное. Чинит DNS, держит доступы
  (пароль root, ключ SSH), показывает, что где работает. Два лица — терминал и
  веб-панель на порту 666.
- **proxyveth — из прокси сетевой интерфейс.** На сервере работает в одном из двух режимов:
  - `usb` — модем выглядит как настоящий USB Huawei E3372h (бывший vmodem); предел около 40 на сервер;
  - `gw` — шлюз без USB и без такого предела (бывший proxyveth-virt).

  Список модемов берёт из Google-таблицы и держит её локальную копию.
- **modlink — из сетевого интерфейса прокси.** Любой интерфейс (настоящий модем или
  proxyveth) становится прокси с логином, паролем и портом. Смена IP через HiLink — по
  кнопке, по ссылке-триггеру или по таймеру.
- **hivelink — драйвер настоящих модемов.** Доводит их до рабочего состояния и чинит
  после сбоев. На хост ставится автоматически, в ВМ — по запросу.

Старые имена (vmodem, proxyveth-virt, e3372-*) уходят. Алиасов не делаем: они путают.

**Отложено — не тянуть в работу:**
- PCS прямо на VPS с Ubuntu;
- связка хостов между собой;
- yaspeed;
- оповещения в Telegram (код может остаться, в меню и панели их нет).

## 2. Словарь

| слово | значение |
|---|---|
| хост | машина с Proxmox (Debian), где живёт хаб |
| сервер | ВМ Ubuntu 24.04 под модемы (одна ВМ = один сервер) |
| модем N | номер 1–254; модем на 192.168.N.1, сервер получает 192.168.N.100 |
| real | октет настоящего модема за прокси ген1 (192.168.real.1 у его мини-сервера) |
| прокси ген1 | `host:port:login:pass`, SOCKS5 от настоящего модема |
| mp.space | mobileproxy.space, агрегатор; его софт стоит на сервере |

## 3. Репозиторий и владельцы

| путь | что | владелец |
|---|---|---|
| `docs/ARCHITECTURE.md`, `pcs/core/{util,singbox,net,table}.py`, `pcs/__init__.py`, `bin/*`, `tests/run-all.sh`, `tests/singbox_check.py`, `tests/test_core.py` | ядро и каркас | ведущий |
| `pcs/hub/**`, `pcs/core/dns.py`, `bin/pcs-node`, `install.sh`, `tests/test_hub*.py`, `tests/test_dns.py`, `tests/test_pcs.py` | хаб, меню, DNS, установка на хост, служебная команда хаба на сервере | A |
| `pcs/proxyveth/{cli,usb,modem_web}.py`, `tests/test_proxyveth_usb*.py`, `tests/test_agent.py`, `tests/test_api.py` | proxyveth: команда, режим usb, веб-морда модема | B1 |
| `pcs/proxyveth/gw.py`, `tests/test_proxyveth_gw*.py` | proxyveth: режим gw | B2 |
| `pcs/modlink/**`, `tests/test_modlink*.py` | modlink | C |
| `pcs/hivelink/**`, `tests/test_hivelink*.py` | hivelink | D |
| `pcs/web/**`, `tests/test_web*.py` | веб-панель | E |

`README.md` переписывает ведущий в конце — части его не трогают.

Файлы чужой части не правим. Нужна правка ядра или чужой части — пишем в отчёт
ведущему, что и зачем.

## 4. Где что лежит на машинах

Код один и тот же на хосте и на серверах, одной версии: `pcs/__init__.py: VERSION`.

| | хост Proxmox | сервер (ВМ Ubuntu) |
|---|---|---|
| код | `/opt/pcs` (дерево репозитория) | `/opt/pcs` (то же дерево; кладёт хаб) |
| команды | `/usr/local/bin/{pcs,hivelink}` → `/opt/pcs/bin/…` | `/usr/local/bin/{proxyveth,modlink,hivelink}` → `/opt/pcs/bin/…` |
| sing-box | — | `/usr/local/lib/pcs/sing-box` (см. §5) |
| настройки | `/etc/pcs` (700): `servers/<id>.json`, `ssh/`, `web.json`, `active` | `/etc/proxyveth`, `/etc/modlink`, `/etc/hivelink` (700) |
| состояние | `/var/lib/pcs` | `/var/lib/<часть>`, `/run/<часть>` |
| журнал | journald + `/var/log/pcs` | journald + `/var/log/<часть>` |
| юниты | `pcs-web.service`, `hivelink*.service` | `proxyveth-*`, `modlink*`, `hivelink*` |

- Юниты, файлы, udev-правила и netns каждой части начинаются с имени части:
  `proxyveth-…`, `modlink-…`, `hivelink-…`, `pcs-…`.
- Секреты (пароли прокси, auth.mp, пароль панели) — в файлах 600, никогда в командной строке.
- Python 3 и только стандартная библиотека. На серверах это Ubuntu 24.04 (3.12), на
  хосте Debian 12/13 (3.11/3.13): код хаба, панели и hivelink должен работать на 3.11.

## 5. sing-box — один на всех

- Версия **1.14.2** ставится в `/usr/local/lib/pcs/sing-box` с проверкой суммы (`pcs.core.singbox`).
- `/usr/local/bin/sing-box` чужой. Его не читаем, не ставим и не удаляем.
- Конфиги — **только в формате 1.12+**. Старый 1.14 не принимает вовсе:
  - DNS-серверы: `{"type": "tcp", "server": …, "detour": …}`, а не `"address": "tcp://…"`;
  - никаких служебных outbound `dns`/`block`: вместо них route-действия
    `{"action": "hijack-dns"}`, `{"action": "reject"}`;
  - никаких `sniff`/`sniff_override_destination` на inbound: если нужно, правило
    `{"action": "sniff"}`; `domain_strategy` в dial-полях — устарел.
- **Каждый генератор конфига покрыт тестом с настоящим `sing-box check`**
  (`tests/singbox_check.py: sb_check(config)`). Предупреждение `deprecated` — тоже провал.
- sing-box и всё долгоживущее запускаются **своими юнитами** (шаблонными `…@N`), а не
  дочерними процессами oneshot-юнитов: иначе systemd убьёт их вместе с родителем.
- sing-box в netns — без доступа к D-Bus (`InaccessiblePaths=-/run/dbus/system_bus_socket`).
  Иначе он прописывает DNS своего tun в resolved на интерфейс сервера с тем же номером.

## 6. Команды

### Общее для всех частей

- Без аргументов или с `help` — короткая справка. Ошибка — одна строка `✗ …` в stderr,
  код 1. Без трасс Python.
- `--json` у всех читающих команд и у действий. Конверт строго такой (`pcs.core.util.run_cli`):
  `{"ok": true, "data": …}` или `{"ok": false, "error": "текст для человека"}`. Код 0 или 1.
  Панель и хаб читают только `--json`.
- Деструктивное (снести модемы, сменить режим, удалить ВМ) — с `--yes` в скриптах.
  В меню и панели — подтверждение вводом номера сервера.
- Одна правка за раз: `pcs.core.util.lock(/run/<часть>/lock)`.

### Хаб (хост): `pcs`

```
pcs                                   меню
pcs status [--json]                   хост и все серверы сразу (для приветственной страницы)
pcs server list | info [ID] | use ID  серверы; use — выбрать текущий
pcs server create [параметры]         ВМ Ubuntu 24.04 под ключ (+ --mode usb|gw, --sheet, --mpspace)
pcs server adopt ID | delete ID       взять существующую ВМ / удалить (подтверждение)
pcs dns     [--server ID|--host] [--dns "1.1.1.1 8.8.8.8"]
pcs passwd  [--server ID]             пароль root
pcs key     [--server ID] [--add PUBKEY | --rotate]
pcs ssh [ID] | pcs exec [ID] 'cmd'
pcs mpspace install|auth|check [--server ID]
pcs proxyveth …  [--server ID]        запустить proxyveth на сервере (аргументы — как есть)
pcs modlink …    [--server ID]
pcs hivelink …   [--server ID|--host]
pcs update [--server ID|--all]        одна версия кода на хосте и серверах
pcs web on|off|status|passwd          веб-панель :666
pcs version
```

### Сервер: `proxyveth`

```
proxyveth mode [usb|gw] [--yes]       режим сервера; смена — снять всё своё и поднять в новом
proxyveth source [URL|local]          источник таблицы; local — главной становится локальная копия
proxyveth table [show|pull|edit]      показать / подтянуть из Google / открыть копию в $EDITOR
proxyveth lint                        проверить таблицу, ничего не трогая
proxyveth sync [--force]              привести модемы к таблице (таймер делает это сам)
proxyveth up|down|restart N|all
proxyveth status [--wan]              состояние (с --wan — внешний IP через каждый модем)
proxyveth problems                    только проблемные
proxyveth diag N                      прокси → логин → модем → SIM → интернет
proxyveth rotate N | reboot N         смена IP / перезагрузка настоящего модема (в меню не выносим)
proxyveth doctor                      окружение
proxyveth setup                       подготовить сервер (зовёт хаб при create/update)
proxyveth version
```

**Режимы — один интерфейс.** `cli.py` (B1) читает режим из `/etc/proxyveth/config.json`
(`"mode": "usb"|"gw"`) и зовёт модуль режима. И `usb.py`, и `gw.py` предоставляют
одинаковые функции:

```python
setup(cfg) -> None                        # подготовить систему под режим (идемпотентно)
apply(desired, cfg, force=False) -> dict  # привести модемы к таблице: {"created": […], "recreated": […], "removed": […], "failed": {n: текст}}
up(n=None) / down(n=None) -> None         # None — все
status(wan=False) -> list[Row]
diag(n) -> dict                           # {"n", "steps": [{"name", "ok", "text"}], "verdict"}
teardown() -> None                        # снять всё своё (для смены режима и uninstall)
doctor() -> list[{"name", "ok", "text"}]
```

`Row` — одинаковый для обоих режимов. Его показывают панель и меню:

```json
{"n": 201, "real": 81, "proxy": "188.134.95.184:1198", "state": "ok|warn|broken|absent|disabled|rebooting",
 "problems": ["…"], "iface": "eth1", "ext_ip": "94.25.229.40", "since": 1760000000}
```

`state`:
- `ok` — работает;
- `warn` — наша сторона цела, сломан апстрим (прокси, SIM, нет интернета);
- `broken` — сломана наша сторона;
- `absent` — модема нет;
- `disabled` — выключен в таблице;
- `rebooting` — перезагружается.

### Сервер: `modlink`

```
modlink list
modlink add --lan-ip IP [--name …] [--login …] [--password …|gen] [--port N|auto]
            [--modem-ip IP] [--reconnect-port N|auto] [--interval MIN]
modlink set ID поле=значение …        поля — как в таблице ниже
modlink del ID | enable ID | disable ID
modlink apply                         конфиг sing-box → sing-box check → перезапуск
modlink status
modlink test ID                       внешний IP через этот прокси + доступность HiLink
modlink reconnect ID | reboot ID | log ID
modlink export                        строки для клиента: IP:PORT:LOGIN:PASS<TAB>http://IP:RPORT/reconnect
modlink from-ifaces                   предложить строки по интерфейсам 192.168.N.100 сервера
```

### Хост или сервер: `hivelink`

```
hivelink install | uninstall
hivelink status [--json] | doctor
hivelink reconnect N | reset N | recfg N | dataon N|--all
```

## 7. Сеть

- **Номер модема N** — 1–254. Адреса: 192.168.N.0/24, модем — .1, сервер — .100.
  Октет, занятый сетью самой машины, — брак (`pcs.core.net.local_octets`).
- **Таблицы и приоритеты** — только из `pcs.core.net`:

| часть | таблицы | приоритет ip rule | пометка iptables |
|---|---|---|---|
| proxyveth usb, своя сторона сервера | `100 + N % 200` | авто (как mp.space) | — |
| proxyveth gw | `1000 + N` | `30000 + N` | `pcs:proxyveth` |
| hivelink | `2000 + N` | `31000 + N` | `pcs:hivelink` |
| ядро: адреса прокси → основной канал | `90` | `32765` | — |

  У proxyveth usb N и N+200 делят одну таблицу (так нумерует и mp.space). У
  `pcs.core.table.lint` для этого режима `mp_tables=True`, для gw — `False`.
- **iptables** — только с `-m comment --comment pcs:<часть>`; уборка снимает только помеченное своей частью.
- **Чужое не трогаем:**
  - правила, таблицы и маршруты других частей и mp.space;
  - устройства на шине `dummy_hcd` для hivelink: это модемы proxyveth usb
    (`readlink -f /sys/class/net/X/device` содержит `/dummy_hcd.`);
  - интерфейс хоста с адресом 192.168.X.100 — не модем, если он не USB-модем.
- **База машины — `pcs.core.net.ensure_base()`**, её зовёт `setup` каждой части:
  - один файл `/etc/sysctl.d/90-pcs.conf` (`kernel.panic=10`, `kernel.panic_on_oops=1`,
    `ip_forward=1`); rp_filter части выставляют на своих интерфейсах;
  - drop-in `/etc/systemd/networkd.conf.d/10-pcs.conf` с
    `ManageForeignRoutingPolicyRules=no`, `ManageForeignRoutes=no` — иначе любой
    `netplan apply` стирает маршрутизацию модемов.
- **Адреса прокси** всегда идут через основной канал (`pcs.core.net.pin_proxies`). Это
  зовёт proxyveth при каждом sync. Без этого udhcpc из скрипта mp.space на пару секунд
  уводит маршрут по умолчанию в модем, и соединения к прокси размножаются петлёй.
- **Имена:**

| | proxyveth usb | proxyveth gw |
|---|---|---|
| netns модема | `pvN` | `pvN` |
| сторона сервера | `ethN` (ядро, cdc_ether) | veth `pvN` |
| внутри netns | `usb0`, tun `pvtun0` | `eth0`, tun `pvtun0` |
| юниты | `proxyveth-{dns,px,sb,web}@N` | `proxyveth-gw@N` (sing-box), `proxyveth-hostfix@N` |
| общее | `proxyveth-sync.timer` | то же |

## 8. Данные

### Таблица модемов proxyveth

Колонки `n`, `real`, `proxy` (или `host`/`port`/`login`/`password`) и `enabled`. Разбор —
`pcs.core.table.lint`, чтение — `pcs.core.table.fetch(src, /etc/proxyveth/table.csv)`.
- Источник — ссылка на Google-таблицу. После каждого удачного чтения кладётся
  локальная копия `/etc/proxyveth/table.csv`.
- Если Google недоступен, берётся копия, с предупреждением.
- `proxyveth source local` делает главной копию (её правят панель и `table edit`).
- Защита от ошибки в таблице: если таблица требует снести больше половины модемов,
  без `--force` ничего не сносится.

### Таблица modlink — `/etc/modlink/proxies.json`

| поле | что | куда уходит |
|---|---|---|
| `id` | постоянный номер строки | — |
| `enabled` | строка активна | выключенные не попадают в конфиг |
| `name` | метка | только интерфейс |
| `login`, `password` | доступ к прокси | `users` mixed-inbound |
| `port` | порт прокси, **постоянный** (не от позиции в списке) | `listen_port` |
| `lan_ip` | адрес интерфейса модема на сервере | `inet4_bind_address` outbound: именно это привязывает трафик к модему |
| `modem_ip` | веб-интерфейс Huawei | HiLink API: реконнект, ребут, проверка |
| `reconnect_port` | порт триггера | `GET http://<ip>:<порт>/reconnect` |
| `interval_min` | автореконнект, минут (0 — выкл) | таймер в демоне modlink |

Устройство modlink:
- sing-box (`modlink-sb.service`): на каждую строку mixed-inbound `0.0.0.0:port` с
  `users` и direct-outbound с `inet4_bind_address = lan_ip`. Правило «inbound → свой
  outbound», остальное — `reject`.
- Демон `modlink.service`: триггеры реконнекта, таймеры, клиент HiLink, журнал реконнектов.

Пароли в `export` и в панели видны только по явному запросу.

## 9. Хаб ↔ серверы

- Хаб ходит на сервер по SSH ключом `/etc/pcs/ssh/id_ed25519`, со своим known_hosts. На
  сервере вызывает команды частей с `--json`.
- Своё на сервере (сводка для хаба, DNS-фикс, пароль, ключ) хаб делает через
  служебную команду `pcs-node` (A): `pcs-node info --json` — версия, режим proxyveth,
  сводка модемов, mp.space; `pcs-node dns-fix`, `pcs-node passwd`, `pcs-node key`.
- Долгие операции (создать ВМ, поставить mp.space, обновить) — фоновые задания хаба:
  `pcs.hub.jobs`. Есть журнал и состояние; панель их опрашивает.
- **API хаба для панели** — `pcs/hub/api.py` (пишет A, использует E). Все функции
  возвращают `dict`/`list`, исключения — `pcs.core.util.Fail`:

```python
host_info()                         # {"name", "kind": "proxmox", "version", "cpu", "ram", "disk", "hivelink": {...}}
servers()                           # [{"id", "name", "ip", "state", "mode", "modems": {"ok","warn","broken","total"}, "mpspace"}]
server(id)                          # то же + версии частей
create_server(params) -> job_id     # params: name, cores, ram_gb, disk_gb, mode, sheet, mpspace(bool)
delete_server(id, confirm)          # confirm должен совпасть с id
run(id, part, args) -> dict         # конверт {"ok", "data"|"error"} от `<part> <args> --json` на сервере; id=None — хост
dns_fix(target, dns=None) -> dict   # target: id сервера или "host"
set_password(target, password) -> dict
set_key(target, pubkey=None, rotate=False) -> dict
mpspace(target, action, **kw) -> job_id | dict
job(job_id)                         # {"state": "running|done|failed", "log": [...], "result": ...}
term_argv(target) -> list           # argv для pty терминала: ssh -tt … или bash -l для хоста
```

## 10. Веб-панель

- HTTP на **порту 666**: `pcs-web.service`, `pcs web on|off`. Порт открывает и закрывает
  сам пользователь. Порт 5000 на ВМ (старый modlink) не трогаем.
- **Вход:**
  - логин и пароль задаются при установке хаба (`install.sh` спрашивает), сменить — `pcs web passwd`;
  - хранятся в `/etc/pcs/web.json` как PBKDF2-SHA256 с солью;
  - сессия — cookie `HttpOnly; SameSite=Strict`;
  - каждый POST — с заголовком `X-CSRF` из `/api/session`;
  - никакого CORS; пауза после неудачного входа.
- **Техника:** `http.server.ThreadingHTTPServer`, статика в `pcs/web/static/` — чистые
  HTML, CSS и JS без сборки. Внешних CDN нет: xterm.js лежит в репозитории
  (MIT, с файлом лицензии).
- **Устройство страниц** (как на согласованном макете):
  - слева дерево: хост → его ВМ (точка состояния, режим, `ok/total`), внизу «Создать ВМ»;
  - приветственная страница хоста:
    - имя хоста, сводка по ВМ и модемам;
    - список ВМ с кнопками «Открыть», «Терминал», «Удалить»;
    - действия хоста: DNS, пароль root или ключ SSH, hivelink (ставится сам, показываем состояние), обновить PCS;
  - страница ВМ: сверху крупная цветная плашка с выбранной ВМ (имя, IP, режим,
    модемы, mp.space) — чтобы невозможно было перепутать сервер. Вкладки:
    1. **proxyveth** — режим, источник таблицы, синхронизация, таблица модемов (`Row`),
       правка локальной копии;
    2. **modlink** — таблица из §8 с кнопками Test, ⟳ реконнект, ↻ ребут, ≡ лог, ✕; добавить, применить, экспорт;
    3. **терминал** — настоящая консоль сервера (xterm.js ↔ WebSocket ↔ pty `ssh -tt`) и кнопки частых команд (`proxyveth status --wan`, `proxyveth problems`, `modlink status`, `hivelink status`);
    4. **настройка и управление** — всё, где достаточно ответа «применено / сохранено / ошибка»: DNS, пароль, ключ, mp.space, hivelink в ВМ, смена режима proxyveth (подтверждение номером), обновление.

## 11. Меню в терминале (`pcs` без аргументов)

- Наверху всегда **рамка выбранного сервера** в несколько строк: имя, IP, хост или ВМ,
  режим proxyveth, модемы `ok/total`, mp.space. Цветная и не теряется на экране.
- Строка ввода содержит сервер: `[2000 pcs2000 192.168.88.7] »`. Сменить сервер — клавиша `v`.
- **Разделы:** Серверы / Модемы (proxyveth) / Прокси (modlink) / Софт (mp.space, hivelink) /
  Сеть и доступ (DNS, пароль, ключ, SSH) / Диагностика. «0 — назад» на каждом экране.
- Опасное — с подтверждением номером сервера. Неверный ввод не роняет меню.
- Нет пунктов «сменить IP», «перезагрузить модем», Telegram.

## 12. DNS-фикс (`pcs/core/dns.py`, A) — по исходному ProxyControlService

Один и тот же код для хоста и для серверов.

0. Снять `chattr -i` с `/etc/resolv.conf`.
1. `/etc/systemd/resolved.conf.d/99-pcs.conf`: `DNS=…`, `FallbackDNS=9.9.9.9 1.0.0.1`,
   `Domains=~.`, `DNSStubListener=yes`, `DNSSEC=no`, `DNSOverTLS=no`, `Cache=yes`.
2. Отдельный `/etc/netplan/99-pcs-dns.yaml` (600) — только для интерфейсов с DHCP:
   `dhcp4-overrides`/`dhcp6-overrides: {use-dns: false, use-domains: false}`.
   `50-cloud-init.yaml` не трогаем.
3. `netplan generate`, при ошибке — откат drop-in. `netplan apply` — **только если файл
   изменился** и только после того, как стоит networkd drop-in из §7.
4. `/etc/resolv.conf` → симлинк на `stub-resolv.conf`, перезапуск resolved, `flush-caches`.
5. Проверка: `getent ahostsv4` github.com, docs.google.com, mobileproxy.space и что
   на интерфейсах нет чужого DNS. Иначе — ошибка с выводом `resolvectl`.
6. Сверх старого:
   - oneshot `pcs-dns.service` при загрузке;
   - снятие чужого `172.20.0.2` (след sing-box);
   - повтор после установки mp.space.

## 13. Миграции — без потерь и без чужого

- **B1, vmodem 4.x → proxyveth usb:**
  - что было: юниты `vmodem-*`, `/etc/vmodem`, netns `vmN`, `/usr/local/lib/vmodem/sing-box`;
  - `proxyveth setup` берёт источник таблицы и настройки из `/etc/vmodem/config.json`;
  - старые модемы и юниты снимает, создаёт по-новому, старое удаляет.
- **B2, proxyveth-virt 3.4 → proxyveth gw:**
  - что было: `/etc/proxyveth/env`, `config.json`, юниты `proxyveth*.service`, netns `ns_N`,
    veth `mdmN`, таблицы `1000+N` без приоритета, MASQUERADE без пометок;
  - источник берётся из `env`; снимается только узнаваемо своё.
  - Каталог `/etc/proxyveth` тот же — осторожно с файлами.
- **C, modlink-linux → modlink:**
  - что было: `modems.conf` и `server.json`; порты `base+2i`, триггеры `base+2i+1`;
  - миграция — **только явной командой** `modlink migrate`, порты и пароли сохраняются;
  - старую панель (`modlink-panel.service`, порт 5000) без просьбы не останавливаем;
  - `/usr/local/bin/sing-box` не трогаем.
- **D, e3372/hivelink:** если стоит старый — снять своё (по своим путям) и поставить заново.

## 14. Готовность части

- Тесты `tests/test_<часть>*.py` в общем стиле: `check(name, cond)`, в конце
  `итого: ok N, fail M`, код 0/1. Без root и без сети, кроме скачивания sing-box в `sb_check`.
- Каждый генератор конфига sing-box проходит `sb_check`.
- `bash tests/run-all.sh` зелёный для всего репозитория.
- Коммиты — в своей ветке и своей копии (worktree). Не пушить, не вливать.
- **На серверы и стенд не ходить** — ни SSH, ни curl на 88.87.69.240 и 192.168.88.0/24.
  Проверку на стенде делает ведущий.
- Итоговый отчёт:
  - что сделано, что нет, какие договорённости пришлось трактовать;
  - нужны ли правки ядра или чужих частей;
  - **точные команды проверки на стенде** для ведущего.

## 15. Стиль

- Интерфейс и сообщения — по-русски, коротко, без «успешно» и «пожалуйста».
- Ошибка = что случилось + что делать.
- Комментарии редкие, только про «зачем» (как в нынешнем коде).
- Имена функций — короткие, по делу.

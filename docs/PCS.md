# PCS 5 — полная документация

Документ описывает PCS 5 целиком: зачем он, как устроен, все команды, файлы, форматы,
HTTP API, решения и грабли. Его можно отдать новому чату или новому разработчику, и тот
войдёт в контекст без пересказа.

Короткий контракт между частями — `docs/ARCHITECTURE.md`. Этот файл шире и подробнее.

---

## 0. Коротко — для нового чата

- **Что это.** Софт для фермы мобильных прокси. Купленная прокси «ген1»
  (`host:port:login:pass` — SOCKS5 от настоящего модема) превращается в
  **виртуальный модем**. Агрегатор mobileproxy.space (mp.space) или свой софт видит его
  как настоящий USB-модем Huawei E3372h HiLink. Обратное направление — из любого
  интерфейса сделать прокси с логином и паролем для клиента.
- **Где живёт.** Хаб `pcs` стоит на хосте Proxmox VE (Debian) и создаёт ВМ Ubuntu 24.04
  — «серверы». На серверах работают части:
  - `proxyveth` — прокси → интерфейс;
  - `modlink` — интерфейс → прокси;
  - `hivelink` — драйвер настоящих модемов (на хосте стоит всегда, в ВМ — по запросу).
- **Как управлять.** Терминал (команды и меню `pcs`) и веб-панель на порту 666.
- **Состояние на 08.10.2026.** Всё собрано в ветке `unify`, PR #3 в `main`.
  - Проверено на стенде: установка на хост, создание ВМ в обоих режимах, переезд со
    старого vmodem 4.1, трафик, DNS, modlink, hivelink, вход в панель и её API.
  - Тесты: около 980 проверок, все зелёные.
- **Сейчас всё рассчитано на один хост Proxmox.** Следующий этап — **связать несколько
  хостов через API** (см. §19). HTTP API панели уже есть (§13), но рассчитан на
  браузер: вход по паролю, cookie, CSRF. Для связи машина-машина нужны токены (§19).
- **Отложено по решению пользователя:** PCS прямо на VPS с Ubuntu (без Proxmox), yaspeed
  (спидтест), оповещения в Telegram (код есть, в меню и панели нет).

---

## 1. Легенда

Всё началось с ProxyControlService — bash-скрипта. Он поднимал на Proxmox ВМ под
mobileproxy.space и превращал купленные прокси в «модемы», которые агрегатор принимал за
настоящие.

Ферма росла, и вокруг появлялись инструменты, каждый под свою задачу:
- proxyveth делал из прокси сетевые интерфейсы;
- proxyveth-virt развязал виртуальный и реальный номер модема;
- vmodem маскировал интерфейсы под USB-модемы Huawei;
- modlink раздавал прокси с модемов;
- hivelink (e3372-driver) оживлял настоящие модемы.

Каждый жил отдельно — свой установщик, свои версии sing-box — и иногда они ломали друг
друга.

PCS 5 собрал их в одну семью с одним пультом, одной версией зависимостей и одной схемой
маршрутизации:

| часть | одной фразой |
|---|---|
| **pcs** | пульт и хаб: создать сервер, поставить софт, DNS, доступы, обновить, показать состояние |
| **proxyveth** | из прокси — сетевой интерфейс; режимы `usb` (как USB-модем, до ~40 на сервер) и `gw` (шлюз без USB, без такого предела) |
| **modlink** | из интерфейса — прокси (HTTP и SOCKS5 с логином), смена IP по ссылке и таймеру |
| **hivelink** | драйвер настоящих модемов Huawei/Vodafone |

Два пути трафика:
- прокси ген1 → proxyveth → интерфейс 192.168.N.100 → mp.space или modlink → клиент;
- настоящий модем → hivelink → интерфейс → modlink или mp.space → клиент.

---

## 2. Термины

| слово | значение |
|---|---|
| хост | машина с Proxmox VE (Debian 12/13), где живёт хаб `pcs` |
| сервер | ВМ Ubuntu 24.04 под модемы. Одна ВМ — один сервер. В API — номер ВМ (`id`) |
| `host` | в API и командах — сам хост, в отличие от сервера |
| модем N | номер 1–254. Модем — 192.168.N.1, сервер получает 192.168.N.100 |
| real | октет настоящего модема за прокси (192.168.real.1 у его мини-сервера). Пусто — real = n |
| прокси ген1 | `host:port:login:pass`, SOCKS5 к сети настоящего модема (за ней 192.168.real.1) |
| ген2, ген3 | прокси для клиентов и агрегаторов (на балансировщике) и прокси, выдаваемая клиенту. Не часть PCS |
| mp.space | mobileproxy.space — агрегатор; его софт ставится на сервер (`pcs mpspace install`) |
| режим usb | proxyveth: модем — USB-гаджет (бывший vmodem) |
| режим gw | proxyveth: модем — veth-шлюз без USB (бывший proxyveth-virt) |
| HiLink | веб-API настоящего модема Huawei на 192.168.x.1 |
| часть | proxyveth, modlink или hivelink — то, что работает на сервере (или хосте) |

---

## 3. Общая схема

```
Google-таблица: n, real, proxy          хост Proxmox (Debian)
        │                               ├─ pcs (хаб), меню, /etc/pcs
        │                               ├─ веб-панель :666 (pcs-web.service)
        │                               ├─ hivelink (настоящие модемы на USB хоста)
        ▼                               └─ ВМ ───────────────────────────────┐
ВМ Ubuntu 24.04 (сервер)                                                      │
 ├─ proxyveth   режим usb:  netns pvN ─ usb0 192.168.N.1 ─ USB-гаджет «Huawei E3372h»
 │                          (dnsmasq DHCP .100, DNS, sing-box tun → сокет → SOCKS5 ген1)
 │              режим gw:   netns pvN ─ veth pvN 192.168.N.100 ↔ eth0 .254 (sing-box tun → SOCKS5)
 ├─ сторона сервера: ethN (usb) или pvN (gw) с адресом 192.168.N.100
 │   └─ mobileproxy.space / свой DHCP и маршруты
 ├─ modlink     0.0.0.0:порт (HTTP+SOCKS5, логин) → выход с адреса lan_ip (любого интерфейса)
 └─ hivelink    по запросу (настоящие модемы, проброшенные в ВМ)
```

---

## 4. Репозиторий

```
install.sh                  установка и обновление хаба на хост Proxmox
bin/                        входные команды (тонкие): pcs, pcs-node, proxyveth, modlink, hivelink
pcs/__init__.py             VERSION — одна на весь код
pcs/core/                   общее ядро
  util.py                   sh/rd/wr, Fail/Busy, Log, lock, run_cli (конверт --json)
  singbox.py                sing-box 1.14.2: путь, суммы, install(), check()
  net.py                    таблицы и приоритеты частей, uplink_route, pin_proxies, ensure_base
  table.py                  таблица модемов: normalize_url, fetch (Google + копия), lint
  dns.py                    DNS-фикс (хост и сервер)
pcs/hub/                    хаб на хосте
  cli.py                    команда pcs
  menu.py, ui.py            меню в терминале, оформление
  servers.py, pve.py        создание/взятие/удаление ВМ, cloud-init, qm
  remote.py                 SSH к серверам, доставка кода, вызов частей с --json
  store.py                  /etc/pcs: серверы, выбранный сервер
  api.py, jobs.py           API для панели, фоновые задания
  node.py                   pcs-node — служебная команда хаба на сервере
  ops.py, web.py            DNS/пароль/ключ/mp.space; юнит и вход панели
pcs/proxyveth/
  cli.py                    команда proxyveth (общая для режимов)
  usb.py                    режим usb (бывший vmodem)
  modem_web.py              веб-морда модема в режиме usb (бывший vmodem-api)
  gw.py, gw_*.py            режим gw (бывший proxyveth-virt): модуль, посредник Host, апстрим, перенос
pcs/modlink/                cli, store, ops, daemon, hilink, system
pcs/hivelink/               cli, driver, usb, route, hilink, rndis (DKMS), install, status, files/
pcs/web/                    server (HTTP, вход, WebSocket), panel (JSON API), auth, ws, fake_api, static/
docs/ARCHITECTURE.md        контракт частей
docs/PCS.md                 этот документ
tests/                      run-all.sh, singbox_check.py, test_*.py
```

**Тесты.**
- `bash tests/run-all.sh` — без root, без ВМ, без сети. Исключение — однократное
  скачивание sing-box для `sing-box check`; без сети эти проверки пропускаются.
- Каждый `tests/test_*.py` печатает «итого: ok N, fail M» и возвращает код 0/1.
- Конфиги sing-box проверяются настоящим `sing-box check` той же версии, что ставится
  на серверы (`tests/singbox_check.py`). Предупреждение `deprecated` — тоже провал.
- `tests/stand/` — сценарии для живого сервера: нагрузка, отказы, стратегии создания.
  Остались от PCS 4 и местами ссылаются на старые имена.

**Стиль.**
- Только стандартная библиотека Python: 3.12 на серверах, 3.11+ на хосте.
- Сообщения — по-русски, коротко: что случилось и что делать.
- Ошибка — одна строка `✗ …` в stderr и код 1, без трасс.

---

## 5. Установка и обновление

### Хост

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/Tovarish666/pcs/main/install.sh)
```

**Требования:** root, Proxmox VE (`qm`, `pvesm`, `/etc/pve`), Python 3.11+. На не-Proxmox —
отказ: режим VPS отложен.

**Что делает `install.sh`:**
1. Скачивает `PCS_REPO@PCS_BRANCH` (по умолчанию `Tovarish666/pcs@main`) или берёт
   готовое дерево из `PCS_SRC=/путь`.
2. Проверяет, что код компилируется. Кладёт его в `/opt/pcs`, прежний — в `/opt/pcs.prev`
   (откат — переименовать обратно). Пишет `/opt/pcs/.source` (`PCS_REPO`, `PCS_BRANCH`):
   отсюда же берёт код `pcs update`.
3. Ставит команды `/usr/local/bin/pcs` и `/usr/local/bin/hivelink`.
4. Спрашивает логин и пароль панели (или берёт `PCS_WEB_USER`, `PCS_WEB_PASS`), сохраняет
   хэшем и включает `pcs-web.service` на порту 666. Если вход уже задан — не спрашивает.
5. Ставит hivelink на хост (`hivelink install`). Первая установка тяжёлая: ставятся
   `proxmox-headers-*`, `build-essential`, `dkms`, собирается модуль — несколько минут.

### Серверы

- Код на сервер кладёт хаб: `/opt/pcs` (tar без `.git` и тестов) и ссылки
  `/usr/local/bin/{proxyveth,modlink,hivelink,pcs-node}`.
- `pcs update`:
  - без флагов — хост из GitHub, потом все серверы;
  - `--host` — только хост;
  - `--server ID` — только этот сервер;
  - `--all` — все серверы.
- На сервере после доставки кода:
  - `proxyveth setup` — без выбранного режима ничего не делает и только подсказывает;
    со старым vmodem 4.x сам переезжает;
  - затем `modlink setup`.
- **Версия кода одна** на хосте и всех серверах: `pcs/__init__.py: VERSION`.
  Сейчас `5.0.0-dev`.

---

## 6. Хаб `pcs` (хост)

### Команды

```
pcs                                   меню
pcs status [--json]                   хост и все серверы сразу
pcs server list | info [ID] [--password] | use ID|host
pcs server create [--mode usb|gw] [--sheet URL] [--mpspace] [--id N] [--name …]
                  [--ip A.B.C.D/M --gw …|--dhcp] [--cores N] [--ram ГБ] [--disk ГБ]
                  [--storage …] [--bridge vmbr0] [--dns "…"] [--replace] [--yes]
pcs server adopt ID [--ip IP] | delete ID [--yes]
pcs proxyveth …  [--server ID]        команда proxyveth на сервере (аргументы — как есть)
pcs modlink …    [--server ID]
pcs hivelink …   [--server ID|--host]
pcs dns     [--server ID|--host] [--dns "1.1.1.1 8.8.8.8"]
pcs passwd  [--server ID|--host]      пароль root (Enter — сгенерировать)
pcs key     [--server ID|--host] [--add PUBKEY | --rotate]
pcs ssh [ID] | pcs exec [ID] 'cmd'    консоль / команда на сервере
pcs ssh-port PORT [--server ID]       порт SSH сервера (на 24.04 — через ssh.socket)
pcs mpspace install|auth|check [--server ID]
pcs update [--host | --server ID | --all]
pcs web on|off|status|passwd|serve    веб-панель :666
pcs doctor [--server ID|--host] | pcs job [ID] | pcs log | pcs version
```

- Без `--server` команда идёт на **выбранный сервер** (`pcs server use ID`; хранится в
  `/etc/pcs/active`).
- `--json` — ответ для программ (конверт, §7).
- **Секреты — не в командной строке:**
  - пароль root — `PCS_PASSWORD` или вопрос;
  - auth.mp — `PCS_MP_AUTH` или stdin;
  - HTTP-прокси для зеркал mp.space — `PCS_MP_PROXY`.

### Хранилище хаба — `/etc/pcs` (700)

| файл | что |
|---|---|
| `servers/<id>.json` (600) | сервер: `id`, `name`, `ip`, `net` (dhcp/static), `mode`, `created`, `pcs` (версия), `version`, порт SSH и др. |
| `active` | номер выбранного сервера (или `host`) |
| `ssh/id_ed25519`, `ssh/known_hosts` | ключ PCS для root на серверах, свой known_hosts (сервер пересоздали — новый ключ хоста) |
| `web.json` (600) | вход в панель (§13) |
| `/var/lib/pcs/jobs/` | фоновые задания (§14) |
| `/var/log/pcs/` | журналы хаба |

### Создание сервера (`pcs server create`)

1. Образ Ubuntu 24.04 (cloud image) в `/var/lib/vz/template/iso/ubuntu-24.04-noble.img`.
   Сумма сверяется; если upstream обновил образ — скачивается заново.
2. `qm create`:
   - q35, `cpu host`, balloon 0, `onboot 1`;
   - virtio-scsi-single, диск с discard/ssd/iothread;
   - qemu-guest-agent, `serial0 socket`;
   - `tablet 0` — без «QEMU Tablet» в `lsusb`, по нему видно виртуалку;
   - сеть `virtio,bridge=vmbr0`.
3. cloud-init (свой user-data в `local:snippets/pcs-<id>.yaml`):
   - пакеты, `apt upgrade`; для режима usb — ядро с модулями USB-гаджетов
     (`linux-image-extra-virtual`) и заголовки для dkms;
   - root по паролю и ключ PCS;
   - sshd drop-in **`10-pcs.conf`**, а не `99-`: sshd берёт первое значение, а образ
     кладёт `60-cloudimg` с `PasswordAuthentication no`;
   - авто-обновления выключены, предсказуемый DNS.
4. Адрес ВМ:
   - через guest agent;
   - пока его нет — по SSH через IPv6 link-local (адрес выводится из MAC).
5. Ждёт `cloud-init status --wait`; если нужно новое ядро — перезагрузка.
6. Доставка кода. Затем DNS-фикс (`pcs-node dns-fix`).
7. `proxyveth mode <usb|gw> --yes` — он же готовит сервер.
8. `modlink setup`.
9. Если задана таблица — `proxyveth source <ссылка>` и `proxyveth sync`.
10. Если задан `--mpspace` — `pcs mpspace install`: зеркала, при необходимости HTTP-прокси,
    `auth.mp`, их скрипты, перезагрузка ВМ, повтор DNS-фикса.

Создание из панели — то же самое, но фоновым заданием.

### Остальные операции

- **`adopt ID`** — взять под PCS существующую ВМ: ключ кладётся по паролю один раз.
- **`delete ID`** — с подтверждением номером: `qm stop` + `destroy --purge`, чистка `/etc/pcs`.
- **`dns`** — §11.4. На хосте — тоже: его DNS не должен ломаться от модемов.
- **`passwd`** — `chpasswd` со stdin на сервере. Для ВМ ещё `qm set --cipassword`.
- **`key --add PUBKEY`** — добавить свой ключ root. **`--rotate`** — перевыпустить ключ PCS.
- **`ssh-port`** — на Ubuntu 24.04 через drop-in `ssh.socket` (`ListenStream`): `Port` в
  `sshd_config` там игнорируется.
- **`mpspace check`** — юниты `mproxy`, `nodejs-server`, `monit` и `auth.mp` (порт).
  **`auth`** — записать `auth.mp`.
- **`doctor`** — проверки хоста или сервера со счётчиком проблем.

### Меню (`pcs` без аргументов)

```
  ╔════════════════════════════════════════════════════════════════╗
  ║  ВМ 3000   pcs3000 · 192.168.88.9  ● работает                  ║
  ║ ВМ Proxmox · proxyveth gw · модемы 3/3                         ║
  ║ mp.space: не стоит · PCS 5.0.0-dev                             ║
  ╚════════════════════════════════════════════════════════════════╝

  PCS
    1  Серверы            список, создать, взять, удалить, обновить
    2  Модемы             proxyveth на выбранном сервере
    3  Прокси             modlink на выбранном сервере
    4  Софт               mp.space, hivelink
    5  Сеть и доступ      DNS, пароль root, ключ SSH, порт SSH, консоль
    6  Диагностика        сводка, doctor, журнал

    v  сменить сервер        0  выход
  [3000 pcs3000 192.168.88.9] »
```

- Рамка выбранного сервера — всегда наверху, цветная; цвет у каждого сервера свой.
- Сервер стоит и в строке ввода. Так пользователь просил: одну строку «активный
  сервер» легко не заметить и поработать не с тем сервером.
- Опасное (удалить, снести модемы, сменить режим) — только после ввода номера сервера.
- «0 — назад» на каждом экране; неверный ввод меню не роняет.
- **Нарочно нет пунктов:** «сменить IP», «перезагрузить модем», Telegram.

---

## 7. Общие правила команд и `--json`

- У всех команд всех частей есть `--json`. Ответ — **конверт**:

  ```json
  {"ok": true,  "data": …}
  {"ok": false, "error": "текст для человека"}
  ```

  Код возврата 0 или 1, Ctrl-C — 130. Панель и хаб читают только `--json`.
- Без `--json` — текст для человека. Ошибка — `✗ …` в stderr.
- Деструктивное в скриптах — с `--yes`. В меню и панели — подтверждение номером сервера.
- Одна правка за раз — блокировка `/run/<часть>/lock`. Вторая команда ждёт или пишет «занято».
- **Секреты — через stdin:**
  - пароль modlink — `--password -`;
  - таблица proxyveth — `table put`;
  - пароль root и ключ — `pcs-node passwd`/`key` со stdin.

---

## 8. proxyveth — из прокси сетевой интерфейс

### 8.1 Режимы

| | `usb` (бывший vmodem) | `gw` (бывший proxyveth-virt) |
|---|---|---|
| что видит сервер | USB-устройство 12d1:14dc «HUAWEI_MOBILE», драйвер `cdc_ether`, интерфейс `ethN` | veth-интерфейс `pvN` |
| адрес у сервера | DHCP от «модема»: 192.168.N.100, шлюз и DNS — 192.168.N.1, аренда сутки | 192.168.N.100/24 сразу на `pvN` |
| предел | ~40–55 модемов: у каждого своя USB-шина, а шин в ядре не больше 63 | 1–254 модема, около 47 МБ ОЗУ на один |
| USB reset, перезагрузка модема | как у настоящего: USB пропадает и возвращается | нет USB |
| mp.space | ставит свою сторону сам (udev → udhcpc → свои таблицы) | видит интерфейс с адресом; маршрутизацию по источнику делает proxyveth |
| N и N+200 | нельзя вместе: делят таблицу маршрутов mp.space; 153–155 заняты | можно |

На сервере всегда один режим. Сменить — `proxyveth mode gw|usb --yes`: всё своё снимается
и поднимается в новом режиме.

### 8.2 Таблица модемов

Колонки:
- `n` — номер модема для сервера;
- `real` — `81`, `192.168.81.1` или `192.168.81.100`; пусто — real = n;
- `proxy` — `host:port:login:pass` (в пароле может быть `:`) или вместо неё четыре колонки
  `host`/`port`/`login`/`password` (есть синонимы);
- `enabled` — по желанию: `0`, `нет`, `выкл`, `-` выключают строку.

Проверки (`pcs.core.table.lint`). Брак называется поимённо, хорошие строки работают:
- n вне 1–254;
- n совпадает с сетью самой машины;
- n дважды — не берётся ни одна из строк;
- в режиме usb: N и N+200 вместе, 153–155;
- кривая прокси, пробел в логине или пароле;
- ⚠ два модема на одной прокси — смена IP у одного меняет у всех.

**Источник:**
- `proxyveth source <ссылка>` — ссылка на Google-таблицу прямо из адресной строки (сама
  станет CSV; таблица должна открываться по ссылке) или путь к файлу.
- После каждого удачного чтения кладётся локальная копия `/etc/proxyveth/table.csv`.
- Google недоступен — берётся копия, с предупреждением.
- В таблице ни одной годной строки — она считается сломанной, копия остаётся прошлой.
- `proxyveth source local` — главной становится локальная копия. Её правят:
  - `proxyveth table edit` — в `$EDITOR`;
  - `proxyveth table put` — CSV со stdin, так сохраняет панель;
  - вернуть Google — `source <ссылка>`; `table pull` — перечитать Google в копию.
- **Защита:** если таблица требует снести больше половины модемов, без `--force` ничего
  не сносится.

### 8.3 Команды

```
proxyveth mode [usb|gw] [--yes]       режим; смена — снять всё своё и поднять в новом
proxyveth source [URL|local]          источник таблицы
proxyveth table [show|pull|edit|put]  показать / из Google / копия в $EDITOR / копия со stdin
proxyveth lint                        проверить таблицу, ничего не трогая
proxyveth sync [--force]              привести модемы к таблице (таймер — сам, раз в ~2 мин)
proxyveth up|down|restart N|all       down и restart всех — с --yes
proxyveth status [--wan]              состояние; --wan — внешний IP через каждый модем
proxyveth problems                    только проблемные
proxyveth diag N                      прокси → логин → модем → SIM → интернет
proxyveth rotate N | reboot N         смена IP / перезагрузка настоящего модема (в меню и панели их нет)
proxyveth doctor                      окружение
proxyveth setup                       подготовить сервер (зовёт хаб); без режима — подсказка
proxyveth version
```

### 8.4 Строка состояния (`Row`) — одинакова в обоих режимах

```json
{"n": 201, "real": 81, "proxy": "188.134.88.13:16021", "state": "ok",
 "problems": [], "iface": "eth1", "ext_ip": "94.25.229.40", "since": 1760000000}
```

| `state` | значение |
|---|---|
| `ok` | работает |
| `warn` | наша сторона цела, сломан апстрим: прокси, логин, SIM, нет связи, портал оператора, нет интернета |
| `broken` | сломана наша сторона: юнит, маршрут, гаджет, адрес |
| `absent` | модема нет |
| `disabled` | выключен в таблице |
| `rebooting` | перезагружается (как настоящий: USB вынут) |

**Классы диагностики апстрима:**

| класс | что |
|---|---|
| `proxy-down` | прокси не отвечает |
| `proxy-error` | рвёт соединение — лимит? |
| `proxy-auth` | логин или пароль, в том числе код 2 на соединении |
| `modem-blocked` | прокси не пускает в сеть модема — не ген1 |
| `modem-unreachable` | 192.168.real.1 недоступен |
| `sim` | проблема с SIM |
| `no-data` | у модема нет связи |
| `captive` | SIM не оплачена: портал оператора |
| `no-internet` | нет интернета через модем |

- Интернет проверяется двумя целями с повтором; поломкой считается, только если две
  проверки подряд неудачны.
- Сломанная прокси или SIM **не мешает созданию модема**: модем создаётся и ждёт, а
  `status` говорит, что не так. Так попросил пользователь.

### 8.5 Режим usb — устройство

| что | где |
|---|---|
| модем N | netns `pvN`, гаджет configfs `/sys/kernel/config/usb_gadget/pvN` (ECM, 12d1:14dc, строки `HUAWEI_MOBILE`) на слоте `dummy_udc.K` |
| модуль ядра | `dummy_hcd` собирается dkms (пакет `proxyveth-dummy-hcd`) из исходника той же серии ядра, `MAX_NUM_UDC=64`; слотов по умолчанию 48 |
| MAC | со стороны сервера `0c:5b:8f:27:9a:64` (как у всех E3372h), со стороны модема `00:1e:10:1f:00:00` |
| сеть | в netns: `usb0` 192.168.N.1/24 и tun `pvtun0` 172.20.0.1/30 |
| юниты | `proxyveth-dns@N` (dnsmasq, DHCP ровно .100, аренда 24 ч), `proxyveth-px@N.socket` + `.service` (ретранслятор), `proxyveth-sb@N` (sing-box), `proxyveth-web@N` (веб-морда), таймер `proxyveth-sync.timer` |
| настройки | `/etc/proxyveth/config.json`, `/etc/proxyveth/usb/<N>/{singbox.json,dnsmasq.conf,env,proxy.pass,spec.json}` |
| состояние, журнал | `/var/lib/proxyveth`, `/run/proxyveth/state.json`; `/var/log/proxyveth` |

**Как идёт трафик:**
1. Клиент сервера (mp.space) шлёт пакет на `ethN`.
2. Пакет приходит в netns в `usb0`; правило `from 192.168.N.0/24 lookup 100` ведёт его в
   `pvtun0`.
3. sing-box (стек **system**) держит соединение и открывает его через SOCKS5 на
   `127.0.0.1:1080` внутри netns.
4. Это сокет systemd `proxyveth-px@N.socket` (`NetworkNamespacePath`); его принимает
   `systemd-socket-proxyd` уже на самом сервере.
5. Тот соединяется с прокси ген1 по основному каналу.

Своей сети наружу у netns нет, veth и NAT на сервере тоже нет.

**DNS модема:**
- dnsmasq → `127.0.0.1#5353` (direct-inbound sing-box) → `hijack-dns`;
- DNS-сервер sing-box — `tcp://192.168.real.1` через прокси, потому что UDP через ген1
  не ходит;
- любой DNS клиента, в том числе прямо на 8.8.8.8, тоже уходит к DNS настоящего модема
  (правило `port 53 → hijack-dns`).

**Веб-морда** (`modem_web.py`, `proxyveth-web@N`):
- процесс работает на сервере, а слушающий сокет `192.168.N.1:80` — внутри netns (через `setns`);
- отдаёт HiLink настоящего модема (`192.168.real.1` через прокси) и переписывает в
  ответах адреса на виртуальные;
- опасные записи (подсеть DHCP, пароль, прошивка, сброс) не пропускаются; обходы
  фильтра (`http://…`, `//`, `%xx`, регистр) закрыты;
- SMS не закрыты: их закрывает mobilink на ген2;
- перезагрузка через веб-морду (`Control=1`): USB «вынимается» и возвращается, когда
  настоящий модем снова отвечает (≈26–31 с на стенде).

**Сторона сервера:**
- если стоит mp.space (`/usr/local/bin/modem-interface-setup.sh`), адрес и маршруты
  делает его udev и udhcpc;
- иначе (`hostside: auto|on`) — свой udev-хук и udhcpc с таблицей `100 + N % 200` и
  правилом `from 192.168.N.100`.

`/etc/systemd/network/10-proxyveth-cdc.link` с `NamePolicy=` оставляет ядерные имена
`ethN`: у всех модемов один MAC, имя `enx…` досталось бы только первому.

**Создание — стратегия `window:5`:** не больше 5 модемов «в пути» (у mp.space 5 слотов
настройки). 20 модемов — около 30 с, 40 — около 50 с.

**Лестница починки (`sync` и таймер):**
1. перезапуск юнита systemd;
2. переподключение USB (unbind/bind со стороны сервера);
3. пересоздание модема;
4. другой слот UDC;
5. после 3 неудач подряд — пауза 10 минут.

**Пустые слоты.** У пустого слота `dummy_hcd` отвязан от драйвера: в `lsusb` нет десятков
пустых root hub, root hub есть только у воткнутых модемов. Перед подключением модема шина
его слота возвращается.

**Ещё в config.json (перешло из vmodem, в меню и панели нет):**
- `proxy` — прямые прокси на каждый модем, юнит `proxyveth-proxy`: SOCKS5+HTTP на
  `base_port + N`, выход с адреса модема;
- `notify` — Telegram или webhook.

### 8.6 Режим gw — устройство

| что | где |
|---|---|
| модем N | netns `pvN`; veth: на сервере `pvN` 192.168.N.100/24, в netns `eth0` 192.168.N.254/24 |
| туннель | sing-box tun `pvtun0` в netns (10.0.N.1/30) → SOCKS5 ген1 |
| маршрутизация | `ip rule from 192.168.N.100 lookup 1000+N priority 30000+N`; в таблице — default через .254 |
| выход sing-box к прокси | с адреса .254 через сервер: NAT `-s 192.168.N.254/32 -o <основной канал> MASQUERADE` с пометкой `pcs:proxyveth` |
| в netns | DNS в туннель разрешён, остальной UDP — DROP (ген1 его не возит, а клиенты mp.space заливают тысячами пакетов); MASQUERADE на tun; TCPMSS |
| real ≠ n | `192.168.N.1:80` — посредник `proxyveth-hostfix@N`: подменяет `Host`/`Location`, иначе прошивка отвечает 307; DNS к `192.168.N.1:53` — DNAT на `192.168.real.1:53`, в туннель |
| real = n | 192.168.N.1 маршрутизируется в tun прямо к настоящему модему |
| юниты | `proxyveth-gw@N` (sing-box), `proxyveth-hostfix@N` (посредник), общий `proxyveth-sync.timer` |
| починка | `apply()` чинит лёгкое без пересоздания (юнит, tun, маршрут, правило, таблица, NAT); тяжёлое — пересоздание с нарастающей паузой 0 → 2 → 5 → 10 → 20 → 30 мин. В старом watchdog модем после 3 неудач бросался навсегда |
| основной канал | `pcs.core.net.uplink_route()` — явно, а не «первый default из main» |

### 8.7 Переезды

- **С vmodem 4.x** (`proxyveth setup` сам):
  - переносит источник таблицы и настройки из `/etc/vmodem/config.json`, ставит режим `usb`;
  - снимает старые модемы (`vmN`) и юниты `vmodem-*`;
  - поднимает модемы по-новому;
  - удаляет `/etc/vmodem`, `/usr/local/sbin/vmodem*`, `/usr/local/lib/vmodem`, старые udev,
    `.link`, networkd- и sysctl-файлы;
  - переименовывает пакет dkms.
- **С proxyveth-virt 3.4:** при наличии `/etc/proxyveth/env` режим определяется как `gw`.
  - Источник берётся из `API_URL`/`SHEET_CSV_URL`/`SHEET_ID`.
  - Снимается **только узнаваемо своё**: `ns_N`, `mdmN`, правила по его `config.json`,
    `/etc/netns/ns_N`, его юниты и `/usr/local/bin/proxyveth.py`.
  - Чужое (mp.space, modlink и его sing-box) не трогается.
  - **На рабочей ВМ 200 (90 модемов) не запускался** — только по команде.

---

## 9. modlink — из интерфейса прокси

Работает поверх **любых** интерфейсов — настоящих модемов или proxyveth.

### 9.1 Таблица — `/etc/modlink/proxies.json` (600)

| поле | что | куда уходит |
|---|---|---|
| `id` | постоянный номер строки: N для адреса 192.168.N.x, если свободен, иначе с 1001; удалённые номера не переиспользуются | — |
| `enabled` | строка активна | выключенные не попадают в конфиг |
| `name` | метка | интерфейс |
| `login`, `password` | доступ к прокси (печатный ASCII; в логине нельзя `:`) | `users` mixed-inbound |
| `port` | порт прокси, **постоянный**; `auto` — первый свободный с 10000 | `listen_port` |
| `lan_ip` | адрес интерфейса на сервере | `inet4_bind_address` direct-outbound: именно это привязывает трафик к модему |
| `modem_ip` | веб-интерфейс Huawei | HiLink API: реконнект, ребут, проверка |
| `reconnect_port` | порт триггера; `auto` — следующий за `port` | `GET http://<ip>:<порт>/reconnect` |
| `interval_min` | автореконнект, минут (0 — выкл) | таймер в демоне |

- Порты прокси и триггеров не пересекаются ни между строками, ни с занятыми на машине.
  Пример: на ВМ с mp.space порт 10000 занят им, и первая строка получила 10001.
- Правки вступают в силу **только после `modlink apply`**: демон берёт строки из
  `/var/lib/modlink/applied.json`. `list` и `status` показывают `pending`, если есть
  неприменённые правки.

### 9.2 Устройство

- **`modlink-sb.service`** — sing-box. На каждую включённую строку:
  - mixed-inbound `0.0.0.0:port` (HTTP CONNECT + SOCKS5) с логином;
  - direct-outbound с `inet4_bind_address = lan_ip`;
  - правило inbound → свой outbound.

  Перед правилами строк:
  - имена резолвятся только в IPv4;
  - частные адреса и IPv6 — `reject`: иначе через прокси были бы видны службы самого
    сервера (127.0.0.1, порты proxyveth и mp.space) и его LAN, а IPv6 ушёл бы мимо модема.

  Последнее правило — `reject`.
- **`modlink.service`** (`modlink daemon`):
  - триггеры `GET /reconnect` на портах строк. Ответ `{ok, ip?}` или `{ok:false, error}`,
    коды 200 / 409 (уже идёт) / 502 (модем отказал) / 404 / 405;
  - автореконнект по `interval_min`;
  - SIGHUP — перечитать;
  - состояние — `/run/modlink/state.json`.
- **Реконнект через HiLink:** свежая пара SesTokInfo перед каждым POST → передача данных
  выкл → режим сети `02` и обратно к исходному → данные вкл (3 попытки) → ждём
  `ConnectionStatus 901`.
- Новый внешний IP узнаётся запросом с привязкой к `lan_ip`, то есть тем же путём, что идёт прокси.
- **Ребут** — `device/control Control=1`.
- Журнал строки — `/var/log/modlink/<id>.log`, при 1 МБ переносится в `.1`.

### 9.3 Команды

```
modlink list [--show-pass]
modlink add --lan-ip IP [--name …] [--login …] [--password …|gen|-] [--port N|auto]
            [--modem-ip IP] [--reconnect-port N|auto] [--interval MIN] [--disabled]
modlink set ID поле=значение …       (password=- — со stdin)
modlink del ID --yes | enable ID | disable ID
modlink apply                        конфиг → sing-box check → запись → перезапуск
modlink status | test ID | reconnect ID | reboot ID | log ID [-n N]
modlink export [--ip IP|auto] [--save]   IP:PORT:LOGIN:PASS<TAB>http://IP:RPORT/reconnect
modlink from-ifaces [--add]          строки по интерфейсам 192.168.N.100 (основной канал пропускается)
modlink migrate [--from DIR] [--dry-run] [--force] [--stop-panel]   из modlink-linux
modlink setup | uninstall --yes [--purge] | daemon | version
```

**Перенос из modlink-linux (`migrate`):**
- строки с id N, логином `modemN`, портами `base+2i` и `base+2i+1`, паролями: из
  `modems.conf`, из `singbox.json` или тот sha256, что придумывал `modlink-server`;
- старый `modlink.service` (их sing-box, имя совпадает с нашим демоном) останавливается,
  копия — в `/etc/modlink/legacy/`;
- старая панель (порт 5000) без `--stop-panel` не останавливается;
- `/usr/local/bin/sing-box` не трогается.

### 9.4 Ответы `--json`

| команда | `data` |
|---|---|
| `list` | `{proxies: [строки §9.1, password=null], pending}` |
| `status` | `{sb, daemon, unit, pending, rows: [{id, name, enabled, applied, state: ok\|warn\|broken\|disabled\|pending, port, port_up, lan_ip, iface, reconnect_port, trigger_up, trigger_error, interval_min, next, last: {t, how, ok, dt, text}}]}` |
| `test ID` | `{id, proxy: {ok, ip\|error}, hilink: {ok, status, net, signal\|error}, iface}` |
| `reconnect ID` | `{id, ok, ip, same, dt}` |
| `log ID` | `{id, lines}` |
| `export` | `{ip, lines}` |
| `from-ifaces` | `{suggest, added}` |

---

## 10. hivelink — драйвер настоящих модемов

Модели: Huawei E3372, E3131, E353, Vodafone K5150/K5160. Ставится на хост Proxmox
**автоматически** (`install.sh`), в ВМ — по запросу (`pcs hivelink install --server ID`).

**Проход** (`hivelink.timer` раз в 15 с плюс udev на каждое появление устройства):

| шаг | что делает |
|---|---|
| Zero-CD | `12d1:1f01…` — модем отдался как CD-ROM; usb_modeswitch со своим конфигом |
| USB-конфигурация | перебор `bConfigurationValue` до `rndis_host`/`cdc_ether`; удачная запоминается в `/var/lib/hivelink/confmap`; не годится ни одна — модем ждёт переподключения |
| сеть | DHCP через systemd-networkd (`25-hivelink.network`: `UseDNS=no`, `DNSDefaultRoute=no`, без шлюза); таблица `2000+N`, правило `from 192.168.N.100 priority 31000+N` |
| данные | сессия HiLink → `dataswitch=1`; если `ConnectionStatus` не 900/901/903 — дозвон, не чаще раза в 180 с |
| фикс приёма | DKMS-патч `rndis_host`: параметр `rx_urb_size_override` (16384) вместо зашитых 2048 — download ≈1,3 Мбит/с без него |

**Чужого не трогает:**
- **виртуальные модемы** (шина `dummy_hcd`/`vhci_hcd`) пропускаются везде: в выборке, в
  udev (`GOTO` до любого действия), в `.network` (`Path=!…`), при перезагрузке модуля;
- адрес `.100` на сетевухе хоста — не модем;
- уборка — только своих приоритетов 31001–31254 и таблиц 2001–2254;
- глобальные `rp_filter` и `gc_thresh` не меняет; `rp_filter=2`, `arp_*` — только на
  интерфейсах модемов.

**Сеть модемов:** `config.json: "net": "auto|on|off"`. При `auto` и стоящем mp.space адрес
и маршруты делает его udhcpc, а hivelink — только USB и HiLink: иначе два DHCP-клиента
дрались бы за интерфейс.

**networkd:**
- установщик его не перезапускает: на Ubuntu — `networkctl reload` и reconfigure только модемов;
- на Proxmox — запускает, если не работал, и выключает `wait-online`, если тот не был
  включён (иначе загрузка ждала бы 2 мин). Перед этим — `ensure_base()` (§11.2), чтобы
  networkd не снёс маршруты хоста;
- `uninstall` гасит networkd, только если его запускал hivelink и других `.network` нет.

**DKMS-фикс:**
- версия ядра — из `/proc/version_signature` (Ubuntu) или последнего вхождения в
  `/proc/version` (Debian/Proxmox; в начале строки стоит `gcc …`);
- исходник `rndis_host.c` с git.kernel.org или зеркала; запасной путь — архив ядра.
  Для x.y.0 файл называется `linux-6.8.tar.xz`;
- повторный `install` ничего не пересобирает; initramfs пересобирается, только если в нём
  штатный `rndis_host`;
- `HIVELINK_SKIP_RNDIS_FIX=1` — без фикса;
- при Secure Boot — подсказка про регистрацию ключа MOK.

**Файлы:**
- `/etc/hivelink/config.json` (700);
- `/var/lib/hivelink`, `/run/hivelink`, `/var/log/hivelink` + journald (`-t hivelink`);
- udev `70-hivelink.rules`, `71-hivelink-ports.rules` (ссылки AT-портов `/dev/hivelink/…`),
  `41-hivelink-modeswitch.rules`.

**Команды:**

```
hivelink install | uninstall | status [--json] | doctor [--json]
hivelink reconnect N | reset N|USB-порт | recfg N|USB-порт | dataon N|--all
```

**`status --json`:**

```
{version, installed, net, virtual,
 usb: {total, hilink, zerocd, stick, other, with_addr, ok},
 fix: {active, size, file, dkms, needed},
 modems: [{n, iface, driver, usb_port, usb_id, mode, cfg, addr, conn, conn_text,
           dataswitch, net, signal, ext_ip, errors}]}
```

**На стенде проверено:** на хосте ставится и собирает фикс; на ВМ с виртуальными модемами
видит 0 настоящих и 3 виртуальных и не меняет ни одного правила и маршрута. На настоящих
модемах ещё не запускался: Zero-CD, перебор конфигураций и HiLink перенесены из
e3372-driver один в один.

---

## 11. Сеть, DNS, sing-box — сквозные правила

### 11.1 Таблицы, приоритеты, пометки

| часть | таблицы | приоритет ip rule | пометка iptables |
|---|---|---|---|
| proxyveth usb, своя сторона сервера | `100 + N % 200` | авто (как mp.space) | — |
| proxyveth gw | `1000 + N` | `30000 + N` | `pcs:proxyveth` |
| hivelink | `2000 + N` | `31000 + N` | `pcs:hivelink` |
| ядро: адреса прокси → основной канал | `90` | `32765` | — |

- mp.space нумерует свои таблицы так же, как proxyveth usb (имена `modemK`, где K = 1 +
  октет % 200), а 253–255 у него — системные.
- iptables — только с `-m comment --comment pcs:<часть>`; уборка снимает только своё.

### 11.2 База машины — `pcs.core.net.ensure_base()`

- `/etc/sysctl.d/90-pcs.conf`: `kernel.panic=10`, `kernel.panic_on_oops=1`, `ip_forward=1`.
  Если ядро упадёт, сервер перезагрузится сам, а модемы поднимет таймер.
- `/etc/systemd/networkd.conf.d/10-pcs.conf`: `ManageForeignRoutingPolicyRules=no`,
  `ManageForeignRoutes=no`. Иначе любой `netplan apply` стирает маршрутизацию модемов и mp.space.

### 11.3 Адреса прокси — только через основной канал

`pcs.core.net.pin_proxies` при каждом sync ставит правило `ip rule priority 32765 to <IP
прокси> lookup 90`; в таблице 90 — маршрут по умолчанию основного канала.

**Зачем.** Скрипт mp.space на каждый воткнутый модем запускает `udhcpc`. Стандартный
скрипт udhcpc ставит маршрут по умолчанию через модем **без метрики**, то есть с метрикой
0, — лучше основного. mp.space удаляет его только через пару секунд. Соединение к прокси,
открытое в это окно, уходило в свой же туннель и размножалось петлёй: до 11 000
соединений, после чего прокси начинала отказывать.

### 11.4 DNS-фикс (`pcs dns`, `pcs-node dns-fix`, `pcs/core/dns.py`)

Механизм взят из исходного ProxyControlService и доработан:
0. Снять `chattr -i` с `/etc/resolv.conf`.
1. `/etc/systemd/resolved.conf.d/99-pcs.conf`: `DNS=…` (по умолчанию 1.1.1.1 8.8.8.8),
   `FallbackDNS=9.9.9.9 1.0.0.1`, `Domains=~.`, `DNSStubListener=yes`, `DNSSEC=no`,
   `DNSOverTLS=no`, `Cache=yes`.
2. Отдельный `/etc/netplan/99-pcs-dns.yaml` (600) только для интерфейсов с DHCP:
   `dhcp4/6-overrides: {use-dns: false, use-domains: false}`. `50-cloud-init.yaml` не трогается.
3. `netplan generate` — при ошибке drop-in откатывается. `netplan apply` — только если
   файл изменился и только после `ensure_base()`.
4. `/etc/resolv.conf` → симлинк на `stub-resolv.conf`, перезапуск resolved, `flush-caches`.
5. Проверка: `getent ahostsv4` для github.com, docs.google.com, mobileproxy.space и что на
   интерфейсах нет чужого DNS.

Сверх старого:
- oneshot `pcs-dns.service` при загрузке;
- снятие чужого `172.20.0.2`;
- повтор после установки mp.space.

На хосте без netplan (Proxmox) — только resolved/resolv.conf.

### 11.5 sing-box

- Версия **1.14.2**, `/usr/local/lib/pcs/sing-box`, сумма сверяется (`pcs.core.singbox`).
  Один бинарник на proxyveth и modlink.
- **`/usr/local/bin/sing-box` чужой** — не читаем, не ставим, не удаляем. Там его держат
  modlink-linux (1.13.14) и proxyveth-virt (1.10.0): кто поставил последним, тот и прав.
  На ВМ 200 это ломало proxyveth-virt; починено там отдельным путём через `SINGBOX_BIN`.
- **Конфиги — только в формате 1.12+** (1.14 старый не принимает вовсе):
  - DNS-серверы `{"type": "tcp", "server": …, "detour": …}`, а не `"address": "tcp://…"`;
  - никаких служебных outbound `dns`/`block`: вместо них `{"action": "hijack-dns"}`,
    `{"action": "reject"}`;
  - никаких `sniff*` на inbound (`{"action": "sniff"}` в route);
  - никакого `domain_strategy` в dial-полях.

  Переменная `ENABLE_DEPRECATED_LEGACY_DNS_SERVERS=true` не спасает: следом падает
  outbound `dns`, удалённый в 1.13.
- sing-box и всё долгоживущее — **своими юнитами** (`…@N`), а не дочерними процессами
  oneshot-юнитов: иначе systemd убьёт их вместе с родителем.
- sing-box в netns — **без доступа к D-Bus** (`InaccessiblePaths=-/run/dbus/system_bus_socket`).
  Иначе он прописывает DNS своего tun (172.20.0.2, `~.`) в systemd-resolved по номеру
  интерфейса из netns. D-Bus общий, и на сервере под тем же номером оказывается `eth0`
  или сетевуха модема — весь DNS сервера уходил в никуда.

---

## 12. pcs-node — служебная команда хаба на сервере

Человеку не нужна, её зовёт хаб. Работает и на хосте: так хаб чинит DNS у себя.

```
pcs-node info [--wan] [--json]      версия кода, режим proxyveth, сводка модемов, mp.space
pcs-node dns-fix [--dns "…"] [--boot] [--json]
pcs-node passwd [--json]            пароль root — строкой со stdin
pcs-node key [--add|--drop] [--json]   ключи root; ключ — строкой со stdin
pcs-node ssh-port PORT [--json]
pcs-node mpspace install --params FILE [--no-reboot]
pcs-node mpspace auth|check [--json]   auth.mp — со stdin
pcs-node version
```

---

## 13. Веб-панель — HTTP API

### 13.1 Доступ

- `pcs-web.service`: `pcs web serve` → `pcs.web.server.main`. Порт **666**, слушает 0.0.0.0.
- Порт открывает и закрывает **сам пользователь**: пробрасывает на время работы.
- `pcs web on|off|status|passwd`. Порт 5000 на ВМ (старый modlink) не наш — не трогаем.
- Для разработки — фейковые данные:

  ```bash
  PCS_WEB_FAKE=1 python3 -c 'from pcs.web.server import main; main(["--port","6660"])'
  ```

- **`/etc/pcs/web.json` (600)** — формат пишет и читает `pcs.web.auth`, хаб пользуется им же:

  ```json
  {"user": "admin",
   "password": {"algo": "pbkdf2-sha256", "iterations": 600000, "salt": "<hex>", "hash": "<hex>"},
   "changed": 1760000000}
  ```

### 13.2 Вход и защита

| что | как |
|---|---|
| вход | `POST /api/login` с телом `{"user", "password"}`. Нужны заголовок `X-CSRF` (любое значение — чужая страница не пошлёт его без preflight) и свой `Origin`, если он есть |
| ответы входа | 200 + cookie `pcs_sid_666`; 401 — неверно; 403 — нет `X-CSRF` или чужой Origin; 429 — пауза; 503 — вход не задан |
| пауза после неудач | первые 5 неудач с адреса — без паузы, дальше 30·2^k с (не больше 900), окно 1 ч; удачный вход сбрасывает счёт |
| сессия | cookie `pcs_sid_<порт>; HttpOnly; SameSite=Strict`. Живёт 7 суток, без активности — 24 ч; смена пароля обнуляет все сессии |
| CSRF | `GET /api/session` → `{auth, user, csrf, version, port, fake}`. Каждый POST `/api/*` — с заголовком `X-CSRF: <csrf>` и своим (или пустым) Origin |
| CORS | нет. Чужие страницы сюда не ходят |
| выход | `POST /api/logout` |
| без сессии | `/api/*` → 401 `{"ok": false, "error": "нужен вход", "data": {"auth": false}}`; страницы → редирект на `/login` |
| журнал | каждый POST пишется в журнал панели (`journalctl -u pcs-web`) |

Пример с curl (на хосте):

```bash
J=/tmp/jar
curl -s -c $J -b $J -H 'X-CSRF: 1' -H 'Content-Type: application/json' \
     -d '{"user":"admin","password":"…"}' http://127.0.0.1:666/api/login
T=$(curl -s -b $J http://127.0.0.1:666/api/session | python3 -c 'import json,sys;print(json.load(sys.stdin)["data"]["csrf"])')
curl -s -b $J http://127.0.0.1:666/api/overview
curl -s -b $J -H "X-CSRF: $T" -H 'Content-Type: application/json' \
     -d '{"target":"2000","part":"proxyveth","args":["status"]}' http://127.0.0.1:666/api/run
```

### 13.3 Ответы

Всегда конверт `{"ok": true, "data": …}` или `{"ok": false, "error": "…"}`.

| HTTP-код | когда |
|---|---|
| 200 | запрос дошёл. Если действие не вышло (сервер недоступен, прокси упала) — 200 с `ok: false` |
| 400 | кривой запрос |
| 401 | нет входа |
| 403 | нет CSRF, чужой Origin или опасное действие без подтверждения |
| 404 / 405 | нет адреса / не тот метод |
| 500 | сбой панели — подробности в `journalctl -u pcs-web` |

Тело POST — JSON. Сводные ответы кэшируются на 4 с; действия, меняющие состояние, кэш сбрасывают.

### 13.4 Адреса

| метод и путь | что | тело / параметры → `data` |
|---|---|---|
| `GET /api/overview` | приветственная страница | → `{host, host_error, servers: [§14 servers()], servers_error, ts}` |
| `GET /api/servers/{id}` | один сервер | → §14 `server(id)` |
| `GET /api/servers/{id}/proxyveth` | состояние модемов и источник | `?wan=1` — с внешними IP; `?only=status` → `{status: конверт proxyveth status, source: конверт proxyveth source}` |
| `GET /api/servers/{id}/proxyveth/table` | локальная копия таблицы | → `{csv, meta}` |
| `POST /api/servers/{id}/proxyveth/table` | сохранить копию (`table put`) | `{csv, make_main?}`; `make_main` — сразу `source local` → `{saved, source?}` |
| `GET /api/servers/{id}/modlink` | таблица и состояние modlink | `?only=status` → `{list: {rows (без паролей, password_set), pending}, status}` |
| `POST /api/servers/{id}/modlink/save` | правки разом и apply | `{del: [id], set: [{id, fields}], add: [{lan_ip, name?, login?, password?, port?, modem_ip?, reconnect_port?, interval_min?, enabled?}], apply?: true}` → `{steps: [{what, ok, error, data}], applied}`. Набранный пароль идёт на сервер через stdin |
| `POST /api/servers/{id}/modlink/reveal` | пароль одной строки | `{id}` → `{id, password}` |
| `POST /api/servers/{id}/modlink/export` | строки для клиента | → `{text}` |
| `POST /api/servers` | создать ВМ (задание) | `{name, cores=4, ram_gb=8, disk_gb=40, mode: usb\|gw, sheet?: "https://…", mpspace?: bool}` → `{job}` |
| `POST /api/servers/{id}/delete` | удалить ВМ | `{confirm: "<id>"}`, иначе 403 → `{job}` или `{result}` |
| `POST /api/run` | любая команда части на сервере или хосте | `{target: id\|"host", part: proxyveth\|modlink\|hivelink, args: [...], confirm?}`. С `--yes`/`--force` нужен `confirm` = номер сервера, иначе 403. На хосте — только hivelink. → конверт команды |
| `POST /api/targets/{id\|host}/dns` | DNS-фикс | `{dns?: "1.1.1.1 8.8.8.8"}` |
| `POST /api/targets/{id\|host}/passwd` | пароль root | `{password}` |
| `POST /api/targets/{id\|host}/key` | ключ SSH | `{pubkey}` или `{rotate: true}` |
| `POST /api/targets/{id\|host}/mpspace` | mp.space | `{action: install\|auth\|check, auth?}` → `{job}` или `{result}` |
| `POST /api/targets/{id\|host}/update` | обновить код | → `{job}` или `{result}` |
| `GET /api/jobs/{job}` | ход задания | → §14 `job()` |
| `GET /api/session`, `POST /api/login`, `POST /api/logout` | вход | §13.2 |
| `GET /ws/term?target=<id\|host>&cols=100&rows=30` | терминал | §13.5 |

### 13.5 Терминал (WebSocket)

- RFC 6455, версия 13, только с сессией и своим Origin.
- Не больше 24 терминалов сразу (иначе 503).
- Процесс — `api.term_argv(target)`: `ssh -tt root@сервер` ключом PCS или `bash -l` для хоста.

| направление | кадр |
|---|---|
| клиент → сервер | двоичный — ввод как есть; текстовый — JSON `{"type": "resize", "cols": C, "rows": R}` |
| сервер → клиент | двоичный — вывод pty |

Сервер закрывает соединение, когда процесс кончился; при закрытии вкладки процесс завершается.

### 13.6 Интерфейс

- **Слева дерево:** хост → его ВМ (точка состояния, «USB · ok/total» или «шлюз ·
  ok/total»), внизу «+ Создать ВМ».
- **Приветственная страница хоста:**
  - карточки: ВМ, модемы, проблемы, ОЗУ хоста;
  - список ВМ с кнопками «Открыть», «Терминал», «Удалить» (подтверждение номером);
  - действия хоста: DNS, пароль или ключ, hivelink, обновить PCS.
- **Страница ВМ:** сверху крупная цветная плашка выбранной ВМ — перепутать сервер нельзя.
  Вкладки:
  1. **proxyveth** — режим, источник, синхронизация, таблица модемов, правка копии;
  2. **modlink** — таблица §9.1 с кнопками Test, ⟳ реконнект, ↻ ребут, ≡ лог, ✕; добавить, применить, экспорт;
  3. **терминал** — xterm.js (лежит в репозитории, MIT) и кнопки частых команд;
  4. **настройка и управление** — всё, где ответ «применено / сохранено / ошибка».
- Статика — чистые HTML/CSS/JS без сборки, тёмная и светлая тема; данные опрашиваются раз
  в ~10 с.

---

## 14. API хаба (Python, `pcs/hub/api.py`)

Панель — тонкий слой над этими функциями. Ошибки — `pcs.core.util.Fail` (текст для
человека). Долгое — фоновым заданием: функция сразу отдаёт `job_id`.

```python
host_info()                      # {"name", "kind": "proxmox", "version", "cpu", "ram", "disk", "hivelink": {…}}
servers()                        # [{"id", "name", "ip", "state", "mode", "modems": {"ok","warn","broken","total"}, "mpspace"}]
server(id)                       # то же + версии частей
create_server(params) -> job_id  # params: name, cores, ram_gb, disk_gb, mode, sheet, mpspace
delete_server(id, confirm)       # confirm == id
run(id, part, args, timeout=300, input=None) -> конверт   # id=None — хост (только hivelink); input — stdin
dns_fix(target, dns=None)        # target: id или "host"
set_password(target, password)
set_key(target, pubkey=None, rotate=False)
mpspace(target, action, **kw) -> job_id | dict
update(target=None) -> job_id | dict
job(job_id)                      # {"id", "kind", "title", "state": running|done|failed, "started", "finished", "log": […], "result", "error"}
term_argv(target) -> list        # argv для pty терминала
```

**Значения:**

| поле | значения |
|---|---|
| `servers()[i]["state"]` | `running`, `stopped`, `paused`, `absent` (ВМ нет в Proxmox), `offline` (ВМ работает, SSH не отвечает), `old` (нет `pcs-node` — нужен `pcs update`) |
| `mode` | `"usb"`, `"gw"`, `None` |
| `mpspace` | `None` (не спрашивали) или `{"installed", "ok", "units": {юнит: состояние}, "port", "auth"}` |

**Задания.** Отдельный процесс `pcs job run ID` в юните `pcs-job-ID` — переживает
перезапуск панели и обрыв браузера. В `/var/lib/pcs/jobs/`:
- `ID.json` — состояние;
- `ID.log` — всё, что задание напечатало;
- `ID.params` (600) — параметры, удаляется, как только задание их прочло.

Хранятся последние 50.

---

## 15. Форматы JSON частей (кратко)

| команда | `data` |
|---|---|
| `proxyveth status [--wan]` | список `Row` (§8.4) с итогом |
| `proxyveth diag N` | `{"n", "steps": [{"name", "ok", "text"}], "verdict"}` |
| `proxyveth doctor` | `[{"name", "ok", "text"}]` |
| `proxyveth sync` | `{"created": […], "recreated": […], "removed": […], "failed": {n: текст}}` |
| `proxyveth table show` | сводка + `text` (CSV), `path`, `source` |
| `proxyveth table put` | `{changed, ok: [n], disabled, invalid, problems, source}` |
| `modlink …` | §9.4 |
| `hivelink status` | §10 |
| `pcs-node info` | версия кода, режим proxyveth, сводка модемов из `proxyveth status`, mp.space (юниты `mproxy`, `nodejs-server`, порт, auth) |

---

## 16. Безопасность

- **Секреты** (пароли прокси, auth.mp, пароль панели, ключи):
  - в файлах 600, каталоги 700;
  - на сервер идут через stdin, а не в командной строке (`ps` их не видит);
  - пароль root при создании ВМ — хэшем в cloud-init.
- **Панель:**
  - без CORS, CSRF на каждом POST, `SameSite=Strict`, пауза после неудачных входов;
  - опасное — с подтверждением номером;
  - HTTP без TLS: порт открывается пользователем на время работы.
- **modlink:**
  - субпроцессы только списком аргументов, без `shell=True`. В старом modlink пароль из
    панели попадал в shell — выполнение команд от root;
  - через прокси закрыты локальная сеть сервера и IPv6;
  - пароли в API — только по явному запросу (`reveal`, `export`).
- **Веб-морда модема** режет опасные записи HiLink; SMS открыты сознательно.
- **Чужое не трогаем:**
  - `/usr/local/bin/sing-box`;
  - правила и таблицы других частей и mp.space;
  - порт 5000 старого modlink;
  - устройства на `dummy_hcd` (для hivelink).

---

## 17. Грабли, которые PCS обходит сам (история решений)

1. **Отдельная ВМ на каждый модем не нужна.** Модем — netns плюс USB-гаджет на `dummy_hcd`.
   40 модемов поднимаются меньше чем за минуту на одной ВМ.
2. **У каждого модема своя USB-шина, а шин не больше 63.** Отсюда предел режима usb
   (~40–55). Пустые слоты шину не держат.
3. **У всех E3372h один MAC.** Имя `enx…` достаётся только первому, на остальных udev
   падает. `.link` с `NamePolicy=` оставляет `ethN`, как у mp.space.
4. **systemd-networkd при перезапуске стирает чужие `ip rule`** (любой `netplan apply`) →
   drop-in §11.2; пропавшее правило `sync` замечает и чинит.
5. **dnsmasq иногда не подхватывает интерфейс модема** — проверяется и перезапускается;
   mp.space повторять DHCP не умеет.
6. **Падение туннеля стирает его маршруты** — их ставит `ExecStartPost` при каждом старте sing-box.
7. **Выгрузка `dummy_hcd` при живом гаджете ломает ядро** — разборка всегда сверху вниз;
   не привязавшийся слот обходится.
8. **UDP через прокси ген1 не работает**: UDP ASSOCIATE принимается, но порт ретранслятора
   на стороне прокси закрыт. Поэтому DNS — по TCP. В режиме gw остальной UDP режется сразу.
9. **veth `vxN` на сервере агрегатора были лишними** (PCS 4.1, PR #2). sing-box ушёл в
   netns, выход к прокси — через сокет systemd в netns и `systemd-socket-proxyd` на сервере.
10. **Вариант «sing-box со стеком gvisor на сервере, tun перенесён в netns» ронял ядро 6.8** —
    гонка в `u_ether` (`eth_start_xmit`), 2 падения из 2. Остались на стеке `system`.
    Страховка — `kernel.panic=10`.
11. **udhcpc ставит маршрут через модем с метрикой 0** → петля соединений к прокси (§11.3).
12. **sing-box прописывал DNS своего tun в resolved чужого интерфейса** через D-Bus → D-Bus
    закрыт (§11.5).
13. **Разные версии sing-box в `/usr/local/bin`** ломали друг друга → свой путь и одна версия 1.14.2.
14. **«QEMU Tablet» в `lsusb` выдаёт виртуалку** → `tablet 0`.
15. **sshd в образе Ubuntu** берёт первое значение из `sshd_config.d`, а образ кладёт
    `60-cloudimg` → свой файл `10-pcs.conf`.
16. **Порт SSH на 24.04** задаётся через `ssh.socket`, а не `sshd_config`.
17. **mp.space занимает порт 10000** на сервере → modlink по `auto` берёт следующий свободный.

---

## 18. Состояние, пробелы, что дальше

**Сделано и проверено** — см. §0. Стенд: хост Proxmox и две тестовые ВМ, одна в режиме
usb, другая в режиме gw.

**Не сделано или не проверено:**
- панель в браузере не просмотрена человеком (API проверено curl'ом, включая вход);
- mp.space на сервере в режиме gw не ставился;
- рабочие ВМ на старых версиях (vmodem 4.1, proxyveth-virt 3.4) не переводились. Перевод
  — `pcs update --server ID`, только по решению владельца;
- hivelink на настоящих модемах не запускался (их сейчас нет: стоят под Windows 10);
  офлайн-установка пакетов не сделана;
- `tests/stand/*` ещё со старыми именами;
- последняя вычитка `usb.py`/`cli.py` агентом B1 не доделана (он завис); тесты и стенд в порядке.

**Отложено решением пользователя:** режим VPS (PCS на Ubuntu без Proxmox), yaspeed,
Telegram в интерфейсе.

---

## 19. Следующий этап: связать хосты через API

Сейчас один хаб на одном хосте Proxmox управляет своими ВМ. Нужно связать несколько хостов
(и потом, возможно, VPS). Ниже — что есть, чего не хватает и что надо решить.

### Что уже есть

- **Готовый слой операций** — `pcs/hub/api.py`: сводка, серверы, создание/удаление ВМ,
  команда любой части, DNS, пароль, ключ, mp.space, обновление, задания. Всё возвращает
  JSON-совместимые данные, ошибки — текстом.
- **HTTP API панели** (§13) — тонкий и полный: каждая функция хаба доступна по HTTP с
  конвертом `{ok, data|error}`.
- **Единый `--json` у всех команд** и служебная `pcs-node` на серверах. Хаб уже работает
  с серверами как с удалёнными узлами по SSH и разбирает их JSON.
- **Фоновые задания** с журналом и состоянием — модель для долгих операций по сети.

### Чего не хватает для машина-машина

1. **Аутентификация для программ.** Сейчас вход паролем, cookie, CSRF и Origin — это для
   браузера. Нужны токены (Bearer) с правами (чтение / действия / опасное), отзывом и
   журналом. Хранить хэши токенов в `/etc/pcs`.
2. **Транспорт.** Панель — HTTP на 666, порт открывается вручную. Для связи хостов нужно
   решить:
   - HTTPS со своим CA и mTLS между хостами, или SSH-туннели (ключи PCS уже есть), или
     WireGuard-сеть хостов;
   - кто к кому ходит: центральный хаб опрашивает хосты, или хосты сами регистрируются
     у центра.
3. **Реестр хостов.** Сейчас `/etc/pcs/servers/*.json` — только ВМ своего хоста. Нужна
   модель «хост → его серверы» и глобальная адресация, например `host-a/2000`.
4. **Версионирование API.** Префикс `/api/v1/`, версия в ответе (`VERSION` уже есть в
   `/api/session`), совместимость при разных версиях кода на хостах. `pcs update` уже
   выравнивает версию внутри хоста, между хостами — пока нет.
5. **Идемпотентность и долгие операции.** Создание ВМ и установка mp.space идут минутами.
   Нужны ключ идемпотентности на POST и опрос `/jobs/{id}` (модель есть), возможно — поток событий.
6. **Номера ВМ и модемов.** Номера ВМ уникальны только внутри своего Proxmox, номера
   модемов N — внутри сервера. Для общего обзора нужна составная адресация.
7. **Права на опасное.** «Подтверждение номером сервера» — правило для человека. Для API
   нужен отдельный класс прав или явный флаг подтверждения в запросе.

### С чего начать

1. Вынести из `pcs/web/panel.py` маршруты в отдельный слой `/api/v1` с двумя видами
   входа: сессия браузера (как сейчас) и Bearer-токен.
2. Добавить `pcs token add|list|revoke`.
3. Сделать `pcs host add|list|remove` — реестр соседних хостов (адрес, токен, отпечаток
   сертификата).
4. Агрегирующая сводка: `GET /api/v1/overview?all=1` опрашивает соседей параллельно с
   таймаутом и честно показывает недоступных.
5. Транспорт — по выбору пользователя (см. выше); проще всего начать с SSH-туннеля на
   уже существующих ключах PCS.

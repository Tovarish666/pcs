# PCS — Proxy Control Service v2

Управление сервером mobileproxy.space на хосте Proxmox: ВМ Ubuntu 24.04 с софтом mp.space, а дальше — виртуальные модемы, которые превращают готовые SOCKS5-прокси в USB-модемы Huawei для этой ВМ.

Каждое действие — отдельный маленький скрипт. Меню `pcs` их только вызывает, и любую команду можно выполнить руками.

## Установка на хост Proxmox

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/Tovarish666/pcs/main/install.sh)
```

Репозиторий уезжает в `/opt/pcs`, появляется команда `pcs`. Обновление — `pcs update`.

---

# Команды на хосте

Общее для всех: запускать от root на хосте Proxmox; чего не передали флагом — команда спросит; `--help` показывает параметры; лог в `/var/log/pcs/pcs-ГГГГММДД.log`.

## pcs vm-create — ВМ Ubuntu 24.04

Создаёт ВМ из облачного образа: root по паролю через SSH, qemu-guest-agent, предсказуемый DNS.

```bash
pcs vm-create
```

```bash
pcs vm-create --id 200 --name mpspace --cores 8 --ram 8192 --disk 50 \
              --storage local-lvm --bridge vmbr0 \
              --ip 10.0.0.50/24 --gw 10.0.0.1 --dns "1.1.1.1 8.8.8.8"
```

- `--dhcp` вместо `--ip/--gw` — адрес возьмётся у DHCP, PCS узнает его через guest agent.
- `--replace` — снести существующую ВМ с этим ID и создать заново (спросит подтверждение).
- Пароль root спрашивается вводом или берётся из `PCS_VM_PASSWORD`: в аргументах он был бы виден в `ps`.
- Созданная ВМ становится активной для остальных команд.

## pcs mp-install — софт mobileproxy.space

```bash
pcs mp-install
```

```bash
pcs mp-install --vm 200 --auth-file ~/auth.json
```

Шесть шагов: утилиты PCS на ВМ → выключение авто-обновлений apt и модули ядра для USB-модемов → `install.sh` от mp.space → `auth.mp` → `setup-modem-management.sh` от mp.space → перезагрузка, DNS и проверка служб. В конце печатается сводка для ЛК.

- `--skip-modems` — без `setup-modem-management.sh`.
- `--no-reboot` — без перезагрузки (настройки GRUB и initramfs применятся при следующей).
- Скрипты mp.space скачиваются и выполняются на самой ВМ в фоне: они трогают сеть, и обрыв SSH установку не прерывает. Их логи на ВМ в `/root/pcs-run/`.
- Повторный запуск безопасен: `install.sh` не трогает уже заданный `auth.mp`.

## pcs mp-auth — ключ mobileproxy.space

```bash
pcs mp-auth show
```

```bash
pcs mp-auth set '{"auth":"KEY:KEY","port":1800}'
```

```bash
pcs mp-auth check
```

- `set --file auth.json` — взять из файла, `set --key KEY:KEY --port 1800` — без JSON.
- `show --full` покажет ключ целиком (по умолчанию замаскирован).
- Перезапускается только `nodejs-server`: `auth.mp` читает лишь он, и соединения клиентов mproxy не рвутся.

## pcs modem-source — таблица с модемами

Источник правды — Google-таблица. PCS её только читает.

| n | real | proxy |
|---|---|---|
| 201 | 1 | 188.134.88.13:12000:modem1:пароль |
| 202 | 2 | 188.134.88.13:12002:modem2:пароль |

- **n** — номер для mobileproxy.space. Задаёт адреса: модем `192.168.n.1`, сервер `192.168.n.100`.
- **real** — октет настоящего модема за прокси. Принимает `1`, `192.168.1.1` и `192.168.1.100` — берётся третий октет. Колонки нет — `real = n`.
- **proxy** — `host:port:login:pass`. Можно и отдельными колонками `proxy_host`, `proxy_port`, `login`, `password`.
- **enabled** — необязательная: `0` выключает строку, не удаляя.

```bash
pcs modem-source set 'https://docs.google.com/spreadsheets/d/ВАШ_ID/edit#gid=0'
```

```bash
pcs modem-source
```

```bash
pcs modem-source test
```

Ссылку можно давать прямо из адресной строки браузера — PCS сам приведёт её к CSV. Таблица должна быть доступна по ссылке (или опубликована), иначе Google отдаёт HTML и PCS об этом скажет. Вместо ссылки принимается локальный файл — удобно для проверки.

При разборе PCS предупреждает про грабли сервера mp.space: номера 153–155 попадают у него в системные таблицы маршрутов, а `n` и `n+200` делят одну таблицу между собой.

## pcs modem-list — что в таблице и что уже создано

```bash
pcs modem-list
```

```bash
pcs modem-list --source ./modems.csv
```

Показывает строки таблицы вместе с состоянием: номер ВМ модема, запущена ли она, её адрес. Снизу — модемы, которые есть на хосте, но пропали из таблицы. `--local` — только своё, без обращения к таблице.

## pcs modem-template — шаблон ВМ виртуального модема

Собирается один раз, потом каждый модем будет его связанным клоном.

```bash
pcs modem-template
```

```bash
pcs modem-template --id 9000 --storage local-lvm --bridge vmbr0 \
                   --ram 256 --cores 1 --disk 4 --singbox 1.10.0
```

```bash
PCS_MODEM_PASSWORD=секрет pcs modem-template --print-user-data
```

Что внутри шаблона: Debian 13 из облачного образа, пакеты `usbip`, `dnsmasq-base`, `python3`, `iptables`, модули ядра для USB-устройства, `sing-box` и наши утилиты `vmodem`, `vmodem-api`, `vmodem-setup`. В конце ВМ чистится от следов первой загрузки, выключается и превращается в шаблон Proxmox.

- Пароль root один на все модемы: спрашивается или берётся из `PCS_MODEM_PASSWORD`, хранится в `/etc/pcs/modem/template.conf` с правами 600.
- `--print-user-data` печатает cloud-init и выходит: посмотреть глазами или собрать ВМ руками, без Proxmox и без root.
- `--vmid-base 1000` — из этого числа считаются номера ВМ модемов: модем `n` получит ВМ `1000 + n`.
- `--replace` пересобирает шаблон с нуля.

## pcs modem-add — модем из строки таблицы

```bash
pcs modem-add --n 201
```

```bash
pcs modem-add --n 201 --real 1 --proxy 1.2.3.4:15000:login:pass
```

```bash
pcs modem-add --n 201 --id 1201 --ip 10.0.0.101/24 --gw 10.0.0.1
```

Клонирует шаблон, поднимает ВМ, настраивает внутри `vmodem` и включает автозапуск. Без `--real` и `--proxy` берёт их из таблицы.

- Номер ВМ по умолчанию — `1000 + n` (меняется при сборке шаблона: `--vmid-base`).
- Адрес по умолчанию берётся по DHCP и узнаётся через guest agent; постоянный задаётся через `--ip` и `--gw`.
- `--server-ip` — адрес ВМ mobileproxy.space, которой разрешено забрать USB-устройство. По умолчанию берётся активная ВМ PCS.
- Параметры уезжают на ВМ через stdin: пароль прокси не появляется ни в `ps`, ни в логах.
- `--no-up` — только создать и настроить, не поднимать. `--replace` — пересоздать.

## pcs modem-attach — воткнуть модем в ВМ mobileproxy.space

```bash
pcs modem-attach --n 111
```

```bash
pcs modem-attach --n 111,112,113
```

```bash
pcs modem-attach --all
```

```bash
pcs modem-attach --status
```

Готовит ВМ (usbip, `vhci-hcd`, модуль на будущие загрузки), забирает USB-устройство модема по сети и ждёт, пока скрипты mp.space поднимут интерфейс с адресом `192.168.<N>.100`. Подключение держит служба `vmodem-attach@<N>`: переживает перезагрузку обеих ВМ и моргание сети. `--detach` вынимает и забывает.

## pcs modem-sync — хост по таблице, одной командой

```bash
pcs modem-sync
```

```bash
pcs modem-sync --dry-run
```

```bash
pcs modem-sync --only 112,113
```

```bash
pcs modem-sync --prune
```

Сама руками ничего не делает: смотрит таблицу и состояние хоста, показывает план и выполняет его через `pcs modem-add` и `pcs modem-attach` — то же самое, что набрать их по очереди. Гонять можно сколько угодно: готовые модемы она не трогает.

- План: **создать** (в таблице есть, ВМ нет), **запустить** (ВМ есть, но выключена), **уже готовы**, **остановить** (в таблице `enabled=0`), **нет в таблице**.
- `enabled=0` — модем вынимается из сервера и ВМ останавливается, но не удаляется.
- Удаляет только `--prune` и только то, чего в таблице нет вовсе, с отдельным вопросом на каждый модем.
- После запуска ВМ адрес перечитывается через guest agent: на DHCP он мог смениться, а сервер втыкает USB именно по адресу.
- `--no-attach` — только создать и поднять ВМ, в сервер не втыкать. `--source <ссылка>` — разовая таблица.

## pcs modem-check — проверить модемы целиком

```bash
pcs modem-check
```

```bash
pcs modem-check --n 113,114
```

```bash
pcs modem-check --rotate
```

Проходит по всей цепочке на каждый модем: ВМ модема запущена → внутри неё всё поднято (`vmodem check`: гаджет, usbipd, туннель, DHCP) → на ВМ mobileproxy.space USB воткнут, есть `192.168.<N>.100`, веб-морда `192.168.<N>.1` отвечает → внешний адрес через этот модем.

- `--quick` — без внешнего адреса, только состояние.
- `--rotate` — ещё и сменить IP той же последовательностью, какой это делает mp.space для типа 3, и дождаться, что адрес действительно сменился.
- Ничего не чинит и не меняет (кроме `--rotate`). Возвращает 1, если что-то не сошлось.

## pcs vm-use — какая ВМ активна

```bash
pcs vm-use
```

```bash
pcs vm-use 200
```

Без аргументов — список: сверху ВМ, про которые PCS знает, ниже остальные ВМ Proxmox. `--forget <id>` убирает ВМ из списка PCS, саму ВМ не трогает.

## pcs vm-fix-dns — DNS на ВМ

```bash
pcs vm-fix-dns
```

```bash
pcs vm-fix-dns --dns "1.1.1.1 8.8.8.8"
```

Нужна, если софт агрегатора или модем переписали `/etc/resolv.conf`.

## pcs update — обновить PCS

```bash
pcs update
```

---

# Утилиты на самих машинах

PCS кладёт их в `/usr/local/sbin` и вызывает по SSH, но на машине они работают и сами по себе.

## mp-auth — ключ mp.space на ВМ Ubuntu

```bash
mp-auth show
```

```bash
mp-auth set '{"auth":"KEY:KEY","port":1800}'
```

```bash
mp-auth check
```

Проверяет JSON и порт, пишет файл атомарно, хранит пять прошлых версий рядом (`auth.mp.bak.*`), после записи перезапускает `nodejs-server` и ждёт, пока тот ответит.

## pcs-fix-dns — DNS на ВМ Ubuntu

```bash
pcs-fix-dns
```

Серверы берёт из `/etc/default/pcs-dns`. Идемпотентна: systemd-resolved остаётся, DNS от DHCP выключается в netplan, в конце проверяется разрешение имён.

## vmodem — виртуальный модем целиком

Живёт на ВМ модема и поднимает всё, из чего он состоит: USB-устройство Huawei, отдачу его по сети, DHCP с DNS, туннель в прокси и веб-морду.

```bash
vmodem config set N=64 REAL=101 PROXY=1.2.3.4:15000:login:password USBIP_ALLOW=10.0.0.50
```

```bash
vmodem config show
```

```bash
vmodem up
```

```bash
vmodem up --dry-run
```

```bash
vmodem status
```

```bash
vmodem check
```

```bash
vmodem down
```

```bash
vmodem install-service
```

- `N` — номер модема для mp.space: сам модем будет `192.168.N.1`, сервер получит `192.168.N.100`.
- `REAL` — октет настоящего модема за прокси (его веб-морда `192.168.REAL.1`).
- `PROXY` — можно строкой из таблицы `host:port:login:pass`, тогда логин и пароль разложатся сами.
- `USBIP_ALLOW` — адрес ВМ с mobileproxy.space: только он сможет забрать USB-устройство.
- `--dry-run` показывает все команды и собирает конфиги, ничего не запуская.
- Пароль прокси лежит в файле с правами 600 и в командной строке не появляется.

Что делает `up`, по порядку: гаджет Huawei `12d1:14dc` с функцией ECM в configfs → адрес `192.168.N.1` на `usb0` → dnsmasq (DHCP ровно на `.100`, DNS с переадресацией на DNS настоящего модема) → sing-box (весь TCP и UDP в SOCKS5) → маршруты, NAT и MSS → `vmodem-api` → `usbipd`.

## vmodem-setup — подготовить машину к роли модема

```bash
vmodem-setup
```

```bash
vmodem-setup --check
```

```bash
vmodem-setup --dry-run --singbox-version 1.10.0
```

Загружает модули `configfs`, `libcomposite`, `usbip-vudc` и прописывает их на будущие загрузки, ставит `sing-box` нужной версии и проверяет, что на месте `usbipd`, `dnsmasq`, `python3`, `iptables`, сами `vmodem` и `vmodem-api` и виртуальный USB-контроллер. Вызывается при сборке шаблона и потом руками.

## vmodem-attach — держать модем воткнутым (на ВМ Ubuntu)

```bash
vmodem-attach add 111 192.168.88.3
```

```bash
vmodem-attach list
```

```bash
vmodem-attach check
```

`add` запоминает модем и втыкает его, `watch` (им живёт служба) держит подключение, `down` и `remove` вынимают. `usbip` ищется и отдельным пакетом, и в `linux-tools` рядом с ядром.

## vmodem-attach: внешний адрес и смена IP

```bash
vmodem-attach ip 111
```

```bash
vmodem-attach rotate 111
```

`ip` спрашивает внешний адрес через интерфейс этого модема, `rotate` меняет его той же последовательностью, какой это делает mp.space для типа 3 (выключить данные → режим сети 02 → 03 → включить данные) и ждёт, пока адрес действительно сменится. Без номера `ip` проходит по всем модемам.

## vmodem-api — веб-морда модема на виртуальном адресе

Живёт на виртуальном модеме. Слушает `192.168.<N>.1:80` и пересылает запросы настоящему модему `192.168.<real>.1` через SOCKS5, подменяя адреса в обе стороны. Опасные записи к модему не пропускает.

```bash
vmodem-api --virt 64 --real 101 --socks 10.0.0.5:1080 --socks-user user
```

```bash
vmodem-api --virt 64 --real 101 --socks 10.0.0.5:1080 --socks-user user --probe
```

- Пароль прокси — в переменной `VMODEM_SOCKS_PASS` или в файле через `--socks-pass-file`: в аргументах он виден в `ps`.
- `--probe` — разовая проверка: дойти до модема через прокси и показать его серийный номер.
- Подробности — `vmodem-api --help`.

---

# Устройство

```
pcs                 точка входа: меню и вызов команд
cmd/<команда>.sh    одно действие — один скрипт
lib/                общее: вывод и ввод, состояние, SSH, Proxmox, таблица модемов
guest/              то, что кладётся на машины (/usr/local/sbin)
  mp-auth           ключ mp.space на ВМ Ubuntu
  pcs-fix-dns       DNS на ВМ Ubuntu
  vmodem            виртуальный модем целиком: up/down/status/check
  vmodem-api        посредник HiLink API на виртуальном модеме
  vmodem-setup      подготовка ВМ к роли модема
  vmodem-attach     приём модема по USB/IP на ВМ mobileproxy.space
tests/              проверки без ВМ и без root
```

- Состояние: `/etc/pcs/mp/<vmid>.conf` (права 600), активная ВМ — `/etc/pcs/mp/active`; модемы — `/etc/pcs/modem/` (шаблон, ссылка на таблицу, по файлу на модем).
- SSH к ВМ по паролю через `SSH_ASKPASS` (OpenSSH ≥ 8.4), без ключей и лишних пакетов.
- Долгие установки идут на ВМ в фоне, PCS читает их лог.

# Разработка

```bash
bash tests/run-all.sh
```

# Виртуальный модем: как он устроен

Одна прокси — одна маленькая ВМ. Для сервера mobileproxy.space это воткнутый в USB модем Huawei E3372h (в панели — тип 3).

```
ВМ Ubuntu (mobileproxy.space)            ВМ модема
  eth* (cdc_ether) ◄══ USB/IP ══► гаджет 12d1:14dc → usb0 = 192.168.N.1/24
  192.168.N.100 по DHCP                   dnsmasq: DHCP .100, DNS :53
                                          vmodem-api: :80 → модем через SOCKS5
                                          sing-box: весь TCP и UDP → SOCKS5 → интернет
```

Базовый образ — Debian 13 (`pcs modem-template` собирает шаблон сам): ядро с `libcomposite`, `usbip-vudc` и configfs, пакеты `usbip`, `dnsmasq-base`, `python3`, `curl`, `iptables`, `sing-box` в `/usr/local/bin`. ВМ модема — 384 МБ памяти, одно ядро, 4 ГБ диска.

Со стороны Ubuntu устройство забирает `pcs modem-attach` (и держит воткнутым служба `vmodem-attach@<N>`); руками это те же две команды:

```bash
modprobe vhci-hcd && usbip attach -r <IP ВМ модема> -d usbip-vudc.0
```

**Проверено на живом стенде** (Proxmox 9, Debian 13, шесть модемов Huawei за SOCKS5):

- Ubuntu видит устройство как `12d1:14dc Huawei E3372 LTE/UMTS/GSM HiLink Modem`, драйвер `cdc_ether`.
- Скрипты mp.space поднимают его сами: `eth1…eth6 = 192.168.<N>.100`, маршрутизация по источнику; в их же `modem-manager status` — «Модем OK», «SRC-routing OK», «Активных модемов: 6», «Curl тесты: ✓ 6 / ✗ 0».
- Веб-морда на `192.168.<N>.1` отвечает, DNS модема отвечает, у каждого модема свой внешний адрес.
- Смена IP той же последовательностью, что делает mp.space для типа 3, меняет внешний адрес.
- Подключение переживает перезагрузку ВМ.
- Шесть модемов из таблицы раскатываются одной командой `pcs modem-sync` за несколько минут.

**Не работает UDP**: прокси принимает UDP ASSOCIATE, но отдаёт приватный адрес ретранслятора за NAT — снаружи он недостижим. Это чинится на стороне прокси.

Плюс 194 локальные проверки на разбор таблицы, конфиги, режим показа и заглушки.

# Дальше

UDP: прокси должен отдавать достижимый адрес ретранслятора. Со стороны PCS всё готово — `sing-box` гонит в прокси и TCP, и UDP.

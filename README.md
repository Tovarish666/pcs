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
- `--replace` пересобирает шаблон с нуля.

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
lib/                общее: вывод и ввод, состояние, SSH, Proxmox
guest/              то, что кладётся на машины (/usr/local/sbin)
  mp-auth           ключ mp.space на ВМ Ubuntu
  pcs-fix-dns       DNS на ВМ Ubuntu
  vmodem            виртуальный модем целиком: up/down/status/check
  vmodem-api        посредник HiLink API на виртуальном модеме
  vmodem-setup      подготовка ВМ к роли модема
tests/              проверки без ВМ и без root
```

- Состояние: `/etc/pcs/mp/<vmid>.conf` (права 600), активная ВМ — `/etc/pcs/mp/active`; шаблон модемов — `/etc/pcs/modem/template.conf`.
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

Базовый образ — Debian 13 (`pcs modem-template` собирает шаблон сам): ядро с `libcomposite`, `usbip-vudc` и configfs, пакеты `usbip`, `dnsmasq-base`, `python3`, `curl`, `iptables`, `sing-box` в `/usr/local/bin`. ВМ модема — 256 МБ памяти, одно ядро, 4 ГБ диска.

Со стороны Ubuntu устройство забирается так (пока вручную, потом это возьмёт на себя PCS):

```bash
modprobe vhci-hcd && usbip attach -r <IP ВМ модема> -d usbip-vudc.0
```

**Что уже проверено, а что нет.** `vmodem-api` проверен по-настоящему: на заглушках модема и SOCKS5 гоняются 20 проверок. У `vmodem` проверены конфиг и режим показа (47), у шаблона — cloud-init и `vmodem-setup` (22). Сам запуск в ядре (гаджет, USB/IP, sing-box) и сборка шаблона на живом Proxmox ещё не гонялись.

# Дальше

`pcs modem-add` — клонировать шаблон под конкретную проксю, `pcs modem-attach` — подключить модем в Ubuntu, список, проверка и разбор таблицы с проксями.

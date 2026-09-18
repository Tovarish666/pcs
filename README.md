# PCS — Proxy Control Service v2

Управление сервером mobileproxy.space на хосте Proxmox: ВМ Ubuntu 24.04 с софтом mp.space, а дальше — виртуальные модемы, которые превращают готовые SOCKS5-прокси в USB-модемы Huawei для этой ВМ.

Каждое действие — отдельный маленький скрипт в `cmd/`. Меню `pcs` их только вызывает; любую команду можно запустить напрямую.

## Установка на хост Proxmox

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/Tovarish666/pcs/main/install.sh)
```

Ставит репозиторий в `/opt/pcs` и команду `pcs`. Обновление — `pcs update`.

## Быстрый старт

```bash
pcs vm-create        # ВМ Ubuntu 24.04 (спросит ID, сеть, пароль root)
pcs mp-install       # софт mobileproxy.space + перезагрузка + проверка
pcs mp-auth set      # ключ сервера из ЛК, если не задали при установке
```

После `mp-install` в конце печатается сводка для ЛК mobileproxy.space.

## Команды

| Команда | Что делает |
|---|---|
| `pcs vm-create` | ВМ Ubuntu 24.04 из cloud image: root по паролю, guest agent, DNS |
| `pcs mp-install` | install.sh и setup-modem-management.sh от mp.space, auth.mp, проверка служб |
| `pcs mp-auth show \| set \| check` | ключ сервера (auth.mp): показать, сменить, проверить |
| `pcs vm-use [id]` | список ВМ, выбор активной |
| `pcs vm-fix-dns` | привести DNS на ВМ в порядок |
| `pcs update` | обновить PCS |

`pcs <команда> --help` — параметры. Всё, что не передано флагом, команда спросит.

## Устройство

```
pcs                 точка входа: меню и вызов команд
cmd/<команда>.sh    одно действие — один скрипт
lib/                общее: вывод и ввод, состояние, SSH, Proxmox
guest/              утилиты, которые PCS кладёт на ВМ (/usr/local/sbin)
  mp-auth           ключ mp.space на самой ВМ
  pcs-fix-dns       DNS на самой ВМ
tests/              проверки без ВМ и без root
```

- Состояние: `/etc/pcs/mp/<vmid>.conf` (права 600), активная ВМ — `/etc/pcs/mp/active`.
- Логи: `/var/log/pcs/pcs-ГГГГММДД.log`.
- SSH к ВМ по паролю через `SSH_ASKPASS` (OpenSSH ≥ 8.4), без ключей и лишних пакетов.
- Долгие установки идут на ВМ в фоне (`/root/pcs-run/`), PCS читает их лог. Обрыв SSH установку не прерывает.

## Разработка

```bash
bash tests/test-mp-auth.sh
```

## Дальше

Виртуальные модемы: отдельная маленькая ВМ на каждую прокси, которая подключается к Ubuntu как USB-модем Huawei E3372h (тип 3 в mp.space).

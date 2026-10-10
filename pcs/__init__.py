"""PCS — Proxy Control Service: хаб фермы мобильных прокси.

Части:
  pcs.core       общее для всех: команды, sing-box, маршрутизация, таблица модемов, DNS
  pcs.hub        хаб на хосте Proxmox: серверы (ВМ), меню, доступы, обновления
  pcs.proxyveth  из прокси — сетевой интерфейс (режимы usb и gw)
  pcs.modlink    из интерфейса — прокси
  pcs.hivelink   драйвер настоящих модемов
  pcs.web        веб-панель (порт 666)

Устройство и договорённости — docs/ARCHITECTURE.md.
"""

VERSION = "5.0.0-dev"

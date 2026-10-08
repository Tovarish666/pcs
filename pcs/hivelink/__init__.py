"""hivelink: драйвер настоящих USB-модемов Huawei (E3372/E3131/E353, Vodafone K5150/K5160).

Zero-CD → USB-конфигурация с rndis_host/cdc_ether → DHCP → маршрут на модем → данные
через HiLink → DKMS-фикс скорости приёма rndis_host. На хост Proxmox ставится хабом
сам, в ВМ — по запросу. Виртуальные модемы proxyveth (шина dummy_hcd) не трогает.

  usb       настоящие модемы в sysfs (фильтр dummy_hcd), режимы PID, USB-конфигурации
  route     таблицы 2000+N, правила 31000+N, уборка только своего
  hilink    web-API модема
  driver    один проход приведения к норме (hivelink.service по таймеру и udev)
  rndis     DKMS-фикс rndis_host
  install   установка, снятие, миграция со старого e3372-driver
  status    status и doctor
  cli       команда hivelink

Владелец — агент D (docs/ARCHITECTURE.md).
"""

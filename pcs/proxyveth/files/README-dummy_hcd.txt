dummy_hcd-6.8.c — drivers/usb/gadget/udc/dummy_hcd.c из ядра Linux v6.8 (git.kernel.org, тег v6.8;
sha256 6892efeaca7d13e0f8ba172fac6b9607f9ac5c60de1a5f1812a57eb7998fd993), лицензия GPL-2.0+ — как в файле.
У Ubuntu dummy_hcd не собран; proxyveth собирает его через dkms. Копия здесь — чтобы сборка не зависела
от сети (GitHub и git.kernel.org из России доступны не всегда). Подходит к ядрам Ubuntu 24.04 (6.8.0-*).

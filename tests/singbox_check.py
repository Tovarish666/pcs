"""Проверка конфигов настоящим sing-box той же версии, что ставится на серверы.

    from singbox_check import sb_check
    ok, msg = sb_check(config)        # ok=None — проверить нечем (нет сети), тест пропускается

Бинарник: $PCS_TEST_SINGBOX или скачивается один раз в ~/.cache/pcs-test/
(сумма сверяется с pcs.core.singbox.SHA256). Устаревшие поля, предупреждения
deprecated и любые ERROR/FATAL считаются провалом.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, os.pardir))
from pcs.core import singbox  # noqa: E402

_BIN = [None]


def binary():
    if _BIN[0] is None:
        path = os.environ.get("PCS_TEST_SINGBOX") or os.path.expanduser(
            "~/.cache/pcs-test/sing-box-%s-%s" % (singbox.VERSION, singbox.arch()))
        try:
            if not singbox.installed(path):
                singbox.install(path)
            _BIN[0] = path
        except Exception as e:       # нет сети / нет суммы под эту платформу
            print("  skip sing-box check: %s" % e)
            _BIN[0] = ""
    return _BIN[0]


def sb_check(config):
    b = binary()
    if not b:
        return None, "sing-box недоступен"
    return singbox.check(config, b)

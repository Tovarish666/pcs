#!/usr/bin/env python3
"""Номер SIM по USSD (pcs/core/simnum.py) на подставном модеме HiLink.

    python3 tests/test_simnum.py
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, os.pardir))
from pcs.core import simnum  # noqa: E402

passed = failed = 0


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print("  ok   %s" % name)
    else:
        failed += 1
        print("  FAIL %s%s" % (name, ("\n       " + str(detail)) if detail else ""))


ERR = '<?xml version="1.0" encoding="UTF-8"?><error><code>%s</code><message></message></error>'


class Modem:
    """Подставной HiLink: оператор, режим сети, USSD (ответ через сколько-то опросов), SMS."""

    def __init__(self, plmn="25011", mode="03", answer="Ваш номер: +7 (999) 123-45-67.", after=2, sms=None,
                 send_err=None, get_err="111019", net_err=None, imsi=None, serving=None):
        self.plmn, self.mode, self.answer, self.after = plmn, mode, answer, after
        self.imsi, self.serving = imsi, serving or plmn
        self.sms, self.send_err, self.get_err, self.net_err = dict(sms or {}), send_err, get_err, net_err
        self.calls, self.polls, self.sent = [], 0, None
        self.t = 0.0

    def clock(self):
        return self.t

    def sleep(self, s):
        self.t += s

    def get(self, path):
        self.calls.append(("GET", path))
        if path == "/api/net/current-plmn":
            return "<response><FullName>X</FullName><Numeric>%s</Numeric></response>" % self.serving
        if path == "/api/device/information":
            return "<response><Imsi>%s</Imsi></response>" % (self.imsi if self.imsi is not None else self.plmn + "0257184955")
        if path == "/api/net/net-mode":
            return "<response><NetworkMode>%s</NetworkMode><NetworkBand>3FFFFFFF</NetworkBand><LTEBand>800C5</LTEBand></response>" % self.mode
        if path == "/api/monitoring/status":
            return "<response><CurrentNetworkType>19</CurrentNetworkType><SignalIcon>4</SignalIcon><ServiceStatus>2</ServiceStatus></response>"
        if path == "/api/ussd/get":
            self.polls += 1
            if self.sent and self.polls >= self.after and self.answer is not None:
                return "<response><content>%s</content></response>" % self.answer
            return ERR % self.get_err
        return ERR % "100002"

    def post(self, path, inner):
        self.calls.append(("POST", path, inner))
        if path == "/api/net/net-mode":
            if self.net_err:
                return ERR % self.net_err
            self.mode = simnum.tag(inner, "NetworkMode")
            return "<response>OK</response>"
        if path == "/api/ussd/send":
            if self.send_err:
                return ERR % self.send_err
            self.sent = simnum.tag(inner, "content")
            if self.answer is None:         # номер придёт в SMS
                self.sms["40009"] = ("000000", "Ваш номер 89991112233")
            return "<response>OK</response>"
        if path == "/api/sms/sms-list":
            return "<response>%s</response>" % "".join(
                "<Message><Index>%s</Index><Phone>%s</Phone><Content>%s</Content></Message>" % (i, p, c)
                for i, (p, c) in self.sms.items())
        return "<response>OK</response>"


def run(m, **kw):
    return simnum.find(m.get, m.post, sleep=m.sleep, clock=m.clock, **kw)


print("номер в тексте:")
for text, want in (("Ваш номер: +7 (999) 123-45-67.", "+79991234567"), ("Номер 89031234567", "+79031234567"),
                   ("Ваш федеральный номер 9161112233", "+79161112233"), ("+7 916 111-22-33 баланс 10 р.", "+79161112233"),
                   ("Баланс 123.45 руб", None), ("код 1234567", None)):
    check("%r → %s" % (text, want), simnum.parse_number(text) == want, simnum.parse_number(text))

print("Yota, режим «только LTE»:")
m = Modem()
r = run(m)
check("номер из USSD, код по оператору (*103#)", r["number"] == "+79991234567" and r["ussd"] == "*103#"
      and r["operator"] == "Yota" and r["via"] == "ussd", r)
modes = [c[2] for c in m.calls if c[:2] == ("POST", "/api/net/net-mode")]
check("режим: авто на время USSD, потом как был (диапазоны те же)",
      [simnum.tag(x, "NetworkMode") for x in modes] == ["00", "03"] and all("800C5" in x for x in modes)
      and m.mode == "03" and r["mode_was"] == "03" and r["mode_back"] == "03", modes)
send = [i for i, c in enumerate(m.calls) if c[:2] == ("POST", "/api/ussd/send")]
check("USSD — после смены режима", send and send[0] > m.calls.index(modes and ("POST", "/api/net/net-mode", modes[0])))
check("сессия USSD закрыта", ("POST", "/api/ussd/release", "") in m.calls)

m = Modem(mode="00")
run(m)
check("уже «авто» — режим не трогаем", not any(c[:2] == ("POST", "/api/net/net-mode") for c in m.calls))
m = Modem()
run(m, keep_mode=True)
check("--keep-mode — остаётся «авто»", m.mode == "00")
m = Modem(plmn="25099")
r = run(m, ussd=" *111# ")
check("свой код — он и уходит", m.sent == "*111#" and r["ussd"] == "*111#")
check("Билайн по умолчанию — *110*10#", simnum.USSD_BY_PLMN["25099"][1] == "*110*10#")
m = Modem(plmn="25011", serving="25002")
r = run(m)
check("Yota в сети МегаФона: оператор по IMSI (25011) — *103#, не *205#", m.sent == "*103#" and r["operator"] == "Yota", r)
m = Modem(plmn="25002", imsi="")
r = run(m)
check("нет IMSI — по сети (25002 — *205#)", m.sent == "*205#", r)

print("номер в SMS:")
m = Modem(plmn="25001", answer=None, sms={"40001": ("MTS", "старое 89990000000")})
m.after = 99
r = run(m)
check("в ответе USSD нет — номер из новой SMS (старые не в счёт)", r["number"] == "+79991112233" and r["via"] == "sms", r)

print("ошибки:")
m = Modem(plmn="99999")
try:
    run(m)
    check("неизвестный оператор без кода — просьба вписать", False)
except simnum.SimFail as e:
    check("неизвестный оператор без кода — просьба вписать", "впиши" in str(e), e)
try:
    run(Modem(), ussd="*100#; rm")
    check("код USSD — только цифры, * и #", False)
except simnum.SimFail as e:
    check("код USSD — только цифры, * и #", "цифры" in str(e))
m = Modem(send_err="111022")
try:
    run(m)
    check("оператор отклонил USSD — понятная ошибка, режим вернулся", False)
except simnum.SimFail as e:
    check("оператор отклонил USSD — понятная ошибка, режим вернулся", "отклонён" in str(e) and m.mode == "03", (e, m.mode))
m = Modem(answer=None, sms={})
m.after, m.answer = 99, None
m.post_orig = m.post
m.post = lambda path, inner: (m.post_orig(path, inner) if path != "/api/ussd/send" else (setattr(m, "sent", "*103#") or "<response>OK</response>"))
try:
    run(m)
    check("ни ответа, ни SMS — «ответа нет за 40 с»", False)
except simnum.SimFail as e:
    check("ни ответа, ни SMS — «ответа нет за 40 с»", "ответа нет" in str(e), e)
m = Modem(net_err="100002")
r = run(m)
check("режим сети не меняется (прошивка) — USSD всё равно", r["number"] and r["mode_back"] is None, r)

print("\nитого: ok %d, fail %d" % (passed, failed))
sys.exit(1 if failed else 0)

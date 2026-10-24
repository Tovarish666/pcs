"""Номер SIM в модеме Huawei HiLink — по USSD (proxyveth number N, кнопка «узнать номер»).

По номеру определяют, какой модем какой: SIM переезжают, а таблица — по номерам.
Порядок:
1. оператор SIM — по IMSI (первые 5 цифр — MCC+MNC «домашней» сети), а не по сети, где модем
   сейчас: Yota сидит в сети МегаФона (current-plmn 25002), а код у неё свой (*103#; на *205#
   — «Неверная команда», SpbVL-1, 24.10). Нет IMSI — по current-plmn. Код USSD — заданный или
   по оператору (USSD_BY_PLMN);
2. режим сети → «авто» (00): в «только LTE» (03, его ставит mp.space) USSD у многих
   операторов не проходит — ему нужен откат в 3G/2G (CSFB);
3. /api/ussd/send, ответ — /api/ussd/get (пока не придёт, до USSD_WAIT);
4. номера в ответе нет («придёт в SMS») — новые SMS во входящих, до SMS_WAIT;
5. режим сети — как был (keep_mode — оставить «авто»).

Доступ к модему — снаружи: get(path) → текст ответа, post(path, inner) → текст ответа
(ошибка — исключение). Так один код у proxyveth (модем за прокси) и hivelink (модем на USB).
"""
import re
import time

# PLMN → (оператор, USSD «мой номер»). Своё — в окне «узнать номер» или --ussd.
USSD_BY_PLMN = {
    "25001": ("МТС", "*111*0887#"),
    "25002": ("МегаФон", "*205#"),
    "25099": ("Билайн", "*110*10#"),
    "25020": ("Tele2", "*201#"),
    "25011": ("Yota", "*103#"),
    "25035": ("Мотив", "*104#"),
}
USSD_WAIT = 40
SMS_WAIT = 45
NET_WAIT = 60
NUM_RE = re.compile(r"(?<![\d+])(?:\+?7|8)?[\s\-()]*(9\d{2})[\s\-()]*(\d{3})[\s\-]*(\d{2})[\s\-]*(\d{2})(?!\d)")
ERRORS = {"111019": "оператор не ответил на USSD", "111020": "сеть не приняла USSD",
          "111001": "ошибка USSD", "111012": "USSD занят — повтори позже", "111022": "USSD отклонён",
          "100002": "модем не знает USSD", "100003": "нет прав (вход в веб модема)"}


class SimFail(Exception):
    pass


def tag(text, name):
    m = re.search(r"<%s>([^<]*)</%s>" % (name, name), text or "")
    return m.group(1).strip() if m else None


def error_of(text):
    """Код ошибки HiLink в ответе или None."""
    if "<error>" not in (text or ""):
        return None
    return tag(text, "code") or "?"


def parse_number(text):
    """Российский мобильный номер из текста ответа: «+7 (903) 123-45-67», «89031234567»,
    «9031234567» → «+79031234567». None — нет."""
    m = NUM_RE.search(text or "")
    return "+7" + "".join(m.groups()) if m else None


def operator(get):
    """(MCC+MNC оператора SIM, его название)."""
    try:
        imsi = tag(get("/api/device/information"), "Imsi") or ""
    except Exception:
        imsi = ""
    t = get("/api/net/current-plmn")
    plmn = imsi[:5] if re.fullmatch(r"\d{6,15}", imsi) else (tag(t, "Numeric") or "")
    return plmn, USSD_BY_PLMN.get(plmn, (tag(t, "FullName") or tag(t, "ShortName") or plmn or "?", None))[0]


def sms_inbox(get, post):
    """{Index: (телефон, текст)} входящих (первые 50)."""
    t = post("/api/sms/sms-list", "<PageIndex>1</PageIndex><ReadCount>50</ReadCount><BoxType>1</BoxType>"
             "<SortType>0</SortType><Ascending>0</Ascending><UnreadPreferred>1</UnreadPreferred>")
    out = {}
    for m in re.finditer(r"<Message>(.*?)</Message>", t or "", re.S):
        x = m.group(1)
        out[tag(x, "Index") or ""] = (tag(x, "Phone") or "", tag(x, "Content") or "")
    return out


def find(get, post, ussd=None, keep_mode=False, sleep=time.sleep, clock=time.time, log=lambda m: None):
    """→ {"number", "operator", "plmn", "ussd", "text", "via", "mode_was", "mode_back"}. Ошибка — SimFail."""
    plmn, opname = operator(get)
    code = (ussd or "").strip() or (USSD_BY_PLMN.get(plmn) or (None, None))[1]
    if not code:
        raise SimFail("оператор %s (%s): своего кода USSD нет — впиши его (например *100#)" % (opname, plmn or "?"))
    if not re.fullmatch(r"[*#0-9]{2,40}", code):
        raise SimFail("код USSD — цифры, * и #: например *103#")
    res = {"number": None, "operator": opname, "plmn": plmn, "ussd": code, "text": "", "via": None,
           "mode_was": None, "mode_back": None}

    nm = get("/api/net/net-mode")
    mode, band, lte = tag(nm, "NetworkMode"), tag(nm, "NetworkBand") or "3FFFFFFF", tag(nm, "LTEBand") or "7FFFFFFFFFFFFFFF"
    res["mode_was"] = mode
    changed = False
    if mode and mode != "00" and not error_of(nm):
        log("режим сети %s → авто (00): USSD нужен откат в 3G/2G" % mode)
        r = post("/api/net/net-mode", "<NetworkMode>00</NetworkMode><NetworkBand>%s</NetworkBand><LTEBand>%s</LTEBand>"
                 % (band, lte))
        changed = not error_of(r)
        if not changed:
            log("⚠ режим сети не сменился (%s) — пробую USSD так" % ERRORS.get(error_of(r), "ошибка %s" % error_of(r)))
        end = clock() + (NET_WAIT if changed else 0)
        sleep(5)
        while clock() < end:            # обслуживание есть (ServiceStatus 2) — можно USSD
            st = get("/api/monitoring/status")
            if tag(st, "ServiceStatus") == "2" and (tag(st, "CurrentNetworkType") or "0") not in ("0", ""):
                break
            sleep(2)
    try:
        try:
            before = set(sms_inbox(get, post))
        except Exception:
            before = None               # SMS у прошивки нет — обойдёмся USSD
        log("USSD %s (%s)" % (code, opname))
        r = post("/api/ussd/send", "<content>%s</content><codeType>CodeType</codeType><timeout></timeout>" % code)
        if error_of(r):
            raise SimFail("USSD %s: %s" % (code, ERRORS.get(error_of(r), "ошибка %s" % error_of(r))))
        end, last = clock() + USSD_WAIT, None
        while clock() < end:
            sleep(2)
            t = get("/api/ussd/get")
            err = error_of(t)
            if not err:
                res["text"] = tag(t, "content") or ""
                break
            last = err
        else:
            if last not in (None, "111019"):
                raise SimFail("USSD %s: %s" % (code, ERRORS.get(last, "ошибка %s" % last)))
        try:
            post("/api/ussd/release", "")
        except Exception:
            pass
        res["number"] = parse_number(res["text"])
        if res["number"]:
            res["via"] = "ussd"
        elif before is not None:
            log("в ответе USSD номера нет — жду SMS")
            end = clock() + SMS_WAIT
            while clock() < end and not res["number"]:
                sleep(3)
                for idx, (phone, text) in sms_inbox(get, post).items():
                    if idx in before:
                        continue
                    num = parse_number(text)
                    if num:
                        res.update(number=num, via="sms", text=(res["text"] + "\n" if res["text"] else "") + text)
                        break
        if not res["text"] and not res["number"]:
            raise SimFail("USSD %s: ответа нет за %d с" % (code, USSD_WAIT))
    finally:
        if changed and not keep_mode:
            try:
                post("/api/net/net-mode", "<NetworkMode>%s</NetworkMode><NetworkBand>%s</NetworkBand><LTEBand>%s</LTEBand>"
                     % (mode, band, lte))
                res["mode_back"] = mode
            except Exception as e:
                log("⚠ режим сети не вернулся к %s: %s" % (mode, e))
    return res

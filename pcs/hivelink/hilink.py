"""hivelink: web-API HiLink модема (192.168.N.1).

Прошивки этого поколения требуют сессию: GET /api/webserver/SesTokInfo →
Cookie: SessionID=… + заголовок __RequestVerificationToken. Без неё любой запрос
отдаёт <error><code>125002</code>. Соединение привязывается к адресу модема на
этой машине (192.168.N.100): тогда запрос идёт именно в этот модем.
"""
import http.client
import re
import socket
import ssl

CONN = {900: "подключается", 901: "онлайн", 902: "отключён", 903: "отключается",
        904: "сбой", 905: "сбой сети", 906: "сбой роуминга", 907: "нет сети"}
ERRORS = {125002: "сессия не принята", 125003: "токен не принят", 100002: "не поддерживается прошивкой",
          100003: "нужен вход в веб-морду модема", 108003: "вход занят другим"}
EXT_IP = (("api.ipify.org", "/"), ("ifconfig.me", "/ip"))


class HiFail(Exception):
    pass


def xmlval(text, tag):
    m = re.search(r"<%s>([^<]*)</%s>" % (tag, tag), text or "")
    return m.group(1).strip() if m else None


def num(text, tag):
    v = xmlval(text, tag)
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def parse_session(text):
    """(cookie, токен) из ответа SesTokInfo; (None, None) — сессии не дали."""
    sid, tok = xmlval(text, "SesInfo"), xmlval(text, "TokInfo")
    if not sid:
        return None, None
    return (sid if sid.startswith("SessionID=") else "SessionID=" + sid), tok


def api_error(text):
    """Код ошибки web-API или None."""
    if "<error>" not in (text or ""):
        return None
    return num(text, "code") or -1


def error_text(code):
    return "ошибка web-API %s%s" % (code, (" (%s)" % ERRORS[code]) if code in ERRORS else "")


def is_ok(text):
    return re.search(r"<response>\s*OK\s*</response>", text or "") is not None


def conn_text(code):
    if code is None:
        return "нет ответа"
    if 112 <= code <= 115:
        return "нет регистрации"
    return CONN.get(code, str(code))


def net_text(code):
    """CurrentNetworkType(Ex) → 2G/3G/LTE."""
    if code is None:
        return None
    if code == 0:
        return "нет"
    if code in (19, 101, 1011):
        return "LTE"
    if 1 <= code <= 3 or 21 <= code <= 23:
        return "2G"
    if 4 <= code <= 18 or 41 <= code <= 65:
        return "3G"
    return str(code)


def parse_status(text):
    """Ответ /api/monitoring/status → {conn, net, signal, sim, error}. sim — SimStatus: 1 — SIM на месте."""
    err = api_error(text)
    net = num(text, "CurrentNetworkTypeEx")
    if net is None:
        net = num(text, "CurrentNetworkType")
    return {"conn": num(text, "ConnectionStatus"), "net": net_text(net),
            "signal": num(text, "SignalIcon"), "sim": num(text, "SimStatus"), "error": err}


def no_registration(code):
    return code == 907 or (code is not None and 112 <= code <= 115)


class Api:
    port = 80

    def __init__(self, n, src=None, timeout=5):
        self.host = "192.168.%d.1" % n
        self.src = src
        self.timeout = timeout
        self.cookie = self.token = None

    def _http(self, method, path, body=None, headers=None):
        c = http.client.HTTPConnection(self.host, self.port, timeout=self.timeout,
                                       source_address=(self.src, 0) if self.src else None)
        try:
            c.request(method, path, body=body, headers=headers or {})
            r = c.getresponse()
            return r.read(65536).decode("utf-8", "replace")
        except (OSError, http.client.HTTPException) as e:
            raise HiFail("web-API %s не отвечает (%s)" % (self.host, e.__class__.__name__))
        finally:
            c.close()

    def login(self):
        self.cookie, self.token = parse_session(self._http("GET", "/api/webserver/SesTokInfo"))
        if not self.cookie:
            raise HiFail("web-API %s: сессию не дали" % self.host)
        return self

    def _headers(self, post=False):
        h = {"Cookie": self.cookie or "", "__RequestVerificationToken": self.token or ""}
        if post:
            h["Content-Type"] = "application/xml; charset=UTF-8"
        return h

    def get(self, path):
        if not self.cookie:
            self.login()
        return self._http("GET", path, headers=self._headers())

    def post(self, path, inner):
        """POST с новой сессией: часть прошивок принимает токен только один раз."""
        self.login()
        body = '<?xml version="1.0" encoding="UTF-8"?><request>%s</request>' % inner
        text = self._http("POST", path, body=body.encode(), headers=self._headers(post=True))
        if not is_ok(text):
            code = api_error(text)
            raise HiFail(error_text(code) if code else "web-API %s: неожиданный ответ" % self.host)
        return text

    def status(self):
        return parse_status(self.get("/api/monitoring/status"))

    def dataswitch(self):
        return num(self.get("/api/dialup/mobile-dataswitch"), "dataswitch")

    def set_data(self, on=True):
        self.post("/api/dialup/mobile-dataswitch", "<dataswitch>%d</dataswitch>" % (1 if on else 0))

    def dial(self, on=True):
        self.post("/api/dialup/dial", "<Action>%d</Action>" % (1 if on else 0))

    def net_mode(self):
        """Режим сети и диапазоны: (NetworkMode, NetworkBand, LTEBand); нет у прошивки — HiFail."""
        text = self.get("/api/net/net-mode")
        if api_error(text):
            raise HiFail(error_text(api_error(text)))
        return (xmlval(text, "NetworkMode") or "00", xmlval(text, "NetworkBand") or "3FFFFFFF",
                xmlval(text, "LTEBand") or "7FFFFFFFFFFFFFFF")

    def set_net_mode(self, mode, band, lte):
        self.post("/api/net/net-mode", "<NetworkMode>%s</NetworkMode><NetworkBand>%s</NetworkBand>"
                  "<LTEBand>%s</LTEBand>" % (mode, band, lte))

    def reboot(self):
        self.post("/api/device/control", "<Control>1</Control>")

    def radio(self):
        """Жива ли радиочасть: {"imei", "sim"} (SimState 255 — SIM не видна) или None — прошивка
        не говорит. Веб модема может работать, когда сам модем внутри завис: IMEI пуст, SIM «нет»,
        перезагрузку принимает («OK»), но не делает (SpbVL-2, 24.10)."""
        info = self.get("/api/device/information")
        if api_error(info) or "<Imei>" not in info:
            return None                 # без входа не отдаёт — не знаем
        return {"imei": xmlval(info, "Imei") or "", "sim": num(self.get("/api/monitoring/converged-status"), "SimState")}


def radio_dead(r):
    """Радиочасть не поднялась: IMEI пуст (у живого модема он есть и без SIM)."""
    return bool(r) and not r["imei"]


def reach(src, host="1.1.1.1", port=80, timeout=6):
    """Ходит ли трафик через модем: TCP-соединение с адреса модема (без полезной нагрузки)."""
    try:
        socket.create_connection((host, port), timeout=timeout, source_address=(src, 0)).close()
        return True
    except OSError:
        return False


def probe(n, src, timeout=5):
    """Состояние модема для status: {conn, dataswitch, net, signal, errors}."""
    out = {"conn": None, "dataswitch": None, "net": None, "signal": None, "errors": []}
    api = Api(n, src, timeout)
    try:
        api.login()
        st = api.status()
        out.update(conn=st["conn"], net=st["net"], signal=st["signal"])
        if st["error"]:
            out["errors"].append(error_text(st["error"]))
        out["dataswitch"] = api.dataswitch()
        if out["conn"] != 901 and radio_dead(api.radio()):
            out["radio_dead"] = True
            out["errors"].append("модем завис: SIM не видна, IMEI пуст — перезагрузку не выполняет, нужно снять питание")
    except HiFail as e:
        out["errors"].append(str(e))
    return out


def ext_ip(src, timeout=6):
    """Внешний IP, если выйти в интернет с адреса модема; None — не вышло."""
    for host, path in EXT_IP:
        c = http.client.HTTPSConnection(host, 443, timeout=timeout, source_address=(src, 0),
                                        context=ssl.create_default_context())
        try:
            c.request("GET", path, headers={"User-Agent": "curl/8"})
            r = c.getresponse()
            text = r.read(200).decode("ascii", "replace").strip()
            if r.status == 200 and re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}", text):
                return text
        except (OSError, http.client.HTTPException, socket.timeout):
            pass
        finally:
            c.close()
    return None

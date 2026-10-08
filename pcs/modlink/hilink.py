"""Клиент веб-API Huawei HiLink (E3372h и родня) и внешний IP — только http.client.

Настоящий модем и веб-морда модема proxyveth отвечают одинаково: морда пересылает
запросы настоящему модему. Каждый POST — со свежей парой сессия/токен из SesTokInfo:
токен одноразовый, а прошивки по-разному отдают следующий.
"""
import base64
import http.client
import ipaddress
import re
import time
import urllib.parse

from ..core.util import Fail

STATUS = {"900": "подключается", "901": "подключён", "902": "отключён", "903": "отключается"}
ERRORS = {
    "100002": "модем не знает этой команды",
    "100003": "нет прав — веб-морда модема просит вход или запрос запрещён",
    "100004": "модем занят — повтори позже",
    "108003": "в веб-морду уже вошли с другого места",
    "112003": "SIM не готова",
    "113018": "модем занят переключением",
    "125001": "токен не принят", "125002": "сессия устарела", "125003": "сессия устарела",
}
RETRY_CODES = ("125001", "125002", "125003")
IP_URLS = ["http://api.ipify.org/", "http://ipv4.icanhazip.com/", "http://checkip.amazonaws.com/"]


class Error(Fail):
    pass


def tags(xml):
    """Плоский XML ответа HiLink → {тег: текст}. Без XML-парсера: ответы простые."""
    return {k: v.strip() for k, v in re.findall(r"<(\w+)>([^<]*)</\1>", xml or "")}


class HiLink:
    pause = staticmethod(time.sleep)

    def __init__(self, ip, port=80, timeout=8):
        self.ip, self.port, self.timeout = ip, port, timeout

    def _http(self, method, path, body=None, headers=None):
        c = http.client.HTTPConnection(self.ip, self.port, timeout=self.timeout)
        try:
            c.request(method, path, body=body, headers=headers or {})
            r = c.getresponse()
            text = r.read(256 * 1024).decode("utf-8", "replace")
        except (OSError, http.client.HTTPException) as e:
            raise Error("модем %s не отвечает: %s" % (self.ip, e))
        finally:
            c.close()
        if r.status != 200:
            raise Error("модем %s: HTTP %d на %s" % (self.ip, r.status, path))
        t = tags(text)
        if "<error>" in text:
            code = t.get("code", "?")
            raise Error("модем %s: %s (код %s)" % (self.ip, ERRORS.get(code, t.get("message") or "ошибка"), code))
        return t

    def session(self):
        t = self._http("GET", "/api/webserver/SesTokInfo")
        ses, tok = t.get("SesInfo"), t.get("TokInfo")
        if not ses or not tok:
            raise Error("модем %s: нет сессии в ответе — это точно HiLink?" % self.ip)
        return (ses if ses.startswith("SessionID=") else "SessionID=" + ses), tok

    def get(self, path):
        ses, _ = self.session()
        return self._http("GET", path, headers={"Cookie": ses})

    def post(self, path, inner):
        body = "<?xml version=\"1.0\" encoding=\"UTF-8\"?><request>%s</request>" % inner
        for attempt in (1, 2):
            ses, tok = self.session()
            try:
                return self._http("POST", path, body=body.encode(), headers={
                    "Cookie": ses, "__RequestVerificationToken": tok,
                    "Content-Type": "text/xml; charset=UTF-8"})
            except Error as e:
                if attempt == 2 or not any("код %s" % c in str(e) for c in RETRY_CODES):
                    raise

    def status(self):
        return self.get("/api/monitoring/status")

    def connected(self):
        return self.status().get("ConnectionStatus") == "901"

    def data(self, on):
        self.post("/api/dialup/mobile-dataswitch", "<dataswitch>%d</dataswitch>" % (1 if on else 0))

    def net_mode(self, mode, band, lte):
        self.post("/api/net/net-mode", "<NetworkMode>%s</NetworkMode><NetworkBand>%s</NetworkBand>"
                  "<LTEBand>%s</LTEBand>" % (mode, band, lte))

    def reconnect(self, wait=60):
        """Смена IP: передача данных выкл → режим сети туда-обратно → вкл, ждём «подключён».

        Режим сети модем перерегистрирует в сети оператора — без этого оператор часто
        отдаёт тот же адрес. Исходный режим и диапазоны возвращаются как были.
        """
        try:
            m = self.get("/api/net/net-mode")
        except Error:
            m = {}
        orig = m.get("NetworkMode") or "03"
        band = m.get("NetworkBand") or "3FFFFFFF"
        lte = m.get("LTEBand") or "7FFFFFFFFFFFFFFF"
        self.data(False)
        self.pause(2)
        try:
            self.net_mode("02" if orig != "02" else "03", band, lte)
            self.pause(2)
            self.net_mode(orig, band, lte)
            self.pause(1)
        except Error:
            pass                    # не у всех прошивок есть net-mode — хватит передачи данных
        self.data(True)
        last = ""
        for _ in range(wait):
            try:
                st = self.status().get("ConnectionStatus", "")
                if st == "901":
                    return True
                last = STATUS.get(st, st)
            except Error as e:
                last = str(e)
            self.pause(1)
        raise Error("модем %s не подключился за %d с (%s)" % (self.ip, wait, last or "нет ответа"))

    def reboot(self):
        self.post("/api/device/control", "<Control>1</Control>")


def _get(url, source=None, proxy=None, timeout=8):
    u = urllib.parse.urlsplit(url)
    host, port = u.hostname, u.port or 80
    path = (u.path or "/") + ("?" + u.query if u.query else "")
    if proxy:
        ph, pp, login, pw = proxy
        c = http.client.HTTPConnection(ph, pp, timeout=timeout)
        auth = base64.b64encode(("%s:%s" % (login, pw)).encode()).decode()
        c.set_tunnel(host, port, headers={"Proxy-Authorization": "Basic " + auth})
    else:
        c = http.client.HTTPConnection(host, port, timeout=timeout,
                                       source_address=(source, 0) if source else None)
    try:
        c.request("GET", path, headers={"User-Agent": "curl/8.5.0", "Accept": "text/plain"})
        r = c.getresponse()
        body = r.read(4096).decode("ascii", "replace").strip()
    finally:
        c.close()
    if r.status != 200:
        raise OSError("HTTP %d" % r.status)
    return body


def ext_ip(source=None, proxy=None, timeout=8):
    """Внешний IP: с привязкой к адресу (source=lan_ip — через модем) или через прокси
    (proxy=(хост, порт, логин, пароль) — CONNECT с Basic-авторизацией)."""
    err = "нет ответа"
    for url in IP_URLS:
        try:
            body = _get(url, source=source, proxy=proxy, timeout=timeout)
            return str(ipaddress.IPv4Address(body.split()[0] if body else ""))
        except (OSError, http.client.HTTPException, ValueError, IndexError) as e:
            err = str(e) or e.__class__.__name__
            if "407" in err:
                err = "прокси не принял логин/пароль (407)"
                break
    raise Fail("внешний IP не узнать: %s" % err)

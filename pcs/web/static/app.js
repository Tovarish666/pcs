// Панель PCS: хост → ВМ, proxyveth, modlink, терминал, настройка. Без сборки и без CDN.
// Данные — только через textContent (никакого innerHTML с данными).
"use strict";
(function () {

  // ── DOM ───────────────────────────────────────────────────────────────────
  const $ = (sel, root) => (root || document).querySelector(sel);

  function el(tag, attrs, ...kids) {
    const e = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v == null || v === false) continue;
      if (k === "class") e.className = v;
      else if (k === "text") e.textContent = v;
      else if (k.startsWith("on") && typeof v === "function") e.addEventListener(k.slice(2), v);
      else if (typeof v !== "string" && k in e) e[k] = v;
      else e.setAttribute(k, v === true ? "" : String(v));
    }
    for (const k of kids.flat(Infinity)) {
      if (k == null || k === false) continue;
      e.append(k instanceof Node ? k : String(k));
    }
    return e;
  }
  const btn = (text, onclick, cls, title) => el("button", {type: "button", class: cls || "", title: title || null, onclick}, text);
  const link = (text, href, cls) => el("a", {href, class: cls || "btn"}, text);

  // ── API ───────────────────────────────────────────────────────────────────
  const S = {session: null, ov: null, ovErr: null, page: null};

  async function api(method, path, body) {
    const opt = {method, credentials: "same-origin", headers: {}};
    if (method === "POST") {
      opt.headers["Content-Type"] = "application/json";
      opt.headers["X-CSRF"] = S.session ? S.session.csrf : "";
      opt.body = JSON.stringify(body || {});
    }
    // GET безопасно повторить: связь моргнула — ещё раз (до 4 попыток), а не ошибка на экране
    const tries = method === "GET" ? 4 : 1;
    for (let i = 0; ; i++) {
      let r, text;
      try { r = await fetch(path, opt); } catch (e) {
        if (i + 1 < tries) { await sleep(1000 * (i + 1)); continue; }
        return {ok: false, net: true, error: method === "GET" ? "панель не отвечает — сеть или pcs-web остановлена"
          : "связь с панелью прервалась — действие могло выполниться: «Обновить» или «Фоновые задания» на странице хоста"};
      }
      if (r.status === 401) { location.href = "/login"; return {ok: false, error: "нужен вход"}; }
      try { text = await r.text(); } catch (e) {
        if (i + 1 < tries) { await sleep(1000 * (i + 1)); continue; }
        return {ok: false, net: true, error: "связь оборвалась посреди ответа (HTTP " + r.status + ")" +
          (method === "POST" ? " — действие могло выполниться: «Обновить»" : "")};
      }
      try { return JSON.parse(text); } catch (e) {
        return {ok: false, error: "ответ панели не JSON (HTTP " + r.status + "): " + text.slice(0, 120)};
      }
    }
  }
  const sleep = (ms) => new Promise((res) => setTimeout(res, ms));
  // Долгое панель запускает заданием хаба: ждём его здесь. Обрыв связи — не беда: задание
  // идёт на хосте, а мы спрашиваем снова, пока не узнаем итог.
  let ACT_RES = null;
  async function waitJob(jid, base) {
    const res = ACT_RES;
    for (let miss = 0; ;) {
      const r = await GET((base || "/api") + "/jobs/" + encodeURIComponent(jid));
      if (!r.ok) {
        if (r.net && ++miss < 200) {
          if (res) setRes(res, "run", "связь прервалась — задание идёт на хосте, жду…");
          await sleep(3000); continue;
        }
        return {ok: false, error: r.error + " (задание " + jid + ": #/job/" + jid + ")"};
      }
      miss = 0;
      const j = r.data || {};
      if (j.state === "running" || j.state === "queued") {
        const last = (j.log || []).filter((l) => l.trim()).slice(-1)[0];
        if (res) setRes(res, "run", "идёт" + (last ? ": " + last.trim().slice(0, 90) : "…"));
        await sleep(1500); continue;
      }
      if (j.state === "done") return {ok: true, data: j.result, job: jid};
      return {ok: false, error: ((j.result && j.result.error) || j.error || "задание не удалось"), job: jid, log: j.log};
    }
  }
  const GET = (p) => api("GET", p);
  const POST = (p, b) => api("POST", p, b);
  const runAt = async (base, target, part, args, extra) => {
    const r = await POST(base + "/run", Object.assign({target: String(target), part, args}, extra || {}));
    if (r && r.ok && r.data && r.data.job != null && Object.keys(r.data).length === 1) return waitJob(r.data.job, base);
    return r;
  };
  const run = (target, part, args, extra) => runAt("/api", target, part, args, extra);

  // ── формат ────────────────────────────────────────────────────────────────
  const MODE = {usb: "USB", gw: "шлюз"};
  const modeShort = (m) => MODE[m] || m || "—";
  const modeLong = (m) => m === "usb" ? "proxyveth USB" : m === "gw" ? "proxyveth шлюз" : "proxyveth: режим не задан";
  const VM_STATE = {running: "работает", stopped: "остановлена", unreachable: "нет связи", creating: "создаётся",
    error: "ошибка", paused: "на паузе", absent: "нет в Proxmox — убрать из PCS", offline: "нет SSH", old: "старый код — обновить"};
  const BAD_VM = ["stopped", "unreachable", "error", "down", "broken", "offline", "failed", "absent"];
  const ROW_STATE = {ok: ["ok", "работает"], warn: ["warn", "апстрим"], broken: ["bad", "сломан"],
    absent: ["", "нет"], disabled: ["", "выключен"], stopped: ["", "остановлен"], rebooting: ["info", "перезагрузка"]};

  function mpText(v) {
    if (v && typeof v === "object") v = v.running ? "running" : v.installed ? "installed" : null;
    if (v === true || v === "running" || v === "ok" || v === "active") return "mp.space работает";
    if (v === "installed") return "mp.space установлен";
    if (v === "stopped" || v === "inactive" || v === "failed") return "mp.space не работает";
    if (!v || v === "absent" || v === "none") return "без mp.space";
    return "mp.space: " + v;
  }
  function counts(s) {
    const m = (s && s.modems) || {};
    return {ok: +m.ok || 0, warn: +m.warn || 0, broken: +m.broken || 0, total: +m.total || 0};
  }
  function level(s) {
    const st = String(s.state || "").toLowerCase();
    if (BAD_VM.includes(st)) return "bad";
    const c = counts(s);
    if (c.broken > 0) return "bad";
    if (st && st !== "running" && st !== "ok") return "warn";
    if (c.warn > 0 || c.ok < c.total) return "warn";
    return "ok";
  }
  function levelText(s) {
    const st = String(s.state || "").toLowerCase();
    if (st && st !== "running" && st !== "ok") return VM_STATE[st] || st;
    const c = counts(s);
    if (c.broken) return "сломано модемов: " + c.broken;
    if (c.warn) return "проблемы у модемов: " + c.warn;
    if (c.ok < c.total) return "работают не все модемы";
    return "работает";
  }
  // Цвет ВМ — по месту в списке хоста: соседние ВМ всегда заметно разного цвета, красного нет
  // (красный — это «сломано»). Новые ВМ обычно с большим номером — цвета старых не меняются.
  const PALETTE = [210, 145, 275, 30, 185, 320, 95, 245, 55, 165];
  // 7 цветов на выбор (сервер и присоединённый хост): красный … фиолетовый
  const COLORS = [["red", "красный", 0], ["orange", "оранжевый", 28], ["yellow", "жёлтый", 50], ["green", "зелёный", 140],
    ["cyan", "голубой", 188], ["blue", "синий", 218], ["violet", "фиолетовый", 272]];
  function hueOf(e, X) {
    const c = e && COLORS.find((x) => x[0] === e.color);
    if (c) return c[2];
    const ids = ((((X ? X.ov() : S.ov) || {}).servers) || []).map((s) => Number(s.id)).sort((a, b) => a - b);
    const i = ids.indexOf(Number(e && e.id));
    return PALETTE[(i >= 0 ? i : Number(e && e.id) || 0) % PALETTE.length];
  }
  function colorPick(cur, onPick) {
    const box = el("span", {class: "colors"});
    const draw = (c) => box.replaceChildren(...COLORS.map(([k, name, h]) => el("button", {type: "button", class: "sw" + (c === k ? " on" : ""),
      title: name, style: "--h:" + h, onclick: () => onPick(k).then((ok) => { if (ok) draw(k); })})),
      el("button", {type: "button", class: "sw none" + (!c ? " on" : ""), title: "по номеру (без своего цвета)", text: "×",
        onclick: () => onPick("").then((ok) => { if (ok) draw(""); })}));
    draw(cur || "");
    return box;
  }
  function hue(id) {
    const ids = ((S.ov && S.ov.servers) || []).map((s) => Number(s.id)).sort((a, b) => a - b);
    const i = ids.indexOf(Number(id));
    return PALETTE[(i >= 0 ? i : Number(id)) % PALETTE.length];
  }
  function plural(n, one, few, many) {
    const a = Math.abs(n) % 100, b = a % 10;
    if (a > 10 && a < 20) return many;
    if (b > 1 && b < 5) return few;
    if (b === 1) return one;
    return many;
  }
  function ago(ts) {
    if (!ts) return "—";
    const d = Math.max(0, Date.now() / 1000 - Number(ts));
    if (d < 60) return "только что";
    if (d < 3600) return Math.floor(d / 60) + " мин назад";
    if (d < 86400) return Math.floor(d / 3600) + " ч назад";
    return new Date(ts * 1000).toLocaleString("ru-RU");
  }
  function gb(x) {
    if (!x || typeof x !== "object") return x == null ? "—" : String(x);
    let used = x.used_gb, total = x.total_gb;
    if (total == null && x.total != null) { used = x.used / 2 ** 30; total = x.total / 2 ** 30; }
    if (total == null) return "—";
    const f = (v) => (v >= 100 ? Math.round(v) : Math.round(v * 10) / 10);
    return f(+used || 0) + " / " + f(+total) + " ГБ";
  }
  const KEYS = {ext_ip: "внешний IP", hilink: "HiLink", ms: "мс", installed: "установлен", running: "работает",
    version: "версия", auth_mp: "auth.mp", service: "демон", sing_box: "sing-box", proxies: "прокси",
    enabled: "включено", created: "создано", recreated: "пересоздано", removed: "снято", failed: "не вышло",
    rows: "строк", saved: "сохранено", dns: "DNS", fingerprint: "отпечаток", added: "добавлен",
    deleted: "удалена", mode: "режим", id: "№", restarted: "перезапущен", check: "проверка", config: "конфиг",
    checked: "проверено", disabled: "выключены", invalid: "брак", problems: "замечаний", target: "где",
    sb: "sing-box", daemon: "демон", unit: "юнит", pending: "неприменённые правки"};
  function fmtVal(v) {
    if (v === true) return "да";
    if (v === false) return "нет";
    if (v == null || v === "") return "—";
    if (Array.isArray(v)) return v.length ? v.join(", ") : "—";
    if (typeof v === "object") {
      const ks = Object.keys(v);
      return ks.length ? ks.map((k) => k + " " + fmtVal(v[k])).join("; ") : "—";
    }
    return String(v);
  }
  function summary(d, skip) {
    if (d == null) return "";
    if (typeof d !== "object") return String(d);
    if (Array.isArray(d)) return d.length + " шт.";
    return Object.entries(d)
      .filter(([k]) => !(skip || []).includes(k))
      .map(([k, v]) => (KEYS[k] || k) + ": " + (k === "problems" && Array.isArray(v) ? v.length : fmtVal(v)))
      .join(" · ");
  }
  function maskProxy(p) {
    const a = String(p || "").split(":");
    return a.length > 2 ? a[0] + ":" + a[1] + ":•••" : String(p || "—");
  }
  function rowsOf(d) {
    if (Array.isArray(d)) return d;
    if (d && typeof d === "object") for (const k of ["rows", "proxies", "suggest", "items", "modems", "list"]) if (Array.isArray(d[k])) return d[k];
    return [];
  }
  function srv(id) {
    return ((S.ov && S.ov.servers) || []).find((s) => String(s.id) === String(id)) || null;
  }
  // сам хост — как ВМ для общих вкладок (proxyveth, modlink, настройка): режим, модемы, mp.space
  function ent(id) {
    if (String(id) !== "host") return srv(id);
    const h = (S.ov && S.ov.host) || {};
    return {id: "host", name: h.name, ip: h.ip, mode: h.mode, modems: h.modems, mpspace: h.mpspace, state: "running", host: h};
  }
  const tname = (id) => String(id) === "host" ? "хост" : "ВМ " + id;

  // Хост — свой (API /api) или присоединённый (/api/nodes/<имя> → его /api/v1): страницы одни и те же,
  // так система выглядит единой. Ссылки присоединённого — #/n/<имя>/…, терминал — через хаб.
  function nodeOv(name) {
    const n = (S.nodes || []).find((x) => x.name === name);
    return n ? {host: n.host_info || {}, servers: n.servers || [], host_error: n.ok ? null : n.error, node: n} : {};
  }
  function mkCtx(node) {
    const base = node ? "/api/nodes/" + encodeURIComponent(node) : "/api";
    const X = {node, base,
      ov: () => node ? nodeOv(node) : (S.ov || {}),
      href: (p) => (node ? "#/n/" + encodeURIComponent(node) : "#") + (p || "/"),
      wsPath: node ? "/ws/node/" + encodeURIComponent(node) + "/term" : "/ws/term",
      GET: (p) => GET(base + p), POST: (p, b) => POST(base + p, b),
      run: (t, part, args, extra) => runAt(base, t, part, args, extra)};
    X.srv = (id) => ((X.ov().servers) || []).find((s) => String(s.id) === String(id)) || null;
    X.ent = (id) => {
      if (String(id) !== "host") return X.srv(id);
      const h = X.ov().host || {};
      return {id: "host", name: h.name, ip: h.ip, mode: h.mode, modems: h.modems, mpspace: h.mpspace, state: "running", host: h};
    };
    X.isPve = () => ((X.ov().host || {}).kind || "proxmox") === "proxmox";
    X.title = () => (X.ov().host || {}).name || node || "хост";
    return X;
  }
  const CTX = {};
  const ctx = (node) => node ? (CTX[node] || (CTX[node] = mkCtx(node))) : (CTX[""] || (CTX[""] = mkCtx(null)));

  // ── окна ──────────────────────────────────────────────────────────────────
  function modal(title, body, buttons) {
    const bg = el("div", {class: "modal-bg"});
    const close = () => { bg.remove(); document.removeEventListener("keydown", onKey); };
    const onKey = (e) => { if (e.key === "Escape") close(); };
    const bs = (buttons || [{text: "Закрыть"}]).map((b) => {
      const x = btn(b.text, () => { if (b.onclick) b.onclick(close); else close(); }, b.cls);
      if (b.ref) b.ref(x);
      return x;
    });
    bg.append(el("div", {class: "modal", role: "dialog", "aria-modal": "true"},
      el("h3", {text: title}), body, el("div", {class: "btns"}, bs)));
    bg.addEventListener("mousedown", (e) => { if (e.target === bg) close(); });
    document.addEventListener("keydown", onKey);
    document.body.append(bg);
    const f = bg.querySelector("input, textarea, button.primary, button.danger");
    if (f) f.focus();
    return close;
  }
  // Буфер обмена. Панель — по HTTP, а там у браузера нет navigator.clipboard (только для HTTPS
  // и localhost): тогда — старый способ через скрытое поле. Он работает в обработчике нажатия.
  function copySync(text) {
    const ta = el("textarea", {style: "position:fixed;top:0;left:0;width:1px;height:1px;opacity:0"});
    ta.value = text;
    document.body.append(ta);
    ta.focus(); ta.select();
    let ok = false;
    try { ok = document.execCommand("copy"); } catch (e) { ok = false; }
    ta.remove();
    return ok;
  }
  async function copy(text) {
    if (navigator.clipboard && window.isSecureContext) {
      try { await navigator.clipboard.writeText(text); return true; } catch (e) { /* дальше — запасной путь */ }
    }
    return copySync(text);
  }
  // Кнопка «Копировать» с ответом: скопировано или как сделать руками.
  function copyBtn(getText, label) {
    const b = btn(label || "Копировать", null);
    const base = b.textContent;
    b.addEventListener("click", async () => {
      const ok = await copy(getText());
      b.textContent = ok ? "Скопировано ✓" : "Не вышло — выделите и Ctrl+C";
      b.classList.toggle("primary", ok);
      setTimeout(() => { b.textContent = base; b.classList.remove("primary"); }, 2500);
    });
    return b;
  }
  // Таблица по колонкам; копируется через табуляцию — в Google-таблицу встаёт по ячейкам.
  function showTable(title, columns, rows, note) {
    const tsv = (head) => (head ? [columns] : []).concat(rows).map((r) => r.join("\t")).join("\n");
    const head = el("input", {type: "checkbox"});
    const t = el("table", {class: "t export"}, el("thead", {}, el("tr", {}, columns.map((c) => el("th", {text: c})))),
      el("tbody", {}, rows.length ? rows.map((r) => el("tr", {}, r.map((v) => el("td", {class: "mono", text: v}))))
        : el("tr", {}, el("td", {colspan: columns.length, class: "muted", text: "пусто"}))));
    const bg = el("div", {class: "modal-bg"});
    const close = () => { bg.remove(); document.removeEventListener("keydown", onKey); };
    const onKey = (e) => { if (e.key === "Escape") close(); };
    bg.append(el("div", {class: "modal wide", role: "dialog", "aria-modal": "true"},
      el("h3", {text: title}), note ? el("div", {class: "note"}, note) : null, el("div", {class: "tbl-wrap"}, t),
      el("div", {class: "btns"}, el("label", {class: "muted"}, head, " с шапкой"),
        copyBtn(() => tsv(head.checked), "Копировать для таблицы"), btn("Закрыть", close, "primary"))));
    bg.addEventListener("mousedown", (e) => { if (e.target === bg) close(); });
    document.addEventListener("keydown", onKey);
    document.body.append(bg);
  }
  function showText(title, text) {
    const pre = el("pre", {text: text || "—"});
    const close = modal(title, pre, [{text: "Закрыть", cls: "primary"}]);
    pre.closest(".modal").querySelector(".btns").prepend(copyBtn(() => text || ""));
    void close;
  }
  // Опасное — только после ввода номера ВМ (как в меню).
  function confirmNumber(title, warn, expected, actText) {
    return new Promise((resolve) => {
      let go;
      const inp = el("input", {inputmode: "numeric", autocomplete: "off", placeholder: String(expected)});
      inp.addEventListener("input", () => { go.disabled = inp.value.trim() !== String(expected); });
      inp.addEventListener("keydown", (e) => { if (e.key === "Enter" && !go.disabled) go.click(); });
      let done = false;
      const body = el("div", {}, el("p", {text: warn}),
        el("label", {}, String(expected) === "host" ? "Введите слово " : "Введите номер ВМ ", el("b", {text: String(expected)}), ", чтобы подтвердить: ", inp));
      const close = modal(title, body, [
        {text: "Отмена", onclick: (c) => { done = true; c(); resolve(null); }},
        {text: actText || "Подтвердить", cls: "danger solid", ref: (b) => { go = b; b.disabled = true; },
          onclick: (c) => { done = true; c(); resolve(inp.value.trim()); }}]);
      inp.focus();
      const obs = new MutationObserver(() => { if (!inp.isConnected) { obs.disconnect(); if (!done) resolve(null); } });
      obs.observe(document.body, {childList: true});
      void close;
    });
  }
  function askText(title, label, value, placeholder) {
    return new Promise((resolve) => {
      const inp = el("input", {value: value || "", placeholder: placeholder || "", style: "width:100%"});
      let done = false;
      modal(title, el("label", {}, label, el("br"), inp), [
        {text: "Отмена", onclick: (c) => { done = true; c(); resolve(null); }},
        {text: "Сохранить", cls: "primary", onclick: (c) => { done = true; c(); resolve(inp.value.trim()); }}]);
      inp.addEventListener("keydown", (e) => { if (e.key === "Enter") { done = true; inp.closest(".modal-bg").remove(); resolve(inp.value.trim()); } });
      inp.focus();
      const obs = new MutationObserver(() => { if (!inp.isConnected) { obs.disconnect(); if (!done) resolve(null); } });
      obs.observe(document.body, {childList: true});
    });
  }

  // ── действие с результатом строкой рядом ──────────────────────────────────
  function setRes(res, cls, text) { if (!res) return; res.className = "res " + cls; res.textContent = text; res.title = text; }
  async function act(button, res, fn, okText, jobBox) {
    if (button) button.disabled = true;
    setRes(res, "run", "выполняется…");
    let r;
    ACT_RES = res;
    try { r = await fn(); } catch (e) { r = {ok: false, error: String(e)}; } finally { ACT_RES = null; }
    if (button) button.disabled = false;
    if (!r || !r.ok) { setRes(res, "err", "Ошибка: " + ((r && r.error) || "неизвестно")); return r; }
    const d = r.data;
    if (d && typeof d === "object" && d.job != null && jobBox) {
      setRes(res, "ok", "Запущено");
      jobBox.replaceChildren(jobView(d.job, jobBox.dataset.base ? {base: jobBox.dataset.base} : {}));
      return r;
    }
    setRes(res, "ok", typeof okText === "function" ? okText(d) : (okText || "Применено"));
    return r;
  }

  // Журнал фонового задания хаба: опрос, пока не кончится.
  function jobView(jid, opts) {
    opts = opts || {};
    const st = el("span", {class: "res run", text: "идёт…"});
    const pre = el("pre", {text: "…"});
    const tail = el("div");
    const box = el("div", {class: "joblog"},
      el("div", {}, el("b", {text: opts.title || "Задание " + jid}), " · ", st, " ",
        opts.page || opts.base ? null : el("a", {href: "#/job/" + encodeURIComponent(jid), class: "muted", text: "журнал отдельно"})), pre, tail);
    let first = true;
    async function tick() {
      if (!first && !box.isConnected) return;
      first = false;
      const r = await GET((opts.base || "/api") + "/jobs/" + encodeURIComponent(jid));
      if (!r.ok && r.net) { setRes(st, "run", "связь прервалась — задание идёт на хосте, жду…"); setTimeout(tick, 3000); return; }
      if (!r.ok) { setRes(st, "err", "Ошибка: " + r.error); return; }
      const j = r.data || {};
      if (j.title && !opts.title) box.firstChild.firstChild.textContent = j.title;
      const atEnd = pre.scrollTop + pre.clientHeight >= pre.scrollHeight - 4;
      pre.textContent = (j.log || []).join("\n") || "…";
      if (atEnd) pre.scrollTop = pre.scrollHeight;
      if (j.state === "running" || j.state === "queued") { setTimeout(tick, 1500); return; }
      if (j.state === "done") {
        setRes(st, "ok", "готово");
        const home = opts.base && opts.base.indexOf("/api/nodes/") === 0 ? "#/n/" + opts.base.slice(11) : "#";
        if (j.result && j.result.id != null) tail.replaceChildren(el("p", {}, link("Открыть ВМ " + j.result.id, home + "/vm/" + j.result.id, "btn primary")));
        refresh();
        if (opts.onDone) opts.onDone(j);
      } else {
        setRes(st, "err", "Ошибка: " + ((j.result && j.result.error) || j.error || "задание не удалось"));
        refresh();
      }
    }
    tick();
    return box;
  }

  // строка «название + пояснение + управление + результат»
  function actRow(name, hint, ...ctl) {
    const res = el("span", {class: "res"});
    const jobs = el("div", {class: "joblog-slot"});
    const ctlBox = el("div", {class: "ctl"}, ctl, res);
    const row = el("div", {class: "act"}, el("div", {}, el("div", {class: "name", text: name}), el("div", {class: "hint", text: hint})), ctlBox, el("div", {class: "joblog"}, jobs));
    return {el: row, res, jobs, ctl: ctlBox};
  }

  // ── шапка и дерево ────────────────────────────────────────────────────────
  function renderHeader() {
    const host = (S.ov && S.ov.host) || {};
    $("#h-host").textContent = host.name || "хост";
    $("#h-kind").textContent = (host.kind || "proxmox") === "proxmox" ? "Proxmox" : "VPS";
    $("#h-port").textContent = S.session ? ":" + S.session.port : "";
    $("#h-user").textContent = S.session ? S.session.user : "";
    $("#h-fake").hidden = !(S.session && S.session.fake);
  }
  function hostLevel(X) {
    X = X || ctx(null);
    const list = (X.ov() && X.ov().servers) || [];
    if ((!X.node && S.ovErr) || (X.ov() && (X.ov().host_error || X.ov().servers_error))) return "bad";
    const lv = list.map(level);
    const h = (X.ov() && X.ov().host) || {};
    if (h.modems && counts(h).total) lv.push(level({state: "running", modems: h.modems}));
    return lv.includes("bad") ? "bad" : lv.includes("warn") ? "warn" : "ok";
  }
  function renderTree() {
    const nav = $("#tree");
    const cur = S.page || {};
    const kids = [];
    function hostBlock(X) {
      const h = X.ov().host || {}, n = X.node ? (X.ov().node || {}) : null;
      const here = (cur.node || null) === X.node;
      const off = X.node && n && !n.ok;
      kids.push(el("a", {class: "tree-host" + (here && cur.kind === "host" ? " active" : "") + (X.node ? " joined" : ""), href: X.href("/"),
        title: off ? (n.error || "нет связи") : "", style: X.node ? "--h:" + hueOf({id: 0, color: n && n.color}) : null},
        el("span", {class: "dot " + (off ? "bad" : hostLevel(X))}), X.node ? X.node : (h.name || "хост"),
        el("span", {class: "muted", text: off ? "нет связи" : (h.kind || (X.node ? "?" : "proxmox")) === "proxmox" ? "Proxmox" : "VPS"})));
      for (const sv of X.ov().servers || []) {
        const c = counts(sv);
        kids.push(el("a", {class: "tree-vm" + (here && cur.kind === "vm" && String(cur.id) === String(sv.id) ? " active" : ""),
          href: X.href("/vm/" + sv.id), title: levelText(sv), style: "--h:" + hueOf(sv, X)},
          el("span", {class: "dot " + level(sv)}), el("span", {}, el("b", {text: String(sv.id)}), " ", sv.name || ""),
          el("span", {class: "sub", text: sv.state === "absent" ? "нет в Proxmox" : modeShort(sv.mode) + " · " + c.ok + "/" + c.total})));
      }
      if (X.isPve() && !off) kids.push(el("a", {class: "tree-add", href: X.href("/create"), text: "+ Создать ВМ" + (X.node ? " на " + X.node : "")}));
    }
    hostBlock(ctx(null));
    const nodes = S.nodes || [];
    if (nodes.length) kids.push(el("div", {class: "tree-sec", text: "Присоединённые хосты"}));
    for (const n of nodes) hostBlock(ctx(n.name));
    kids.push(el("a", {class: "tree-add", href: "#/join", text: "+ Присоединить хост"}));
    nav.replaceChildren(...kids);
  }

  // ── страница хоста: вкладки, как у ВМ (сам хост — тоже узел: proxyveth, modlink, mp.space) ──
  const HTABS = [["", "обзор"], ["proxyveth", "proxyveth"], ["modlink", "modlink"], ["term", "терминал"], ["settings", "настройка и управление"]];
  const isPve = () => ctx(null).isPve();

  function plaqueHost(X) {
    const big = el("div", {class: "big"}), line = el("div", {class: "line"}), stDot = el("span", {class: "dot"}), stText = el("span");
    const box = el("div", {class: "plaque host"}, el("div", {}, big, line), el("div", {class: "state"}, stDot, stText));
    function update() {
      const h = (X.ov() && X.ov().host) || {}, c = counts(h);
      big.textContent = "Хост " + (h.name || "");
      line.textContent = [h.ip, h.os || (X.isPve() ? "Proxmox" : ""), X.isPve() ? "ВМ: " + ((X.ov() && X.ov().servers) || []).length : "VPS — модемы на нём",
        h.mode ? "proxyveth " + modeShort(h.mode) + " · модемы " + c.ok + "/" + c.total : null,
        h.mpspace_ok ? mpText(h.mpspace) : null].filter(Boolean).join(" · ");
      stDot.className = "dot " + hostLevel(X);
      stText.textContent = hostLevel(X) === "ok" ? "в порядке" : hostLevel(X) === "warn" ? "есть замечания" : "есть проблемы";
    }
    return {el: box, update};
  }

  function pageHost(X) {
    const pl = plaqueHost(X);
    const tabsEl = el("div", {class: "tabs"}), body = el("div"), panes = {};
    let cur = null;
    const make = {"": () => hostOverview(X), proxyveth: () => pvTab(X, "host"), modlink: () => mlTab(X, "host"),
      term: () => termTab(X, "host", true), settings: () => setTab(X, "host")};
    function show(tab) {
      if (!make[tab]) tab = "";
      if (cur === tab) return;
      if (cur !== null && panes[cur].hide) panes[cur].hide();
      if (cur !== null) panes[cur].el.hidden = true;
      if (!panes[tab]) { panes[tab] = make[tab](); body.append(panes[tab].el); }
      panes[tab].el.hidden = false;
      cur = tab;
      tabsEl.replaceChildren(...HTABS.map(([k, t]) => el("a", {href: k ? X.href("/host/" + k) : X.href("/"), class: k === tab ? "active" : "", text: t})));
      if (panes[tab].show) panes[tab].show();
      if (panes[tab].update) panes[tab].update();
    }
    function update() {
      pl.update();
      document.title = X.title() + " — PCS";
      for (const x of Object.values(panes)) if (x.update) x.update();
    }
    function dispose() { for (const x of Object.values(panes)) if (x.dispose) x.dispose(); }
    return {kind: "host", node: X.node, el: el("div", {}, pl.el, tabsEl, body), show, update, dispose};
  }

  // вкладка «обзор» хоста: карточки, ВМ (на Proxmox), фоновые задания
  function hostOverview(X) {
    const banner = el("div"), cards = el("div", {class: "cards"}), list = el("div", {class: "list"});
    const vmHead = el("h2", {text: "Виртуальные машины"});
    const jobsBox = el("div", {class: "list"});
    const others = otherVms(X);
    const nodeBar = X.node ? nodeTools(X) : null;
    const root = el("div", {}, nodeBar ? nodeBar.el : null, banner, cards, vmHead, list, others.el, el("h2", {text: "Фоновые задания"}), jobsBox);
    let othersLoaded = 0;
    const JOB_ST = {running: ["info", "идёт"], done: ["ok", "готово"], failed: ["bad", "ошибка"]};
    async function loadJobs() {
      const r = await X.GET("/jobs");
      if (!r.ok) return;
      const lst = r.data || [];
      jobsBox.replaceChildren(...(lst.length ? lst.map((j) => {
        const [cls, txt] = JOB_ST[j.state] || ["", j.state];
        return el("div", {class: "item"}, el("span", {class: "dot " + cls, title: txt}),
          el("div", {class: "grow"}, el("div", {class: "title", text: j.title || j.id}),
            el("div", {class: "sub", text: [txt, j.started ? new Date(j.started * 1000).toLocaleString() : "", j.error || ""].filter(Boolean).join(" · ")})),
          el("div", {class: "btns"}, link("Журнал", X.href("/job/" + encodeURIComponent(j.id)))));
      }) : [el("div", {class: "item muted", text: "заданий пока нет: долгое (обновление, установка, перенос) идёт здесь в фоне и не боится обрыва связи"})]));
    }
    function update() {
      const ov = X.ov() || {}, host = ov.host || {}, list_ = ov.servers || [];
      const errs = [!X.node && S.ovErr, ov.host_error && (X.node ? "нет связи: " : "хост: ") + ov.host_error,
        ov.servers_error && "серверы: " + ov.servers_error].filter(Boolean);
      banner.replaceChildren(...errs.map((t) => el("div", {class: "banner", text: t})));
      if (nodeBar) nodeBar.update();
      loadJobs();
      if (Date.now() - othersLoaded > 30000) { othersLoaded = Date.now(); others.load(); }
      const card = (k, v, cls) => el("div", {class: "card"}, el("div", {class: "k", text: k}), el("div", {class: "v " + (cls || ""), text: v}));
      const hc = counts(host);
      if (!X.isPve()) {                      // VPS: модемы и софт прямо здесь, ВМ нет
        vmHead.hidden = true; list.hidden = true;
        cards.replaceChildren(
          card("Модемы", hc.ok + " / " + hc.total, hc.ok < hc.total ? "warn" : ""),
          card("Режим proxyveth", host.mode ? modeShort(host.mode) : "не задан", host.mode ? "" : "warn"),
          card("mp.space", mpText(host.mpspace).replace("mp.space ", "")),
          card("ОЗУ хоста", gb(host.ram)));
        return;
      }
      vmHead.hidden = false; list.hidden = false;
      let ok = 0, total = 0, probs = 0, up = 0;
      for (const s of list_) {
        const c = counts(s);
        ok += c.ok; total += c.total; probs += c.warn + c.broken;
        if (!BAD_VM.includes(String(s.state || "").toLowerCase())) up += 1; else probs += 1;
      }
      const h = host.hivelink || {};
      const real = Array.isArray(h.modems) ? h.modems.length : (+h.modems || 0);
      cards.replaceChildren(
        card("ВМ работают", up + " / " + list_.length, up < list_.length ? "bad" : ""),
        card("Модемы в ВМ", ok + " / " + total, ok < total ? "warn" : ""),
        card("Проблемы", String(probs), probs ? "warn" : ""),
        card("Настоящие модемы (hivelink)", h.installed === false ? "не установлен" : String(real)),
        card("ОЗУ хоста", gb(host.ram)));
      if (!list_.length) {
        list.replaceChildren(el("div", {class: "item"}, el("span", {class: "muted", text: "ВМ пока нет — "}), link("+ Создать ВМ", X.href("/create"), "")));
        return;
      }
      list.replaceChildren(...list_.map((s) => {
        const c = counts(s);
        return el("div", {class: "item"}, el("span", {class: "dot " + level(s), title: levelText(s)}),
          el("div", {class: "grow"}, el("div", {class: "title", text: s.id + " " + (s.name || "")}),
            el("div", {class: "sub", text: [s.ip || "—", modeShort(s.mode), "модемы " + c.ok + "/" + c.total, mpText(s.mpspace)].join(" · ")})),
          vmButtons(X, s));
      }));
    }
    return {el: root, update};
  }

  // Кнопки ВМ в списке хоста: открыть, терминал, питание, убрать из PCS, удалить
  function vmButtons(X, s) {
    const st = String(s.state || "").toLowerCase();
    const b = [];
    const pw = (action, text, title, ask, cls) => btn(text, async (e) => {
      if (ask && !window.confirm(ask)) return;
      const r = await act(e.target, null, () => X.POST("/servers/" + s.id + "/power", {action}), "Готово");
      if (r && !r.ok) showText("ВМ " + s.id + ": не вышло", "Ошибка: " + r.error);
      X.node ? refreshNodes() : refresh();
    }, "icon" + (cls ? " " + cls : ""), title);
    if (st !== "absent") {
      b.push(link("Открыть", X.href("/vm/" + s.id)), link("Терминал", X.href("/vm/" + s.id + "/term")));
      if (st === "stopped") b.push(pw("start", "▶", "включить"));
      else {
        b.push(pw("shutdown", "⏻", "выключить штатно (до 2 мин)", "Выключить ВМ " + s.id + " (" + (s.name || "") + ")? Модемы на ней пропадут."));
        b.push(pw("reboot", "↻", "перезагрузить", "Перезагрузить ВМ " + s.id + " (" + (s.name || "") + ")?"));
        b.push(pw("stop", "■", "остановить жёстко (как выдернуть питание)", "Жёстко остановить ВМ " + s.id + "? Как выдернуть питание.", "danger"));
      }
    }
    b.push(btn("Убрать из PCS", () => forgetVm(X, s), st === "absent" ? "danger" : "", "PCS перестанет управлять ВМ; сама ВМ, диски и модемы не трогаются"));
    if (st !== "absent") b.push(btn("Удалить", () => deleteVm(X, s), "danger", "удалить ВМ вместе с дисками"));
    return el("div", {class: "btns"}, b);
  }

  async function forgetVm(X, s) {
    const v = await confirmNumber("Убрать ВМ " + s.id + " из PCS", (s.state === "absent" ? "ВМ " + s.id + " уже нет в Proxmox — уберём только её запись в PCS."
      : "PCS перестанет управлять ВМ " + s.id + " (" + (s.name || "") + "). Сама ВМ, её диски и модемы останутся; вернуть — «Добавить в PCS»."),
      s.id, "Убрать из PCS");
    if (v == null) return;
    const r = await X.POST("/servers/" + s.id + "/forget", {confirm: v});
    if (!r.ok) { showText("ВМ " + s.id + " не убрана", "Ошибка: " + r.error); return; }
    await (X.node ? refreshNodes() : refresh());
    if (location.hash.indexOf("/vm/" + s.id) >= 0) location.hash = X.href("/");
  }

  // ВМ Proxmox, которых нет в PCS: «Добавить в PCS» (ключ хаба по паролю root, код, части — заданием)
  function otherVms(X) {
    const box = el("div", {class: "list"}), head = el("h2", {text: "ВМ Proxmox не под PCS"});
    const jobs = el("div", {class: "joblog-slot"});
    if (X.node) jobs.dataset.base = X.base;
    async function load() {
      if (!X.isPve()) { head.hidden = box.hidden = true; return; }
      const r = await X.GET("/vms");
      const lst = r.ok ? r.data || [] : [];
      head.hidden = box.hidden = !lst.length;
      box.replaceChildren(...lst.map((v) => el("div", {class: "item"}, el("span", {class: "dot " + (v.state === "running" ? "" : "off")}),
        el("div", {class: "grow"}, el("div", {class: "title", text: v.id + " " + (v.name || "")}),
          el("div", {class: "sub", text: v.state === "running" ? "работает · не под PCS" : (v.state || "?") + " · не под PCS"})),
        el("div", {class: "btns"}, btn("Добавить в PCS", () => adoptVm(X, v, jobs), "", "PCS начнёт управлять этой ВМ: ключ хаба, код, proxyveth и modlink")))));
    }
    return {el: el("div", {}, head, box, jobs), load};
  }

  async function adoptVm(X, v, jobs) {
    const ip = el("input", {placeholder: "пусто — узнать у гостевого агента"});
    const pw = el("input", {type: "password", autocomplete: "new-password", placeholder: "нужен, если ключ хаба на ВМ ещё не лежит"});
    modal("Добавить ВМ " + v.id + " (" + (v.name || "") + ") в PCS", el("div", {class: "form"},
      el("p", {class: "muted", text: "Хаб зайдёт по паролю root один раз, положит свой ключ, поставит код PCS, proxyveth и modlink. " +
        "Нужна Ubuntu 22.04/24.04 с SSH. Модемы и софт на ВМ не трогаются."}),
      el("label", {}, "IP ВМ", ip), el("label", {}, "Пароль root", pw)), [
      {text: "Отмена"},
      {text: "Добавить", cls: "primary", onclick: async (close) => {
        close();
        const r = await X.POST("/vms/" + v.id + "/adopt", {ip: ip.value.trim(), password: pw.value});
        if (!r.ok) { showText("ВМ " + v.id + " не добавлена", "Ошибка: " + r.error); return; }
        jobs.replaceChildren(jobView(r.data.job, {title: "Добавить ВМ " + v.id + " в PCS", base: X.node ? X.base : undefined,
          onDone: () => (X.node ? refreshNodes() : refresh())}));
      }}]);
  }

  async function deleteVm(X, s) {
    const v = await confirmNumber("Удалить ВМ " + s.id + " · " + (s.name || ""),
      "ВМ " + s.id + " (" + (s.name || "") + ", " + (s.ip || "без IP") + ") будет остановлена и удалена вместе с дисками. Это необратимо.",
      s.id, "Удалить");
    if (v == null) return;
    const r = await X.POST("/servers/" + s.id + "/delete", {confirm: v});
    if (!r.ok) { showText("ВМ " + s.id + " не удалена", "Ошибка: " + r.error); return; }
    if (r.data && r.data.job != null) { location.hash = X.href("/job/" + encodeURIComponent(r.data.job)); return; }
    await refresh();
    location.hash = X.href("/");
  }

  // ── страница ВМ ───────────────────────────────────────────────────────────
  const TABS = [["proxyveth", "proxyveth"], ["modlink", "modlink"], ["term", "терминал"], ["settings", "настройка и управление"]];

  function plaque(X, id) {
    const big = el("div", {class: "big"}), line = el("div", {class: "line"}), stDot = el("span", {class: "dot"}), stText = el("span");
    const box = el("div", {class: "plaque"}, el("div", {}, big, line), el("div", {class: "state"}, stDot, stText));
    function update() {
      box.style.setProperty("--h", String(hueOf(X.srv(id) || {id}, X)));
      const s = X.srv(id);
      if (!s) {
        big.textContent = "ВМ " + id;
        line.textContent = X.ov() ? "нет в списке PCS — удалена или ещё создаётся" : "загрузка…";
        stDot.className = "dot"; stText.textContent = "";
        return;
      }
      const c = counts(s);
      big.textContent = "ВМ " + s.id + " · " + (s.name || "");
      line.textContent = [s.ip || "без IP", modeLong(s.mode), "модемы " + c.ok + "/" + c.total, mpText(s.mpspace)].join(" · ");
      stDot.className = "dot " + level(s);
      stText.textContent = levelText(s);
      document.title = s.id + " " + (s.name || "") + " — PCS";
    }
    return {el: box, update};
  }

  function pageVm(X, id) {
    const pl = plaque(X, id);
    const tabsEl = el("div", {class: "tabs"});
    const body = el("div");
    const panes = {};
    let cur = null;
    const make = {proxyveth: () => pvTab(X, id), modlink: () => mlTab(X, id), term: () => termTab(X, id), settings: () => setTab(X, id)};
    function show(tab) {
      if (!make[tab]) tab = "proxyveth";
      if (cur === tab) return;
      if (cur && panes[cur].hide) panes[cur].hide();
      if (cur) panes[cur].el.hidden = true;
      if (!panes[tab]) { panes[tab] = make[tab](); body.append(panes[tab].el); }
      panes[tab].el.hidden = false;
      cur = tab;
      tabsEl.replaceChildren(...TABS.map(([k, t]) => el("a", {href: X.href("/vm/" + id + (k === "proxyveth" ? "" : "/" + k)), class: k === tab ? "active" : "", text: t})));
      if (panes[tab].show) panes[tab].show();
    }
    function update() {
      pl.update();
      for (const p of Object.values(panes)) if (p.update) p.update();
    }
    function dispose() { for (const p of Object.values(panes)) if (p.dispose) p.dispose(); }
    return {kind: "vm", node: X.node, id: String(id), el: el("div", {}, pl.el, tabsEl, body), show, update, dispose};
  }

  // опрос, пока вкладка видна
  function poller(ms, fn) {
    let t = null, on = false;
    const loop = async () => {
      if (!on) return;
      if (!document.hidden) { try { await fn(); } catch (e) { console.error(e); } }
      if (on) t = setTimeout(loop, ms);
    };
    return {
      start() { if (on) return; on = true; t = setTimeout(loop, ms); },
      stop() { on = false; clearTimeout(t); },
    };
  }

  // ── вкладка proxyveth ─────────────────────────────────────────────────────
  function pvTab(X, id) {
    const info = el("div", {class: "kv"});
    const res = el("span", {class: "res"});
    const extra = el("div");
    const cnt = el("span", {class: "muted"});
    const onlyBad = el("input", {type: "checkbox", onchange: () => renderRows()});
    const tbl = el("div", {class: "tbl-wrap"});
    const editor = el("div");
    let rows = [], source = null, statusErr = null;

    const bSync = btn("Синхронизировать", () => sync(false), "primary", "привести модемы к таблице");
    const bEdit = btn("Править локально", () => openEditor(), "", "править локальную копию таблицы прямо здесь");
    const bQuick = btn("Быстрый тест", async (e) => {
      e.target.disabled = true; setRes(res, "run", "проверяю свою сторону…");
      await load(false); e.target.disabled = false; setRes(res, "ok", "быстрый тест готов");
    }, "", "proxyveth status — своя сторона: юниты, адреса, маршруты, USB (секунды)");
    const bWan = btn("Долгий тест (внешний IP)", async (e) => {
      e.target.disabled = true; setRes(res, "run", "проверяю внешний IP через каждый модем — минута-две…");
      await load(true); e.target.disabled = false; setRes(res, "ok", "долгий тест готов, внешний IP обновлён");
    }, "", "proxyveth status --wan — выход в интернет через каждый модем");
    const allBar = el("div", {class: "bar"}, el("span", {class: "muted", text: "все модемы:"}),
      btn("поднять", (e) => updown(e.target, "up"), "", "proxyveth up all — поднять всех из таблицы, снять «остановлен»"),
      btn("остановить", (e) => updown(e.target, "down"), "", "proxyveth down all — снять всех; таймер не поднимет, пока не «поднять»"),
      btn("пересоздать", (e) => updown(e.target, "restart"), "", "proxyveth restart all — снять и поднять всех заново"));
    const bSrc = btn("Сменить ссылку", async () => {
      const url = await askText("Источник таблицы", "Ссылка на Google-таблицу (из адресной строки) или local:",
        source && source.url || "", "https://docs.google.com/spreadsheets/d/…");
      if (!url) return;
      await act(null, res, () => X.run(id, "proxyveth", ["source", url]), "Сохранено");
      load(false);
    }, "link");

    async function sync(force) {
      extra.replaceChildren();
      let confirm;
      if (force) {
        confirm = await confirmNumber("Синхронизировать с --force",
          "Таблица требует снести больше половины модемов. С --force они будут сняты.", id, "Синхронизировать");
        if (confirm == null) return;
      }
      const r = await act(bSync, res, () => X.run(id, "proxyveth", force ? ["sync", "--force"] : ["sync"], force ? {confirm} : null),
        (d) => "Применено" + (d && typeof d === "object" ? ": " + summary(d) : ""));
      if (r && !r.ok && /--force/.test(r.error || "") && !force)
        extra.replaceChildren(el("p", {}, btn("Синхронизировать с --force…", () => sync(true), "danger")));
      load(false);
      refresh();
    }

    function renderInfo() {
      const s = X.ent(id) || {};
      const kv = [["Режим", s.mode === "usb" ? "USB — модем как USB Huawei E3372h, до 40 на сервер"
        : s.mode === "gw" ? "шлюз — без USB и без такого предела" : modeShort(s.mode)]];
      const d = source && source.ok ? source.data : null;
      if (source && !source.ok) kv.push(["Таблица", "Ошибка: " + source.error]);
      else if (d && typeof d === "object") {
        const kind = d.source || d.kind || (d.url ? "google" : null);
        const url = d.url || (typeof d.source === "string" && /^https?:/.test(d.source) ? d.source : null);
        const urlEl = url ? el("a", {href: url, target: "_blank", rel: "noopener noreferrer", text: url.length > 70 ? url.slice(0, 70) + "…" : url}) : "не задана";
        if (kind === "local") {
          kv.push(["Главная", "локальная копия " + (d.local || "/etc/proxyveth/table.csv") + " (правится в панели)"]);
          kv.push(["Google", el("span", {}, urlEl, " — не главная")]);
        } else {
          kv.push(["Главная", el("span", {}, "Google-таблица ", urlEl)]);
          kv.push(["Локальная копия", (d.local || "/etc/proxyveth/table.csv")]);
        }
        kv.push(["Синхронизирована", ago(d.synced || d.pulled || d.mtime)]);
        if (d.stale) kv.push(["", el("span", {class: "problems", text: "⚠ Google недоступен — работает локальная копия"})]);
      } else if (d) kv.push(["Таблица", String(d)]);
      info.replaceChildren(...kv.flatMap(([k, v]) => [el("div", {class: "k", text: k}), el("div", {}, v)]));
    }

    function renderRows() {
      if (statusErr) { tbl.replaceChildren(el("div", {class: "banner", text: "Ошибка: " + statusErr})); cnt.textContent = ""; return; }
      const c = {ok: 0, warn: 0, broken: 0, server: 0, stopped: 0};
      for (const r of rows) if (r.state === "warn" && r.side === "server") c.server += 1; else if (c[r.state] != null) c[r.state] += 1;
      cnt.textContent = "работают " + c.ok + " · апстрим " + c.warn + (c.server ? " · ждут сервер " + c.server : "")
        + " · сломаны " + c.broken + (c.stopped ? " · остановлены " + c.stopped : "") + " · всего " + rows.length;
      const shown = onlyBad.checked ? rows.filter((r) => !["ok", "stopped", "disabled"].includes(r.state)) : rows;
      if (!rows.length) { tbl.replaceChildren(el("div", {class: "item muted", text: "модемов нет — таблица пуста или не задана"})); return; }
      const head = el("tr", {}, ["n", "real", "прокси", "состояние", "внешний IP", "проблемы", ""].map((t) => el("th", {text: t})));
      const body = shown.map((r) => {
        const [cls, txt] = r.state === "warn" && r.side === "server" ? ["warn", "сервер"]
          : ROW_STATE[r.state] || ["", r.state || "—"];
        return el("tr", {},
          el("td", {class: "num", text: String(r.n)}), el("td", {class: "num", text: r.real == null ? "—" : String(r.real)}),
          el("td", {class: "mono", text: maskProxy(r.proxy)}),
          el("td", {}, el("span", {class: "pill", title: (r.side === "server" ? "беда на стороне сервера, не у прокси: сервер (mp.space) "
              + "ещё не взял модем — адрес, правило маршрутизации, USB. " : "") + (r.since ? "с " + new Date(r.since * 1000).toLocaleString("ru-RU") : "")},
            el("span", {class: "dot " + cls}), txt)),
          el("td", {class: "mono", text: r.ext_ip || "—"}),
          r.note && !(r.problems || []).length ? el("td", {class: "muted note", text: r.note, title: r.note})
            : el("td", {class: "problems", text: (r.problems || []).join("; ")}),
          el("td", {class: "acts-cell"}, btn("диагн.", () => diag(r.n), "icon", "долгий тест модема: прокси → логин → модем → SIM → интернет (proxyveth diag " + r.n + ")"),
            r.state === "stopped"
              ? btn("поднять", (e) => updown(e.target, "up", r.n), "icon", "proxyveth up " + r.n)
              : btn("остановить", (e) => updown(e.target, "down", r.n), "icon", "снять модем " + r.n + "; таймер не поднимет, пока не «поднять» (proxyveth down " + r.n + ")"),
            btn("пересоздать", (e) => updown(e.target, "restart", r.n), "icon", "снять интерфейс модема " + r.n + " и поднять заново (proxyveth restart " + r.n + ")")));
      });
      tbl.replaceChildren(el("table", {class: "t"}, el("thead", {}, head), el("tbody", {}, body)));
    }

    // up / down / restart — один модем (n) или все (n не задан)
    async function updown(button, cmd, n) {
      const all = n == null, what = all ? "все модемы" : "модем " + n;
      const ask = {down: "Остановить " + what + " (" + tname(id) + ")? Таймер не поднимет, пока не нажмёшь «поднять».",
        restart: "Пересоздать " + what + " (" + tname(id) + ")? Интерфейс снимется и поднимется заново — около минуты без связи"
          + (all ? " у каждого, по одному." : "."),
        up: all ? "Поднять все модемы из таблицы (" + tname(id) + ")? Остановленные вручную тоже поднимутся." : null}[cmd];
      if (ask && !window.confirm(ask)) return;
      const args = [cmd, all ? "all" : String(n)].concat(all && cmd !== "up" ? ["--yes"] : []);
      const done = {up: "поднят", down: "остановлен", restart: "пересоздан"}[cmd] + (all ? "ы" : "");
      const r = await act(button, res, () => X.run(id, "proxyveth", args), what + ": " + done);
      if (r && !r.ok) showText(what[0].toUpperCase() + what.slice(1) + ": не " + done, "Ошибка: " + r.error);
      load(false);
    }

    async function diag(n) {
      const body = el("div", {}, el("p", {class: "muted", text: "прокси → логин → модем → SIM → интернет…"}));
      modal("Диагностика модема " + n + " · " + tname(id), body);
      const r = await X.run(id, "proxyveth", ["diag", String(n)]);
      if (!r.ok) { body.replaceChildren(el("p", {class: "res err", text: "Ошибка: " + r.error})); return; }
      const d = r.data || {};
      body.replaceChildren(el("ul", {class: "steps"}, (d.steps || []).map((s) =>
        el("li", {}, el("span", {class: s.ok ? "ok" : "bad", text: s.ok ? "✓" : "✗"}), el("b", {text: s.name}), el("span", {text: s.text || ""})))),
        el("p", {}, el("b", {text: "Итог: "}), d.verdict || "—"));
    }

    async function load(wan) {
      const r = await X.GET("/targets/" + id + "/proxyveth" + (wan ? "?wan=1" : ""));
      apply(r);
    }
    async function pollStatus() { apply(await X.GET("/targets/" + id + "/proxyveth?only=status")); }
    function apply(r) {
      if (!r.ok) { statusErr = r.error; renderRows(); return; }
      const st = r.data.status;
      if (r.data.source) source = r.data.source;
      statusErr = st && !st.ok ? st.error : null;
      rows = st && st.ok ? rowsOf(st.data) : rows;
      renderInfo(); renderRows();
    }

    // редактор локальной копии таблицы
    async function openEditor() {
      bEdit.disabled = true;
      editor.replaceChildren(el("p", {class: "muted", text: "загрузка таблицы…"}));
      const r = await X.GET("/targets/" + id + "/proxyveth/table");
      if (!r.ok) { editor.replaceChildren(el("div", {class: "banner", text: "Ошибка: " + r.error})); bEdit.disabled = false; return; }
      let grid = csvParse((r.data && r.data.csv) || "n,real,proxy,enabled\n");
      if (!grid.length) grid = [["n", "real", "proxy", "enabled"]];
      const width = Math.max(...grid.map((x) => x.length));
      grid = grid.map((x) => x.concat(Array(width - x.length).fill("")));
      let textMode = false;
      const area = el("div", {class: "grid-ed"});
      const ta = el("textarea", {rows: 18, spellcheck: false});
      const mainCb = el("input", {type: "checkbox"});
      const eres = el("span", {class: "res"});
      const isLocal = source && source.ok && source.data && source.data.source === "local";
      mainCb.checked = !!isLocal;

      const wcls = (j) => /^\s*(n|num|номер|real|modem|enabled|вкл|on|port|proxy_port)\s*$/i.test(grid[0][j] || "") ? "w-s" : "w-l";
      function drawGrid() {
        const head = el("tr", {}, grid[0].map((v, j) => el("th", {}, el("input", {value: v, class: wcls(j), oninput: (e) => { grid[0][j] = e.target.value; }}))), el("th"));
        const body = grid.slice(1).map((row, i) => el("tr", {},
          row.map((v, j) => el("td", {}, el("input", {value: v, class: wcls(j), spellcheck: false, oninput: (e) => { grid[i + 1][j] = e.target.value; }}))),
          el("td", {}, btn("✕", () => { grid.splice(i + 1, 1); drawGrid(); }, "icon", "убрать строку"))));
        area.replaceChildren(el("div", {class: "tbl-wrap"}, el("table", {class: "t"}, el("thead", {}, head), el("tbody", {}, body))),
          el("div", {class: "bar"}, btn("+ строка", () => { grid.push(Array(grid[0].length).fill("")); drawGrid(); })));
      }
      function toText() { ta.value = csvJoin(grid.filter((r, i) => i === 0 || r.some((c) => c.trim()))); }
      const bMode = btn("Как текст (CSV)", () => {
        if (!textMode) { toText(); area.replaceChildren(ta); bMode.textContent = "Как таблица"; }
        else { grid = csvParse(ta.value); if (!grid.length) grid = [["n", "real", "proxy", "enabled"]]; drawGrid(); bMode.textContent = "Как текст (CSV)"; }
        textMode = !textMode;
      });
      const bSave = btn("Сохранить в локальную копию", async () => {
        if (!textMode) toText();
        const r2 = await act(bSave, eres, () => X.POST("/targets/" + id + "/proxyveth/table", {csv: ta.value, make_main: mainCb.checked}),
          (d) => {
            const sd = d && d.saved && d.saved.data;
            const p = sd && Array.isArray(sd.problems) ? sd.problems : [];
            return "Сохранено" + (sd && sd.rows != null ? ": строк " + sd.rows : "") + (p.length ? " · замечаний " + p.length : "");
          });
        if (r2 && r2.ok) {
          const sd = r2.data.saved && r2.data.saved.data;
          const p = sd && Array.isArray(sd.problems) ? sd.problems : [];
          editor.replaceChildren(el("div", {class: "banner warn" + (p.length ? "" : " ok"), hidden: !p.length},
            el("b", {text: "Замечания к таблице:"}), el("ul", {}, p.map((t) => el("li", {text: t})))));
          setRes(res, "ok", "Таблица сохранена — «Синхронизировать», чтобы привести модемы к ней");
          bEdit.disabled = false;
          load(false);
        }
      }, "primary");
      editor.replaceChildren(el("h2", {text: "Локальная копия таблицы"}),
        el("p", {class: "muted", text: isLocal ? "Главная — локальная копия: правка сразу становится источником модемов."
          : "Главная — Google-таблица: при следующей синхронизации копия перезапишется из Google, если не сделать её главной."}),
        area,
        el("div", {class: "bar"}, bMode, el("label", {class: "pill"}, mainCb, "сделать локальную копию главной (proxyveth source local)")),
        el("div", {class: "bar"}, bSave, btn("Отмена", () => { editor.replaceChildren(); bEdit.disabled = false; }), eres));
      drawGrid();
    }

    const pl = poller(10000, pollStatus);
    const root = el("div", {}, info, el("div", {class: "bar"}, bSync, bQuick, bWan, bEdit, bSrc, res), allBar, extra, editor,
      el("div", {class: "bar"}, cnt, el("span", {class: "spacer"}), el("label", {class: "pill"}, onlyBad, "только проблемные")), tbl);
    let loaded = false;
    return {
      el: root,
      show() { if (!loaded) { loaded = true; tbl.replaceChildren(el("div", {class: "item muted", text: "загрузка…"})); load(false); } else pollStatus(); pl.start(); },
      hide() { pl.stop(); },
      dispose() { pl.stop(); },
      update() { renderInfo(); },
    };
  }

  function csvParse(text) {
    text = String(text || "").replace(/^﻿/, "");
    const rows = [];
    let row = [], cell = "", q = false;
    for (let i = 0; i < text.length; i++) {
      const c = text[i];
      if (q) {
        if (c === '"') { if (text[i + 1] === '"') { cell += '"'; i++; } else q = false; } else cell += c;
      } else if (c === '"') q = true;
      else if (c === ",") { row.push(cell); cell = ""; }
      else if (c === "\n" || c === "\r") {
        if (c === "\r" && text[i + 1] === "\n") i++;
        row.push(cell); rows.push(row); row = []; cell = "";
      } else cell += c;
    }
    if (cell !== "" || row.length) { row.push(cell); rows.push(row); }
    return rows.filter((r, i) => i === 0 || r.some((c) => c.trim()));
  }
  const csvCell = (v) => /[",\n\r]/.test(v) ? '"' + v.replace(/"/g, '""') + '"' : v;
  const csvJoin = (rows) => rows.map((r) => r.map((c) => csvCell(String(c))).join(",")).join("\n") + "\n";

  // ── вкладка modlink ───────────────────────────────────────────────────────
  // modlink — по образцу modlink для Windows (ветка app-v2): сверху основная сеть, режим портов и трафик;
  // ниже модемы — № · IP · порт · логин · пароль · внешний IP и действия, «ещё» раскрывается под строкой;
  // «Копировать» — прокси, ссылки ребута, ссылки реконнекта или всё по колонкам.
  const ML_MAIN = [["lan_ip", "IP", "адрес интерфейса модема на сервере — через него выходит прокси"],
    ["port", "Порт", "порт прокси: HTTP и SOCKS5 на одном"], ["login", "Логин"], ["password", "Пароль"]];
  const ML_MORE = [["name", "Имя", "метка"], ["modem_ip", "IP модема (HiLink)", "веб модема: реконнект, ребут, статус"],
    ["reboot_port", "Порт ребута", "GET http://IP:порт/reboot; 0 — нет ссылки"],
    ["reconnect_port", "Порт реконнекта", "GET http://IP:порт/reconnect; 0 — нет ссылки"],
    ["interval_min", "Авто-реконнект, мин", "0 — выкл"]];
  const ML_NEW = {enabled: true, name: "", login: "", password: "", port: "", lan_ip: "", modem_ip: "",
    reboot_port: "", reconnect_port: "", interval_min: "0", protect: true};

  function mlTab(X, id) {
    const where = id === "host" ? "хост" : "ВМ " + id;
    const legacyBox = el("div");
    const res = el("span", {class: "res"});
    const out = el("div", {class: "muted mono"});
    const dirtyEl = el("span", {class: "problems"});
    const pill = el("span", {class: "pill"}, el("span", {class: "dot"}), el("span", {text: "3proxy: …"}));
    const engineEl = el("span", {class: "muted"});
    const cnt = el("span", {class: "muted"});
    const tbl = el("div", {class: "tbl-wrap ml-rows"});
    let orig = [], states = {}, edits = {}, added = [], deleted = new Set(), revealed = {}, opened = new Set();
    let err = null, seq = 0, pending = false, S0 = null, tests = {};

    // ── основная сеть ────────────────────────────────────────────────────
    const sLan = el("input", {class: "w-ip", placeholder: "авто"});
    const sWan = el("input", {class: "w-ip", placeholder: "авто"});
    const sBase = el("input", {class: "w-s", placeholder: "10000", inputmode: "numeric"});
    const sRes = el("span", {class: "res"});
    const segMode = seg([["triples", "Разные порты (тройки)", "у строки свои: прокси, ребут, реконнект подряд"],
      ["shared", "Один порт (по логину)", "у всех один порт прокси — модем по логину; один порт управления — ссылки с паролем строки"]],
      async (v) => {
        if (!window.confirm("Режим портов «" + (v === "shared" ? "один порт" : "разные порты") + "»: порты ВСЕХ строк заполнятся заново от базового " +
            ((S0 && S0.base_port) || 10000) + ". Клиентам понадобятся новые строки. Дальше — «Применить». Продолжить?")) return false;
        return saveSet({mode: v}, "порты заполнены под режим — «Применить»");
      });
    const segTraffic = seg([["tcp+udp", "TCP и UDP", "SOCKS5 UDP ASSOCIATE: игры, звонки, QUIC"], ["tcp", "Только TCP", "UDP через прокси запрещён"]],
      (v) => saveSet({traffic: v}, "трафик: " + (v === "tcp" ? "только TCP" : "TCP и UDP") + " — «Применить»"));
    const tWan = toggle("Внешний IP раз в час", "демон проверяет внешний IP через каждый прокси", (v) => saveSet({wan_check: v}));
    const tSpeed = toggle("Скорость в 12:00", "раз в день тест скорости каждого модема (yaspeed) — это трафик модема", (v) => saveSet({speedtest: v}));
    const tLog = toggle("Журнал соединений", "каждое соединение 3proxy — в /var/log/modlink/3proxy.log (3 дня)", (v) => saveSet({access_log: v}, "«Применить»"));
    const netCard = el("div", {class: "card ml-net"},
      el("div", {class: "eyebrow", text: "Основная сеть"}),
      el("div", {class: "grid3"},
        field("IP этого хоста", sLan, "адрес в локальной сети — строки «Для proxyveth» (ВМ этого хоста)"),
        field("Внешний IP", sWan, "для строк и ссылок клиентам; его же 3proxy сообщает для UDP"),
        field("Базовый порт", sBase, "от него порты новых строк и смены режима")),
      el("div", {class: "bar"}, btn("Сохранить", async (e) => {
        const body = {lan_ip: sLan.value.trim(), wan_ip: sWan.value.trim()};
        const base = sBase.value.trim();
        if (base && S0 && String(S0.base_port) !== base) {
          if (!window.confirm("Порты всех строк заполнятся заново от " + base + ". Клиентам понадобятся новые строки. Дальше — «Применить». Продолжить?")) return;
          body.base_port = base;
        }
        if (dirtyCount() && body.base_port && !window.confirm("Несохранённые правки таблицы пропадут. Продолжить?")) return;
        const r = await act(e.target, sRes, () => X.POST("/targets/" + id + "/modlink/settings", body),
          (d) => d && d.renumbered ? "порты пересчитаны — «Применить»" : "Сохранено");
        if (r && r.ok) await load();
      }, "", "сохранить адреса и базовый порт"), sRes),
      el("div", {class: "ml-opts"},
        el("label", {class: "muted", text: "Режим портов"}), segMode.el,
        el("label", {class: "muted", text: "Трафик"}), segTraffic.el),
      el("div", {class: "ml-opts"}, tWan.el, tSpeed.el, tLog.el));

    function field(label, input, hint) {
      return el("div", {class: "field"}, el("label", {text: label}), input, el("div", {class: "hint", text: hint}));
    }
    function seg(items, onPick) {
      let cur = null;
      const bs = items.map(([v, t, title]) => btn(t, async () => {
        if (v === cur) return;
        const ok = await onPick(v);
        if (ok !== false) set(v);
      }, "", title));
      function set(v) { cur = v; bs.forEach((b, i) => b.classList.toggle("primary", items[i][0] === v)); }
      return {el: el("span", {class: "seg"}, ...bs), set};
    }
    function toggle(label, title, onChange) {
      const box = el("input", {type: "checkbox", onchange: async () => {
        const ok = await onChange(box.checked);
        if (ok === false) box.checked = !box.checked;
      }});
      return {el: el("label", {class: "pill", title}, box, label), set: (v) => { box.checked = !!v; }};
    }
    async function saveSet(body, okText) {
      const r = await act(null, sRes, () => X.POST("/targets/" + id + "/modlink/settings", body), okText || "Сохранено");
      if (!r || !r.ok) return false;
      showSettings({ok: true, data: r.data});
      if (r.data && r.data.renumbered) await load();
      return true;
    }
    function showSettings(x) {
      if (!x || !x.ok || !x.data) { netCard.hidden = !!(x && !x.ok); return; }
      netCard.hidden = false;
      S0 = x.data;
      sBase.value = String(S0.base_port || "");
      sLan.value = S0.lan_ip || ""; sLan.placeholder = "авто" + (S0.lan_ip_now ? " (" + S0.lan_ip_now + ")" : "");
      sWan.value = S0.wan_ip || ""; sWan.placeholder = "авто (при копировании)";
      segMode.set(S0.mode || "triples"); segTraffic.set(S0.traffic || "tcp+udp");
      tWan.set(S0.wan_check !== false); tSpeed.set(!!S0.speedtest); tLog.set(!!S0.access_log);
    }

    // ── правки строк ─────────────────────────────────────────────────────
    const val = (r, k) => (edits[r.id] && k in edits[r.id]) ? edits[r.id][k] : r[k];
    const dirtyCount = () => Object.keys(edits).length + added.length + deleted.size;
    function setEdit(r, k, v) {
      const e = edits[r.id] || (edits[r.id] = {});
      const o = k === "password" ? revealed[r.id] : r[k];
      if (String(v) === String(o == null ? "" : o)) delete e[k]; else e[k] = v;
      if (!Object.keys(e).length) delete edits[r.id];
      markDirty();
    }
    function markDirty() {
      const n = dirtyCount();
      dirtyEl.textContent = n ? "несохранённых правок: " + n + " — «Применить»"
        : pending ? "есть неприменённые правки — «Применить»" : "";
      for (const tr of tbl.querySelectorAll("tr[data-id]")) tr.classList.toggle("dirty", !!edits[tr.dataset.id]);
    }
    function inputFor(r, k, isNew) {
      const v = isNew ? r.fields[k] : k === "password" ? ((edits[r.id] || {}).password ?? revealed[r.id]) : val(r, k);
      const cls = k === "interval_min" ? "w-xs" : /port/.test(k) ? "w-s" : (k === "lan_ip" || k === "modem_ip") ? "w-ip" : "w-m";
      if (k === "enabled") {
        return el("label", {class: "pill"}, el("input", {type: "checkbox", checked: !!v,
          onchange: (e) => { isNew ? (r.fields[k] = e.target.checked) : setEdit(r, k, e.target.checked); draw(); }}), "включён");
      }
      if (k === "protect") {
        const on = v !== false;
        const box = el("input", {type: "checkbox", checked: on, onchange: (e) => {
          if (!e.target.checked && !window.confirm("Снять защиту строки " + (isNew ? "(новой)" : r.id) + "? Клиенту прокси откроются " +
              "личный кабинет оператора (вход по SIM без пароля), веб модема и опасные порты. Только для своих (ген1).")) {
            e.target.checked = true; return;
          }
          if (isNew) r.fields[k] = e.target.checked; else setEdit(r, k, e.target.checked);
          draw();
        }});
        return el("label", {class: "guard" + (on ? "" : " off"), title: "закрыты ЛК и сайты операторов, опасные порты и веб модема"},
          box, el("span", {text: on ? "защита активна" : "защита НЕ активна"}));
      }
      return el("input", {value: v == null ? "" : String(v), class: cls,
        placeholder: isNew && /port/.test(k) ? "auto" : isNew && k === "password" ? "gen" : "",
        oninput: (e) => isNew ? (r.fields[k] = e.target.value) : setEdit(r, k, e.target.value)});
    }
    function pwCell(r) {
      const e = edits[r.id] || {};
      if (e.password === "gen")
        return el("span", {}, el("span", {class: "pw-gen", text: "новый "}),
          btn("↶", () => { delete e.password; if (!Object.keys(e).length) delete edits[r.id]; draw(); }, "icon", "не менять"));
      if (r.id in revealed) return el("span", {class: "ml-pw"}, inputFor(r, "password"),
        btn("↺", () => { setEdit(r, "password", "gen"); draw(); }, "icon", "сгенерировать новый пароль"));
      return el("span", {class: "ml-pw"},
        el("span", {class: "pw-hidden", text: r.password_set === false ? "—" : "••••••"}),
        btn("показать", () => reveal(r), "link", "показать пароль (явный запрос)"),
        btn("↺", () => { setEdit(r, "password", "gen"); draw(); }, "icon", "сгенерировать новый пароль"));
    }
    async function reveal(r) {
      const x = await X.POST("/targets/" + id + "/modlink/reveal", {id: r.id});
      if (!x.ok) { setRes(res, "err", "Ошибка: " + x.error); return null; }
      revealed[r.id] = x.data.password;
      draw();
      return x.data.password;
    }
    function extCell(r) {
      const t = tests[r.id], st = states[r.id] || {};
      if (t) return el("span", {class: "mono " + (t.ok ? "ok" : "bad"), text: t.text, title: t.title || ""});
      if (st.wan_ip) return el("span", {class: "mono", text: st.wan_ip, title: "проверено " + (st.wan_t ? new Date(st.wan_t * 1000).toLocaleString("ru-RU") : "")});
      if (st.wan_error) return el("span", {class: "mono bad", text: "нет выхода", title: st.wan_error});
      return el("span", {class: "muted", text: "—"});
    }

    async function rowAct(r, what, args, ask) {
      if (ask && !window.confirm(ask)) return null;
      out.textContent = "строка " + r.id + " · " + what + "…";
      const x = await X.run(id, "modlink", args);
      if (!x.ok) { out.textContent = "строка " + r.id + " · " + what + " — Ошибка: " + x.error; return x; }
      if (args[0] === "log") {
        const L = x.data && Array.isArray(x.data.lines) ? x.data.lines : rowsOf(x.data);
        out.textContent = "";
        showText("Журнал строки " + r.id + " · " + where, (L || []).join("\n") || "пусто");
        return x;
      }
      out.textContent = "строка " + r.id + " · " + what + ": " + mlResult(args[0], x.data);
      return x;
    }
    async function test(r) {
      tests[r.id] = {ok: true, text: "…"}; draw();
      const x = await X.run(id, "modlink", ["test", String(r.id)]);
      const d = x.ok ? x.data || {} : {};
      tests[r.id] = x.ok && d.proxy && d.proxy.ok
        ? {ok: true, text: d.proxy.ip + (d.hilink && d.hilink.ok ? "  ·H" : ""), title: mlResult("test", d)}
        : {ok: false, text: x.ok ? (d.proxy && d.proxy.error) || "нет ответа" : x.error, title: x.ok ? mlResult("test", d) : x.error};
      draw();
    }
    async function copyRow(r) {
      const x = await X.POST("/targets/" + id + "/modlink/export", {what: "all"});
      if (!x.ok) { setRes(res, "err", "Ошибка: " + x.error); return; }
      const row = (x.data.rows || []).find((c) => String(c[0]).split(":")[1] === String(r.port) && String(c[0]).split(":")[2] === r.login);
      if (!row) { setRes(res, "err", "строки нет среди включённых — «Применить»?"); return; }
      showTable("Строка " + r.id + " · " + where, x.data.columns, [row], "Прокси и ссылки — по колонкам. «Копировать для таблицы» — вставляется по ячейкам.");
    }
    async function copyAll(what) {
      const x = await X.POST("/targets/" + id + "/modlink/export", {what});
      if (!x.ok) { setRes(res, "err", "Ошибка: " + x.error); return; }
      const title = {proxy: "Только прокси", reboot: "Только ссылки ребута", reconnect: "Только ссылки реконнекта",
        all: "Прокси + ребут + реконнект"}[what] + " · " + where;
      showTable(title, x.data.columns, x.data.rows, "По одной строке на модем; колонки — через табуляцию: в Google-таблицу встают по ячейкам." +
        (S0 && S0.mode === "shared" ? " Порт управления общий — в ссылках пароль строки." : ""));
    }
    async function speed(r, line) {
      line.textContent = "тест… (до минуты)"; line.className = "speedline busy";
      const x = await X.run(id, "modlink", ["speedtest", String(r.id)]);
      if (x.ok) { line.textContent = fmtSpeed(x.data); line.className = "speedline ok"; }
      else { line.textContent = x.error; line.className = "speedline bad"; }
    }
    const fmtSpeed = (d) => "↓ " + (+d.down || 0).toFixed(1) + " · ↑ " + (+d.up || 0).toFixed(1) + " Мбит/с · " + Math.round(+d.ping || 0) + " мс" +
      (d.t ? " · " + new Date(d.t * 1000).toLocaleString("ru-RU") : "");

    function draw() {
      if (err) { tbl.replaceChildren(el("div", {class: "banner", text: "Ошибка: " + err})); return; }
      const head = el("tr", {}, el("th", {text: "№"}), ML_MAIN.map(([, t, title]) => el("th", {text: t, title: title || null})),
        el("th", {text: "Внешний IP"}), el("th"));
      const body = [];
      for (const r of orig) {
        const st = states[r.id];
        const gone = deleted.has(r.id), open = opened.has(r.id), off = val(r, "enabled") === false;
        const speedLine = el("div", {class: "speedline", text: st && st.speed ? (st.speed.error ? st.speed.error : fmtSpeed(st.speed)) : ""});
        body.push(el("tr", {"data-id": String(r.id), class: (gone ? "gone" : "") + (edits[r.id] ? " dirty" : "") + (off ? " off" : "")},
          el("td", {class: "ml-n", title: mlStateText(st)}, el("span", {class: "dot " + mlDot(st)}), String(r.id)),
          ML_MAIN.map(([k]) => el("td", {}, k === "password" ? pwCell(r) : inputFor(r, k))),
          el("td", {class: "ml-ext"}, extCell(r)),
          el("td", {class: "acts-cell"},
            btn("Test", () => test(r), "icon", "внешний IP через этот прокси + HiLink"),
            btn("⟳", () => rowAct(r, "реконнект", ["reconnect", String(r.id)]), "icon", "реконнект: сменить IP"),
            btn("⧉", () => copyRow(r), "icon", "копировать строку: прокси и ссылки"),
            btn(open ? "▴" : "▾", () => { open ? opened.delete(r.id) : opened.add(r.id); draw(); }, "icon", "ещё: порты, автореконнект, защита, ребут, скорость"),
            btn("✕", () => { if (gone) deleted.delete(r.id); else deleted.add(r.id); draw(); markDirty(); }, "icon danger", gone ? "не удалять" : "удалить строку (при «Применить»)"))));
        if (open) body.push(el("tr", {class: "ml-more"}, el("td"), el("td", {colspan: ML_MAIN.length + 2},
          el("div", {class: "ml-more-grid"}, ML_MORE.map(([k, t, title]) => el("label", {title: title || null}, el("span", {class: "muted", text: t}), inputFor(r, k)))),
          el("div", {class: "bar"}, inputFor(r, "enabled"), inputFor(r, "protect"),
            btn("Ребут модема", () => rowAct(r, "ребут", ["reboot", String(r.id)], "Перезагрузить модем строки " + r.id + " (" + (r.name || "") + ")?"), "", "через HiLink; модем вернётся минуты через 1–2"),
            btn("Спидтест", () => speed(r, speedLine), "", "скорость модема (yaspeed) — это трафик модема"),
            btn("Журнал", () => rowAct(r, "журнал", ["log", String(r.id)]), "link", "реконнекты, ребуты, тесты строки")),
          speedLine,
          el("div", {class: "muted", text: mlStateText(st)}))));
      }
      for (const a of added) {
        body.push(el("tr", {class: "new"}, el("td", {class: "ml-n", text: "+"}),
          ML_MAIN.map(([k]) => el("td", {}, inputFor(a, k, true))), el("td", {class: "muted", text: "новая"}),
          el("td", {class: "acts-cell"}, btn("✕", () => { added = added.filter((x) => x !== a); draw(); markDirty(); }, "icon danger", "убрать новую строку"))));
        body.push(el("tr", {class: "ml-more"}, el("td"), el("td", {colspan: ML_MAIN.length + 2},
          el("div", {class: "ml-more-grid"}, ML_MORE.map(([k, t, title]) => el("label", {title: title || null}, el("span", {class: "muted", text: t}), inputFor(a, k, true)))),
          el("div", {class: "bar"}, inputFor(a, "protect", true)))));
      }
      if (!body.length) body.push(el("tr", {}, el("td", {colspan: ML_MAIN.length + 3, class: "muted", text: "модемов нет — «+ Модем» или «Из интерфейсов»"})));
      tbl.replaceChildren(el("table", {class: "t ml"}, el("thead", {}, head), el("tbody", {}, body)));
      cnt.textContent = orig.length ? "модемов " + orig.length + (S0 ? " · " + (S0.mode === "shared" ? "один порт" : "свои порты") + " · " +
        (S0.traffic === "tcp" ? "только TCP" : "TCP+UDP") : "") : "";
    }

    async function load() {
      const r = await X.GET("/targets/" + id + "/modlink");
      if (!r.ok) { err = r.error; draw(); return; }
      const L = r.data.list, St = r.data.status;
      showSettings(r.data.settings);
      err = L.ok ? null : L.error;
      orig = L.ok ? rowsOf(L.data) : [];
      pending = !!(L.ok && L.data && L.data.pending);
      applyStatus(St);
      edits = {}; added = []; deleted = new Set(); revealed = {};
      draw(); markDirty();
    }
    function showLegacy(d) {
      if (!d || d.unit !== "legacy") { legacyBox.replaceChildren(); return; }
      legacyBox.replaceChildren(el("div", {class: "banner"},
        el("div", {text: "Здесь стоит старый modlink-linux" + (d.panel === "active" ? " (его панель :5000 работает)" : "") +
          ": его служба держит порты прокси, поэтому «Применить» не пройдёт. Перенос возьмёт его строки — порты и пароли как были," +
          " клиентам ничего менять не надо — и остановит старую панель."}),
        btn("Перенести из modlink-linux", migrate, "primary", "modlink migrate: сначала покажет, что перенесётся")));
    }
    async function migrate(e) {
      const r = await act(e.target, res, () => X.run(id, "modlink", ["migrate", "--dry-run"]), (d) => "к переносу строк: " + rowsOf(d).length);
      if (!r || !r.ok) return;
      const rows = rowsOf(r.data), warns = (r.data && r.data.warnings) || [];
      modal("Перенос из modlink-linux · " + where, el("div", {},
        el("p", {text: "Таблица modlink заменится строками старого modlink-linux. Правки, сделанные здесь до переноса, пропадут." +
          " Защита у перенесённых строк включится — снять можно галочкой после переноса."}),
        el("pre", {text: rows.map((x) => [x.id, x.name, ":" + x.port, x.lan_ip, x.modem_ip].join("  ")).join("\n") +
          (warns.length ? "\n\n" + warns.map((w) => "⚠ " + w).join("\n") : "")})), [
        {text: "Отмена"},
        {text: "Перенести " + rows.length, cls: "primary", onclick: async (close) => {
          close();
          await act(e.target, res, () => X.run(id, "modlink", ["migrate", "--force", "--stop-panel"], {confirm: String(id)}), "Перенесено");
          await load();
        }}]);
    }
    function applyStatus(St) {
      states = {};
      showLegacy(St && St.ok ? St.data : null);
      if (St && St.ok && St.data && typeof St.data === "object") {
        for (const x of rowsOf(St.data)) if (x && x.id != null) states[x.id] = x;
        if (!Array.isArray(St.data)) {
          const up = St.data.proxy === "active";
          pill.className = "pill"; pill.firstChild.className = "dot " + (up ? "ok" : "bad");
          pill.lastChild.textContent = "3proxy: " + (up ? "работает" : St.data.proxy || "—") + " · демон: " + (St.data.daemon === "active" ? "работает" : St.data.daemon || "—");
          engineEl.textContent = St.data.engine || "";
          if (St.data.pending != null) pending = !!St.data.pending;
        }
      } else { pill.lastChild.textContent = St && !St.ok ? "modlink status — Ошибка: " + St.error : "3proxy: —"; }
      for (const td of tbl.querySelectorAll("td.ml-n")) {
        const tr = td.closest("tr"), st = tr && states[tr.dataset.id];
        if (!st) continue;
        td.title = mlStateText(st);
        td.firstChild.className = "dot " + mlDot(st);
      }
      for (const tr of tbl.querySelectorAll("tr[data-id]")) {
        const r = orig.find((x) => String(x.id) === tr.dataset.id), cell = tr.querySelector("td.ml-ext");
        if (r && cell) cell.replaceChildren(extCell(r));
      }
      markDirty();
    }
    async function pollStatus() {
      const r = await X.GET("/targets/" + id + "/modlink?only=status");
      if (r.ok) applyStatus(r.data.status);
    }

    // ── кнопки над таблицей ──────────────────────────────────────────────
    const bAdd = btn("+ Модем", () => {
      added.push({tmp: ++seq, fields: Object.assign({}, ML_NEW)});
      draw(); markDirty();
    }, "", "новая строка: порт, логин и пароль подставятся сами (auto/gen)");
    const bIf = btn("Из интерфейсов", async (e) => {
      const r = await act(e.target, res, () => X.run(id, "modlink", ["from-ifaces"]), (d) => "предложено строк: " + rowsOf(d).length);
      if (!r || !r.ok) return;
      const sug = rowsOf(r.data);
      if (!sug.length) { setRes(res, "ok", "новых интерфейсов 192.168.N.100 нет — всё уже в таблице"); return; }
      modal("Строки по интерфейсам · " + where, el("div", {},
        el("p", {text: "modlink добавит строки (логин, пароль и порты — сам). В силу вступят после «Применить»."}),
        el("pre", {text: sug.map((x) => [x.lan_ip, x.modem_ip, x.dev || x.iface, x.name].filter(Boolean).join("  ")).join("\n")})), [
        {text: "Отмена"},
        {text: "Добавить " + sug.length, cls: "primary", onclick: async (close) => {
          close();
          await act(bIf, res, () => X.run(id, "modlink", ["from-ifaces", "--add"]), (d) => "добавлено строк: " + ((d && d.added) || []).length + " — «Применить»");
          await load();
        }}]);
    }, "", "modlink from-ifaces: строки по интерфейсам 192.168.N.100 сервера");
    const bApply = btn("Применить", async (e) => {
      if (deleted.size && !window.confirm("Удалить строки modlink: " + [...deleted].join(", ") + "? Прокси на их портах перестанут работать.")) return;
      const payload = {
        del: [...deleted],
        set: Object.entries(edits).filter(([k]) => !deleted.has(Number(k)) && !deleted.has(k)).map(([k, f]) => ({id: Number(k), fields: f})),
        add: added.map((a) => a.fields), apply: true};
      const r = await act(e.target, res, () => X.POST("/targets/" + id + "/modlink/save", payload), "Применено — 3proxy перечитал конфиг");
      if (r && !r.ok && r.data && r.data.steps) {
        out.textContent = r.data.steps.map((s) => (s.ok ? "✓ " : "✗ ") + s.what + (s.error ? ": " + s.error : "")).join("   ");
      }
      tests = {};
      await load();
    }, "primary", "сохранить правки → конфиг 3proxy → перечитать (соединения не рвутся) → проверка портов");
    const copyMenu = menuBtn("Копировать ▾", [["proxy", "Только прокси"], ["reboot", "Только ссылки ребута"],
      ["reconnect", "Только ссылки реконнекта"], ["all", "Прокси + ребут + реконнект"]], copyAll, "строки для клиента — с паролями");
    const bTbl = btn("Для proxyveth", async (e) => {
      const r = await act(e.target, res, () => X.POST("/targets/" + id + "/modlink/export", {table: true}), "Готово");
      if (r && r.ok) showTable("Строки для таблицы proxyveth · " + where, r.data.columns, r.data.rows,
        el("div", {}, ...r.data.warnings.map((w) => el("div", {class: "warn-line", text: "⚠ " + w})),
          el("div", {class: "muted", text: "Колонки n, real, proxy — как в таблице proxyveth ВМ этого хоста. n (номер виртуального модема)" +
            " можно поменять. У строк-поставщиков защита снята: proxyveth ходит через прокси к 192.168.N.1 за DNS и HiLink."})));
    }, "", "виртуальные модемы из этих настоящих: строки n,real,proxy,enabled для proxyveth");
    const logsMenu = menuBtn("Логи ▾", [["3proxy", "Логи 3proxy"], ["modlink", "Логи modlink (демон)"]], async (which) => {
      const x = await X.run(id, "modlink", ["logs", which]);
      if (!x.ok) { setRes(res, "err", "Ошибка: " + x.error); return; }
      showText((which === "3proxy" ? "Логи 3proxy" : "Логи modlink") + " · " + where, ((x.data && x.data.lines) || []).join("\n") || "пусто");
    }, "журналы служб");

    function menuBtn(label, items, onPick, title) {
      const menu = el("div", {class: "menu", hidden: true});
      const b = btn(label, (e) => { e.stopPropagation(); menu.hidden = !menu.hidden; }, "", title);
      for (const [v, t] of items) menu.append(el("button", {type: "button", text: t, onclick: (e) => { e.stopPropagation(); menu.hidden = true; onPick(v); }}));
      document.addEventListener("click", () => { menu.hidden = true; });
      return el("span", {class: "menu-wrap"}, b, menu);
    }

    const root = el("div", {}, legacyBox,
      el("div", {class: "bar"}, pill, engineEl, el("span", {class: "spacer"}), dirtyEl, bApply),
      netCard,
      el("div", {class: "bar ml-head"}, el("b", {text: "Модемы"}), cnt, el("span", {class: "spacer"}),
        copyMenu, bAdd, bIf, bTbl, logsMenu, btn("Обновить", () => load(), "link", "перечитать (несохранённое пропадёт)"), res),
      tbl, out);
    let loaded = false;
    const pl = poller(15000, pollStatus);
    return {
      el: root,
      show() { if (!loaded) { loaded = true; tbl.replaceChildren(el("div", {class: "item muted", text: "загрузка…"})); load(); } else pollStatus(); pl.start(); },
      hide() { pl.stop(); },
      dispose() { pl.stop(); },
    };
  }

  const ML_STATE = {ok: ["ok", "работает"], warn: ["warn", "модем/выход"], broken: ["bad", "сломан"],
    disabled: ["", "выключен"], pending: ["info", "ждёт «Применить»"]};
  const mlDot = (st) => st ? (ML_STATE[st.state] || ["bad"])[0] : "";
  function mlStateText(st) {
    if (!st) return "";
    const t = [(ML_STATE[st.state] || [, st.state])[1]];
    if (st.port_up === false) t.push("порт " + (st.port || "") + " не слушает");
    if (st.trigger_up === false) t.push("порт управления не слушает" + (st.trigger_error ? ": " + st.trigger_error : ""));
    if (st.wan_ip) t.push("внешний IP " + st.wan_ip + (st.wan_t ? " (" + new Date(st.wan_t * 1000).toLocaleTimeString("ru-RU") + ")" : ""));
    else if (st.wan_error) t.push("внешний IP: " + st.wan_error);
    if (st.iface) t.push("интерфейс " + st.iface);
    if (st.next) t.push("следующий реконнект " + new Date(st.next * 1000).toLocaleTimeString("ru-RU"));
    if (st.last) t.push("последний: " + (st.last.t ? new Date(st.last.t * 1000).toLocaleString("ru-RU") + " " : "") +
      (st.last.how || "") + " " + (st.last.ok ? "✓" : "✗") + " " + (st.last.text || "") + (st.last.dt ? " за " + st.last.dt + " с" : ""));
    return t.join(" · ");
  }
  function okErr(o, good) {
    if (!o || typeof o !== "object") return fmtVal(o);
    return o.ok ? "✓ " + good(o) : "✗ " + (o.error || "нет");
  }
  function mlResult(cmd, d) {
    if (!d || typeof d !== "object") return d == null ? "Применено" : String(d);
    if (cmd === "test")
      return "прокси " + okErr(d.proxy, (p) => "внешний IP " + (p.ip || "—")) + " · HiLink " +
        okErr(d.hilink, (h) => [h.status, h.net, h.signal].filter(Boolean).join(", ") || "отвечает") + (d.iface ? " · " + d.iface : "");
    if (cmd === "reconnect")
      return d.ok === false ? "✗ " + (d.error || "не вышло") : "✓ IP " + (d.ip || "—") + (d.same ? " (тот же!)" : "") + (d.dt ? " за " + d.dt + " с" : "");
    if (cmd === "reboot") return d.ok === false ? "✗ " + (d.error || "не вышло") : "✓ модем перезагружается";
    return summary(d, ["id"]) || "Применено";
  }

  // ── терминал ──────────────────────────────────────────────────────────────
  let xtermP = null;
  function loadXterm() {
    if (window.Terminal && window.FitAddon) return Promise.resolve();
    if (!xtermP) xtermP = new Promise((resolve, reject) => {
      const a = el("script", {src: "/static/vendor/xterm/xterm.js"});
      a.onload = () => {
        const b = el("script", {src: "/static/vendor/xterm/addon-fit.js"});
        b.onload = resolve; b.onerror = reject; document.head.append(b);
      };
      a.onerror = reject;
      document.head.append(a);
    });
    return xtermP;
  }

  class Term {
    constructor(target, box, stateEl, wsPath) { this.target = target; this.box = box; this.stateEl = stateEl; this.wsPath = wsPath || "/ws/term"; this.enc = new TextEncoder(); }
    async open() {
      try { await loadXterm(); } catch (e) { this.state("xterm.js не загрузился", "err"); return; }
      this.t = new window.Terminal({cursorBlink: true, fontSize: 13, scrollback: 5000,
        fontFamily: 'ui-monospace, SFMono-Regular, Menlo, Consolas, "DejaVu Sans Mono", monospace',
        theme: {background: "#0f1115", foreground: "#d8dee9"}});
      this.fit = new window.FitAddon.FitAddon();
      this.t.loadAddon(this.fit);
      this.t.open(this.box);
      this.refit();
      this.t.onData((d) => this.send(this.enc.encode(d)));
      this.t.onBinary((d) => this.send(Uint8Array.from(d, (c) => c.charCodeAt(0) & 255)));
      this.t.onResize(({cols, rows}) => this.ctl({type: "resize", cols, rows}));
      this.ro = new ResizeObserver(() => this.refit());
      this.ro.observe(this.box);
      this.connect();
    }
    refit() { try { if (this.box.offsetParent) this.fit.fit(); } catch (e) { /* скрыт */ } }
    state(text, cls) { this.stateEl.className = "res " + (cls || ""); this.stateEl.textContent = text; }
    connect() {
      if (!this.t) return;
      if (this.ws && this.ws.readyState <= 1) return;
      const proto = location.protocol === "https:" ? "wss:" : "ws:";
      const url = proto + "//" + location.host + this.wsPath + "?target=" + encodeURIComponent(this.target) +
        "&cols=" + this.t.cols + "&rows=" + this.t.rows;
      this.state("подключение…", "run");
      const ws = this.ws = new WebSocket(url);
      ws.binaryType = "arraybuffer";
      ws.onopen = () => { this.state("подключено", "ok"); this.ctl({type: "resize", cols: this.t.cols, rows: this.t.rows}); this.t.focus(); };
      ws.onmessage = (ev) => this.t.write(typeof ev.data === "string" ? ev.data : new Uint8Array(ev.data));
      ws.onclose = (ev) => {
        const why = ev.reason || (ev.code === 1006 ? "соединение оборвалось" : "закрыто");
        this.state("отключено: " + why, "err");
        this.t.write("\r\n\x1b[2m[" + why + " — «Переподключить», чтобы открыть заново]\x1b[0m\r\n");
      };
    }
    send(bytes) { if (this.ws && this.ws.readyState === 1) this.ws.send(bytes); }
    ctl(o) { if (this.ws && this.ws.readyState === 1) this.ws.send(JSON.stringify(o)); }
    type(cmd) {
      if (!this.ws || this.ws.readyState !== 1) { this.connect(); return; }
      this.send(this.enc.encode(cmd + "\r"));
      this.t.focus();
    }
    close() {
      if (this.ro) this.ro.disconnect();
      if (this.ws) { this.ws.onclose = null; try { this.ws.close(1000, "вкладка закрыта"); } catch (e) { /* уже */ } }
      if (this.t) this.t.dispose();
    }
  }

  const QUICK = ["proxyveth status --wan", "proxyveth problems", "modlink status", "hivelink status"];
  function termTab(X, target, full) {
    const st = el("span", {class: "res"});
    const box = el("div", {class: "term-box" + (full ? " full" : "")});
    let t = null;
    const quick = (target === "host" ? ["hivelink status"] : QUICK).map((q) =>
      btn(q, () => t && t.type(q), "", "напечатать в консоль и выполнить"));
    const root = el("div", {},
      el("div", {class: "term-bar"}, quick, el("span", {class: "spacer"}), st,
        btn("Переподключить", () => { if (t) { t.t.write("\r\n"); t.connect(); } }, "link")),
      box);
    return {
      el: root,
      show() { if (!t) { t = new Term(target, box, st, X.wsPath); t.open(); } else { t.refit(); if (t.t) t.t.focus(); } },
      dispose() { if (t) t.close(); },
    };
  }

  // ── вкладка «настройка и управление» ─────────────────────────────────────
  function setTab(X, id) {
    const T = "/api/targets/" + id;
    const H = String(id) === "host";
    const facts = el("div", {class: "muted"});
    const dns = el("input", {placeholder: "1.1.1.1 8.8.8.8 (необязательно)"});
    const rDns = actRow("Починить DNS", "resolved, netplan без DNS от DHCP, проверка github, Google, mp.space", dns,
      btn("Починить", (e) => act(e.target, rDns.res, () => POST(T + "/dns", {dns: dns.value}), "Применено")));
    const pw = el("input", {type: "password", autocomplete: "new-password", placeholder: "новый пароль root"});
    const rPw = actRow("Пароль root", H ? "пароль root этого хоста (вход по SSH)" : "пароль root этой ВМ (вход по SSH и в консоль Proxmox)", pw,
      btn("Сохранить", async (e) => {
        const r = await act(e.target, rPw.res, () => POST(T + "/passwd", {password: pw.value}), "Сохранено");
        if (r && r.ok) pw.value = "";
      }));
    const key = el("input", {placeholder: "ssh-ed25519 AAAA… имя"});
    const rKey = actRow("Ключ SSH", H ? "добавить свой открытый ключ root" : "добавить свой открытый ключ root или сменить ключ, которым ходит хаб", key,
      btn("Добавить", async (e) => {
        const r = await act(e.target, rKey.res, () => POST(T + "/key", {pubkey: key.value}), "Сохранено");
        if (r && r.ok) key.value = "";
      }),
      H ? null : btn("Сменить ключ хаба", (e) => {
        if (!window.confirm("Сменить ключ, которым хаб ходит на ВМ " + id + "?")) return;
        act(e.target, rKey.res, () => POST(T + "/key", {rotate: true}), (d) => "Применено" + (d && d.fingerprint ? ": " + d.fingerprint : ""));
      }));
    const mpState = el("span", {class: "muted"});
    const auth = el("textarea", {rows: 2, placeholder: "содержимое auth.mp из ЛК mobileproxy.space (или выберите файл)", style: "max-width:420px"});
    const file = el("input", {type: "file", onchange: async (e) => { const f = e.target.files[0]; if (f) auth.value = (await f.text()).trim(); }});
    const rMp = actRow("mobileproxy.space", H ? "софт агрегатора прямо на этом хосте (только Ubuntu): установка и ключ auth.mp; в конце хост перезагрузится"
      : "софт агрегатора на этой ВМ: установка и ключ auth.mp", mpState,
      btn("Установить", (e) => act(e.target, rMp.res, () => POST(T + "/mpspace", {action: "install"}), "Применено", rMp.jobs)),
      btn("Проверить", (e) => act(e.target, rMp.res, () => POST(T + "/mpspace", {action: "check"}), (d) => summary(d && d.result !== undefined ? d.result : d))),
      auth, file,
      btn("Сохранить auth.mp", async (e) => {
        const r = await act(e.target, rMp.res, () => POST(T + "/mpspace", {action: "auth", auth: auth.value}), "Сохранено", rMp.jobs);
        if (r && r.ok) auth.value = "";
      }));
    const hlState = el("span", {class: "muted", text: "—"});
    const rHl = actRow(H ? "hivelink на хост" : "hivelink в эту ВМ", H ? "драйвер настоящих модемов, воткнутых в этот хост"
      : "драйвер настоящих модемов — если они воткнуты прямо в ВМ (проброс USB)", hlState,
      btn("Установить / обновить", async (e) => {
        const r = await act(e.target, rHl.res, () => X.run(id, "hivelink", ["install"]), "Применено");
        if (r && r.ok) loadHl();
      }));
    const modeSel = el("select", {}, el("option", {value: "usb", text: "USB — до 40 модемов"}), el("option", {value: "gw", text: "шлюз — без USB"}));
    const modeCur = el("span", {class: "muted"});
    const rMode = actRow("Режим proxyveth", "смена снимает все модемы и поднимает их в новом режиме — подтверждение " + (H ? "словом host" : "номером ВМ"), modeCur, modeSel,
      btn("Сменить…", async (e) => {
        const s = X.ent(id) || {};
        const m = modeSel.value;
        if (m === s.mode) { setRes(rMode.res, "ok", "уже " + modeShort(m)); return; }
        const v = await confirmNumber("Сменить режим proxyveth: " + tname(id),
          "Режим " + modeShort(s.mode) + " → " + modeShort(m) + ". Все модемы (" + tname(id) + ") будут сняты и подняты заново.", id, "Сменить режим");
        if (v == null) return;
        await act(e.target, rMode.res, () => X.run(id, "proxyveth", ["mode", m, "--yes"], {confirm: v}), "Применено");
        refresh();
      }, "danger"));
    const rUp = actRow(H ? "Обновить PCS" : "Обновить агентов", H ? "код PCS с GitHub — на хост" + (X.isPve() ? " и все его ВМ" : "")
      : "код PCS и части на этой ВМ — той же версии, что на хосте", el("span", {class: "muted", text: ""}),
      btn("Обновить", (e) => act(e.target, rUp.res, () => POST(T + "/update", {}), "Применено", rUp.jobs)));

    async function loadHl() {
      const r = await X.run(id, "hivelink", ["status"]);
      hlState.textContent = !r.ok ? "Ошибка: " + r.error : r.data && r.data.installed === false ? "не установлен"
        : "установлен" + (r.data && r.data.version ? " · " + r.data.version : "");
    }
    async function loadFacts() {
      const r = await X.GET("/targets/" + id);
      if (!r.ok) { facts.textContent = "Ошибка: " + r.error; return; }
      const d = r.data || {}, v = d.versions || {};
      if (H) {
        facts.textContent = [d.os, d.ip, d.cpu && d.cpu.cores && d.cpu.cores + " ядер", d.ram && "ОЗУ " + gb(d.ram),
          "PCS " + (d.version || "—"), d.proxyveth && (d.proxyveth.mode ? "proxyveth " + modeShort(d.proxyveth.mode) : "proxyveth: режим не задан")]
          .filter(Boolean).join(" · ");
        return;
      }
      facts.textContent = [d.os, d.cores && d.cores + " ядер", d.ram_gb && "ОЗУ " + d.ram_gb + " ГБ", d.disk_gb && "диск " + d.disk_gb + " ГБ",
        Object.keys(v).length && "версии: " + Object.entries(v).map(([k, x]) => k + " " + (x || "—")).join(", ")].filter(Boolean).join(" · ");
    }
    function update() {
      const s = X.ent(id) || {};
      const h = s.host || {};
      mpState.textContent = H && h.mpspace_ok === false ? "на хосте Proxmox mp.space не ставится — он меняет GRUB и ядро и"
        + " перезагружает машину, легли бы все ВМ; ставь в ВМ" : mpText(s.mpspace);
      for (const b of rMp.ctl.querySelectorAll("button, textarea, input")) b.disabled = H && h.mpspace_ok === false;
      modeCur.textContent = "сейчас: " + modeShort(s.mode);
      if (s.mode && s.mode !== shownMode) { shownMode = s.mode; modeSel.value = s.mode === "usb" ? "gw" : "usb"; }
    }
    let shownMode = null;
    if (X.node) for (const r of [rMp, rUp]) r.jobs.dataset.base = X.base;
    const power = (action, text, ask, cls) => btn(text, async (e) => {
      if (ask && !window.confirm(ask)) return;
      const r = await act(e.target, rPower.res, () => X.POST("/servers/" + id + "/power", {action}), (d) => "ВМ: " + (d && d.state || "готово"));
      if (r && r.ok) (X.node ? refreshNodes() : refresh());
    }, cls);
    const rPower = actRow("Питание ВМ", "через Proxmox: включить, выключить штатно (до 2 мин), перезагрузить, остановить жёстко", el("span"),
      power("start", "▶ Включить"), power("shutdown", "⏻ Выключить", "Выключить ВМ " + id + "? Модемы на ней пропадут."),
      power("reboot", "↻ Перезагрузить", "Перезагрузить ВМ " + id + "?"),
      power("stop", "■ Стоп", "Жёстко остановить ВМ " + id + "? Как выдернуть питание.", "danger"));
    const rColor = actRow("Цвет", "цвет ВМ в дереве и на плашке — один из семи", colorPick((X.srv(id) || {}).color, async (c) => {
      const r = await X.POST("/servers/" + id + "/color", {color: c});
      if (r.ok) (X.node ? refreshNodes() : refresh());
      return r.ok;
    }));
    const rForget = actRow("Убрать из PCS", "PCS перестанет управлять ВМ; сама ВМ, её диски и модемы останутся (вернуть — «Добавить в PCS» на обзоре хоста)",
      el("span"), btn("Убрать из PCS…", () => forgetVm(X, X.srv(id) || {id}), "danger"));
    const fpText = el("span", {class: "mono", text: "…"});
    const rLink = actRow("Связь хостов", "чтобы управлять этим хостом с хоста Proxmox: там «+ Присоединить хост» → адрес этого хоста, логин и пароль"
      + " этой панели; отпечаток — сверить (хаб ходит сюда только по HTTPS к этому сертификату)", fpText);
    const root = el("div", {}, facts, el("div", {class: "acts", style: "margin-top:10px"}, H ? null : rPower.el, H ? null : rColor.el,
      rDns.el, rPw.el, rKey.el, rMp.el, rHl.el, rMode.el, rUp.el, H ? (X.node ? null : rLink.el) : rForget.el));
    let loaded = false;
    async function loadFp() {
      const r = await X.GET("/self");
      fpText.textContent = r.ok && r.data.fingerprint ? r.data.fingerprint : "HTTPS выключен — pcs web on (сертификат создастся сам)";
    }
    return {el: root, update, show() { update(); if (!loaded) { loaded = true; loadHl(); loadFacts(); if (H) loadFp(); } }};
  }

  // ── создать ВМ ────────────────────────────────────────────────────────────
  function pageCreate(X) {
    const f = {
      name: el("input", {placeholder: "pcs3000", required: true, pattern: "[A-Za-z0-9][A-Za-z0-9-]*"}),
      cores: el("input", {type: "number", min: 1, max: 256, value: "4"}),
      ram: el("input", {type: "number", min: 1, max: 4096, value: "8"}),
      disk: el("input", {type: "number", min: 8, max: 65536, value: "40"}),
      sheet: el("input", {type: "url", placeholder: "https://docs.google.com/spreadsheets/d/…/edit#gid=0"}),
      mp: el("input", {type: "checkbox"}),
    };
    const usb = el("input", {type: "radio", name: "mode", value: "usb", checked: true});
    const gw = el("input", {type: "radio", name: "mode", value: "gw"});
    const res = el("span", {class: "res"});
    const submit = el("button", {type: "submit", class: "primary"}, "Создать");
    const form = el("form", {class: "form"},
      el("label", {}, "Имя ВМ (это и имя хоста)", f.name),
      el("div", {class: "row3"}, el("label", {}, "Ядра", f.cores), el("label", {}, "ОЗУ, ГБ", f.ram), el("label", {}, "Диск, ГБ", f.disk)),
      el("div", {class: "radio"}, el("b", {text: "Режим proxyveth"}),
        el("label", {}, usb, el("span", {}, "USB — модем выглядит как USB Huawei E3372h; ", el("span", {class: "muted", text: "до 40 на сервер"}))),
        el("label", {}, gw, el("span", {}, "шлюз — без USB и без такого предела"))),
      el("label", {}, "Ссылка на таблицу модемов (можно потом)", f.sheet),
      el("label", {class: "inline"}, f.mp, "поставить mobileproxy.space"),
      el("div", {class: "bar"}, submit, res));
    form.addEventListener("submit", async (ev) => {
      ev.preventDefault();
      const body = {name: f.name.value.trim(), cores: +f.cores.value, ram_gb: +f.ram.value, disk_gb: +f.disk.value,
        mode: gw.checked ? "gw" : "usb", sheet: f.sheet.value.trim(), mpspace: f.mp.checked};
      const r = await act(submit, res, () => X.POST("/servers", body), "Запущено");
      if (r && r.ok && r.data && r.data.job != null) location.hash = X.href("/job/" + encodeURIComponent(r.data.job));
    });
    return {kind: "create", node: X.node, el: el("div", {}, el("h1", {text: "Создать ВМ" + (X.node ? " на хосте " + X.node : "")}),
      el("p", {class: "muted", text: "Ubuntu 24.04 под ключ: ОС, PCS, proxyveth в выбранном режиме, модемы по таблице, по желанию — mobileproxy.space. Долго — журнал ниже обновляется сам."}),
      form), update() { document.title = "Создать ВМ — PCS"; }};
  }

  function pageJob(X, jid) {
    return {kind: "job", el: el("div", {}, el("h1", {text: "Задание"}), jobView(jid, {page: true, base: X.node ? X.base : undefined})), update() { document.title = "Задание — PCS"; }};
  }

  // ── присоединить хост (VPS или другой Proxmox) ──────────────────────────
  function pageJoin() {
    const f = {addr: el("input", {placeholder: "95.165.83.236 или vps.example.ru (порт 666 — по умолчанию)", required: true}),
      user: el("input", {value: "admin", autocomplete: "off"}),
      pw: el("input", {type: "password", autocomplete: "new-password", required: true}),
      name: el("input", {placeholder: "необязательно — по умолчанию имя того хоста"}),
      fp: el("input", {placeholder: "необязательно — AB:CD:… со страницы того хоста (настройка)"})};
    const res = el("span", {class: "res"});
    const submit = el("button", {type: "submit", class: "primary"}, "Присоединить");
    const form = el("form", {class: "form"},
      el("p", {class: "muted", text: "На том хосте уже стоит PCS (bash <(curl …/install.sh)) и открыт порт 666. Логин и пароль — его панели: " +
        "нужны один раз, хаб получит свой токен и дальше ходит только по HTTPS к сертификату того хоста."}),
      el("label", {}, "Адрес хоста", f.addr), el("div", {class: "row3"}, el("label", {}, "Логин панели", f.user), el("label", {}, "Пароль панели", f.pw),
        el("label", {}, "Имя здесь", f.name)),
      el("label", {}, "Отпечаток сертификата (проверить, что это он)", f.fp), el("div", {class: "bar"}, submit, res));
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const r = await act(submit, res, () => POST("/api/nodes/join", {addr: f.addr.value, user: f.user.value, password: f.pw.value,
        name: f.name.value, fingerprint: f.fp.value}), (d) => "Присоединён: " + d.name);
      f.pw.value = "";
      if (r && r.ok) { await refreshNodes(); location.hash = "#/n/" + encodeURIComponent(r.data.name); }
    });
    return {kind: "join", el: el("div", {}, el("h1", {text: "Присоединить хост"}), form), update() { document.title = "Присоединить хост — PCS"; }};
  }


  function nodeTools(X) {
    const name = X.node;
    const res = el("span", {class: "res"}), jobs = el("div", {class: "joblog-slot"}), fp = el("span", {class: "muted mono"});
    const n0 = (S.nodes || []).find((x) => x.name === name) || {};
    const bOpen = btn("Его панель в новой вкладке", async (e) => {
      const w = window.open("about:blank", "_blank");
      const r = await act(e.target, res, () => POST("/api/nodes/" + encodeURIComponent(name) + "/login", {}), "Открыто");
      if (r && r.ok && w) w.location = r.data.url; else if (w) w.close();
    }, "", "вход в его собственную панель без пароля: одноразовая ссылка на 60 с");
    const bUp = btn("Обновить PCS на нём", (e) => act(e.target, res, () => X.POST("/targets/host/update", {}), "Запущено")
      .then((r) => { if (r && r.ok && r.data && r.data.job != null) jobs.replaceChildren(jobView(r.data.job, {base: X.base, title: "Обновление PCS на " + name})); }));
    const bDel = btn("Отсоединить", async () => {
      const v = await confirmNumber("Отсоединить хост " + name, "Хаб забудет хост " + name + ", а тот отзовёт наш токен. Модемы, ВМ и софт на нём не тронутся.", name, "Отсоединить");
      if (v == null) return;
      const r = await act(null, res, () => POST("/api/nodes/" + encodeURIComponent(name) + "/remove", {confirm: v}), "Отсоединён");
      if (r && r.ok) { await refreshNodes(); location.hash = "#/"; }
    }, "danger");
    const colors = colorPick(n0.color, async (c) => {
      const r = await POST("/api/nodes/" + encodeURIComponent(name) + "/color", {color: c});
      if (r.ok) refreshNodes();
      return r.ok;
    });
    const el_ = el("div", {class: "node-bar"}, el("span", {class: "muted", text: "присоединённый хост · цвет "}), colors,
      el("span", {class: "spacer"}), bOpen, bUp, bDel, res, jobs);
    return {el: el("div", {}, el_, fp), update() {
      const n = (S.nodes || []).find((x) => x.name === name) || {};
      fp.textContent = n.host ? n.host + ":" + n.port + " · сертификат " + (n.fingerprint || "—") : "";
    }};
  }

  async function refreshNodes() {
    const r = await GET("/api/nodes");
    if (r.ok) { S.nodes = r.data || []; renderTree(); if (S.page && S.page.node && S.page.update) S.page.update(); }
  }

  // ── маршруты и опрос ──────────────────────────────────────────────────────
  function parse() {
    let p = location.hash.replace(/^#\/?/, "").split("/").filter(Boolean).map(decodeURIComponent);
    let node = null;
    if (p[0] === "n" && p[1]) { node = p[1]; p = p.slice(2); }
    if (p[0] === "vm" && /^\d+$/.test(p[1] || "")) return {node, kind: "vm", id: p[1], tab: p[2] || "proxyveth"};
    if (p[0] === "create") return {node, kind: "create"};
    if (p[0] === "job" && p[1]) return {node, kind: "job", id: p[1]};
    if (!node && p[0] === "join") return {kind: "join"};
    if (p[0] === "host") return {node, kind: "host", tab: p[1] || ""};
    return {node, kind: "host", tab: ""};
  }
  function route() {
    const r = parse();
    const same = S.page && (S.page.node || null) === (r.node || null) && S.page.kind === r.kind &&
      (r.kind === "host" || (r.kind === "vm" && S.page.id === r.id));
    if (same) { S.page.show(r.tab); renderTree(); return; }
    if (S.page && S.page.dispose) S.page.dispose();
    const X = ctx(r.node);
    const page = r.kind === "vm" ? pageVm(X, r.id) : r.kind === "create" ? pageCreate(X) : r.kind === "job" ? pageJob(X, r.id)
      : r.kind === "join" ? pageJoin() : pageHost(X);
    S.page = page;
    $("#main").replaceChildren(page.el);
    if (page.update) page.update();
    if (page.show) page.show(r.tab);
    renderTree();
    $("#main").scrollTop = 0;
  }

  let refreshing = null;
  function refresh() {
    if (refreshing) return refreshing;
    refreshing = (async () => {
      const r = await GET("/api/overview");
      if (r.ok) { S.ov = r.data; S.ovErr = null; } else S.ovErr = "Хаб: " + r.error;
      renderHeader(); renderTree();
      if (S.page && S.page.update) S.page.update();
    })().finally(() => { refreshing = null; });
    return refreshing;
  }

  function themeBtn() {
    const order = ["auto", "light", "dark"], names = {auto: "◐", light: "☀", dark: "☾"};
    let t = "auto";
    try { t = localStorage.getItem("pcs-theme") || "auto"; } catch (e) { /* без хранилища */ }
    const b = $("#theme");
    const set = (v) => {
      t = v;
      if (v === "auto") document.documentElement.removeAttribute("data-theme"); else document.documentElement.setAttribute("data-theme", v);
      try { localStorage.setItem("pcs-theme", v); } catch (e) { /* без хранилища */ }
      b.textContent = names[v]; b.title = "тема: " + ({auto: "как в системе", light: "светлая", dark: "тёмная"})[v];
    };
    set(t);
    b.addEventListener("click", () => set(order[(order.indexOf(t) + 1) % order.length]));
  }

  async function boot() {
    themeBtn();
    const r = await GET("/api/session");
    if (!r.ok) return;
    S.session = r.data;
    $("#logout").addEventListener("click", async () => { await POST("/api/logout", {}); location.href = "/login"; });
    renderHeader();
    await refresh();
    window.addEventListener("hashchange", route);
    route();
    poller(10000, refresh).start();
    refreshNodes();
    poller(15000, refreshNodes).start();
    document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(); });
  }
  boot();
})();

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

  // Иконки — свой набор, одна толщина линии (24×24, stroke 1.7). Только украшение: подпись — в title/aria-label.
  const ICONS = {
    play: "M7 5.5v13l11-6.5z", stop: "M7 7h10v10H7z", redo: "M20 11a8 8 0 1 0-2.4 5.7M20 4.5V11h-6.5",
    pulse: "M3 12h4l2.5-6 4 12 2.5-6H21", more: "M5.5 12h.01M12 12h.01M18.5 12h.01", chev: "M6 9l6 6 6-6",
    term: "M4 6l5 5-5 5M11 18h9", plus: "M12 5v14M5 12h14", search: "M10.5 17a6.5 6.5 0 1 0 0-13 6.5 6.5 0 0 0 0 13zM20 20l-4.6-4.6",
    copy: "M9 9h11v11H9zM5 15V4h11", power: "M12 3v8M6.4 6.6a8 8 0 1 0 11.2 0", bolt: "M13 3 5 14h6l-1 7 8-11h-6z",
    link: "M10 14a4 4 0 0 0 5.7 0l3-3a4 4 0 0 0-5.7-5.7l-1 1M14 10a4 4 0 0 0-5.7 0l-3 3a4 4 0 0 0 5.7 5.7l1-1",
    sync: "M4 12a8 8 0 0 1 14-5.3L20 9M20 4v5h-5M20 12a8 8 0 0 1-14 5.3L4 15M4 20v-5h5",
    edit: "M4 20h4L19 9l-4-4L4 16zM13.5 6.5l4 4", trash: "M4 7h16M10 11v6M14 11v6M6 7l1 13h10l1-13M9 7V4h6v3",
    out: "M14 4h6v6M20 4l-9 9M18 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h5",
    menu: "M4 7h16M4 12h16M4 17h16", list: "M8 6h12M8 12h12M8 18h12M4 6h.01M4 12h.01M4 18h.01",
    gauge: "M12 14l4-4M4.9 19a9 9 0 1 1 14.2 0", wand: "M15 4V2M15 10V8M11 6H9M21 6h-2M5 21l10-10M17.8 3.8l1.4 1.4",
  };
  function ic(name) {
    const NS = "http://www.w3.org/2000/svg";
    const s = document.createElementNS(NS, "svg");
    s.setAttribute("viewBox", "0 0 24 24"); s.setAttribute("class", "ic"); s.setAttribute("aria-hidden", "true");
    const p = document.createElementNS(NS, "path");
    p.setAttribute("d", ICONS[name] || "");
    if (name === "play" || name === "stop") p.setAttribute("fill", "currentColor");
    if (name === "more") {                 // три точки — кружками, иначе их почти не видно
      for (const x of [6, 12, 18]) {
        const c = document.createElementNS(NS, "circle");
        c.setAttribute("cx", String(x)); c.setAttribute("cy", "12"); c.setAttribute("r", "1.6"); c.setAttribute("fill", "currentColor"); c.setAttribute("stroke", "none");
        s.append(c);
      }
      return s;
    }
    s.append(p);
    return s;
  }
  // кнопка-иконка с подписью для экранных читалок и подсказкой; label — короткая видимая подпись (по желанию)
  function ibtn(icon, title, onclick, cls, label) {
    return el("button", {type: "button", class: cls || "", title, "aria-label": title, onclick}, ic(icon), label ? el("span", {class: "lbl", text: label}) : null);
  }
  // всплывающее сообщение внизу справа
  function toast(text, kind) {
    const box = $("#toasts");
    if (!box) return;
    const t = el("div", {class: "toast"}, el("span", {class: "dot " + (kind || "ok")}), el("div", {text}));
    box.append(t);
    setTimeout(() => { t.classList.add("out"); setTimeout(() => t.remove(), 260); }, kind === "bad" ? 6000 : 3200);
  }
  // меню по кнопке: пункты [значение, подпись, иконка?, класс?] или "-" (черта)
  function menuBtn(label, items, onPick, title, opts) {
    opts = opts || {};
    const left = opts.left != null ? opts.left : label !== null;      // кнопка с подписью — меню вправо от неё; «⋯» в конце строки — влево
    const menu = el("div", {class: "menu" + (left ? " left" : ""), hidden: true, role: "menu"});
    const b = label === null ? ibtn("more", title || "ещё", null, "ghost icon") : btn(label, null, opts.cls || "", title);
    if (label !== null) b.append(ic("chev"));
    b.setAttribute("aria-haspopup", "menu");
    const close = () => { menu.hidden = true; b.setAttribute("aria-expanded", "false"); };
    b.addEventListener("click", (e) => {
      e.stopPropagation();
      for (const m of document.querySelectorAll(".menu")) if (m !== menu) m.hidden = true;
      menu.hidden = !menu.hidden; b.setAttribute("aria-expanded", String(!menu.hidden));
    });
    for (const it of items) {
      if (it === "-") { menu.append(el("hr")); continue; }
      if (!it) continue;
      const [v, t, icon, cls, hint] = it;
      menu.append(el("button", {type: "button", role: "menuitem", class: cls || "", onclick: (e) => { e.stopPropagation(); close(); onPick(v, b); }},
        icon ? ic(icon) : null, el("span", {}, t, hint ? el("span", {class: "hint", text: hint}) : null)));
    }
    return el("span", {class: "menu-wrap"}, b, menu);
  }
  document.addEventListener("click", () => { for (const m of document.querySelectorAll(".menu")) m.hidden = true; });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") for (const m of document.querySelectorAll(".menu")) m.hidden = true; });
  // плавный счёт числа при первом показе (один раз)
  function countTo(node, to, suffix) {
    const v = Number(to);
    if (!isFinite(v) || matchMedia("(prefers-reduced-motion: reduce)").matches || node.dataset.counted) {
      node.textContent = String(to) + (suffix || ""); return;
    }
    node.dataset.counted = "1";
    const t0 = performance.now(), dur = 650;
    const step = (t) => {
      const k = Math.min(1, (t - t0) / dur), e = 1 - Math.pow(1 - k, 3);
      node.textContent = String(Math.round(v * e)) + (suffix || "");
      if (k < 1) requestAnimationFrame(step);
    };
    requestAnimationFrame(step);
  }
  // скелет на время первой загрузки: n строк по образцу таблицы
  function skelRows(n) {
    return el("div", {class: "skel-rows"}, Array.from({length: n || 6}, (_, i) => el("div", {},
      ...[30, 70, 55, 40, 85].map((w) => el("span", {class: "skel", style: "width:" + Math.max(20, w - (i * 7) % 25) + "%"})))));
  }
  function stagger(container) {
    if (!container || container.dataset.staggered) return;
    container.dataset.staggered = "1";
    container.classList.add("stagger");
    [...container.children].forEach((c, i) => c.style.setProperty("--i", String(i)));
  }

  // ── API ───────────────────────────────────────────────────────────────────
  const S = {session: null, ov: null, ovErr: null, page: null};

  // Полоса сверху — только для того, что запустил человек: фоновый опрос её не дёргает.
  let inflight = 0, pollDepth = 0, busyT = null;
  function busy(d) {
    inflight = Math.max(0, inflight + d);
    const p = $("#progress");
    if (!p) return;
    clearTimeout(busyT);
    if (inflight > 0) busyT = setTimeout(() => p.classList.add("on"), 180);
    else p.classList.remove("on");
  }
  async function api(method, path, body) {
    const quiet = pollDepth > 0;
    if (!quiet) busy(1);
    try { return await api0(method, path, body); } finally { if (!quiet) busy(-1); }
  }
  async function api0(method, path, body) {
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

  // Журнал фонового задания хаба: опрос, пока не кончится. Этапы — строки «── что ──» из вывода
  // команды (ui.hdr): видно, где задание сейчас, без чтения всего журнала; часы — сколько идёт.
  const STAGE_RE = /^\s*(?:\d\d:\d\d:\d\d\s+)?── (.+?) ──\s*$/;
  const clock = (sec) => { sec = Math.max(0, Math.floor(sec)); const m = Math.floor(sec / 60), s = sec % 60; return m >= 60 ? Math.floor(m / 60) + " ч " + (m % 60) + " мин" : m + ":" + String(s).padStart(2, "0"); };
  function jobView(jid, opts) {
    opts = opts || {};
    const st = el("span", {class: "res run", text: "идёт…"});
    const timer = el("span", {class: "job-time num"});
    const title = el("b", {text: opts.title || "Задание " + jid});
    const stages = el("ol", {class: "stages", hidden: true});
    const pre = el("pre", {text: "…"});
    const raw = opts.page ? el("div", {}, pre) : pre;
    const tail = el("div");
    const box = el("div", {class: "joblog" + (opts.page ? " page" : "")},
      el("div", {class: "job-head"}, title, st, timer, el("span", {class: "spacer"}),
        opts.page || opts.base ? null : el("a", {href: "#/job/" + encodeURIComponent(jid), class: "muted", text: "журнал отдельно"})),
      stages, raw, tail);
    let first = true, started = 0, finished = 0, tm = null, seen = 0;
    function tickClock() {
      if (!box.isConnected && !first) { clearInterval(tm); tm = null; return; }
      if (started) timer.textContent = (finished ? "за " : "") + clock((finished || Date.now() / 1000) - started);
    }
    function drawStages(log, state) {
      // этапы и замечания (⚠) внутри каждого
      const names = [], warned = [];
      for (const l of log) {
        const m = STAGE_RE.exec(l);
        if (m) { names.push(m[1]); warned.push(false); }
        else if (names.length && /⚠/.test(l)) warned[warned.length - 1] = true;
      }
      stages.hidden = !names.length;
      if (!names.length || names.length === seen && state === "running") return;
      seen = names.length;
      const prev = stages.childElementCount;
      stages.replaceChildren(...names.map((n, i) => {
        const last = i === names.length - 1;
        let cls = !last || state === "done" ? "done" : state === "running" ? "now" : "fail";
        if (cls === "done" && warned[i]) cls = "warn";
        return el("li", {class: cls + (i >= prev ? " fresh" : "")}, el("span", {class: "mk", "aria-hidden": "true"}), el("span", {text: n}));
      }));
    }
    async function tick() {
      if (!first && !box.isConnected) return;
      first = false;
      const r = await GET((opts.base || "/api") + "/jobs/" + encodeURIComponent(jid));
      if (!r.ok && r.net) { setRes(st, "run", "связь прервалась — задание идёт на хосте, жду…"); setTimeout(tick, 3000); return; }
      if (!r.ok) { setRes(st, "err", "Ошибка: " + r.error); return; }
      const j = r.data || {};
      if (j.title && !opts.title) title.textContent = j.title;
      if (opts.onTitle && j.title) opts.onTitle(j);
      started = +j.started || 0;
      finished = j.state === "running" || j.state === "queued" ? 0 : (+j.finished || Date.now() / 1000);
      tickClock();
      if (!tm && !finished) tm = setInterval(tickClock, 1000);
      if (finished && tm) { clearInterval(tm); tm = null; }
      const atEnd = pre.scrollTop + pre.clientHeight >= pre.scrollHeight - 4;
      pre.textContent = (j.log || []).join("\n") || "…";
      if (atEnd) pre.scrollTop = pre.scrollHeight;
      drawStages(j.log || [], j.state);
      if (j.state === "running" || j.state === "queued") { setTimeout(tick, 1500); return; }
      if (j.state === "done") {
        const W = (j.result && Array.isArray(j.result.warnings)) ? j.result.warnings : [];
        setRes(st, W.length ? "" : "ok", W.length ? "готово, с замечаниями: " + W.length : "готово");
        const home = opts.base && opts.base.indexOf("/api/nodes/") === 0 ? "#/n/" + opts.base.slice(11) : "#";
        tail.replaceChildren(
          W.length ? el("div", {class: "banner warn"}, el("b", {text: "Замечания:"}), el("ul", {}, W.map((w) => el("li", {text: w})))) : "",
          j.result && j.result.id != null ? el("p", {}, link("Открыть ВМ " + j.result.id, home + "/vm/" + j.result.id, "btn primary")) : "");
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
  // Сверху — где я (хост › ВМ › вкладка) и общая сводка; слева — дерево хостов и их ВМ.
  const TAB_NAMES = {"": "обзор", net: "сетевые интерфейсы", modlink: "modlink", term: "терминал", settings: "управление",
    mp: "mp.space", ym: "ЮMoney"};
  function renderHeader() {
    $("#h-port").textContent = S.session ? ":" + S.session.port : "";
    $("#h-user").textContent = S.session ? S.session.user : "";
    $("#h-fake").hidden = !(S.session && S.session.fake);
    const cur = S.route || {};
    const X = ctx(cur.node || null);
    const crumbs = [el("a", {href: X.href("/"), text: X.node ? X.node : (((S.ov || {}).host || {}).name || "хост")})];
    if (cur.kind === "vm") {
      const s = X.srv(cur.id);
      crumbs.push(el("span", {class: "sep", text: "/"}), el("a", {href: X.href("/vm/" + cur.id), text: "ВМ " + cur.id + (s && s.name ? " · " + s.name : "")}));
    }
    if (cur.kind === "create") crumbs.push(el("span", {class: "sep", text: "/"}), el("b", {text: "новая ВМ"}));
    if (cur.kind === "job") crumbs.push(el("span", {class: "sep", text: "/"}), el("b", {text: "задание"}));
    if (cur.kind === "join") crumbs.splice(0, 1, el("b", {text: "присоединить хост"}));
    const tab = cur.tab != null ? cur.tab : "";
    if ((cur.kind === "vm" || cur.kind === "host") && TAB_NAMES[tab] != null && !(cur.kind === "vm" && !tab)) {
      crumbs.push(el("span", {class: "sep", text: "/"}), el("b", {text: TAB_NAMES[tab]}));
    }
    $("#crumbs").replaceChildren(...crumbs);
    // сводка: ВМ и модемы на своём хосте
    const ov = S.ov || {}, list = ov.servers || [];
    let ok = 0, total = 0, up = 0;
    for (const sv of list) { const c = counts(sv); ok += c.ok; total += c.total; if (!BAD_VM.includes(String(sv.state || "").toLowerCase())) up++; }
    const hc = counts(ov.host || {});
    ok += hc.ok; total += hc.total;
    const lv = S.ov ? hostLevel() : "";
    $("#h-sum").replaceChildren(
      S.ov ? el("span", {}, el("span", {class: "dot " + lv}), lv === "ok" ? "всё в порядке" : lv === "warn" ? "есть замечания" : "есть проблемы") : el("span", {class: "muted", text: "загрузка…"}),
      list.length ? el("span", {class: "num", text: "ВМ " + up + "/" + list.length}) : null,
      total ? el("span", {class: "num", text: "модемы " + ok + "/" + total}) : null);
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
  const TREE = {q: "", closed: (() => { try { return JSON.parse(localStorage.getItem("pcs-tree-closed") || "{}"); } catch (e) { return {}; } })()};
  function treeToggle(key) {
    TREE.closed[key] = !TREE.closed[key];
    try { localStorage.setItem("pcs-tree-closed", JSON.stringify(TREE.closed)); } catch (e) { /* без хранилища */ }
  }
  function meterOf(c) {
    const v = c.total ? c.ok / c.total : 0;
    return el("span", {class: "meter" + (c.broken ? " bad" : c.ok < c.total ? " warn" : ""), style: "--v:" + v.toFixed(3), "aria-hidden": "true"});
  }
  function renderTree() {
    const nav = $("#tree");
    const cur = S.route || {};
    const q = TREE.q.trim().toLowerCase();
    const kids = [];
    const loading = !S.ov && !S.ovErr;
    function hostBlock(X) {
      const h = X.ov().host || {}, n = X.node ? (X.ov().node || {}) : null;
      const key = X.node || "";
      const here = (cur.node || null) === X.node;
      const off = X.node && n && !n.ok;
      const vms = (X.ov().servers || []).filter((sv) => !q || (String(sv.id) + " " + (sv.name || "") + " " + (sv.ip || "")).toLowerCase().includes(q));
      if (q && !vms.length) return;
      const closed = !q && TREE.closed[key];
      const twist = el("button", {type: "button", class: "t-twist", "aria-label": closed ? "развернуть" : "свернуть", "aria-expanded": String(!closed),
        onclick: (e) => { e.preventDefault(); e.stopPropagation(); treeToggle(key); renderTree(); }}, ic("chev"));
      const head = el("a", {class: "t-row" + (here && cur.kind === "host" ? " active" : "") + (off ? " off" : ""), href: X.href("/"),
        title: off ? (n.error || "нет связи") : ""},
        twist, el("span", {class: "dot " + (loading && !X.node ? "" : off ? "bad" : hostLevel(X))}),
        el("span", {class: "t-name", text: X.node ? X.node : (h.name || (loading ? "хост" : "хост"))}),
        el("span", {class: "t-meta", text: off ? "нет связи" : (h.kind || (X.node ? "?" : "proxmox")) === "proxmox" ? "Proxmox" : "VPS"}));
      const inner = el("div");
      for (const sv of vms) {
        const c = counts(sv);
        inner.append(el("a", {class: "t-row t-vm" + (here && cur.kind === "vm" && String(cur.id) === String(sv.id) ? " active" : "") +
          (sv.state === "stopped" || sv.state === "absent" ? " off" : ""),
          href: X.href("/vm/" + sv.id), title: levelText(sv), style: "--h:" + hueOf(sv, X)},
          el("span", {class: "chip"}), el("span", {class: "dot " + level(sv)}),
          el("span", {class: "t-name"}, el("b", {text: String(sv.id)}), sv.name || ""),
          el("span", {class: "t-meta"}, sv.state === "absent" ? "нет в Proxmox" : sv.state === "stopped" ? "выкл"
            : [c.total ? meterOf(c) : null, el("span", {text: c.total ? c.ok + "/" + c.total : modeShort(sv.mode)})])));
      }
      if (loading && !X.node && !vms.length) for (let i = 0; i < 3; i++) inner.append(el("div", {class: "t-row t-vm"}, el("span"), el("span"), el("span", {class: "skel", style: "width:" + (70 - i * 12) + "%"}), el("span")));
      if (X.isPve() && !off && !q) inner.append(el("a", {class: "t-add" + (here && cur.kind === "create" ? " active" : ""), href: X.href("/create")}, ic("plus"), "Создать ВМ" + (X.node ? " на " + X.node : "")));
      kids.push(el("div", {class: "t-host" + (closed ? " closed" : "")}, head, el("div", {class: "t-kids"}, inner)));
    }
    hostBlock(ctx(null));
    const nodes = S.nodes || [];
    if (nodes.length) kids.push(el("div", {class: "t-sec", text: "Присоединённые хосты"}));
    for (const n of nodes) hostBlock(ctx(n.name));
    if (!q) kids.push(el("a", {class: "t-add" + (cur.kind === "join" ? " active" : ""), href: "#/join", style: "padding-left:8px;margin-top:10px"}, ic("link"), "Присоединить хост"));
    if (q && !kids.some((k) => k.classList.contains("t-host"))) kids.push(el("div", {class: "t-empty", text: "ничего не нашлось"}));
    nav.replaceChildren(...kids);
  }
  function treeSearch() {
    const box = $("#tree-search");
    const inp = el("input", {type: "search", placeholder: "Поиск ВМ…", autocomplete: "off", spellcheck: false, "aria-label": "поиск ВМ по номеру, имени, IP",
      oninput: () => { TREE.q = inp.value; renderTree(); },
      onkeydown: (e) => {
        if (e.key === "Enter") { const a = $("#tree .t-vm"); if (a) { location.hash = a.getAttribute("href"); inp.blur(); } }
        if (e.key === "Escape") { inp.value = ""; TREE.q = ""; renderTree(); inp.blur(); }
      }});
    box.replaceChildren(ic("search"), inp);
    // «/» — к поиску, как в Gmail и GitHub
    document.addEventListener("keydown", (e) => {
      if (e.key === "/" && !/^(INPUT|TEXTAREA|SELECT)$/.test((document.activeElement || {}).tagName || "") && !e.metaKey && !e.ctrlKey) {
        e.preventDefault(); inp.focus();
      }
    });
    const mb = $("#menu-btn");
    mb.replaceChildren(ic("menu"));
    mb.addEventListener("click", () => { const o = !document.body.classList.contains("side-open"); document.body.classList.toggle("side-open", o); mb.setAttribute("aria-expanded", String(o)); });
    document.addEventListener("click", (e) => {
      if (document.body.classList.contains("side-open") && !e.target.closest(".side") && !e.target.closest("#menu-btn")) document.body.classList.remove("side-open");
    });
  }

  // ── страница хоста: вкладки, как у ВМ (сам хост — тоже узел: proxyveth, modlink, mp.space) ──
  const HTABS = [["", "обзор"], ["net", "сетевые интерфейсы"], ["modlink", "modlink"], ["term", "терминал"], ["settings", "управление"],
    ["mp", "mp.space"], ["ym", "ЮMoney"]];
  const isPve = () => ctx(null).isPve();

  // Шапка страницы: имя, строка сведений, состояние и главные действия справа.
  function pageHead(opts) {
    const title = el("h1"), meta = el("div", {class: "meta"}), state = el("span", {class: "tag"});
    const right = el("div", {class: "side-r"}, state, opts.actions || null);
    const chip = opts.chip ? el("span", {class: "chip", "aria-hidden": "true"}) : null;
    const box = el("div", {class: "ph" + (chip ? "" : " plain")}, chip, title, right, meta);
    return {el: box, set(t, metas, lv, lvText, hueVal) {
      title.replaceChildren(...[].concat(t));
      meta.replaceChildren(...metas.filter(Boolean).map((m) => m instanceof Node ? el("span", {}, m) : el("span", {text: m})));
      state.className = "tag " + (lv || "");
      state.replaceChildren(el("span", {class: "dot " + (lv || "")}), lvText || "");
      state.hidden = !lvText;
      if (hueVal != null) box.style.setProperty("--h", String(hueVal));
    }};
  }
  // Вкладки с бегунком под активной (бегунок едет, а не прыгает)
  function tabsBar() {
    const box = el("div", {class: "tabs", role: "tablist"}), ind = el("span", {class: "ind", "aria-hidden": "true"});
    let first = true;
    function set(items, active) {
      box.replaceChildren(...items.map(([href, t, on]) => el("a", {href, class: on ? "active" : "", role: "tab", "aria-selected": String(!!on), text: t})), ind);
      requestAnimationFrame(() => {
        const a = box.querySelector("a.active");
        if (!a) { ind.style.opacity = "0"; return; }
        if (first) { ind.style.transition = "none"; first = false; }
        ind.style.opacity = "1";
        ind.style.transform = "translateX(" + a.offsetLeft + "px) scaleX(" + (a.offsetWidth / 100) + ")";
        if (ind.style.transition === "none") requestAnimationFrame(() => { ind.style.transition = ""; });
      });
    }
    return {el: box, set};
  }
  function plaqueHost(X) {
    const ph = pageHead({});
    function update() {
      const h = (X.ov() && X.ov().host) || {}, c = counts(h);
      const lv = (X.ov() && X.ov().host) ? hostLevel(X) : "";
      ph.set([X.node ? X.node : (h.name || "хост"), el("span", {class: "id", text: X.isPve() ? "Proxmox" : "VPS"})],
        [h.ip ? el("span", {class: "mono", text: h.ip}) : null, h.os || null,
          X.isPve() ? "ВМ " + ((X.ov() && X.ov().servers) || []).length : "модемы прямо на хосте",
          h.mode ? "proxyveth " + modeShort(h.mode) + " · " + c.ok + "/" + c.total : null,
          h.mpspace_ok ? mpText(h.mpspace) : null],
        lv, lv ? (lv === "ok" ? "в порядке" : lv === "warn" ? "есть замечания" : "есть проблемы") : "");
    }
    return {el: ph.el, update};
  }

  function pageHost(X) {
    const pl = plaqueHost(X);
    const tb = tabsBar(), tabsEl = tb.el, body = el("div"), panes = {};
    let cur = null;
    const make = {"": () => hostOverview(X), net: () => netTab(X, "host"), modlink: () => mlTab(X, "host"),
      term: () => termTab(X, "host", true), settings: () => setTab(X, "host"),
      mp: () => browserTab(X, "host", "mp"), ym: () => browserTab(X, "host", "ym")};
    function show(tab) {
      if (!make[tab]) tab = "";
      if (cur === tab) return;
      if (cur !== null && panes[cur].hide) panes[cur].hide();
      if (cur !== null) panes[cur].el.hidden = true;
      if (!panes[tab]) { panes[tab] = make[tab](); body.append(panes[tab].el); }
      panes[tab].el.hidden = false;
      panes[tab].el.classList.remove("pane-in"); void panes[tab].el.offsetWidth; panes[tab].el.classList.add("pane-in");
      cur = tab;
      tb.set(HTABS.map(([k, t]) => [k ? X.href("/host/" + k) : X.href("/"), t, k === tab]));
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

  // вкладка «обзор» хоста: показатели одной полосой, ВМ таблицей, фоновые задания
  function hostOverview(X) {
    const banner = el("div"), kpis = el("div", {class: "kpis"}), list = el("div", {class: "list"});
    const vmHead = el("div", {class: "sec-head"}, el("h2", {text: "Виртуальные машины"}), el("span", {class: "count"}), el("span", {class: "spacer"}),
      link("", X.href("/create"), "btn"));
    vmHead.lastChild.replaceChildren(ic("plus"), "Создать ВМ");
    const jobsBox = el("div", {class: "list"});
    const others = otherVms(X);
    const nodeBar = X.node ? nodeTools(X) : null;
    const root = el("div", {}, nodeBar ? nodeBar.el : null, banner, kpis, vmHead, list, others.el,
      el("div", {class: "sec-head"}, el("h2", {text: "Фоновые задания"})), jobsBox);
    let othersLoaded = 0, listShown = false;
    const JOB_ST = {running: ["info", "идёт"], done: ["ok", "готово"], failed: ["bad", "ошибка"]};
    async function loadJobs() {
      const r = await X.GET("/jobs");
      if (!r.ok) return;
      const lst = r.data || [];
      jobsBox.replaceChildren(...(lst.length ? lst.map((j) => {
        const [cls, txt] = JOB_ST[j.state] || ["", j.state];
        return el("div", {class: "item"}, el("span", {class: "dot " + cls, title: txt}),
          el("div", {class: "grow"}, el("div", {class: "title", text: j.title || j.id}),
            el("div", {class: "sub", text: [txt, j.started ? new Date(j.started * 1000).toLocaleString("ru-RU") : "", j.error || ""].filter(Boolean).join(" · ")})),
          el("div", {class: "btns"}, link("Журнал", X.href("/job/" + encodeURIComponent(j.id)), "btn ghost")));
      }) : [el("div", {class: "item muted", text: "Заданий пока нет. Долгое — обновление, установка, перенос — идёт здесь в фоне и не боится обрыва связи."})]));
    }
    function kpi(k, v, total, cls, meterC) {
      const val = el("div", {class: "v " + (cls || "")});
      const num = el("span");
      val.append(num);
      if (total != null) val.append(el("small", {text: " / " + total}));
      countTo(num, v);
      return el("div", {class: "kpi"}, el("div", {class: "k", text: k}), val, meterC ? meterOf(meterC) : null);
    }
    function skeleton() {
      kpis.replaceChildren(...[1, 2, 3, 4].map(() => el("div", {class: "kpi"}, el("span", {class: "skel", style: "width:60%"}),
        el("span", {class: "skel h", style: "margin-top:12px;width:40%"}))));
      list.replaceChildren(skelRows(3));
    }
    function update() {
      const ov = X.ov() || {}, host = ov.host || {}, list_ = ov.servers || [];
      const loading = !X.node ? (!S.ov && !S.ovErr) : !ov.host && !ov.host_error;
      const errs = [!X.node && S.ovErr, ov.host_error && (X.node ? "нет связи: " : "хост: ") + ov.host_error,
        ov.servers_error && "серверы: " + ov.servers_error].filter(Boolean);
      banner.replaceChildren(...errs.map((t) => el("div", {class: "banner", text: t})));
      if (nodeBar) nodeBar.update();
      if (loading) { skeleton(); return; }
      loadJobs();
      if (Date.now() - othersLoaded > 30000) { othersLoaded = Date.now(); others.load(); }
      const hc = counts(host);
      if (!X.isPve()) {                      // VPS: модемы и софт прямо здесь, ВМ нет
        vmHead.hidden = true; list.hidden = true;
        kpis.replaceChildren(
          kpi("Модемы", hc.ok, hc.total, hc.ok < hc.total ? "warn" : "", hc),
          el("div", {class: "kpi"}, el("div", {class: "k", text: "Режим proxyveth"}), el("div", {class: "v " + (host.mode ? "" : "warn"), text: host.mode ? modeShort(host.mode) : "не задан"})),
          el("div", {class: "kpi"}, el("div", {class: "k", text: "mp.space"}), el("div", {class: "v", style: "font-size:18px", text: mpText(host.mpspace).replace("mp.space ", "")})),
          el("div", {class: "kpi"}, el("div", {class: "k", text: "ОЗУ хоста"}), el("div", {class: "v", style: "font-size:18px", text: gb(host.ram)})));
        return;
      }
      vmHead.hidden = false; list.hidden = false;
      let ok = 0, total = 0, probs = 0, up = 0, broken = 0;
      for (const s of list_) {
        const c = counts(s);
        ok += c.ok; total += c.total; probs += c.warn + c.broken; broken += c.broken;
        if (!BAD_VM.includes(String(s.state || "").toLowerCase())) up += 1; else probs += 1;
      }
      const h = host.hivelink || {};
      const real = Array.isArray(h.modems) ? h.modems.length : (+h.modems || 0);
      const ram = host.ram || {};
      kpis.replaceChildren(
        kpi("ВМ работают", up, list_.length, up < list_.length ? "bad" : ""),
        kpi("Модемы в ВМ", ok, total, ok < total ? "warn" : "", {ok, total, broken}),
        kpi("Требуют внимания", probs, null, probs ? "warn" : ""),
        h.installed === false ? el("div", {class: "kpi"}, el("div", {class: "k", text: "Физические модемы"}), el("div", {class: "v", style: "font-size:18px", text: "hivelink не стоит"}))
          : kpi("Настоящие модемы", real),
        el("div", {class: "kpi"}, el("div", {class: "k", text: "ОЗУ хоста"}), el("div", {class: "v", style: "font-size:18px;margin-top:8px", text: gb(host.ram)}),
          ram.total_gb ? el("span", {class: "meter lg" + ((ram.used_gb / ram.total_gb) > .85 ? " warn" : ""), style: "--v:" + Math.min(1, (ram.used_gb || 0) / ram.total_gb).toFixed(3)}) : null));
      vmHead.children[1].textContent = String(list_.length);
      if (!list_.length) {
        list.replaceChildren(el("div", {class: "item"}, el("span", {class: "muted", text: "ВМ пока нет."}), link("Создать ВМ", X.href("/create"), "btn primary")));
        return;
      }
      const rows = [el("div", {class: "vmrow vmhead"}, el("span"), el("span", {text: "ВМ"}), el("span", {class: "cell", text: "режим"}),
        el("span", {class: "cell", text: "модемы"}), el("span", {class: "cell", text: "mp.space"}), el("span"))];
      for (const s of list_) {
        const c = counts(s), st = String(s.state || "").toLowerCase();
        rows.push(el("div", {class: "vmrow", style: "--h:" + hueOf(s, X)},
          el("span", {class: "dot " + level(s), title: levelText(s)}),
          el("div", {class: "who"}, el("a", {href: X.href("/vm/" + s.id)}, el("span", {class: "id", text: String(s.id)}), s.name || ""),
            el("div", {class: "sub", text: (s.ip || "—") + (st !== "running" ? "  ·  " + (VM_STATE[st] || st) : "")})),
          el("span", {class: "cell", text: modeShort(s.mode)}),
          el("div", {class: "cell mod"}, el("span", {}, el("b", {text: c.ok + " / " + c.total}), c.warn + c.broken ? el("span", {class: "problems", text: "  ·  " + (c.warn + c.broken) + " с бедой"}) : null), meterOf(c)),
          el("span", {class: "cell", text: mpText(s.mpspace).replace("mp.space ", "")}),
          vmButtons(X, s)));
      }
      list.replaceChildren(...rows);
      if (!listShown) { listShown = true; stagger(list); }
    }
    return {el: root, update};
  }

  // Действия с ВМ в строке хоста: «Открыть», «Терминал» и меню ⋯ (питание, убрать из PCS, удалить)
  function vmButtons(X, s) {
    const st = String(s.state || "").toLowerCase();
    const items = [];
    if (st !== "absent") {
      if (st === "stopped") items.push(["start", "Включить", "play"]);
      else items.push(["shutdown", "Выключить штатно", "power", "", "до 2 минут"], ["reboot", "Перезагрузить", "redo"],
        ["stop", "Остановить жёстко", "stop", "danger", "как выдернуть питание"]);
      items.push("-");
    }
    items.push(["forget", "Убрать из PCS", "out", st === "absent" ? "danger" : "", "ВМ и модемы останутся"]);
    if (st !== "absent") items.push(["delete", "Удалить ВМ", "trash", "danger", "вместе с дисками"]);
    const ask = {shutdown: "Выключить ВМ " + s.id + " (" + (s.name || "") + ")? Модемы на ней пропадут.",
      reboot: "Перезагрузить ВМ " + s.id + " (" + (s.name || "") + ")?", stop: "Жёстко остановить ВМ " + s.id + "? Как выдернуть питание."};
    const menu = menuBtn(null, items, async (v) => {
      if (v === "forget") return forgetVm(X, s);
      if (v === "delete") return deleteVm(X, s);
      if (ask[v] && !window.confirm(ask[v])) return;
      const r = await X.POST("/servers/" + s.id + "/power", {action: v});
      if (r && !r.ok) toast("ВМ " + s.id + ": " + r.error, "bad");
      else toast("ВМ " + s.id + ": " + ({start: "включается", shutdown: "выключается", reboot: "перезагружается", stop: "остановлена"})[v]);
      X.node ? refreshNodes() : refresh();
    }, "питание и прочее");
    const b = [];
    if (st !== "absent") {
      b.push(link("Открыть", X.href("/vm/" + s.id), "btn"));
      const t = link("", X.href("/vm/" + s.id + "/term"), "btn ghost icon");
      t.title = "терминал"; t.setAttribute("aria-label", "терминал ВМ " + s.id); t.append(ic("term"));
      b.push(t);
    }
    b.push(menu);
    return el("div", {class: "row-acts"}, b);
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
    const box = el("div", {class: "list"}), head = el("div", {class: "sec-head"}, el("h2", {text: "ВМ Proxmox не под PCS"}));
    const jobs = el("div", {class: "joblog-slot"});
    if (X.node) jobs.dataset.base = X.base;
    async function load() {
      if (!X.isPve()) { head.hidden = box.hidden = true; return; }
      const r = await X.GET("/vms");
      const lst = r.ok ? r.data || [] : [];
      head.hidden = box.hidden = !lst.length;
      box.replaceChildren(...lst.map((v) => el("div", {class: "item"}, el("span", {class: "dot " + (v.state === "running" ? "" : "off")}),
        el("div", {class: "grow"}, el("div", {class: "title"}, el("span", {class: "id", text: String(v.id)}), v.name || ""),
          el("div", {class: "sub", text: v.state === "running" ? "работает · не под PCS" : (v.state || "?") + " · не под PCS"})),
        el("div", {class: "btns"}, btn("Добавить в PCS", () => adoptVm(X, v, jobs), "", "PCS начнёт управлять этой ВМ: ключ хаба, код, proxyveth и modlink")))));
    }
    return {el: el("div", {class: "sub-sec"}, head, box, jobs), load};
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
  const TABS = [["net", "сетевые интерфейсы"], ["modlink", "modlink"], ["term", "терминал"], ["settings", "управление"],
    ["mp", "mp.space"], ["ym", "ЮMoney"]];

  function plaque(X, id) {
    const term = link("", X.href("/vm/" + id + "/term"), "btn");
    term.replaceChildren(ic("term"), "Терминал");
    const ph = pageHead({chip: true, actions: term});
    function update() {
      const s = X.srv(id);
      if (!s) {
        ph.set(["ВМ ", el("span", {class: "id", text: String(id)})], [X.ov() && (X.ov().servers || []).length ? "нет в списке PCS — удалена или ещё создаётся" : "загрузка…"], "", "", hueOf({id}, X));
        return;
      }
      const c = counts(s);
      ph.set([s.name || "ВМ " + s.id, el("span", {class: "id", text: "ВМ " + s.id})],
        [el("span", {class: "mono", text: s.ip || "без IP"}), modeLong(s.mode), "модемы " + c.ok + "/" + c.total, mpText(s.mpspace)],
        level(s), levelText(s), hueOf(s, X));
      document.title = s.id + " " + (s.name || "") + " — PCS";
    }
    return {el: ph.el, update};
  }

  function pageVm(X, id) {
    const pl = plaque(X, id);
    const tb = tabsBar(), tabsEl = tb.el;
    const body = el("div");
    const panes = {};
    let cur = null;
    const make = {net: () => netTab(X, id), modlink: () => mlTab(X, id), term: () => termTab(X, id), settings: () => setTab(X, id),
      mp: () => browserTab(X, id, "mp"), ym: () => browserTab(X, id, "ym")};
    function show(tab) {
      if (!make[tab]) tab = "net";
      if (cur === tab) return;
      if (cur && panes[cur].hide) panes[cur].hide();
      if (cur) panes[cur].el.hidden = true;
      if (!panes[tab]) { panes[tab] = make[tab](); body.append(panes[tab].el); }
      panes[tab].el.hidden = false;
      panes[tab].el.classList.remove("pane-in"); void panes[tab].el.offsetWidth; panes[tab].el.classList.add("pane-in");
      cur = tab;
      tb.set(TABS.map(([k, t]) => [X.href("/vm/" + id + (k === "net" ? "" : "/" + k)), t, k === tab]));
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
      if (!document.hidden) { pollDepth++; try { await fn(); } catch (e) { console.error(e); } finally { pollDepth--; } }
      if (on) t = setTimeout(loop, ms);
    };
    return {
      start() { if (on) return; on = true; t = setTimeout(loop, ms); },
      stop() { on = false; clearTimeout(t); },
    };
  }

  // ── вкладка «Сетевые интерфейсы»: физические модемы (hivelink) и виртуальные (proxyveth) ─
  // Обычно на сервере что-то одно, но бывает и то и другое — тогда всё в одном месте, без дублей.
  // Раздел с модемами — сверху (физические первыми); пустой сжимается в строку внизу.
  function netTab(X, id) {
    const cnt = {phys: null, virt: null, physOk: 0, virtOk: 0};
    const sum = el("div", {class: "net-sum"});
    const hl = hlSection(X, id, {onCount: (n, ok) => { cnt.phys = n; cnt.physOk = ok; arrange(); }});
    const pv = pvTab(X, id, {onCount: (n, ok) => { cnt.virt = n; cnt.virtOk = ok; arrange(); }});
    const virtCount = el("span", {class: "count"});
    const secPhys = el("section", {class: "net-sec"}, el("div", {class: "sec-head"}, el("h2", {text: "Физические модемы"}), hl.count,
      el("span", {class: "spacer"}), hl.tools), hl.el);
    const secVirt = el("section", {class: "net-sec"}, el("div", {class: "sec-head"}, el("h2", {text: "Виртуальные модемы"}), virtCount,
      el("span", {class: "spacer"}), el("span", {class: "muted", text: "proxyveth"})), pv.el);
    const root = el("div", {}, sum, secPhys, secVirt);
    function arrange() {
      virtCount.textContent = cnt.virt == null ? "" : String(cnt.virt);
      const parts = [];
      if (cnt.phys) parts.push(el("span", {}, el("b", {text: String(cnt.phys)}), " физических · работают " + cnt.physOk));
      if (cnt.virt) parts.push(el("span", {}, el("b", {text: String(cnt.virt)}), " виртуальных · работают " + cnt.virtOk));
      sum.replaceChildren(...parts);
      sum.hidden = !(cnt.phys && cnt.virt);          // сводка нужна, только когда есть и те и другие
      // пустой раздел — вниз и тише; физические, если есть, — первыми
      const physFirst = cnt.phys || !cnt.virt;
      if (physFirst && root.children[1] !== secPhys) root.insertBefore(secPhys, secVirt);
      if (!physFirst && root.children[1] !== secVirt) root.insertBefore(secVirt, secPhys);
      secPhys.classList.toggle("empty", cnt.phys === 0);
      secVirt.classList.toggle("empty", cnt.virt === 0);
    }
    sum.hidden = true;
    return {
      el: root,
      show() { hl.show(); pv.show(); },
      hide() { hl.hide(); pv.hide(); },
      dispose() { hl.dispose(); pv.dispose(); },
      update() { pv.update(); },
    };
  }

  // физические USB-модемы (hivelink): связь, сеть, данные; починка — кнопками в строке
  const SIG = (v) => el("span", {class: "sig", title: v == null ? "сигнал неизвестен" : "сигнал " + v + " из 5", "aria-label": "сигнал " + (v ?? "?")},
    [1, 2, 3, 4, 5].map((i) => el("i", {class: v != null && i <= v ? "on" : ""})));
  function hlSection(X, id, opts) {
    const count = el("span", {class: "count"});
    const res = el("span", {class: "res"});
    const info = el("div", {class: "srcline"});
    const box = el("div");
    let data = null, err = null, first = true;
    const bWan = btn("Проверить внешние IP", async (e) => {
      const b = e.currentTarget;
      b.disabled = true;
      setRes(res, "run", "внешний IP через каждый модем…");
      await load(true);
      b.disabled = false;
      setRes(res, err ? "err" : "ok", err ? "Ошибка: " + err : "готово");
    }, "", "hivelink status: внешний IP через каждый физический модем — до минуты");
    const tools = el("span", {class: "row-tools"}, res, bWan);
    async function act1(button, cmd, n, okText) {
      const r = await act(button, res, () => X.run(id, "hivelink", [cmd, String(n)]), okText);
      if (r && r.ok) toast("Модем " + n + ": " + okText);
      load(false);
    }
    function draw() {
      if (err) { box.replaceChildren(el("div", {class: "banner", style: "margin:0", text: "hivelink: " + err})); tools.hidden = true; return; }
      if (!data) { box.replaceChildren(el("div", {class: "tbl-wrap"}, skelRows(2))); return; }
      const rows = data.modems || [];
      count.textContent = String(rows.length);
      tools.hidden = !rows.length;
      if (data.installed === false) {
        box.replaceChildren(el("div", {class: "net-empty"}, "hivelink не установлен — физические USB-модемы здесь не обслуживаются. ",
          btn("Установить hivelink", (e) => act(e.currentTarget, res, () => X.run(id, "hivelink", ["install"]), "Установлено").then(() => load(false)), "", "драйвер физических модемов Huawei"), res));
        return;
      }
      info.replaceChildren(...[
        el("span", {text: data.net === "on" ? "сеть модемов — своя (DHCP и маршруты)" : "сеть модемов — у mp.space, hivelink: USB и связь"}),
        data.fix ? el("span", {text: "фикс приёма " + (data.fix.active ? "активен" : data.fix.needed ? "НЕ активен" : "не нужен")}) : null,
        el("span", {class: "muted", text: "чинит сам: дозвон → откл/вкл → перерегистрация → перезагрузка"})].filter(Boolean));
      if (!rows.length) {
        box.replaceChildren(el("div", {class: "net-empty", text: "Физических модемов нет — USB-модемов Huawei hivelink не видит."}));
        return;
      }
      const head = el("tr", {}, ["N", "интерфейс", "связь", "сеть", "данные", "внешний IP", "заметки"].map((t) => el("th", {text: t})),
        el("th", {class: "acts-cell", text: "действия"}));
      const tb = el("tbody", {}, rows.map((r) => {
        const okc = r.conn === 901 && !(r.errors || []).length;
        const cls = r.conn === 901 ? (okc ? "ok" : "warn") : r.conn == null ? "off" : "bad";
        const note = (r.errors || []).join("; ");
        const can = r.n != null;
        return el("tr", {},
          el("td", {class: "num", text: r.n == null ? "—" : String(r.n)}),
          el("td", {class: "mono", title: (r.usb_id || "") + " · USB " + (r.usb_port || "?") + " · конфигурация " + (r.cfg || "?") + " · " + (r.driver || "без драйвера")},
            el("div", {text: r.iface || "—"}), el("div", {class: "sub", text: "USB " + (r.usb_port || "?") + (r.driver ? " · " + r.driver : "")})),
          el("td", {}, el("span", {class: "pill"}, el("span", {class: "dot " + cls}), r.conn_text || (r.mode === "cdrom" ? "Zero-CD" : "—"))),
          el("td", {}, el("span", {class: "net-cell"}, SIG(r.signal), el("span", {text: r.net || "—"}))),
          el("td", {text: r.dataswitch === 1 ? "вкл" : r.dataswitch === 0 ? "выкл" : "—"}),
          el("td", {class: "mono", text: r.ext_ip || "—"}),
          el("td", {class: "clip " + (note ? "problems" : "note"), title: note}, el("span", {text: note})),
          el("td", {class: "acts-cell"}, el("span", {class: "ia"},
            r.dataswitch === 0 && can ? ibtn("bolt", "включить мобильные данные (hivelink dataon " + r.n + ")", (e) => act1(e.currentTarget, "dataon", r.n, "данные включены"), "", "данные") : null,
            can ? ibtn("redo", "переподключить: откл → вкл (hivelink reconnect " + r.n + ")", (e) => act1(e.currentTarget, "reconnect", r.n, "переподключён")) : null,
            can ? ibtn("link", "веб модема " + r.n + " (192.168." + r.n + ".1) — в отдельном окне", () => openModem(X, id, "192.168." + r.n + ".1")) : null,
            ibtn("sync", "заново подключить USB-устройство (hivelink reset " + (r.n ?? r.usb_port) + ") — питание не снимается",
              (e) => { if (window.confirm("Заново подключить USB-устройство модема " + (r.n ?? r.usb_port) + "? Связь пропадёт на полминуты.")) act1(e.currentTarget, "reset", r.n ?? r.usb_port, "USB подключён заново"); }))));
      }));
      box.replaceChildren(info, el("div", {class: "tbl-wrap"}, el("table", {class: "t"}, el("thead", {}, head), tb)));
      if (first) { first = false; stagger(tb); }
    }
    async function load(ext) {
      const r = await X.run(id, "hivelink", ext ? ["status"] : ["status", "--fast"]);
      err = r.ok ? null : r.error;
      data = r.ok ? r.data || {} : data;
      draw();
      const rows = (data && data.modems) || [];
      if (opts.onCount) opts.onCount(err ? 0 : rows.length, rows.filter((x) => x.conn === 901 && !(x.errors || []).length).length);
    }
    const pl = poller(15000, () => load(false));
    let loaded = false;
    return {
      el: box, count, tools,
      show() { if (!loaded) { loaded = true; draw(); load(false); } pl.start(); },
      hide() { pl.stop(); },
      dispose() { pl.stop(); },
    };
  }

  // ── вкладки mp.space и ЮMoney: браузер на одно окно через прокси ─────────────
  // Firefox на этой машине (хабе), свой профиль на хост/ВМ и вкладку — куки, вход и вкладки
  // сохраняются. Окно — через noVNC. Сверху прокси и внешний адрес через него (понятно, что
  // прокси применился), под ним — кнопки-помогаторы на нужные страницы.
  let RFBmod = null;
  async function loadRFB() {
    if (!RFBmod) RFBmod = (await import("/static/vendor/novnc/core/rfb.js")).default;
    return RFBmod;
  }

  // Веб модема — в отдельном окне: браузер модемов машины, где модем воткнут (хост — сам хаб,
  // ВМ — через SSH хаба). Одно окно на машину, каждый модем — новая вкладка в нём.
  function openModem(X, id, ip) {
    if (X.node) { toast("веб модема присоединённого хоста — в панели того хоста", "warn"); return; }
    const name = "pcs-md-" + id;
    const w = window.open("", name, "popup=yes,width=1320,height=860");   // сразу, в нажатии: иначе окно заблокируют
    const view = location.origin + location.pathname + "#/md/" + id;
    toast("открываю веб модема " + ip + "…", "info");
    X.POST("/browser/md/" + id + "/modem", {ip}).then((r) => {
      if (!r.ok) { if (w && w.location.href === "about:blank") w.close(); toast("Веб модема: " + r.error, "bad"); return; }
      if (!w) { toast("браузер не дал открыть окно — разрешите всплывающие окна для панели", "warn"); return; }
      try { if (!w.location.hash.startsWith("#/md/" + id)) w.location.href = view; } catch (e) { w.location.href = view; }
      w.focus();
    });
  }
  function pageMd(X, id) {
    const where = id === "host" ? "хост " + X.title() : "ВМ " + id;
    document.title = "Модемы · " + where;
    const res = el("span", {class: "res"});
    const screen = el("div", {class: "md-screen"});
    let rfb = null, alive = true, tries = 0;
    const msg = (t, ...b) => screen.replaceChildren(el("div", {class: "br-empty"}, el("div", {class: "br-empty-t", text: t}), b.length ? el("div", {class: "bar"}, b) : null));
    async function connect() {
      if (rfb || !alive) return;
      let RFB;
      try { RFB = await loadRFB(); } catch (e) { msg("noVNC не загрузился: " + e); return; }
      const url = (location.protocol === "https:" ? "wss://" : "ws://") + location.host + "/ws/browser?" + new URLSearchParams({kind: "md", target: id});
      screen.replaceChildren();
      rfb = new RFB(screen, url, {shared: true});
      rfb.resizeSession = false;
      rfb.scaleViewport = true;
      rfb.focusOnClick = true;
      rfb.background = "transparent";
      rfb.addEventListener("connect", () => { tries = 0; rfb.focus(); });
      rfb.addEventListener("disconnect", () => {
        rfb = null;
        if (!alive) return;
        if (++tries <= 5) { setTimeout(connect, 1500); return; }           // браузер ещё поднимается
        msg("Браузер модемов закрыт", btn("Подключиться снова", () => { tries = 0; connect(); }, "primary"));
      });
    }
    const bFull = btn("На весь экран", () => { if (screen.requestFullscreen) screen.requestFullscreen(); }, "ghost");
    const bStop = btn("Закрыть браузер", async (e) => {
      if (!window.confirm("Закрыть браузер модемов (" + where + ")? Вход в веб модемов сохранится.")) return;
      alive = false;
      if (rfb) { try { rfb.disconnect(); } catch (x) { /* уже закрыт */ } }
      await act(e.currentTarget, res, () => X.POST("/browser/md/" + id + "/stop", {}), "Закрыт");
      window.close();
    }, "ghost");
    const bar = el("div", {class: "md-bar"}, el("b", {text: "Веб модемов · " + where}),
      el("span", {class: "muted", text: "каждая кнопка «веб модема» в панели — новая вкладка здесь; вход в модемы сохраняется"}),
      el("span", {class: "spacer"}), res, bFull, bStop);
    msg("Подключаюсь к браузеру модемов…");
    connect();
    return {kind: "md", id, node: X.node, el: el("div", {class: "md-page"}, bar, screen), show() {},
      dispose() { alive = false; if (rfb) { try { rfb.disconnect(); } catch (x) { /* уже закрыт */ } } }};
  }
  function browserTab(X, id, kind) {
    const q = X.node ? "?node=" + encodeURIComponent(X.node) : "";
    const B = "/api/browser/" + kind + "/" + id;
    const GETb = () => GET(B + q), POSTb = (what, body) => POST(B + "/" + what + q, body || {});
    let st = null, rfb = null, lastClip = "", shown = false;

    // прокси
    const ptype = el("select", {"aria-label": "тип прокси"}, el("option", {value: "http", text: "HTTP"}), el("option", {value: "socks5", text: "SOCKS5"}));
    const pin = el("input", {class: "br-proxy-in", placeholder: "host:port:логин:пароль  ·  socks5://логин:пароль@host:port", autocomplete: "off", spellcheck: "false"});
    const pres = el("span", {class: "res"});
    const ipLine = el("div", {class: "br-ip"});
    const bApply = btn("Применить", async (e) => {
      if (!pin.value.trim()) { pin.focus(); setRes(pres, "err", "впиши прокси"); return; }
      const r = await act(e.currentTarget, pres, () => POSTb("proxy", {proxy: pin.value.trim(), type: ptype.value}),
        (d) => d.running ? "применён — браузер уже ходит через него (открытые страницы обнови F5)"
          + (d.tz_changed ? "; часовой пояс сменится при следующем открытии браузера" : "") : "применён");
      if (r && r.ok) { pin.value = ""; await load(); }
    }, "primary", "проверить прокси и сохранить: браузер пойдёт через него");
    const bCheck = btn("Проверить", async (e) => {
      const r = await act(e.currentTarget, pres, () => POSTb("check"), "адрес обновлён");
      if (r && r.ok) await load();
    }, "", "внешний адрес через сохранённый прокси прямо сейчас");
    pin.addEventListener("keydown", (e) => { if (e.key === "Enter") bApply.click(); });
    const proxyCard = el("div", {class: "br-card"},
      el("div", {class: "br-row"}, el("span", {class: "br-lbl", text: "Прокси"}), ptype, pin, bApply, bCheck),
      el("div", {class: "br-row br-row2"}, ipLine, pres));

    // кнопки-помогаторы
    const links = el("div", {class: "br-links"});
    const bLinks = ibtn("edit", "настроить кнопки", () => editLinks(), "ghost icon");

    // экран
    const screenBox = el("div", {class: "br-screen"});
    const sres = el("span", {class: "res"});
    const sState = el("span", {class: "pill"});
    const bPaste = btn("Вставить текст", async () => {
      const t = await askText("Вставить в браузер", "Текст попадёт в буфер обмена браузера — дальше Ctrl+V в нужном поле", "", "");
      if (t == null || !rfb) return;
      rfb.clipboardPasteFrom(t);
      rfb.focus();
      toast("Текст в буфере браузера — Ctrl+V в нужном поле");
    }, "", "буфер обмена: отсюда → в браузер");
    const bCopy = btn("Скопировать из браузера", async () => {
      if (!lastClip) { toast("В браузере ничего не скопировано — сначала Ctrl+C там", "info"); return; }
      toast(await copy(lastClip) ? "Скопировано из браузера" : "Не вышло скопировать", "ok");
    }, "", "буфер обмена: из браузера → сюда (что скопировано там Ctrl+C)");
    const bFull = ibtn("out", "во весь экран", () => { if (screenBox.requestFullscreen) screenBox.requestFullscreen(); }, "ghost icon");
    const bStop = btn("Закрыть браузер", async (e) => {
      const r = await act(e.currentTarget, sres, () => POSTb("stop"), "закрыт — вход и вкладки сохранены");
      if (r && r.ok) { disconnect(); await load(); }
    }, "", "закрыть Firefox; профиль, куки и вкладки остаются");
    const screenBar = el("div", {class: "br-bar"}, sState, el("span", {class: "spacer"}), sres, bPaste, bCopy, bFull, bStop);
    const body = el("div");

    async function startAnd(openUrl) {
      if (!st || !st.running) {
        setRes(sres, "run", "открываю браузер…");
        const r = await POSTb("start");
        if (!r.ok) { setRes(sres, "err", "Ошибка: " + r.error); return false; }
        setRes(sres, "", "");
        await load();
      }
      if (openUrl) {
        const r = await POSTb("open", {url: openUrl});
        if (!r.ok) toast("Не открылось: " + r.error, "bad");
      }
      return true;
    }
    function drawLinks() {
      const L = (st && st.links) || [];
      links.replaceChildren(el("span", {class: "br-lbl", text: "Перейти"}),
        ...L.map(([name, url]) => btn([ic("out"), name], () => startAnd(url), "", url)), bLinks);
    }
    async function editLinks() {
      const cur = ((st && st.links) || []).map(([n, u]) => n + " | " + u).join("\n");
      const ta = el("textarea", {rows: 8, spellcheck: false}, cur);
      ta.value = cur;
      modal("Кнопки-помогаторы · " + (st ? st.title : ""), el("div", {}, el("p", {class: "note", text: "По строке на кнопку: «Название | https://адрес». Пусто — вернутся стандартные."}), ta),
        [{text: "Отмена"}, {text: "Сохранить", cls: "primary", onclick: async (close) => {
          const L = ta.value.split("\n").map((x) => x.split("|").map((y) => y.trim())).filter((x) => x[0] && x[1]).map((x) => [x[0], x.slice(1).join("|")]);
          const r = await POSTb("links", {links: L});
          if (!r.ok) { toast("Ошибка: " + r.error, "bad"); return; }
          close();
          await load();
        }}]);
    }
    function drawIp() {
      const c = st && st.check;
      if (!st || !st.proxy) { ipLine.replaceChildren(el("span", {class: "muted", text: "прокси не задан — нужен " + (st ? st.want : "")})); return; }
      const parts = [el("span", {class: "mono", text: st.proxy})];
      if (c) {
        parts.push(el("span", {}, "внешний адрес ", el("b", {class: "mono", text: c.ip})));
        if (c.country) parts.push(el("span", {text: [c.cc, c.country, c.city].filter(Boolean).join(" · ")}));
        if (c.tz) parts.push(el("span", {class: "muted", title: "часовой пояс браузера — страны адреса", text: "время " + c.tz}));
        if (c.region_ok === true) parts.push(el("span", {class: "tag ok"}, el("span", {class: "dot ok"}), st.want + " ✓"));
        else if (c.region_ok === false) parts.push(el("span", {class: "tag bad"}, el("span", {class: "dot bad"}), "нужен " + st.want));
        parts.push(el("span", {class: "muted", text: "проверено " + ago(c.t)}));
      }
      ipLine.replaceChildren(...parts);
    }
    function drawScreen() {
      if (!st) { body.replaceChildren(el("div", {class: "br-screen br-skel"}, el("span", {class: "skel", style: "width:40%"}))); return; }
      if (!st.installed) {
        const jobs = el("div");
        body.replaceChildren(el("div", {class: "br-empty"},
          el("div", {class: "br-empty-t", text: "Браузер ещё не установлен на этом хосте"}),
          el("p", {class: "muted", text: "Яндекс Браузер для Linux (из репозитория Яндекса), экран и шрифты — около 500 МБ, несколько минут. Ставится один раз на хост с панелью и служит всем вкладкам mp.space и ЮMoney."}),
          el("div", {class: "bar"}, btn("Установить браузер", async (e) => {
            const r = await act(e.currentTarget, sres, () => POST("/api/browser/install", {}), "ставлю…");
            if (r && r.ok && r.data && r.data.job) jobs.replaceChildren(jobView(r.data.job, {title: "Установка браузера", onDone: () => load()}));
          }, "primary"), sres), jobs));
        return;
      }
      if (/став/.test(sres.textContent)) setRes(sres, "", "");
      if (!st.running) {
        disconnect();
        body.replaceChildren(el("div", {class: "br-empty"},
          el("div", {class: "br-empty-t", text: st.used ? "Браузер закрыт — сеанс сохранён" : "Браузер ещё не открывали"}),
          el("p", {class: "muted", text: st.used ? "Куки, вход и вкладки лежат в профиле на хосте — откроется как было."
            : "Свой профиль для этой вкладки: вход и куки будут сохраняться между запусками."}),
          el("div", {class: "bar"}, btn("Открыть браузер", () => startAnd(null), "primary", st.proxy ? "" : "сначала прокси"), sres),
          st.proxy ? null : el("p", {class: "problems", text: "Сначала прокси — без него браузер вышел бы в сеть с адреса сервера."})));
        return;
      }
      sState.replaceChildren(el("span", {class: "dot ok"}), "работает · без зрителя закроется сам через " + st.idle_min + " мин");
      if (body.firstChild !== screenBar) body.replaceChildren(screenBar, screenBox);
      if (st.fake) { screenBox.replaceChildren(el("div", {class: "br-fake", text: "Фейковая панель: окна браузера нет. На настоящем хосте здесь Яндекс Браузер."})); return; }
      if (shown && !rfb) connect();
    }
    async function connect() {
      if (rfb) return;
      let RFB;
      try { RFB = await loadRFB(); } catch (e) { screenBox.replaceChildren(el("div", {class: "banner", text: "noVNC не загрузился: " + e})); return; }
      const qs = new URLSearchParams({kind, target: id});
      if (X.node) qs.set("node", X.node);
      const url = (location.protocol === "https:" ? "wss://" : "ws://") + location.host + "/ws/browser?" + qs;
      screenBox.replaceChildren();
      rfb = new RFB(screenBox, url, {shared: true});
      rfb.resizeSession = false;          // экран — обычный 1920×1080, во вкладке — масштаб
      rfb.scaleViewport = true;
      rfb.focusOnClick = true;
      rfb.background = "transparent";
      rfb.addEventListener("connect", () => { setRes(sres, "", ""); rfb.focus(); });
      rfb.addEventListener("clipboard", (e) => { lastClip = (e.detail && e.detail.text) || ""; });
      rfb.addEventListener("disconnect", (e) => {
        rfb = null;
        if (!shown) return;
        screenBox.replaceChildren(el("div", {class: "br-empty"}, el("div", {class: "br-empty-t", text: "Окно отключилось"}),
          el("div", {class: "bar"}, btn("Подключиться снова", () => load().then(() => connect()), "primary"))));
        if (!(e.detail && e.detail.clean)) load();
      });
    }
    function disconnect() {
      if (rfb) { const r = rfb; rfb = null; try { r.disconnect(); } catch (e) { /* уже закрыт */ } }
    }
    async function load() {
      const r = await GETb();
      if (!r.ok) { body.replaceChildren(el("div", {class: "banner", text: "Ошибка: " + r.error})); return; }
      st = r.data || {};
      if (!pin.value && st.proxy_type) ptype.value = st.proxy_type;
      drawIp(); drawLinks(); drawScreen();
    }
    const pl = poller(10000, load);
    const K = kind === "mp" ? "mp.space: нужен европейский адрес" : "ЮMoney: нужен российский адрес; IP лучше не менять посреди работы";
    const root = el("div", {class: "br"}, el("p", {class: "muted br-hint", text: K + ". Окно — обычный Яндекс Браузер на хосте панели: "
      + "о прокси он не знает, весь его интернет идёт через прокси, как у компьютера за роутером. Вход и куки сохраняются."}),
      proxyCard, links, body);
    return {
      el: root,
      show() { shown = true; if (!st) { drawScreen(); load(); } else { load(); } pl.start(); },
      hide() { shown = false; pl.stop(); disconnect(); },
      dispose() { shown = false; pl.stop(); disconnect(); },
    };
  }

  // ── вкладка proxyveth ─────────────────────────────────────────────────────
  function pvTab(X, id, opts) {
    opts = opts || {};
    const info = el("div", {class: "srcline"});
    const res = el("span", {class: "res"});
    const extra = el("div");
    const chips = el("div", {class: "chips"});
    const tbl = el("div", {class: "tbl-wrap"});
    const editor = el("div");
    let rows = [], source = null, statusErr = null, flt = "all", firstRows = true;
    const prevState = {};

    const bSync = btn("Синхронизировать", () => sync(false), "primary", "привести модемы к таблице (proxyveth sync)");
    bSync.prepend(ic("sync"));
    const tests = menuBtn("Тест", [
      ["quick", "Быстрый тест", "bolt", "", "своя сторона: юниты, адреса, USB — секунды"],
      ["wan", "Долгий тест", "gauge", "", "внешний IP через каждый модем — минута-две"]], async (v, b) => {
      b.disabled = true;
      setRes(res, "run", v === "wan" ? "проверяю внешний IP через каждый модем — минута-две…" : "проверяю свою сторону…");
      await load(v === "wan");
      b.disabled = false;
      setRes(res, "ok", v === "wan" ? "долгий тест готов — внешний IP обновлён" : "быстрый тест готов");
    }, "проверить модемы");
    const allMenu = menuBtn("Все модемы", [
      ["up", "Поднять все", "play", "", "из таблицы; «остановлен» снимается"],
      ["down", "Остановить все", "stop", "", "таймер не поднимет, пока не «поднять»"],
      ["restart", "Пересоздать все", "redo", "", "по одному, около минуты на модем"]], (v, b) => updown(b, v), "действия со всеми модемами сервера");
    const tableMenu = menuBtn("Таблица", [
      ["edit", "Править локально", "edit", "", "локальная копия прямо здесь"],
      ["src", "Сменить ссылку", "link", "", "Google-таблица или local"]], async (v) => {
      if (v === "edit") return openEditor();
      const url = await askText("Источник таблицы", "Ссылка на Google-таблицу (из адресной строки) или local:",
        source && source.url || "", "https://docs.google.com/spreadsheets/d/…");
      if (!url) return;
      await act(null, res, () => X.run(id, "proxyveth", ["source", url]), "Сохранено");
      load(false);
    }, "источник модемов");
    const bEdit = {set disabled(v) { tableMenu.querySelector("button").disabled = v; }};
    const bar = el("div", {class: "toolbar"}, bSync, tests, allMenu, tableMenu, res);

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

    // одна строка: режим · откуда таблица · когда синхронизирована
    function renderInfo() {
      const s = X.ent(id) || {};
      const parts = [el("span", {title: s.mode === "usb" ? "модем как USB Huawei E3372h, до 40 на сервер" : s.mode === "gw" ? "шлюз — без USB и без такого предела" : "",
        text: s.mode === "usb" ? "режим USB" : s.mode === "gw" ? "режим шлюз" : "режим не задан"})];
      const d = source && source.ok ? source.data : null;
      if (source && !source.ok) parts.push(el("span", {class: "problems", text: "таблица: " + source.error}));
      else if (d && typeof d === "object") {
        const kind = d.source || d.kind || (d.url ? "google" : null);
        const url = d.url || (typeof d.source === "string" && /^https?:/.test(d.source) ? d.source : null);
        const g = url ? el("a", {href: url, target: "_blank", rel: "noopener noreferrer", title: url}, "Google-таблица", ic("out")) : el("span", {text: "Google — не задана"});
        if (kind === "local") parts.push(el("span", {title: d.local || "/etc/proxyveth/table.csv", text: "главная — локальная копия"}), g);
        else parts.push(el("span", {}, "главная — ", g), el("span", {class: "mono", title: "локальная копия", text: d.local || "/etc/proxyveth/table.csv"}));
        parts.push(el("span", {text: "синхронизирована " + ago(d.synced || d.pulled || d.mtime)}));
        if (d.stale) parts.push(el("span", {class: "problems", text: "Google недоступен — работает локальная копия"}));
      } else if (!source) parts.push(el("span", {class: "skel", style: "width:260px;display:inline-block"}));
      info.replaceChildren(...parts);
    }

    // счётчики — они же фильтры таблицы
    const kindOf = (r) => r.state === "warn" && r.side === "server" ? "server" : r.state;
    const FLT = [["all", "Все", ""], ["ok", "Работают", "ok"], ["server", "Ждут сервер", "warn"], ["warn", "Апстрим", "warn"],
      ["broken", "Сломаны", "bad"], ["stopped", "Остановлены", "off"], ["rebooting", "Перезагрузка", "info"], ["absent", "Нет", "off"], ["disabled", "Выключены", "off"]];
    function renderChips() {
      const c = {all: rows.length};
      for (const r of rows) { const k = kindOf(r); c[k] = (c[k] || 0) + 1; }
      if (flt !== "all" && !c[flt]) flt = "all";
      chips.replaceChildren(...FLT.filter(([k]) => k === "all" || c[k]).map(([k, t, dot]) =>
        el("button", {type: "button", class: flt === k ? "on" : "", "aria-pressed": String(flt === k),
          onclick: () => { flt = k; renderRows(); }}, dot ? el("span", {class: "dot " + dot}) : null, t, el("span", {class: "n", text: String(c[k] || 0)}))));
      chips.hidden = !rows.length;
    }
    function renderRows() {
      const unset = !!(statusErr && /режим[^.]*не задан/i.test(statusErr));
      info.hidden = bar.hidden = unset;          // не настроен — одна строка, без кнопок
      if (unset) {
        tbl.replaceChildren(el("div", {class: "net-empty", text: "Виртуальных модемов нет — proxyveth здесь не настроен. Включить: «управление» → «Режим proxyveth»."}));
        chips.hidden = true; return;
      }
      if (statusErr) { tbl.replaceChildren(el("div", {class: "banner", style: "margin:0", text: "Ошибка: " + statusErr})); chips.hidden = true; return; }
      renderChips();
      if (!rows.length) { tbl.replaceChildren(el("div", {class: "item muted", style: "padding:16px", text: "Модемов нет — таблица пуста или не задана. «Таблица» → «Сменить ссылку» или «Править локально»."})); return; }
      const shown = flt === "all" ? rows : rows.filter((r) => kindOf(r) === flt);
      const head = el("tr", {}, ["n", "real", "прокси", "состояние", "внешний IP", "заметки"].map((t) => el("th", {text: t})), el("th", {class: "acts-cell", text: "действия"}));
      const body = shown.map((r) => {
        const [cls, txt] = r.state === "warn" && r.side === "server" ? ["warn", "сервер"]
          : ROW_STATE[r.state] || ["", r.state || "—"];
        const note = (r.problems || []).length ? (r.problems || []).join("; ") : r.note || "";
        const changed = !firstRows && prevState[r.n] && prevState[r.n] !== r.state;
        prevState[r.n] = r.state;
        const stopped = r.state === "stopped";
        return el("tr", {class: changed ? "flash" : ""},
          el("td", {class: "num", text: String(r.n)}), el("td", {class: "num", text: r.real == null ? "—" : String(r.real)}),
          el("td", {class: "mono", text: maskProxy(r.proxy)}),
          el("td", {}, el("span", {class: "pill", title: (r.side === "server" ? "беда на стороне сервера, не у прокси: сервер (mp.space) "
              + "ещё не взял модем — адрес, правило маршрутизации, USB. " : "") + (r.since ? "с " + new Date(r.since * 1000).toLocaleString("ru-RU") : "")},
            el("span", {class: "dot " + cls}), txt)),
          el("td", {class: "mono", text: r.ext_ip || "—"}),
          el("td", {class: "clip " + ((r.problems || []).length ? "problems" : "note"), title: note}, el("span", {text: note})),
          el("td", {class: "acts-cell"}, el("span", {class: "ia"},
            stopped
              ? ibtn("play", "поднять модем " + r.n + " (proxyveth up " + r.n + ")", (e) => updown(e.currentTarget, "up", r.n), "", "поднять")
              : ibtn("stop", "остановить модем " + r.n + ": таймер не поднимет, пока не «поднять» (proxyveth down " + r.n + ")", (e) => updown(e.currentTarget, "down", r.n), "", "остановить"),
            ibtn("redo", "пересоздать модем " + r.n + ": интерфейс снимется и поднимется заново (proxyveth restart " + r.n + ")", (e) => updown(e.currentTarget, "restart", r.n)),
            ibtn("pulse", "диагностика модема " + r.n + ": прокси → логин → модем → SIM → интернет", () => diag(r.n)),
            ibtn("link", "веб модема " + r.n + ": настоящий модем за прокси (через 192.168." + r.n + ".1 этого сервера) — в отдельном окне",
              () => openModem(X, id, "192.168." + r.n + ".1")))));
      });
      const tb = el("tbody", {}, body);
      tbl.replaceChildren(el("table", {class: "t"}, el("thead", {}, head), tb));
      if (firstRows) { firstRows = false; stagger(tb); }
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
      else if (r && r.ok) toast(what[0].toUpperCase() + what.slice(1) + ": " + done);
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
      if (!r.ok) { statusErr = r.error; renderRows(); if (opts.onCount) opts.onCount(0, 0); return; }
      const st = r.data.status;
      if (r.data.source) source = r.data.source;
      statusErr = st && !st.ok ? st.error : null;
      rows = st && st.ok ? rowsOf(st.data) : statusErr && /режим[^.]*не задан/i.test(statusErr) ? [] : rows;
      renderInfo(); renderRows();
      if (opts.onCount) opts.onCount(rows.length, rows.filter((r) => r.state === "ok").length);
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
    const root = el("div", {}, info, bar, extra, editor, chips, tbl);
    let loaded = false;
    return {
      el: root,
      show() { if (!loaded) { loaded = true; renderInfo(); tbl.replaceChildren(skelRows(8)); load(false); } else pollStatus(); pl.start(); },
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

  function mlTab(X, id) {
    const where = id === "host" ? "хост" : "ВМ " + id;
    const legacyBox = el("div");
    const res = el("span", {class: "res"});
    const out = el("div", {class: "muted mono"});
    const dirtyEl = el("span", {class: "problems"});
    const pill = el("span", {class: "pill"}, el("span", {class: "dot"}), el("span", {text: "3proxy: …"}));
    const engineEl = el("span", {class: "muted"});
    // Выключатель: modlink запускается только по явному «включить»; выключен — ни 3proxy, ни демона,
    // ни одного порта modlink в ss -tulpn. Таблицу можно править и применять и выключенной.
    let isOn = null;
    const swT = el("b", {text: "modlink"});
    const swSub = el("span", {class: "muted", text: "…"});
    const swRes = el("span", {class: "res"});
    const sw = el("button", {type: "button", class: "switch", role: "switch", "aria-checked": "false", "aria-label": "modlink включён",
      title: "включить или выключить modlink: 3proxy и демон", onclick: () => power()}, el("span", {class: "knob", "aria-hidden": "true"}));
    const powerBar = el("div", {class: "ml-power"}, sw, el("div", {class: "ml-power-t"}, swT, swSub), swRes);
    function showPower(d) {
      if (d.on === undefined) {                 // старый modlink без выключателя — как раньше
        powerBar.hidden = true; pill.hidden = false;
        return;
      }
      powerBar.hidden = false; pill.hidden = true;
      isOn = !!d.on;
      sw.setAttribute("aria-checked", String(isOn));
      powerBar.classList.toggle("on", isOn);
      const st = (x) => x === "active" ? "работает" : x || "—";
      swT.textContent = isOn ? "modlink включён" : "modlink выключен";
      swSub.textContent = isOn ? "3proxy: " + st(d.proxy) + " · демон: " + st(d.daemon)
        : "3proxy и демон не запущены, портов modlink нет — таблицу можно править";
    }
    async function power() {
      const want = !isOn;
      if (!want && !window.confirm("Выключить modlink (" + where + ")? 3proxy и демон остановятся — все прокси и ссылки modlink " +
          "перестанут отвечать. Таблица и настройки останутся.")) return;
      sw.disabled = true;
      setRes(swRes, "run", want ? "включаю…" : "выключаю…");
      const r = await X.run(id, "modlink", [want ? "on" : "off"]);
      sw.disabled = false;
      if (!r.ok) { setRes(swRes, "err", "Ошибка: " + r.error); return; }
      setRes(swRes, "", "");
      toast("modlink " + (want ? "включён" : "выключен") + " · " + where, want ? "ok" : "info");
      await pollStatus();
    }
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
      // IP модема выводится из IP интерфейса (как в modlink для Windows): 192.168.N.100 → 192.168.N.1,
      // если его не правили отдельно
      if (k === "lan_ip" && val(r, "modem_ip") === gateway(val(r, "lan_ip")) && gateway(v)) {
        const g = gateway(v);
        if (g === String(r.modem_ip || "")) delete e.modem_ip; else e.modem_ip = g;
      }
      if (String(v) === String(o == null ? "" : o)) delete e[k]; else e[k] = v;
      if (!Object.keys(e).length) delete edits[r.id];
      markDirty();
    }
    const gateway = (ip) => { const m = /^(\d+\.\d+\.\d+)\.\d+$/.exec(String(ip || "").trim()); return m ? m[1] + ".1" : ""; };
    // ── автозаполнение новых строк (логика modlink для Windows) ──
    const PW_ABC = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789";
    function genPw(n) {
      const a = new Uint32Array(n || 12);
      crypto.getRandomValues(a);
      return Array.from(a, (x) => PW_ABC[x % PW_ABC.length]).join("");
    }
    function usedN() { return new Set([...orig.map((r) => +r.id), ...added.map((a) => +a.fields.n)].filter((x) => x >= 1 && x <= 254)); }
    function nextN(used) {
      used = used || usedN();
      let hi = 0;
      for (const u of used) if (u > hi) hi = u;
      let n = hi + 1;
      if (n > 254) { n = 1; while (used.has(n) && n <= 254) n++; }
      return n <= 254 ? n : null;
    }
    function nextPorts() {
      const base = (S0 && +S0.base_port) || 10000;
      if (S0 && S0.mode === "shared") return [base, base + 1, base + 1];
      const all = [...orig.map((r) => +val(r, "port")), ...added.map((a) => +a.fields.port)].filter((x) => x > 0);
      const p = all.length ? Math.max(...all) + 3 : base;
      return [p, p + 1, p + 2];
    }
    const DERIVED = {lan_ip: (n) => "192.168." + n + ".100", modem_ip: (n) => "192.168." + n + ".1", login: (n) => "modem" + n, name: (n) => "modem " + n};
    function newRow(n) {
      const [p, b, rc] = nextPorts();
      const f = {enabled: true, n: n || "", password: genPw(12), port: String(p), reboot_port: String(b), reconnect_port: String(rc),
        interval_min: "0", protect: true};
      for (const [k, fn] of Object.entries(DERIVED)) f[k] = n ? fn(n) : "";
      return {tmp: ++seq, touched: {}, fields: f};
    }
    function setN(a, v) {
      const n = parseInt(v, 10);
      a.fields.n = v;
      if (!(n >= 1 && n <= 254)) return;
      for (const [k, fn] of Object.entries(DERIVED)) if (!a.touched[k]) a.fields[k] = fn(n);
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
      return el("input", {value: v == null ? "" : String(v), class: cls, spellcheck: false, autocomplete: "off",
        placeholder: isNew && /port/.test(k) ? "auto" : isNew && k === "password" ? "gen" : "",
        oninput: (e) => {
          if (!isNew) return setEdit(r, k, e.target.value);
          r.fields[k] = e.target.value;
          r.touched[k] = true;
          if (k === "lan_ip" && !r.touched.modem_ip && gateway(e.target.value)) {
            r.fields.modem_ip = gateway(e.target.value);
            const mi = tbl.querySelector('tr[data-tmp="' + r.tmp + '-more"] input[data-k="modem_ip"]');
            if (mi) mi.value = r.fields.modem_ip;
          }
        }, "data-k": k});
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
        el("th", {text: "Внешний IP"}), el("th", {class: "acts-cell", text: "действия"}));
      const body = [];
      for (const r of orig) {
        const st = states[r.id];
        const gone = deleted.has(r.id), open = opened.has(r.id), off = val(r, "enabled") === false;
        const speedLine = el("div", {class: "speedline", text: st && st.speed ? (st.speed.error ? st.speed.error : fmtSpeed(st.speed)) : ""});
        body.push(el("tr", {"data-id": String(r.id), class: (gone ? "gone" : "") + (edits[r.id] ? " dirty" : "") + (off ? " off" : "")},
          el("td", {class: "ml-n", title: mlStateText(st)}, el("span", {class: "dot " + mlDot(st)}), String(r.id)),
          ML_MAIN.map(([k]) => el("td", {}, k === "password" ? pwCell(r) : inputFor(r, k))),
          el("td", {class: "ml-ext"}, extCell(r)),
          el("td", {class: "acts-cell"}, el("span", {class: "ia"},
            ibtn("bolt", "Test: внешний IP через этот прокси + HiLink", () => test(r), "", "test"),
            ibtn("redo", "реконнект: сменить IP", () => rowAct(r, "реконнект", ["reconnect", String(r.id)])),
            val(r, "modem_ip") ? ibtn("link", "веб модема (" + val(r, "modem_ip") + ") — в отдельном окне", () => openModem(X, id, val(r, "modem_ip"))) : null,
            ibtn("copy", "копировать строку: прокси и ссылки", () => copyRow(r)),
            ibtn("chev", open ? "свернуть" : "ещё: порты, автореконнект, защита, ребут, скорость", () => { open ? opened.delete(r.id) : opened.add(r.id); draw(); }, open ? "on" : ""),
            ibtn("trash", gone ? "не удалять" : "удалить строку (при «Применить»)", () => { if (gone) deleted.delete(r.id); else deleted.add(r.id); draw(); markDirty(); }, "danger")))));
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
        const nIn = el("input", {value: String(a.fields.n || ""), class: "w-xs", inputmode: "numeric", "aria-label": "номер модема N",
          title: "№ модема: от него IP 192.168.N.100, IP модема .1, логин modemN", oninput: (e) => { setN(a, e.target.value); draw(); nIn2(); }});
        const nIn2 = () => { const x = tbl.querySelector('tr[data-tmp="' + a.tmp + '"] td.ml-n input'); if (x) { x.focus(); const L = x.value.length; x.setSelectionRange(L, L); } };
        body.push(el("tr", {class: "new", "data-tmp": String(a.tmp)}, el("td", {class: "ml-n"}, nIn),
          ML_MAIN.map(([k]) => el("td", {}, inputFor(a, k, true))), el("td", {class: "muted", text: "новая"}),
          el("td", {class: "acts-cell"}, el("span", {class: "ia"}, ibtn("trash", "убрать новую строку", () => { added = added.filter((x) => x !== a); draw(); markDirty(); }, "danger")))));
        body.push(el("tr", {class: "ml-more", "data-tmp": a.tmp + "-more"}, el("td"), el("td", {colspan: ML_MAIN.length + 2},
          el("div", {class: "ml-more-grid"}, ML_MORE.map(([k, t, title]) => el("label", {title: title || null}, el("span", {class: "muted", text: t}), inputFor(a, k, true)))),
          el("div", {class: "bar"}, inputFor(a, "protect", true)))));
      }
      if (!body.length) body.push(el("tr", {}, el("td", {colspan: ML_MAIN.length + 3, class: "muted", text: "модемов нет — «+ Модем» или «Из интерфейсов»"})));
      const tb = el("tbody", {}, body);
      tbl.replaceChildren(el("table", {class: "t ml"}, el("thead", {}, head), tb));
      if (orig.length && !tbl.dataset.shown) { tbl.dataset.shown = "1"; stagger(tb); }
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
          showPower(St.data);
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
    const bAdd = btn("Модем", () => {
      added.push(newRow(nextN()));
      draw(); markDirty();
      const x = tbl.querySelector("tr.new:last-of-type td.ml-n input, tr.new td.ml-n input");
      const all = tbl.querySelectorAll("tr.new td.ml-n input");
      if (all.length) all[all.length - 1].focus();
      void x;
    }, "", "новая строка: №, IP, логин, пароль и порты — сами (как в modlink для Windows); № можно поменять — IP и логин пойдут за ним");
    bAdd.prepend(ic("plus"));
    const bRange = btn("Диапазон", () => {
      const from = el("input", {inputmode: "numeric", value: String(nextN() || 1), style: "width:90px"});
      const to = el("input", {inputmode: "numeric", value: String(Math.min(254, (nextN() || 1) + 9)), style: "width:90px"});
      modal("Добавить модемы диапазоном · " + where, el("div", {class: "form", style: "border:0;padding:0"},
        el("p", {class: "muted", text: "Строка на каждый № из диапазона: IP 192.168.N.100, IP модема .1, логин modemN, случайный пароль, порты подряд " +
          (S0 && S0.mode === "shared" ? "(режим «один порт» — общие)" : "тройками") + ". Занятые № пропускаются. Вступят в силу после «Применить»."}),
        el("div", {style: "display:flex;gap:12px;align-items:center"}, el("label", {}, "с №", from), el("label", {}, "по №", to))), [
        {text: "Отмена"},
        {text: "Добавить", cls: "primary", onclick: (close) => {
          const a = parseInt(from.value, 10), b = parseInt(to.value, 10);
          if (!(a >= 1 && b <= 254 && a <= b)) { toast("№ — от 1 до 254, «с» не больше «по»", "bad"); return; }
          const used = usedN();
          let n = 0;
          for (let k = a; k <= b; k++) if (!used.has(k)) { added.push(newRow(k)); used.add(k); n++; }
          close(); draw(); markDirty();
          toast(n ? "добавлено строк: " + n + " — «Применить»" : "все № из диапазона уже есть", n ? "ok" : "warn");
        }}]);
    }, "", "сразу несколько модемов: № с … по …");
    bRange.prepend(ic("plus"));
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
      const r = await act(e.target, res, () => X.POST("/targets/" + id + "/modlink/save", payload),
        isOn === false ? "Применено — заработает после включения modlink" : "Применено — 3proxy перечитал конфиг");
      if (r && !r.ok && r.data && r.data.steps) {
        out.textContent = r.data.steps.map((s) => (s.ok ? "✓ " : "✗ ") + s.what + (s.error ? ": " + s.error : "")).join("   ");
      }
      tests = {};
      await load();
    }, "primary", "сохранить правки → конфиг 3proxy → перечитать (соединения не рвутся) → проверка портов");
    const copyMenu = menuBtn("Копировать", [["proxy", "Только прокси", "copy"], ["reboot", "Только ссылки ребута", "power"],
      ["reconnect", "Только ссылки реконнекта", "redo"], "-", ["all", "Прокси + ребут + реконнект", "list", "", "по колонкам — для таблицы"]], copyAll, "строки для клиента — с паролями");
    const bTbl = btn("Для proxyveth", async (e) => {
      const r = await act(e.target, res, () => X.POST("/targets/" + id + "/modlink/export", {table: true}), "Готово");
      if (r && r.ok) showTable("Строки для таблицы proxyveth · " + where, r.data.columns, r.data.rows,
        el("div", {}, ...r.data.warnings.map((w) => el("div", {class: "warn-line", text: "⚠ " + w})),
          el("div", {class: "muted", text: "Колонки n, real, proxy — как в таблице proxyveth ВМ этого хоста. n (номер виртуального модема)" +
            " можно поменять. У строк-поставщиков защита снята: proxyveth ходит через прокси к 192.168.N.1 за DNS и HiLink."})));
    }, "", "виртуальные модемы из этих настоящих: строки n,real,proxy,enabled для proxyveth");
    // Конфиг по таблице: строки, которые уже у клиентов, — как есть; modlink подстраивается под них.
    const bImp = btn("Конфиг по таблице", () => importDlg(), "", "строки клиентов «Ip:port:log:pass · ReBoot · ReConnect» → таблица modlink, где каждая работает ровно так");
    bImp.prepend(ic("wand"));
    const IMP_ACT = {add: ["добавить", "ok"], change: ["поправить", "warn"], same: ["как есть", ""]};
    const IMP_SET = {wan_ip: "внешний адрес", mode: "режим портов", base_port: "базовый порт"};
    const IMP_F = {login: "логин", password: "пароль", port: "порт", reboot_port: "ребут", reconnect_port: "реконнект", lan_ip: "модем", enabled: "включена"};
    function importDlg() {
      const ta = el("textarea", {rows: 7, spellcheck: "false", style: "width:100%;font:12.5px/1.5 var(--mono)",
        placeholder: "Вставьте из таблицы колонки Ip:port:log:pass, ReBoot, ReConnect — как есть (ReBoot может не быть; соседние колонки не мешают)"});
      const url = el("input", {placeholder: "или ссылка на лист Google-таблицы (доступ «всем, у кого есть ссылка»)", style: "width:100%"});
      const how = el("select", {}, el("option", {value: "number", text: "модем строки — по логину modemN (модем 192.168.N.1)"}),
        el("option", {value: "order", text: "модем строки — по порядку: первая строка — первому свободному модему"}));
      const repl = el("input", {type: "checkbox"});
      const turnOn = el("input", {type: "checkbox", checked: isOn === false});
      const planBox = el("div", {style: "margin-top:12px"});
      let applyBtn = null, checked = null;
      const body = () => url.value.trim() ? {url: url.value.trim(), map: how.value, replace: repl.checked, on: turnOn.checked}
        : {text: ta.value, map: how.value, replace: repl.checked, on: turnOn.checked};
      const sig = () => JSON.stringify(body());
      const stale = () => { checked = null; if (applyBtn) applyBtn.disabled = true; };
      for (const x of [ta, url, how, repl, turnOn]) { x.addEventListener("input", stale); x.addEventListener("change", stale); }
      const td = (text, cls) => el("td", {class: cls || "", text: String(text)});
      function showPlan(d) {
        const n = (k) => (d.rows || []).filter((x) => x.action === k).length;
        const rows = (d.rows || []).map((x) => el("tr", {}, el("td", {}, el("span", {class: "tag " + IMP_ACT[x.action][1], text: IMP_ACT[x.action][0]})),
          td(x.id, "num"), td(x.login, "mono"), td(x.port, "mono num"), td(x.reboot_port || "—", "mono num"), td(x.reconnect_port || "—", "mono num"),
          td(x.lan_ip.replace(/\.100$/, ".1"), "mono"), td(x.dev || "нет на машине", x.dev ? "" : "muted"), td((x.changed || []).map((f) => IMP_F[f] || f).join(", "), "muted")))
          .concat((d.removed || []).map((x) => el("tr", {}, el("td", {}, el("span", {class: "tag bad", text: "убрать"})), td(x.id, "num"), td(x.login, "mono"),
            td(x.port, "mono num"), td(""), td(""), td(x.lan_ip.replace(/\.100$/, ".1"), "mono"), td("нет в таблице", "muted"), td(""))));
        planBox.replaceChildren(
          el("div", {style: "margin-bottom:8px"}, el("b", {text: "План: "}), "добавить " + n("add") + ", поправить " + n("change") + ", как есть " + n("same")
            + ", убрать " + (d.removed || []).length + (d.on ? "" : turnOn.checked ? " · modlink включится" : " · modlink выключен — заработает после включения")),
          el("div", {class: "tbl-wrap"}, el("table", {class: "t"}, el("thead", {}, el("tr", {}, ["", "строка", "логин", "порт", "ребут", "реконнект", "модем", "интерфейс", "что меняется"]
            .map((h) => el("th", {text: h})))), el("tbody", {}, rows))),
          ...Object.entries(d.settings || {}).map(([k, v]) => el("div", {class: "muted", text: (IMP_SET[k] || k) + " → " + (k === "mode" ? (v === "shared" ? "один порт на всех" : "тройками") : v)})),
          ...(d.warnings || []).map((w) => el("div", {class: "warn-line", text: "⚠ " + w})));
      }
      async function check() {
        planBox.replaceChildren(el("div", {class: "muted", text: "проверяю…"}));
        const r = await X.POST("/targets/" + id + "/modlink/import", Object.assign(body(), {dry: true}));
        if (!r.ok) { planBox.replaceChildren(el("div", {class: "warn-line", text: "Ошибка: " + r.error})); return; }
        showPlan(r.data || {});
        checked = sig();
        if (applyBtn) applyBtn.disabled = false;
      }
      modal("Конфиг по таблице · " + where, el("div", {},
        el("p", {class: "muted", style: "margin-top:0", text: "Строки, которые уже у клиентов, остаются как есть: modlink подстроится, чтобы каждая заработала ровно так —" +
          " тот же порт, логин, пароль, ссылки ребута и реконнекта, внешний адрес. Сначала «Проверить» — покажу, что изменится."}),
        ta, el("div", {style: "margin-top:8px"}, url), el("div", {style: "margin-top:8px"}, how),
        el("div", {style: "margin-top:8px;display:flex;gap:18px;flex-wrap:wrap"},
          el("label", {}, repl, " убрать строки modlink, которых нет в таблице"), isOn ? null : el("label", {}, turnOn, " включить modlink")),
        planBox), [
        {text: "Закрыть"},
        {text: "Проверить", onclick: () => check()},
        {text: "Применить", cls: "primary", ref: (b) => { applyBtn = b; b.disabled = true; }, onclick: async (close) => {
          if (checked !== sig()) { toast("сначала «Проверить»", "warn"); return; }
          applyBtn.disabled = true;
          const r = await X.POST("/targets/" + id + "/modlink/import", Object.assign(body(), {dry: false}));
          if (!r.ok) { planBox.prepend(el("div", {class: "warn-line", text: "Ошибка: " + r.error})); applyBtn.disabled = false; return; }
          close();
          toast("modlink: конфиг по таблице применён · " + where, "ok");
          tests = {};
          await load();
        }}]);
      ta.closest(".modal").classList.add("wide");
    }
    const logsMenu = menuBtn("Логи", [["3proxy", "Логи 3proxy", "list"], ["modlink", "Логи modlink (демон)", "list"]], async (which) => {
      const x = await X.run(id, "modlink", ["logs", which]);
      if (!x.ok) { setRes(res, "err", "Ошибка: " + x.error); return; }
      showText((which === "3proxy" ? "Логи 3proxy" : "Логи modlink") + " · " + where, ((x.data && x.data.lines) || []).join("\n") || "пусто");
    }, "журналы служб");

    powerBar.hidden = true;
    const root = el("div", {}, legacyBox,
      el("div", {class: "toolbar"}, powerBar, pill, engineEl, el("span", {class: "spacer"}), dirtyEl, bApply),
      netCard,
      el("div", {class: "sec-head"}, el("h2", {text: "Модемы"}), cnt, el("span", {class: "spacer"}), res),
      el("div", {class: "toolbar"}, bAdd, bRange, bIf, bImp, copyMenu, bTbl, logsMenu, el("span", {class: "spacer"}),
        btn("Обновить", () => load(), "ghost", "перечитать (несохранённое пропадёт)")),
      tbl, out);
    let loaded = false;
    const pl = poller(15000, pollStatus);
    return {
      el: root,
      show() { if (!loaded) { loaded = true; tbl.replaceChildren(skelRows(6)); load(); } else pollStatus(); pl.start(); },
      hide() { pl.stop(); },
      dispose() { pl.stop(); },
    };
  }

  const ML_STATE = {ok: ["ok", "работает"], warn: ["warn", "модем/выход"], broken: ["bad", "сломан"],
    disabled: ["", "выключен"], pending: ["info", "ждёт «Применить»"], off: ["", "modlink выключен"]};
  const mlDot = (st) => st ? (ML_STATE[st.state] || ["bad"])[0] : "";
  function mlStateText(st) {
    if (!st) return "";
    const t = [(ML_STATE[st.state] || [, st.state])[1]];
    const idle = st.state === "off" || st.state === "disabled";     // не слушает — так и задумано
    if (st.port_up === false && !idle) t.push("порт " + (st.port || "") + " не слушает");
    if (st.trigger_up === false && !idle) t.push("порт управления не слушает" + (st.trigger_error ? ": " + st.trigger_error : ""));
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
    const facts = el("div", {class: "facts"}, el("span", {class: "skel", style: "width:320px"}));
    const fact = (list) => facts.replaceChildren(...list.filter(Boolean).map((t) => el("span", {text: t})));
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
    // Ключ, по которому хаб узнаёт ВМ. Меняется, когда cloud-init считает ВМ новой машиной: тогда
    // «Host key verification failed». Новый сверяется через guest agent Proxmox (мимо сети).
    const hkText = el("span", {class: "mono muted"});
    const hkAccept = el("span");
    const rHk = H ? null : actRow("Ключ SSH сервера", "по нему хаб узнаёт ВМ; сменился (переустановка, cloud-init) — сверить через guest agent Proxmox и принять",
      hkText, btn("Проверить", (e) => checkHk(e.target)), hkAccept);
    async function checkHk(b) {
      hkAccept.replaceChildren();
      const r = await act(b, rHk.res, () => X.POST("/servers/" + id + "/hostkey", {}),
        (d) => d.same ? "не менялся" : !(d.shown || []).length ? "сервер не ответил" : d.agent_ok ? "сменился — guest agent подтверждает новый" : "сменился — подтвердить нечем");
      if (!r || !r.ok) return;
      const d = r.data || {};
      hkText.textContent = (d.shown || []).join(", ") || "—";
      if (d.same || !(d.shown || []).length) return;
      const fp = d.shown[0];
      hkAccept.replaceChildren(btn(d.agent_ok ? "Принять новый" : "Принять " + fp.slice(0, 18) + "…", async (e) => {
        if (!d.agent_ok && !window.confirm("Принять ключ " + fp + "?\nСверь его в консоли ВМ: ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub")) return;
        const a = await act(e.currentTarget, rHk.res, () => X.POST("/servers/" + id + "/hostkey", {accept: d.agent_ok ? "agent" : fp}), "Принят");
        if (a && a.ok) { hkAccept.replaceChildren(); (X.node ? refreshNodes() : refresh()); }
      }, d.agent_ok ? "" : "danger"));
    }
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
      if (!r.ok) { fact(["Ошибка: " + r.error]); return; }
      const d = r.data || {}, v = d.versions || {};
      if (H) {
        fact([d.os, d.ip, d.cpu && d.cpu.cores && d.cpu.cores + " ядер", d.ram && "ОЗУ " + gb(d.ram),
          "PCS " + (d.version || "—"), d.proxyveth && (d.proxyveth.mode ? "proxyveth " + modeShort(d.proxyveth.mode) : "proxyveth: режим не задан")]);
        return;
      }
      fact([d.os, d.cores && d.cores + " ядер", d.ram_gb && "ОЗУ " + d.ram_gb + " ГБ", d.disk_gb && "диск " + d.disk_gb + " ГБ",
        ...Object.entries(v).map(([k, x]) => k + " " + (x || "—"))]);
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
      const b = e.currentTarget;               // клик мог прийти в иконку внутри кнопки
      if (ask && !window.confirm(ask)) return;
      const r = await act(b, rPower.res, () => X.POST("/servers/" + id + "/power", {action}), (d) => "ВМ: " + (d && d.state || "готово"));
      if (r && r.ok) (X.node ? refreshNodes() : refresh());
    }, cls);
    const rPower = actRow("Питание ВМ", "через Proxmox: включить, выключить штатно (до 2 мин), перезагрузить, остановить жёстко", el("span"),
      power("start", [ic("play"), "Включить"]), power("shutdown", [ic("power"), "Выключить"], "Выключить ВМ " + id + "? Модемы на ней пропадут."),
      power("reboot", [ic("redo"), "Перезагрузить"], "Перезагрузить ВМ " + id + "?"),
      power("stop", [ic("stop"), "Стоп"], "Жёстко остановить ВМ " + id + "? Как выдернуть питание.", "danger"));
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
    // разделы: что с машиной, как на неё попасть, сеть и софт; опасное — в конце
    const sec = (title, ...rows) => {
      rows = rows.filter(Boolean);
      return rows.length ? el("section", {class: "set-sec"}, el("h2", {text: title}), el("div", {class: "acts"}, rows)) : null;
    };
    const root = el("div", {class: "stagger-sec"}, facts,
      sec(H ? "Хост" : "Машина", H ? null : rPower.el, H ? null : rColor.el, rUp.el),
      sec("Доступ", rPw.el, rKey.el, rHk && rHk.el, H && !X.node ? rLink.el : null),
      sec("Сеть и софт", rDns.el, rMp.el, rHl.el),
      sec("Осторожно", rMode.el, H ? null : rForget.el));
    let loaded = false;
    async function loadFp() {
      const r = await X.GET("/self");
      fpText.textContent = r.ok && r.data.fingerprint ? r.data.fingerprint : "HTTPS выключен — pcs web on (сертификат создастся сам)";
    }
    return {el: root, update, show() { update(); if (!loaded) { loaded = true; loadHl(); loadFacts(); if (H) loadFp(); } }};
  }

  // ── создать ВМ ────────────────────────────────────────────────────────────
  // Форма слева, справа — план: что сделает задание с этими настройками (меняется на ходу).
  function optCard(name, value, checked, head, sub) {
    const input = el("input", {type: "radio", name, value, checked: checked || null});
    return {input, el: el("label", {class: "opt"}, input, el("b", {text: head}), el("span", {text: sub}))};
  }
  function planList(items) {
    return el("ol", {class: "plan"}, items.map(([t, on, sub]) => el("li", {class: on === false ? "off" : ""},
      el("span", {class: "mk", "aria-hidden": "true"}), el("div", {}, el("div", {text: t}), sub ? el("div", {class: "sub", text: sub}) : null))));
  }
  function pageCreate(X) {
    const f = {
      name: el("input", {placeholder: "pcs3000", required: true, pattern: "[A-Za-z0-9][A-Za-z0-9\\-]*", title: "латиница, цифры и дефис; с буквы или цифры", autocomplete: "off", spellcheck: "false"}),
      cores: el("input", {type: "number", min: 1, max: 256, value: "4"}),
      ram: el("input", {type: "number", min: 1, max: 4096, value: "8"}),
      disk: el("input", {type: "number", min: 8, max: 65536, value: "40"}),
      sheet: el("input", {type: "url", placeholder: "https://docs.google.com/spreadsheets/d/…/edit#gid=0"}),
      mp: el("input", {type: "checkbox"}),
    };
    const usb = optCard("mode", "usb", true, "USB", "модем виден как USB Huawei E3372h; до 40 на ВМ");
    const gw = optCard("mode", "gw", false, "Шлюз", "без USB и без такого предела");
    const res = el("span", {class: "res"});
    const submit = el("button", {type: "submit", class: "primary"}, "Создать ВМ");
    const plan = el("div");
    const form = el("form", {class: "form"},
      el("div", {class: "fs"}, el("div", {class: "fs-t", text: "Машина"}),
        el("label", {}, "Имя ВМ — оно же имя хоста", f.name),
        el("div", {class: "row3"}, el("label", {}, "Ядра", f.cores), el("label", {}, "ОЗУ, ГБ", f.ram), el("label", {}, "Диск, ГБ", f.disk))),
      el("div", {class: "fs"}, el("div", {class: "fs-t", text: "Режим proxyveth"}), el("div", {class: "opts"}, usb.el, gw.el)),
      el("div", {class: "fs"}, el("div", {class: "fs-t", text: "Модемы"}),
        el("label", {}, "Ссылка на таблицу модемов — можно дать потом", f.sheet),
        el("label", {class: "inline"}, f.mp, "поставить mobileproxy.space")),
      el("div", {class: "fs bar-end"}, submit, res));
    function drawPlan() {
      const n = f.name.value.trim() || "…";
      plan.replaceChildren(el("div", {class: "aside-t", text: "Что будет"}), planList([
        ["ВМ «" + n + "» в Proxmox", true, (+f.cores.value || "?") + " ядер · " + (+f.ram.value || "?") + " ГБ ОЗУ · диск " + (+f.disk.value || "?") + " ГБ"],
        ["Ubuntu 24.04 и обновления", true, usb.input.checked ? "и ядро с USB-модулями — одна перезагрузка" : null],
        ["PCS, proxyveth и modlink", true, "proxyveth — " + (usb.input.checked ? "USB" : "шлюз")],
        ["Модемы по таблице", !!f.sheet.value.trim(), f.sheet.value.trim() ? null : "таблицы нет — добавишь потом во вкладке proxyveth"],
        ["mobileproxy.space", f.mp.checked, f.mp.checked ? "в конце ВМ перезагрузится" : "не ставим"],
      ]), el("p", {class: "aside-note", text: "Идёт в фоне: страницу можно закрыть, задание доработает. Журнал откроется сам."}));
    }
    form.addEventListener("input", drawPlan);
    form.addEventListener("change", drawPlan);
    drawPlan();
    form.addEventListener("submit", async (ev) => {
      ev.preventDefault();
      const body = {name: f.name.value.trim(), cores: +f.cores.value, ram_gb: +f.ram.value, disk_gb: +f.disk.value,
        mode: gw.input.checked ? "gw" : "usb", sheet: f.sheet.value.trim(), mpspace: f.mp.checked};
      const r = await act(submit, res, () => X.POST("/servers", body), "Запущено");
      if (r && r.ok && r.data && r.data.job != null) location.hash = X.href("/job/" + encodeURIComponent(r.data.job));
    });
    const ph = pageHead({});
    ph.set("Новая ВМ", [X.node ? "на хосте " + X.node : "на этом хосте", "Ubuntu 24.04 под ключ"]);
    return {kind: "create", node: X.node, el: el("div", {}, ph.el, el("div", {class: "split"}, form, el("aside", {class: "aside-card"}, plan))),
      update() { document.title = "Новая ВМ — PCS"; }};
  }

  function pageJob(X, jid) {
    const ph = pageHead({});
    ph.set("Задание", ["задание " + jid]);
    const jv = jobView(jid, {page: true, base: X.node ? X.base : undefined,
      onTitle: (j) => ph.set(j.title || "Задание", ["задание " + jid, j.started ? "начато " + new Date(j.started * 1000).toLocaleString("ru-RU", {hour: "2-digit", minute: "2-digit", day: "numeric", month: "short"}) : null])});
    return {kind: "job", el: el("div", {}, ph.el, jv), update() { document.title = "Задание — PCS"; }};
  }

  // ── присоединить хост (VPS или другой Proxmox) ──────────────────────────
  function pageJoin() {
    const f = {addr: el("input", {placeholder: "95.165.83.236 или vps.example.ru", required: true, autocomplete: "off", spellcheck: "false"}),
      user: el("input", {value: "admin", autocomplete: "off"}),
      pw: el("input", {type: "password", autocomplete: "new-password", required: true}),
      name: el("input", {placeholder: "как у того хоста", autocomplete: "off"}),
      fp: el("input", {placeholder: "AB:CD:… — со страницы того хоста", autocomplete: "off", spellcheck: "false", class: "mono"})};
    const res = el("span", {class: "res"});
    const submit = el("button", {type: "submit", class: "primary"}, "Присоединить");
    const form = el("form", {class: "form"},
      el("div", {class: "fs"}, el("div", {class: "fs-t", text: "Тот хост"}),
        el("label", {}, "Адрес — порт 666 по умолчанию", f.addr),
        el("div", {class: "row3"}, el("label", {}, "Логин его панели", f.user), el("label", {}, "Пароль его панели", f.pw), el("label", {}, "Имя здесь", f.name))),
      el("div", {class: "fs"}, el("div", {class: "fs-t", text: "Проверка"}),
        el("label", {}, "Отпечаток сертификата — необязательно, но так надёжнее", f.fp)),
      el("div", {class: "fs bar-end"}, submit, res));
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const r = await act(submit, res, () => POST("/api/nodes/join", {addr: f.addr.value, user: f.user.value, password: f.pw.value,
        name: f.name.value, fingerprint: f.fp.value}), (d) => "Присоединён: " + d.name);
      f.pw.value = "";
      if (r && r.ok) { await refreshNodes(); location.hash = "#/n/" + encodeURIComponent(r.data.name); }
    });
    const cmd = "bash <(curl -fsSL https://raw.githubusercontent.com/Tovarish666/pcs/main/install.sh)";
    const aside = el("aside", {class: "aside-card"}, el("div", {class: "aside-t", text: "Как это работает"}),
      planList([["На том хосте стоит PCS", true, "Ubuntu-VPS или Proxmox; установка — одной командой:"]]),
      el("div", {class: "cmd"}, el("code", {text: cmd}), ibtn("copy", "скопировать команду", async () => {
        const ok = await copy(cmd);
        toast(ok ? "Команда скопирована" : "Не вышло — выделите и Ctrl+C", ok ? "ok" : "bad");
      }, "ghost")),
      planList([["Открыт порт 666", true, "панель того хоста; хаб ходит только по HTTPS"],
        ["Логин и пароль — один раз", true, "дальше у хаба свой токен к тому хосту"],
        ["Хост появится в дереве слева", true, "его ВМ и модемы — как свои"]]));
    const ph = pageHead({});
    ph.set("Присоединить хост", ["управлять другим хостом PCS отсюда"]);
    return {kind: "join", el: el("div", {}, ph.el, el("div", {class: "split"}, form, aside)), update() { document.title = "Присоединить хост — PCS"; }};
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
    const tabOf = (t) => t === "proxyveth" ? "net" : t;      // старые ссылки на вкладку proxyveth
    if (p[0] === "vm" && /^\d+$/.test(p[1] || "")) return {node, kind: "vm", id: p[1], tab: tabOf(p[2] || "net")};
    if (p[0] === "md" && /^(host|\d+)$/.test(p[1] || "")) return {node, kind: "md", id: p[1]};
    if (p[0] === "create") return {node, kind: "create"};
    if (p[0] === "job" && p[1]) return {node, kind: "job", id: p[1]};
    if (!node && p[0] === "join") return {kind: "join"};
    if (p[0] === "host") return {node, kind: "host", tab: tabOf(p[1] || "")};
    return {node, kind: "host", tab: ""};
  }
  function route() {
    const r = parse();
    S.route = r;
    document.body.classList.remove("side-open");
    const same = S.page && (S.page.node || null) === (r.node || null) && S.page.kind === r.kind &&
      (r.kind === "host" || (r.kind === "vm" && S.page.id === r.id));
    if (same) { S.page.show(r.tab); renderTree(); renderHeader(); return; }
    if (S.page && S.page.dispose) S.page.dispose();
    const X = ctx(r.node);
    document.body.classList.toggle("viewer", r.kind === "md");
    const page = r.kind === "md" ? pageMd(X, r.id) : r.kind === "vm" ? pageVm(X, r.id) : r.kind === "create" ? pageCreate(X)
      : r.kind === "job" ? pageJob(X, r.id) : r.kind === "join" ? pageJoin() : pageHost(X);
    S.page = page;
    $("#main").replaceChildren(el("div", {class: "page page-in"}, page.el));
    if (page.update) page.update();
    if (page.show) page.show(r.tab);
    renderTree();
    renderHeader();
    $("#main").scrollTop = 0;
  }

  // Обзор хаба. Последний удачный — в localStorage: при входе дерево и шапки рисуются сразу,
  // а свежие данные приходят следом (раньше экран ждал опрос всех ВМ — до 20 с).
  const OV_KEY = "pcs-ov";
  function ovCache(v) {
    try {
      if (v === undefined) { const x = JSON.parse(localStorage.getItem(OV_KEY) || "null"); return x && x.host ? x : null; }
      localStorage.setItem(OV_KEY, JSON.stringify(v));
    } catch (e) { return null; }
    return null;
  }
  let refreshing = null;
  function refresh() {
    if (refreshing) return refreshing;
    refreshing = (async () => {
      const r = await GET("/api/overview");
      if (r.ok) { S.ov = r.data; S.ovErr = null; S.ovStale = !!(r.data && r.data.stale); ovCache(r.data); }
      else S.ovErr = "Хаб: " + r.error;
      renderHeader(); renderTree();
      if (S.page && S.page.update) S.page.update();
      // хаб отдал сохранённое и обновляет в фоне — спросить ещё раз вскоре
      if (S.ovStale) setTimeout(() => refresh(), 2500);
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
    treeSearch();
    const cached = ovCache();
    if (cached) { S.ov = cached; S.ovStale = true; }
    renderHeader(); renderTree();
    const r = await GET("/api/session");
    if (!r.ok) return;
    S.session = r.data;
    $("#logout").addEventListener("click", async () => { await POST("/api/logout", {}); location.href = "/login"; });
    window.addEventListener("hashchange", route);
    route();                                 // страница — сразу, данные дотянутся
    if (S.route.kind === "md") return;       // окно веба модемов: обзор и дерево ему не нужны
    refresh();
    poller(10000, refresh).start();
    refreshNodes();
    poller(15000, refreshNodes).start();
    document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(); });
  }
  boot();
})();

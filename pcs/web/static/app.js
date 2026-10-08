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
    let r;
    try { r = await fetch(path, opt); } catch (e) {
      return {ok: false, error: "панель не отвечает — сеть или pcs-web остановлена"};
    }
    if (r.status === 401) { location.href = "/login"; return {ok: false, error: "нужен вход"}; }
    try { return await r.json(); } catch (e) {
      return {ok: false, error: "ответ панели не JSON (HTTP " + r.status + ")"};
    }
  }
  const GET = (p) => api("GET", p);
  const POST = (p, b) => api("POST", p, b);
  const run = (target, part, args, extra) =>
    POST("/api/run", Object.assign({target: String(target), part, args}, extra || {}));

  // ── формат ────────────────────────────────────────────────────────────────
  const MODE = {usb: "USB", gw: "шлюз"};
  const modeShort = (m) => MODE[m] || m || "—";
  const modeLong = (m) => m === "usb" ? "proxyveth USB" : m === "gw" ? "proxyveth шлюз" : "proxyveth: режим не задан";
  const VM_STATE = {running: "работает", stopped: "остановлена", unreachable: "нет связи", creating: "создаётся",
    error: "ошибка", paused: "на паузе"};
  const BAD_VM = ["stopped", "unreachable", "error", "down", "broken", "offline", "failed"];
  const ROW_STATE = {ok: ["ok", "работает"], warn: ["warn", "апстрим"], broken: ["bad", "сломан"],
    absent: ["", "нет"], disabled: ["", "выключен"], rebooting: ["info", "перезагрузка"]};

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
  async function copy(text) {
    try { await navigator.clipboard.writeText(text); return true; } catch (e) { return false; }
  }
  function showText(title, text) {
    const pre = el("pre", {text: text || "—"});
    modal(title, pre, [
      {text: "Копировать", onclick: async () => { pre.dataset.c = (await copy(text)) ? "1" : ""; }},
      {text: "Закрыть", cls: "primary"}]);
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
        el("label", {}, "Введите номер ВМ ", el("b", {text: String(expected)}), ", чтобы подтвердить: ", inp));
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
  function setRes(res, cls, text) { res.className = "res " + cls; res.textContent = text; res.title = text; }
  async function act(button, res, fn, okText, jobBox) {
    if (button) button.disabled = true;
    setRes(res, "run", "выполняется…");
    let r;
    try { r = await fn(); } catch (e) { r = {ok: false, error: String(e)}; }
    if (button) button.disabled = false;
    if (!r || !r.ok) { setRes(res, "err", "Ошибка: " + ((r && r.error) || "неизвестно")); return r; }
    const d = r.data;
    if (d && typeof d === "object" && d.job != null && jobBox) {
      setRes(res, "ok", "Запущено");
      jobBox.replaceChildren(jobView(d.job, {}));
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
        opts.page ? null : el("a", {href: "#/job/" + encodeURIComponent(jid), class: "muted", text: "журнал отдельно"})), pre, tail);
    let first = true;
    async function tick() {
      if (!first && !box.isConnected) return;
      first = false;
      const r = await GET("/api/jobs/" + encodeURIComponent(jid));
      if (!r.ok) { setRes(st, "err", "Ошибка: " + r.error); return; }
      const j = r.data || {};
      if (j.title && !opts.title) box.firstChild.firstChild.textContent = j.title;
      const atEnd = pre.scrollTop + pre.clientHeight >= pre.scrollHeight - 4;
      pre.textContent = (j.log || []).join("\n") || "…";
      if (atEnd) pre.scrollTop = pre.scrollHeight;
      if (j.state === "running" || j.state === "queued") { setTimeout(tick, 1500); return; }
      if (j.state === "done") {
        setRes(st, "ok", "готово");
        if (j.result && j.result.id != null) tail.replaceChildren(el("p", {}, link("Открыть ВМ " + j.result.id, "#/vm/" + j.result.id, "btn primary")));
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
    $("#h-port").textContent = S.session ? ":" + S.session.port : "";
    $("#h-user").textContent = S.session ? S.session.user : "";
    $("#h-fake").hidden = !(S.session && S.session.fake);
  }
  function hostLevel() {
    const list = (S.ov && S.ov.servers) || [];
    if (S.ovErr || (S.ov && (S.ov.host_error || S.ov.servers_error))) return "bad";
    const lv = list.map(level);
    return lv.includes("bad") ? "bad" : lv.includes("warn") ? "warn" : "ok";
  }
  function renderTree() {
    const nav = $("#tree");
    const host = (S.ov && S.ov.host) || {};
    const cur = S.page || {};
    const kids = [el("a", {class: "tree-host" + (cur.kind === "host" ? " active" : ""), href: "#/"},
      el("span", {class: "dot " + hostLevel()}), host.name || "хост", el("span", {class: "muted", text: "Proxmox"}))];
    for (const s of (S.ov && S.ov.servers) || []) {
      const c = counts(s);
      kids.push(el("a", {class: "tree-vm" + (cur.kind === "vm" && String(cur.id) === String(s.id) ? " active" : ""),
        href: "#/vm/" + s.id, title: levelText(s), style: "--h:" + hue(s.id)},
        el("span", {class: "dot " + level(s)}), el("span", {}, el("b", {text: String(s.id)}), " ", s.name || ""),
        el("span", {class: "sub", text: modeShort(s.mode) + " · " + c.ok + "/" + c.total})));
    }
    kids.push(el("a", {class: "tree-add", href: "#/create", text: "+ Создать ВМ"}));
    nav.replaceChildren(...kids);
  }

  // ── страница хоста ────────────────────────────────────────────────────────
  function pageHost() {
    const banner = el("div"), head = el("div"), cards = el("div", {class: "cards"}), list = el("div", {class: "list"});
    const hl = el("span", {class: "muted"}), ver = el("span", {class: "muted"});

    const dns = el("input", {placeholder: "1.1.1.1 8.8.8.8 (необязательно)"});
    const rDns = actRow("Починить DNS", "resolved и netplan по правилам PCS; проверка github, Google, mp.space", dns,
      btn("Починить", (e) => act(e.target, rDns.res, () => POST("/api/targets/host/dns", {dns: dns.value}), "Применено")));
    const pw = el("input", {type: "password", autocomplete: "new-password", placeholder: "новый пароль root"});
    const rPw = actRow("Пароль root", "пароль root хоста Proxmox", pw,
      btn("Сохранить", async (e) => {
        const r = await act(e.target, rPw.res, () => POST("/api/targets/host/passwd", {password: pw.value}), "Сохранено");
        if (r && r.ok) pw.value = "";
      }));
    const key = el("input", {placeholder: "ssh-ed25519 AAAA… имя"});
    const rKey = actRow("Ключ SSH", "открытый ключ — в authorized_keys root хоста", key,
      btn("Добавить", async (e) => {
        const r = await act(e.target, rKey.res, () => POST("/api/targets/host/key", {pubkey: key.value}), "Сохранено");
        if (r && r.ok) key.value = "";
      }));
    const rHl = actRow("hivelink", "драйвер настоящих модемов: на хост ставится сам", hl,
      btn("Состояние", async (e) => {
        const r = await act(e.target, rHl.res, () => run("host", "hivelink", ["status"]), (d) => summary(d, ["modems"]));
        if (r && r.ok) showText("hivelink на хосте", JSON.stringify(r.data, null, 1));
      }),
      btn("Обновить", (e) => act(e.target, rHl.res, () => run("host", "hivelink", ["install"]), "Применено")));
    const rUp = actRow("Обновить PCS", "одна версия кода на хосте и всех серверах", ver,
      btn("Обновить", (e) => act(e.target, rUp.res, () => POST("/api/targets/host/update", {}), "Применено", rUp.jobs)));
    const rTerm = actRow("Терминал хоста", "консоль root на хосте Proxmox", link("Открыть", "#/host/term"));
    const acts = el("div", {class: "acts"}, rDns.el, rPw.el, rKey.el, rHl.el, rUp.el, rTerm.el);

    const root = el("div", {}, banner, head, cards, el("h2", {text: "Виртуальные машины"}), list,
      el("h2", {text: "Хост"}), acts);

    function update() {
      const ov = S.ov || {}, host = ov.host || {}, list_ = ov.servers || [];
      document.title = (host.name || "хост") + " — PCS";
      const errs = [S.ovErr, ov.host_error && "хост: " + ov.host_error, ov.servers_error && "серверы: " + ov.servers_error].filter(Boolean);
      banner.replaceChildren(...errs.map((t) => el("div", {class: "banner", text: t})));
      const h = host.hivelink || {};
      const real = Array.isArray(h.modems) ? h.modems.length : (+h.modems || 0);
      head.replaceChildren(el("h1", {text: host.name || "хост"}),
        el("div", {class: "muted", text: ["Хост Proxmox" + (host.pve ? " " + host.pve : ""),
          h.installed === false ? "hivelink не установлен" : "hivelink установлен",
          real + " " + plural(real, "настоящий модем", "настоящих модема", "настоящих модемов")].join(" · ")}));
      hl.textContent = h.installed === false ? "не установлен" : ["установлен", h.version, real + " " + plural(real, "модем", "модема", "модемов")].filter(Boolean).join(" · ");
      ver.textContent = "версия " + (host.version || (S.session && S.session.version) || "—");
      let ok = 0, total = 0, probs = 0, up = 0;
      for (const s of list_) {
        const c = counts(s);
        ok += c.ok; total += c.total; probs += c.warn + c.broken;
        if (!BAD_VM.includes(String(s.state || "").toLowerCase())) up += 1; else probs += 1;
      }
      const card = (k, v, cls) => el("div", {class: "card"}, el("div", {class: "k", text: k}), el("div", {class: "v " + (cls || ""), text: v}));
      cards.replaceChildren(
        card("ВМ работают", up + " / " + list_.length, up < list_.length ? "bad" : ""),
        card("Модемы", ok + " / " + total, ok < total ? "warn" : ""),
        card("Проблемы", String(probs), probs ? "warn" : ""),
        card("ОЗУ хоста", gb(host.ram)));
      if (!list_.length) {
        list.replaceChildren(el("div", {class: "item"}, el("span", {class: "muted", text: "ВМ пока нет — "}), link("+ Создать ВМ", "#/create", "")));
        return;
      }
      list.replaceChildren(...list_.map((s) => {
        const c = counts(s);
        return el("div", {class: "item"}, el("span", {class: "dot " + level(s), title: levelText(s)}),
          el("div", {class: "grow"}, el("div", {class: "title", text: s.id + " " + (s.name || "")}),
            el("div", {class: "sub", text: [s.ip || "—", modeShort(s.mode), "модемы " + c.ok + "/" + c.total, mpText(s.mpspace)].join(" · ")})),
          el("div", {class: "btns"}, link("Открыть", "#/vm/" + s.id), link("Терминал", "#/vm/" + s.id + "/term"),
            btn("Удалить", () => deleteVm(s), "danger")));
      }));
    }
    return {kind: "host", el: root, update};
  }

  async function deleteVm(s) {
    const v = await confirmNumber("Удалить ВМ " + s.id + " · " + (s.name || ""),
      "ВМ " + s.id + " (" + (s.name || "") + ", " + (s.ip || "без IP") + ") будет остановлена и удалена вместе с дисками. Это необратимо.",
      s.id, "Удалить");
    if (v == null) return;
    const r = await POST("/api/servers/" + s.id + "/delete", {confirm: v});
    if (!r.ok) { showText("ВМ " + s.id + " не удалена", "Ошибка: " + r.error); return; }
    if (r.data && r.data.job != null) { location.hash = "#/job/" + encodeURIComponent(r.data.job); return; }
    await refresh();
    if (location.hash !== "#/") location.hash = "#/";
  }

  // ── страница ВМ ───────────────────────────────────────────────────────────
  const TABS = [["proxyveth", "proxyveth"], ["modlink", "modlink"], ["term", "терминал"], ["settings", "настройка и управление"]];

  function plaque(id) {
    const big = el("div", {class: "big"}), line = el("div", {class: "line"}), stDot = el("span", {class: "dot"}), stText = el("span");
    const box = el("div", {class: "plaque"}, el("div", {}, big, line), el("div", {class: "state"}, stDot, stText));
    function update() {
      box.style.setProperty("--h", String(hue(id)));
      const s = srv(id);
      if (!s) {
        big.textContent = "ВМ " + id;
        line.textContent = S.ov ? "нет в списке PCS — удалена или ещё создаётся" : "загрузка…";
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

  function pageVm(id) {
    const pl = plaque(id);
    const tabsEl = el("div", {class: "tabs"});
    const body = el("div");
    const panes = {};
    let cur = null;
    const make = {proxyveth: () => pvTab(id), modlink: () => mlTab(id), term: () => termTab(id), settings: () => setTab(id)};
    function show(tab) {
      if (!make[tab]) tab = "proxyveth";
      if (cur === tab) return;
      if (cur && panes[cur].hide) panes[cur].hide();
      if (cur) panes[cur].el.hidden = true;
      if (!panes[tab]) { panes[tab] = make[tab](); body.append(panes[tab].el); }
      panes[tab].el.hidden = false;
      cur = tab;
      tabsEl.replaceChildren(...TABS.map(([k, t]) => el("a", {href: "#/vm/" + id + (k === "proxyveth" ? "" : "/" + k), class: k === tab ? "active" : "", text: t})));
      if (panes[tab].show) panes[tab].show();
    }
    function update() {
      pl.update();
      for (const p of Object.values(panes)) if (p.update) p.update();
    }
    function dispose() { for (const p of Object.values(panes)) if (p.dispose) p.dispose(); }
    return {kind: "vm", id: String(id), el: el("div", {}, pl.el, tabsEl, body), show, update, dispose};
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
  function pvTab(id) {
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
    const bWan = btn("Проверить внешний IP", async (e) => {
      e.target.disabled = true; setRes(res, "run", "проверяю внешний IP через каждый модем…");
      await load(true); e.target.disabled = false; setRes(res, "ok", "внешний IP обновлён");
    }, "", "proxyveth status --wan");
    const bSrc = btn("Сменить ссылку", async () => {
      const url = await askText("Источник таблицы", "Ссылка на Google-таблицу (из адресной строки) или local:",
        source && source.url || "", "https://docs.google.com/spreadsheets/d/…");
      if (!url) return;
      await act(null, res, () => run(id, "proxyveth", ["source", url]), "Сохранено");
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
      const r = await act(bSync, res, () => run(id, "proxyveth", force ? ["sync", "--force"] : ["sync"], force ? {confirm} : null),
        (d) => "Применено" + (d && typeof d === "object" ? ": " + summary(d) : ""));
      if (r && !r.ok && /--force/.test(r.error || "") && !force)
        extra.replaceChildren(el("p", {}, btn("Синхронизировать с --force…", () => sync(true), "danger")));
      load(false);
      refresh();
    }

    function renderInfo() {
      const s = srv(id) || {};
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
      const c = {ok: 0, warn: 0, broken: 0};
      for (const r of rows) if (c[r.state] != null) c[r.state] += 1;
      cnt.textContent = "работают " + c.ok + " · апстрим " + c.warn + " · сломаны " + c.broken + " · всего " + rows.length;
      const shown = onlyBad.checked ? rows.filter((r) => r.state !== "ok") : rows;
      if (!rows.length) { tbl.replaceChildren(el("div", {class: "item muted", text: "модемов нет — таблица пуста или не задана"})); return; }
      const head = el("tr", {}, ["n", "real", "прокси", "состояние", "внешний IP", "проблемы", ""].map((t) => el("th", {text: t})));
      const body = shown.map((r) => {
        const [cls, txt] = ROW_STATE[r.state] || ["", r.state || "—"];
        return el("tr", {},
          el("td", {class: "num", text: String(r.n)}), el("td", {class: "num", text: r.real == null ? "—" : String(r.real)}),
          el("td", {class: "mono", text: maskProxy(r.proxy)}),
          el("td", {}, el("span", {class: "pill", title: r.since ? "с " + new Date(r.since * 1000).toLocaleString("ru-RU") : ""},
            el("span", {class: "dot " + cls}), txt)),
          el("td", {class: "mono", text: r.ext_ip || "—"}),
          el("td", {class: "problems", text: (r.problems || []).join("; ")}),
          el("td", {}, btn("диагн.", () => diag(r.n), "icon", "proxyveth diag " + r.n)));
      });
      tbl.replaceChildren(el("table", {class: "t"}, el("thead", {}, head), el("tbody", {}, body)));
    }

    async function diag(n) {
      const body = el("div", {}, el("p", {class: "muted", text: "прокси → логин → модем → SIM → интернет…"}));
      modal("Диагностика модема " + n + " · ВМ " + id, body);
      const r = await run(id, "proxyveth", ["diag", String(n)]);
      if (!r.ok) { body.replaceChildren(el("p", {class: "res err", text: "Ошибка: " + r.error})); return; }
      const d = r.data || {};
      body.replaceChildren(el("ul", {class: "steps"}, (d.steps || []).map((s) =>
        el("li", {}, el("span", {class: s.ok ? "ok" : "bad", text: s.ok ? "✓" : "✗"}), el("b", {text: s.name}), el("span", {text: s.text || ""})))),
        el("p", {}, el("b", {text: "Итог: "}), d.verdict || "—"));
    }

    async function load(wan) {
      const r = await GET("/api/servers/" + id + "/proxyveth" + (wan ? "?wan=1" : ""));
      apply(r);
    }
    async function pollStatus() { apply(await GET("/api/servers/" + id + "/proxyveth?only=status")); }
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
      const r = await GET("/api/servers/" + id + "/proxyveth/table");
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
        const r2 = await act(bSave, eres, () => POST("/api/servers/" + id + "/proxyveth/table", {csv: ta.value, make_main: mainCb.checked}),
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
    const root = el("div", {}, info, el("div", {class: "bar"}, bSync, bEdit, bWan, bSrc, res), extra, editor,
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
  const ML_COLS = [["enabled", "вкл", "строка активна"], ["name", "имя", "метка"], ["login", "логин"], ["password", "пароль"],
    ["port", "порт", "порт прокси (постоянный)"], ["lan_ip", "LAN IP", "адрес интерфейса модема на сервере"],
    ["modem_ip", "IP модема", "веб-интерфейс Huawei (HiLink)"], ["reconnect_port", "реконнект", "порт триггера: GET http://IP:порт/reconnect"],
    ["interval_min", "мин", "автореконнект, минут (0 — выкл)"]];

  function mlTab(id) {
    const statusLine = el("div", {class: "muted"});
    const tbl = el("div", {class: "tbl-wrap"});
    const res = el("span", {class: "res"});
    const out = el("div", {class: "muted mono"});
    const dirtyEl = el("span", {class: "problems"});
    let orig = [], states = {}, edits = {}, added = [], deleted = new Set(), revealed = {}, err = null, seq = 0, pending = false;

    const val = (r, k) => (edits[r.id] && k in edits[r.id]) ? edits[r.id][k] : r[k];
    function setEdit(r, k, v) {
      const e = edits[r.id] || (edits[r.id] = {});
      const o = k === "password" ? revealed[r.id] : r[k];
      if (String(v) === String(o == null ? "" : o)) delete e[k]; else e[k] = v;
      if (!Object.keys(e).length) delete edits[r.id];
      markDirty();
    }
    function markDirty() {
      const n = Object.keys(edits).length + added.length + deleted.size;
      dirtyEl.textContent = n ? "несохранённых правок: " + n + " — «Применить»"
        : pending ? "есть неприменённые правки (modlink apply не было) — «Применить»" : "";
      for (const tr of tbl.querySelectorAll("tr[data-id]")) tr.classList.toggle("dirty", !!edits[tr.dataset.id]);
    }

    function inputFor(r, k, isNew) {
      const v = isNew ? r.fields[k] : k === "password" ? ((edits[r.id] || {}).password ?? revealed[r.id]) : val(r, k);
      const cls = k === "interval_min" ? "w-xs" : ["port", "reconnect_port"].includes(k) ? "w-s" : (k === "lan_ip" || k === "modem_ip") ? "w-ip" : "w-m";
      if (k === "enabled") {
        return el("input", {type: "checkbox", checked: !!v, onchange: (e) => isNew ? (r.fields[k] = e.target.checked) : setEdit(r, k, e.target.checked)});
      }
      return el("input", {value: v == null ? "" : String(v), class: cls, placeholder: isNew && /port/.test(k) ? "auto" : isNew && k === "password" ? "gen" : "",
        oninput: (e) => isNew ? (r.fields[k] = e.target.value) : setEdit(r, k, e.target.value)});
    }
    function pwCell(r) {
      const e = edits[r.id] || {};
      if (e.password === "gen")
        return el("span", {}, el("span", {class: "pw-gen", text: "новый (сгенерируется) "}),
          btn("↶", () => { delete e.password; if (!Object.keys(e).length) delete edits[r.id]; draw(); }, "icon", "не менять"));
      if (r.id in revealed) return el("span", {class: "pill"}, inputFor(r, "password"),
        btn("↺", () => { setEdit(r, "password", "gen"); draw(); }, "icon", "сгенерировать новый пароль"));
      return el("span", {class: "pill"},
        el("span", {class: "pw-hidden", text: r.password_set === false ? "—" : "••••••"}),
        btn("показать", () => reveal(r), "link", "показать пароль (явный запрос)"),
        btn("↺", () => { setEdit(r, "password", "gen"); draw(); }, "icon", "сгенерировать новый пароль"));
    }
    async function reveal(r) {
      const x = await POST("/api/servers/" + id + "/modlink/reveal", {id: r.id});
      if (!x.ok) { setRes(res, "err", "Ошибка: " + x.error); return; }
      revealed[r.id] = x.data.password;
      draw();
    }
    async function rowAct(r, what, args, ask) {
      if (ask && !window.confirm(ask)) return;
      out.textContent = "строка " + r.id + " · " + what + "…";
      const x = await run(id, "modlink", args);
      if (!x.ok) { out.textContent = "строка " + r.id + " · " + what + " — Ошибка: " + x.error; return; }
      if (args[0] === "log") {
        const L = Array.isArray(x.data) ? x.data : x.data && Array.isArray(x.data.lines) ? x.data.lines : null;
        const lines = L ? L.map((l) => typeof l === "string" ? l : JSON.stringify(l)).join("\n")
          : typeof x.data === "string" ? x.data : JSON.stringify(x.data, null, 1);
        out.textContent = "";
        showText("Журнал реконнектов · строка " + r.id + " · ВМ " + id, lines || "пусто");
        return;
      }
      out.textContent = "строка " + r.id + " · " + what + ": " + mlResult(args[0], x.data);
    }

    function draw() {
      if (err) { tbl.replaceChildren(el("div", {class: "banner", text: "Ошибка: " + err})); return; }
      const head = el("tr", {}, el("th"), ML_COLS.map(([, t, title]) => el("th", {text: t, title: title || null})), el("th"));
      const body = [];
      for (const r of orig) {
        const st = states[r.id];
        const gone = deleted.has(r.id);
        body.push(el("tr", {"data-id": String(r.id), class: (gone ? "gone" : "") + (edits[r.id] ? " dirty" : "")},
          el("td", {class: "ml-dot", "data-id": String(r.id), title: mlStateText(st)}, el("span", {class: "dot " + mlDot(st)})),
          ML_COLS.map(([k]) => el("td", {}, k === "password" ? pwCell(r) : inputFor(r, k))),
          el("td", {class: "acts-cell"},
            btn("Test", () => rowAct(r, "Test", ["test", String(r.id)]), "icon", "внешний IP через прокси + HiLink"),
            btn("⟳", () => rowAct(r, "реконнект", ["reconnect", String(r.id)]), "icon", "реконнект: сменить IP"),
            btn("↻", () => rowAct(r, "ребут", ["reboot", String(r.id)], "Перезагрузить модем строки " + r.id + " (" + (r.name || "") + ")?"), "icon", "перезагрузить модем"),
            btn("≡", () => rowAct(r, "лог", ["log", String(r.id)]), "icon", "журнал реконнектов"),
            btn("✕", () => { if (gone) deleted.delete(r.id); else deleted.add(r.id); draw(); markDirty(); }, "icon danger", gone ? "не удалять" : "удалить строку (при «Применить»)"))));
      }
      for (const a of added) {
        body.push(el("tr", {class: "new"}, el("td"),
          ML_COLS.map(([k]) => el("td", {}, inputFor(a, k, true))),
          el("td", {class: "acts-cell"}, btn("✕", () => { added = added.filter((x) => x !== a); draw(); markDirty(); }, "icon danger", "убрать новую строку"))));
      }
      if (!body.length) body.push(el("tr", {}, el("td", {colspan: ML_COLS.length + 2, class: "muted", text: "прокси нет — «Добавить» или «Из интерфейсов»"})));
      tbl.replaceChildren(el("table", {class: "t ml"}, el("thead", {}, head), el("tbody", {}, body)));
    }

    async function load() {
      const r = await GET("/api/servers/" + id + "/modlink");
      if (!r.ok) { err = r.error; draw(); return; }
      const L = r.data.list, St = r.data.status;
      err = L.ok ? null : L.error;
      orig = L.ok ? rowsOf(L.data) : [];
      pending = !!(L.ok && L.data && L.data.pending);
      applyStatus(St);
      edits = {}; added = []; deleted = new Set(); revealed = {};
      draw(); markDirty();
    }
    function applyStatus(St) {
      states = {};
      if (St && St.ok && St.data && typeof St.data === "object") {
        for (const x of rowsOf(St.data)) if (x && x.id != null) states[x.id] = x;
        if (!Array.isArray(St.data)) {
          statusLine.textContent = summary(St.data, ["rows", "pending"]);
          if (St.data.pending != null) pending = !!St.data.pending;
        }
      } else statusLine.textContent = St && !St.ok ? "modlink status — Ошибка: " + St.error : "";
      for (const td of tbl.querySelectorAll("td.ml-dot")) {
        const st = states[td.dataset.id];
        td.title = mlStateText(st);
        td.firstChild.className = "dot " + mlDot(st);
      }
      markDirty();
    }
    async function pollStatus() {
      const r = await GET("/api/servers/" + id + "/modlink?only=status");
      if (r.ok) applyStatus(r.data.status);
    }

    const bAdd = btn("Добавить", () => {
      added.push({tmp: ++seq, fields: {enabled: true, name: "", login: "", password: "", port: "", lan_ip: "", modem_ip: "", reconnect_port: "", interval_min: "0"}});
      draw(); markDirty();
    });
    const bIf = btn("Из интерфейсов", async (e) => {
      const r = await act(e.target, res, () => run(id, "modlink", ["from-ifaces"]), (d) => "предложено строк: " + rowsOf(d).length);
      if (!r || !r.ok) return;
      const sug = rowsOf(r.data);
      if (!sug.length) { setRes(res, "ok", "новых интерфейсов 192.168.N.100 нет — всё уже в таблице"); return; }
      modal("Строки по интерфейсам · ВМ " + id, el("div", {},
        el("p", {text: "modlink добавит строки (логин, пароль и порты — сам). В силу вступят после «Применить»."}),
        el("pre", {text: sug.map((x) => [x.lan_ip, x.modem_ip, x.iface, x.name].filter(Boolean).join("  ")).join("\n")})), [
        {text: "Отмена"},
        {text: "Добавить " + sug.length, cls: "primary", onclick: async (close) => {
          close();
          await act(bIf, res, () => run(id, "modlink", ["from-ifaces", "--add"]), (d) => "добавлено строк: " + ((d && d.added) || []).length + " — «Применить»");
          await load();
        }}]);
    }, "", "modlink from-ifaces: строки по интерфейсам 192.168.N.100 сервера");
    const bApply = btn("Применить", async (e) => {
      if (deleted.size && !window.confirm("Удалить строки modlink: " + [...deleted].join(", ") + "? Прокси на их портах перестанут работать.")) return;
      const payload = {
        del: [...deleted],
        set: Object.entries(edits).filter(([k]) => !deleted.has(Number(k)) && !deleted.has(k)).map(([k, f]) => ({id: Number(k), fields: f})),
        add: added.map((a) => a.fields), apply: true};
      const r = await act(e.target, res, () => POST("/api/servers/" + id + "/modlink/save", payload), "Применено");
      if (r && !r.ok && r.data && r.data.steps) {
        out.textContent = r.data.steps.map((s) => (s.ok ? "✓ " : "✗ ") + s.what + (s.error ? ": " + s.error : "")).join("   ");
      }
      await load();
    }, "primary", "сохранить правки и modlink apply (sing-box check → перезапуск)");
    const bExp = btn("Экспорт для клиента", async (e) => {
      const r = await act(e.target, res, () => POST("/api/servers/" + id + "/modlink/export", {}), "Готово");
      if (r && r.ok) showText("Экспорт для клиента · ВМ " + id, r.data.text || "пусто");
    }, "", "IP:PORT:LOGIN:PASS и ссылка реконнекта — с паролями");

    const root = el("div", {}, statusLine, el("div", {class: "bar"}, dirtyEl), tbl,
      el("div", {class: "bar"}, bAdd, bIf, bApply, bExp, btn("Обновить", () => load(), "link", "перечитать таблицу (несохранённое пропадёт)"), res), out);
    let loaded = false;
    const pl = poller(15000, pollStatus);
    return {
      el: root,
      show() { if (!loaded) { loaded = true; tbl.replaceChildren(el("div", {class: "item muted", text: "загрузка…"})); load(); } else pollStatus(); pl.start(); },
      hide() { pl.stop(); },
      dispose() { pl.stop(); },
    };
  }

  const ML_STATE = {ok: ["ok", "работает"], warn: ["warn", "апстрим/модем"], broken: ["bad", "сломан"],
    disabled: ["", "выключен"], pending: ["info", "ждёт «Применить»"]};
  const mlDot = (st) => st ? (ML_STATE[st.state] || ["bad"])[0] : "";
  function mlStateText(st) {
    if (!st) return "";
    const t = [(ML_STATE[st.state] || [, st.state])[1]];
    if (st.port_up === false) t.push("порт " + (st.port || "") + " не слушает");
    if (st.trigger_up === false) t.push("триггер не слушает" + (st.trigger_error ? ": " + st.trigger_error : ""));
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
    constructor(target, box, stateEl) { this.target = target; this.box = box; this.stateEl = stateEl; this.enc = new TextEncoder(); }
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
      const url = proto + "//" + location.host + "/ws/term?target=" + encodeURIComponent(this.target) +
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
  function termTab(target, full) {
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
      show() { if (!t) { t = new Term(target, box, st); t.open(); } else { t.refit(); if (t.t) t.t.focus(); } },
      dispose() { if (t) t.close(); },
    };
  }

  // ── вкладка «настройка и управление» ─────────────────────────────────────
  function setTab(id) {
    const T = "/api/targets/" + id;
    const facts = el("div", {class: "muted"});
    const dns = el("input", {placeholder: "1.1.1.1 8.8.8.8 (необязательно)"});
    const rDns = actRow("Починить DNS", "resolved, netplan без DNS от DHCP, проверка github, Google, mp.space", dns,
      btn("Починить", (e) => act(e.target, rDns.res, () => POST(T + "/dns", {dns: dns.value}), "Применено")));
    const pw = el("input", {type: "password", autocomplete: "new-password", placeholder: "новый пароль root"});
    const rPw = actRow("Пароль root", "пароль root этой ВМ (вход по SSH и в консоль Proxmox)", pw,
      btn("Сохранить", async (e) => {
        const r = await act(e.target, rPw.res, () => POST(T + "/passwd", {password: pw.value}), "Сохранено");
        if (r && r.ok) pw.value = "";
      }));
    const key = el("input", {placeholder: "ssh-ed25519 AAAA… имя"});
    const rKey = actRow("Ключ SSH", "добавить свой открытый ключ root или сменить ключ, которым ходит хаб", key,
      btn("Добавить", async (e) => {
        const r = await act(e.target, rKey.res, () => POST(T + "/key", {pubkey: key.value}), "Сохранено");
        if (r && r.ok) key.value = "";
      }),
      btn("Сменить ключ хаба", (e) => {
        if (!window.confirm("Сменить ключ, которым хаб ходит на ВМ " + id + "?")) return;
        act(e.target, rKey.res, () => POST(T + "/key", {rotate: true}), (d) => "Применено" + (d && d.fingerprint ? ": " + d.fingerprint : ""));
      }));
    const mpState = el("span", {class: "muted"});
    const auth = el("textarea", {rows: 2, placeholder: "содержимое auth.mp из ЛК mobileproxy.space (или выберите файл)", style: "max-width:420px"});
    const file = el("input", {type: "file", onchange: async (e) => { const f = e.target.files[0]; if (f) auth.value = (await f.text()).trim(); }});
    const rMp = actRow("mobileproxy.space", "софт агрегатора на этой ВМ: установка и ключ auth.mp", mpState,
      btn("Установить", (e) => act(e.target, rMp.res, () => POST(T + "/mpspace", {action: "install"}), "Применено", rMp.jobs)),
      btn("Проверить", (e) => act(e.target, rMp.res, () => POST(T + "/mpspace", {action: "check"}), (d) => summary(d && d.result !== undefined ? d.result : d))),
      auth, file,
      btn("Сохранить auth.mp", async (e) => {
        const r = await act(e.target, rMp.res, () => POST(T + "/mpspace", {action: "auth", auth: auth.value}), "Сохранено", rMp.jobs);
        if (r && r.ok) auth.value = "";
      }));
    const hlState = el("span", {class: "muted", text: "—"});
    const rHl = actRow("hivelink в эту ВМ", "драйвер настоящих модемов — если они воткнуты прямо в ВМ (проброс USB)", hlState,
      btn("Установить / обновить", async (e) => {
        const r = await act(e.target, rHl.res, () => run(id, "hivelink", ["install"]), "Применено");
        if (r && r.ok) loadHl();
      }));
    const modeSel = el("select", {}, el("option", {value: "usb", text: "USB — до 40 модемов"}), el("option", {value: "gw", text: "шлюз — без USB"}));
    const modeCur = el("span", {class: "muted"});
    const rMode = actRow("Режим proxyveth", "смена снимает все модемы и поднимает их в новом режиме — подтверждение номером ВМ", modeCur, modeSel,
      btn("Сменить…", async (e) => {
        const s = srv(id) || {};
        const m = modeSel.value;
        if (m === s.mode) { setRes(rMode.res, "ok", "уже " + modeShort(m)); return; }
        const v = await confirmNumber("Сменить режим proxyveth на ВМ " + id,
          "Режим " + modeShort(s.mode) + " → " + modeShort(m) + ". Все модемы ВМ " + id + " будут сняты и подняты заново.", id, "Сменить режим");
        if (v == null) return;
        await act(e.target, rMode.res, () => run(id, "proxyveth", ["mode", m, "--yes"], {confirm: v}), "Применено");
        refresh();
      }, "danger"));
    const rUp = actRow("Обновить агентов", "код PCS и части на этой ВМ — той же версии, что на хосте", el("span", {class: "muted", text: ""}),
      btn("Обновить", (e) => act(e.target, rUp.res, () => POST(T + "/update", {}), "Применено", rUp.jobs)));

    async function loadHl() {
      const r = await run(id, "hivelink", ["status"]);
      hlState.textContent = !r.ok ? "Ошибка: " + r.error : r.data && r.data.installed === false ? "не установлен"
        : "установлен" + (r.data && r.data.version ? " · " + r.data.version : "");
    }
    async function loadFacts() {
      const r = await GET("/api/servers/" + id);
      if (!r.ok) { facts.textContent = "Ошибка: " + r.error; return; }
      const d = r.data || {}, v = d.versions || {};
      facts.textContent = [d.os, d.cores && d.cores + " ядер", d.ram_gb && "ОЗУ " + d.ram_gb + " ГБ", d.disk_gb && "диск " + d.disk_gb + " ГБ",
        Object.keys(v).length && "версии: " + Object.entries(v).map(([k, x]) => k + " " + (x || "—")).join(", ")].filter(Boolean).join(" · ");
    }
    function update() {
      const s = srv(id) || {};
      mpState.textContent = mpText(s.mpspace);
      modeCur.textContent = "сейчас: " + modeShort(s.mode);
      if (s.mode && s.mode !== shownMode) { shownMode = s.mode; modeSel.value = s.mode === "usb" ? "gw" : "usb"; }
    }
    let shownMode = null;
    const root = el("div", {}, facts, el("div", {class: "acts", style: "margin-top:10px"}, rDns.el, rPw.el, rKey.el, rMp.el, rHl.el, rMode.el, rUp.el));
    let loaded = false;
    return {el: root, update, show() { update(); if (!loaded) { loaded = true; loadHl(); loadFacts(); } }};
  }

  // ── создать ВМ ────────────────────────────────────────────────────────────
  function pageCreate() {
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
      const r = await act(submit, res, () => POST("/api/servers", body), "Запущено");
      if (r && r.ok && r.data && r.data.job != null) location.hash = "#/job/" + encodeURIComponent(r.data.job);
    });
    return {kind: "create", el: el("div", {}, el("h1", {text: "Создать ВМ"}),
      el("p", {class: "muted", text: "Ubuntu 24.04 под ключ: ОС, PCS, proxyveth в выбранном режиме, модемы по таблице, по желанию — mobileproxy.space. Долго — журнал ниже обновляется сам."}),
      form), update() { document.title = "Создать ВМ — PCS"; }};
  }

  function pageJob(jid) {
    return {kind: "job", el: el("div", {}, el("h1", {text: "Задание"}), jobView(jid, {page: true})), update() { document.title = "Задание — PCS"; }};
  }

  function pageHostTerm() {
    const host = (S.ov && S.ov.host) || {};
    const pl = el("div", {class: "plaque host"}, el("div", {}, el("div", {class: "big", text: "Хост " + (host.name || "")}),
      el("div", {class: "line", text: "Proxmox · консоль root на самом хосте — не на ВМ"})));
    const t = termTab("host", true);
    return {kind: "hostterm", el: el("div", {}, pl, t.el), show: () => t.show(), dispose: () => t.dispose(),
      update() { document.title = "Хост — терминал — PCS"; }};
  }

  // ── маршруты и опрос ──────────────────────────────────────────────────────
  function parse() {
    const p = location.hash.replace(/^#\/?/, "").split("/").filter(Boolean).map(decodeURIComponent);
    if (p[0] === "vm" && /^\d+$/.test(p[1] || "")) return {kind: "vm", id: p[1], tab: p[2] || "proxyveth"};
    if (p[0] === "create") return {kind: "create"};
    if (p[0] === "job" && p[1]) return {kind: "job", id: p[1]};
    if (p[0] === "host" && p[1] === "term") return {kind: "hostterm"};
    return {kind: "host"};
  }
  function route() {
    const r = parse();
    if (S.page && S.page.kind === "vm" && r.kind === "vm" && S.page.id === r.id) { S.page.show(r.tab); renderTree(); return; }
    if (S.page && S.page.dispose) S.page.dispose();
    const page = r.kind === "vm" ? pageVm(r.id) : r.kind === "create" ? pageCreate() : r.kind === "job" ? pageJob(r.id)
      : r.kind === "hostterm" ? pageHostTerm() : pageHost();
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
    document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(); });
  }
  boot();
})();

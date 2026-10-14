"use strict";
(function () {
  const f = document.getElementById("login");
  const msg = document.getElementById("msg");
  const btn = document.getElementById("go");
  const lbl = btn.querySelector(".lbl");
  const caps = document.getElementById("caps");
  const eye = document.getElementById("eye");
  let timer = null;

  document.getElementById("where").textContent = "вход в панель · " + location.host;

  // показать / скрыть пароль
  eye.addEventListener("click", () => {
    const show = f.password.type === "password";
    f.password.type = show ? "text" : "password";
    eye.setAttribute("aria-pressed", String(show));
    eye.title = eye.ariaLabel = show ? "скрыть пароль" : "показать пароль";
    f.password.focus();
  });
  // Caps Lock — частая причина «неверного пароля»
  f.password.addEventListener("keyup", (e) => { if (e.getModifierState) caps.hidden = !e.getModifierState("CapsLock"); });
  f.password.addEventListener("blur", () => { caps.hidden = true; });

  function wait(on) {
    btn.disabled = on;
    btn.classList.toggle("busy", on);
    lbl.textContent = on ? "Вход…" : "Войти";
  }
  function fail(text) {
    msg.textContent = text;
    f.classList.remove("shake");
    void f.offsetWidth;                    // перезапуск анимации
    f.classList.add("shake");
  }
  function lock(sec) {
    btn.disabled = true;
    clearInterval(timer);
    const tick = () => {
      if (sec <= 0) { clearInterval(timer); btn.disabled = false; msg.textContent = ""; return; }
      msg.textContent = "Много неудачных входов — подождите " + sec + " с";
      sec -= 1;
    };
    tick();
    timer = setInterval(tick, 1000);
  }

  f.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    msg.textContent = "";
    if (!f.user.value.trim() || !f.password.value) { fail("Нужны логин и пароль"); (f.user.value.trim() ? f.password : f.user).focus(); return; }
    wait(true);
    let r, j = {};
    try {
      r = await fetch("/api/login", {
        method: "POST", credentials: "same-origin",
        headers: {"Content-Type": "application/json", "X-CSRF": "login"},
        body: JSON.stringify({user: f.user.value.trim(), password: f.password.value}),
      });
      try { j = await r.json(); } catch (e) { j = {}; }
    } catch (e) {
      wait(false);
      fail("Панель не отвечает — проверьте сеть или pcs web status");
      return;
    }
    if (r.ok && j.ok) {
      lbl.textContent = "Открываю…";
      f.classList.add("leaving");
      setTimeout(() => location.replace("/" + location.hash), 160);
      return;
    }
    wait(false);
    if (r.status === 429 && j.data && j.data.wait) { lock(j.data.wait); return; }
    fail(j.error || ("Ошибка входа (HTTP " + r.status + ")"));
    f.password.value = "";
    f.password.focus();
  });
})();

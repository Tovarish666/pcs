"use strict";
(function () {
  const f = document.getElementById("login");
  const msg = document.getElementById("msg");
  const btn = f.querySelector("button");
  let timer = null;

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
    btn.disabled = true;
    let r, j = {};
    try {
      r = await fetch("/api/login", {
        method: "POST", credentials: "same-origin",
        headers: {"Content-Type": "application/json", "X-CSRF": "login"},
        body: JSON.stringify({user: f.user.value.trim(), password: f.password.value}),
      });
      try { j = await r.json(); } catch (e) { j = {}; }
    } catch (e) {
      msg.textContent = "Панель не отвечает — проверьте сеть или pcs web status";
      btn.disabled = false;
      return;
    }
    if (r.ok && j.ok) { location.replace("/"); return; }
    if (r.status === 429 && j.data && j.data.wait) { lock(j.data.wait); return; }
    msg.textContent = j.error || ("Ошибка входа (HTTP " + r.status + ")");
    f.password.value = "";
    f.password.focus();
    btn.disabled = false;
  });
})();

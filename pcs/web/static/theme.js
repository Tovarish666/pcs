// Тема до отрисовки (без мигания): auto | light | dark, выбор хранится в браузере.
(function () {
  var t = "auto";
  try { t = localStorage.getItem("pcs-theme") || "auto"; } catch (e) {}
  if (t === "light" || t === "dark") document.documentElement.setAttribute("data-theme", t);
})();

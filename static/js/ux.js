/* Birthday Hub - small UX helpers. Loads after app.js; never changes app logic. */
(() => {
  "use strict";
  const $$ = (s, r = document) => [...r.querySelectorAll(s)];

  /* time-aware greeting on the home header */
  const g = document.querySelector(".hello[data-greet]");
  if (g) { const h = new Date().getHours(); g.textContent = (h < 5 ? "Hello," : h < 12 ? "Good morning," : h < 17 ? "Good afternoon," : h < 22 ? "Good evening," : "Hello,"); }

  /* show / hide password */
  $$('input[type="password"]').forEach(inp => {
    const label = inp.closest("label"); if (!label) return;
    label.classList.add("has-eye");
    const b = document.createElement("button");
    b.type = "button"; b.className = "eye"; b.textContent = "👁"; b.setAttribute("aria-label", "Show password"); b.setAttribute("aria-pressed", "false");
    b.addEventListener("click", () => {
      const show = inp.type === "password"; inp.type = show ? "text" : "password";
      b.setAttribute("aria-pressed", String(show)); b.setAttribute("aria-label", show ? "Hide password" : "Show password"); b.textContent = show ? "🙈" : "👁";
    });
    label.append(b);
  });

  /* stop double submits on normal (full page) forms */
  $$("form").forEach(f => f.addEventListener("submit", e => {
    if (e.defaultPrevented || !f.checkValidity()) return;
    const btn = f.querySelector('button[type="submit"],button:not([type])'); if (!btn) return;
    setTimeout(() => { btn.classList.add("is-busy"); btn.setAttribute("aria-busy", "true"); btn.disabled = true; }, 0);
  }));
  addEventListener("pageshow", e => { if (e.persisted) $$(".is-busy").forEach(b => { b.classList.remove("is-busy"); b.disabled = false; b.removeAttribute("aria-busy"); }); });

  /* connection status snackbar */
  const snack = (msg, err) => { const t = document.getElementById("toast"); if (!t) return; t.textContent = msg; t.classList.toggle("err", !!err); t.classList.add("show"); clearTimeout(snack.t); snack.t = setTimeout(() => t.classList.remove("show"), 3000); };
  addEventListener("offline", () => snack("You are offline. Changes will not send until you reconnect.", true));
  addEventListener("online", () => snack("Back online"));
})();

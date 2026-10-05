// Rotating wallpapers: your images in static/wallpapers (or Mongo), else Radha-Krishna gradients.
(async () => {
  const gradients = [
    "radial-gradient(circle at 20% 15%,#f2a93b 0,transparent 35%),radial-gradient(circle at 80% 80%,#e8749a 0,transparent 40%),linear-gradient(160deg,#14163a,#0e7c86)",
    "radial-gradient(circle at 75% 20%,#ffd27a 0,transparent 30%),linear-gradient(200deg,#0b2a5b,#1b6aa8 55%,#0e7c86)",
    "radial-gradient(circle at 30% 80%,#e8749a 0,transparent 40%),linear-gradient(150deg,#2a0f3d,#14163a 60%,#f2a93b 140%)",
  ];
  let urls = [];
  try { urls = await (await fetch("/api/wallpapers")).json(); } catch {}
  const bgs = urls.length ? urls.map(u => `url(${u}) center/cover`) : gradients;
  const A = document.getElementById("wallA"), B = document.getElementById("wallB");
  let n = 0, front = A;
  const next = () => { const back = front === A ? B : A; back.style.background = bgs[n++ % bgs.length];
    back.classList.add("show"); front.classList.remove("show"); front = back; };
  next(); setInterval(next, 9000);
})();

// College ID: 10 separate boxes -> one hidden input
document.querySelectorAll(".ids").forEach(box => {
  const hidden = Object.assign(document.createElement("input"), { type: "hidden", name: box.dataset.name });
  const cells = Array.from({ length: 10 }, (_, i) => {
    const c = Object.assign(document.createElement("input"), { maxLength: 1, className: "idb", autocomplete: "off" });
    c.setAttribute("aria-label", `College ID character ${i + 1}`); c.setAttribute("autocapitalize", "characters"); return c;
  });
  const sync = () => { hidden.value = cells.map(c => c.value).join(""); };
  cells.forEach((c, i) => {
    c.addEventListener("input", () => { c.value = c.value.toUpperCase().replace(/[^A-Z0-9]/g, ""); sync(); if (c.value && cells[i + 1]) cells[i + 1].focus(); });
    c.addEventListener("keydown", e => { if (e.key === "Backspace" && !c.value && cells[i - 1]) { cells[i - 1].focus(); cells[i - 1].value = ""; sync(); }
      if (e.key === "ArrowLeft" && cells[i - 1]) cells[i - 1].focus(); if (e.key === "ArrowRight" && cells[i + 1]) cells[i + 1].focus(); });
    c.addEventListener("paste", e => { e.preventDefault();
      const t = (e.clipboardData.getData("text") || "").toUpperCase().replace(/[^A-Z0-9]/g, "").slice(0, 10);
      [...t].forEach((ch, k) => cells[k].value = ch); sync(); cells[Math.min(t.length, 9)].focus(); });
  });
  box.append(...cells, hidden);
  box.closest("form").addEventListener("submit", e => { if (hidden.value.length !== 10) { e.preventDefault(); box.classList.add("shake");
    setTimeout(() => box.classList.remove("shake"), 500); cells[hidden.value.length].focus(); } });
});

// Today's-birthday banner: rotating funny wishes + confetti
(() => {
  const bar = document.querySelector(".bday"); if (!bar) return;
  const wishes = JSON.parse(bar.dataset.wishes || "[]"), line = document.getElementById("wishline"); let k = 0;
  const tick = () => { line.classList.remove("in"); setTimeout(() => { line.textContent = wishes[k++ % wishes.length] || ""; line.classList.add("in"); }, 250); };
  tick(); setInterval(tick, 4500);
  if (matchMedia("(prefers-reduced-motion: reduce)").matches) return;
  const box = Object.assign(document.createElement("div"), { className: "confetti", ariaHidden: "true" });
  for (let n = 0; n < 16; n++) { const s = document.createElement("span"); s.textContent = ["🎉", "🎂", "✨", "🦚", "🌸", "🎈"][n % 6];
    s.style.cssText = `left:${Math.random() * 100}%;animation-duration:${6 + Math.random() * 6}s;animation-delay:${-Math.random() * 8}s`; box.append(s); }
  document.body.append(box);
})();

// In-app wish / reply (stored in MongoDB; 50 chars, 10 per day)
async function sendMsg(to, text) {
  try {
    const r = await fetch("/api/messages", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ to, text }) });
    const d = await r.json(); return r.ok ? d : { error: d.error || "Couldn't send." };
  } catch { return { error: "Network problem. Try again." }; }
}


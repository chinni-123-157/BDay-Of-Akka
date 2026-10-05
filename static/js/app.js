<<<<<<< HEAD
(() => {
  "use strict";
  const $ = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => [...r.querySelectorAll(s)];
  const csrf = $('meta[name="csrf-token"]')?.content || "";
  const reduce = matchMedia("(prefers-reduced-motion: reduce)").matches;

  async function api(url, { method = "POST", json, form } = {}) {
    const headers = { "X-CSRFToken": csrf, "X-Requested-With": "fetch", Accept: "application/json" };
    let body;
    if (json !== undefined) { headers["Content-Type"] = "application/json"; body = JSON.stringify(json); }
    else if (form) body = form;
    const r = await fetch(url, { method: method, headers, body, credentials: "same-origin" });
    let data = {};
    try { data = await r.json(); } catch (_) { /* non-JSON */ }
    if (!r.ok) throw new Error(data.error || `Request failed (${r.status})`);
    return data;
  }

  let toastTimer;
  function toast(msg, isErr) {
    const t = $("#toast"); if (!t) return;
    t.textContent = msg; t.classList.toggle("err", !!isErr); t.classList.add("show");
    clearTimeout(toastTimer); toastTimer = setTimeout(() => t.classList.remove("show"), 3200);
  }
  const guard = (fn) => async (...a) => { try { await fn(...a); } catch (e) { toast(e.message, true); } };

  function confetti() {
    if (reduce) return;
    const c = document.createElement("canvas");
    Object.assign(c.style, { position: "fixed", inset: 0, width: "100%", height: "100%", pointerEvents: "none", zIndex: 200 });
    c.width = innerWidth; c.height = innerHeight; document.body.append(c);
    const ctx = c.getContext("2d"), cols = ["#ff6aa2", "#ffd86b", "#8b9cff", "#34d399", "#fff"];
    const ps = Array.from({ length: 120 }, () => ({ x: c.width / 2, y: c.height * .55, vx: (Math.random() - .5) * 14, vy: -Math.random() * 14 - 4,
      s: 4 + Math.random() * 6, r: Math.random() * 6, c: cols[Math.random() * cols.length | 0] }));
    let f = 0;
    (function tick() {
      ctx.clearRect(0, 0, c.width, c.height);
      ps.forEach(p => { p.vy += .35; p.x += p.vx; p.y += p.vy; p.r += .2; ctx.fillStyle = p.c;
        ctx.save(); ctx.translate(p.x, p.y); ctx.rotate(p.r); ctx.fillRect(-p.s / 2, -p.s / 2, p.s, p.s * .6); ctx.restore(); });
      if (++f < 110) requestAnimationFrame(tick); else c.remove();
    })();
  }

  function upload(form, url, onDone) {
    const prog = $("progress", form), btn = $("button[type=submit]", form);
    const xhr = new XMLHttpRequest();
    xhr.open("POST", url);
    xhr.setRequestHeader("X-CSRFToken", csrf); xhr.setRequestHeader("X-Requested-With", "fetch");
    xhr.upload.onprogress = (e) => { if (prog && e.lengthComputable) { prog.hidden = false; prog.value = e.loaded / e.total * 100; } };
    xhr.onloadend = () => {
      btn.disabled = false; if (prog) prog.hidden = true;
      let d = {}; try { d = JSON.parse(xhr.responseText); } catch (_) { /* ignore */ }
      if (xhr.status >= 200 && xhr.status < 300) onDone(d); else toast(d.error || `Upload failed (${xhr.status})`, true);
    };
    btn.disabled = true; xhr.send(new FormData(form));
  }

  /* ---------------- page inits ---------------- */
  const init = {
    signup() {
      const boxes = $$(".box");
      boxes.forEach((b, i) => {
        b.addEventListener("input", () => {
          b.value = b.value.replace(/[^A-Za-z0-9]/g, "").toUpperCase().slice(-1);
          if (b.value && boxes[i + 1]) boxes[i + 1].focus();
        });
        b.addEventListener("keydown", (e) => {
          if (e.key === "Backspace" && !b.value && boxes[i - 1]) { e.preventDefault(); boxes[i - 1].value = ""; boxes[i - 1].focus(); }
          if (e.key === "ArrowLeft" && boxes[i - 1]) boxes[i - 1].focus();
          if (e.key === "ArrowRight" && boxes[i + 1]) boxes[i + 1].focus();
        });
        b.addEventListener("paste", (e) => {
          e.preventDefault();
          const t = (e.clipboardData.getData("text") || "").replace(/[^A-Za-z0-9]/g, "").toUpperCase().slice(0, 10);
          [...t].forEach((ch, k) => { boxes[k].value = ch; });
          boxes[Math.min(t.length, 9)].focus();
        });
        b.addEventListener("focus", () => b.select());
      });
      $("#signupForm").addEventListener("submit", (e) => {
        const empty = boxes.find(b => !b.value);
        if (empty) { e.preventDefault(); $("#formMsg").textContent = "Fill all 10 College ID boxes."; empty.focus(); }
      });
    },

    admin() {
      $$("[data-toggle]").forEach(sw => sw.addEventListener("click", guard(async () => {
        const next = sw.getAttribute("aria-checked") !== "true";
        await api(`/admin/toggle/${sw.dataset.toggle}`, { json: { value: next } });
        sw.setAttribute("aria-checked", String(next));
        toast(next ? "Turned on" : "Turned off");
        if (sw.dataset.toggle === "birthday_on") setTimeout(() => location.reload(), 700);
      })));
      $("#bdUser").addEventListener("change", guard(async (e) => {
        await api("/admin/birthday-user", { json: { user_id: +e.target.value } });
        toast("Birthday person updated"); setTimeout(() => location.reload(), 600);
      }));
      $$("[data-status]").forEach(b => b.addEventListener("click", guard(async () => {
        await api(`/admin/users/${b.dataset.id}/status`, { json: { status: b.dataset.status } });
        toast(b.dataset.status === "approved" ? "Approved" : "Denied"); setTimeout(() => location.reload(), 500);
      })));
      $$("[data-del]").forEach(b => b.addEventListener("click", guard(async () => {
        if (!confirm("Delete this?")) return;
        await api(b.dataset.del); location.reload();
      })));
      $("#annForm").addEventListener("submit", guard(async (e) => {
        e.preventDefault(); await api("/admin/announce", { json: { text: $("#annText").value } }); location.reload();
      }));
      $("#wallForm").addEventListener("submit", (e) => {
        e.preventDefault(); upload(e.target, "/admin/wallpapers", (d) => { toast(`${d.added} wallpaper(s) added`); setTimeout(() => location.reload(), 600); });
      });
      const dlg = $("#resetDlg");
      $$("[data-reset]").forEach(b => b.addEventListener("click", guard(async () => {
        const d = await api(`/admin/users/${b.dataset.reset}/reset`);
        $("#rName").textContent = d.name; $("#rEmail").textContent = d.email; $("#rLink").value = d.link;
        $("#rMail").href = d.mailto; $("#rGmail").href = d.gmail; dlg.showModal();
      })));
      $("#rCopy").addEventListener("click", guard(async () => { await navigator.clipboard.writeText($("#rLink").value); toast("Link copied"); }));
      $("#rClose").addEventListener("click", () => dlg.close());
    },

    dashboard() { wishes(); uploads(); },
    birthday() { wishes(); uploads(); },

    questions() {
      guard(async () => {
        const { questions: qs } = await api("/api/questions", { method: "GET" });
        let i = 0;
        const text = $("#qText"), dlg = $("#popup");
        const show = () => {
          text.textContent = qs[i].q; text.classList.remove("swap"); void text.offsetWidth; text.classList.add("swap");
          $("#qCount").textContent = `${i + 1} / ${qs.length}`; $("#qBar").style.width = `${i / qs.length * 100}%`;
        };
        const answer = (a) => { $("#popupMsg").textContent = qs[i][a]; dlg.showModal(); if (a === "yes") confetti(); };
        $("#yesBtn").onclick = () => answer("yes"); $("#noBtn").onclick = () => answer("no");
        $("#popupNext").onclick = () => {
          dlg.close(); i++;
          if (i < qs.length) show();
          else { $("#qBar").style.width = "100%"; text.classList.add("hidden"); $("#qBtns").classList.add("hidden"); $("#qDone").classList.remove("hidden"); confetti(); }
        };
        show();
      })();
    },

    reels() {
      const box = $("#reels"); if (!box) return;
      const viewer = box.dataset.viewer || "";
      const io = new IntersectionObserver((es) => es.forEach(e => {
        const v = $("video", e.target); if (!v) return;
        if (e.isIntersecting) v.play().catch(() => {}); else v.pause();
      }), { root: box, threshold: .65 });
      $$(".reel", box).forEach(r => io.observe(r));
      $$("video", box).forEach(v => v.addEventListener("click", () => { v.muted = !v.muted; toast(v.muted ? "Muted" : "Sound on"); }));
      const gmail = (r, share) => {
        const name = r.dataset.name, to = r.dataset.email || "";
        const link = `${location.origin}/reels#m-${r.dataset.id}`;
        const body = `Hi ${name},\n\nThank you so much for your lovely ${r.dataset.kind === "video" ? "video" : "photo"} wish! It made my day.\n` +
          (share ? `\n${link}\n` : "") + `\nWith love,\n${viewer}`;
        const u = `https://mail.google.com/mail/?view=cm&fs=1&to=${encodeURIComponent(to)}&su=${encodeURIComponent("Thank you! 🎉")}&body=${encodeURIComponent(body)}`;
        window.open(u, "_blank", "noopener");  // opened synchronously so popup blockers allow it
      };
      $$(".reel", box).forEach(r => {
        $(".like", r).addEventListener("click", (e) => {
          const b = e.currentTarget; gmail(r, false);
          guard(async () => {
            const d = await api(`/api/media/${r.dataset.id}/like`);
            b.classList.toggle("on", d.liked); b.setAttribute("aria-pressed", String(d.liked)); $(".n", b).textContent = d.count;
          })();
        });
        $(".share", r).addEventListener("click", () => gmail(r, true));
      });
      if (location.hash) $(location.hash)?.scrollIntoView();
      const dlg = $("#uploadDlg");
      $("#openUpload")?.addEventListener("click", () => dlg.showModal());
      $("#closeUpload")?.addEventListener("click", () => dlg.close());
      uploads();
    },

    gift() {
      let amt = "";
      const out = $("#amt"), pay = $("#payBtn"), st = $("#payState");
      const render = () => { out.textContent = "₹" + (amt || "0"); pay.disabled = !amt || +amt <= 0; pay.textContent = amt ? `Pay ₹${amt}` : "Pay"; };
      $$(".keys button").forEach(b => b.addEventListener("click", () => {
        const k = b.dataset.k;
        if (k === "C") amt = ""; else if (k === "⌫") amt = amt.slice(0, -1);
        else if (amt.length < 6 && !(amt === "" && k === "0")) amt += k;
        render();
      }));
      pay.addEventListener("click", () => {
        st.classList.remove("hidden");
        st.innerHTML = '<div class="spin" aria-hidden="true"></div><p><b>Processing…</b></p>';
        setTimeout(() => {
          const ref = "DEMO" + Math.random().toString(36).slice(2, 10).toUpperCase();
          st.innerHTML = '<svg class="tick" viewBox="0 0 96 96" aria-hidden="true"><circle cx="48" cy="48" r="44"/><path d="M28 50l14 14 27-30"/></svg>' +
            `<h2>Gift sent</h2><p class="small muted">₹${amt} · Ref ${ref}<br>Demo only, no real payment</p><button class="btn" id="payDone" type="button">Done</button>`;
          confetti();
          $("#payDone").onclick = () => { st.classList.add("hidden"); amt = ""; render(); };
        }, 1700);
      });
      render();
    },
  };

  function uploads() {
    const f = $("#uploadForm"); if (!f) return;
    f.addEventListener("submit", (e) => { e.preventDefault(); upload(f, "/api/media", () => { toast("Posted"); setTimeout(() => location.reload(), 500); }); });
  }

  function wishes() {
    const list = $("#thread"), form = $("#wishForm"); if (!list) return;
    const mode = list.dataset.mode, input = $("#wishText"), count = $("#wishCount");
    const render = (ws) => {
      list.replaceChildren(...ws.map(w => {
        const li = document.createElement("li"); li.className = "wish" + (w.mine ? " mine" : "");
        const b = document.createElement("b"); b.textContent = w.mine ? `You → ${w.to_name || "birthday person"}` : w.from_name;
        const p = document.createElement("span"); p.textContent = w.text;
        const t = document.createElement("time"); t.dateTime = w.at; t.textContent = new Date(w.at).toLocaleString([], { hour: "2-digit", minute: "2-digit", day: "numeric", month: "short" });
        li.append(b, p, t);
        if (mode === "inbox" && !w.mine) {
          const r = document.createElement("button"); r.type = "button"; r.className = "btn sm"; r.textContent = "Reply";
          r.onclick = () => { form.to_id.value = w.from_id; form.parent_id.value = w.id; input.disabled = false; form.querySelector("button[type=submit]").disabled = false;
            input.placeholder = `Reply to ${w.from_name}`; input.focus(); };
          li.append(r);
        }
        return li;
      }));
      if (!ws.length) { const e = document.createElement("li"); e.className = "muted"; e.textContent = "No wishes yet."; list.append(e); }
    };
    const load = guard(async () => { const d = await api("/api/wishes", { method: "GET" }); render(d.wishes); $("#wishLeft").textContent = d.left; });
    input.addEventListener("input", () => { count.textContent = input.value.length; });
    form.addEventListener("submit", guard(async (e) => {
      e.preventDefault();
      const sel = $("#recipient");
      const d = await api("/api/wishes", { json: { text: input.value, to_id: sel ? +sel.value : +form.to_id.value || 0, parent_id: form.parent_id.value || null } });
      input.value = ""; count.textContent = "0"; $("#wishLeft").textContent = d.left; toast("Sent 🎉"); confetti(); await load();
    }));
    load();
  }

  init[document.body.dataset.page]?.();
})();
=======
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

>>>>>>> 2ca04a679866463af5912f6698af40ad97731308

(() => {
  "use strict";
  const $ = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => [...r.querySelectorAll(s)];
  const csrf = $('meta[name="csrf-token"]')?.content || "";
  const reduce = matchMedia("(prefers-reduced-motion: reduce)").matches;

  async function api(url, { method = "POST", json } = {}) {
    const headers = { "X-CSRFToken": csrf, "X-Requested-With": "fetch", Accept: "application/json" };
    let body;
    if (json !== undefined) { headers["Content-Type"] = "application/json"; body = JSON.stringify(json); }
    const r = await fetch(url, { method, headers, body, credentials: "same-origin" });
    let data = {};
    try { data = await r.json(); } catch (_) { /* non-JSON */ }
    if (!r.ok) throw new Error(data.error || `Request failed (${r.status})`);
    return data;
  }

  // multipart upload with progress; resolves with parsed JSON, rejects with Error(message)
  function xhr(url, fd, onProgress) {
    return new Promise((resolve, reject) => {
      const x = new XMLHttpRequest();
      x.open("POST", url);
      x.setRequestHeader("X-CSRFToken", csrf); x.setRequestHeader("X-Requested-With", "fetch");
      if (onProgress) x.upload.onprogress = (e) => e.lengthComputable && onProgress(e.loaded / e.total);
      x.onerror = () => reject(new Error("Network error. Check your connection."));
      x.onload = () => {
        let d = {}; try { d = JSON.parse(x.responseText); } catch (_) { /* ignore */ }
        x.status >= 200 && x.status < 300 ? resolve(d) : reject(new Error(d.error || `Upload failed (${x.status})`));
      };
      x.send(fd);
    });
  }

  let toastTimer;
  function toast(msg, isErr) {
    const t = $("#toast"); if (!t) return;
    t.textContent = msg; t.classList.toggle("err", !!isErr); t.classList.add("show");
    clearTimeout(toastTimer); toastTimer = setTimeout(() => t.classList.remove("show"), 3400);
  }
  const guard = (fn) => async (...a) => { try { await fn(...a); } catch (e) { toast(e.message, true); } };

  function confetti() {
    if (reduce) return;
    const c = document.createElement("canvas");
    Object.assign(c.style, { position: "fixed", inset: 0, width: "100%", height: "100%", pointerEvents: "none", zIndex: 200 });
    c.width = innerWidth; c.height = innerHeight; document.body.append(c);
    const ctx = c.getContext("2d"), cols = ["#25d366", "#00a884", "#d9fdd3", "#ffd86b", "#ff6aa2", "#fff"];
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

  const fmtTimes = () => $$("time[data-t]").forEach(t => {
    t.textContent = new Date(t.dataset.t).toLocaleString([], { hour: "2-digit", minute: "2-digit", day: "numeric", month: "short" });
  });

  /* ---------- shared widgets ---------- */
  function activity() {
    const ul = $("#activity"); if (!ul) return;
    guard(async () => {
      const { items } = await api("/api/activity", { method: "GET" });
      ul.replaceChildren(...items.map(it => {
        const li = document.createElement("li");
        const ic = document.createElement("span"); ic.className = "ic"; ic.setAttribute("aria-hidden", "true");
        ic.textContent = { like: "❤️", comment: "💬", gift: "🎁" }[it.type] || "🔔";
        const a = document.createElement("a"); a.href = it.href;
        const p = document.createElement("span"); p.textContent = it.text;
        const t = document.createElement("time"); t.dateTime = it.at; t.dataset.t = it.at;
        a.append(p, t); li.append(ic, a); return li;
      }));
      if (!items.length) { const li = document.createElement("li"); li.className = "muted"; li.textContent = ul.dataset.empty || "Nothing yet."; ul.append(li); }
      fmtTimes();
    })();
  }

  function uploads() {
    const f = $("#uploadForm"); if (!f) return;
    f.addEventListener("submit", guard(async (e) => {
      e.preventDefault();
      const prog = $("progress", f), btn = $("button[type=submit]", f); btn.disabled = true;
      try {
        await xhr("/api/media", new FormData(f), (v) => { if (prog) { prog.hidden = false; prog.value = v * 100; } });
        toast("Posted"); setTimeout(() => location.reload(), 500);
      } finally { btn.disabled = false; if (prog) prog.hidden = true; }
    }));
  }

  function wishes() {
    const list = $("#thread"), form = $("#wishForm"); if (!list) return;
    const mode = list.dataset.mode, input = $("#wishText"), count = $("#wishCount");
    const render = (ws) => {
      list.replaceChildren(...ws.map(w => {
        const li = document.createElement("li"); li.className = "wish" + (w.mine ? " mine" : "");
        const b = document.createElement("b"); b.textContent = w.mine ? `You → ${w.to_name || "birthday person"}` : w.from_name;
        const p = document.createElement("span"); p.textContent = w.text;
        const t = document.createElement("time"); t.dateTime = w.at; t.dataset.t = w.at;
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
      fmtTimes();
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

  /* ---------- pages ---------- */
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
      // tabs (kept in the URL hash so a reload stays on the same tab)
      const tabs = $$("[role=tab][data-tab]");
      const show = (name) => {
        if (!$(`#tab-${name}`)) name = "overview";
        tabs.forEach(t => t.setAttribute("aria-selected", String(t.dataset.tab === name)));
        $$("[role=tabpanel]").forEach(p => { p.hidden = p.id !== `tab-${name}`; });
        history.replaceState(null, "", `#${name}`);
      };
      tabs.forEach(t => t.addEventListener("click", () => show(t.dataset.tab)));
      show(location.hash.slice(1) || "overview");

      const reload = (ms = 500) => setTimeout(() => location.reload(), ms);
      const setSwitch = (sw, v) => sw.setAttribute("aria-checked", String(v));
      $$("[data-toggle]").forEach(sw => sw.addEventListener("click", guard(async () => {
        const next = sw.getAttribute("aria-checked") !== "true";
        await api(`/admin/toggle/${sw.dataset.toggle}`, { json: { value: next } });
        setSwitch(sw, next); toast(next ? "Turned on" : "Turned off");
      })));
      $$("[data-uptoggle]").forEach(sw => sw.addEventListener("click", guard(async () => {
        const next = sw.getAttribute("aria-checked") !== "true";
        await api(`/admin/users/${sw.dataset.uptoggle}/upload`, { json: { value: next } });
        setSwitch(sw, next); toast(next ? "Upload allowed" : "Upload blocked");
      })));
      const saveBd = guard(async (ids) => {
        const d = await api("/admin/birthday-users", { json: { user_ids: ids } });
        toast(d.count ? `${d.count} birthday ${d.count > 1 ? "people" : "person"} saved` : "Using date of birth"); reload(600);
      });
      $("#bdSave")?.addEventListener("click", () => saveBd($$("#bdChips input:checked").map(i => +i.value)));
      $("#bdAuto")?.addEventListener("click", () => saveBd([]));
      $$("[data-status]").forEach(b => b.addEventListener("click", guard(async () => {
        await api(`/admin/users/${b.dataset.id}/status`, { json: { status: b.dataset.status } });
        toast(b.dataset.status === "approved" ? "Approved" : "Denied"); reload();
      })));
      $$("[data-del]").forEach(b => b.addEventListener("click", guard(async () => {
        if (!confirm(`Delete this ${b.dataset.what || "item"}? It will be removed from the database for everyone.`)) return;
        await api(b.dataset.del); toast("Deleted"); reload(350);
      })));
      $("#annForm").addEventListener("submit", guard(async (e) => {
        e.preventDefault(); await api("/admin/announce", { json: { text: $("#annText").value } }); toast("Posted"); reload(350);
      }));
      $("#wallForm").addEventListener("submit", guard(async (e) => {
        e.preventDefault(); const d = await xhr("/admin/wallpapers", new FormData(e.target)); toast(`${d.added} wallpaper(s) added`); reload(600);
      }));
      $("#mSearch").addEventListener("input", (e) => {
        const v = e.target.value.toLowerCase();
        $$("#mBody tr").forEach(r => { r.hidden = !r.textContent.toLowerCase().includes(v); });
      });
      const dlg = $("#resetDlg");
      $$("[data-reset]").forEach(b => b.addEventListener("click", guard(async () => {
        const d = await api(`/admin/users/${b.dataset.reset}/reset`);
        $("#rName").textContent = d.name; $("#rEmail").textContent = d.email; $("#rLink").value = d.link;
        $("#rMail").href = d.mailto; $("#rGmail").href = d.gmail; dlg.showModal();
      })));
      $("#rCopy").addEventListener("click", guard(async () => { await navigator.clipboard.writeText($("#rLink").value); toast("Link copied"); }));
      $("#rClose").addEventListener("click", () => dlg.close());
      uploads(); activity(); fmtTimes();
    },

    dashboard() { wishes(); uploads(); activity(); fmtTimes(); },
    birthday() { wishes(); uploads(); activity(); fmtTimes(); },

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
        window.open(`https://mail.google.com/mail/?view=cm&fs=1&to=${encodeURIComponent(to)}&su=${encodeURIComponent("Thank you! 🎉")}&body=${encodeURIComponent(body)}`, "_blank", "noopener");
      };
      const cdlg = $("#commentDlg"); let target = null;
      $$(".reel", box).forEach(r => {
        $(".like", r).addEventListener("click", (e) => {
          const b = e.currentTarget; gmail(r, false);
          guard(async () => {
            const d = await api(`/api/media/${r.dataset.id}/like`);
            b.classList.toggle("on", d.liked); b.setAttribute("aria-pressed", String(d.liked)); $(".n", b).textContent = d.count;
          })();
        });
        $(".share", r).addEventListener("click", () => gmail(r, true));
        $(".cmt", r)?.addEventListener("click", () => { target = r; $("#cmtTo").textContent = r.dataset.name; $("#cmtText").value = ""; cdlg.showModal(); $("#cmtText").focus(); });
      });
      $("#cmtClose")?.addEventListener("click", () => cdlg.close());
      $("#commentForm")?.addEventListener("submit", guard(async (e) => {
        e.preventDefault();
        const d = await api(`/api/media/${target.dataset.id}/comment`, { json: { text: $("#cmtText").value } });
        cdlg.close(); toast(`Sent privately to ${d.to || "the poster"}`);
      }));
      if (location.hash) { try { $(location.hash)?.scrollIntoView(); } catch (_) { /* bad hash */ } }
      const dlg = $("#uploadDlg");
      $("#openUpload")?.addEventListener("click", () => dlg.showModal());
      $("#closeUpload")?.addEventListener("click", () => dlg.close());
      uploads();
    },

    gift() {
      fmtTimes();
      const form = $("#giftForm"), tabs = $$("[data-kind]"), err = $("#giftErr");
      let kind = "text", blob = null, stream = null, rec = null, chunks = [], tick = null, secs = 0;
      const live = $("#live"), prev = $("#preview"), btn = $("#recBtn"), info = $("#recInfo");
      const LIMIT = { voice: 120, video: 60 };

      const stopStream = () => { stream?.getTracks().forEach(t => t.stop()); stream = null; live.srcObject = null; live.hidden = true; };
      const reset = () => {
        if (rec && rec.state !== "inactive") { rec.onstop = null; rec.stop(); }
        clearInterval(tick); stopStream(); blob = null; chunks = [];
        prev.replaceChildren(); prev.classList.add("hidden"); btn.classList.remove("on"); btn.setAttribute("aria-label", "Start recording");
        info.textContent = "Tap the red button to record"; err.textContent = ""; $("#giftFile").value = "";
      };
      const setKind = (k) => {
        kind = k; reset();
        tabs.forEach(t => t.setAttribute("aria-selected", String(t.dataset.kind === k)));
        $('[data-panel="text"]').hidden = k !== "text"; $('[data-panel="media"]').hidden = k === "text";
      };
      tabs.forEach(t => t.addEventListener("click", () => setKind(t.dataset.kind)));
      $("#giftText").addEventListener("input", (e) => { $("#giftCount").textContent = e.target.value.length; });

      const preview = () => {
        const el = document.createElement(kind === "video" ? "video" : "audio");
        el.controls = true; el.playsInline = true; el.src = URL.createObjectURL(blob);
        prev.replaceChildren(el); prev.classList.remove("hidden");
      };
      const pick = (types) => types.find(t => window.MediaRecorder && MediaRecorder.isTypeSupported(t)) || "";
      async function start() {
        reset();
        if (!navigator.mediaDevices?.getUserMedia || !window.MediaRecorder) { err.textContent = "Recording isn't supported here. Choose a file instead."; return; }
        try {
          stream = await navigator.mediaDevices.getUserMedia(kind === "video" ? { video: { facingMode: "user" }, audio: true } : { audio: true });
        } catch (_) { err.textContent = "Allow camera/microphone access, or choose a file instead."; return; }
        if (kind === "video") { live.srcObject = stream; live.hidden = false; live.play().catch(() => {}); }
        const mime = pick(kind === "video"
          ? ["video/webm;codecs=vp9,opus", "video/webm;codecs=vp8,opus", "video/webm", "video/mp4"]
          : ["audio/webm;codecs=opus", "audio/webm", "audio/mp4", "audio/ogg;codecs=opus"]);
        rec = new MediaRecorder(stream, mime ? { mimeType: mime } : undefined); chunks = [];
        rec.ondataavailable = (e) => { if (e.data.size) chunks.push(e.data); };
        rec.onstop = () => { clearInterval(tick); btn.classList.remove("on"); blob = new Blob(chunks, { type: rec.mimeType || mime }); stopStream(); preview(); info.textContent = "Recorded. Send it or record again."; };
        rec.start(); secs = 0; btn.classList.add("on"); btn.setAttribute("aria-label", "Stop recording");
        tick = setInterval(() => { secs++; info.textContent = `Recording… ${secs}s / ${LIMIT[kind]}s`; if (secs >= LIMIT[kind]) rec.stop(); }, 1000);
      }
      btn.addEventListener("click", () => { if (rec && rec.state === "recording") rec.stop(); else start(); });
      $("#giftFile").addEventListener("change", (e) => {
        const f = e.target.files[0]; if (!f) return;
        if (rec && rec.state === "recording") { rec.onstop = null; rec.stop(); stopStream(); clearInterval(tick); btn.classList.remove("on"); }
        blob = f; preview(); info.textContent = `Selected: ${f.name}`;
      });

      const overlay = $("#giftState");
      form.addEventListener("submit", async (e) => {
        e.preventDefault(); err.textContent = "";
        const fd = new FormData(); fd.append("kind", kind);
        if (kind === "text") fd.append("text", $("#giftText").value);
        else {
          if (!blob) { err.textContent = "Record or choose a file first."; return; }
          fd.append("text", $("#giftCap").value);
          fd.append("file", blob, blob.name || `gift.${/mp4/.test(blob.type) ? "mp4" : /ogg/.test(blob.type) ? "ogg" : "webm"}`);
        }
        overlay.classList.remove("hidden");
        overlay.innerHTML = '<div class="spin" aria-hidden="true"></div><p><b>Sending your gift…</b></p>';
        try {
          await Promise.all([xhr("/api/gifts", fd), new Promise(r => setTimeout(r, 1300))]);
          overlay.innerHTML = '<svg class="tick" viewBox="0 0 96 96" aria-hidden="true"><circle cx="48" cy="48" r="44"/><path d="M28 50l14 14 27-30"/></svg><h2>Gift sent 🎉</h2><p class="muted small">Everyone can see it on the wall.</p>';
          confetti(); setTimeout(() => location.reload(), 1700);
        } catch (ex) { overlay.classList.add("hidden"); err.textContent = ex.message; toast(ex.message, true); }
      });
    },
  };

  init[document.body.dataset.page]?.();
})();

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


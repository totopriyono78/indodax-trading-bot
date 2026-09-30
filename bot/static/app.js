/* Utilitas bersama untuk semua halaman dashboard. */
const App = {
  csrf: null,
  user: null,

  esc(s) {
    return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  },

  async me() {
    const r = await fetch("/api/me", { cache: "no-store" });
    if (r.status === 401) { location.href = "/login"; throw new Error("login"); }
    const j = await r.json();
    App.csrf = j.csrf; App.user = j.username;
    return j;
  },

  async api(method, url, body) {
    const opt = { method, cache: "no-store", headers: { "X-Dashboard": "1" } };
    if (App.csrf) opt.headers["X-CSRF-Token"] = App.csrf;
    if (body !== undefined) { opt.headers["Content-Type"] = "application/json"; opt.body = JSON.stringify(body); }
    const r = await fetch(url, opt);
    let j = {};
    try { j = await r.json(); } catch (e) { j = { error: "respon tidak valid (HTTP " + r.status + ")" }; }
    if (r.status === 401 && j.login) { location.href = "/login"; throw new Error(j.error); }
    if (!r.ok) throw new Error(j.error || ("HTTP " + r.status));
    if (j.login) setTimeout(() => (location.href = "/login"), 1200);
    return j;
  },

  toast(msg, isErr) {
    let t = document.getElementById("toast");
    if (!t) { t = document.createElement("div"); t.id = "toast"; t.className = "toast"; document.body.appendChild(t); }
    t.textContent = msg;
    t.style.background = isErr ? "var(--critical)" : "";
    t.style.color = isErr ? "#fff" : "";
    t.style.display = "block";
    clearTimeout(App._t);
    App._t = setTimeout(() => (t.style.display = "none"), isErr ? 7000 : 4500);
  },

  async nav(active) {
    const el = document.getElementById("topnav");
    await App.me();
    el.innerHTML = `
      <a href="/" class="${active === "dash" ? "active" : ""}">Dashboard</a>
      <a href="/analysis" class="${active === "analysis" ? "active" : ""}">Analisis</a>
      <a href="/optimizer" class="${active === "optimizer" ? "active" : ""}">Optimasi</a>
      <a href="/settings" class="${active === "settings" ? "active" : ""}">Pengaturan</a>
      <span class="spacer"></span>
      <span class="who">${App.esc(App.user)}</span>
      <button id="logout">Keluar</button>`;
    document.getElementById("logout").onclick = async () => {
      try { await App.api("POST", "/api/logout", {}); } catch (e) {}
      location.href = "/login";
    };
  },

  /** Angka dari input: terima koma (2,5) maupun titik (2.5). Kosong -> null. */
  num(v) {
    v = String(v ?? "").trim().replace(/\s/g, "");
    if (v === "") return null;
    if (/^[1-9]\d{0,2}(\.\d{3})+(,\d+)?$/.test(v)) v = v.replace(/\./g, ""); // 1.000.000 (bukan 0.100)
    return v.replace(",", ".");
  },

  fmtNum(v) {
    if (v === null || v === undefined || v === "") return "";
    return String(v).replace(".", ",");
  },
};

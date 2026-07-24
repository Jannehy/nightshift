/* Nightshift – shared base: theme (shift change), i18n, fetch helper */

const NS = {
  lang: localStorage.getItem("ns-lang") || document.documentElement.lang || "de",
  theme: localStorage.getItem("ns-theme") || "auto",
  dict: {},

  applyTheme() {
    let t = this.theme;
    if (t === "auto") {
      t = window.matchMedia("(prefers-color-scheme: light)").matches
        ? "light" : "dark";
    }
    document.documentElement.dataset.theme = t;
  },

  toggleTheme() {
    const current = document.documentElement.dataset.theme;
    this.theme = current === "light" ? "dark" : "light";
    localStorage.setItem("ns-theme", this.theme);
    this.applyTheme();
  },

  async loadLang(lang) {
    this.lang = lang;
    localStorage.setItem("ns-lang", lang);
    document.documentElement.lang = lang;
    try {
      const r = await fetch(`/static/i18n/${lang}.json`);
      this.dict = await r.json();
    } catch (e) {
      this.dict = {};
    }
    this.translatePage();
    document.dispatchEvent(new CustomEvent("ns:lang"));
  },

  t(key) {
    return this.dict[key] || key;
  },

  translatePage() {
    document.querySelectorAll("[data-i18n]").forEach((el) => {
      el.textContent = this.t(el.dataset.i18n);
    });
    document.querySelectorAll("[data-i18n-ph]").forEach((el) => {
      el.placeholder = this.t(el.dataset.i18nPh);
    });
    document.querySelectorAll("[data-i18n-title]").forEach((el) => {
      el.title = this.t(el.dataset.i18nTitle);
    });
  },

  async api(path, opts = {}) {
    const r = await fetch(path, {
      headers: { "Content-Type": "application/json" },
      ...opts,
      body: opts.body ? JSON.stringify(opts.body) : undefined,
    });
    let data = {};
    try { data = await r.json(); } catch (e) { /* empty body */ }
    if (!r.ok) throw new Error(data.error || `HTTP ${r.status}`);
    return data;
  },
};

NS.applyTheme();

document.addEventListener("DOMContentLoaded", () => {
  NS.loadLang(NS.lang);

  const toggle = document.getElementById("shift-toggle");
  if (toggle) toggle.addEventListener("click", () => NS.toggleTheme());

  const langSel = document.getElementById("lang-select");
  if (langSel) {
    langSel.value = NS.lang;
    langSel.addEventListener("change", (e) => NS.loadLang(e.target.value));
  }

  const logout = document.getElementById("logout-btn");
  if (logout) {
    logout.addEventListener("click", async () => {
      await NS.api("/api/logout", { method: "POST" });
      location.href = "/login";
    });
  }
});

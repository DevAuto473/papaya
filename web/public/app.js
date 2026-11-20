"use strict";
/* ببايه PWA — talks to the bot through /api/bot (Vercel) and keeps working on stored data while it sleeps. */

const VERSION = "1.1.0";
const $app = document.getElementById("app");

// ---------- tiny helpers -------------------------------------------------------------------------
const store = {
  get(k) { try { return localStorage.getItem(k); } catch { return null; } },
  set(k, v) { try { v == null ? localStorage.removeItem(k) : localStorage.setItem(k, v); } catch { /* private mode */ } },
  json(k, d) { try { return JSON.parse(store.get(k)) ?? d; } catch { return d; } },
};

const S = {
  token: store.get("babaya.token"),
  admin: store.get("babaya.admin") === "1",
  as: store.get("babaya.as"),
  state: null,
  asleep: false,
  adminData: null,
  newCode: null,
  timers: [],
};
S.state = store.json(stateKey(), null);

function stateKey() { return "babaya.state:" + (S.as || "me"); }

function h(tag, props, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(props || {})) {
    if (v == null || v === false) continue;
    if (k === "class") el.className = v;
    else if (k === "text") el.textContent = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "checked" || k === "value" || k === "disabled" || k === "selected") el[k] = v;
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const c of kids.flat(Infinity)) {
    if (c == null || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}

const SVG = {
  clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
  check: '<path d="M5 12l5 5 9-10"/>',
  back: '<path d="M9 6l6 6-6 6"/>',
  bell: '<path d="M6 8a6 6 0 0 1 12 0c0 7 3 9 3 9H3s3-2 3-9"/><path d="M10 21a2 2 0 0 0 4 0"/>',
  chat: '<path d="M4 5h16v11H9l-5 4z"/>',
  send: '<path d="M20 12H5"/><path d="M11 6l-6 6 6 6"/>',
  warn: '<path d="M12 3l10 18H2z"/><path d="M12 10v5"/><path d="M12 18v.5"/>',
  refresh: '<path d="M20 11a8 8 0 1 0-2 5.3"/><path d="M20 4v7h-7"/>',
};
function icon(name, size = 20) {
  const s = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  s.setAttribute("width", size); s.setAttribute("height", size); s.setAttribute("viewBox", "0 0 24 24");
  s.setAttribute("fill", "none"); s.setAttribute("stroke", "currentColor"); s.setAttribute("stroke-width", "2.6");
  s.setAttribute("aria-hidden", "true");
  s.innerHTML = SVG[name];
  return s;
}

function toast(text) {
  document.querySelectorAll(".toast").forEach((t) => t.remove());
  const t = h("div", { class: "toast", role: "status", text });
  document.body.append(t);
  setTimeout(() => t.remove(), 3200);
}

function clearTimers() { S.timers.forEach(clearInterval); S.timers = []; }
function every(ms, fn) { S.timers.push(setInterval(() => { if (!document.hidden) fn(); }, ms)); }

const isIOS = /iphone|ipad|ipod/i.test(navigator.userAgent) || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
const standalone = () => window.matchMedia("(display-mode: standalone)").matches || navigator.standalone === true;

// ---------- API ----------------------------------------------------------------------------------------
class Asleep extends Error {}
class ApiError extends Error { constructor(msg, code) { super(msg); this.code = code; } }

async function api(path, body, query) {
  const q = new URLSearchParams({ p: "/api/" + path, ...(query || {}) });
  const headers = { "Content-Type": "application/json" };
  if (S.token) headers.Authorization = "Bearer " + S.token;
  if (S.admin && S.as) headers["X-As"] = S.as;
  let r;
  try {
    r = await fetch("/api/bot?" + q, { method: body ? "POST" : "GET", headers, body: body ? JSON.stringify(body) : undefined });
  } catch {
    throw new ApiError("ما فيه إنترنت", 0);
  }
  let data = {};
  try { data = await r.json(); } catch { /* empty */ }
  if (r.status === 503 && data.asleep) { S.asleep = true; throw new Asleep("asleep"); }
  S.asleep = false;
  if (r.status === 401 && path !== "login") { logout(true); throw new ApiError("login", 401); }
  if (!r.ok) throw new ApiError(data.error || "صار خطأ، جرّب مرة ثانية", r.status);
  return data;
}

async function queue(action, id) {
  const r = await fetch("/api/queue", {
    method: "POST",
    headers: { "Content-Type": "application/json", Authorization: "Bearer " + S.token },
    body: JSON.stringify({ action, ...(id ? { id } : {}), ...(S.admin && S.as ? { as: S.as } : {}) }),
  }).catch(() => null);
  if (r && r.ok) {
    const waiting = store.json("babaya.waiting", []);
    waiting.push({ action, id: id || null, at: Date.now() });
    store.set("babaya.waiting", JSON.stringify(waiting.slice(-10)));
    return true;
  }
  return false;
}

async function loadState() {
  try {
    const st = await api("state");
    S.state = st;
    if (st.admin && !S.as && st.devices?.length) { S.as = st.device; store.set("babaya.as", S.as); }
    store.set(stateKey(), JSON.stringify(st));
    store.set("babaya.waiting", null);            // it is awake: queued requests were taken
    return st;
  } catch (e) {
    if (e instanceof Asleep) return S.state;
    throw e;
  }
}

function logout(silent) {
  if (!silent && S.token) api("logout", {}).catch(() => {});
  S.token = null; S.admin = false; S.as = null; S.state = null;
  ["babaya.token", "babaya.admin", "babaya.as", "babaya.waiting"].forEach((k) => store.set(k, null));
  Object.keys(localStorage).filter((k) => k.startsWith("babaya.state:")).forEach((k) => store.set(k, null));
  location.hash = "#/login";
}

// ---------- Babaya -------------------------------------------------------------------------------------
const KIND = { assignment: "واجب", exam: "اختبار" };

function mood(st) {
  if (S.asleep) return "sleeping";
  if (!st) return "thinking";
  if (st.error) return "sad";
  if (st.items.some((i) => i.urgent && (i.can_solve || i.opened))) return "angry";
  if (st.items.some((i) => i.solution === "ready")) return "idea";
  if (!st.items.length) return "happy";
  return "normal";
}

function bubbleText(st, m) {
  if (m === "sleeping") return "البوت نايم 💤 اطلب اللي تبغاه، وأول ما يصحى ينفّذه ويبلغك.";
  if (m === "sad") return "ما قدرت أدخل مدرستي آخر مرة. الأدمن بيصلحها.";
  if (m === "angry") {
    const u = st.items.find((i) => i.urgent);
    return `${u.subject || u.title} باقي له ${u.left}! يلا نحله`;
  }
  if (m === "idea") return "فيه حل جاهز ينتظرك 💡";
  if (m === "happy") return "ما عليك شي! استانس 🎉";
  return "هلا! أنا أراقب واجباتك واختباراتك.";
}

function scene({ m, cls = "", bubble, top = true, back = false, extra }) {
  return h("header", { class: `scene ${cls} ${m === "sleeping" ? "night" : ""}` },
    h("img", { class: "bird", src: `/img/babaya-${m}.webp`, alt: "ببايه" }),
    back && h("a", { class: "b3d icon back", href: "#/", "aria-label": "رجوع للرئيسية" }, icon("back", 22)),
    top && h("div", { class: "top" },
      h("h1", { class: "logo" }, "ببايه"),
      statusChip()),
    extra,
    bubble && h("div", { class: "bubble" }, bubble));
}

function statusChip() {
  const name = S.state?.device || S.as || "";
  return h("button", {
    class: "status" + (S.asleep ? " asleep" : ""), type: "button",
    "aria-label": S.asleep ? "البوت نايم" : "حدّث من مدرستي",
    onclick: async () => {
      if (S.asleep) return toast("البوت نايم، أول ما يصحى يحدّث");
      try { await api("refresh", {}); toast("أفحص مدرستي الحين… 🔄"); setTimeout(route, 9000); } catch (e) { toast(e.message); }
    },
  }, h("span", { class: "dot", "aria-hidden": "true" }), `${name} · ${S.asleep ? "نايم" : "صاحي"}`);
}

function bar(title, color, right) {
  return h("div", { class: `bar ${color}` }, h("h2", {}, title),
    right || h("div", { class: "winbtns", "aria-hidden": "true" }, h("i"), h("i")));
}

// ---------- actions ------------------------------------------------------------------------------------------
async function startSolve(item) {
  if (S.asleep) {
    const ok = await queue("solve", item.id);
    return toast(ok ? "انحفظ طلبك 📝 أول ما يصحى ببايه يحله ويرسل لك إشعار" : "البوت نايم وما قدرت أحفظ طلبك");
  }
  try {
    if (item.solution !== "ready") await api("solve", { id: item.id });
    location.hash = `#/solve/${item.kind}/${item.id}`;
  } catch (e) {
    if (e instanceof Asleep) return startSolve(item);
    toast(e.message);
  }
}

function itemName(i) { return i.subject ? (i.title && i.title !== i.subject ? `${i.subject} — ${i.title}` : i.subject) : i.title; }

function itemAction(i) {
  const exam = i.kind === "exam";
  if (i.solution === "ready") {
    return h("a", { class: "b3d lime", href: `#/solve/${i.kind}/${i.id}` }, "الحل جاهز");
  }
  if (i.can_solve) {
    return h("button", { class: `b3d ${exam ? "lilac" : "orange"}`, type: "button", onclick: () => startSolve(i) }, "حل");
  }
  if (i.external) return h("span", { class: "chip warn" }, "خارج المنصة");
  if (!i.opened) return h("span", { class: "chip" }, "ما انفتح");
  if (i.url) return h("a", { class: "b3d small", href: i.url, target: "_blank", rel: "noopener" }, exam ? "تحله بنفسك" : "افتحه");
  return null;
}

function itemRow(i) {
  const exam = i.kind === "exam";
  return h("div", { class: "row" },
    h("div", { class: "grow" },
      h("div", { class: "t" }, itemName(i)),
      h("div", { class: "s" + (i.urgent ? " hot" : "") }, `باقي ${i.left}`)),
    itemAction(i),
    exam && i.opened && !S.asleep && h("button", {
      class: "b3d small", type: "button", "aria-label": "سلّمته",
      onclick: async () => {
        try { await api("done", { kind: i.kind, id: i.id }); toast("تمام ✅"); route(); } catch (e) { toast(e.message); }
      },
    }, icon("check", 16)));
}

// ---------- screens -----------------------------------------------------------------------------------------
function loginScreen() {
  clearTimers();
  const cells = Array.from({ length: 6 }, (_, n) => h("input", {
    "aria-label": `الخانة ${n + 1}`, inputmode: "text", autocomplete: n ? "off" : "one-time-code", maxlength: "6",
    autocapitalize: "characters", spellcheck: "false",
  }));
  const err = h("p", { class: "err", role: "alert" });
  const go = h("button", { class: "b3d big orange", type: "submit" }, "دخول");
  const fill = (text, from = 0) => {
    const chars = text.toUpperCase().replace(/[^A-Z0-9]/g, "").split("");
    chars.forEach((c, k) => { if (cells[from + k]) cells[from + k].value = c; });
    const next = Math.min(from + chars.length, 5);
    cells[next].focus();
    if (cells.every((x) => x.value)) form.requestSubmit();     // pasted / autofilled the whole code
  };
  cells.forEach((c, n) => {
    c.addEventListener("input", () => {
      const v = c.value;
      if (v.length > 1) { c.value = ""; fill(v, n); return; }
      c.value = v.toUpperCase().replace(/[^A-Z0-9]/g, "");
      if (c.value && n < 5) cells[n + 1].focus();
      if (cells.every((x) => x.value)) form.requestSubmit();
    });
    c.addEventListener("keydown", (e) => { if (e.key === "Backspace" && !c.value && n) cells[n - 1].focus(); });
  });
  const form = h("form", { class: "card login-form", onsubmit: async (e) => {
    e.preventDefault();
    const code = cells.map((c) => c.value).join("");
    if (code.length !== 6) { err.textContent = "الكود 6 خانات"; return; }
    go.disabled = true; err.textContent = "";
    try {
      const out = await api("login", { code });
      S.token = out.token; S.admin = !!out.admin;
      store.set("babaya.token", out.token); store.set("babaya.admin", out.admin ? "1" : null);
      location.hash = "#/";
    } catch (e2) {
      err.textContent = e2 instanceof Asleep ? "البوت نايم الحين، ما أقدر أتحقق من الكود. جرّب بعدين." : e2.message;
      cells.forEach((c) => (c.value = "")); cells[0].focus();
    } finally { go.disabled = false; }
  } },
  bar("هلا! ادخل كودك", "green", h("span")),
  h("div", { class: "body" },
    h("p", { text: "الكود عطاك إياه الأدمن. تدخله مرة وحدة، وبعدها الجوال يتذكرك." }),
    h("fieldset", { class: "vh-legend" }, h("legend", { class: "vh" }, "كود الدخول"), h("div", { class: "cells" }, cells)),
    err, go));
  $app.replaceChildren(
    scene({ m: "happy", cls: "", top: false, extra: h("div", { class: "top" }, h("h1", { class: "logo" }, "ببايه"), h("span")) }),
    form,
    isIOS && !standalone() && h("div", { class: "tip" }, icon("bell", 22),
      h("span", {}, h("b", {}, "آيفون؟ "), "عشان توصلك الإشعارات: زر المشاركة ← «إضافة إلى الشاشة الرئيسية»، وافتح ببايه من هناك.")));
  setTimeout(() => cells[0].focus(), 50);
}

function homeView(st) {
  const m = mood(st);
  const kids = [scene({ m, bubble: st ? bubbleText(st, m) : "لحظة… أجيب واجباتك" })];
  const main = h("main", { class: "page" });
  kids.push(main);
  if (S.asleep) main.append(sleepingCard());
  if (!st) { main.append(h("div", { class: "loading" }, h("span", { class: "dots" }, h("i"), h("i"), h("i")))); return kids; }

  const hw = st.items.filter((i) => i.kind === "assignment");
  const ex = st.items.filter((i) => i.kind === "exam");
  main.append(h("div", { class: "counts" },
    h("div", { class: "count hw" }, h("b", {}, hw.length), h("span", {}, "واجبات باقية")),
    h("div", { class: "count ex" }, h("b", {}, ex.length), h("span", {}, "اختبارات باقية"))));
  main.append(h("div", { class: "counts" },
    h("a", { class: "b3d big", href: "#/chat" }, icon("chat", 22), "كلّم ببايه"),
    h("a", { class: "b3d big", href: "#/cmd" }, h("span", { "aria-hidden": "true" }, ">_"), "الأوامر")));

  const urgent = st.items.find((i) => i.urgent && (i.can_solve || i.solution === "ready"));
  if (urgent) {
    main.append(h("section", { class: "card" }, bar("مستعجل", "red"),
      h("div", { class: "body" },
        h("div", {}, h("div", { class: "urgent-title" }, itemName(urgent)),
          h("div", { class: "urgent-left" }, icon("clock", 18), `باقي ${urgent.left}`)),
        urgent.solution === "ready"
          ? h("a", { class: "b3d big lime", href: `#/solve/${urgent.kind}/${urgent.id}` }, "شوف الحل")
          : h("button", { class: "b3d big lime", type: "button", onclick: () => startSolve(urgent) }, "حل الحين"))));
  }

  const canAll = st.solver.homework && hw.some((i) => i.can_solve);
  main.append(h("section", { class: "card" },
    bar("الواجبات", "green", canAll && h("button", { class: "b3d small lime", type: "button", onclick: async () => {
      if (S.asleep) return toast("البوت نايم الحين");
      try { await api("solveall", {}); toast("أحل كل الواجبات… أرسل لك لما أخلص ⏳"); } catch (e) { toast(e.message); }
    } }, "حل الكل")),
    hw.length ? h("div", { class: "rows" }, hw.map(itemRow)) : h("div", { class: "empty" }, "ما عندك واجبات 🎉")));

  main.append(h("section", { class: "card exam" }, bar("الاختبارات", "purple"),
    ex.length ? h("div", { class: "rows" }, ex.map(itemRow)) : h("div", { class: "empty" }, "ما عندك اختبارات 🎉")));

  main.append(h("section", { class: "card" },
    bar("الإعدادات", "blue", h("a", { class: "b3d small", href: "#/settings" }, "كل الإعدادات")),
    h("div", { class: "setlist" }, pushSetting())));

  if (st.admin) main.append(adminCard(st));
  return kids;
}

function sleepingCard() {
  const waiting = store.json("babaya.waiting", []);
  return h("section", { class: "card" }, bar("البوت نايم", "night", h("span")),
    h("div", { class: "body" },
      h("p", { text: "ببايه طافي الحين. اللي تشوفه تحت هو آخر شي شافه. اطلب اللي تبغاه، وأول ما يصحى ينفّذه ويرسل لك إشعار." }),
      h("button", { class: "b3d big lime", type: "button", onclick: async () => {
        const ok = await queue("notify");
        toast(ok ? "أبشر، أول ما يصحى يوصلك إشعار 🔔" : "ما قدرت أحفظ الطلب، جرّب بعدين");
        route();
      } }, icon("bell", 22), "نبّهني لما يصحى"),
      waiting.map((w) => h("div", { class: "chip ok" }, icon("check", 14),
        w.action === "notify" ? "بالانتظار: تنبيه لما يصحى" : `بالانتظار: ${w.action === "solve" ? "حل" : w.action} ${itemTitle(w.id)}`))));
}

function itemTitle(id) { const i = S.state?.items.find((x) => x.id === id); return i ? `«${i.subject || i.title}»` : ""; }

async function homeScreen() {
  clearTimers();
  if (!S.token) { location.hash = "#/login"; return; }
  $app.replaceChildren(...homeView(S.state));
  try {
    await loadState();
    if (S.state?.admin && !S.adminData) S.adminData = await api("admin").catch(() => null);
  } catch (e) { if (e.code !== 401) toast(e.message); }
  if (location.hash.replace(/^#\/?/, "") === "") $app.replaceChildren(...homeView(S.state));
  every(60000, async () => { await loadState().catch(() => {}); if (!location.hash.replace(/^#\/?/, "")) $app.replaceChildren(...homeView(S.state)); });
}

// ---------- admin -------------------------------------------------------------------------------------------------
function adminCard(st) {
  const a = S.adminData;
  const reload = async (p) => { try { S.adminData = await p; $app.replaceChildren(...homeView(S.state)); } catch (e) { toast(e.message); } };
  const sel = h("select", { "aria-label": "اختر الجهاز", onchange: (e) => {
    S.as = e.target.value; store.set("babaya.as", S.as); S.state = store.json(stateKey(), null); route();
  } }, (st.devices || []).map((d) => h("option", { value: d, selected: d === st.device }, d)));
  const body = h("div", { class: "body" }, h("label", { class: "set" }, "أشوف جهاز:", sel));
  if (S.newCode) {
    body.append(h("div", {}, h("div", { class: "muted" }, `كود ${S.newCode.name} الجديد (يظهر مرة وحدة):`),
      h("div", { class: "codebox" }, S.newCode.code)));
  }
  if (!a) {
    body.append(h("button", { class: "b3d small", type: "button", onclick: () => reload(api("admin")) }, "حمّل لوحة الأدمن"));
  } else {
    a.devices.forEach((d) => {
      const hours = h("select", { "aria-label": `مدة صلاحية ${d.name}` },
        [["0", "دائم"], ["1", "ساعة"], ["3", "3 ساعات"], ["24", "يوم"]].map(([v, t]) => h("option", { value: v }, t)));
      body.append(h("div", { class: "dev" + (d.linked || d.sessions ? "" : " off") },
        h("div", {}, h("b", {}, d.name), h("div", { class: "muted" },
          `${d.homework ? "واجبات ✓" : "واجبات ✗"} · ${d.granted ? "اختبارات ✓" : "اختبارات ✗"}${d.until ? " (مؤقت)" : ""} · ${d.sessions} جوال`,
          d.error ? ` · ⚠ ${d.error}` : "")),
        h("div", { class: "acts" },
          h("button", { class: "b3d small lime", type: "button", onclick: async () => {
            try { S.newCode = await api("admin/code", { name: d.name }); $app.replaceChildren(...homeView(S.state)); } catch (e) { toast(e.message); }
          } }, "ولّد كود"),
          d.granted
            ? h("button", { class: "b3d small", type: "button", onclick: () => reload(api("admin/solver", { name: d.name, on: false })) }, "اسحب الصلاحية")
            : [hours, h("button", { class: "b3d small", type: "button", onclick: () => reload(api("admin/solver", { name: d.name, on: true, hours: +hours.value })) }, "امنح الاختبارات")],
          d.sessions ? h("button", { class: "b3d small", type: "button", onclick: () => reload(api("admin/logout", { name: d.name })) }, "اطلعه") : null)));
    });
    body.append(h("div", { class: "counts" },
      h("button", { class: `b3d ${a.everyone ? "lime" : ""}`, type: "button",
        onclick: () => reload(api("admin/everyone", { on: !a.everyone })) }, `الحل للجميع: ${a.everyone ? "شغّال" : "مطفي"}`),
      h("div", { class: "b3d" }, `الميزانية ${a.spend}$ / ${a.budget}$`)));
    body.append(h("button", { class: "b3d small", type: "button", onclick: async () => {
      try { S.newCode = await api("admin/code", { name: "@admin" }); $app.replaceChildren(...homeView(S.state)); } catch (e) { toast(e.message); }
    } }, "كود جديد للأدمن"));
    if (a.pending) body.append(h("div", { class: "muted" }, `${a.pending} طلب ربط ينتظرك في تيليجرام`));
    if (a.logs?.length) body.append(h("div", { class: "muted" }, a.logs.slice(0, 5).map((l) => h("div", {}, `${(l.ts || "").slice(11, 16)} ${l.account || ""} ${l.text}`))));
  }
  return h("section", { class: "card admin" }, bar("الأدمن", "", h("span", { class: "muted" }, "يظهر لك بس")), body);
}

// ---------- solve ------------------------------------------------------------------------------------------------
async function solveScreen(kind, id) {
  clearTimers();
  const exam = kind === "exam";
  let item = S.state?.items.find((i) => i.id === id) || { id, kind, title: "", subject: "", left: "" };
  let data = null, started = false, submitting = false;

  const lastBotText = async () => {
    try {
      const msgs = (await api("inbox", null, { after: 0 })).messages.filter((m) => m.who === "bot" && !m.progress);
      return msgs.length ? msgs[msgs.length - 1].text : "";
    } catch { return ""; }
  };

  const draw = (err) => {
    const sol = data?.solution, job = data?.job;
    let m = "thinking", bubble = "دقيقة… أحل لك";
    if (S.asleep) { m = "sleeping"; bubble = "البوت نايم 💤"; }
    else if (sol?.status === "submitted") { m = "happy"; bubble = "انسلّم! برافو عليك 🎉"; }
    else if (err) { m = "sad"; bubble = "ما زبطت 😔"; }
    else if (sol && sol.status !== "dropped" && job !== "working") { m = "idea"; bubble = exam ? "جاهز! راجعها قبل التسليم" : "لقيتها! هذي الإجابات"; }
    else if (submitting) { bubble = "أسلّم الإجابات…"; }
    const main = h("main", { class: "page" + (exam ? " exam" : "") },
      h("div", {}, h("h1", { class: "title" }, itemName(item) || "الحل"),
        h("div", { class: "badges" },
          exam && h("span", { class: "chip lilac" }, "اختبار"),
          item.left && h("span", { class: "chip" }, icon("clock", 14), `باقي ${item.left}`),
          sol && h("span", { class: "chip " + (sol.all_agree ? "ok" : "warn") },
            sol.all_agree ? [icon("check", 14), "النماذج متفقة على الكل"] : `${sol.answers.filter((a) => !a.sure).length} راجعها`),
          sol && h("span", { class: "chip" }, `${sol.answers.length} أسئلة`))));
    if (err) {
      main.append(h("div", { class: "note" }, icon("warn", 20), err));
      main.append(h("button", { class: "b3d big orange", type: "button", onclick: () => { data = null; started = false; begin(); } }, "جرّب مرة ثانية"));
    } else if (sol && sol.status !== "dropped" && (job !== "working" || submitting)) {
      main.append(h("ol", { class: "card answers" }, sol.answers.map((a) => h("li", {},
        h("span", { class: "num" }, a.n),
        h("div", { class: "grow" }, h("div", { class: "q" }, a.q), h("div", { class: "a" + (a.sure ? "" : " unsure") }, a.answer))))));
      if (sol.status === "submitted") {
        main.append(h("a", { class: "b3d big lime", href: "#/" }, "رجوع للرئيسية"));
      } else {
        if (exam) main.append(h("div", { class: "note" }, icon("warn", 20), "التسليم نهائي وما ينفع ترجع تعدّل."));
        main.append(h("button", { class: `b3d big ${exam ? "lilac" : "lime"}`, type: "button", disabled: submitting, onclick: submit },
          submitting ? "أسلّم…" : exam ? "سلّم الاختبار" : "سلّم الحل"));
        if (item.url) main.append(h("a", { class: "b3d big", href: item.url, target: "_blank", rel: "noopener" }, "افتحه في مدرستي"));
        main.append(h("button", { class: "b3d small", type: "button", onclick: async () => {
          try { await api("drop", { id }); toast("تجاهلت الحل"); location.hash = "#/"; } catch (e) { toast(e.message); }
        } }, "تجاهل الحل"));
      }
    } else if (!started && !sol) {
      main.append(h("button", { class: `b3d big ${exam ? "lilac" : "orange"}`, type: "button", onclick: begin }, "ابدأ الحل"));
    } else {
      main.append(h("div", { class: "note lime" }, h("span", { class: "dots" }, h("i"), h("i"), h("i")), "ببايه يقرأ الأسئلة ويحلها… تقريباً دقيقة"));
    }
    $app.replaceChildren(scene({ m, cls: "short", bubble, top: false, back: true }), main);
  };

  const poll = async () => {
    try {
      data = await api("solution", null, { id });
      const job = data.job;
      if (job === "working") return draw();
      clearTimers();
      if (submitting) {
        submitting = false;
        if (data.solution?.status !== "submitted") return draw((await lastBotText()) || "ما انسلّم. افتحه وسلّمه بنفسك.");
        loadState().catch(() => {});
        return draw();
      }
      if (!data.solution && started) return draw((await lastBotText()) || "ما قدرت أحله الحين.");
      draw();
    } catch (e) {
      clearTimers();
      draw(e instanceof Asleep ? "البوت نام قبل ما يخلص. أول ما يصحى كمّل من هنا." : e.message);
    }
  };

  async function begin() {
    started = true; draw();
    try { await api("solve", { id }); } catch (e) { return draw(e instanceof Asleep ? "البوت نايم" : e.message); }
    every(1500, poll);
  }

  async function submit() {
    submitting = true; draw();
    try { await api("submit", { id }); } catch (e) { submitting = false; return draw(e.message); }
    every(1500, poll);
  }

  draw();
  await poll();
  if (data?.job === "working") { started = true; draw(); every(1500, poll); }
}

// ---------- settings --------------------------------------------------------------------------------------------
function pushSetting() {
  const sw = h("input", { class: "sw", type: "checkbox", "aria-label": "الإشعارات على هذا الجوال" });
  const label = h("label", { class: "set" }, h("span", {}, "الإشعارات على هذا الجوال",
    isIOS && !standalone() ? h("small", {}, "أضف ببايه للشاشة الرئيسية أول") : null), sw);
  const supported = "serviceWorker" in navigator && "PushManager" in window && "Notification" in window;
  if (!supported) { sw.disabled = true; return label; }
  navigator.serviceWorker.ready.then((reg) => reg.pushManager.getSubscription()).then((sub) => {
    sw.checked = !!sub && Notification.permission === "granted";
  }).catch(() => {});
  sw.addEventListener("change", async () => {
    sw.disabled = true;
    try {
      if (sw.checked) await enablePush(); else await disablePush();
    } catch (e) {
      sw.checked = !sw.checked;
      toast(e instanceof Asleep ? "البوت نايم، فعّلها لما يصحى" : e.message);
    } finally { sw.disabled = false; }
  });
  return label;
}

function b64ToBytes(b64) {
  const pad = "=".repeat((4 - (b64.length % 4)) % 4);
  const raw = atob((b64 + pad).replace(/-/g, "+").replace(/_/g, "/"));
  return Uint8Array.from(raw, (c) => c.charCodeAt(0));
}

async function enablePush() {
  if (isIOS && !standalone()) throw new Error("في الآيفون: أضف ببايه للشاشة الرئيسية وافتحه من هناك");
  const perm = await Notification.requestPermission();
  if (perm !== "granted") throw new Error("ما سمحت بالإشعارات. فعّلها من إعدادات الجوال");
  const { vapid } = await api("ping");
  if (!vapid) throw new Error("الإشعارات مو مفعّلة على البوت");
  const reg = await navigator.serviceWorker.ready;
  const sub = (await reg.pushManager.getSubscription()) ||
    (await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: b64ToBytes(vapid) }));
  await api("push", { subscription: sub.toJSON() });
  toast("تمام! بتوصلك الإشعارات 🔔");
}

async function disablePush() {
  const reg = await navigator.serviceWorker.ready;
  const sub = await reg.pushManager.getSubscription();
  if (sub) { await api("push/off", { endpoint: sub.endpoint }).catch(() => {}); await sub.unsubscribe(); }
  toast("وقفت الإشعارات على هذا الجوال");
}

function toggle(label, key, small) {
  const st = S.state;
  const sw = h("input", { class: "sw", type: "checkbox", checked: !!st.settings[key], "aria-label": label });
  sw.addEventListener("change", () => saveSetting({ [key]: sw.checked }, () => (sw.checked = !sw.checked)));
  return h("label", { class: "set" }, h("span", {}, label, small && h("small", {}, small)), sw);
}

function choice(label, options, current, onpick) {
  const sel = h("select", { "aria-label": label }, options.map(([v, t]) => h("option", { value: v, selected: String(v) === String(current) }, t)));
  sel.addEventListener("change", () => onpick(sel.value));
  return h("label", { class: "set" }, label, sel);
}

async function saveSetting(body, undo) {
  if (S.asleep) { undo?.(); return toast("البوت نايم، غيّرها لما يصحى"); }
  try {
    S.state.settings = { ...S.state.settings, ...(await api("settings", body)) };
    store.set(stateKey(), JSON.stringify(S.state));
  } catch (e) { undo?.(); toast(e.message); }
}

async function settingsScreen() {
  clearTimers();
  if (!S.state) await loadState().catch(() => {});
  const st = S.state;
  if (!st) { $app.replaceChildren(h("div", { class: "loading" }, "لحظة…")); return; }
  const s = st.settings;
  const perm = st.solver.exams ? "واجبات + اختبارات" : st.solver.homework ? "واجبات" : "التنبيهات بس";
  const quiet = `${s.quiet_start}:${s.quiet_end}`;
  $app.replaceChildren(
    scene({ m: S.asleep ? "sleeping" : "thinking", cls: "mini", top: false, back: true,
      extra: h("h1", { class: "logo bubble" }, "الإعدادات") }),
    h("main", { class: "page" },
      h("section", { class: "card" }, bar("الحساب", "green", h("span")),
        h("div", { class: "setlist" },
          h("div", { class: "set" }, "الجهاز", h("span", { class: "chip" }, st.device)),
          h("div", { class: "set" }, "الصلاحية", h("span", { class: "chip lilac" }, perm)),
          st.checked && h("div", { class: "set" }, "آخر فحص لمدرستي", h("span", {}, String(st.checked).replace("T", " ").slice(0, 16))),
          h("button", { class: "b3d big danger", type: "button", onclick: () => logout(false) }, "اطلع من هذا الجوال"))),
      h("section", { class: "card" }, bar("الإشعارات", "orange", h("span")),
        h("div", { class: "setlist" },
          pushSetting(),
          toggle("المواعيد القريبة والجديدة", "notify_new"),
          toggle("التذكير قبل الموعد", "notify_remind"),
          choice("التذكير قبل", [[3, "3 ساعات"], [6, "6 ساعات"], [10, "10 ساعات"], [24, "يوم"]], s.remind_hours,
            (v) => saveSetting({ remind_hours: +v })),
          choice("أنبهك عن اللي ينتهي خلال", Object.entries(s.horizons || {}).map(([k, v]) => [k, v]), s.horizon_days,
            (v) => saveSetting({ horizon_days: +v })),
          choice("ساعات الهدوء", [["23:6", "11م → 6ص"], ["0:7", "12ص → 7ص"], ["0:0", "بدون هدوء"]], quiet,
            (v) => saveSetting({ quiet: v.split(":").map(Number) })),
          toggle("التسليم خارج المنصة 📨", "remind_external"),
          h("button", { class: "b3d big lime", type: "button", onclick: async () => {
            try { const r = await api("push/test", {}); toast(r.sent ? "أرسلت إشعار تجربة 🔔" : "فعّل الإشعارات أول"); } catch (e) { toast(e.message); }
          } }, icon("bell", 20), "جرّب إشعار"))),
      st.solver.homework && h("section", { class: "card" }, bar("الحل", "purple", h("span")),
        h("div", { class: "setlist" },
          toggle("الحل التلقائي للطوارئ", "auto_solve", `يحضّر الحل لما يبقى أقل من ${st.solver.auto_hours || 5} ساعة`),
          toggle("التسليم التلقائي", "auto_submit", "بس إذا اتفقت النماذج على كل الإجابات"),
          toggle("التسليم المباشر", "direct_submit", "يسلّم أول ما تضغط حل، بدون مراجعة"))),
      h("section", { class: "card" }, bar("التطبيق", "blue", h("span")),
        h("div", { class: "setlist" },
          h("div", { class: "set" }, "مضاف للشاشة الرئيسية", h("span", { class: "chip " + (standalone() ? "ok" : "") }, standalone() ? "نعم" : "لا")),
          h("div", { class: "set" }, "الإصدار", h("span", { class: "logo" }, `ببايه ${VERSION}`))))));
}

// ---------- chat ---------------------------------------------------------------------------------------------------
async function chatScreen() {
  clearTimers();
  if (!S.token) { location.hash = "#/login"; return; }
  let msgs = [], last = 0, waiting = false;
  const list = h("div", { class: "msgs", role: "log", "aria-live": "polite" });
  const input = h("input", { id: "msg", placeholder: "اكتب لببايه…", autocomplete: "off", enterkeyhint: "send" });
  const sceneBox = h("div");

  const act = async (b) => {
    if (b.url) return window.open(b.url, "_blank", "noopener");
    if (S.asleep) {
      const ok = ["solve", "submit"].includes(b.action) && (await queue(b.action, b.id));
      return toast(ok ? "انحفظ طلبك، أول ما يصحى ينفذه" : "البوت نايم الحين");
    }
    try {
      if (b.action === "submitall") await api("submitall", {});
      else await api(b.action, { id: b.id });
      toast(b.action === "solve" ? "أحل… ⏳" : b.action === "submit" ? "أسلّم… ⏳" : "تمام");
    } catch (e) { toast(e.message); }
  };

  const bubbleOf = (m) => {
    const el = h("div", { class: `msg ${m.who === "me" ? "me" : "bot"}` }, m.text);
    (m.items || []).forEach((i) => el.append(h("div", { class: "mini" },
      h("div", { class: "grow" }, h("div", { class: "t" }, itemName(i)), h("div", { class: "s" + (i.urgent ? " hot" : "") }, `باقي ${i.left}`)),
      itemAction(i))));
    if (m.buttons?.length) el.append(h("div", { class: "btns" }, m.buttons.map((b) => b.url
      ? h("a", { class: "b3d small", href: b.url, target: "_blank", rel: "noopener" }, b.label)
      : h("button", { class: "b3d small", type: "button", onclick: () => act(b) }, b.label))));
    return el;
  };

  const chips = h("div", { class: "chips" });
  const setChips = (arr) => chips.replaceChildren(...(arr || ["وش عندي اليوم؟", "وش عندي بكرة؟", "حل الكل", "حدّث"])
    .map((c) => h("button", { class: "b3d small", type: "button", onclick: () => send(c) }, c)));

  const paint = () => {
    sceneBox.replaceChildren(scene({ m: S.asleep ? "sleeping" : waiting ? "thinking" : "normal", cls: "mini", top: false, back: true,
      extra: h("div", { class: "bubble" }, h("span", {}, "ببايه "), waiting ? h("span", { class: "dots" }, h("i"), h("i"), h("i")) : S.asleep ? "نايم 💤" : "صاحي") }));
    list.replaceChildren(...(msgs.length ? msgs.map(bubbleOf) : [bubbleOf({ who: "bot", text: "هلا! وش تبغى أسوي لك؟" })]));
    list.scrollTop = list.scrollHeight;
  };

  const merge = (arr) => {
    for (const m of arr) if (!msgs.some((x) => x.id === m.id)) msgs.push(m);
    msgs = msgs.filter((m) => !m.progress || m.id === msgs[msgs.length - 1]?.id).slice(-150);
    last = msgs.reduce((a, m) => Math.max(a, m.id || 0), last);
    const lastBot = [...msgs].reverse().find((m) => m.who === "bot");
    setChips(lastBot?.chips);
  };

  const pull = async () => {
    try { merge((await api("inbox", null, { after: last })).messages); paint(); } catch (e) { if (e instanceof Asleep) paint(); }
  };

  async function send(text) {
    text = (text || "").trim();
    if (!text) return;
    input.value = "";
    if (S.asleep) { toast("البوت نايم، ما يقدر يرد الحين"); return; }
    msgs.push({ id: 0, who: "me", text }); waiting = true; paint();
    try {
      const out = await api("chat", { text });
      msgs = msgs.filter((m) => m.id !== 0);
      merge(out.messages);
    } catch (e) { msgs = msgs.filter((m) => m.id !== 0); toast(e instanceof Asleep ? "البوت نايم" : e.message); }
    waiting = false; paint();
  }

  const form = h("form", { onsubmit: (e) => { e.preventDefault(); send(input.value); } },
    h("label", { class: "vh", for: "msg" }, "رسالتك لببايه"), input,
    h("button", { class: "b3d icon orange", type: "submit", "aria-label": "أرسل" }, icon("send", 22)));
  $app.replaceChildren(h("div", { class: "chat" }, sceneBox, list, h("div", { class: "composer" }, chips, form)));
  setChips(); paint();
  await pull();
  every(3000, pull);
}

// ---------- command line: the Telegram bot itself, typed -----------------------------------------------------
const CMDS_KID = ["/list", "/settings", "/solveall", "/remind 6", "/quiet 23 6", "/help"];
const CMDS_ADMIN = ["/admin", "/status", "/health", "/everyone", "/grant", "/revoke", "/as", "/me"];

async function cmdScreen() {
  clearTimers();
  if (!S.token) { location.hash = "#/login"; return; }
  let v = 0, keys = [];
  const shown = new Map();                       // id -> element
  const history = store.json("babaya.cmdhist", []);
  let hpos = history.length;
  const screen = h("div", { class: "screen", role: "log", "aria-live": "polite" });
  const keysRow = h("div", { class: "keys" });
  const input = h("input", { id: "cmd", placeholder: "/list", autocomplete: "off", autocapitalize: "off", spellcheck: "false", enterkeyhint: "send" });

  const lineOf = (m) => {
    const el = h("div", { class: "line" + (m.me ? " me" : "") }, m.text);
    if (m.buttons?.length) el.append(h("div", { class: "btns" }, m.buttons.flat().map((b) => b.url
      ? h("a", { class: "b3d small", href: b.url, target: "_blank", rel: "noopener" }, b.label)
      : h("button", { class: "b3d small", type: "button", onclick: () => run({ data: b.data, id: m.id }) }, b.label))));
    return el;
  };

  const apply = (out) => {
    for (const id of out.gone || []) { shown.get(id)?.remove(); shown.delete(id); }
    for (const m of out.messages || []) {
      const el = lineOf(m);
      if (shown.has(m.id)) shown.get(m.id).replaceWith(el); else screen.append(el);
      shown.set(m.id, el);
      if (m.keys?.length) keys = m.keys;
    }
    v = Math.max(v, out.v || 0);
    const base = keys.length ? keys : [...CMDS_KID, ...(S.admin ? CMDS_ADMIN : [])];
    keysRow.replaceChildren(...base.map((k) => h("button", { class: "b3d small", type: "button", onclick: () => {
      if (/ $|^\/(grant|revoke|as|remind|quiet)$/.test(k)) { input.value = k + (k.endsWith(" ") ? "" : " "); input.focus(); }
      else run({ text: k });
    } }, k)));
    screen.scrollTop = screen.scrollHeight;
  };

  async function run(body) {
    if (S.asleep) return toast("البوت نايم، الأوامر تشتغل لما يصحى");
    try { apply(await api("cmd", { ...body, after: v })); } catch (e) { toast(e instanceof Asleep ? "البوت نايم" : e.message); }
  }

  const form = h("form", { class: "prompt", onsubmit: (e) => {
    e.preventDefault();
    const t = input.value.trim();
    if (!t) return;
    history.push(t); store.set("babaya.cmdhist", JSON.stringify(history.slice(-50))); hpos = history.length;
    input.value = "";
    run({ text: t });
  } }, h("label", { class: "vh", for: "cmd" }, "اكتب أمر"), input,
  h("button", { class: "b3d icon lime", type: "submit", "aria-label": "نفّذ" }, icon("send", 22)));
  input.addEventListener("keydown", (e) => {
    if (e.key === "ArrowUp" && hpos > 0) { input.value = history[--hpos]; e.preventDefault(); }
    if (e.key === "ArrowDown") { hpos = Math.min(history.length, hpos + 1); input.value = history[hpos] || ""; e.preventDefault(); }
  });

  $app.replaceChildren(h("div", { class: "term" },
    h("div", { class: "prompt" }, h("a", { class: "b3d icon", href: "#/", "aria-label": "رجوع للرئيسية" }, icon("back", 22)),
      h("h1", { class: "logo" }, "الأوامر")),
    h("section", { class: "card" },
      bar(`الطرفية · ${S.admin ? "الأدمن" : S.state?.device || ""}`, "", h("div", { class: "winbtns", "aria-hidden": "true" }, h("i"), h("i"), h("i"))),
      screen),
    keysRow, form));
  apply({ messages: [], v: 0 });
  try { apply(await api("cmd", null, { after: 0 })); } catch (e) { if (e instanceof Asleep) toast("البوت نايم"); }
  if (!shown.size) screen.append(h("div", { class: "line" }, "اكتب أي أمر من أوامر تيليجرام، مثل /list أو /settings. cls يمسح الشاشة."));
  every(2000, async () => { try { apply(await api("cmd", null, { after: v })); } catch { /* asleep */ } });
  setTimeout(() => input.focus(), 50);
}

// ---------- router ------------------------------------------------------------------------------------------------
function route() {
  const parts = location.hash.replace(/^#\/?/, "").split("/").filter(Boolean);
  if (!S.token && parts[0] !== "login") { location.hash = "#/login"; return; }
  window.scrollTo(0, 0);
  if (parts[0] === "login") return loginScreen();
  if (parts[0] === "solve" && parts[2]) return solveScreen(parts[1], decodeURIComponent(parts[2]));
  if (parts[0] === "settings") return settingsScreen();
  if (parts[0] === "chat") return chatScreen();
  if (parts[0] === "cmd") return cmdScreen();
  return homeScreen();
}

window.addEventListener("hashchange", route);
document.addEventListener("visibilitychange", () => { if (!document.hidden && !location.hash.includes("solve")) route(); });
if ("serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js").catch(() => {});
route();

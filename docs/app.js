import { connect, fromSaved } from "./link.js";
import { loadAssets, drawPage, cleanLines, canvasToPdf, STYLES, MAX_LINES } from "./render.js";

const $ = (id) => document.getElementById(id);
const SAVED_KEY = "rp-link";
const JOBS_KEY = "rp-jobs";
const MAX_FILE = 15 * 1024 * 1024;
const PRINT_W = 2339, PRINT_H = 1654;   // 200 dpi A4 landscape

const store = {
  get(k, d) { try { return JSON.parse(localStorage.getItem(k)) ?? d; } catch { return d; } },
  set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch {} },
  del(k) { try { localStorage.removeItem(k); } catch {} },
};

let link = null;
let events = null;
let style = STYLES[0];
let assetsReady = null;
let lastPong = 0;
let pongTimer = null;

// ---------------------------------------------------------------- status pill
function setStatus(kind, text) {
  $("statusPill").hidden = false;
  $("statusDot").className = "dot " + kind;
  $("statusText").textContent = text;
}

// ---------------------------------------------------------------- jobs list
const STATE_TEXT = {
  sent: "⏳ נשלח, ממתין למחשב",
  printing: "🖨️ מדפיס…",
  done: "✅ הודפס",
  error: "❌ נכשל",
};

function jobs() { return store.get(JOBS_KEY, []); }

function saveJob(job) {
  const list = jobs().filter((j) => j.id !== job.id);
  list.unshift(job);
  store.set(JOBS_KEY, list.slice(0, 15));
  renderJobs();
}

function updateJob(id, patch) {
  const list = jobs();
  const job = list.find((j) => j.id === id);
  if (!job) return;
  Object.assign(job, patch);
  store.set(JOBS_KEY, list);
  renderJobs();
}

function renderJobs() {
  const list = jobs();
  $("noJobs").hidden = list.length > 0;
  $("jobs").replaceChildren(...list.map((j) => {
    const li = document.createElement("li");
    const label = document.createElement("span");
    const time = new Date(j.time).toLocaleString("he-IL", { day: "numeric", month: "numeric", hour: "2-digit", minute: "2-digit" });
    label.textContent = `${j.label} · ${time}`;
    const st = document.createElement("span");
    st.className = "state " + j.state;
    st.textContent = STATE_TEXT[j.state] + (j.error ? `: ${j.error}` : "");
    li.append(label, st);
    return li;
  }));
}

// ---------------------------------------------------------------- printer computer
function onReply(msg) {
  if (msg.t === "status" && msg.id) {
    updateJob(msg.id, { state: msg.state, error: msg.error || "" });
  } else if (msg.t === "pong") {
    if ((msg.ts || 0) < Date.now() - 5 * 60 * 1000) return;   // old reply from history
    lastPong = Date.now();
    clearTimeout(pongTimer);
    setStatus("ok", `המחשב מחובר${msg.host ? ` (${msg.host})` : ""}`);
    fillPrinters(msg.printers || [], msg.printer || "", msg.default || "");
  }
}

function fillPrinters(printers, current, def) {
  const sel = $("printer");
  sel.replaceChildren();
  const opt = (value, text) => { const o = document.createElement("option"); o.value = value; o.textContent = text; sel.append(o); };
  opt("", `מדפסת ברירת המחדל${def ? ` (${def})` : ""}`);
  for (const p of printers) opt(p, p);
  sel.value = printers.includes(current) ? current : "";
  sel.disabled = false;
  $("printerHint").textContent = "";
}

async function ping() {
  if (!link) return;
  setStatus("wait", "בודק חיבור למחשב…");
  try {
    await link.send({ t: "ping" });
  } catch {
    setStatus("bad", "אין חיבור לאינטרנט");
    return;
  }
  clearTimeout(pongTimer);
  pongTimer = setTimeout(() => {
    if (Date.now() - lastPong > 12000) {
      setStatus("bad", "המחשב לא עונה");
      $("printerHint").textContent =
        "המחשב של המדפסת לא ענה. בדקו שהוא דלוק ומחובר לאינטרנט, ושהסיסמה נכונה. " +
        "אפשר בכל זאת לשלוח, וזה יודפס כשהמחשב יחזור (עד כמה שעות).";
    }
  }, 12000);
}

// ---------------------------------------------------------------- login
async function startSession(newLink) {
  link = newLink;
  $("login").hidden = true;
  $("app").hidden = false;
  renderJobs();
  events?.close();
  events = link.listen(onReply);
  ping();
  assetsReady = assetsReady || loadAssets();
  await assetsReady;
  redraw();
}

$("loginForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  $("loginBtn").disabled = true;
  $("loginBtn").textContent = "מתחבר…";
  try {
    const l = await connect($("password").value);
    if ($("remember").checked) store.set(SAVED_KEY, l.saved); else store.del(SAVED_KEY);
    $("password").value = "";
    await startSession(l);
  } finally {
    $("loginBtn").disabled = false;
    $("loginBtn").textContent = "כניסה";
  }
});

$("logout").addEventListener("click", () => {
  store.del(SAVED_KEY);
  events?.close();
  link = null;
  $("app").hidden = true;
  $("login").hidden = false;
  $("statusPill").hidden = true;
});

// ---------------------------------------------------------------- tabs
document.querySelectorAll("[role=tab]").forEach((b) => b.addEventListener("click", () => {
  document.querySelectorAll("[role=tab]").forEach((x) => x.setAttribute("aria-selected", x === b));
  $("tab-name").hidden = b.dataset.tab !== "name";
  $("tab-file").hidden = b.dataset.tab !== "file";
}));

// ---------------------------------------------------------------- name in a style
for (const s of STYLES) {
  const b = document.createElement("button");
  b.type = "button";
  b.textContent = s.title;
  b.setAttribute("aria-pressed", s === style);
  b.addEventListener("click", () => {
    style = s;
    document.querySelectorAll("#styles button").forEach((x) => x.setAttribute("aria-pressed", x === b));
    redraw();
  });
  $("styles").append(b);
}

function currentNames() { return cleanLines($("names").value); }

let raf = 0;
function redraw() {
  cancelAnimationFrame(raf);
  raf = requestAnimationFrame(async () => {
    await assetsReady;
    drawPage($("preview"), style, currentNames(), true);
    const n = $("names").value.split(/\r?\n/).filter((l) => l.trim()).length;
    $("namesHint").textContent = n > MAX_LINES ? `רק ${MAX_LINES} השורות הראשונות יודפסו.` : `עד ${MAX_LINES} שורות.`;
  });
}
$("names").addEventListener("input", redraw);

function newId() {
  return Date.now().toString(36) + Math.random().toString(36).slice(2, 8);
}

function showMsg(el, kind, text) {
  el.className = "msg " + kind;
  el.textContent = text;
}

$("sendName").addEventListener("click", async () => {
  const names = currentNames();
  if (!names.length) {
    showMsg($("nameMsg"), "bad", "צריך לכתוב שם.");
    $("names").focus();
    return;
  }
  const btn = $("sendName");
  btn.disabled = true;
  showMsg($("nameMsg"), "", "מכין ושולח…");
  try {
    await assetsReady;
    const canvas = document.createElement("canvas");
    canvas.width = PRINT_W;
    canvas.height = PRINT_H;
    drawPage(canvas, style, names, false);
    const pdf = await canvasToPdf(canvas);
    const id = newId();
    const label = `${style.title} – ${names.join(", ")}`;
    await link.sendPrint({ id, name: `${style.title} - ${names[0]}.pdf`, label }, pdf);
    saveJob({ id, label, time: Date.now(), state: "sent" });
    showMsg($("nameMsg"), "ok", "✓ נשלח! הסטטוס מתעדכן למטה.");
  } catch (e) {
    showMsg($("nameMsg"), "bad", e.message || "השליחה נכשלה");
  } finally {
    btn.disabled = false;
  }
});

// ---------------------------------------------------------------- file
let chosen = null;
$("file").addEventListener("change", () => {
  chosen = $("file").files[0] || null;
  $("drop").classList.toggle("has", !!chosen);
  $("fileLabel").textContent = chosen ? `📄 ${chosen.name}` : "לחצו כאן לבחירת קובץ";
  $("sendFile").disabled = !chosen;
  showMsg($("fileMsg"), "", "");
  if (chosen && chosen.size > MAX_FILE) {
    showMsg($("fileMsg"), "bad", "הקובץ גדול מ-15MB.");
    $("sendFile").disabled = true;
  }
});

$("sendFile").addEventListener("click", async () => {
  if (!chosen) return;
  const btn = $("sendFile");
  btn.disabled = true;
  showMsg($("fileMsg"), "", "מצפין ושולח…");
  try {
    const id = newId();
    await link.sendPrint({ id, name: chosen.name, label: chosen.name }, new Uint8Array(await chosen.arrayBuffer()));
    saveJob({ id, label: chosen.name, time: Date.now(), state: "sent" });
    showMsg($("fileMsg"), "ok", "✓ נשלח! הסטטוס מתעדכן למטה.");
  } catch (e) {
    showMsg($("fileMsg"), "bad", e.message || "השליחה נכשלה");
  } finally {
    btn.disabled = false;
  }
});

// ---------------------------------------------------------------- printer choice
$("printer").addEventListener("change", async () => {
  try {
    await link.send({ t: "settings", printer: $("printer").value });
    $("printerHint").textContent = "✓ נשמר";
  } catch (e) {
    $("printerHint").textContent = e.message;
  }
});
$("refresh").addEventListener("click", ping);
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible" && link && Date.now() - lastPong > 60000) ping();
});

// ---------------------------------------------------------------- start
const saved = store.get(SAVED_KEY, null);
if (saved?.topic && saved?.key) {
  fromSaved(saved).then(startSession).catch(() => store.del(SAVED_KEY));
}

import { login, resume, WrongPassword } from "./link.js";
import { loadAssets, drawPage, cleanLines, canvasToPdf, PRESETS, MAX_TITLE_LINES, MAX_NAME_LINES } from "./render.js";

const $ = (id) => document.getElementById(id);
const SAVED_KEY = "rp-link";
const JOBS_KEY = "rp-jobs";
const MAX_FILE = 15 * 1024 * 1024;
const PRINT_SIZES = [[3508, 2480], [2339, 1654]];   // 300 dpi A4 landscape, fallback 200 dpi
const PONG_TIMEOUT = 12000;
const RECHECK_EVERY = 10 * 60 * 1000;      // the free relay allows ~250 messages a day - be frugal

const store = {
  get(k, d) { try { return JSON.parse(localStorage.getItem(k)) ?? d; } catch { return d; } },
  set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch {} },
  del(k) { try { localStorage.removeItem(k); } catch {} },
};

let link = null;
let events = null;
let assetsReady = null;
let health = null;          // last answer from the printer computer
let lastPong = 0;
let pongTimer = null;
let wordOnPc = true;
let raf = 0;

// ================================================================ printer-ready button
function setReady(kind, icon, big, small) {
  $("ready").className = "ready " + kind;
  $("readyIcon").textContent = icon;
  $("readyBig").textContent = big;
  $("readySmall").textContent = small;
}

async function check() {
  if (!link) return;
  setReady("checking", "⋯", "בודק את המדפסת…", "פונה למחשב של המדפסת");
  try {
    await link.send({ t: "ping" });
  } catch (e) {
    if (e.status === 429) setReady("bad", "!", "שירות ההעברה עמוס כרגע", "נסו שוב בעוד דקה (לחצו כאן)");
    else setReady("bad", "!", "אין חיבור לאינטרנט", "בדקו את החיבור של המכשיר הזה ולחצו לבדיקה חוזרת");
    return;
  }
  clearTimeout(pongTimer);
  pongTimer = setTimeout(() => {
    if (Date.now() - lastPong > PONG_TIMEOUT) {
      health = null;
      setReady("bad", "✕", "המחשב של המדפסת לא עונה",
        "בדקו שהמחשב דלוק ומחובר לאינטרנט ושהסיסמה נכונה. לחצו לבדיקה חוזרת.");
      $("printerHint").textContent = "אפשר בכל זאת לשלוח. ההדפסה תצא כשהמחשב יחזור (תוך כמה שעות).";
    }
  }, PONG_TIMEOUT);
}

function onPong(msg) {
  if ((msg.ts || 0) < Date.now() - 5 * 60 * 1000) return;      // old answer from history
  lastPong = Date.now();
  clearTimeout(pongTimer);
  health = msg.health || { ready: true, problems: [], warnings: [], name: "" };
  wordOnPc = msg.word !== false;
  const how = health.how === "auto" && health.name ? " (זוהתה אוטומטית)" : "";
  const where = `${health.name || "מדפסת"}${how}${msg.host ? ` · מחשב ${msg.host}` : ""}`;
  if (health.ready) {
    const warn = health.warnings?.length ? ` · ${health.warnings.join(", ")}` : "";
    setReady("ok", "✓", "המדפסת מוכנה להדפסה", where + warn);
  } else {
    setReady("warn", "!", "המחשב מחובר, אבל יש בעיה במדפסת", `${health.problems.join(" · ")} (${where})`);
  }
  fillPrinters(msg.printers || [], msg.printer || "", health.name || "");
  updateFileHint();
}

// ================================================================ jobs + result panels
const STATE = {
  sent:     { icon: "⏳", big: "נשלח, ממתין לאישור מהמחשב…", list: "⏳ נשלח" },
  printing: { icon: "🖨️", big: "המחשב קיבל, מדפיס…",            list: "🖨️ מדפיס" },
  done:     { icon: "✅", big: "הודפס בהצלחה!",                   list: "✅ הודפס" },
  error:    { icon: "❌", big: "ההדפסה לא הצליחה",                 list: "❌ נכשל" },
};
const resultFor = {};      // job id -> result panel element

function showShot(box, url, caption) {
  const img = box.querySelector("img");
  img.src = url;
  img.onclick = () => window.open(url, "_blank");
  if (caption !== undefined) box.querySelector(".cap").textContent = caption;
  box.hidden = false;
}

function showResult(panel, state, small) {
  panel.hidden = false;
  if (state !== "error") panel.querySelector(".shotbox").hidden = true;
  panel.className = "result " + state;
  panel.querySelector(".icon").textContent = STATE[state].icon;
  panel.querySelector(".big").textContent = STATE[state].big;
  panel.querySelector(".small").textContent = small || "";
}

function jobs() { return store.get(JOBS_KEY, []); }

function saveJob(job) {
  store.set(JOBS_KEY, [job, ...jobs().filter((j) => j.id !== job.id)].slice(0, 15));
  renderJobs();
}

function onStatus(msg, attachment) {
  const list = jobs();
  const job = list.find((j) => j.id === msg.id);
  if (!job) return;
  job.state = msg.state;
  job.note = msg.state === "error" ? msg.error : msg.detail;
  store.set(JOBS_KEY, list);
  renderJobs();
  const panel = resultFor[msg.id];
  if (panel) {
    const small = msg.state === "done"
      ? `המחשב אישר: ${msg.detail || "ההדפסה הסתיימה תקין"}`
      : (msg.state === "error" ? `המחשב דיווח: ${msg.error || "תקלה לא ידועה"}` : msg.detail);
    showResult(panel, msg.state, small);
    if (msg.state === "error" && msg.shot && attachment) {
      link.fetchImage(attachment).then((url) => showShot(panel.querySelector(".shotbox"), url)).catch(() => {});
    }
  }
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
    st.textContent = STATE[j.state].list;
    if (j.note) st.title = j.note;
    li.append(label, st);
    return li;
  }));
}

let shotRequest = null;
function onReply(msg, attachment) {
  if (msg.t === "pong") onPong(msg);
  else if (msg.t === "status" && msg.id) onStatus(msg, attachment);
  else if (msg.t === "shot" && msg.id && msg.id === shotRequest) {
    shotRequest = null;
    $("shotBtn").disabled = false;
    if (msg.ok && attachment) {
      link.fetchImage(attachment)
        .then((url) => showShot($("shotBox"), url, `צילום מסך מהמחשב, ${new Date(msg.ts).toLocaleTimeString("he-IL")} (לחצו להגדלה)`))
        .catch((e) => { $("printerHint").textContent = e.message; });
    } else {
      $("printerHint").textContent = msg.error || "לא התקבל צילום מסך";
    }
  }
}

$("shotBtn").addEventListener("click", async () => {
  shotRequest = newId();
  $("shotBtn").disabled = true;
  $("printerHint").textContent = "מבקש צילום מסך מהמחשב…";
  try {
    await link.send({ t: "shot", id: shotRequest });
  } catch (e) {
    $("printerHint").textContent = e.message;
    $("shotBtn").disabled = false;
    return;
  }
  setTimeout(() => {
    if (shotRequest) { shotRequest = null; $("shotBtn").disabled = false; $("printerHint").textContent = "המחשב לא שלח צילום מסך. האם הוא דלוק?"; }
  }, 30000);
});

// ================================================================ printers
function fillPrinters(printers, current, def) {
  const sel = $("printer");
  sel.replaceChildren();
  const opt = (value, text) => { const o = document.createElement("option"); o.value = value; o.textContent = text; sel.append(o); };
  opt("", `אוטומטי${def ? ` (${def})` : ""}`);
  for (const p of printers) opt(p, p);
  sel.value = printers.includes(current) ? current : "";
  sel.disabled = false;
  $("printerHint").textContent = "";
}

$("printer").addEventListener("change", async () => {
  try {
    await link.send({ t: "settings", printer: $("printer").value });
    $("printerHint").textContent = "✓ נשמר. בודק את המדפסת שנבחרה…";
    setReady("checking", "⋯", "בודק את המדפסת…", $("printer").value || "מדפסת ברירת המחדל");
  } catch (e) {
    $("printerHint").textContent = e.message;
  }
});

// ================================================================ session
async function startSession(newLink) {
  link = newLink;
  $("login").hidden = true;
  $("app").hidden = false;
  renderJobs();
  events?.close();
  events = link.listen(onReply);
  check();
  assetsReady = assetsReady || loadAssets();
  await assetsReady;
  redraw();
}

$("loginForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  $("loginBtn").disabled = true;
  $("loginBtn").textContent = "מתחבר…";
  $("loginMsg").textContent = "";
  try {
    const l = await login($("password").value);
    if ($("remember").checked) store.set(SAVED_KEY, l.seedHex); else store.del(SAVED_KEY);
    $("password").value = "";
    await startSession(l);
  } catch (err) {
    $("loginMsg").textContent = err.message || "הכניסה נכשלה";
    if (err instanceof WrongPassword) $("password").select();
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
});

$("ready").addEventListener("click", check);
setInterval(() => { if (link && document.visibilityState === "visible") check(); }, RECHECK_EVERY);
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible" && link && Date.now() - lastPong > 60000) check();
});

// ================================================================ tabs
document.querySelectorAll("[role=tab]").forEach((b) => b.addEventListener("click", () => {
  document.querySelectorAll("[role=tab]").forEach((x) => x.setAttribute("aria-selected", x === b));
  $("tab-name").hidden = b.dataset.tab !== "name";
  $("tab-file").hidden = b.dataset.tab !== "file";
}));

// ================================================================ wording + name
function choosePreset(p, button) {
  document.querySelectorAll("#presets button").forEach((x) => x.setAttribute("aria-pressed", x === button));
  $("title").value = p.title;
  $("footer").value = p.footer;
  if (p.key === "custom") $("title").focus();
  redraw();
}
PRESETS.forEach((p, i) => {
  const b = document.createElement("button");
  b.type = "button";
  b.textContent = p.label;
  b.setAttribute("aria-pressed", "false");
  b.addEventListener("click", () => choosePreset(p, b));
  $("presets").append(b);
  if (i === 0) choosePreset(p, b);
});

function currentDoc() {
  return {
    title: cleanLines($("title").value, MAX_TITLE_LINES),
    names: cleanLines($("names").value, MAX_NAME_LINES),
    footer: $("footer").value.replace(/\s+/g, " ").trim(),
  };
}

function autoGrow(el) {
  el.style.height = "auto";
  el.style.height = el.scrollHeight + 2 + "px";
}

function redraw() {
  cancelAnimationFrame(raf);
  raf = requestAnimationFrame(async () => {
    if (!assetsReady) return;
    await assetsReady;
    drawPage($("preview"), currentDoc(), true);
  });
}
for (const id of ["title", "names", "footer"]) {
  $(id).addEventListener("input", () => {
    if (id !== "footer") autoGrow($(id));
    if (id === "title") {   // typing your own wording = "custom"
      const match = PRESETS.find((p) => p.title === $("title").value.trim());
      document.querySelectorAll("#presets button").forEach((b, i) =>
        b.setAttribute("aria-pressed", PRESETS[i] === (match || PRESETS[PRESETS.length - 1])));
    }
    redraw();
  });
}

const newId = () => Date.now().toString(36) + Math.random().toString(36).slice(2, 8);

function confirmIfNotReady() {
  if (health?.ready) return true;
  const why = health ? health.problems.join(", ") : "המחשב של המדפסת לא ענה";
  return confirm(`שימו לב: ${why}.\nלשלוח בכל זאת? (ההדפסה תצא כשהבעיה תסודר)`);
}

async function sendJob(meta, bytes, button, msgEl, panel) {
  msgEl.textContent = "";
  if (!confirmIfNotReady()) return;
  if (Date.now() - lastPong > 5 * 60 * 1000) check();   // refresh the printer state in the background
  button.disabled = true;
  try {
    const id = newId();
    await link.sendPrint({ id, ...meta }, bytes);
    saveJob({ id, label: meta.label, time: Date.now(), state: "sent" });
    resultFor[id] = panel;
    showResult(panel, "sent", "הדף הוכן ונשלח מוצפן למחשב של המדפסת");
  } catch (e) {
    msgEl.textContent = e.message || "השליחה נכשלה";
  } finally {
    button.disabled = false;
  }
}

$("sendName").addEventListener("click", async () => {
  const doc = currentDoc();
  if (!doc.names.length) {
    $("nameMsg").textContent = "צריך לכתוב שם.";
    $("names").focus();
    return;
  }
  if (!doc.title.length) {
    $("nameMsg").textContent = "צריך לבחור או לכתוב נוסח.";
    $("title").focus();
    return;
  }
  await assetsReady;
  // Print-ready PDF: one A4-landscape page holding the page as a 300 dpi image.
  // No fonts or layout inside the PDF, so it prints identically on any printer.
  let pdf = null;
  for (const [w, h] of PRINT_SIZES) {
    try {
      const canvas = document.createElement("canvas");
      canvas.width = w;
      canvas.height = h;
      drawPage(canvas, doc, false);
      pdf = await canvasToPdf(canvas);
      break;
    } catch { /* device could not make such a large image - try smaller */ }
  }
  if (!pdf) { $("nameMsg").textContent = "לא הצלחתי להכין את הדף במכשיר הזה."; return; }
  const label = `${doc.title.join(" ")} – ${doc.names.join(", ")}`;
  await sendJob({ name: `${doc.title[0]} - ${doc.names[0]}.pdf`, label }, pdf, $("sendName"), $("nameMsg"), $("nameResult"));
});

// ================================================================ file
let chosen = null;
function updateFileHint() {
  const isWord = chosen && !/\.pdf$/i.test(chosen.name);
  $("fileHint").textContent = isWord && !wordOnPc
    ? "⚠️ במחשב של המדפסת לא מותקן Word, ולכן קבצי Word לא יודפסו. שלחו PDF."
    : "עד 15MB. קבצי Word מודפסים אם במחשב של המדפסת מותקן Word.";
}
$("file").addEventListener("change", () => {
  chosen = $("file").files[0] || null;
  $("drop").classList.toggle("has", !!chosen);
  $("fileLabel").textContent = chosen ? `📄 ${chosen.name}` : "לחצו כאן לבחירת קובץ";
  $("sendFile").disabled = !chosen || chosen.size > MAX_FILE;
  $("fileMsg").textContent = chosen && chosen.size > MAX_FILE ? "הקובץ גדול מ-15MB." : "";
  $("fileResult").hidden = true;
  updateFileHint();
});
$("sendFile").addEventListener("click", async () => {
  if (!chosen) return;
  await sendJob({ name: chosen.name, label: chosen.name }, new Uint8Array(await chosen.arrayBuffer()),
    $("sendFile"), $("fileMsg"), $("fileResult"));
});

// ================================================================ start
const saved = store.get(SAVED_KEY, null);
if (typeof saved === "string" && /^[0-9a-f]{64}$/.test(saved)) {
  resume(saved).then(startSession).catch(() => store.del(SAVED_KEY));   // password changed -> log in again
}

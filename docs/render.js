// Draws the printed page: the scroll frame + fixed wording + the names.
// Used both for the live preview and for the PDF that is sent to the printer.

export const PAGE_W = 2970;   // A4 landscape, in 0.1 mm
export const PAGE_H = 2100;

export const STYLES = [
  { key: "niftar",    title: "לעילוי נשמת",    footer: "ת.נ.צ.ב.ה" },
  { key: "refua",     title: "לרפואה שלמה",    footer: "בתוך שאר חולי ישראל" },
  { key: "hatzlacha", title: "להצלחה ופרנסה", footer: "" },
];

export const MAX_LINES = 8;
const FONT = "Stam";
const INK = "#1b1b1b";

let frame = null;

export async function loadAssets() {
  await document.fonts.load(`100px ${FONT}`, "לעילוי נשמת");
  frame = new Image();
  frame.src = "frame.svg";
  await frame.decode();
}

function fontPx(size) {
  return `${size}px ${FONT}`;
}

// Largest size <= max at which the text fits in maxWidth.
function fitSize(ctx, text, max, maxWidth) {
  ctx.font = fontPx(max);
  const w = ctx.measureText(text).width;
  return w <= maxWidth ? max : Math.floor(max * maxWidth / w);
}

export function cleanLines(text) {
  return (text || "")
    .split(/\r?\n/)
    .map((l) => l.replace(/\s+/g, " ").trim())
    .filter(Boolean)
    .slice(0, MAX_LINES);
}

/**
 * @param {HTMLCanvasElement} canvas  any size with a 2970:2100 ratio
 * @param {object} style              one of STYLES
 * @param {string[]} names            lines to print
 * @param {boolean} placeholder       show a grey hint when there are no names (preview only)
 */
export function drawPage(canvas, style, names, placeholder = false) {
  const ctx = canvas.getContext("2d");
  const s = canvas.width / PAGE_W;
  ctx.setTransform(1, 0, 0, 1, 0, 0);
  ctx.fillStyle = "#fff";
  ctx.fillRect(0, 0, canvas.width, canvas.height);
  ctx.setTransform(s, 0, 0, s, 0, 0);
  ctx.drawImage(frame, 0, 0, PAGE_W, PAGE_H);

  ctx.textAlign = "center";
  ctx.textBaseline = "alphabetic";
  ctx.direction = "rtl";

  const shadow = (on) => {
    ctx.shadowColor = on ? "rgba(0,0,0,0.28)" : "transparent";
    ctx.shadowBlur = on ? 8 : 0;
    ctx.shadowOffsetX = on ? 3 : 0;
    ctx.shadowOffsetY = on ? 4 : 0;
  };

  // heading
  shadow(true);
  ctx.fillStyle = INK;
  const titleSize = fitSize(ctx, style.title, 270, 1900);
  ctx.font = fontPx(titleSize);
  ctx.fillText(style.title, PAGE_W / 2, 610);

  // names
  const top = 830;
  const bottom = style.footer ? 1640 : 1760;
  let lines = names;
  let color = INK;
  if (!lines.length && placeholder) {
    lines = ["השם יופיע כאן"];
    color = "#a3a3a3";
    shadow(false);
  }
  if (lines.length) {
    const lineH = Math.min(250, (bottom - top) / lines.length);
    let size = Math.floor(lineH / 1.3);
    for (const l of lines) size = Math.min(size, fitSize(ctx, l, size, 2050));
    ctx.font = fontPx(size);
    ctx.fillStyle = color;
    const blockH = lineH * lines.length;
    let y = top + (bottom - top - blockH) / 2 + lineH / 2 + size * 0.35;
    for (const l of lines) {
      ctx.fillText(l, PAGE_W / 2, y);
      y += lineH;
    }
  }

  // closing line
  if (style.footer) {
    shadow(false);
    ctx.fillStyle = "#3a3a3a";
    ctx.font = fontPx(105);
    ctx.fillText(style.footer, PAGE_W / 2, 1765);
  }
  ctx.setTransform(1, 0, 0, 1, 0, 0);
}

// ---- PDF: one A4-landscape page holding the rendered image (no libraries needed)
export async function canvasToPdf(canvas) {
  const blob = await new Promise((r) => canvas.toBlob(r, "image/jpeg", 0.93));
  const jpg = new Uint8Array(await blob.arrayBuffer());
  const enc = new TextEncoder();
  const parts = [];
  const offsets = [];
  let length = 0;
  const push = (p) => {
    const bytes = typeof p === "string" ? enc.encode(p) : p;
    parts.push(bytes);
    length += bytes.length;
  };
  const obj = (n, body) => {
    offsets[n] = length;
    push(`${n} 0 obj\n${body}\nendobj\n`);
  };
  const pw = 842, ph = 595;   // A4 landscape in points

  push("%PDF-1.4\n%\xE2\xE3\xCF\xD3\n");
  obj(1, "<< /Type /Catalog /Pages 2 0 R >>");
  obj(2, "<< /Type /Pages /Kids [3 0 R] /Count 1 >>");
  obj(3, `<< /Type /Page /Parent 2 0 R /MediaBox [0 0 ${pw} ${ph}] ` +
         "/Resources << /XObject << /Im0 4 0 R >> >> /Contents 5 0 R >>");
  offsets[4] = length;
  push(`4 0 obj\n<< /Type /XObject /Subtype /Image /Width ${canvas.width} /Height ${canvas.height} ` +
       `/ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /DCTDecode /Length ${jpg.length} >>\nstream\n`);
  push(jpg);
  push("\nendstream\nendobj\n");
  const content = `q ${pw} 0 0 ${ph} 0 0 cm /Im0 Do Q`;
  obj(5, `<< /Length ${content.length} >>\nstream\n${content}\nendstream`);

  const xref = length;
  let table = "xref\n0 6\n0000000000 65535 f \n";
  for (let i = 1; i <= 5; i++) table += String(offsets[i]).padStart(10, "0") + " 00000 n \n";
  push(table + `trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n${xref}\n%%EOF\n`);

  const out = new Uint8Array(length);
  let pos = 0;
  for (const p of parts) { out.set(p, pos); pos += p.length; }
  return out;
}

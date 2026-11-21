// Draws the printed page: the decorative frame + the wording + the name (in larger letters).
// Used both for the live preview and for the PDF that is sent to the printer.
//
// To change the look, replace frame.svg (any image the size of an A4 landscape page
// works, e.g. frame.png - then change FRAME_SRC) and adjust LAYOUT below.

export const PAGE_W = 2970;   // A4 landscape, in 0.1 mm
export const PAGE_H = 2100;
const FRAME_SRC = "frame.svg";
const ORNAMENT_SRC = "ornament.svg";

// Fixed wordings. "custom" lets the user write their own.
export const PRESETS = [
  { key: "niftar",    label: "לעילוי נשמת",    title: "לעילוי נשמת",    footer: "ת.נ.צ.ב.ה" },
  { key: "refua",     label: "לרפואה שלמה",    title: "לרפואה שלמה",    footer: "בתוך שאר חולי ישראל" },
  { key: "hatzlacha", label: "להצלחה ופרנסה", title: "להצלחה ופרנסה", footer: "" },
  { key: "zera",      label: "לזרעא בר קיימא", title: "לזרעא בר קיימא", footer: "" },
  { key: "custom",    label: "נוסח אישי",       title: "",               footer: "" },
];

const LAYOUT = {
  textWidth: 2000,       // max width of any line
  top: 330,              // top of the wording block
  titleSize: 190,        // wording font size (shrinks to fit)
  nameMax: 320,          // name font size (shrinks to fit) - larger than the wording
  footerY: 1765,
  footerSize: 105,
  bottom: 1680,          // lowest point for names when there is a footer
  bottomNoFooter: 1790,
};

export const MAX_TITLE_LINES = 3;
export const MAX_NAME_LINES = 6;
const FONT = "Stam";
const INK = "#1b1b1b";

let frame = null;
let ornament = null;

async function loadImage(src) {
  const img = new Image();
  img.src = src;
  await img.decode();
  return img;
}

export async function loadAssets() {
  await document.fonts.load(`100px ${FONT}`, "לעילוי נשמת");
  [frame, ornament] = await Promise.all([loadImage(FRAME_SRC), loadImage(ORNAMENT_SRC)]);
}

const fontPx = (size) => `${size}px ${FONT}`;

function fitSize(ctx, text, max, maxWidth) {
  ctx.font = fontPx(max);
  const w = ctx.measureText(text).width;
  return w <= maxWidth ? max : Math.floor(max * maxWidth / w);
}

export function cleanLines(text, max) {
  return (text || "")
    .split(/\r?\n/)
    .map((l) => l.replace(/\s+/g, " ").trim())
    .filter(Boolean)
    .slice(0, max);
}

/**
 * @param {HTMLCanvasElement} canvas  any size with a 2970:2100 ratio
 * @param {{title: string[], names: string[], footer: string}} doc
 * @param {boolean} placeholder  show grey hints for empty fields (preview only)
 */
export function drawPage(canvas, doc, placeholder = false) {
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
  const cx = PAGE_W / 2;
  const shadow = (on) => {
    ctx.shadowColor = on ? "rgba(0,0,0,0.28)" : "transparent";
    ctx.shadowBlur = on ? 8 : 0;
    ctx.shadowOffsetX = on ? 3 : 0;
    ctx.shadowOffsetY = on ? 4 : 0;
  };
  const hint = (lines, fallback) => (lines.length || !placeholder) ? [lines, INK] : [[fallback], "#a8a8a8"];

  // wording
  const [title, titleInk] = hint(doc.title, "הנוסח יופיע כאן");
  let y = LAYOUT.top;
  if (title.length) {
    let size = Math.min(LAYOUT.titleSize, Math.floor(LAYOUT.titleSize * 2.2 / (title.length + 1.2)));
    for (const l of title) size = Math.min(size, fitSize(ctx, l, size, LAYOUT.textWidth));
    ctx.font = fontPx(size);
    ctx.fillStyle = titleInk;
    shadow(titleInk === INK);
    for (const l of title) {
      y += size * 1.12;
      ctx.fillText(l, cx, y);
    }
  }

  // ornament under the wording
  shadow(false);
  const ornW = 840 * 0.95, ornH = 120 * 0.95;
  const ornY = y + 55;
  ctx.drawImage(ornament, cx - ornW / 2, ornY, ornW, ornH);

  // names - the biggest text on the page
  const footer = (doc.footer || "").trim();
  const top = ornY + ornH + 40;
  const bottom = footer ? LAYOUT.bottom : LAYOUT.bottomNoFooter;
  const [names, nameInk] = hint(doc.names, "השם יופיע כאן");
  if (names.length) {
    const lineH = Math.min(LAYOUT.nameMax * 1.25, (bottom - top) / names.length);
    let size = Math.floor(lineH / 1.25);
    for (const l of names) size = Math.min(size, fitSize(ctx, l, size, LAYOUT.textWidth));
    ctx.font = fontPx(size);
    ctx.fillStyle = nameInk;
    shadow(nameInk === INK);
    const blockH = lineH * names.length;
    let ny = top + (bottom - top - blockH) / 2 + lineH / 2 + size * 0.36;
    for (const l of names) {
      ctx.fillText(l, cx, ny);
      ny += lineH;
    }
  }

  // closing line
  if (footer) {
    shadow(false);
    ctx.fillStyle = "#3a3a3a";
    ctx.font = fontPx(fitSize(ctx, footer, LAYOUT.footerSize, LAYOUT.textWidth));
    ctx.fillText(footer, cx, LAYOUT.footerY);
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
  const [pw, ph] = canvas.width >= canvas.height ? [842, 595] : [595, 842];   // A4 in points, by orientation

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


// ---- image files: any photo becomes one A4 page (portrait or landscape, whichever fits the photo better)
const IMAGE_NAME = /\.(jpe?g|png|gif|webp|bmp|avif|heic|heif)$/i;
export const isImageFile = (f) => !!f && ((f.type || "").startsWith("image/") || IMAGE_NAME.test(f.name || ""));

// Canvas sizes for the printed page: 300 dpi, and 200 dpi for devices that cannot make such a large canvas.
export function imagePageSizes(image) {
  const landscape = (image.naturalWidth || image.width) >= (image.naturalHeight || image.height);
  return [[3508, 2480], [2339, 1654]].map(([w, h]) => (landscape ? [w, h] : [h, w]));
}

export async function decodeImage(file) {
  try {
    return await createImageBitmap(file);                  // applies the rotation stored in a phone photo
  } catch {
    const url = URL.createObjectURL(file);
    try {
      const img = new Image();
      img.src = url;
      await img.decode();
      return img;
    } finally {
      URL.revokeObjectURL(url);
    }
  }
}

// White page, the image as large as fits inside a small margin, centred, proportions kept.
export function drawImagePage(canvas, image) {
  const ctx = canvas.getContext("2d");
  ctx.setTransform(1, 0, 0, 1, 0, 0);
  ctx.fillStyle = "#fff";
  ctx.fillRect(0, 0, canvas.width, canvas.height);
  const iw = image.naturalWidth || image.width, ih = image.naturalHeight || image.height;
  const margin = Math.round(canvas.width * 0.03);
  const scale = Math.min((canvas.width - 2 * margin) / iw, (canvas.height - 2 * margin) / ih);
  const w = iw * scale, h = ih * scale;
  ctx.imageSmoothingEnabled = true;
  ctx.imageSmoothingQuality = "high";
  ctx.drawImage(image, (canvas.width - w) / 2, (canvas.height - h) / 2, w, h);
}

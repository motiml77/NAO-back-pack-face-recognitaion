// Secure link between this site and the printer computer.
//
// Messages travel through ntfy.sh (a free public relay, no account needed) on a fixed
// channel that is also built into the printer program (channel.json).
// - Everything is encrypted (AES-256-GCM).
// - Every command is digitally signed (Ed25519) with a key derived from the site password.
//   The printer program knows only the public key (channel.json "verify"), so it can check
//   signatures but nobody can produce one without the password. Unsigned messages are ignored.
// The password itself never leaves this page and is not stored anywhere.

const enc = new TextEncoder();
const dec = new TextDecoder();
const PKCS8_ED25519 = [0x30, 0x2e, 0x02, 0x01, 0x00, 0x30, 0x05, 0x06, 0x03, 0x2b, 0x65, 0x70, 0x04, 0x22, 0x04, 0x20];

const hex = (buf) => [...new Uint8Array(buf)].map((b) => b.toString(16).padStart(2, "0")).join("");
const unhex = (h) => new Uint8Array(h.match(/../g).map((x) => parseInt(x, 16)));

function b64(bytes) {
  let s = "";
  for (let i = 0; i < bytes.length; i += 0x8000) s += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
  return btoa(s);
}
function unb64(s) {
  const bin = atob(s);
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out;
}
const b64urlToHex = (s) => hex(unb64(s.replace(/-/g, "+").replace(/_/g, "/") + "===".slice((s.length + 3) % 4)));

export class WrongPassword extends Error {}
export class OldBrowser extends Error {}

let channel = null;
export async function loadChannel() {
  if (channel) return channel;
  const res = await fetch(`channel.json?t=${Date.now()}`, { cache: "no-store" });
  if (!res.ok) throw new Error("לא ניתן לטעון את הגדרות האתר");
  channel = await res.json();
  return channel;
}

async function signerFromSeed(seed) {
  const pkcs8 = new Uint8Array([...PKCS8_ED25519, ...seed]);
  let key;
  try {
    key = await crypto.subtle.importKey("pkcs8", pkcs8, { name: "Ed25519" }, true, ["sign"]);
  } catch {
    throw new OldBrowser("הדפדפן הזה ישן מדי. עדכנו אותו (או השתמשו ב-Chrome / Safari מעודכנים).");
  }
  const jwk = await crypto.subtle.exportKey("jwk", key);
  return { key, publicHex: b64urlToHex(jwk.x) };
}

async function derivedSeed(password, ch) {
  const pw = enc.encode((password || "").normalize("NFC").trim());
  const base = await crypto.subtle.importKey("raw", pw, "PBKDF2", false, ["deriveBits"]);
  const bits = await crypto.subtle.deriveBits(
    { name: "PBKDF2", hash: "SHA-256", salt: enc.encode(ch.salt), iterations: ch.iterations }, base, 256);
  return new Uint8Array(bits);
}

// Password -> Link. Throws WrongPassword if it does not match the site key.
export async function login(password) {
  const ch = await loadChannel();
  return open(await derivedSeed(password, ch), ch);
}

// Remembered login (the derived seed, never the password).
export async function resume(seedHex) {
  return open(unhex(seedHex), await loadChannel());
}

async function open(seed, ch) {
  const signer = await signerFromSeed(seed);
  if (signer.publicHex !== ch.verify) throw new WrongPassword("הסיסמה שגויה");
  const aes = await crypto.subtle.importKey("raw", unhex(ch.key), "AES-GCM", false, ["encrypt", "decrypt"]);
  return new Link(ch, aes, signer.key, hex(seed));
}

class Link {
  constructor(ch, aes, signKey, seedHex) {
    this.relay = ch.relay || "https://ntfy.sh";
    this.topic = ch.topic;
    this.aes = aes;
    this.signKey = signKey;
    this.seedHex = seedHex;
  }

  async encrypt(bytes) {
    const iv = crypto.getRandomValues(new Uint8Array(12));
    const ct = new Uint8Array(await crypto.subtle.encrypt({ name: "AES-GCM", iv }, this.aes, bytes));
    const out = new Uint8Array(12 + ct.length);
    out.set(iv);
    out.set(ct, 12);
    return out;
  }

  async decrypt(bytes) {
    return new Uint8Array(await crypto.subtle.decrypt({ name: "AES-GCM", iv: bytes.subarray(0, 12) }, this.aes, bytes.subarray(12)));
  }

  // signed + encrypted command
  async seal(obj) {
    const payload = JSON.stringify({ ...obj, ts: Date.now() });
    const sig = new Uint8Array(await crypto.subtle.sign({ name: "Ed25519" }, this.signKey, enc.encode(payload)));
    return b64(await this.encrypt(enc.encode(JSON.stringify({ p: payload, s: b64(sig) }))));
  }

  async openReply(text) {
    return JSON.parse(dec.decode(await this.decrypt(unb64(text))));
  }

  static async check(res) {
    if (res.ok) return;
    let detail = "";
    try { detail = (await res.json()).error || ""; } catch {}
    const msg = res.status === 429 ? "שירות ההעברה עמוס כרגע, נסו שוב בעוד דקה." : `שגיאת רשת (${res.status}) ${detail}`;
    throw Object.assign(new Error(msg), { status: res.status });
  }

  async send(obj) {
    await Link.check(await fetch(`${this.relay}/${this.topic}`, { method: "POST", body: await this.seal(obj) }));
  }

  async sendPrint(meta, bytes) {
    const sha = hex(await crypto.subtle.digest("SHA-256", bytes));
    const message = await this.seal({ t: "print", ...meta, sha });
    const url = `${this.relay}/${this.topic}?filename=job.bin&message=${encodeURIComponent(message)}`;
    await Link.check(await fetch(url, { method: "PUT", body: await this.encrypt(bytes) }));
  }

  // encrypted file sent by the printer computer (screenshot) -> object URL of the image
  async fetchImage(attachment) {
    const res = await fetch(attachment.url);
    if (!res.ok) throw new Error("התמונה כבר לא זמינה");
    const plain = await this.decrypt(new Uint8Array(await res.arrayBuffer()));
    return URL.createObjectURL(new Blob([plain], { type: "image/jpeg" }));
  }

  // replies from the printer computer (pong / job status)
  listen(onMessage, since = "3h") {
    const es = new EventSource(`${this.relay}/${this.topic}-s/sse?since=${since}`);
    es.onmessage = async (ev) => {
      try {
        const data = JSON.parse(ev.data);
        if (data.event !== "message" || !data.message) return;
        onMessage(await this.openReply(data.message), data.attachment || null);
      } catch { /* not decryptable - ignore */ }
    };
    return es;
  }
}

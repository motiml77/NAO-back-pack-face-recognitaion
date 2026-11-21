// Secure link between this site and the printer computer.
//
// Messages travel through ntfy.sh (a free public relay, no account needed) on a fixed
// channel that is also built into the printer program (channel.json).
// - Commands are encrypted with the channel key (AES-256-GCM). That key is PUBLIC (channel.json), so this
//   hides nothing from somebody who looks for it - what protects the printer is the signature.
// - Replies (printer state, job status, SCREENSHOTS) are encrypted to THIS device: every command carries the
//   device's X25519 public key "rk" inside the signed part, and the printer computer encrypts its answer to it
//   (ECIES: X25519 + HKDF-SHA256 + AES-256-GCM). Only this browser holds the private key.
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

const REPLY_INFO = enc.encode("remote-printer/reply/v1");
const concat = (a, b) => { const o = new Uint8Array(a.length + b.length); o.set(a); o.set(b, a.length); return o; };
const b64urlToBytes = (s) => unb64(s.replace(/-/g, "+").replace(/_/g, "/") + "===".slice((s.length + 3) % 4));

// This device's reply key pair, created once and kept in the browser. Null when the browser has no X25519:
// then replies stay readable by anybody, so the printer computer sends no screenshots.
export async function loadDeviceKey() {
  try {
    let jwk = null;
    try { jwk = JSON.parse(localStorage.getItem("rp-device") || "null"); } catch {}
    let priv;
    if (jwk && jwk.d && jwk.x) {
      priv = await crypto.subtle.importKey("jwk", jwk, { name: "X25519" }, true, ["deriveBits"]);
    } else {
      priv = (await crypto.subtle.generateKey({ name: "X25519" }, true, ["deriveBits"])).privateKey;
      jwk = await crypto.subtle.exportKey("jwk", priv);
      try { localStorage.setItem("rp-device", JSON.stringify({ kty: jwk.kty, crv: jwk.crv, d: jwk.d, x: jwk.x })); } catch {}
    }
    return { priv, rk: b64urlToBytes(jwk.x) };
  } catch {
    return null;
  }
}

// e_pub(32) | iv(12) | AES-GCM(key, iv, data, aad = e_pub | rk),  key = HKDF-SHA256(X25519(priv, e_pub), salt = e_pub | rk)
export async function decryptReply(device, blob) {
  if (blob.length < 32 + 12 + 16) throw new Error("reply too short");
  const ePub = blob.subarray(0, 32), iv = blob.subarray(32, 44), ct = blob.subarray(44);
  const peer = await crypto.subtle.importKey("raw", ePub, { name: "X25519" }, false, []);
  const shared = await crypto.subtle.deriveBits({ name: "X25519", public: peer }, device.priv, 256);
  const salt = concat(ePub, device.rk);
  const hk = await crypto.subtle.importKey("raw", shared, "HKDF", false, ["deriveKey"]);
  const key = await crypto.subtle.deriveKey({ name: "HKDF", hash: "SHA-256", salt, info: REPLY_INFO }, hk,
    { name: "AES-GCM", length: 256 }, false, ["decrypt"]);
  return new Uint8Array(await crypto.subtle.decrypt({ name: "AES-GCM", iv, additionalData: salt }, key, ct));
}

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
  return new Link(ch, aes, signer.key, hex(seed), await loadDeviceKey());
}

class Link {
  constructor(ch, aes, signKey, seedHex, device) {
    this.device = device;
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
    const body = { ...obj, ts: Date.now() };
    if (this.device) body.rk = b64(this.device.rk);          // inside the signed part: cannot be swapped
    const payload = JSON.stringify(body);
    const sig = new Uint8Array(await crypto.subtle.sign({ name: "Ed25519" }, this.signKey, enc.encode(payload)));
    return b64(await this.encrypt(enc.encode(JSON.stringify({ p: payload, s: b64(sig) }))));
  }

  async openReply(text) {
    const raw = unb64(text);
    if (this.device) {
      try { return JSON.parse(dec.decode(await decryptReply(this.device, raw))); } catch { /* not for this device */ }
    }
    return JSON.parse(dec.decode(await this.decrypt(raw)));   // replies of an older program version
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
    if (!this.device) throw new Error("הדפדפן לא תומך בהצפנת תמונות");
    const plain = await decryptReply(this.device, new Uint8Array(await res.arrayBuffer()));
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

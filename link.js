// Secure link between this site and the printer computer.
//
// Messages travel through ntfy.sh (a free public relay, no account needed).
// Everything is end-to-end encrypted with a key derived from the password,
// and the channel name itself is derived from the password too - so without
// the password nobody can find the channel, read it, or send a print job.
// The printer computer ignores anything that is not encrypted with the key.

export const RELAY = "https://ntfy.sh";
const ITERATIONS = 150000;
const enc = new TextEncoder();
const dec = new TextDecoder();

const hex = (buf) => [...new Uint8Array(buf)].map((b) => b.toString(16).padStart(2, "0")).join("");

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

export function normalizePassword(pw) {
  return (pw || "").normalize("NFC").trim();
}

export async function connect(password) {
  const base = await crypto.subtle.importKey("raw", enc.encode(normalizePassword(password)), "PBKDF2", false, ["deriveBits"]);
  const bits = (salt, n) => crypto.subtle.deriveBits(
    { name: "PBKDF2", hash: "SHA-256", salt: enc.encode(salt), iterations: ITERATIONS }, base, n);
  const topic = "rp" + hex(await bits("remote-printer/topic/v1", 128));
  return fromSaved({ topic, key: hex(await bits("remote-printer/key/v1", 256)) });
}

// Saved form keeps only the derived channel + key, never the password itself.
export async function fromSaved(saved) {
  const raw = new Uint8Array(saved.key.match(/../g).map((h) => parseInt(h, 16)));
  const key = await crypto.subtle.importKey("raw", raw, "AES-GCM", false, ["encrypt", "decrypt"]);
  const link = new Link(saved.topic, key);
  link.saved = { topic: saved.topic, key: saved.key };
  return link;
}

class Link {
  constructor(topic, key) {
    this.topic = topic;
    this.key = key;
  }

  async encrypt(bytes) {
    const iv = crypto.getRandomValues(new Uint8Array(12));
    const ct = new Uint8Array(await crypto.subtle.encrypt({ name: "AES-GCM", iv }, this.key, bytes));
    const out = new Uint8Array(12 + ct.length);
    out.set(iv);
    out.set(ct, 12);
    return out;
  }

  async decrypt(bytes) {
    const pt = await crypto.subtle.decrypt({ name: "AES-GCM", iv: bytes.subarray(0, 12) }, this.key, bytes.subarray(12));
    return new Uint8Array(pt);
  }

  async sealJson(obj) {
    return b64(await this.encrypt(enc.encode(JSON.stringify({ ...obj, ts: Date.now() }))));
  }

  async openJson(text) {
    return JSON.parse(dec.decode(await this.decrypt(unb64(text))));
  }

  // small control message (ping, settings)
  async send(obj) {
    const res = await fetch(`${RELAY}/${this.topic}`, { method: "POST", body: await this.sealJson(obj) });
    if (!res.ok) throw new Error(`שגיאת רשת (${res.status})`);
  }

  // a document to print: encrypted file as attachment + encrypted description as the message
  async sendPrint(meta, bytes) {
    const message = await this.sealJson({ t: "print", ...meta });
    const url = `${RELAY}/${this.topic}?filename=job.bin&message=${encodeURIComponent(message)}`;
    const res = await fetch(url, { method: "PUT", body: await this.encrypt(bytes) });
    if (!res.ok) {
      let detail = "";
      try { detail = (await res.json()).error || ""; } catch {}
      throw new Error(`השליחה נכשלה (${res.status}) ${detail}`);
    }
  }

  // replies from the printer computer
  listen(onMessage, since = "3h") {
    const es = new EventSource(`${RELAY}/${this.topic}-s/sse?since=${since}`);
    es.onmessage = async (ev) => {
      try {
        const data = JSON.parse(ev.data);
        if (data.event !== "message" || !data.message) return;
        onMessage(await this.openJson(data.message));
      } catch {
        /* not ours / not decryptable - ignore */
      }
    };
    return es;
  }
}

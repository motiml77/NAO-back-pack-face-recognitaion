#!/usr/bin/env python3
"""Test client: talks to a running Remote Printer exactly like the website does.

    python send_test.py --password PW ping
    python send_test.py --password PW print FILE.pdf
Exits 0 when the expected reply arrives (pong / status "done").
"""
import argparse
import base64
import hashlib
import json
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import remote_printer as rp   # noqa: E402
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402


def http(url, data=None, method=None):
    req = urllib.request.Request(url, data=data, method=method, headers={"User-Agent": "rp-test"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--password", required=True)
    ap.add_argument("--timeout", type=int, default=120)
    ap.add_argument("--topic", default=rp.CHANNEL_TOPIC)
    ap.add_argument("--key", default=rp.CHANNEL_KEY)
    ap.add_argument("--expect-error", action="store_true", help="success = an error reply arrives")
    ap.add_argument("mode", choices=["ping", "print", "shot"])
    ap.add_argument("file", nargs="?")
    a = ap.parse_args()

    ch = rp.Channel(a.topic, a.key, None)
    signer = Ed25519PrivateKey.from_private_bytes(rp.derive_signing_seed(a.password, a.topic))

    def signed(obj):
        payload = json.dumps(dict(obj, ts=int(time.time() * 1000)), ensure_ascii=False)
        sig = base64.b64encode(signer.sign(payload.encode())).decode()
        return ch.seal_json({"p": payload, "s": sig})

    start = int(time.time()) - 2
    if a.mode == "ping":
        http(f"{rp.RELAY}/{ch.topic}", signed({"t": "ping"}).encode(), "POST")
        want = lambda m: m.get("t") == "pong" and m.get("ts", 0) / 1000 >= start
    elif a.mode == "shot":
        sid = "shot" + str(int(time.time()))
        http(f"{rp.RELAY}/{ch.topic}", signed({"t": "shot", "id": sid}).encode(), "POST")
        want = lambda m: m.get("t") == "shot" and m.get("id") == sid
    else:
        data = Path(a.file).read_bytes()
        job = "test" + str(int(time.time()))
        meta = signed({"t": "print", "id": job, "name": Path(a.file).name, "label": "test",
                       "sha": hashlib.sha256(data).hexdigest()})
        url = f"{rp.RELAY}/{ch.topic}?filename=job.bin&message={urllib.parse.quote(meta)}"
        http(url, ch.encrypt(data), "PUT")
        want = lambda m: m.get("t") == "status" and m.get("id") == job and m.get("state") in ("done", "error")

    deadline = time.time() + a.timeout
    while time.time() < deadline:
        for line in http(f"{rp.RELAY}/{ch.topic}-s/json?poll=1&since={start}").decode().splitlines():
            ev = json.loads(line)
            try:
                msg = ch.open_json(ev.get("message", ""))
            except Exception:
                continue
            if want(msg):
                print("REPLY:", json.dumps(msg, ensure_ascii=False))
                att = ev.get("attachment")
                if att:      # screenshot: download, decrypt, check it is a JPEG
                    img = ch.decrypt(http(att["url"]))
                    print(f"SCREENSHOT: {len(img)} bytes, jpeg={img[:2] == bytes([0xFF, 0xD8])}")
                if a.mode == "shot":
                    return 0
                ok = msg.get("state", "done") == "done"
                return 0 if ok != a.expect_error else 1
        time.sleep(3)
    print("TIMEOUT - no reply")
    return 1


if __name__ == "__main__":
    sys.exit(main())

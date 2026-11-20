#!/usr/bin/env python3
"""Test client: talks to a running Remote Printer exactly like the website does.

    python send_test.py --password PW ping
    python send_test.py --password PW print FILE.pdf
Exits 0 when the expected reply arrives (pong / status "done").
"""
import argparse
import json
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import remote_printer as rp   # noqa: E402


def http(url, data=None, method=None):
    req = urllib.request.Request(url, data=data, method=method, headers={"User-Agent": "rp-test"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--password", required=True)
    ap.add_argument("--timeout", type=int, default=90)
    ap.add_argument("mode", choices=["ping", "print"])
    ap.add_argument("file", nargs="?")
    a = ap.parse_args()

    ch = rp.Channel(*rp.derive_channel(a.password))
    start = int(time.time()) - 2
    if a.mode == "ping":
        http(f"{rp.RELAY}/{ch.topic}", ch.seal_json({"t": "ping"}).encode(), "POST")
        want = lambda m: m.get("t") == "pong"
    else:
        job = "test" + str(int(time.time()))
        meta = ch.seal_json({"t": "print", "id": job, "name": Path(a.file).name, "label": "test"})
        url = f"{rp.RELAY}/{ch.topic}?filename=job.bin&message={urllib.parse.quote(meta)}"
        http(url, ch.encrypt(Path(a.file).read_bytes()), "PUT")
        want = lambda m: m.get("t") == "status" and m.get("id") == job and m.get("state") in ("done", "error")

    deadline = time.time() + a.timeout
    while time.time() < deadline:
        lines = http(f"{rp.RELAY}/{ch.topic}-s/json?poll=1&since={start}").decode().splitlines()
        for line in lines:
            ev = json.loads(line)
            try:
                msg = ch.open_json(ev.get("message", ""))
            except Exception:
                continue
            if want(msg):
                print("REPLY:", json.dumps(msg, ensure_ascii=False))
                return 0 if msg.get("state", "done") == "done" else 1
        time.sleep(3)
    print("TIMEOUT - no reply")
    return 1


if __name__ == "__main__":
    sys.exit(main())

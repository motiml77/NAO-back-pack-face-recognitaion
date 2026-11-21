#!/usr/bin/env python3
"""Tests of the encryption of replies (screenshots, printer state).

The channel key is public (docs/channel.json), so on its own it protects nothing. Replies are encrypted to the
X25519 key of the device that sent the (signed) command. The decryption here is written separately from the
program's own helper, straight from the format description, so a mistake cannot hide in both.

    python tools/test_reply_crypto.py
"""
import base64
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import remote_printer as rp  # noqa: E402
from cryptography.exceptions import InvalidTag  # noqa: E402
from cryptography.hazmat.primitives import hashes, serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey  # noqa: E402
from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # noqa: E402
from cryptography.hazmat.primitives.kdf.hkdf import HKDF  # noqa: E402

RAW = dict(encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw)


def spec_decrypt(device_priv, blob):
    """e_pub(32) | iv(12) | ciphertext+tag, key = HKDF-SHA256(X25519, salt = e_pub|rk, info), aad = e_pub|rk"""
    rk = device_priv.public_key().public_bytes(**RAW)
    e_pub, iv, ct = blob[:32], blob[32:44], blob[44:]
    shared = device_priv.exchange(X25519PublicKey.from_public_bytes(e_pub))
    key = HKDF(algorithm=hashes.SHA256(), length=32, salt=e_pub + rk, info=b"remote-printer/reply/v1").derive(shared)
    return AESGCM(key).decrypt(iv, ct, e_pub + rk)


def check(name, ok):
    print(("PASS  " if ok else "FAIL  ") + name)
    if not ok:
        check.failed = True


check.failed = False

device = X25519PrivateKey.generate()
rk = device.public_key().public_bytes(**RAW)
other = X25519PrivateKey.generate()          # somebody else: another device, or an eavesdropper who knows the channel key
photo = os.urandom(200_000)                   # stands for a JPEG screenshot

blob = rp.encrypt_reply(rk, photo)
check("1. the device decrypts what the program encrypted (independent implementation)", spec_decrypt(device, blob) == photo)
check("2. the program's own helper agrees", rp.decrypt_reply(device, blob) == photo)
check("3. the screenshot is not visible in the encrypted bytes", photo[:64] not in blob)

try:
    spec_decrypt(other, blob)
    check("4. another device cannot decrypt", False)
except Exception:
    check("4. another device cannot decrypt", True)

ch = rp.Channel(rp.CHANNEL_TOPIC, rp.CHANNEL_KEY, None)      # anybody who reads channel.json has exactly this
try:
    ch.decrypt(blob)
    check("5. the PUBLIC channel key cannot decrypt a reply", False)
except Exception:
    check("5. the PUBLIC channel key cannot decrypt a reply", True)

tampered = bytearray(blob)
tampered[-1] ^= 1
try:
    spec_decrypt(device, bytes(tampered))
    check("6. a modified reply is rejected", False)
except InvalidTag:
    check("6. a modified reply is rejected", True)

check("7. every message uses a fresh key (same input, different bytes)", rp.encrypt_reply(rk, b"x") != rp.encrypt_reply(rk, b"x"))

# the key arrives inside a signed command as base64; garbage must never be accepted
good = base64.b64encode(rk).decode()
check("8. parse_rk accepts a valid key", rp.parse_rk(good) == rk)
check("9. parse_rk rejects garbage",
      all(rp.parse_rk(v) is None for v in (None, "", "!!!", base64.b64encode(b"short").decode(), good + "AA")))

# a whole reply message as the website receives it
text = base64.b64encode(rp.encrypt_reply(rk, json.dumps({"t": "pong", "host": "PC"}).encode())).decode()
check("10. a pong round-trips as JSON", json.loads(spec_decrypt(device, base64.b64decode(text))) == {"t": "pong", "host": "PC"})

sys.exit(1 if check.failed else 0)

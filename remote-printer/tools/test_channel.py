#!/usr/bin/env python3
"""Print config lines for a throw-away test channel: python test_channel.py PASSWORD"""
import secrets
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import remote_printer as rp  # noqa: E402
from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402

topic = "rptest" + secrets.token_hex(12)
key = secrets.token_hex(32)
pub = Ed25519PrivateKey.from_private_bytes(rp.derive_signing_seed(sys.argv[1], topic)).public_key()
raw = pub.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw).hex()
print(f"topic = {topic}\nkey = {key}\nverify_key = {raw}")

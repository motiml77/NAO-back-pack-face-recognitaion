#!/usr/bin/env python3
"""Set (or change) the website password.

    python set_password.py "the new password"

Writes ONLY the public verification key to docs/channel.json - the password itself
is not stored anywhere. Publish the site (push) and the printer computer picks the
new key up by itself within a few minutes. No reinstall needed.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import remote_printer as rp   # noqa: E402
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402
from cryptography.hazmat.primitives import serialization  # noqa: E402

SITE_CHANNEL = Path(__file__).resolve().parents[2] / "docs" / "channel.json"


def public_key_hex(password: str) -> str:
    priv = Ed25519PrivateKey.from_private_bytes(rp.derive_signing_seed(password))
    return priv.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw).hex()


def main():
    if len(sys.argv) != 2 or len(sys.argv[1].strip()) < 6:
        sys.exit("usage: set_password.py PASSWORD   (at least 6 characters)")
    data = {
        "topic": rp.CHANNEL_TOPIC,
        "key": rp.CHANNEL_KEY,
        "verify": public_key_hex(sys.argv[1]),
        "iterations": rp.SIGN_ITERATIONS,
        "salt": f"remote-printer/sign/v3|{rp.CHANNEL_TOPIC}",
        "relay": rp.RELAY,
    }
    SITE_CHANNEL.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {SITE_CHANNEL} (verify key {data['verify'][:16]}...)")


if __name__ == "__main__":
    main()

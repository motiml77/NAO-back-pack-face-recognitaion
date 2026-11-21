#!/usr/bin/env python3
"""Reproduces and checks the HTTPS failure seen on a real computer:

  CERTIFICATE_VERIFY_FAILED: unable to get local issuer certificate

happens when the Windows certificate store lacks a root authority that no program asked Windows to fetch yet.
  1. with NO trusted roots, the connection must fail like that (this is the PC from the log);
  2. with only the roots bundled in the program (certifi), it must work.

Needs real internet (a filtering proxy that re-signs HTTPS breaks check 2) - run in CI.
"""
import ssl
import sys
import urllib.request

import certifi

URLS = ["https://ntfy.sh/v1/health", "https://motiml77.github.io/NAO-back-pack-face-recognitaion/channel.json"]


def get(url, ctx):
    opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=ctx))
    return opener.open(url, timeout=30).status


no_roots = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)          # trusts nothing
bundled = ssl.create_default_context(cafile=certifi.where())  # trusts only the bundled roots
ok = True
for url in URLS:
    host = url.split("/")[2]
    try:
        get(url, no_roots)
        print("UNEXPECTED: %s worked without any roots" % host)
        ok = False
    except Exception as e:
        print("no roots : %-22s fails as on the user's PC -> %s" % (host, str(e)[:100]))
    try:
        print("bundled  : %-22s works, HTTP %s" % (host, get(url, bundled)))
    except Exception as e:
        print("bundled  : %-22s FAILED -> %s" % (host, e))
        ok = False
sys.exit(0 if ok else 1)

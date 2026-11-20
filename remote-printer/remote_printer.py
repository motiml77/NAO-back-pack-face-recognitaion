#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Remote Printer - the program that runs on the computer next to the printer.

It listens (hidden, in the background) for print jobs sent from the
"הדפסה מרחוק" website and prints them immediately, without any dialog.

How the link works
------------------
The website and this program talk through ntfy.sh, a free public relay.
Everything is end-to-end encrypted (AES-256-GCM) with a key derived from the
password chosen during installation, and the channel name is derived from it
as well. Messages that are not encrypted with that key are ignored, so only
someone who knows the password can print. Only outgoing connections are used,
so no router or firewall configuration is needed.

Command line (normally not needed - the installer does everything):
    RemotePrinter.exe                    run (reads config.ini in the program folder)
    RemotePrinter.exe --test-print FILE  print one local file
    RemotePrinter.exe --selftest         check the installation (writes selftest.txt)
"""

import argparse
import base64
import configparser
import hashlib
import json
import logging
import logging.handlers
import os
import queue
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

VERSION = "2.0"
FROZEN = getattr(sys, "frozen", False)
BASE_DIR = Path(sys.executable).resolve().parent if FROZEN else Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.ini"
STATE_PATH = BASE_DIR / "state.json"
IS_WINDOWS = sys.platform.startswith("win")
IS_MAC = sys.platform == "darwin"

RELAY = "https://ntfy.sh"
KDF_ITERATIONS = 150000
MAX_JOB_AGE_SEC = 12 * 3600       # ignore jobs older than this (e.g. after a long shutdown)
MAX_FILE_BYTES = 16 * 1024 * 1024

PDF_EXT = {".pdf"}
WORD_EXT = {".doc", ".docx", ".rtf", ".odt"}
SUPPORTED_EXT = PDF_EXT | WORD_EXT

PRINT_TIMEOUT_SEC = 180
SINGLE_INSTANCE_PORT = 47631
CREATE_NO_WINDOW = 0x08000000
USER_AGENT = f"RemotePrinter/{VERSION}"

log = logging.getLogger("remote_printer")


class PrintError(Exception):
    pass


# --------------------------------------------------------------------------- helpers

def setup_logging():
    log.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(threadName)s: %(message)s")
    fh = logging.handlers.RotatingFileHandler(
        BASE_DIR / "remote_printer.log", maxBytes=1_000_000, backupCount=2, encoding="utf-8")
    fh.setFormatter(fmt)
    log.addHandler(fh)
    if sys.stderr is not None:
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        log.addHandler(sh)


def load_config() -> configparser.ConfigParser:
    cfg = configparser.ConfigParser(interpolation=None)
    if CONFIG_PATH.exists():
        cfg.read(CONFIG_PATH, encoding="utf-8-sig")
    if not cfg.has_section("general"):
        cfg.add_section("general")
    return cfg


def save_config(cfg: configparser.ConfigParser):
    tmp = CONFIG_PATH.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        cfg.write(f)
    os.replace(tmp, CONFIG_PATH)


def safe_filename(name: str) -> str:
    name = os.path.basename((name or "").replace("\\", "/"))
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")
    stem, ext = os.path.splitext(name)
    stem = stem[:80] or "document"
    return f"{datetime.now():%Y%m%d-%H%M%S}_{stem}{ext.lower()}"


def unique_path(folder: Path, filename: str) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / filename
    n = 1
    while path.exists():
        path = folder / f"{Path(filename).stem}_{n}{Path(filename).suffix}"
        n += 1
    return path


def run_quiet(args, timeout=PRINT_TIMEOUT_SEC):
    kwargs = {"capture_output": True, "timeout": timeout}
    if IS_WINDOWS:
        kwargs["creationflags"] = CREATE_NO_WINDOW
    try:
        res = subprocess.run([str(a) for a in args], **kwargs)
    except subprocess.TimeoutExpired:
        raise PrintError(f"{Path(str(args[0])).name} did not finish within {timeout} seconds")
    if res.returncode != 0:
        err = (res.stderr or res.stdout or b"").decode(errors="replace").strip()
        raise PrintError(f"{Path(str(args[0])).name} failed (code {res.returncode}) {err[:300]}")
    return res


def acquire_single_instance_lock():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if IS_WINDOWS and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
        s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    try:
        s.bind(("127.0.0.1", SINGLE_INSTANCE_PORT))
        s.listen(1)
        return s
    except OSError:
        s.close()
        return None


def list_printers():
    """Returns (printers, default_printer)."""
    if IS_WINDOWS:
        try:
            import win32print
            flags = win32print.PRINTER_ENUM_LOCAL | win32print.PRINTER_ENUM_CONNECTIONS
            names = sorted({p[2] for p in win32print.EnumPrinters(flags)})
            try:
                default = win32print.GetDefaultPrinter()
            except Exception:
                default = ""
            return names, default
        except Exception as e:
            log.warning("Could not list printers: %s", e)
            return [], ""
    names, default = [], ""
    try:
        out = subprocess.run(["lpstat", "-e"], capture_output=True, text=True, timeout=10).stdout
        names = sorted(n.strip() for n in out.splitlines() if n.strip())
        out = subprocess.run(["lpstat", "-d"], capture_output=True, text=True, timeout=10).stdout
        default = out.split(":", 1)[1].strip() if ":" in out else ""
    except Exception:
        pass
    return names, default


# --------------------------------------------------------------------------- printing

class Printer:
    def __init__(self, cfg: configparser.ConfigParser):
        g = "general"
        self.printer_name = cfg.get(g, "printer_name", fallback="").strip()
        self.dry_run = cfg.getboolean(g, "dry_run", fallback=False)
        self.sumatra = self._find_sumatra(cfg.get(g, "sumatra_path", fallback="").strip())
        self.soffice = self._find_soffice(cfg.get(g, "libreoffice_path", fallback="").strip())

    # ---- discovery
    @staticmethod
    def _find_sumatra(configured):
        if not IS_WINDOWS:
            return None
        candidates = [configured] if configured else []
        candidates += [str(p) for p in sorted(BASE_DIR.glob("SumatraPDF*.exe"))]
        for env in ("LOCALAPPDATA", "ProgramFiles", "ProgramFiles(x86)"):
            if os.environ.get(env):
                candidates.append(os.path.join(os.environ[env], "SumatraPDF", "SumatraPDF.exe"))
        candidates.append(shutil.which("SumatraPDF") or "")
        return next((c for c in candidates if c and os.path.isfile(c)), None)

    @staticmethod
    def _find_soffice(configured):
        candidates = [configured] if configured else []
        candidates += [shutil.which("soffice") or "", shutil.which("libreoffice") or ""]
        if IS_WINDOWS:
            for env in ("ProgramFiles", "ProgramFiles(x86)"):
                if os.environ.get(env):
                    candidates.append(os.path.join(os.environ[env], "LibreOffice", "program", "soffice.exe"))
        if IS_MAC:
            candidates.append("/Applications/LibreOffice.app/Contents/MacOS/soffice")
        return next((c for c in candidates if c and os.path.isfile(c)), None)

    def describe(self):
        name = self.printer_name
        if not name and IS_WINDOWS:
            try:
                import win32print
                name = win32print.GetDefaultPrinter() + " (ברירת מחדל)"
            except Exception:
                pass
        return name or "מדפסת ברירת המחדל"

    def log_setup(self):
        log.info("Printer: %s | dry_run=%s", self.printer_name or "<system default>", self.dry_run)
        if IS_WINDOWS:
            log.info("SumatraPDF: %s", self.sumatra or "NOT FOUND (PDF will use the default PDF app)")
        log.info("LibreOffice: %s", self.soffice or "not found")

    # ---- entry point
    def print_file(self, path: Path):
        ext = path.suffix.lower()
        if ext not in SUPPORTED_EXT:
            raise PrintError(f"Unsupported file type: {ext}")
        if self.dry_run:
            log.info("[DRY RUN] would print: %s", path)
            return
        if ext in PDF_EXT:
            self._print_pdf(path)
        else:
            self._print_word(path)

    # ---- PDF
    def _print_pdf(self, path: Path):
        if IS_WINDOWS:
            if self.sumatra:
                target = ["-print-to", self.printer_name] if self.printer_name else ["-print-to-default"]
                run_quiet([self.sumatra, *target, "-print-settings", "fit", "-silent", path])
                return
            log.warning("SumatraPDF not found - falling back to the default PDF program")
            self._shell_print(path)
        else:
            self._lp(path)

    def _lp(self, path: Path):
        if shutil.which("lp"):
            run_quiet(["lp", *(["-d", self.printer_name] if self.printer_name else []), path])
        elif shutil.which("lpr"):
            run_quiet(["lpr", *(["-P", self.printer_name] if self.printer_name else []), path])
        else:
            raise PrintError("Neither 'lp' nor 'lpr' found - is CUPS installed?")

    @staticmethod
    def _shell_print(path: Path):
        try:
            os.startfile(str(path), "print")  # type: ignore[attr-defined]
        except OSError as e:
            raise PrintError(f"Windows could not print this file type ({e})")
        time.sleep(15)  # give the external program time to spool before the next job

    # ---- Word
    def _print_word(self, path: Path):
        if IS_WINDOWS:
            try:
                import win32com.client  # noqa: F401  (pywin32)
                have_pywin32 = True
            except ImportError:
                have_pywin32 = False
            if have_pywin32:
                try:
                    self._print_word_com(path)
                    return
                except PrintError:
                    raise
                except Exception as e:
                    if not self.soffice:
                        raise PrintError(f"Microsoft Word could not print the file: {e}")
                    log.warning("Word automation failed (%s) - trying LibreOffice", e)
            if self.soffice:
                self._print_via_pdf(path)
                return
            log.warning("pywin32/LibreOffice not available - falling back to Windows 'print' verb")
            self._shell_print(path)
        else:
            if not self.soffice:
                raise PrintError("LibreOffice is required to print Word files on this system")
            self._print_via_pdf(path)

    def _print_via_pdf(self, path: Path):
        """Convert with LibreOffice (headless, isolated profile) and print the PDF."""
        with tempfile.TemporaryDirectory(prefix="remote_printer_") as tmp:
            profile = Path(tmp, "profile").as_uri()
            run_quiet([self.soffice, f"-env:UserInstallation={profile}", "--headless", "--norestore",
                       "--convert-to", "pdf", "--outdir", tmp, path])
            pdf = Path(tmp, path.stem + ".pdf")
            if not pdf.exists():
                raise PrintError("LibreOffice did not produce a PDF")
            self._print_pdf(pdf)

    def _print_word_com(self, path: Path):
        import pythoncom
        import win32com.client

        pythoncom.CoInitialize()
        word = doc = watchdog = None
        old_printer = None
        killed = threading.Event()
        try:
            word = win32com.client.DispatchEx("Word.Application")  # separate, invisible instance
            word.Visible = False
            word.DisplayAlerts = 0                     # wdAlertsNone
            try:
                word.AutomationSecurity = 3            # msoAutomationSecurityForceDisable: never run macros
            except Exception:
                pass

            pid = self._word_pid(word)
            if pid:  # if Word gets stuck on a hidden dialog, kill it so the queue keeps working
                def _kill():
                    killed.set()
                    log.error("Word is stuck - killing process %s", pid)
                    subprocess.run(["taskkill", "/PID", str(pid), "/F", "/T"],
                                   capture_output=True, creationflags=CREATE_NO_WINDOW)
                watchdog = threading.Timer(PRINT_TIMEOUT_SEC, _kill)
                watchdog.daemon = True
                watchdog.start()

            if self.printer_name:
                old_printer = word.ActivePrinter
                word.ActivePrinter = self.printer_name

            doc = word.Documents.Open(
                FileName=str(path), ConfirmConversions=False, ReadOnly=True,
                AddToRecentFiles=False, Visible=False,
                PasswordDocument="__no_password__",   # password-protected files fail instead of prompting
                NoEncodingDialog=True)
            doc.PrintOut(Background=False)           # wait until the job is spooled
        except Exception as e:
            if killed.is_set():
                raise PrintError(f"Word did not finish within {PRINT_TIMEOUT_SEC} seconds") from e
            raise
        finally:
            if watchdog:
                watchdog.cancel()
            if not killed.is_set():
                for action in (
                    lambda: doc and doc.Close(SaveChanges=0),
                    lambda: old_printer and setattr(word, "ActivePrinter", old_printer),
                    lambda: word and word.Quit(SaveChanges=0),
                ):
                    try:
                        action()
                    except Exception:
                        pass
            pythoncom.CoUninitialize()

    @staticmethod
    def _word_pid(word):
        try:
            import win32gui
            import win32process
            marker = f"RemotePrinter-{os.getpid()}-{time.time()}"
            word.Caption = marker
            hwnd = win32gui.FindWindow("OpusApp", marker)
            if hwnd:
                return win32process.GetWindowThreadProcessId(hwnd)[1]
        except Exception:
            pass
        return None


class PrintQueue:
    """All jobs are printed one at a time, by one worker thread."""

    def __init__(self, printer: Printer, keep_files: bool):
        self.printer = printer
        self.keep_files = keep_files
        self.q = queue.Queue()
        threading.Thread(target=self._worker, name="printer", daemon=True).start()

    def submit(self, path: Path, source: str, notify=None):
        log.info("Queued %s (from %s)", path.name, source)
        self.q.put((path, source, notify))

    def pending(self):
        return self.q.qsize()

    def _worker(self):
        while True:
            path, source, notify = self.q.get()
            ok, err = True, None
            try:
                self.printer.print_file(path)
                log.info("Printed %s (from %s)", path.name, source)
            except Exception as e:
                ok, err = False, str(e)
                log.exception("Printing %s failed", path.name)
            finally:
                if not self.keep_files:
                    try:
                        path.unlink()
                    except OSError:
                        pass
            if notify:
                try:
                    notify(ok, err)
                except Exception:
                    log.exception("Could not send print result")

# --------------------------------------------------------------------------- secure channel

def derive_channel(password: str):
    """Same derivation as the website (docs/link.js)."""
    pw = unicodedata.normalize("NFC", password).strip().encode("utf-8")
    topic = "rp" + hashlib.pbkdf2_hmac("sha256", pw, b"remote-printer/topic/v1", KDF_ITERATIONS, 16).hex()
    key = hashlib.pbkdf2_hmac("sha256", pw, b"remote-printer/key/v1", KDF_ITERATIONS, 32).hex()
    return topic, key


class Channel:
    def __init__(self, topic: str, key_hex: str):
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        self.topic = topic
        self.aes = AESGCM(bytes.fromhex(key_hex))

    def encrypt(self, data: bytes) -> bytes:
        iv = os.urandom(12)
        return iv + self.aes.encrypt(iv, data, None)

    def decrypt(self, blob: bytes) -> bytes:
        return self.aes.decrypt(blob[:12], blob[12:], None)

    def seal_json(self, obj) -> str:
        obj = dict(obj, ts=int(time.time() * 1000))
        return base64.b64encode(self.encrypt(json.dumps(obj, ensure_ascii=False).encode("utf-8"))).decode()

    def open_json(self, text: str):
        return json.loads(self.decrypt(base64.b64decode(text)).decode("utf-8"))

    def reply(self, obj):
        """Send an encrypted message to the website (status / pong)."""
        body = self.seal_json(obj).encode()
        for attempt in range(4):
            req = urllib.request.Request(f"{RELAY}/{self.topic}-s", data=body,
                                         headers={"User-Agent": USER_AGENT}, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=20) as r:
                    r.read()
                return
            except urllib.error.HTTPError as e:
                if e.code != 429 or attempt == 3:
                    log.warning("Could not send reply to the website: %s", e)
                    return
                time.sleep(3 * (attempt + 1))     # rate limited - try again shortly
            except Exception as e:
                log.warning("Could not send reply to the website: %s", e)
                return


def channel_from_config(cfg) -> Channel:
    g = cfg["general"]
    password = g.get("password", "").strip()
    if password:   # first start after installation: keep only the derived values, not the password
        topic, key = derive_channel(password)
        g["channel"], g["key"] = topic, key
        g["password"] = ""
        save_config(cfg)
        log.info("Password converted to channel key.")
    topic, key = g.get("channel", "").strip(), g.get("key", "").strip()
    if not (topic and key):
        raise SystemExit("No password configured - run the installer again.")
    return Channel(topic, key)


# --------------------------------------------------------------------------- agent

class Agent:
    def __init__(self, cfg, channel: Channel, printer: Printer, pq: PrintQueue):
        self.cfg = cfg
        self.ch = channel
        self.printer = printer
        self.pq = pq
        self.inbox = BASE_DIR / "received"
        self.state = self._load_state()
        self.state_lock = threading.Lock()

    # ---- persistent state (last message seen, jobs already handled)
    @staticmethod
    def _load_state():
        try:
            st = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except Exception:
            st = {}
        st.setdefault("last_id", "")
        st.setdefault("jobs", [])
        return st

    def _save_state(self):
        with self.state_lock:
            self.state["jobs"] = self.state["jobs"][-300:]
            tmp = STATE_PATH.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.state), encoding="utf-8")
            os.replace(tmp, STATE_PATH)

    # ---- main loop
    def run(self):
        delay = 5
        while True:
            since = self.state["last_id"] or "10m"
            url = f"{RELAY}/{self.ch.topic}/json?since={urllib.parse.quote(since)}"
            try:
                req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
                with urllib.request.urlopen(req, timeout=120) as stream:
                    log.info("Connected - waiting for print jobs.")
                    delay = 5
                    for line in self._lines(stream):   # ntfy sends a keepalive every ~45s
                        ev = json.loads(line)
                        if ev.get("event") != "message":
                            continue
                        try:
                            self.handle(ev)
                        except Exception:
                            log.exception("Error handling message")
                        self.state["last_id"] = ev.get("id", self.state["last_id"])
                        self._save_state()
            except Exception as e:
                log.warning("Connection problem: %s (retry in %ss)", e, delay)
                time.sleep(delay)
                delay = min(delay * 2, 120)

    @staticmethod
    def _lines(stream):
        """Yield each line as soon as it arrives (plain readline waits for more data)."""
        buf = b""
        while True:
            chunk = stream.read1(65536)
            if not chunk:
                return
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                if line.strip():
                    yield line

    def handle(self, ev):
        try:
            msg = self.ch.open_json(ev.get("message", ""))
        except Exception:
            log.warning("Ignored a message that is not encrypted with our key")
            return
        age = time.time() - msg.get("ts", 0) / 1000
        if age > MAX_JOB_AGE_SEC or age < -600:
            log.info("Ignored an old message (%.0f minutes)", age / 60)
            return
        kind = msg.get("t")
        if kind == "ping":
            self.pong()
        elif kind == "settings":
            self.apply_settings(msg)
        elif kind == "print":
            self.receive_job(msg, ev.get("attachment") or {})

    def pong(self):
        printers, default = list_printers()
        self.ch.reply({"t": "pong", "printers": printers, "default": default,
                       "printer": self.printer.printer_name, "host": socket.gethostname(),
                       "version": VERSION})

    def apply_settings(self, msg):
        name = (msg.get("printer") or "").strip()
        printers, _ = list_printers()
        if name and printers and name not in printers:
            log.warning("Unknown printer from website: %s", name)
            return
        self.printer.printer_name = name
        self.cfg["general"]["printer_name"] = name
        save_config(self.cfg)
        log.info("Printer set from website: %s", name or "<default>")
        self.pong()

    def receive_job(self, msg, att):
        job_id = str(msg.get("id", ""))[:40]
        if not job_id or job_id in self.state["jobs"]:
            return
        self.state["jobs"].append(job_id)
        self._save_state()
        label = msg.get("label") or msg.get("name") or job_id

        def status(state, error=""):
            self.ch.reply({"t": "status", "id": job_id, "state": state, "error": error})

        try:
            name = msg.get("name") or "document.pdf"
            if Path(name).suffix.lower() not in SUPPORTED_EXT:
                raise PrintError("סוג קובץ לא נתמך")
            url = att.get("url", "")
            if not url.startswith(RELAY + "/"):
                raise PrintError("הקובץ לא הגיע (אולי פג תוקפו)")
            if int(att.get("size") or 0) > MAX_FILE_BYTES:
                raise PrintError("הקובץ גדול מדי")
            data = self._download(url)
            content = self.ch.decrypt(data)
            path = unique_path(self.inbox, safe_filename(name))
            path.write_bytes(content)
        except Exception as e:
            log.error("Job %s (%s) failed: %s", job_id, label, e)
            status("error", str(e) if isinstance(e, PrintError) else "הקובץ לא התקבל כראוי")
            return

        log.info("Job received: %s", label)
        status("printing")

        def notify(ok, err):
            status("done" if ok else "error", "" if ok else (err or "")[:200])

        self.pq.submit(path, f"site/{label}", notify)

    @staticmethod
    def _download(url) -> bytes:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=120) as r:
            data = r.read(MAX_FILE_BYTES + 1)
        if len(data) > MAX_FILE_BYTES:
            raise PrintError("הקובץ גדול מדי")
        return data


# --------------------------------------------------------------------------- self test (used by the build)

def selftest() -> int:
    lines, ok = [], True

    def check(name, fn):
        nonlocal ok
        try:
            lines.append(f"OK   {name}: {fn()}")
        except Exception as e:
            ok = False
            lines.append(f"FAIL {name}: {e!r}")

    def crypto_roundtrip():
        topic, key = derive_channel("בדיקה-test-1234")
        ch = Channel(topic, key)
        assert ch.open_json(ch.seal_json({"x": "שלום"}))["x"] == "שלום"
        return topic

    check("crypto", crypto_roundtrip)
    check("printer list", lambda: list_printers())
    if IS_WINDOWS:
        check("pywin32", lambda: __import__("win32com.client").__name__)
        check("SumatraPDF", lambda: Printer._find_sumatra("") or (_ for _ in ()).throw(RuntimeError("missing")))
    lines.append("SELFTEST " + ("PASSED" if ok else "FAILED"))
    (BASE_DIR / "selftest.txt").write_text("\n".join(lines), encoding="utf-8")
    return 0 if ok else 1


# --------------------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description="Print documents sent from the remote printing website.")
    ap.add_argument("--test-print", metavar="FILE")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()

    if args.selftest:
        sys.exit(selftest())

    setup_logging()
    cfg = load_config()
    printer = Printer(cfg)
    printer.log_setup()

    if args.test_print:
        printer.print_file(Path(args.test_print).resolve())
        log.info("Test print sent.")
        return

    lock = acquire_single_instance_lock()
    if lock is None:
        log.error("Remote Printer is already running - exiting.")
        sys.exit(1)

    channel = channel_from_config(cfg)
    pq = PrintQueue(printer, cfg.getboolean("general", "keep_files", fallback=False))
    agent = Agent(cfg, channel, printer, pq)
    log.info("Remote Printer %s started.", VERSION)
    try:
        agent.run()
    except KeyboardInterrupt:
        log.info("Stopped.")


if __name__ == "__main__":
    try:
        main()
    except SystemExit as e:
        if e.code not in (None, 0) and not isinstance(e.code, int):
            log.error("%s", e.code)
        raise

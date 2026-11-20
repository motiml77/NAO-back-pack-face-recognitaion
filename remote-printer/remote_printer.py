#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Remote Printer - the program that runs on the computer next to the printer.

It listens (hidden, in the background) for print jobs sent from the
"הדפסה מרחוק" website and prints them immediately, without any dialog.

How the link works
------------------
The website and this program talk through ntfy.sh, a free public relay, on a
fixed channel that is built into the program (no setup, no password at install).
Messages are encrypted (AES-256-GCM), and every command from the website is
digitally signed (Ed25519) with a key derived from the website password.
This program only knows the matching *public* key, which it reads from the
website (channel.json), so it can check signatures but could never sign.
Unsigned or wrongly signed messages are ignored: only someone who knows the
website password can print. Changing the password = updating channel.json on
the website; nothing changes here. Only outgoing connections are used.

Command line (normally not needed - the installer does everything):
    RemotePrinter.exe                    run
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

CRYPTO_ERROR = ""
try:   # top-level imports so the packager bundles them
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    from cryptography.exceptions import InvalidSignature
except Exception as _e:   # pragma: no cover
    AESGCM = Ed25519PublicKey = None
    InvalidSignature = Exception
    CRYPTO_ERROR = repr(_e)

VERSION = "3.0"
FROZEN = getattr(sys, "frozen", False)
BASE_DIR = Path(sys.executable).resolve().parent if FROZEN else Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.ini"
STATE_PATH = BASE_DIR / "state.json"
IS_WINDOWS = sys.platform.startswith("win")
IS_MAC = sys.platform == "darwin"

RELAY = "https://ntfy.sh"
SITE_URL = "https://motiml77.github.io/NAO-back-pack-face-recognitaion/"
# Fixed channel shared with the website (docs/channel.json). Not a secret by itself:
# printing also requires a signature made with the website password.
CHANNEL_TOPIC = "rpaa34d41e65f38a30074f2c39639fb2f0"
CHANNEL_KEY = "2f99c66c2cc9af14cde8bf155f9757e913dfddbad07adba3b2c37befe2c983b5"
VERIFY_REFRESH_SEC = 600
SIGN_ITERATIONS = 600000


def derive_signing_seed(password: str, topic: str = CHANNEL_TOPIC) -> bytes:
    """Website password -> Ed25519 private seed. Same as docs/link.js. Used only by the tools;
    the running program never sees the password."""
    pw = unicodedata.normalize("NFC", password).strip().encode("utf-8")
    return hashlib.pbkdf2_hmac("sha256", pw, f"remote-printer/sign/v3|{topic}".encode(), SIGN_ITERATIONS, 32)
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


# --------------------------------------------------------------------------- printer health

VIRTUAL_PRINTERS = ("microsoft print to pdf", "microsoft xps", "onenote", "fax", "send to", "adobe pdf", "pdf24", "cutepdf")

PRINTER_PROBLEMS = [          # Windows PRINTER_STATUS_* flags
    (0x00000001, "המדפסת מושהית"),
    (0x00000002, "יש שגיאה במדפסת"),
    (0x00000008, "נייר תקוע במדפסת"),
    (0x00000010, "נגמר הנייר"),
    (0x00000020, "המדפסת מחכה להזנת נייר ידנית"),
    (0x00000080, "המדפסת לא מחוברת (Offline)"),
    (0x00001000, "המדפסת לא זמינה"),
    (0x00040000, "נגמר הדיו / הטונר"),
    (0x00100000, "המדפסת צריכה טיפול"),
    (0x00400000, "מכסה המדפסת פתוח"),
]
PRINTER_WARNINGS = [(0x00020000, "הדיו / הטונר עומד להיגמר")]

JOB_DONE = 0x0080 | 0x1000 | 0x0100                    # PRINTED | COMPLETE | DELETED
JOB_PROBLEMS = [(0x0040, "נגמר הנייר"), (0x0020, "המדפסת לא מחוברת"), (0x0400, "המדפסת צריכה טיפול"),
                (0x0002, "שגיאה בהדפסה"), (0x0200, "ההדפסה נחסמה"), (0x0001, "ההדפסה מושהית")]


PREFERRED_BRANDS = ("pantum",)      # the printer connected here - picked automatically


def is_virtual(name: str) -> bool:
    return any(v in name.lower() for v in VIRTUAL_PRINTERS)


def resolve_printer(configured: str):
    """Which printer to use. Returns (name, how) - how is 'chosen' / 'auto'.
    Chosen on the website wins; otherwise a Pantum printer, otherwise the Windows default
    (if it is a real printer), otherwise the first real printer."""
    printers, default = list_printers()
    if configured and configured in printers:
        return configured, "chosen"
    for brand in PREFERRED_BRANDS:
        for p in printers:
            if brand in p.lower():
                return p, "auto"
    if default and not is_virtual(default):
        return default, "auto"
    real = [p for p in printers if not is_virtual(p)]
    if real:
        return real[0], "auto"
    return (default or ""), "auto"


def word_installed() -> bool:
    if not IS_WINDOWS:
        return False
    try:
        import winreg
        winreg.CloseKey(winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, r"Word.Application\CLSID"))
        return True
    except OSError:
        return False


def printer_health(configured: str) -> dict:
    """Is the printer ready? Returns {name, how, ready, problems, warnings}."""
    printers, _ = list_printers()
    name, how = resolve_printer(configured)
    info = {"name": name, "how": how, "ready": False, "problems": [], "warnings": []}
    if not printers:
        info["problems"].append("לא נמצאה אף מדפסת במחשב")
        return info
    if configured and configured not in printers:
        info["warnings"].append(f"המדפסת שנבחרה (\"{configured}\") לא נמצאה - נבחרה מדפסת אחרת אוטומטית")
    if not name:
        info["problems"].append("לא נמצאה מדפסת - בדקו שהמדפסת מחוברת ודלוקה")
        return info
    if is_virtual(name):
        info["problems"].append("זו מדפסת וירטואלית ששומרת לקובץ - בחרו את המדפסת האמיתית")
    if IS_WINDOWS:
        try:
            import win32print
            h = win32print.OpenPrinter(name)
            try:
                d = win32print.GetPrinter(h, 2)
            finally:
                win32print.ClosePrinter(h)
            status, attrs = d.get("Status", 0), d.get("Attributes", 0)
            info["problems"] += [t for f, t in PRINTER_PROBLEMS if status & f]
            info["warnings"] += [t for f, t in PRINTER_WARNINGS if status & f]
            if attrs & 0x400:     # PRINTER_ATTRIBUTE_WORK_OFFLINE
                info["problems"].append("המדפסת מוגדרת במצב לא מקוון")
            if d.get("cJobs"):
                info["warnings"].append(f"יש {d['cJobs']} עבודות בתור של המדפסת")
        except Exception as e:
            info["problems"].append(f"לא ניתן לבדוק את המדפסת ({e})")
    else:
        try:
            out = subprocess.run(["lpstat", "-p", name], capture_output=True, text=True, timeout=10).stdout
            if "disabled" in out:
                info["problems"].append("המדפסת מושבתת")
        except Exception:
            pass
    info["ready"] = not info["problems"]
    return info


def spool_jobs(name: str) -> dict:
    """Windows print queue: {job_id: status}. Empty dict when not available."""
    if not (IS_WINDOWS and name):
        return {}
    try:
        import win32print
        h = win32print.OpenPrinter(name)
        try:
            return {j["JobId"]: j.get("Status", 0) for j in win32print.EnumJobs(h, 0, 999, 1)}
        finally:
            win32print.ClosePrinter(h)
    except Exception:
        return {}


def wait_for_printer(name: str, before: dict, timeout=300):
    """Follow our job in the Windows queue until the printer has finished it.
    Returns (ok, message)."""
    if not (IS_WINDOWS and name):
        return True, "נשלח למדפסת"
    ours, t0 = set(), time.time()
    while time.time() - t0 < 15 and not ours:       # wait for the job to show up in the queue
        ours = set(spool_jobs(name)) - set(before)
        if not ours:
            time.sleep(0.5)
    if not ours:     # very fast printers / drivers that hide finished jobs
        return True, "המדפסת קיבלה את הדף"
    problem_since = None
    while time.time() - t0 < timeout:
        jobs = spool_jobs(name)
        left = {j: st for j, st in jobs.items() if j in ours and not st & JOB_DONE}
        if not left:
            return True, "המדפסת סיימה להדפיס"
        problems = [t for st in left.values() for f, t in JOB_PROBLEMS if st & f]
        if problems:
            problem_since = problem_since or time.time()
            if time.time() - problem_since > 25:
                return False, problems[0] + " (הדף ממתין בתור ויודפס כשהבעיה תסודר)"
        else:
            problem_since = None
        time.sleep(1)
    return False, "ההדפסה עדיין ממתינה בתור של המדפסת"


# --------------------------------------------------------------------------- printing

class Printer:
    def __init__(self, cfg: configparser.ConfigParser):
        g = "general"
        self.configured = cfg.get(g, "printer_name", fallback="").strip()   # chosen on the website ("" = auto)
        self.printer_name = resolve_printer(self.configured)[0]
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
        log.info("Printer: %s | dry_run=%s", self.printer_name or "<none found>", self.dry_run)
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
            ok, err = True, "נשלח למדפסת"
            try:
                target = resolve_printer(self.printer.configured)[0]
                self.printer.printer_name = target
                if not target and not self.printer.dry_run:
                    raise PrintError("לא נמצאה מדפסת - בדקו שהמדפסת מחוברת ודלוקה")
                before = {} if self.printer.dry_run else spool_jobs(target)
                self.printer.print_file(path)
                if not self.printer.dry_run:
                    ok, err = wait_for_printer(target, before)
                else:
                    err = "מצב בדיקה - לא הודפס באמת"
                log.info("Printed %s (from %s): %s", path.name, source, err)
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

class VerifyKey:
    """The website's public signing key (docs/channel.json on the website), cached on disk."""

    def __init__(self, cfg, state_get, state_set):
        self.override = cfg.get("general", "verify_key", fallback="").strip().lower()   # for tests
        self.state_get, self.state_set = state_get, state_set
        self.hex = self.override or state_get("verify_key", "")
        self.fetched = 0.0

    def refresh(self):
        if self.override:
            return
        self.fetched = time.time()
        try:
            req = urllib.request.Request(SITE_URL + "channel.json?t=" + str(int(time.time())),
                                         headers={"User-Agent": USER_AGENT, "Cache-Control": "no-cache"})
            with urllib.request.urlopen(req, timeout=20) as r:
                data = json.loads(r.read().decode("utf-8"))
            if data.get("topic") != CHANNEL_TOPIC:
                log.warning("channel.json is for a different channel - ignored")
                return
            new = (data.get("verify") or "").strip().lower()
            if re.fullmatch(r"[0-9a-f]{64}", new):
                if new != self.hex:
                    log.info("Website signing key %s", "updated" if self.hex else "loaded")
                self.hex = new
                self.state_set("verify_key", new)
        except Exception as e:
            log.warning("Could not read the website key (%s) - using the saved one", e)

    def get(self):
        if not self.override and time.time() - self.fetched > VERIFY_REFRESH_SEC:
            self.refresh()
        return self.hex


class Channel:
    def __init__(self, topic: str, key_hex: str, verify):
        if AESGCM is None:
            raise SystemExit(f"The 'cryptography' package is missing: {CRYPTO_ERROR}")
        self.topic = topic
        self.aes = AESGCM(bytes.fromhex(key_hex))
        self.verify = verify

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

    def _check(self, key_hex, sig: bytes, payload: bytes):
        Ed25519PublicKey.from_public_bytes(bytes.fromhex(key_hex)).verify(sig, payload)

    def open_signed(self, text: str):
        """Decrypt a command from the website and check its signature. Raises on any problem."""
        env = self.open_json(text)
        payload = (env.get("p") or "").encode("utf-8")
        sig = base64.b64decode(env.get("s") or "")
        key_hex = self.verify.get()
        if not key_hex:
            raise PermissionError("no website key yet")
        try:
            self._check(key_hex, sig, payload)
        except InvalidSignature:
            if self.verify.override:
                raise
            self.verify.refresh()        # the password may have just been changed on the website
            self._check(self.verify.hex, sig, payload)
        return json.loads(payload)

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
        self.last_job = None

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

    def state_put(self, key, value):
        self.state[key] = value
        self._save_state()

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
            msg = self.ch.open_signed(ev.get("message", ""))
        except Exception as e:
            log.warning("Ignored a message without a valid website signature (%s)", type(e).__name__)
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
        health = printer_health(self.printer.configured)
        if IS_WINDOWS and not self.printer.sumatra:
            health["problems"].append("רכיב ההדפסה חסר - הריצו שוב את ההתקנה")
            health["ready"] = False
        self.ch.reply({"t": "pong", "printers": printers, "default": default,
                       "printer": self.printer.configured, "host": socket.gethostname(),
                       "version": VERSION, "health": health, "word": word_installed(),
                       "queue": self.pq.pending(), "last_job": self.last_job})

    def apply_settings(self, msg):
        name = (msg.get("printer") or "").strip()
        printers, _ = list_printers()
        if name and printers and name not in printers:
            log.warning("Unknown printer from website: %s", name)
            return
        self.printer.configured = name
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

        def status(state, error="", detail=""):
            self.ch.reply({"t": "status", "id": job_id, "state": state, "error": error, "detail": detail})

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
            if hashlib.sha256(content).hexdigest() != msg.get("sha", ""):
                raise PrintError("הקובץ לא תואם לחתימה")
            path = unique_path(self.inbox, safe_filename(name))
            path.write_bytes(content)
        except Exception as e:
            log.error("Job %s (%s) failed: %s", job_id, label, e)
            status("error", str(e) if isinstance(e, PrintError) else "הקובץ לא התקבל כראוי")
            return

        log.info("Job received: %s", label)
        status("printing", detail="המחשב קיבל את הדף ושלח אותו למדפסת")

        def notify(ok, detail):
            self.last_job = {"label": label[:80], "ok": ok, "detail": (detail or "")[:200],
                             "printer": self.printer.printer_name, "time": int(time.time() * 1000)}
            if ok:
                status("done", detail=detail)
            else:
                status("error", error=(detail or "")[:200])

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
        except BaseException as e:
            ok = False
            lines.append(f"FAIL {name}: {e!r}")

    def crypto_roundtrip():
        if AESGCM is None:
            raise RuntimeError(CRYPTO_ERROR)
        ch = Channel(CHANNEL_TOPIC, CHANNEL_KEY, None)
        assert ch.open_json(ch.seal_json({"x": "שלום"}))["x"] == "שלום"
        Ed25519PublicKey.from_public_bytes(bytes(32))
        return "AES-GCM + Ed25519"

    check("crypto", crypto_roundtrip)
    check("printer list", lambda: list_printers())
    check("printer health", lambda: printer_health(""))
    check("website key", lambda: VerifyKey(load_config(), lambda k, d: d, lambda k, v: None).get()[:12] or "not reachable")
    check("Word installed", word_installed)
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

    pq = PrintQueue(printer, cfg.getboolean("general", "keep_files", fallback=False))
    agent = Agent(cfg, None, printer, pq)
    verify = VerifyKey(cfg, lambda k, d: agent.state.get(k, d), agent.state_put)
    verify.refresh()
    g = cfg["general"]          # test installations may use their own channel
    agent.ch = Channel(g.get("topic", "").strip() or CHANNEL_TOPIC, g.get("key", "").strip() or CHANNEL_KEY, verify)
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
    except BaseException:   # no console in the installed program - leave a trace for support
        import traceback
        (BASE_DIR / "crash.txt").write_text(traceback.format_exc(), encoding="utf-8")
        log.exception("Crashed")
        raise

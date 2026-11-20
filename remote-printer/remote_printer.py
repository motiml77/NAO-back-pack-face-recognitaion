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

Runs as a Windows scheduled task under the SYSTEM account: it starts when the computer
boots - before anybody logs in - so printing works after any power cut or restart.
Things that need a person's desktop (screenshots, Microsoft Word) are done by a short
helper process that this program starts inside the logged-on user's session.

Command line (normally not needed - the installer does everything):
    RemotePrinter.exe                    run
    RemotePrinter.exe --test-print FILE  print one local file
    RemotePrinter.exe --selftest         check the installation (writes selftest.txt)
"""

import argparse
import base64
import configparser
import hashlib
import io
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

VERSION = "3.5"
FROZEN = getattr(sys, "frozen", False)
IS_WINDOWS = sys.platform.startswith("win")
IS_MAC = sys.platform == "darwin"
BASE_DIR = Path(sys.executable).resolve().parent if FROZEN else Path(__file__).resolve().parent   # program files
# Settings, logs and received files live apart from the program (Program Files is read-only for users).
DATA_DIR = (Path(os.environ.get("ProgramData", r"C:\ProgramData")) / "RemotePrinter") if (IS_WINDOWS and FROZEN) else BASE_DIR
CONFIG_PATH = DATA_DIR / "config.ini"
STATE_PATH = DATA_DIR / "state.json"
TASK_NAME = "RemotePrinter"
NO_USER = "אף משתמש לא מחובר ל-Windows במחשב, ולכן אי אפשר להפעיל את זה כרגע"
WAITING_NOTE = "אף משתמש לא מחובר ל-Windows. ההדפסה תצא מיד אחרי שמישהו ייכנס"

RELAY = "https://ntfy.sh"
SITE_URL = "https://motiml77.github.io/NAO-back-pack-face-recognitaion/"
# Fixed channel shared with the website (docs/channel.json). Not a secret by itself:
# printing also requires a signature made with the website password.
CHANNEL_TOPIC = "rp4b664a92fc23d7ad321f82c438f1e2c6"
CHANNEL_KEY = "92070395d36a1820bef31c8961a2797e2af8674736617e0fbbbcf33c28c87468"
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


def quiet_kwargs():
    """subprocess arguments that start a program with no window of any kind (console or GUI).
    EVERY subprocess call in this program must use them - tools/check_silent.py enforces it."""
    if not IS_WINDOWS:
        return {}
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = subprocess.SW_HIDE
    return {"creationflags": CREATE_NO_WINDOW, "startupinfo": si}
USER_AGENT = f"RemotePrinter/{VERSION}"

log = logging.getLogger("remote_printer")


class PrintError(Exception):
    pass


class NoUserSession(PrintError):
    """Nobody is logged on to Windows, so nothing can run in a user's session."""


# --------------------------------------------------------------------------- helpers

def setup_logging():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    log.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(threadName)s: %(message)s")
    fh = logging.handlers.RotatingFileHandler(
        DATA_DIR / "remote_printer.log", maxBytes=1_000_000, backupCount=2, encoding="utf-8")
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
    DATA_DIR.mkdir(parents=True, exist_ok=True)
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
    kwargs = {"capture_output": True, "timeout": timeout, **quiet_kwargs()}
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
        out = subprocess.run(["lpstat", "-e"], capture_output=True, text=True, timeout=10, **quiet_kwargs()).stdout
        names = sorted(n.strip() for n in out.splitlines() if n.strip())
        out = subprocess.run(["lpstat", "-d"], capture_output=True, text=True, timeout=10, **quiet_kwargs()).stdout
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
            out = subprocess.run(["lpstat", "-p", name], capture_output=True, text=True, timeout=10, **quiet_kwargs()).stdout
            if "disabled" in out:
                info["problems"].append("המדפסת מושבתת")
        except Exception:
            pass
    info["ready"] = not info["problems"]
    return info


def printer_flags(name: str) -> int:
    """Windows PRINTER_STATUS_* bits for a printer (0 if unknown)."""
    if not (IS_WINDOWS and name):
        return 0
    try:
        import win32print
        h = win32print.OpenPrinter(name)
        try:
            d = win32print.GetPrinter(h, 2)
        finally:
            win32print.ClosePrinter(h)
        return d.get("Status", 0) | (0x80 if d.get("Attributes", 0) & 0x400 else 0)
    except Exception:
        return 0


def is_system_account() -> bool:
    """True when running as LocalSystem (the installed scheduled task)."""
    if not IS_WINDOWS:
        return False
    try:
        import win32process
        import win32security
        tok = win32security.OpenProcessToken(win32process.GetCurrentProcess(), win32security.TOKEN_QUERY)
        sid = win32security.GetTokenInformation(tok, win32security.TokenUser)[0]
        return win32security.ConvertSidToStringSid(sid) == "S-1-5-18"
    except Exception:
        return False


def user_session_ready() -> bool:
    """True when somebody is logged on to Windows (the console session belongs to a user)."""
    if not IS_WINDOWS:
        return True
    try:
        import win32api
        import win32ts
        sid = win32ts.WTSGetActiveConsoleSessionId()
        if sid in (0xFFFFFFFF, -1):
            return False
        token = win32ts.WTSQueryUserToken(sid)
        win32api.CloseHandle(token)
        return True
    except Exception:
        return False


def run_in_user_session(args, timeout=60, program=None) -> int:
    """Run `program` (default: this program) with `args`, hidden, inside the logged-on user's desktop
    session. Needed for screenshots, Microsoft Word and printer drivers that only print for a real
    user - none of them work from the SYSTEM session. Returns the exit code.
    Raises NoUserSession if nobody is logged on."""
    import pywintypes
    import win32api
    import win32event
    import win32process
    import win32profile
    import win32ts

    sid = win32ts.WTSGetActiveConsoleSessionId()
    if sid in (0xFFFFFFFF, -1):
        raise NoUserSession(NO_USER)
    try:
        token = win32ts.WTSQueryUserToken(sid)
    except pywintypes.error:
        raise NoUserSession(NO_USER)
    try:
        env = win32profile.CreateEnvironmentBlock(token, False)
        si = win32process.STARTUPINFO()
        si.lpDesktop = "winsta0\\default"
        si.dwFlags = win32process.STARTF_USESHOWWINDOW
        si.wShowWindow = 0   # SW_HIDE
        prefix = [str(program)] if program else ([sys.executable] if FROZEN else [sys.executable, str(Path(__file__).resolve())])
        cmdline = subprocess.list2cmdline(prefix + [str(a) for a in args])
        h_proc, h_thread, _, _ = win32process.CreateProcessAsUser(
            token, None, cmdline, None, None, False, 0x08000000 | 0x00000400,   # NO_WINDOW | UNICODE_ENV
            env, str(BASE_DIR), si)
    finally:
        win32api.CloseHandle(token)
    try:
        win32api.CloseHandle(h_thread)
        if win32event.WaitForSingleObject(h_proc, int(timeout * 1000)) != win32event.WAIT_OBJECT_0:
            win32process.TerminateProcess(h_proc, 1)
            raise PrintError("פעולה בחלון המשתמש לא הסתיימה בזמן")
        return win32process.GetExitCodeProcess(h_proc)
    finally:
        win32api.CloseHandle(h_proc)


SHOT_ERROR = {"text": ""}


def _grab_screen():
    """JPEG bytes of the screen in THIS process, or None (sets SHOT_ERROR)."""
    try:
        from PIL import ImageGrab
        img = ImageGrab.grab(all_screens=True)
        if img.convert("L").getextrema()[1] < 8:
            SHOT_ERROR["text"] = "המסך שחור או נעול"
            return None
        img.thumbnail((1600, 1600))
        buf = io.BytesIO()
        img.convert("RGB").save(buf, "JPEG", quality=72, optimize=True)
        return buf.getvalue()
    except Exception as e:
        SHOT_ERROR["text"] = "לא ניתן לצלם את המסך (אולי המחשב נעול)"
        log.warning("Screenshot failed: %s", e)
        return None


def take_screenshot():
    """JPEG of the whole screen (all monitors), or None. Used to show the problem on the website."""
    SHOT_ERROR["text"] = ""
    if is_system_account():       # the screen belongs to the logged-on user - ask a helper in that session
        out = DATA_DIR / "tmp" / f"shot-{os.getpid()}-{int(time.time())}.jpg"
        try:
            out.parent.mkdir(parents=True, exist_ok=True)
            rc = run_in_user_session(["--screenshot", out], timeout=30)
            if rc == 0 and out.exists():
                return out.read_bytes()
            SHOT_ERROR["text"] = "המסך נעול או שחור, לא ניתן לצלם"
        except PrintError as e:
            SHOT_ERROR["text"] = str(e)
        except Exception as e:
            SHOT_ERROR["text"] = "לא ניתן לצלם את המסך"
            log.warning("Screenshot helper failed: %s", e)
        finally:
            try:
                out.unlink()
            except OSError:
                pass
        return None
    return _grab_screen()


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


def delete_spool_jobs(name: str, ids):
    """Remove jobs from the Windows print queue (used for jobs stuck in ERROR - they never print
    and would block everything behind them)."""
    try:
        import win32print
        h = win32print.OpenPrinter(name)
        try:
            for job_id in ids:
                try:
                    win32print.SetJob(h, job_id, 0, None, win32print.JOB_CONTROL_DELETE)
                except Exception as e:
                    log.warning("Could not remove job %s: %s", job_id, e)
        finally:
            win32print.ClosePrinter(h)
    except Exception as e:
        log.warning("Could not clean the print queue: %s", e)


def wait_for_printer(name: str, before: dict, timeout=300):
    """Follow our job in the Windows queue until the printer has finished it.
    Returns (ok, message, dead) - dead=True when the job ended in ERROR and was removed."""
    if not (IS_WINDOWS and name):
        return True, "נשלח למדפסת", False
    ours, t0 = set(), time.time()
    while time.time() - t0 < 15 and not ours:       # wait for the job to show up in the queue
        ours = set(spool_jobs(name)) - set(before)
        if not ours:
            time.sleep(0.5)
    if not ours:     # very fast printers / drivers that hide finished jobs
        return True, "המדפסת קיבלה את הדף", False
    problem_since = None
    logged = False
    while time.time() - t0 < timeout:
        jobs = spool_jobs(name)
        left = {j: st for j, st in jobs.items() if j in ours and not st & JOB_DONE}
        if not left:
            return True, "המדפסת סיימה להדפיס", False
        problems = [t for st in left.values() for f, t in JOB_PROBLEMS if st & f]
        flags = printer_flags(name)              # many printers report paper-out etc. only here
        problems += [t for f, t in PRINTER_PROBLEMS if flags & f]
        if problems:
            if not logged:
                logged = True
                log.warning("Print problem %s | job status %s | printer status 0x%x",
                            problems, {j: hex(st) for j, st in left.items()}, flags)
            problem_since = problem_since or time.time()
            if time.time() - problem_since > 20:
                dead = [j for j, st in left.items() if st & 0x2]    # JOB_STATUS_ERROR: will never print
                if dead:
                    delete_spool_jobs(name, dead)
                    return False, problems[0] + " (העבודה בוטלה כדי לא לחסום את התור)", True
                return False, problems[0] + " (הדף ממתין בתור ויודפס כשהבעיה תסודר)", False
        else:
            problem_since = None
        time.sleep(1)
    return False, "ההדפסה עדיין ממתינה בתור של המדפסת", False


# --------------------------------------------------------------------------- printing

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


def word_export_pdf(src: Path, dst: Path):
    """Hidden Microsoft Word opens the file and saves it as PDF. Word prints nothing and shows nothing.
    Needs a logged-on user's session (see run_in_user_session)."""
    import pythoncom
    import win32com.client

    pythoncom.CoInitialize()
    word = doc = watchdog = None
    killed = threading.Event()
    try:
        try:
            word = win32com.client.DispatchEx("Word.Application")      # separate, invisible instance
        except Exception as e:
            raise PrintError("Microsoft Word לא מותקן במחשב או שהוא לא נפתח") from e
        word.Visible = False
        word.DisplayAlerts = 0                                         # wdAlertsNone
        for attr, value in (("AutomationSecurity", 3),                 # never run macros
                            ("ScreenUpdating", False)):
            try:
                setattr(word, attr, value)
            except Exception:
                pass
        pid = _word_pid(word)
        if pid:      # if Word gets stuck on a hidden dialog, kill it so the queue keeps working
            def _kill():
                killed.set()
                log.error("Word is stuck - killing process %s", pid)
                subprocess.run(["taskkill", "/PID", str(pid), "/F", "/T"], capture_output=True, **quiet_kwargs())
            watchdog = threading.Timer(PRINT_TIMEOUT_SEC - 20, _kill)
            watchdog.daemon = True
            watchdog.start()
        doc = word.Documents.Open(
            FileName=str(src), ConfirmConversions=False, ReadOnly=True,
            AddToRecentFiles=False, Visible=False,
            PasswordDocument="__no_password__",       # password-protected files fail instead of asking
            NoEncodingDialog=True)
        try:
            doc.ExportAsFixedFormat(OutputFileName=str(dst), ExportFormat=17, OpenAfterExport=False, OptimizeFor=0)
        except Exception:
            doc.SaveAs2(FileName=str(dst), FileFormat=17)             # wdFormatPDF
    except Exception as e:
        if killed.is_set():
            raise PrintError("Word לא הגיב בזמן") from e
        raise
    finally:
        if watchdog:
            watchdog.cancel()
        if not killed.is_set():
            for action in (lambda: doc and doc.Close(SaveChanges=0), lambda: word and word.Quit(SaveChanges=0)):
                try:
                    action()
                except Exception:
                    pass
        pythoncom.CoUninitialize()


class Printer:
    def __init__(self, cfg: configparser.ConfigParser):
        g = "general"
        self.fallback_used = False  # this job was printed as SYSTEM because nobody is logged on
        self.user_only = False      # print only through a user's session (never as SYSTEM)
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
            if not self.sumatra:
                raise PrintError("רכיב ההדפסה (SumatraPDF) חסר - הריצו שוב את ההתקנה")
            target = ["-print-to", self.printer_name] if self.printer_name else ["-print-to-default"]
            args = [*target, "-print-settings", "fit", "-silent", path]
            if is_system_account():
                # Many printer drivers only print for a real user (a job owned by SYSTEM ends in an error),
                # so print as the logged-on user - hidden. Nobody logged on: print as SYSTEM.
                try:
                    rc = run_in_user_session(args, timeout=PRINT_TIMEOUT_SEC, program=self.sumatra)
                    if rc != 0:
                        raise PrintError(f"רכיב ההדפסה נכשל (קוד {rc})")
                    return
                except NoUserSession:
                    if self.user_only:
                        raise
                    self.fallback_used = True
                    log.info("Nobody is logged on - trying to print as SYSTEM")
            run_quiet([self.sumatra, *args])
        else:
            self._lp(path)

    def _lp(self, path: Path):
        if shutil.which("lp"):
            run_quiet(["lp", *(["-d", self.printer_name] if self.printer_name else []), path])
        elif shutil.which("lpr"):
            run_quiet(["lpr", *(["-P", self.printer_name] if self.printer_name else []), path])
        else:
            raise PrintError("Neither 'lp' nor 'lpr' found - is CUPS installed?")

    # ---- Word: Word (hidden) saves it as PDF, then it is printed silently like any PDF
    def _print_word(self, path: Path):
        if not IS_WINDOWS:
            if not self.soffice:
                raise PrintError("LibreOffice is required to print Word files on this system")
            self._print_via_pdf(path)
            return
        base = DATA_DIR / "tmp"
        base.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="word-", dir=base, ignore_cleanup_errors=True) as tmp:
            pdf = Path(tmp) / (path.stem + ".pdf")
            try:
                self._word_convert(path, pdf)
            except PrintError as e:
                if not self.soffice:
                    raise
                log.warning("Word could not convert the file (%s) - trying LibreOffice", e)
                self._print_via_pdf(path)
                return
            self._print_pdf(pdf)

    @staticmethod
    def _word_convert(path: Path, pdf: Path):
        if is_system_account():       # Word cannot run in the SYSTEM session - a helper does it in the user's
            err = pdf.with_suffix(".err")
            rc = run_in_user_session(["--word-to-pdf", path, pdf, err], timeout=PRINT_TIMEOUT_SEC)
            if rc != 0 or not pdf.exists():
                msg = err.read_text(encoding="utf-8").strip() if err.exists() else ""
                raise PrintError(msg or f"המרת קובץ Word נכשלה (קוד {rc})")
            return
        try:
            word_export_pdf(path, pdf)
        except PrintError:
            raise
        except Exception as e:
            raise PrintError(f"Microsoft Word לא הצליח לפתוח את הקובץ: {e}")
        if not pdf.exists():
            raise PrintError("Word לא הצליח להכין את הקובץ להדפסה")

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


class PrintQueue:
    """All jobs are printed one at a time, by one worker thread.

    A job that cannot be printed because nobody is logged on to Windows (and printing as SYSTEM did not
    work either) is parked, and printed automatically a few seconds after somebody logs on."""

    WAIT_MAX_SEC = 24 * 3600

    def __init__(self, printer: Printer, keep_files: bool, user_ready=None, poll=5.0, settle=10.0):
        self.printer = printer
        self.keep_files = keep_files
        self.user_ready = user_ready or user_session_ready
        self.poll, self.settle = poll, settle
        self.q = queue.Queue()
        self.waiting = []            # [(path, source, notify, since)]
        self.lock = threading.Lock()
        threading.Thread(target=self._worker, name="printer", daemon=True).start()
        threading.Thread(target=self._watch_login, name="login-watch", daemon=True).start()

    def submit(self, path: Path, source: str, notify=None, mode="auto"):
        """mode "auto": print as soon as possible. mode "user": only through a logged-on user."""
        log.info("Queued %s (from %s)", path.name, source)
        self.q.put((path, source, notify, mode))

    def pending(self):
        return self.q.qsize() + len(self.waiting)

    @staticmethod
    def _tell(notify, state, detail=""):
        if notify:
            try:
                notify(state, detail)
            except Exception:
                log.exception("Could not send print status")

    def _park(self, path, source, notify):
        with self.lock:
            self.waiting.append((path, source, notify, time.time()))
        log.info("Waiting for somebody to log on to Windows: %s", path.name)
        self._tell(notify, "waiting", WAITING_NOTE)

    def _worker(self):
        while True:
            path, source, notify, mode = self.q.get()
            state, detail, parked = "done", "נשלח למדפסת", False
            try:
                target = resolve_printer(self.printer.configured)[0]
                self.printer.printer_name = target
                if not target and not self.printer.dry_run:
                    raise PrintError("לא נמצאה מדפסת - בדקו שהמדפסת מחוברת ודלוקה")
                before = {} if self.printer.dry_run else spool_jobs(target)
                self.printer.fallback_used = False
                self.printer.user_only = (mode == "user")
                self.printer.print_file(path)
                if self.printer.dry_run:
                    detail = "מצב בדיקה - לא הודפס באמת"
                else:
                    ok, detail, dead = wait_for_printer(target, before)
                    if not ok:
                        if dead and self.printer.fallback_used:
                            parked = True          # printing as SYSTEM did not work: wait for a user
                        else:
                            state = "error"
                log.info("Printed %s (from %s): %s", path.name, source, detail)
            except NoUserSession:
                parked = True
            except Exception as e:
                state, detail = "error", str(e)
                log.exception("Printing %s failed", path.name)
            if parked:
                self._park(path, source, notify)       # the file is kept
                continue
            if not self.keep_files:
                try:
                    path.unlink()
                except OSError:
                    pass
            self._tell(notify, state, detail)

    def _expire(self):
        now = time.time()
        with self.lock:
            old = [w for w in self.waiting if now - w[3] > self.WAIT_MAX_SEC]
            self.waiting = [w for w in self.waiting if now - w[3] <= self.WAIT_MAX_SEC]
        for path, _source, notify, _since in old:
            self._tell(notify, "error", "לא הודפס: אף אחד לא נכנס ל-Windows תוך 24 שעות")
            try:
                path.unlink()
            except OSError:
                pass

    def _watch_login(self):
        """When somebody logs on, print everything that was waiting for them."""
        while True:
            time.sleep(self.poll)
            if not self.waiting:
                continue
            self._expire()
            try:
                if not (self.waiting and self.user_ready()):
                    continue
                time.sleep(self.settle)         # let the desktop and the user's printers finish loading
                if not self.user_ready():
                    continue
            except Exception:
                log.exception("Login check failed")
                continue
            with self.lock:
                jobs, self.waiting = self.waiting, []
            for path, source, notify, _since in jobs:
                log.info("Somebody logged on - printing %s now", path.name)
                self._tell(notify, "printing", "נכנסתם ל-Windows, מדפיס עכשיו")
                self.q.put((path, source, notify, "user"))


# --------------------------------------------------------------------------- secure channel

class VerifyKey:
    """The website's public signing key (docs/channel.json on the website), cached on disk."""

    def __init__(self, cfg, state_get, state_set):
        self.override = (cfg.get("general", "verify_key", fallback="").strip()
                         or os.environ.get("RP_VERIFY_KEY", "").strip()).lower()   # for tests
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
        interval = VERIFY_REFRESH_SEC if self.hex else 30       # no key yet (first start offline): retry quickly
        if not self.override and time.time() - self.fetched > interval:
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

    def reply(self, obj, attachment: bytes = None):
        """Send an encrypted message to the website (status / pong), optionally with an
        encrypted file (screenshot)."""
        if attachment:
            body = self.encrypt(attachment)
            url = f"{RELAY}/{self.topic}-s?filename=shot.bin&message={urllib.parse.quote(self.seal_json(obj))}"
            method = "PUT"
        else:
            body = self.seal_json(obj).encode()
            url = f"{RELAY}/{self.topic}-s"
            method = "POST"
        for attempt in range(4):
            req = urllib.request.Request(url, data=body, headers={"User-Agent": USER_AGENT}, method=method)
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
        self.inbox = DATA_DIR / "received"
        self.state = self._load_state()
        self.state_lock = threading.RLock()
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
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            tmp = STATE_PATH.with_suffix(".tmp")
            for _ in range(5):          # another thread may change the state while it is written
                try:
                    data = json.dumps(self.state)
                    break
                except RuntimeError:
                    time.sleep(0.01)
            tmp.write_text(data, encoding="utf-8")
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
        elif kind == "shot":
            shot = take_screenshot()
            self.ch.reply({"t": "shot", "id": str(msg.get("id", ""))[:40], "ok": bool(shot),
                           "error": "" if shot else (SHOT_ERROR["text"] or "לא ניתן לצלם את המסך")}, shot)
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

    def _status(self, job_id, state, error="", detail=""):
        obj = {"t": "status", "id": job_id, "state": state, "error": error, "detail": detail}
        shot = take_screenshot() if state == "error" else None
        obj["shot"] = bool(shot)
        self.ch.reply(obj, shot)

    def _remember_waiting(self, job_id, path, label):
        with self.state_lock:
            self.state.setdefault("waiting", {})[job_id] = {"path": str(path), "label": label, "since": int(time.time())}
            self._save_state()

    def _forget_waiting(self, job_id):
        with self.state_lock:
            if self.state.setdefault("waiting", {}).pop(job_id, None) is not None:
                self._save_state()

    def _make_notify(self, job_id, label, path):
        """Callback for the print queue: state is waiting / printing / done / error."""
        def notify(state, detail=""):
            if state in ("done", "error"):
                self.last_job = {"label": label[:80], "ok": state == "done", "detail": (detail or "")[:200],
                                 "printer": self.printer.printer_name, "time": int(time.time() * 1000)}
                self._forget_waiting(job_id)
            elif state == "waiting":
                self._remember_waiting(job_id, path, label)     # survives a restart of the program
            if state == "error":
                self._status(job_id, "error", error=(detail or "")[:200])
            else:
                self._status(job_id, state, detail=detail)
        return notify

    def restore_waiting(self):
        """Jobs that were waiting for somebody to log on when the program last stopped."""
        for job_id, w in list(self.state.get("waiting", {}).items()):
            path = Path(w.get("path", ""))
            notify = self._make_notify(job_id, w.get("label", job_id), path)
            if not path.exists() or time.time() - w.get("since", 0) > PrintQueue.WAIT_MAX_SEC:
                PrintQueue._tell(notify, "error", "לא הודפס: הקובץ כבר לא זמין")
                continue
            log.info("Restored a job that was waiting for a user: %s", w.get("label"))
            self.pq.submit(path, "restored", notify, mode="user")

    def receive_job(self, msg, att):
        job_id = str(msg.get("id", ""))[:40]
        if not job_id or job_id in self.state["jobs"]:
            return
        self.state["jobs"].append(job_id)
        self._save_state()
        label = msg.get("label") or msg.get("name") or job_id

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
            self._status(job_id, "error", error=str(e) if isinstance(e, PrintError) else "הקובץ לא התקבל כראוי")
            return

        log.info("Job received: %s", label)
        self._status(job_id, "printing", detail="המחשב קיבל את הדף ושלח אותו למדפסת")
        self.pq.submit(path, f"site/{label}", self._make_notify(job_id, label, path))

    @staticmethod
    def _download(url) -> bytes:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=120) as r:
            data = r.read(MAX_FILE_BYTES + 1)
        if len(data) > MAX_FILE_BYTES:
            raise PrintError("הקובץ גדול מדי")
        return data


# --------------------------------------------------------------------------- installation (admin)

def task_xml(exe: Path) -> str:
    """Scheduled task: SYSTEM, highest privileges, starts at boot (before login), at every logon and
    every 5 minutes (one copy only), restarts itself if it ever fails, no time limit, battery allowed."""
    from xml.sax.saxutils import escape
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>Remote Printer - prints documents sent from the website</Description>
  </RegistrationInfo>
  <Triggers>
    <BootTrigger>
      <Enabled>true</Enabled>
      <Delay>PT15S</Delay>
    </BootTrigger>
    <LogonTrigger>
      <Enabled>true</Enabled>
      <Delay>PT10S</Delay>
    </LogonTrigger>
    <TimeTrigger>
      <Repetition>
        <Interval>PT5M</Interval>
        <StopAtDurationEnd>false</StopAtDurationEnd>
      </Repetition>
      <StartBoundary>2020-01-01T00:00:00</StartBoundary>
      <Enabled>true</Enabled>
    </TimeTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>S-1-5-18</UserId>
      <RunLevel>HighestAvailable</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>true</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Priority>6</Priority>
    <RestartOnFailure>
      <Interval>PT1M</Interval>
      <Count>999</Count>
    </RestartOnFailure>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{escape(str(exe))}</Command>
      <WorkingDirectory>{escape(str(exe.parent))}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


def _run(args, **kw):
    kw.setdefault("capture_output", True)
    kw.setdefault("timeout", 120)
    kw = {**quiet_kwargs(), **kw}
    return subprocess.run([str(a) for a in args], **kw)


def do_setup() -> int:
    """Run once by the installer (as administrator): prepare everything so the program runs at every
    boot with full permissions, then start it immediately - no restart needed."""
    import ctypes
    if not IS_WINDOWS:
        print("Setup is for Windows only")
        return 1
    if not ctypes.windll.shell32.IsUserAnAdmin():
        log.error("Setup needs administrator rights")
        return 1
    exe = Path(sys.executable).resolve() if FROZEN else Path(__file__).resolve()
    tmp = DATA_DIR / "tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    # the helper processes that run as the logged-on user may write only into tmp
    r = _run(["icacls", tmp, "/grant", "*S-1-5-32-545:(OI)(CI)M"])
    log.info("Folder permissions: %s", "ok" if r.returncode == 0 else r.stdout.decode(errors="replace")[:200])

    # 1. the startup task (SYSTEM, at boot) - full definition, falling back to simple tasks
    xml_path = DATA_DIR / "task.xml"
    xml_path.write_text(task_xml(exe), encoding="utf-16")
    r = _run(["schtasks", "/Create", "/TN", TASK_NAME, "/XML", xml_path, "/F"])
    if r.returncode == 0:
        log.info("Startup task registered (boot + logon + every 5 minutes, SYSTEM)")
    else:
        log.warning("Full task definition refused (%s) - using simple tasks",
                    (r.stderr or r.stdout).decode(errors="replace").strip()[:300])
        quoted = f'"{exe}"'
        r1 = _run(["schtasks", "/Create", "/TN", TASK_NAME, "/SC", "ONSTART", "/DELAY", "0000:15",
                   "/RU", "SYSTEM", "/RL", "HIGHEST", "/TR", quoted, "/F"])
        r2 = _run(["schtasks", "/Create", "/TN", TASK_NAME + " Watchdog", "/SC", "MINUTE", "/MO", "5",
                   "/RU", "SYSTEM", "/RL", "HIGHEST", "/TR", quoted, "/F"])
        if r1.returncode or r2.returncode:
            log.error("Could not register the startup task")
            return 2
    xml_path.unlink(missing_ok=True)

    # 2. keep the computer reachable: no sleep / hibernate on mains power, no USB power saving (USB printers)
    for args in (["powercfg", "/change", "standby-timeout-ac", "0"],
                 ["powercfg", "/change", "hibernate-timeout-ac", "0"],
                 ["powercfg", "/setacvalueindex", "SCHEME_CURRENT", "2a737441-1930-4402-8d77-b2bebba308a3",
                  "48e6b7a6-50f5-4782-a5d4-53bb8f07e226", "0"],
                 ["powercfg", "/setactive", "SCHEME_CURRENT"]):
        try:
            _run(args, timeout=30)
        except Exception as e:
            log.warning("powercfg failed: %s", e)

    # 3. start now, through the task (so it runs exactly as it will after a restart)
    r = _run(["schtasks", "/Run", "/TN", TASK_NAME])
    log.info("Started now: %s", "ok" if r.returncode == 0 else (r.stderr or r.stdout).decode(errors="replace").strip()[:200])
    return 0


def do_remove() -> int:
    for name in (TASK_NAME, TASK_NAME + " Watchdog"):
        try:
            _run(["schtasks", "/End", "/TN", name], timeout=30)
            _run(["schtasks", "/Delete", "/TN", name, "/F"], timeout=30)
        except Exception:
            pass
    return 0


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
    def data_dir_writable():
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        probe = DATA_DIR / "probe.tmp"
        probe.write_text("x")
        probe.unlink()
        return str(DATA_DIR)

    check("data folder", data_dir_writable)
    check("running as SYSTEM", is_system_account)
    check("task xml", lambda: len(task_xml(Path(sys.executable))))
    check("printer health", lambda: printer_health(""))
    check("website key", lambda: VerifyKey(load_config(), lambda k, d: d, lambda k, v: None).get()[:12] or "not reachable")
    check("Word installed", word_installed)
    check("screenshot", lambda: f"{len(take_screenshot() or b'')} bytes")
    if IS_WINDOWS:
        check("pywin32", lambda: __import__("win32com.client").__name__)
        check("SumatraPDF", lambda: Printer._find_sumatra("") or (_ for _ in ()).throw(RuntimeError("missing")))
    lines.append("SELFTEST " + ("PASSED" if ok else "FAILED"))
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    (DATA_DIR / "selftest.txt").write_text("\n".join(lines), encoding="utf-8")
    return 0 if ok else 1


# --------------------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description="Print documents sent from the remote printing website.")
    ap.add_argument("--test-print", metavar="FILE")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--setup", action="store_true", help="installer: register the startup task and start now")
    ap.add_argument("--remove", action="store_true", help="uninstaller: remove the startup task")
    ap.add_argument("--screenshot", metavar="FILE", help="internal helper: save a screenshot")
    ap.add_argument("--word-to-pdf", nargs=3, metavar=("SRC", "DST", "ERRFILE"), help="internal helper")
    args = ap.parse_args()

    if IS_WINDOWS:   # no crash, missing-disk or file-error dialog can ever appear (inherited by child programs)
        try:
            import ctypes
            ctypes.windll.kernel32.SetErrorMode(0x0001 | 0x0002 | 0x8000)
        except Exception:
            pass

    if args.selftest:
        sys.exit(selftest())
    if args.screenshot:                      # helper, runs in the logged-on user's session
        data = _grab_screen()
        if data:
            Path(args.screenshot).write_bytes(data)
        sys.exit(0 if data else 2)
    if args.word_to_pdf:                     # helper, runs in the logged-on user's session
        try:
            word_export_pdf(Path(args.word_to_pdf[0]), Path(args.word_to_pdf[1]))
        except BaseException as e:
            Path(args.word_to_pdf[2]).write_text(str(e), encoding="utf-8")
            sys.exit(3)
        sys.exit(0)
    if args.setup or args.remove:
        setup_logging()
        sys.exit(do_setup() if args.setup else do_remove())

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
    threading.Thread(target=verify.refresh, name="verify", daemon=True).start()   # never delay the start
    g = cfg["general"]          # test installations (CI) use their own channel - never the real one
    agent.ch = Channel(g.get("topic", "").strip() or os.environ.get("RP_TOPIC", "").strip() or CHANNEL_TOPIC,
                       g.get("key", "").strip() or os.environ.get("RP_KEY", "").strip() or CHANNEL_KEY, verify)
    log.info("Remote Printer %s started.", VERSION)
    agent.restore_waiting()
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
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        (DATA_DIR / "crash.txt").write_text(traceback.format_exc(), encoding="utf-8")
        log.exception("Crashed")
        sys.exit(1)     # never re-raise: the windowed launcher would pop up "Failed to execute script"

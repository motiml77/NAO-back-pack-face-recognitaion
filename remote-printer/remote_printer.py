#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Remote Printer
==============
Runs permanently on a computer that is connected to a printer.
Receives PDF / Word documents remotely (Telegram bot and/or e-mail) and prints
them immediately - no dialog, no confirmation, no click on the computer.

Only outgoing connections are used (polling), so no router / port-forwarding
configuration is needed.

Usage:
    python remote_printer.py                      run (reads config.ini next to this file)
    python remote_printer.py --test-print FILE    print one local file to check the printer setup
    python remote_printer.py --install-autostart  start automatically when Windows logs in
    python remote_printer.py --remove-autostart   undo the above
"""

import argparse
import configparser
import email
import email.policy
import imaplib
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
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from email.utils import parseaddr
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
IS_WINDOWS = sys.platform.startswith("win")
IS_MAC = sys.platform == "darwin"

PDF_EXT = {".pdf"}
WORD_EXT = {".doc", ".docx", ".rtf", ".odt"}
SUPPORTED_EXT = PDF_EXT | WORD_EXT

PRINT_TIMEOUT_SEC = 180          # max time for a single print job before it is killed
SINGLE_INSTANCE_PORT = 47631     # local port used only to prevent two copies running
EMAIL_LOOKBACK_DAYS = 3          # ignore unread e-mails older than this
CREATE_NO_WINDOW = 0x08000000    # Windows: run helper programs without a console window
AUTOSTART_NAME = "RemotePrinter"

log = logging.getLogger("remote_printer")


class PrintError(Exception):
    pass


# --------------------------------------------------------------------------- helpers

def setup_logging():
    log.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(threadName)s: %(message)s")
    fh = logging.handlers.RotatingFileHandler(
        BASE_DIR / "remote_printer.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    fh.setFormatter(fmt)
    log.addHandler(fh)
    if sys.stderr is not None:            # pythonw.exe has no console
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        log.addHandler(sh)


def load_config(path: Path) -> configparser.ConfigParser:
    if not path.exists():
        example = BASE_DIR / "config.example.ini"
        if example.exists():
            shutil.copy(example, path)
            raise SystemExit(f"Created {path.name} from the example - edit it and run again.")
        raise SystemExit(f"Config file not found: {path}")
    cfg = configparser.ConfigParser(interpolation=None)
    cfg.read(path, encoding="utf-8-sig")
    return cfg


def split_list(value: str):
    return [v.strip() for v in re.split(r"[,\s;]+", value or "") if v.strip()]


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
    """Return a bound socket, or None if another copy is already running."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", SINGLE_INSTANCE_PORT))
        s.listen(1)
        return s
    except OSError:
        s.close()
        return None


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
                run_quiet([self.sumatra, *target, "-silent", path])
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


# --------------------------------------------------------------------------- Telegram

class TelegramBot:
    API = "https://api.telegram.org"

    def __init__(self, token, allowed_ids, inbox: Path, max_bytes, pq: PrintQueue, printer: Printer):
        self.token = token
        self.allowed = set(allowed_ids)
        self.inbox = inbox
        self.max_bytes = max_bytes
        self.pq = pq
        self.printer = printer

    def _call(self, method, params=None, timeout=30):
        data = urllib.parse.urlencode({k: v for k, v in (params or {}).items() if v is not None}).encode()
        req = urllib.request.Request(f"{self.API}/bot{self.token}/{method}", data=data)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                res = json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:   # never log the URL - it contains the token
            try:
                desc = json.loads(e.read().decode("utf-8")).get("description", "")
            except Exception:
                desc = ""
            raise RuntimeError(f"Telegram {method}: HTTP {e.code} {desc}") from None
        if not res.get("ok"):
            raise RuntimeError(f"Telegram {method}: {res.get('description')}")
        return res["result"]

    def send(self, chat_id, text, reply_to=None):
        try:
            reply = json.dumps({"message_id": reply_to, "allow_sending_without_reply": True}) if reply_to else None
            self._call("sendMessage", {"chat_id": chat_id, "text": text, "reply_parameters": reply})
        except Exception as e:
            log.warning("Could not send Telegram message: %s", e)

    def run(self):
        offset, delay = None, 5
        while True:
            try:
                me = self._call("getMe")
                log.info("Telegram bot connected: @%s", me.get("username"))
                break
            except Exception as e:
                log.error("Telegram connection failed: %s (retry in %ss)", e, delay)
                time.sleep(delay)
                delay = min(delay * 2, 300)
        delay = 5
        while True:
            try:
                updates = self._call("getUpdates", {"timeout": 50, "offset": offset,
                                                    "allowed_updates": '["message"]'}, timeout=70)
                delay = 5
                for upd in updates:
                    offset = upd["update_id"] + 1
                    try:
                        self._handle(upd.get("message"))
                    except Exception:
                        log.exception("Error handling Telegram message")
            except Exception as e:
                log.warning("Telegram polling error: %s (retry in %ss)", e, delay)
                time.sleep(delay)
                delay = min(delay * 2, 300)

    def _handle(self, msg):
        if not msg:
            return
        chat_id = msg["chat"]["id"]
        msg_id = msg.get("message_id")
        user = msg.get("from") or {}
        uid = user.get("id")
        who = " ".join(filter(None, [user.get("first_name"), user.get("last_name")])) or str(uid)

        if uid not in self.allowed:
            log.warning("Rejected Telegram user %s (%s)", uid, who)
            self.send(chat_id, "⛔ אין לך הרשאה להדפיס במדפסת הזאת.\n"
                               f"מספר המשתמש שלך: {uid}\n"
                               "כדי לקבל הרשאה, יש להוסיף את המספר ל-allowed_user_ids בקובץ config.ini.")
            return

        text = (msg.get("text") or "").strip()
        doc = msg.get("document")
        if doc:
            try:
                self._handle_document(chat_id, msg_id, who, doc)
            except Exception as e:
                log.exception("Could not receive Telegram file")
                self.send(chat_id, f"❌ לא הצלחתי לקבל את הקובץ: {e}", msg_id)
        elif text.startswith("/status"):
            self.send(chat_id, f"🟢 מחובר.\nמדפסת: {self.printer.describe()}\n"
                               f"ממתינים בתור: {self.pq.pending()}")
        else:
            self.send(chat_id, "🖨️ שלחו לכאן קובץ PDF או Word (doc/docx) והוא יודפס מיד.\n"
                               "/status - מצב המדפסת")

    def _handle_document(self, chat_id, msg_id, who, doc):
        name = doc.get("file_name") or "document"
        ext = Path(name).suffix.lower()
        if ext not in SUPPORTED_EXT:
            self.send(chat_id, f"❌ סוג קובץ לא נתמך ({ext or 'ללא סיומת'}). אפשר לשלוח PDF או Word.", msg_id)
            return
        if doc.get("file_size", 0) > self.max_bytes:
            self.send(chat_id, f"❌ הקובץ גדול מדי (מקסימום {self.max_bytes // 1_000_000}MB).", msg_id)
            return

        info = self._call("getFile", {"file_id": doc["file_id"]})
        dest = unique_path(self.inbox, safe_filename(name))
        self._download(f"{self.API}/file/bot{self.token}/{info['file_path']}", dest)
        log.info("Telegram: received %s from %s", name, who)

        def notify(ok, err):
            if ok:
                self.send(chat_id, f"✅ נשלח למדפסת: {name}", msg_id)
            else:
                self.send(chat_id, f"❌ ההדפסה נכשלה: {name}\n{err}", msg_id)

        self.pq.submit(dest, f"Telegram/{who}", notify)

    def _download(self, url, dest: Path):
        size = 0
        try:
            with urllib.request.urlopen(url, timeout=120) as r, open(dest, "wb") as f:
                while chunk := r.read(65536):
                    size += len(chunk)
                    if size > self.max_bytes:
                        raise PrintError("file too large")
                    f.write(chunk)
        except Exception:
            dest.unlink(missing_ok=True)
            raise


# --------------------------------------------------------------------------- E-mail

class EmailWatcher:
    def __init__(self, cfg: configparser.ConfigParser, inbox: Path, max_bytes, pq: PrintQueue):
        e = "email"
        self.server = cfg.get(e, "imap_server", fallback="imap.gmail.com").strip()
        self.port = cfg.getint(e, "imap_port", fallback=993)
        self.username = cfg.get(e, "username", fallback="").strip()
        self.password = cfg.get(e, "password", fallback="").strip()
        self.folder = cfg.get(e, "folder", fallback="INBOX").strip() or "INBOX"
        self.allowed = [a.lower() for a in split_list(cfg.get(e, "allowed_senders", fallback=""))]
        self.secret = cfg.get(e, "subject_secret", fallback="").strip()
        self.interval = max(10, cfg.getint(e, "check_every_seconds", fallback=30))
        self.inbox = inbox
        self.max_bytes = max_bytes
        self.pq = pq
        self.imap = None
        self.skipped = set()   # UIDs already looked at and ignored (don't log them again)

        bad = [a for a in self.allowed if not re.fullmatch(r"[^@\s\"]+@[^@\s\"]+", a)]
        if bad:
            raise SystemExit(f"Invalid address in allowed_senders: {bad}")

    def run(self):
        delay = 10
        while True:
            try:
                self.imap = imaplib.IMAP4_SSL(self.server, self.port, timeout=60)
                self.imap.login(self.username, self.password)
                log.info("E-mail connected: %s (allowed senders: %s)", self.username, ", ".join(self.allowed))
                delay = 10
                while True:
                    self._poll()
                    time.sleep(self.interval)
            except Exception as e:
                log.warning("E-mail error: %s (retry in %ss)", e, delay)
                try:
                    self.imap.logout()
                except Exception:
                    pass
                time.sleep(delay)
                delay = min(delay * 2, 600)

    def _poll(self):
        typ, _ = self.imap.select(self.folder)
        if typ != "OK":
            raise RuntimeError(f"cannot open folder {self.folder}")
        since = (datetime.now() - timedelta(days=EMAIL_LOOKBACK_DAYS)).strftime("%d-%b-%Y")
        uids = set()
        for sender in self.allowed:   # only look at mail from allowed senders - never touch other mail
            typ, data = self.imap.uid("SEARCH", None, "UNSEEN", "SINCE", since, "FROM", f'"{sender}"')
            if typ == "OK" and data and data[0]:
                uids.update(data[0].split())
        for uid in sorted(uids - self.skipped, key=int):
            typ, data = self.imap.uid("FETCH", uid, "(BODY.PEEK[])")
            raw = next((p[1] for p in data if isinstance(p, tuple)), None)
            if typ != "OK" or raw is None:
                continue
            try:
                handled = self._process(raw)
            except Exception:
                log.exception("Could not process e-mail %s", uid)
                handled = False
            if handled:
                self.imap.uid("STORE", uid, "+FLAGS", "(\\Seen)")
            else:
                self.skipped.add(uid)

    def _process(self, raw: bytes) -> bool:
        msg = email.message_from_bytes(raw, policy=email.policy.default)
        sender = parseaddr(str(msg.get("From", "")))[1].lower()
        subject = str(msg.get("Subject", ""))
        if sender not in self.allowed:
            return False
        if self.secret and self.secret not in subject:
            log.warning("E-mail from %s ignored: subject does not contain the secret word", sender)
            return False

        found = 0
        for part in msg.walk():
            if part.is_multipart():
                continue
            name = part.get_filename()
            if not name or Path(name).suffix.lower() not in SUPPORTED_EXT:
                continue
            payload = part.get_payload(decode=True) or b""
            if len(payload) > self.max_bytes:
                log.warning("E-mail attachment %s too large - skipped", name)
                continue
            dest = unique_path(self.inbox, safe_filename(name))
            dest.write_bytes(payload)
            log.info("E-mail: received %s from %s", name, sender)
            self.pq.submit(dest, f"E-mail/{sender}")
            found += 1
        if not found:
            log.info("E-mail from %s (%r) has no PDF/Word attachment - ignored", sender, subject)
        return found > 0


# --------------------------------------------------------------------------- autostart

def autostart(install: bool):
    script = Path(__file__).resolve()
    if not IS_WINDOWS:
        print("On Linux use systemd and on macOS use launchd - see README.md")
        return
    import winreg
    key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_SET_VALUE) as key:
        if install:
            pyw = Path(sys.executable).with_name("pythonw.exe")
            exe = pyw if pyw.exists() else Path(sys.executable)
            winreg.SetValueEx(key, AUTOSTART_NAME, 0, winreg.REG_SZ, f'"{exe}" "{script}"')
            print(f"OK - the program will start automatically at logon:\n  {exe} {script}")
        else:
            try:
                winreg.DeleteValue(key, AUTOSTART_NAME)
                print("Autostart removed.")
            except FileNotFoundError:
                print("Autostart was not installed.")


# --------------------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description="Receive documents remotely and print them.")
    ap.add_argument("--config", default=str(BASE_DIR / "config.ini"))
    ap.add_argument("--test-print", metavar="FILE", help="print one local file and exit")
    ap.add_argument("--install-autostart", action="store_true")
    ap.add_argument("--remove-autostart", action="store_true")
    args = ap.parse_args()

    if args.install_autostart or args.remove_autostart:
        autostart(args.install_autostart)
        return

    setup_logging()
    cfg = load_config(Path(args.config))
    printer = Printer(cfg)
    printer.log_setup()

    if args.test_print:
        printer.print_file(Path(args.test_print).resolve())
        log.info("Test print sent.")
        return

    lock = acquire_single_instance_lock()
    if lock is None:
        log.error("Another copy of Remote Printer is already running - exiting.")
        sys.exit(1)

    inbox = BASE_DIR / "received"
    max_bytes = int(cfg.getfloat("general", "max_file_mb", fallback=20) * 1_000_000)
    pq = PrintQueue(printer, cfg.getboolean("general", "keep_files", fallback=True))
    started = []

    if cfg.getboolean("telegram", "enabled", fallback=False):
        token = cfg.get("telegram", "bot_token", fallback="").strip()
        if not token:
            raise SystemExit("telegram.bot_token is empty in config.ini")
        ids = []
        for v in split_list(cfg.get("telegram", "allowed_user_ids", fallback="")):
            try:
                ids.append(int(v))
            except ValueError:
                raise SystemExit(f"Invalid Telegram user id in allowed_user_ids: {v}")
        if not ids:
            log.warning("No allowed_user_ids yet - send /start to the bot to see your id, then add it.")
        bot = TelegramBot(token, ids, inbox, max_bytes, pq, printer)
        threading.Thread(target=bot.run, name="telegram", daemon=True).start()
        started.append("Telegram")

    if cfg.getboolean("email", "enabled", fallback=False):
        watcher = EmailWatcher(cfg, inbox, max_bytes, pq)
        if not (watcher.username and watcher.password and watcher.allowed):
            raise SystemExit("email: username, password and allowed_senders are required")
        threading.Thread(target=watcher.run, name="email", daemon=True).start()
        started.append("E-mail")

    if not started:
        raise SystemExit("Nothing enabled - set enabled = yes under [telegram] and/or [email]")

    log.info("Remote Printer is running (%s). Press Ctrl+C to stop.", " + ".join(started))
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        log.info("Stopped.")


if __name__ == "__main__":
    try:
        main()
    except SystemExit as e:
        if e.code not in (None, 0) and not isinstance(e.code, int):
            log.error("%s", e.code)
        raise

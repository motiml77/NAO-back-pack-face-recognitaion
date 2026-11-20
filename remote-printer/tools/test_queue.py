#!/usr/bin/env python3
"""Tests of the "nobody is logged on" logic - no Windows, no printer, no network needed.

A computer that has just booted sits at the login screen. Printing as SYSTEM may not work with
some printer drivers. The job must then WAIT, tell the website so, and print by itself a few
seconds after somebody logs on - even if the program was restarted in between.

    python tools/test_queue.py
"""
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import remote_printer as rp  # noqa: E402


class FakePrinter:
    configured = ""
    printer_name = "Test Printer"
    dry_run = False

    def __init__(self, world):
        self.world = world
        self.fallback_used = False
        self.user_only = False

    def print_file(self, path):
        w = self.world
        if w["raise_no_user"] and not w["logged_in"]:
            raise rp.NoUserSession("nobody")
        if not w["logged_in"] and not self.user_only:
            self.fallback_used = True           # tried as SYSTEM
        elif not w["logged_in"] and self.user_only:
            raise rp.NoUserSession("nobody")


def make(world, keep_files=False):
    rp.resolve_printer = lambda configured: ("Test Printer", "auto")
    rp.spool_jobs = lambda name: {}

    def fake_wait(name, before, timeout=300):
        if world["logged_in"]:
            return True, "המדפסת סיימה להדפיס", False
        return world["system_result"]
    rp.wait_for_printer = fake_wait
    return rp.PrintQueue(FakePrinter(world), keep_files, user_ready=lambda: world["logged_in"], poll=0.05, settle=0.0)


def recorder():
    events = []
    return events, lambda state, detail="": events.append((state, detail))


def wait_for(cond, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


def check(name, ok):
    print(("PASS  " if ok else "FAIL  ") + name)
    if not ok:
        check.failed = True


check.failed = False


def new_file(folder, name="job.pdf"):
    f = Path(folder) / name
    f.write_bytes(b"%PDF-1.4")
    return f


with tempfile.TemporaryDirectory() as tmp:
    # 1. login screen, SYSTEM printing ends in a dead job -> waits, then prints after login
    world = {"logged_in": False, "raise_no_user": False, "system_result": (False, "שגיאה בהדפסה (העבודה בוטלה)", True)}
    q = make(world)
    events, notify = recorder()
    f = new_file(tmp)
    q.submit(f, "test", notify)
    check("1a. at the login screen the job waits (not an error)", wait_for(lambda: [e[0] for e in events] == ["waiting"]))
    time.sleep(0.4)
    check("1b. it keeps waiting while nobody logs on", [e[0] for e in events] == ["waiting"] and f.exists())
    world["logged_in"] = True
    check("1c. after login it prints by itself: waiting -> printing -> done",
          wait_for(lambda: [e[0] for e in events] == ["waiting", "printing", "done"]))
    check("1d. the file is removed after printing", wait_for(lambda: not f.exists()))

    # 2. Word-like job: cannot even start without a user
    world = {"logged_in": False, "raise_no_user": True, "system_result": (True, "x", False)}
    q = make(world)
    events, notify = recorder()
    f = new_file(tmp, "word.pdf")
    q.submit(f, "test", notify)
    check("2a. a job that needs a user waits", wait_for(lambda: [e[0] for e in events] == ["waiting"]))
    world["logged_in"] = True
    world["raise_no_user"] = False
    check("2b. and prints after login", wait_for(lambda: [e[0] for e in events] == ["waiting", "printing", "done"]))

    # 3. SYSTEM printing works at the login screen -> prints at once, no waiting
    world = {"logged_in": False, "raise_no_user": False, "system_result": (True, "המדפסת סיימה להדפיס", False)}
    q = make(world)
    events, notify = recorder()
    q.submit(new_file(tmp, "ok.pdf"), "test", notify)
    check("3. when SYSTEM can print, it prints at once", wait_for(lambda: [e[0] for e in events] == ["done"]))

    # 4. a real printer problem (paper out) is reported, not parked
    world = {"logged_in": False, "raise_no_user": False, "system_result": (False, "נגמר הנייר", False)}
    q = make(world)
    events, notify = recorder()
    q.submit(new_file(tmp, "paper.pdf"), "test", notify)
    check("4. paper out is an error, not a wait for login", wait_for(lambda: [e[0] for e in events] == ["error"]))

    # 5. nobody logs on for too long -> the job is given up with a clear message
    world = {"logged_in": False, "raise_no_user": True, "system_result": (True, "x", False)}
    rp.PrintQueue.WAIT_MAX_SEC = 0.3
    q = make(world)
    events, notify = recorder()
    f = new_file(tmp, "old.pdf")
    q.submit(f, "test", notify)
    check("5. after the waiting limit it reports an error", wait_for(lambda: [e[0] for e in events] == ["waiting", "error"]))
    check("5b. and removes the file", wait_for(lambda: not f.exists()))
    rp.PrintQueue.WAIT_MAX_SEC = 24 * 3600

    # 6. the program is restarted while a job waits: it is picked up again from the saved state
    rp.DATA_DIR = Path(tmp)
    rp.STATE_PATH = Path(tmp) / "state.json"
    world = {"logged_in": False, "raise_no_user": True, "system_result": (True, "x", False)}
    q = make(world)
    replies = []

    class FakeChannel:
        def reply(self, obj, attachment=None):
            replies.append(obj)

    agent = rp.Agent(None, FakeChannel(), FakePrinter(world), q)
    f = new_file(tmp, "restart.pdf")
    q.submit(f, "site", agent._make_notify("job1", "דף לבדיקה", f))
    check("6a. waiting job is saved to disk", wait_for(lambda: "job1" in agent.state.get("waiting", {})
                                                          and "job1" in rp.STATE_PATH.read_text(encoding="utf-8")))
    # --- "restart": a new queue and a new agent that read the saved state
    q2 = make(world)
    replies.clear()
    agent2 = rp.Agent(None, FakeChannel(), FakePrinter(world), q2)
    agent2.restore_waiting()
    check("6b. after a restart it waits again", wait_for(lambda: [r["state"] for r in replies] == ["waiting"]))
    world["logged_in"] = True
    world["raise_no_user"] = False
    check("6c. and prints after login", wait_for(lambda: [r["state"] for r in replies] == ["waiting", "printing", "done"]))
    check("6d. then it is forgotten", wait_for(lambda: "job1" not in agent2.state.get("waiting", {})))

sys.exit(1 if check.failed else 0)

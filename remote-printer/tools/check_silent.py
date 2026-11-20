#!/usr/bin/env python3
"""Guard for "completely silent": fails if the program could ever show a window.

Rules checked on remote_printer.py:
  1. every function that starts another program (subprocess) must use quiet_kwargs()
     (no console window, hidden GUI window);
  2. nothing may open a document or URL in a visible program (os.startfile, os.system,
     os.popen, webbrowser);
  3. no message boxes / dialogs (MessageBox, tkinter).

    python tools/check_silent.py
"""
import ast
import sys
from pathlib import Path

SOURCE = Path(__file__).resolve().parent.parent / "remote_printer.py"
SUBPROCESS_CALLS = {"run", "Popen", "call", "check_call", "check_output"}
FORBIDDEN_OS = {"startfile", "system", "popen"}


def is_call(node, module, names):
    return (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name) and node.func.value.id == module
            and node.func.attr in names)


def main():
    text = SOURCE.read_text(encoding="utf-8")
    tree = ast.parse(text)
    problems = []

    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        spawns = [n for n in ast.walk(fn) if is_call(n, "subprocess", SUBPROCESS_CALLS)]
        quiet = any(isinstance(n, ast.Call) and getattr(n.func, "id", "") == "quiet_kwargs" for n in ast.walk(fn))
        if spawns and not quiet:
            problems.append(f"line {spawns[0].lineno}: {fn.name}() starts a program without quiet_kwargs()")

    for n in ast.walk(tree):
        if is_call(n, "os", FORBIDDEN_OS):
            problems.append(f"line {n.lineno}: os.{n.func.attr}() can open a visible program")
        if is_call(n, "webbrowser", {"open", "open_new", "open_new_tab"}):
            problems.append(f"line {n.lineno}: webbrowser opens a window")
        if isinstance(n, (ast.Import, ast.ImportFrom)):
            names = [a.name for a in n.names] + ([n.module] if isinstance(n, ast.ImportFrom) and n.module else [])
            if any(x.split(".")[0] in ("tkinter", "Tkinter") for x in names):
                problems.append(f"line {n.lineno}: tkinter import (dialogs)")
    if "MessageBox" in text:
        problems.append("MessageBox is used")

    if problems:
        print("NOT SILENT:")
        for line in problems:
            print("  -", line)
        return 1
    print("OK: every program the software starts is windowless; nothing opens a visible program or dialog.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

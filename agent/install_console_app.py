#!/usr/bin/env python
"""install_console_app.py - create the shortcuts for the AI Delivery Console (Windows).

  python install_console_app.py             Desktop shortcut "AI Delivery Console" (opens the app window)
  python install_console_app.py --startup   also start the background service at Windows sign-in
  python install_console_app.py --remove    remove both shortcuts

Why shortcuts and not an .exe
  The console needs your repository, the virtual environment, .env, the Salesforce CLI and the Copilot CLI.
  An .exe would still need all of them, and a managed machine may block a new unsigned .exe. A shortcut that
  runs the same pythonw.exe you already use needs no approval and nothing new to trust.

The shortcuts are created in your Desktop and Startup folders, never inside the repository (pre-flight
requires a clean working tree). Uses PowerShell's WScript.Shell to create the .lnk files.
"""
import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
APP_NAME = "AI Delivery Console"
STARTUP_NAME = "AI Delivery Console (background service)"


def pythonw():
    exe = Path(sys.executable)
    if exe.name.lower() == "python.exe" and exe.with_name("pythonw.exe").exists():
        return str(exe.with_name("pythonw.exe"))
    return str(exe)


def ps_quote(s):
    return "'" + str(s).replace("'", "''") + "'"


def build_script(items, remove=False):
    """PowerShell text that creates (or removes) the given shortcuts.
    items: list of dicts with name, where ('Desktop' or 'Startup'), target, args, workdir, description, style."""
    lines = ["$ErrorActionPreference = 'Stop'", "$ws = New-Object -ComObject WScript.Shell",
             "function Dir-For($w) { if ($w -eq 'Desktop') { [Environment]::GetFolderPath('Desktop') } "
             "else { Join-Path $env:APPDATA 'Microsoft\\Windows\\Start Menu\\Programs\\Startup' } }"]
    for it in items:
        lines.append(f"$p = Join-Path (Dir-For {ps_quote(it['where'])}) {ps_quote(it['name'] + '.lnk')}")
        if remove:
            lines.append("if (Test-Path $p) { Remove-Item $p -Force; Write-Output \"Removed $p\" } else { Write-Output \"Not found: $p\" }")
        else:
            lines += ["$s = $ws.CreateShortcut($p)", f"$s.TargetPath = {ps_quote(it['target'])}", f"$s.Arguments = {ps_quote(it['args'])}",
                      f"$s.WorkingDirectory = {ps_quote(it['workdir'])}", f"$s.Description = {ps_quote(it['description'])}",
                      f"$s.WindowStyle = {int(it.get('style', 1))}", "$s.Save()", "Write-Output \"Created $p\""]
    return "\n".join(lines) + "\n"


def items(startup):
    svc = str(HERE / "console_service.py")
    out = [{"name": APP_NAME, "where": "Desktop", "target": pythonw(), "args": f'"{svc}" --app', "workdir": str(HERE),
            "description": "Open the AI delivery pipeline console (the watcher keeps running in the background).", "style": 7}]
    if startup:
        out.append({"name": STARTUP_NAME, "where": "Startup", "target": pythonw(), "args": f'"{svc}" --background', "workdir": str(HERE),
                    "description": "Start the AI delivery pipeline console service at sign-in.", "style": 7})
    return out


def run_ps(text):
    f = Path(tempfile.gettempdir()) / "ai-console-shortcuts.ps1"
    f.write_text(text, encoding="utf-8")
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(f)], capture_output=True, text=True, timeout=60)
        return r.returncode, (r.stdout + r.stderr).strip()
    finally:
        try:
            f.unlink()
        except OSError:
            pass


def main(argv=None):
    ap = argparse.ArgumentParser(description="Create the AI Delivery Console shortcuts")
    ap.add_argument("--startup", action="store_true", help="also start the background service at Windows sign-in")
    ap.add_argument("--remove", action="store_true", help="remove the shortcuts")
    ap.add_argument("--print", action="store_true", dest="show", help="print the PowerShell script and exit")
    a = ap.parse_args(argv)
    its = items(startup=a.startup or a.remove)
    text = build_script(its, remove=a.remove)
    if a.show:
        print(text)
        return 0
    if os.name != "nt":
        print("This installer creates Windows shortcuts. On this system run: python console_service.py")
        return 1
    code, out = run_ps(text)
    print(out or "(no output)")
    if code != 0:
        print("\nCould not create the shortcuts. Create one by hand: right-click the Desktop > New > Shortcut, target:")
        print(f'  "{pythonw()}" "{HERE / "console_service.py"}" --app')
        return code
    print("\nDone. Double-click 'AI Delivery Console' on your Desktop.")
    if a.startup:
        print("The background service will also start when you sign in to Windows (the watcher starts only if you turned on "
              "'Start the watcher automatically' in the Service settings).")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Hangar desktop shell.

Launches Hangar as a native-feeling desktop app by opening it in a chrome-less
Edge/Chrome "app window" (no tabs, no address bar, its own taskbar icon). This
avoids the pywebview -> WebView2 -> .NET (pythonnet/clr) bridge, which is fragile
to package with PyInstaller and was crashing frozen builds on launch.

    python desktop.py

If no Chromium-based browser is found, Hangar falls back to opening in the
user's default browser. Either way the Flask server runs locally in a daemon
thread and nothing leaves the machine.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser

os.environ["HANGAR_DESKTOP"] = "1"

import app as backend  # noqa: E402  (after env flag so /api/state reports desktop mode)


def _serve():
    backend.run_server(open_browser=False)


def _find_chromium():
    """Path to a Chromium-based browser (Edge first — always on Win10/11 — then
    Chrome/Chromium/Brave), or None. Edge/Chrome support the --app flag that
    gives a borderless standalone window."""
    candidates = []
    if sys.platform == "win32":
        pf = os.environ.get("ProgramFiles", r"C:\Program Files")
        pf86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
        local = os.environ.get("LOCALAPPDATA", "")
        candidates = [
            os.path.join(pf86, r"Microsoft\Edge\Application\msedge.exe"),
            os.path.join(pf, r"Microsoft\Edge\Application\msedge.exe"),
            os.path.join(pf, r"Google\Chrome\Application\chrome.exe"),
            os.path.join(pf86, r"Google\Chrome\Application\chrome.exe"),
            os.path.join(local, r"Google\Chrome\Application\chrome.exe"),
        ]
    elif sys.platform == "darwin":
        candidates = [
            "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Chromium.app/Contents/MacOS/Chromium",
        ]
    else:
        for name in ("microsoft-edge", "google-chrome", "google-chrome-stable",
                     "chromium", "chromium-browser", "brave-browser"):
            found = shutil.which(name)
            if found:
                candidates.append(found)
    for c in candidates:
        if c and os.path.exists(c):
            return c
    return None


def _launch_app_window(url):
    """Open `url` in a borderless Chromium app window and block until the user
    closes it. Returns False if no suitable browser was found / couldn't launch."""
    browser = _find_chromium()
    if not browser:
        return False
    # A dedicated profile dir makes this its own browser process tree, so the
    # launched process stays in the foreground until the window is closed (and
    # it doesn't merge into the user's existing Edge/Chrome session).
    profile = os.path.join(tempfile.gettempdir(), "hangar-app-profile")
    try:
        proc = subprocess.Popen([
            browser,
            f"--app={url}",
            f"--user-data-dir={profile}",
            "--window-size=1320,860",
            "--no-first-run",
            "--no-default-browser-check",
        ])
    except Exception as e:
        sys.stderr.write(f"[Hangar] Couldn't launch app window: {e!r}\n")
        return False
    proc.wait()  # returns when the user closes the Hangar window
    return True


def _run_in_default_browser(url):
    """Last-resort fallback: open the default browser and keep the process (and
    thus the Flask server) alive."""
    sys.stderr.write(
        f"[Hangar] Opening in your default browser: {url}\n"
        f"         (Quit Hangar from your taskbar / Task Manager when done.)\n"
    )
    try:
        webbrowser.open(url)
    except Exception:
        pass
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass


def main():
    threading.Thread(target=_serve, daemon=True).start()
    time.sleep(0.6)  # give Flask a moment to bind the port
    url = f"http://{backend.HOST}:{backend.PORT}"
    if not _launch_app_window(url):
        _run_in_default_browser(url)


if __name__ == "__main__":
    main()

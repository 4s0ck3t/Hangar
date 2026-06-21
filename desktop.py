"""Hangar desktop shell.

Primary path: a real native window via pywebview (movable, resizable, with a
title bar). On Windows that renders through the Edge WebView2 runtime, which
pywebview reaches via .NET/pythonnet.

Because that .NET bridge is fragile to *freeze* with PyInstaller, there are two
automatic fallbacks so the app is never dead-on-arrival:
  1. pywebview native window  (preferred)
  2. chrome-less Edge/Chrome --app window
  3. the default browser

Run `python desktop.py --selftest` to exercise the native-window import chain
(pywebview -> WinForms -> clr) and exit 0/1 — CI runs this on real Windows so we
can see whether the frozen webview actually loads.
"""

import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import webbrowser

os.environ["HANGAR_DESKTOP"] = "1"


def _hint_pythonnet_pydll():
    """When frozen, point pythonnet at the bundled Python DLL before any `clr`
    import. A frozen app has no python3XX.dll on PATH the way pythonnet's loader
    expects, which is a common cause of the .NET loader failing to initialise."""
    if not getattr(sys, "frozen", False):
        return
    base = getattr(sys, "_MEIPASS", None) or os.path.dirname(sys.executable)
    for cand in ("python313.dll", "python312.dll", "python311.dll", "python310.dll"):
        p = os.path.join(base, cand)
        if os.path.exists(p):
            os.environ.setdefault("PYTHONNET_PYDLL", p)
            return


_hint_pythonnet_pydll()

import app as backend  # noqa: E402  (after env flag so /api/state reports desktop mode)
import store  # noqa: E402  (data dir / log path)

_LOG_PATH = store.DATA_DIR / "desktop.log"


def _safe_write(stream, text):
    """stdout/stderr are None in a --noconsole PyInstaller build, so guard every
    write. Never raise from logging."""
    if stream is None:
        return
    try:
        stream.write(text)
    except Exception:
        pass


def _log(msg):
    """Append a line to ~/.hangar/desktop.log (and stderr if present), so the
    window-strategy decisions + any backend error are recoverable from a frozen
    --noconsole build where stderr is None."""
    line = f"[Hangar] {msg}"
    try:
        store.DATA_DIR.mkdir(parents=True, exist_ok=True)
        with open(_LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:
        pass
    _safe_write(sys.stderr, line + "\n")


class Api:
    """Exposed to the native window's JS as window.pywebview.api.*"""

    def pick_folder(self):
        import webview
        win = webview.active_window()
        result = win.create_file_dialog(webview.FOLDER_DIALOG)
        if not result:
            return None
        return result[0] if isinstance(result, (list, tuple)) else result


def _serve():
    try:
        backend.run_server(open_browser=False)
    except Exception:
        # A blank window almost always means the server never came up — capture
        # why (e.g. port already bound by an orphaned Hangar) instead of dying
        # silently on a thread with no console.
        _log("Flask server crashed:\n" + traceback.format_exc())


def _pick_free_port(preferred):
    """Use the preferred port if it's free, else let the OS assign one. Avoids a
    blank window when an earlier Hangar process is still holding the default port."""
    for candidate in (preferred, 0):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind((backend.HOST, candidate))
            port = s.getsockname()[1]
            s.close()
            return port
        except OSError:
            continue
    return preferred


def _wait_for_server(host, port, timeout=20.0):
    """Block until Flask is actually accepting connections, so the window never
    opens onto a not-yet-listening (blank) server."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.15)
    return False


# ---- self-test (CI runs this on real Windows) -----------------------------
def _selftest():
    """Exercise the exact native-window init chain so CI tells us whether the
    frozen webview/.NET bridge loads. Writes the result next to a temp file and
    returns an exit code (0 ok, 1 failed)."""
    out = os.path.join(tempfile.gettempdir(), "hangar_selftest.txt")
    try:
        import webview  # noqa: F401
        # Importing the WinForms backend triggers `import clr` — the exact line
        # the user's traceback died on.
        if sys.platform == "win32":
            import webview.platforms.winforms  # noqa: F401
        msg = "SELFTEST OK: webview + clr import succeeded"
        code = 0
    except Exception as e:
        msg = "SELFTEST FAIL: " + repr(e) + "\n" + traceback.format_exc()
        code = 1
    try:
        with open(out, "w", encoding="utf-8") as fh:
            fh.write(msg + "\n")
    except Exception:
        pass
    _safe_write(sys.stderr, msg + "\n")
    _safe_write(sys.stdout, msg + "\n")
    return code


# ---- window strategies ----------------------------------------------------
def _try_pywebview(url):
    """Open the real native window. Returns True if it ran (and the user closed
    it), False if the backend couldn't start so we should fall back. The full
    failure (the actual reason WebView2 didn't come up) is written to
    ~/.hangar/desktop.log so it's recoverable from a --noconsole build."""
    try:
        import webview
    except Exception:
        _log("pywebview import failed:\n" + traceback.format_exc())
        return False

    def _ready(w):
        try:
            w.maximize()
        except Exception:
            _log("maximize failed (non-fatal):\n" + traceback.format_exc())

    try:
        webview_ver = getattr(webview, "__version__", "?")
        _log(f"pywebview {webview_ver}: creating window")
        window = webview.create_window(
            "Hangar", url, js_api=Api(),
            width=1320, height=860, min_size=(960, 620),
            background_color="#131418",
        )
        # Force the EdgeChromium (WebView2) backend so a failure raises here with
        # the real reason instead of pywebview silently trying a dead backend.
        _log("starting pywebview (gui=edgechromium)")
        webview.start(_ready, window, gui="edgechromium")
        _log("native window closed normally")
        return True
    except Exception:
        _log("native webview FAILED — falling back to Edge --app:\n"
             + traceback.format_exc())
        return False


def _find_chromium():
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
    browser = _find_chromium()
    if not browser:
        _log("no Chromium browser found for --app fallback")
        return False
    profile = os.path.join(tempfile.gettempdir(), "hangar-app-profile")
    try:
        _log(f"launching Edge/Chrome --app window via {browser}")
        proc = subprocess.Popen([
            browser, f"--app={url}", f"--user-data-dir={profile}",
            "--window-size=1320,860", "--no-first-run", "--no-default-browser-check",
        ])
    except Exception:
        _log("couldn't launch --app window:\n" + traceback.format_exc())
        return False
    proc.wait()
    return True


def _run_in_default_browser(url):
    _log(f"Opening in your default browser: {url} "
         "(quit Hangar from the taskbar / Task Manager when done)")
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
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    # Bind to a guaranteed-free port (the default may be held by an orphaned
    # instance), then start Flask there.
    backend.PORT = _pick_free_port(backend.PORT)
    url = f"http://{backend.HOST}:{backend.PORT}"
    _log(f"--- Hangar v{backend.__version__} starting on {url} "
         f"(frozen={getattr(sys, 'frozen', False)}, "
         f"PYTHONNET_PYDLL={os.environ.get('PYTHONNET_PYDLL', '<unset>')}) ---")
    threading.Thread(target=_serve, daemon=True).start()
    if _wait_for_server(backend.HOST, backend.PORT):
        _log("server is listening")
    else:
        _log("WARNING: server not reachable after 20s — window may be blank")
    if _try_pywebview(url):
        return
    if _launch_app_window(url):
        return
    _run_in_default_browser(url)


if __name__ == "__main__":
    main()

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
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import webbrowser

os.environ["HANGAR_DESKTOP"] = "1"


def _no_window():
    """subprocess kwargs that stop a child from flashing a console window on
    Windows (tasklist/taskkill would otherwise pop a cmd window that steals
    focus). No-op everywhere else."""
    if sys.platform != "win32":
        return {}
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = 0                              # SW_HIDE
    return {"startupinfo": si, "creationflags": 0x08000000}  # CREATE_NO_WINDOW


def _hint_pythonnet_pydll():
    """When frozen, point pythonnet at the bundled Python DLL before any `clr`
    import. A frozen app has no python3XX.dll on PATH the way pythonnet's loader
    expects, which is a common cause of the .NET loader failing to initialise.

    Always prefer the DLL bundled with THIS frozen build. Older Hangar launches
    can leave PYTHONNET_PYDLL in the parent environment pointing at a previous
    update folder, and setdefault would keep that stale path.
    """
    if not getattr(sys, "frozen", False):
        return
    base = getattr(sys, "_MEIPASS", None) or os.path.dirname(sys.executable)
    for cand in ("python313.dll", "python312.dll", "python311.dll", "python310.dll"):
        p = os.path.join(base, cand)
        if os.path.exists(p):
            os.environ["PYTHONNET_PYDLL"] = p
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
def _webview2_installed():
    """True if the Edge WebView2 runtime is registered (Windows). Non-Windows
    returns True (not applicable)."""
    if sys.platform != "win32":
        return True
    try:
        import winreg
    except Exception:
        return True  # can't check — assume present rather than block
    guid = "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"  # WebView2 Evergreen runtime
    keys = [
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients" + "\\" + guid),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\EdgeUpdate\Clients" + "\\" + guid),
        (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\EdgeUpdate\Clients" + "\\" + guid),
    ]
    for root, path in keys:
        try:
            with winreg.OpenKey(root, path) as k:
                pv, _ = winreg.QueryValueEx(k, "pv")
                if pv and pv != "0.0.0.0":
                    return True
        except OSError:
            continue
    return False


def _ensure_webview2():
    """If the WebView2 runtime is missing on Windows, install it via the bundled
    Evergreen bootstrapper (~2 MB stub; downloads the runtime, per-user, no admin).
    No-op on non-Windows or if already present / bootstrapper not bundled."""
    if _webview2_installed():
        return
    base = getattr(sys, "_MEIPASS", None) or os.path.dirname(os.path.abspath(__file__))
    boot = os.path.join(base, "MicrosoftEdgeWebview2Setup.exe")
    if not os.path.exists(boot):
        _log("WebView2 runtime missing and bootstrapper not bundled — using fallback window")
        return
    _log("WebView2 runtime missing — installing via bundled bootstrapper (one-time)…")
    try:
        subprocess.run([boot, "/silent", "/install"], timeout=600)
        _log("WebView2 bootstrapper finished; installed=" + str(_webview2_installed()))
    except Exception:
        _log("WebView2 bootstrapper failed:\n" + traceback.format_exc())


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
    except Exception as e:
        # Expected on most frozen Windows builds: pythonnet/.NET can't initialise
        # under PyInstaller, so the embedded webview is unavailable and we use an
        # Edge/Chrome --app window instead. Functionally identical, so log a single
        # concise line rather than a multi-frame traceback that reads like a crash.
        reason = str(e).splitlines()[0] if str(e).strip() else type(e).__name__
        _log(f"native webview unavailable ({reason}) — using Edge/Chrome --app window")
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


def _keep_alive():
    """Block the foreground process so the daemon Flask thread keeps serving."""
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass


def _cleanup_profile(profile):
    try:
        shutil.rmtree(profile, ignore_errors=True)
    except Exception:
        pass


def _seed_edge_profile(profile, url):
    """Write a minimal Chromium/Edge Local State + Preferences into the fresh
    profile directory before launch. This pre-registers the app origin so Edge
    picks up Hangar's icon (from the manifest) for the title bar and taskbar
    rather than showing its own logo.

    The file is tiny and written once; Edge merges/overwrites it on first run.
    Failure is silently swallowed — it's a best-effort cosmetic improvement."""
    try:
        os.makedirs(profile, exist_ok=True)
        # The icon path: prefer the frozen static dir, fall back to the source tree.
        icon_candidates = [
            os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "icon-256.png"),
            os.path.join(getattr(sys, "_MEIPASS", ""), "static", "icon-256.png"),
        ]
        icon_path = next((p for p in icon_candidates if os.path.exists(p)), "")
        prefs = {
            "browser": {"check_default_browser": False},
            "profile": {"name": "Hangar"},
            # Hangar is a local app window, not a browser — stop Edge from signing
            # the throwaway profile into the Windows account and popping its
            # "we're now syncing your data across devices" toast.
            "signin": {"allowed": False, "allowed_on_next_startup": False},
            "sync": {"requested": False, "keep_everything_synced": False},
            "sync_promo": {"show_on_first_run_allowed": False, "user_skipped": True},
        }
        if icon_path:
            prefs["web_app_icon"] = icon_path.replace("\\", "/")
        prefs_path = os.path.join(profile, "Default")
        os.makedirs(prefs_path, exist_ok=True)
        import json as _json
        with open(os.path.join(prefs_path, "Preferences"), "w", encoding="utf-8") as f:
            _json.dump(prefs, f)
    except Exception:
        pass  # cosmetic only — never block the launch


def _launch_app_window(url):
    browser = _find_chromium()
    if not browser:
        _log("no Chromium browser found for --app fallback")
        return False
    # A UNIQUE profile per launch + --new-window, so a new Hangar always opens
    # its OWN app window connected to ITS server. With a shared profile, Chromium
    # would re-focus an older still-running Hangar's window instead — which is why
    # a freshly-updated build could keep showing the previous version.
    profile = os.path.join(tempfile.gettempdir(), f"hangar-app-{os.getpid()}")
    _seed_edge_profile(profile, url)
    args = [browser, f"--app={url}", f"--user-data-dir={profile}", "--new-window",
            "--window-size=1320,860", "--no-first-run", "--no-default-browser-check",
            # Kill Edge's account sign-in / sync so it stops nagging in the app
            # window. --disable-sync stops sync itself; the disabled features cover
            # Edge's implicit Windows-account sign-in and the sync promo toast
            # (unknown feature names are harmless — Chromium ignores them).
            "--disable-sync",
            "--disable-background-networking",
            "--disable-features=msImplicitSignin,msEdgeSyncPromotion,EdgeSync,"
            "SyncPromoAfterSignin,ShowSyncPromo,msEdgeIdentityFRE"]
    # Chromium refuses to start as root unless sandboxing is disabled.
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        args.append("--no-sandbox")
        _log("running as root — adding --no-sandbox so Chromium will start")
    try:
        _log(f"launching Edge/Chrome --app window via {browser}")
        proc = subprocess.Popen(args)
        backend.WINDOW_PROC = proc  # let the updater close this window on handover
        backend.WINDOW_PROFILE = profile
    except Exception:
        _log("couldn't launch --app window:\n" + traceback.format_exc())
        return False
    # If it dies almost immediately it never really opened (bad flags, no display,
    # sandbox refusal): treat a non-zero quick exit as failure so we fall through
    # to the browser. A quick exit of 0 means it handed off to a browser process
    # that owns the window — keep this process alive so Flask keeps serving it.
    try:
        rc = proc.wait(timeout=2.5)
    except subprocess.TimeoutExpired:
        proc.wait()      # foreground window — block until the user closes it
        _cleanup_profile(profile)
        return True
    if rc == 0:
        _log("--app launcher handed off (exit 0); keeping server alive")
        _keep_alive()
        return True
    _cleanup_profile(profile)
    _log(f"--app window exited early (rc={rc}) — falling back to browser")
    return False


def _run_in_default_browser(url):
    _log(f"Opening in your default browser: {url} "
         "(quit Hangar from the taskbar / Task Manager when done)")
    try:
        webbrowser.open(url)
    except Exception:
        pass
    _keep_alive()


def _kill_stale_instances():
    """Terminate other running Hangar instances so an updated build is never
    shadowed by an orphaned older one (e.g. a previous version stuck keeping its
    server alive). Best-effort; only targets processes named exactly Hangar, and
    never the current process. Frozen builds only."""
    if not getattr(sys, "frozen", False):
        return
    me = os.getpid()
    try:
        if sys.platform == "win32":
            import csv
            import io
            out = subprocess.run(
                ["tasklist", "/FI", "IMAGENAME eq Hangar.exe", "/FO", "CSV", "/NH"],
                capture_output=True, text=True, timeout=10, **_no_window()).stdout
            for row in csv.reader(io.StringIO(out)):
                if len(row) >= 2 and row[0].strip().lower() == "hangar.exe":
                    try:
                        pid = int(row[1].strip())
                    except ValueError:
                        continue
                    if pid != me:
                        subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                                       capture_output=True, timeout=10, **_no_window())
                        _log(f"terminated stale Hangar.exe pid {pid}")
        else:
            out = subprocess.run(["pgrep", "-x", "Hangar"],
                                 capture_output=True, text=True, timeout=10).stdout
            for tok in out.split():
                try:
                    pid = int(tok)
                except ValueError:
                    continue
                if pid != me:
                    try:
                        os.kill(pid, signal.SIGTERM)
                        _log(f"terminated stale Hangar pid {pid}")
                    except Exception:
                        pass
    except Exception:
        _log("stale-instance cleanup skipped:\n" + traceback.format_exc())


def main():
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    # Render-farm worker mode: no window, just claim/render/return against a
    # coordinator. Same exe, so a remote box runs `Hangar.exe --worker …`.
    if "--worker" in sys.argv:
        import worker
        argv = [a for a in sys.argv[1:] if a != "--worker"]
        worker.main(argv)
        return
    # Clear out any orphaned older instances before we bind a port / open a window.
    _kill_stale_instances()
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
    _ensure_webview2()  # Windows: install the WebView2 runtime if it's missing
    if getattr(sys, "frozen", False) and sys.platform == "win32" and "--native-window" not in sys.argv:
        _log("frozen Windows build: skipping native pywebview; using Edge/Chrome --app window")
        if _launch_app_window(url):
            return
        _run_in_default_browser(url)
        return
    if _try_pywebview(url):
        return
    if _launch_app_window(url):
        return
    _run_in_default_browser(url)


if __name__ == "__main__":
    main()

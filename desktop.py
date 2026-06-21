"""Hangar desktop shell.

Launches Hangar as a real native-window application (no browser tab) using
pywebview. On Windows this renders through the built-in Edge WebView2 runtime.

    python desktop.py

To build a single distributable Hangar.exe, see the README ("Build a
standalone .exe").
"""

import os
import sys
import threading
import time
import webbrowser

os.environ["HANGAR_DESKTOP"] = "1"

import webview  # noqa: E402  (after env flag so /api/state reports desktop mode)
import app as backend  # noqa: E402


class Api:
    """Methods here are callable from the UI as window.pywebview.api.*"""

    def pick_folder(self):
        win = webview.active_window()
        result = win.create_file_dialog(webview.FOLDER_DIALOG)
        if not result:
            return None
        # create_file_dialog returns a tuple/list of selected paths.
        return result[0] if isinstance(result, (list, tuple)) else result


def _serve():
    backend.run_server(open_browser=False)


def _on_start(window):
    window.maximize()


def _run_in_browser():
    """Fallback when the native window backend can't start (e.g. pywebview's
    .NET/clr loader fails on this machine). The Flask server is already running
    in a daemon thread, so just open the default browser and keep the process
    alive to serve it."""
    url = f"http://{backend.HOST}:{backend.PORT}"
    sys.stderr.write(
        f"[Hangar] Native window unavailable — opening in your browser instead:\n"
        f"         {url}\n"
        f"         (Quit Hangar from your taskbar / Task Manager when you're done.)\n"
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
    window = webview.create_window(
        "Hangar",
        f"http://{backend.HOST}:{backend.PORT}",
        js_api=Api(),
        width=1320,
        height=860,
        min_size=(960, 620),
        background_color="#131418",
    )
    try:
        webview.start(_on_start, window)
    except Exception as e:
        # Most commonly a frozen-build pythonnet/clr load failure on Windows.
        # Don't crash to a traceback dialog — degrade to browser mode.
        sys.stderr.write(f"[Hangar] webview.start failed: {e!r}\n")
        _run_in_browser()


if __name__ == "__main__":
    main()

"""Hangar desktop shell.

Launches Hangar as a real native-window application (no browser tab) using
pywebview. On Windows this renders through the built-in Edge WebView2 runtime.

    python desktop.py

To build a single distributable Hangar.exe, see the README ("Build a
standalone .exe").
"""

import os
import threading
import time

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
    webview.start(_on_start, window)


if __name__ == "__main__":
    main()

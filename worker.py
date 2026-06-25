"""Hangar render-farm worker.

Run on another machine that can see the SAME asset paths as the coordinator
(e.g. the NAS share, reachable over Tailscale). It registers with the
coordinator, then loops: claim a chunk of jobs, render each with Hangar's
shared Blender pipeline, and POST the resulting JPEG back.

    python worker.py --coordinator http://main-host:7575 [--token TOKEN] [--name NAME]

Frozen builds can run the same thing via:  Hangar.exe --worker --coordinator ...

Requires Blender installed on this machine and read access to the asset paths.
"""
import argparse
import json
import os
import platform
import socket
import tempfile
import time
import urllib.request

import store
import thumbs

LOGFILE = store.DATA_DIR / "worker.log"


def _log(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)          # visible when run from a console / python
    try:                             # and a file, since the frozen exe is windowed
        with open(LOGFILE, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:
        pass


def _request(url, data=None, token="", raw=False, timeout=120):
    headers = {}
    if token:
        headers["X-Hangar-Farm-Token"] = token
    method = "POST" if data is not None else "GET"
    body = None
    if data is not None:
        if raw:
            headers["Content-Type"] = "application/octet-stream"
            body = data
        else:
            headers["Content-Type"] = "application/json"
            body = json.dumps(data).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8") or "{}")


def _safe(url, data, token, raw=False):
    try:
        _request(url, data, token, raw=raw)
        return True
    except Exception as e:
        _log(f"post failed ({url.split('?')[0]}): {e}")
        return False


def run(coordinator, token="", name=None, poll=3.0):
    base = coordinator.rstrip("/")
    wid = name or platform.node() or socket.gethostname() or "worker"
    gpu = ", ".join(thumbs.system_gpus()) or "unknown"
    _log(f"{wid} | gpu: {gpu} | coordinator: {base}")
    if not thumbs.blender_available():
        _log("WARNING: Blender not found here — renders will fail until "
             "it is installed / on PATH.")

    # Register, retrying until the coordinator is reachable.
    while True:
        try:
            _request(f"{base}/api/farm/register",
                     {"worker_id": wid, "name": wid, "gpu": gpu}, token)
            _log("registered, waiting for work")
            break
        except Exception as e:
            _log(f"register failed ({e}); retrying in 5s")
            time.sleep(5)

    while True:
        try:
            res = _request(f"{base}/api/farm/claim", {"worker_id": wid}, token)
        except Exception as e:
            _log(f"claim failed ({e}); retrying in {poll}s")
            time.sleep(poll)
            continue
        jobs = res.get("jobs") or []
        if not jobs:
            time.sleep(poll)
            continue
        _log(f"claimed {len(jobs)} job(s)")
        for job in jobs:
            aid, path = job["id"], job.get("path", "")
            if not path or not os.path.exists(path):
                _safe(f"{base}/api/farm/fail/{aid}?worker={wid}",
                      {"reason": "unreachable"}, token)
                continue
            try:
                with tempfile.TemporaryDirectory() as td:
                    out = os.path.join(td, "thumb.jpg")
                    if thumbs.render_model(path, out) and os.path.exists(out):
                        with open(out, "rb") as fh:
                            blob = fh.read()
                        _safe(f"{base}/api/farm/result/{aid}?worker={wid}",
                              blob, token, raw=True)
                    else:
                        _safe(f"{base}/api/farm/fail/{aid}?worker={wid}",
                              {"reason": thumbs.LAST_RENDER_ERROR or "render failed"}, token)
            except Exception as e:
                _safe(f"{base}/api/farm/fail/{aid}?worker={wid}",
                      {"reason": str(e)}, token)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Hangar render-farm worker")
    ap.add_argument("--coordinator", required=True,
                    help="Base URL of the main Hangar, e.g. http://main-host:7575")
    ap.add_argument("--token", default=os.environ.get("HANGAR_FARM_TOKEN", ""),
                    help="Shared farm token (or set HANGAR_FARM_TOKEN)")
    ap.add_argument("--name", default=None, help="Worker display name (default: hostname)")
    ap.add_argument("--poll", type=float, default=3.0,
                    help="Seconds to wait between empty claims")
    args = ap.parse_args(argv)
    run(args.coordinator, token=args.token, name=args.name, poll=args.poll)


if __name__ == "__main__":
    main()

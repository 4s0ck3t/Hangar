"""Hangar render-farm worker — standalone launcher.

Download this, run it on any Windows/Linux machine that can see the same asset
paths as Hangar (e.g. the NAS over Tailscale), and enter the address of the
computer running Hangar. It then helps generate previews whenever Hangar is
rendering.

    HangarWorker            -> prompts for the Hangar address (remembered)
    HangarWorker --coordinator http://host:7575 [--token T] [--name N]

Needs Blender installed on this machine.
"""
import argparse
import json
import platform
import socket

import store
import worker

CONFIG = store.DATA_DIR / "worker-config.json"


def _load():
    try:
        return json.loads(CONFIG.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save(cfg):
    try:
        store.DATA_DIR.mkdir(parents=True, exist_ok=True)
        CONFIG.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    except Exception:
        pass


def _normalize(addr):
    """Make 'mybox', '192.168.1.5', 'host:7575' all into a full URL."""
    addr = addr.strip().strip("/")
    if not addr:
        return ""
    if "://" not in addr:
        addr = "http://" + addr
    scheme, rest = addr.split("://", 1)
    if ":" not in rest.split("/", 1)[0]:        # no port → Hangar's default
        host, _, path = rest.partition("/")
        rest = host + ":7575" + (("/" + path) if path else "")
    return f"{scheme}://{rest}"


def _prompt(prev):
    print("=" * 60)
    print("  Hangar render-farm worker")
    print("=" * 60)
    print("Enter the address of the computer running Hangar.")
    print("  e.g.  192.168.1.50   mybox   100.105.241.43   http://host:7575")
    default = prev.get("coordinator", "")
    addr = input(f"Hangar address{f' [{default}]' if default else ''}: ").strip() or default
    addr = _normalize(addr)
    tok_hint = " [saved]" if prev.get("token") else ""
    token = input(f"Farm token (optional){tok_hint}: ").strip() or prev.get("token", "")
    return addr, token


def main(argv=None):
    ap = argparse.ArgumentParser(description="Hangar render-farm worker")
    ap.add_argument("--coordinator", help="Hangar address, e.g. http://host:7575")
    ap.add_argument("--token", default=None)
    ap.add_argument("--name", default=None, help="Worker display name (default: hostname)")
    ap.add_argument("--poll", type=float, default=3.0)
    args = ap.parse_args(argv)

    prev = _load()
    coordinator = _normalize(args.coordinator) if args.coordinator else ""
    token = args.token if args.token is not None else ""
    if not coordinator:
        coordinator, token = _prompt(prev)
    if not coordinator:
        print("No Hangar address entered — exiting.")
        return
    name = args.name or prev.get("name") or platform.node() or socket.gethostname() or "worker"
    _save({"coordinator": coordinator, "token": token, "name": name})

    print(f"\nConnecting to {coordinator} as '{name}'…  (Ctrl+C to stop)\n")
    try:
        worker.run(coordinator, token=token, name=name, poll=args.poll)
    except KeyboardInterrupt:
        print("\nWorker stopped.")


if __name__ == "__main__":
    main()

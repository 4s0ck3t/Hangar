# Hangar

A local-first asset manager for 3D work — models, textures, HDRIs and
materials — with a dark dev-tool UI and a one-click bridge into Blender.
Your files are never moved or copied; Hangar only reads and indexes them.

---

## Run it (standalone app)

```bash
pip install -r requirements.txt
python desktop.py
```

`desktop.py` opens Hangar in its own native window — no browser tab. On
Windows it renders through the built-in Edge WebView2 runtime (present on
Windows 10/11 by default).

### Or run it as a local web app

```bash
python app.py
```

…then open http://127.0.0.1:7575 in your browser. Same app, just in a tab.

---

## Adding folders

Click **Add asset folder** and pick a folder in the native OS dialog — no
path typing. Hangar scans it in the background; watch progress in the
**status bar along the bottom**. Add as many folders as you like; remove one
with the × next to it (files stay on disk). Hover a folder to see its full
path.

Indexed types:
- **Models** — .blend .fbx .obj .gltf .glb .stl .ply .usd .usdz .abc .dae .3ds
- **Textures** — .png .jpg .jpeg .tif .tiff .tga .bmp .webp .exr
- **HDRIs** — .hdr .exr
- **Materials** — .sbsar .mat .mtl

---

## Categories (auto-assigned)

Hangar files your assets into **categories** automatically as it scans — no
tagging by hand. It works purely on names: each category carries a list of
**keywords**, and any asset whose folder or file name contains one of them is
filed there. So `…/vehicles/cars/sedan.fbx` lands in **Vehicles**, a
`forest_hdri.hdr` lands in **Nature**, and a `mech_droid.obj` lands in
**Sci-Fi**. An asset can belong to several categories at once. This runs on
every file as it's indexed, across all asset types (models, textures, HDRIs,
materials).

Hangar ships with a starter taxonomy (Sci-Fi, Buildings, Architecture,
Vehicles, Characters, Weapons, Nature, Furniture, Props, Industrial, Fantasy,
Food). You can shape it to your own library:

- **Add a category** with the **+** next to *Categories* and give it keywords
  (e.g. *Robots* → `robot, droid, mech`).
- **Edit a category's keywords** — hover it and click **✎**.
- **Auto-classify (⚡)** re-runs all the rules across everything already
  indexed. Use it after adding a category or editing keywords to back-fill
  matches; it only ever *adds* — categories you assigned by hand are never
  removed.

You can also drag a card onto a category, or set categories per-asset in the
detail drawer.

## Send to Blender

1. In Blender: **Edit ▸ Preferences ▸ Add-ons ▸ Install…**, choose
   `blender_addon/hangar_bridge.py`, enable **Hangar Bridge**.
2. Open the **Hangar** tab in the 3D viewport's N-panel and click **Connect**.
3. In Hangar, open any model and click **Send to Blender** — it imports into
   your open scene.

The bridge watches a small queue file (`~/.hangar/blender_queue.jsonl`); no
ports, no network.

---

## Viewing .blend files

Hangar shows previews for `.blend` files two ways:

- **Embedded preview (instant, no Blender needed).** Blender saves a small
  preview image inside each `.blend` by default; Hangar reads it straight out
  of the file, so most `.blend` files show their thumbnail in the grid
  immediately.
- **Render on demand.** For a `.blend` with no embedded preview, open it and
  click **Render preview** in the detail drawer. Hangar launches your
  installed Blender in the background (a fast, lighting-free Workbench render)
  and caches the result. Hangar auto-detects Blender on PATH and in the usual
  install locations; if it can't find it, set `HANGAR_BLENDER` to your
  `blender` / `blender.exe` and restart.

Compressed `.blend` files (the "Compress File" save option, zstd) don't expose
an embedded preview — use **Render preview** for those.

---

## Build a standalone .exe (Windows)

To hand someone a single double-click `Hangar.exe`:

```bash
pip install pyinstaller
pyinstaller --noconsole --name Hangar --add-data "static;static" desktop.py
```

The build lands in `dist/Hangar/Hangar.exe`. Notes:
- The `;` in `--add-data` is the Windows separator (macOS/Linux use `:`).
- PyInstaller builds per-platform — run this **on the Windows machine** to get
  a Windows .exe (you can't cross-build it from macOS/Linux).
- The target PC needs the Edge **WebView2 runtime** (already on Win10/11).
- For one loose file instead of a folder, add `--onefile` (slower to start).

---

## Where Hangar keeps its data

Everything lives in `~/.hangar/` — the SQLite index, cached thumbnails and the
Blender queue. Delete that folder to reset. Your actual asset files are never
touched. Override the location with `HANGAR_HOME`, or the port with
`HANGAR_PORT`.

---

## What's inside

| File | Role |
|------|------|
| `desktop.py` | Native-window launcher (pywebview) — the standalone app |
| `app.py` | Flask server + REST API + OS integration |
| `scanner.py` | Folder walking, file classification, progress, stats |
| `thumbs.py` | Thumbnail generation + caching |
| `store.py` | SQLite index (assets, tags, collections, libraries) |
| `static/` | The UI (HTML/CSS/JS, no build step) |
| `blender_addon/hangar_bridge.py` | Installable Blender add-on |

Pure Python + vanilla JS. No build step, no cloud, no account.

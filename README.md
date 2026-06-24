# Hangar

A local-first asset manager for 3D work — models, textures, HDRIs and
materials — with a dark dev-tool UI and a one-click bridge into Blender.
Your files are never moved or copied; Hangar only reads and indexes them.

---

## Download

Grab the latest build from the [Releases page](https://github.com/4s0ck3t/Hangar/releases/latest):
- **Windows** — `Hangar-windows.zip` (extract, run `Hangar.exe`)
- **Linux** — `Hangar-linux.tar.gz` (extract, run `./Hangar`)

Once you're running a build, Hangar checks for newer releases on launch and
shows an **⬆ Update** pill — one click downloads and unpacks the new version
beside the current one. (Requires the GitHub repo to be public.)

## Run it from source (standalone app)

```bash
pip install -r requirements.txt
python desktop.py
```

`desktop.py` opens Hangar in its own window. It tries, in order:
1. a **native window** (pywebview — Edge WebView2 on Windows, WebKit on macOS,
   GTK/Qt WebKit on Linux if installed),
2. a chrome-less **Edge/Chrome `--app` window** (used automatically if the
   native backend isn't available — common on Linux), then
3. your **default browser**.

So it works out of the box on Windows, macOS and Linux; on Linux you just need
either a WebKit backend for pywebview *or* any Chromium-based browser installed
(Edge/Chrome/Chromium) — most desktops already have one.

**Windows:** the packaged build bundles the tiny (~2 MB) WebView2 Evergreen
bootstrapper and silently installs the runtime on first launch if it's missing,
so the native window works even on a machine without it.

**Linux native window (optional):** the `--app` window is the default and needs
no extra packages. If you'd rather have a true pywebview window from source,
install the WebKitGTK backend, e.g. on Debian/Ubuntu:
`sudo apt install python3-gi gir1.2-webkit2-4.1` (there's no small bundled
runtime for Linux the way Windows has WebView2). Avoid running as **root** —
browsers refuse the sandbox as root (Hangar passes `--no-sandbox` to cope, but
a normal user account is better).

### Or run it as a local web app

```bash
python app.py
```

…then open http://127.0.0.1:7575 in your browser. Same app, just in a tab.

---

## Adding folders

Click **Add asset folder** and pick a folder in the native OS dialog — no
path typing. Hangar scans it in the background; watch progress in the
**status bar along the bottom**. Once indexing finishes it **pre-bakes every
thumbnail in the background** ("Generating previews — N/M"), so the first time
you browse a library the grid reads cached images off disk instead of
rendering as you scroll. Each tile also shows the **folder it lives in** under
the name (hover it for the full path). Add as many folders as you like; remove
one with the × next to it (files stay on disk). Hover a folder to see its full
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

## Build standalone executables

CI builds both platforms automatically on every `v*` tag (see
`.github/workflows/release-{windows,linux}.yml`) and attaches them to the
GitHub release. To build by hand:

**Windows** (`dist/Hangar/Hangar.exe`):
```bash
pip install pyinstaller
pyinstaller --noconsole --name Hangar --add-data "static;static" desktop.py
```

**Linux** (`dist/Hangar/Hangar`):
```bash
pip install pyinstaller
pyinstaller --name Hangar --add-data "static:static" desktop.py
```

Notes:
- `--add-data` separator is `;` on Windows, `:` on macOS/Linux.
- PyInstaller builds **per-platform** — you can't cross-build (build the Windows
  exe on Windows, the Linux binary on Linux).
- Windows targets need the Edge **WebView2 runtime** (already on Win10/11) for
  the native window; otherwise Hangar falls back to an Edge/Chrome `--app`
  window. Linux uses the `--app` window / browser fallback.
- For one loose file instead of a folder, add `--onefile` (slower to start;
  also trips more AV false-positives on Windows).

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

"""Local SQLite store for Hangar.

Keeps the asset index, tags, collections, library folders and settings.
Everything lives under ~/.hangar so the tool is fully local and portable.
"""

import os
import re
import sqlite3
import time
from pathlib import Path

DATA_DIR = Path(os.environ.get("HANGAR_HOME", Path.home() / ".hangar"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
THUMB_DIR = DATA_DIR / "thumbs"
THUMB_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "hangar.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS libraries (
    id        INTEGER PRIMARY KEY,
    path      TEXT UNIQUE NOT NULL,
    name      TEXT NOT NULL,
    added_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS assets (
    id          INTEGER PRIMARY KEY,
    path        TEXT UNIQUE NOT NULL,
    name        TEXT NOT NULL,
    ext         TEXT NOT NULL,
    kind        TEXT NOT NULL,
    size        INTEGER NOT NULL,
    mtime       REAL NOT NULL,
    vertices    INTEGER,
    faces       INTEGER,
    stats_done  INTEGER NOT NULL DEFAULT 0,
    favorite    INTEGER NOT NULL DEFAULT 0,
    missing     INTEGER NOT NULL DEFAULT 0,
    set_key     TEXT NOT NULL DEFAULT '',
    map_role    TEXT NOT NULL DEFAULT '',
    map_order   INTEGER NOT NULL DEFAULT 50,
    blend_assets INTEGER,
    blend_missing_textures INTEGER NOT NULL DEFAULT 0,
    blend_packed_tex INTEGER NOT NULL DEFAULT 0,
    blend_external_tex INTEGER NOT NULL DEFAULT 0,
    subtype     TEXT NOT NULL DEFAULT '',
    resolution  TEXT NOT NULL DEFAULT '',
    author      TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    license     TEXT NOT NULL DEFAULT '',
    copyright   TEXT NOT NULL DEFAULT '',
    content_hash     TEXT NOT NULL DEFAULT '',
    content_hash_sig TEXT NOT NULL DEFAULT '',
    added_at    REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS tags (
    id     INTEGER PRIMARY KEY,
    name   TEXT UNIQUE NOT NULL,
    color  TEXT NOT NULL DEFAULT '#8A8F9A'
);
CREATE TABLE IF NOT EXISTS asset_tags (
    asset_id INTEGER NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
    tag_id   INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
    PRIMARY KEY (asset_id, tag_id)
);
CREATE TABLE IF NOT EXISTS collections (
    id    INTEGER PRIMARY KEY,
    name  TEXT UNIQUE NOT NULL
);
CREATE TABLE IF NOT EXISTS collection_assets (
    collection_id INTEGER NOT NULL REFERENCES collections(id) ON DELETE CASCADE,
    asset_id      INTEGER NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
    PRIMARY KEY (collection_id, asset_id)
);
CREATE TABLE IF NOT EXISTS categories (
    id        INTEGER PRIMARY KEY,
    name      TEXT UNIQUE NOT NULL,
    icon      TEXT NOT NULL DEFAULT '',
    sort      INTEGER NOT NULL DEFAULT 0,
    keywords  TEXT NOT NULL DEFAULT '',
    kind      TEXT NOT NULL DEFAULT '',
    parent_id INTEGER REFERENCES categories(id) ON DELETE SET NULL
);
CREATE TABLE IF NOT EXISTS asset_categories (
    category_id INTEGER NOT NULL REFERENCES categories(id) ON DELETE CASCADE,
    asset_id    INTEGER NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
    PRIMARY KEY (category_id, asset_id)
);
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);
CREATE INDEX IF NOT EXISTS idx_assets_kind ON assets(kind);
CREATE INDEX IF NOT EXISTS idx_assets_name ON assets(name);
CREATE INDEX IF NOT EXISTS idx_asset_categories_asset ON asset_categories(asset_id);
"""
# NOTE: the index on assets(set_key) is created in init_db() AFTER the column
# migration — never put it in SCHEMA. On a fresh DB the CREATE TABLE includes
# set_key, but on an upgrade the assets table predates the column, and a
# CREATE INDEX referencing set_key inside this script would run before the
# ALTER TABLE adds it ("no such column: set_key").

# Starter taxonomy seeded on first run. Each category carries a keyword list used
# to auto-suggest a category from an asset's folder/file name during scanning, and
# a `kind` scope: "model" categories only match models, "hdri" only HDRIs, and ""
# matches any kind (shared). Users can add their own; these are a sensible base.
DEFAULT_CATEGORIES = [
    # (name, icon, kind, [keywords])
    ("Sci-Fi",       "🚀", "model", ["scifi", "sci-fi", "spaceship", "spacecraft",
                            "space", "starship", "mech", "robot", "droid", "cyber",
                            "cyberpunk", "futuristic", "alien", "ufo", "laser"]),
    ("Buildings",    "🏢", "model", ["building", "buildings", "house", "home", "tower",
                            "skyscraper", "apartment", "office"]),
    ("Architecture", "🏛", "model", ["architecture", "interior", "exterior", "facade",
                            "room", "kitchen", "bathroom", "stairs", "wall"]),
    ("Vehicles",     "🚗", "model", ["vehicle", "car", "cars", "truck", "tank", "plane",
                            "aircraft", "jet", "ship", "boat", "motorcycle",
                            "bike", "bicycle", "train", "bus"]),
    ("Characters",   "🧍", "model", ["character", "char", "human", "person", "people",
                            "creature", "monster", "npc", "avatar", "zombie",
                            "soldier"]),
    ("Weapons",      "🗡", "model", ["weapon", "weapons", "gun", "guns", "rifle",
                            "pistol", "sword", "blade", "knife", "axe", "firearm",
                            "ammo", "grenade"]),
    ("Furniture",    "🛋", "model", ["furniture", "chair", "table", "sofa", "couch",
                            "desk", "bed", "shelf", "cabinet", "lamp"]),
    ("Props",        "📦", "model", ["prop", "props", "barrel", "crate", "box",
                            "container"]),
    ("Industrial",   "🏭", "model", ["industrial", "machine", "machinery", "pipe",
                            "pipes", "factory", "mechanical", "engine", "gear"]),
    ("Fantasy",      "🐉", "model", ["fantasy", "medieval", "castle", "dragon",
                            "magic", "wizard", "knight", "dungeon"]),
    ("Food",         "🍎", "model", ["food", "fruit", "drink", "meal", "vegetable",
                            "bottle"]),
    ("Nature",       "🌲", "model", ["nature", "tree", "trees", "plant", "plants",
                            "rock", "rocks", "terrain", "foliage", "grass",
                            "environment", "landscape", "forest", "flower", "mountain"]),
    # HDRI environment categories, modelled on Poly Haven's taxonomy.
    ("Outdoor",      "🌤", "hdri", ["outdoor", "exterior", "outside", "field",
                            "park", "garden", "courtyard"]),
    ("Skies",        "☁", "hdri", ["sky", "skies", "cloud", "clouds", "cloudy",
                            "overcast", "clear"]),
    ("Indoor",       "🚪", "hdri", ["indoor", "interior", "inside", "room", "hall",
                            "office", "warehouse"]),
    ("Studio",       "💡", "hdri", ["studio", "softbox", "photostudio"]),
    ("Sunrise/Sunset", "🌅", "hdri", ["sunrise", "sunset", "dusk", "dawn", "golden",
                            "evening", "morning"]),
    ("Night",        "🌙", "hdri", ["night", "nighttime", "midnight", "stars",
                            "starry", "moonlit", "moon"]),
    ("Urban",        "🏙", "hdri", ["urban", "city", "street", "town", "rooftop",
                            "alley"]),
    # Texture surface categories, modelled on Poly Haven's texture taxonomy.
    ("Wood",         "🪵", "texture", ["wood", "wooden", "plank", "planks",
                            "parquet", "timber", "bark", "log"]),
    ("Bricks",       "🧱", "texture", ["brick", "bricks", "brickwall"]),
    ("Concrete",     "⬜", "texture", ["concrete", "cement"]),
    ("Metal",        "⚙", "texture", ["metal", "metallic", "steel", "iron",
                            "rust", "rusty", "rusted", "aluminium", "aluminum",
                            "copper", "bronze", "brass"]),
    ("Stone",        "🪨", "texture", ["stone", "cobble", "cobblestone",
                            "granite", "slate", "pebble", "pebbles"]),
    ("Tiles",        "🔲", "texture", ["tile", "tiles", "tiling"]),
    ("Fabric",       "🧵", "texture", ["fabric", "cloth", "textile", "denim",
                            "wool", "cotton", "linen", "canvas"]),
    ("Ground",       "🟫", "texture", ["ground", "dirt", "soil", "mud",
                            "terrain", "sand", "gravel", "moss"]),
    ("Plaster",      "🎨", "texture", ["plaster", "stucco"]),
    ("Marble",       "🔘", "texture", ["marble"]),
    ("Roof",         "🏠", "texture", ["roof", "roofing", "shingle", "shingles"]),
    ("Leather",      "🟤", "texture", ["leather", "hide"]),
    ("Plastic",      "🧴", "texture", ["plastic", "rubber"]),
    ("Paper",        "📄", "texture", ["paper", "cardboard"]),
    ("Asphalt",      "🛣", "texture", ["asphalt", "tarmac"]),
]
# {category_id: (name, set(keywords))} cache, built lazily from the DB and
# invalidated whenever a category is created/edited/removed. See _matchers().
_CATEGORY_MATCHERS = None


def connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    with connect() as conn:
        conn.executescript(SCHEMA)
        # Migrate older DBs that predate newer columns (ALTER is idempotent here
        # because we guard on the live column set).
        cat_cols = {r["name"] for r in conn.execute("PRAGMA table_info(categories)")}
        if "keywords" not in cat_cols:
            conn.execute("ALTER TABLE categories ADD COLUMN keywords TEXT NOT NULL DEFAULT ''")
        if "kind" not in cat_cols:
            conn.execute("ALTER TABLE categories ADD COLUMN kind TEXT NOT NULL DEFAULT ''")
        if "parent_id" not in cat_cols:
            conn.execute(
                "ALTER TABLE categories ADD COLUMN parent_id "
                "INTEGER REFERENCES categories(id) ON DELETE SET NULL")
        asset_cols = {r["name"] for r in conn.execute("PRAGMA table_info(assets)")}
        for col, ddl in (
            ("set_key",   "ALTER TABLE assets ADD COLUMN set_key TEXT NOT NULL DEFAULT ''"),
            ("map_role",  "ALTER TABLE assets ADD COLUMN map_role TEXT NOT NULL DEFAULT ''"),
            ("map_order", "ALTER TABLE assets ADD COLUMN map_order INTEGER NOT NULL DEFAULT 50"),
            ("blend_assets", "ALTER TABLE assets ADD COLUMN blend_assets INTEGER"),
            ("blend_missing_textures", "ALTER TABLE assets ADD COLUMN blend_missing_textures INTEGER NOT NULL DEFAULT 0"),
            ("blend_packed_tex", "ALTER TABLE assets ADD COLUMN blend_packed_tex INTEGER NOT NULL DEFAULT 0"),
            ("blend_external_tex", "ALTER TABLE assets ADD COLUMN blend_external_tex INTEGER NOT NULL DEFAULT 0"),
            ("subtype",    "ALTER TABLE assets ADD COLUMN subtype TEXT NOT NULL DEFAULT ''"),
            ("resolution", "ALTER TABLE assets ADD COLUMN resolution TEXT NOT NULL DEFAULT ''"),
            # Aggregated searchable text from a .blend's marked-asset metadata
            # (asset names + tags + author + catalog), so search can reach inside.
            ("blend_meta", "ALTER TABLE assets ADD COLUMN blend_meta TEXT NOT NULL DEFAULT ''"),
            # File-level metadata the user edits in Hangar (any asset, no marking).
            ("author",      "ALTER TABLE assets ADD COLUMN author TEXT NOT NULL DEFAULT ''"),
            ("description", "ALTER TABLE assets ADD COLUMN description TEXT NOT NULL DEFAULT ''"),
            ("license",     "ALTER TABLE assets ADD COLUMN license TEXT NOT NULL DEFAULT ''"),
            ("copyright",   "ALTER TABLE assets ADD COLUMN copyright TEXT NOT NULL DEFAULT ''"),
            # Content-duplicate detection: BLAKE2b of the file's bytes, plus the
            # size:mtime signature captured at hash time so a changed file gets
            # re-hashed on the next duplicates scan.
            ("content_hash",     "ALTER TABLE assets ADD COLUMN content_hash TEXT NOT NULL DEFAULT ''"),
            ("content_hash_sig", "ALTER TABLE assets ADD COLUMN content_hash_sig TEXT NOT NULL DEFAULT ''"),
        ):
            if col not in asset_cols:
                conn.execute(ddl)
        # Safe now that set_key is guaranteed to exist (fresh CREATE TABLE or the
        # ALTER above). Must come after the migration — see the SCHEMA note.
        conn.execute("CREATE INDEX IF NOT EXISTS idx_assets_set_key ON assets(set_key)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_assets_content_hash ON assets(content_hash)")
        # One-time backfill: rows indexed before set_key existed migrated in with
        # set_key='' and would collapse into a single group=set tile. Give every
        # asset its unique path, then re-derive proper map-set grouping for
        # textures so existing libraries group correctly without a manual rescan.
        done = conn.execute(
            "SELECT 1 FROM settings WHERE key='set_key_backfilled'").fetchone()
        if not done:
            conn.execute("UPDATE assets SET set_key=path "
                         "WHERE set_key IS NULL OR set_key=''")
            try:
                import scanner  # lazy: avoids an import cycle at module load
                for r in conn.execute(
                        "SELECT id, path FROM assets WHERE kind='texture'").fetchall():
                    folder = os.path.dirname(r["path"])
                    name_noext = os.path.splitext(os.path.basename(r["path"]))[0]
                    sk, role, order = scanner.texture_set_info(folder, name_noext)
                    conn.execute(
                        "UPDATE assets SET set_key=?, map_role=?, map_order=? WHERE id=?",
                        (sk, role, order, r["id"]))
            except Exception:
                pass  # path-based set_key already prevents the collapse
            conn.execute(
                "INSERT OR REPLACE INTO settings(key, value) VALUES('set_key_backfilled','1')")
        # One-time backfill: derive subtype (decal/atlas) + resolution facets for
        # images already indexed before those columns existed.
        if not conn.execute(
                "SELECT 1 FROM settings WHERE key='facets_backfilled'").fetchone():
            try:
                import scanner  # lazy: avoids an import cycle at module load
                for r in conn.execute(
                        "SELECT id, path FROM assets "
                        "WHERE kind IN ('texture','hdri')").fetchall():
                    folder = os.path.dirname(r["path"])
                    name_noext = os.path.splitext(os.path.basename(r["path"]))[0]
                    subtype, resolution = scanner.texture_facets(folder, name_noext)
                    conn.execute(
                        "UPDATE assets SET subtype=?, resolution=? WHERE id=?",
                        (subtype, resolution, r["id"]))
            except Exception:
                pass
            conn.execute(
                "INSERT OR REPLACE INTO settings(key, value) VALUES('facets_backfilled','1')")
        # Sensible default tag palette so new users aren't staring at a blank wall.
        defaults = [
            ("hero", "#E8B04B"), ("wip", "#E87D3E"), ("approved", "#3DBE8B"),
            ("client", "#5B8DEF"), ("retopo-needed", "#C7596B"),
        ]
        for name, color in defaults:
            conn.execute(
                "INSERT OR IGNORE INTO tags(name, color) VALUES (?, ?)", (name, color)
            )
        # Seed the starter category taxonomy (Sci-Fi, Outdoor, …) with its keyword
        # rules and kind scope. On upgrade, back-fill keywords/kind for seeded
        # categories that are still blank — but never clobber a user's edits.
        for sort, (name, icon, kind, kws) in enumerate(DEFAULT_CATEGORIES):
            conn.execute(
                "INSERT OR IGNORE INTO categories(name, icon, sort, keywords, kind) "
                "VALUES (?, ?, ?, ?, ?)",
                (name, icon, sort, ",".join(kws), kind),
            )
            conn.execute(
                "UPDATE categories SET keywords=? WHERE name=? AND keywords=''",
                (",".join(kws), name),
            )
            if kind:
                conn.execute(
                    "UPDATE categories SET kind=? WHERE name=? AND kind=''",
                    (kind, name),
                )
        # One-time: after new default categories ship (e.g. the texture set),
        # back-fill auto-classification across the existing library so they're
        # populated without the user having to hit ⚡. Bumped flag = re-run once.
        need_reclassify = not conn.execute(
            "SELECT 1 FROM settings WHERE key='autoclassify_v2'").fetchone()
    _invalidate_matchers()
    if need_reclassify:
        try:
            auto_categorize_all()
        except Exception:
            pass
        set_setting("autoclassify_v2", "1")


# ---- settings -------------------------------------------------------------

def get_setting(key, default=None):
    with connect() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key, value):
    with connect() as conn:
        conn.execute(
            "INSERT INTO settings(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )


# ---- libraries ------------------------------------------------------------

def add_library(path, name=None):
    path = str(Path(path).expanduser().resolve())
    name = name or Path(path).name
    with connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO libraries(path, name, added_at) VALUES (?, ?, ?)",
            (path, name, time.time()),
        )
        row = conn.execute("SELECT * FROM libraries WHERE path=?", (path,)).fetchone()
    return dict(row)


def remove_library(library_id):
    with connect() as conn:
        row = conn.execute("SELECT path FROM libraries WHERE id=?", (library_id,)).fetchone()
        if not row:
            return
        prefix = row["path"]
        # Drop assets that lived under this library root.
        conn.execute("DELETE FROM assets WHERE path LIKE ?", (prefix + os.sep + "%",))
        conn.execute("DELETE FROM libraries WHERE id=?", (library_id,))


def list_libraries():
    with connect() as conn:
        rows = conn.execute("SELECT * FROM libraries ORDER BY name").fetchall()
        libs = []
        for r in rows:
            d = dict(r)
            # Is the source folder reachable right now? (drive unplugged, network
            # share down, moved, or no permission all read as unavailable.)
            d["available"] = os.path.isdir(d["path"])
            d["asset_count"] = conn.execute(
                "SELECT COUNT(*) c FROM assets WHERE path LIKE ?",
                (d["path"].rstrip("/\\") + os.sep + "%",)).fetchone()["c"]
            libs.append(d)
    return libs


# ---- assets ---------------------------------------------------------------

def _upsert_asset(conn, meta):
    set_key = meta.get("set_key") or meta["path"]
    map_role = meta.get("map_role", "")
    map_order = meta.get("map_order", 50)
    subtype = meta.get("subtype", "")
    resolution = meta.get("resolution", "")
    existing = conn.execute(
        "SELECT id, mtime FROM assets WHERE path=?", (meta["path"],)
    ).fetchone()
    if existing:
        # If the file changed on disk, invalidate cached mesh stats.
        stats_reset = meta["mtime"] != existing["mtime"]
        conn.execute(
            "UPDATE assets SET name=?, ext=?, kind=?, size=?, mtime=?, "
            "set_key=?, map_role=?, map_order=?, subtype=?, resolution=?, missing=0"
            + (", stats_done=0, vertices=NULL, faces=NULL" if stats_reset else "")
            + " WHERE id=?",
            (meta["name"], meta["ext"], meta["kind"], meta["size"],
             meta["mtime"], set_key, map_role, map_order, subtype, resolution,
             existing["id"]),
        )
        return existing["id"]
    cur = conn.execute(
        "INSERT INTO assets(path, name, ext, kind, size, mtime, "
        "set_key, map_role, map_order, subtype, resolution, author, added_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (meta["path"], meta["name"], meta["ext"], meta["kind"],
         meta["size"], meta["mtime"], set_key, map_role, map_order,
         subtype, resolution, meta.get("author", ""), time.time()),
    )
    # Auto-suggest categories for any new asset from its folder/file name.
    _auto_categorize(conn, cur.lastrowid, meta["path"], meta["kind"])
    return cur.lastrowid


def upsert_asset(meta):
    """meta: dict with path, name, ext, kind, size, mtime, set_key, map_role,
    map_order (the last three default sensibly when absent)."""
    with connect() as conn:
        return _upsert_asset(conn, meta)


def upsert_assets(metas):
    """Upsert many assets in one SQLite transaction and return their ids."""
    ids = []
    with connect() as conn:
        for meta in metas:
            ids.append(_upsert_asset(conn, meta))
    return ids


def mark_missing(seen_ids, library_path):
    """Flag assets under a library that weren't seen in the latest scan."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT id FROM assets WHERE path LIKE ?",
            (library_path + os.sep + "%",),
        ).fetchall()
        for r in rows:
            if r["id"] not in seen_ids:
                conn.execute("UPDATE assets SET missing=1 WHERE id=?", (r["id"],))


def delete_missing():
    """Permanently remove all missing assets from the index. Returns the count deleted."""
    with connect() as conn:
        n = conn.execute("SELECT COUNT(*) c FROM assets WHERE missing=1").fetchone()["c"]
        conn.execute("DELETE FROM assets WHERE missing=1")
    return n


def save_stats(asset_id, vertices, faces):
    with connect() as conn:
        conn.execute(
            "UPDATE assets SET vertices=?, faces=?, stats_done=1 WHERE id=?",
            (vertices, faces, asset_id),
        )


def save_blend_asset_count(asset_id, count):
    """Persist the number of datablocks marked as assets inside a .blend.

    ``count`` may be None when the file could not be parsed; we only store
    real integers so a failed parse can be retried later."""
    if count is None:
        return
    with connect() as conn:
        conn.execute(
            "UPDATE assets SET blend_assets=? WHERE id=?",
            (int(count), asset_id),
        )


def rename_asset(asset_id, new_path, new_name):
    """Point an existing asset row at a renamed file on disk (same id, new
    path + display name). The caller is responsible for the actual os.rename."""
    with connect() as conn:
        conn.execute(
            "UPDATE assets SET path=?, name=? WHERE id=?",
            (new_path, new_name, asset_id),
        )


def source_folder(path, root):
    """The 'source pack' folder for an asset: the first folder under its library
    root (e.g. .../assets/<Poly Haven>/... -> 'Poly Haven'). A file sitting
    directly in the root falls back to the root folder's own name. String-based
    (not os.path) so it works on stored Windows paths regardless of host OS."""
    p = (path or "").replace("\\", "/")
    r = (root or "").replace("\\", "/").rstrip("/")
    if not r or not p.lower().startswith(r.lower() + "/"):
        return ""
    parts = [x for x in p[len(r) + 1:].split("/") if x]
    if len(parts) >= 2:
        return parts[0]                       # first folder beneath the root
    return r.split("/")[-1] or ""             # file directly in root -> root name


def backfill_source_authors(force=False):
    """Set each asset's Author to its source-pack folder. Only fills empty
    Authors unless force=True, so it never overwrites what the user has typed —
    and since Author is stored, it stays put when a file is later moved."""
    with connect() as conn:
        roots = [r["path"] for r in conn.execute("SELECT path FROM libraries")]
        roots.sort(key=len, reverse=True)     # longest (most specific) root wins
        where = "" if force else "WHERE author='' OR author IS NULL"
        rows = conn.execute(f"SELECT id, path FROM assets {where}").fetchall()
        n = 0
        for row in rows:
            src = next((s for s in (source_folder(row["path"], rt) for rt in roots) if s), "")
            if not src:
                # Not under any known library root (path form differs, library
                # removed, etc.) — fall back to the file's immediate parent folder
                # so every file still gets an origin rather than staying blank.
                parent = os.path.dirname((row["path"] or "").replace("\\", "/")).rstrip("/")
                src = parent.split("/")[-1] if parent else ""
            if src:
                conn.execute("UPDATE assets SET author=? WHERE id=?", (src, row["id"]))
                n += 1
    return n


def set_asset_details(asset_id, author, description, license, copyright):
    """Store the user-editable file-level metadata for an asset."""
    with connect() as conn:
        conn.execute(
            "UPDATE assets SET author=?, description=?, license=?, copyright=? WHERE id=?",
            (author or "", description or "", license or "", copyright or "", asset_id),
        )


def set_blend_meta(asset_id, text, missing_textures=None,
                   packed_tex=None, external_tex=None):
    """Store aggregated .blend metadata and, when known, texture counts
    (missing, packed/embedded, external/linked)."""
    updates = ["blend_meta=?"]
    params = [text or ""]
    if missing_textures is not None:
        updates.append("blend_missing_textures=?")
        params.append(max(0, int(missing_textures or 0)))
    if packed_tex is not None:
        updates.append("blend_packed_tex=?")
        params.append(max(0, int(packed_tex or 0)))
    if external_tex is not None:
        updates.append("blend_external_tex=?")
        params.append(max(0, int(external_tex or 0)))
    params.append(asset_id)
    with connect() as conn:
        conn.execute(
            f"UPDATE assets SET {', '.join(updates)} WHERE id=?",
            params,
        )


def blend_meta_targets():
    """(id, path, mtime) for every indexed .blend — for the metadata-index pass."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT id, path, mtime FROM assets WHERE missing=0 AND ext='.blend'"
        ).fetchall()
    return [dict(r) for r in rows]


def existing_blend_names():
    """Set of lowercased base names (no extension) of every .blend asset in the
    library. Used to tell whether a marked datablock has its own .blend file."""
    with connect() as conn:
        return {
            r["name"].lower()
            for r in conn.execute(
                "SELECT name FROM assets WHERE ext='.blend' AND missing=0"
            ).fetchall()
        }


def get_asset(asset_id):
    with connect() as conn:
        row = conn.execute("SELECT * FROM assets WHERE id=?", (asset_id,)).fetchone()
        if not row:
            return None
        asset = dict(row)
        asset["tags"] = _tags_for(conn, asset_id)
        asset["collections"] = [
            r["name"] for r in conn.execute(
                "SELECT c.name FROM collections c "
                "JOIN collection_assets ca ON ca.collection_id=c.id "
                "WHERE ca.asset_id=?", (asset_id,)
            ).fetchall()
        ]
        asset["categories"] = [
            r["name"] for r in conn.execute(
                "SELECT cat.name FROM categories cat "
                "JOIN asset_categories ac ON ac.category_id=cat.id "
                "WHERE ac.asset_id=?", (asset_id,)
            ).fetchall()
        ]
    return asset


def _tags_for(conn, asset_id):
    return [
        {"name": r["name"], "color": r["color"]}
        for r in conn.execute(
            "SELECT t.name, t.color FROM tags t "
            "JOIN asset_tags at ON at.tag_id=t.id WHERE at.asset_id=? ORDER BY t.name",
            (asset_id,),
        ).fetchall()
    ]


def model_ext_counts():
    """Count of model assets per file extension, for sidebar subcategories."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT ext, COUNT(*) c FROM assets WHERE missing=0 AND kind='model' "
            "GROUP BY ext ORDER BY c DESC"
        ).fetchall()
    return {r["ext"]: r["c"] for r in rows}


def iter_thumb_targets():
    """Minimal rows for background thumbnail warming: id, path, ext, kind, mtime
    for every present (non-missing) asset. HDRIs sort first so environment
    previews appear quickly; models are warmed last because Blender renders are
    the slow path."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT id, path, ext, kind, mtime FROM assets WHERE missing=0 "
            "ORDER BY CASE kind "
            "WHEN 'hdri' THEN 0 WHEN 'texture' THEN 1 "
            "WHEN 'material' THEN 2 WHEN 'model' THEN 3 ELSE 4 END, "
            "CASE ext WHEN '.hdr' THEN 0 WHEN '.exr' THEN 2 ELSE 1 END, id"
        ).fetchall()
    return [dict(r) for r in rows]


def iter_dup_hash_targets():
    """Assets that still need content-hashing for the duplicates view. Only
    files whose byte size collides with another live file can possibly be exact
    duplicates, and of those only ones never hashed — or whose file changed
    since (the sig is size:mtime captured at hash time) — need work."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT id, path, size, mtime, content_hash, content_hash_sig "
            "FROM assets WHERE missing=0 AND size IN ("
            "SELECT size FROM assets WHERE missing=0 "
            "GROUP BY size HAVING COUNT(*) > 1)"
        ).fetchall()
    out = []
    for r in rows:
        sig = f"{r['size']}:{r['mtime']}"
        if not r["content_hash"] or r["content_hash_sig"] != sig:
            out.append({"id": r["id"], "path": r["path"], "sig": sig})
    return out


def set_content_hash(asset_id, content_hash, sig):
    """Store one asset's content hash (empty hash = unreadable, retried on the
    next duplicates scan only if the file's size/mtime changes)."""
    with connect() as conn:
        conn.execute(
            "UPDATE assets SET content_hash=?, content_hash_sig=? WHERE id=?",
            (content_hash, sig, asset_id))


def query_assets(search="", kind="", ext="", tag="", collection="", category="",
                 folder="", favorite=False, sort="name", limit=200, offset=0,
                 group="", set_key="", with_categories=False,
                 subtype="", resolution="", missing=False,
                 missing_blend_textures=False, duplicates=False, no_author=False,
                 linked=False):
    clauses = ["a.missing=1"] if missing else ["a.missing=0"]
    if no_author:
        clauses.append("(a.author='' OR a.author IS NULL)")
    if linked:
        clauses.append("a.blend_external_tex>0")   # .blend files referencing external textures
    if duplicates:
        # Only assets whose file CONTENT is byte-identical to another indexed
        # file — same BLAKE2b hash, computed by the duplicates scan in app.py.
        # A shared name is neither necessary (renamed copies still match) nor
        # sufficient (same-named different files don't).
        clauses.append(
            "a.content_hash != '' AND a.content_hash IN ("
            "SELECT content_hash FROM assets WHERE missing=0 AND content_hash != '' "
            "GROUP BY content_hash HAVING COUNT(*) > 1)"
        )
    if missing_blend_textures:
        clauses.append("a.ext='.blend'")
        clauses.append("a.blend_missing_textures>0")
    joins = ""
    # Placeholders in the final SQL appear JOINs-first (text precedes WHERE), so
    # params must be ordered the same way. Keep join params and where-clause
    # params in separate lists and concatenate joins-first — appending to one
    # flat list in code order silently mis-binds any join+clause combination.
    join_params = []
    where_params = []
    if set_key:
        # Listing the individual files of one texture set — overrides grouping.
        clauses.append("a.set_key=?")
        where_params.append(set_key)
        group = ""
    if search:
        # Match the file name, the user's file-level metadata (author/
        # description), OR the aggregated .blend metadata (marked-asset names,
        # tags, author, catalog) — so search reaches all of it.
        clauses.append(
            "(a.name LIKE ? OR a.blend_meta LIKE ? OR a.author LIKE ? OR a.description LIKE ?)")
        where_params += [f"%{search}%"] * 4
    if kind:
        clauses.append("a.kind=?")
        where_params.append(kind)
    if subtype:
        clauses.append("a.subtype=?")
        where_params.append(subtype)
    if resolution:
        clauses.append("a.resolution=?")
        where_params.append(resolution)
    if ext:
        # ext may be comma-separated for grouped formats (e.g. ".glb,.gltf")
        exts = [e.strip() for e in ext.split(",") if e.strip()]
        if len(exts) == 1:
            clauses.append("a.ext=?")
            where_params.append(exts[0])
        elif len(exts) > 1:
            placeholders = ",".join("?" * len(exts))
            clauses.append(f"a.ext IN ({placeholders})")
            where_params.extend(exts)
    if favorite:
        clauses.append("a.favorite=1")
    if tag:
        joins += (" JOIN asset_tags fat ON fat.asset_id=a.id "
                  " JOIN tags ft ON ft.id=fat.tag_id AND ft.name=?")
        join_params.append(tag)
    if collection:
        joins += (" JOIN collection_assets fca ON fca.asset_id=a.id "
                  " JOIN collections fc ON fc.id=fca.collection_id AND fc.name=?")
        join_params.append(collection)
    if category:
        joins += (" JOIN asset_categories fac ON fac.asset_id=a.id "
                  " JOIN categories fcat ON fcat.id=fac.category_id AND fcat.name=?")
        join_params.append(category)
    if folder:
        # Match every asset living under this folder root (any depth).
        prefix = folder.rstrip("/\\")
        clauses.append("a.path LIKE ?")
        where_params.append(prefix + os.sep + "%")

    params = join_params + where_params

    def order_for(alias):
        return {
            "name": f"{alias}.name COLLATE NOCASE ASC",
            "recent": f"{alias}.added_at DESC",
            "size": f"{alias}.size DESC",
            "modified": f"{alias}.mtime DESC",
        }.get(sort, f"{alias}.name COLLATE NOCASE ASC")

    where = " AND ".join(clauses)

    if group == "set":
        # Collapse texture-map sets into one representative tile each. The pick
        # is the lowest map_order (diffuse beats normal/roughness/…), tie-broken
        # by id; set_count carries how many maps the set holds.
        # An empty set_key (e.g. pre-set_key rows migrated in before a re-scan)
        # must NOT collapse together — fall back to the unique path so each such
        # asset stays its own tile.
        gkey = "(CASE WHEN a.set_key IS NULL OR a.set_key='' THEN a.path ELSE a.set_key END)"
        sql = (
            f"SELECT g.* FROM ("
            f"  SELECT a.*, "
            f"    COUNT(*)    OVER (PARTITION BY {gkey}) AS set_count, "
            f"    ROW_NUMBER() OVER (PARTITION BY {gkey} "
            f"                       ORDER BY a.map_order, a.id) AS rn "
            f"  FROM assets a {joins} WHERE {where}"
            f") g WHERE g.rn = 1 "
            f"ORDER BY {order_for('g')} LIMIT ? OFFSET ?"
        )
        count_sql = (f"SELECT COUNT(DISTINCT {gkey}) c "
                     f"FROM assets a {joins} WHERE {where}")
    else:
        sql = (f"SELECT DISTINCT a.* FROM assets a {joins} WHERE {where} "
               f"ORDER BY {order_for('a')} LIMIT ? OFFSET ?")
        count_sql = (f"SELECT COUNT(DISTINCT a.id) c "
                     f"FROM assets a {joins} WHERE {where}")

    with connect() as conn:
        rows = conn.execute(sql, params + [limit, offset]).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d.setdefault("set_count", 1)
            d["tags"] = _tags_for(conn, r["id"])
            out.append(d)
        # Batch-attach each asset's categories (for the grouped grid view).
        if with_categories and out:
            ids = [d["id"] for d in out]
            ph = ",".join("?" * len(ids))
            cat_rows = conn.execute(
                f"SELECT ac.asset_id, cat.name FROM asset_categories ac "
                f"JOIN categories cat ON cat.id=ac.category_id "
                f"WHERE ac.asset_id IN ({ph})", ids).fetchall()
            by_asset = {}
            for cr in cat_rows:
                by_asset.setdefault(cr["asset_id"], []).append(cr["name"])
            for d in out:
                d["categories"] = by_asset.get(d["id"], [])
        total = conn.execute(count_sql, params).fetchone()["c"]
    return out, total


_RES_ORDER = {"256": 0, "512": 1, "1k": 2, "2k": 3, "4k": 4, "8k": 5, "16k": 6}


def facet_counts(kind=""):
    """Available subtype + resolution facets (with counts) across live assets,
    optionally scoped to one kind. Drives the faceted-filter strip so it only
    ever offers values that actually match something."""
    clauses = ["missing=0"]
    params = []
    if kind:
        clauses.append("kind=?")
        params.append(kind)
    where = " AND ".join(clauses)
    with connect() as conn:
        sub = conn.execute(
            f"SELECT subtype AS v, COUNT(*) c FROM assets "
            f"WHERE {where} AND subtype!='' GROUP BY subtype", params).fetchall()
        res = conn.execute(
            f"SELECT resolution AS v, COUNT(*) c FROM assets "
            f"WHERE {where} AND resolution!='' GROUP BY resolution", params).fetchall()
    subtypes = [{"value": r["v"], "count": r["c"]} for r in sub]
    resolutions = sorted(
        ({"value": r["v"], "count": r["c"]} for r in res),
        key=lambda d: _RES_ORDER.get(d["value"], 99))
    return {"subtypes": subtypes, "resolutions": resolutions}


def set_members(asset_id):
    """All assets sharing the texture set of `asset_id`, diffuse-first."""
    with connect() as conn:
        row = conn.execute("SELECT set_key FROM assets WHERE id=?", (asset_id,)).fetchone()
        if not row:
            return []
        rows = conn.execute(
            "SELECT * FROM assets WHERE set_key=? AND missing=0 "
            "ORDER BY map_order, name COLLATE NOCASE",
            (row["set_key"],),
        ).fetchall()
    return [dict(r) for r in rows]


def set_favorite(asset_id, value):
    with connect() as conn:
        conn.execute("UPDATE assets SET favorite=? WHERE id=?",
                     (1 if value else 0, asset_id))


def kind_counts():
    with connect() as conn:
        rows = conn.execute(
            "SELECT kind, COUNT(*) c FROM assets WHERE missing=0 GROUP BY kind"
        ).fetchall()
        total = conn.execute("SELECT COUNT(*) c FROM assets WHERE missing=0").fetchone()["c"]
        favs = conn.execute(
            "SELECT COUNT(*) c FROM assets WHERE missing=0 AND favorite=1"
        ).fetchone()["c"]
        ext_rows = conn.execute(
            "SELECT ext, COUNT(*) c FROM assets WHERE missing=0 AND kind='model' "
            "GROUP BY ext ORDER BY c DESC"
        ).fetchall()
    with connect() as conn2:
        missing_count = conn2.execute(
            "SELECT COUNT(*) c FROM assets WHERE missing=1"
        ).fetchone()["c"]
        blend_missing_textures = conn2.execute(
            "SELECT COUNT(*) c FROM assets "
            "WHERE missing=0 AND ext='.blend' AND blend_missing_textures>0"
        ).fetchone()["c"]
        blend_missing_texture_refs = conn2.execute(
            "SELECT COALESCE(SUM(blend_missing_textures), 0) c FROM assets "
            "WHERE missing=0 AND ext='.blend'"
        ).fetchone()["c"]
    return {
        "by_kind": {r["kind"]: r["c"] for r in rows},
        "total": total,
        "favorites": favs,
        "model_by_ext": {r["ext"]: r["c"] for r in ext_rows},
        "missing": missing_count,
        "blend_missing_textures": blend_missing_textures,
        "blend_missing_texture_refs": blend_missing_texture_refs,
    }


# ---- tags & collections ---------------------------------------------------

def list_tags():
    with connect() as conn:
        rows = conn.execute(
            "SELECT t.name, t.color, COUNT(at.asset_id) c FROM tags t "
            "LEFT JOIN asset_tags at ON at.tag_id=t.id "
            "GROUP BY t.id ORDER BY t.name"
        ).fetchall()
    return [dict(r) for r in rows]


def create_tag(name, color="#8A8F9A"):
    with connect() as conn:
        conn.execute("INSERT OR IGNORE INTO tags(name, color) VALUES (?, ?)",
                     (name.strip(), color))


def set_asset_tags(asset_id, tag_names):
    with connect() as conn:
        conn.execute("DELETE FROM asset_tags WHERE asset_id=?", (asset_id,))
        for name in tag_names:
            name = name.strip()
            if not name:
                continue
            conn.execute("INSERT OR IGNORE INTO tags(name) VALUES (?)", (name,))
            tag = conn.execute("SELECT id FROM tags WHERE name=?", (name,)).fetchone()
            conn.execute(
                "INSERT OR IGNORE INTO asset_tags(asset_id, tag_id) VALUES (?, ?)",
                (asset_id, tag["id"]),
            )


def list_collections():
    with connect() as conn:
        rows = conn.execute(
            "SELECT c.name, COUNT(ca.asset_id) c FROM collections c "
            "LEFT JOIN collection_assets ca ON ca.collection_id=c.id "
            "GROUP BY c.id ORDER BY c.name"
        ).fetchall()
    return [dict(r) for r in rows]


def create_collection(name):
    with connect() as conn:
        conn.execute("INSERT OR IGNORE INTO collections(name) VALUES (?)", (name.strip(),))


def remove_asset(asset_id):
    """Remove an asset from the index (file stays on disk)."""
    with connect() as conn:
        conn.execute("DELETE FROM assets WHERE id=?", (asset_id,))


def batch_add_tag(asset_ids, tag_name):
    """Add a tag to multiple assets (creates tag if needed)."""
    with connect() as conn:
        conn.execute("INSERT OR IGNORE INTO tags(name) VALUES (?)", (tag_name.strip(),))
        tag = conn.execute("SELECT id FROM tags WHERE name=?", (tag_name.strip(),)).fetchone()
        for aid in asset_ids:
            conn.execute(
                "INSERT OR IGNORE INTO asset_tags(asset_id, tag_id) VALUES (?, ?)",
                (aid, tag["id"]),
            )


def set_collection_membership(collection_name, asset_id, add=True):
    with connect() as conn:
        conn.execute("INSERT OR IGNORE INTO collections(name) VALUES (?)", (collection_name,))
        coll = conn.execute("SELECT id FROM collections WHERE name=?",
                            (collection_name,)).fetchone()
        if add:
            conn.execute(
                "INSERT OR IGNORE INTO collection_assets(collection_id, asset_id) "
                "VALUES (?, ?)", (coll["id"], asset_id),
            )
        else:
            conn.execute(
                "DELETE FROM collection_assets WHERE collection_id=? AND asset_id=?",
                (coll["id"], asset_id),
            )


# ---- categories -----------------------------------------------------------

def list_categories():
    with connect() as conn:
        rows = conn.execute(
            "SELECT cat.id, cat.name, cat.icon, cat.keywords, cat.kind, cat.parent_id, "
            "COUNT(ac.asset_id) c FROM categories cat "
            "LEFT JOIN asset_categories ac ON ac.category_id=cat.id "
            "GROUP BY cat.id ORDER BY cat.name COLLATE NOCASE"
        ).fetchall()
    return [dict(r) for r in rows]


def _category_descendant_ids(conn, cat_id):
    """Every category id nested under `cat_id` (any depth), for the cycle guard."""
    ids, frontier = set(), [cat_id]
    while frontier:
        rows = conn.execute(
            "SELECT id FROM categories WHERE parent_id=?", (frontier.pop(),)
        ).fetchall()
        for r in rows:
            if r["id"] not in ids:
                ids.add(r["id"]); frontier.append(r["id"])
    return ids


def set_category_parent(cat_id, parent_id):
    """Nest a category under another (or clear its nesting with parent_id=None).
    Refuses a move that would make a category its own ancestor. Returns
    (ok, error_or_None)."""
    with connect() as conn:
        if parent_id is not None:
            if parent_id == cat_id:
                return False, "A category can't be nested under itself."
            if parent_id in _category_descendant_ids(conn, cat_id):
                return False, "That would nest a category inside its own child."
            row = conn.execute("SELECT kind FROM categories WHERE id=?", (parent_id,)).fetchone()
            if row is None:
                return False, "Target category not found."
        conn.execute("UPDATE categories SET parent_id=? WHERE id=?", (parent_id, cat_id))
    return True, None


def category_folder_counts():
    """Immediate parent folders represented inside each category.

    The sidebar uses this to show e.g. Furniture > Beds, while keeping the
    existing category membership model unchanged. Counts are by indexed asset
    row, not by physical directory size.
    """
    with connect() as conn:
        rows = conn.execute(
            "SELECT cat.name category, cat.kind, a.path "
            "FROM categories cat "
            "JOIN asset_categories ac ON ac.category_id=cat.id "
            "JOIN assets a ON a.id=ac.asset_id "
            "WHERE a.missing=0 "
            "ORDER BY cat.sort, cat.name COLLATE NOCASE, a.path"
        ).fetchall()
    by_key = {}
    for r in rows:
        folder = os.path.dirname(r["path"])
        if not folder:
            continue
        key = (r["category"], r["kind"] or "", folder)
        item = by_key.setdefault(key, {
            "category": r["category"],
            "kind": r["kind"] or "",
            "path": folder,
            "name": os.path.basename(folder) or folder,
            "count": 0,
        })
        item["count"] += 1
    out = list(by_key.values())
    out.sort(key=lambda x: (x["category"].lower(), x["name"].lower()))
    return out


def create_category(name, icon="", keywords="", kind=""):
    name = (name or "").strip()
    if not name:
        return
    keywords = _clean_keywords(keywords)
    kind = (kind or "").strip().lower()
    if kind not in ("", "model", "texture", "hdri", "material"):
        kind = ""
    with connect() as conn:
        nxt = conn.execute("SELECT COALESCE(MAX(sort), -1) + 1 m FROM categories").fetchone()["m"]
        conn.execute(
            "INSERT OR IGNORE INTO categories(name, icon, sort, keywords, kind) "
            "VALUES (?, ?, ?, ?, ?)",
            (name, icon, nxt, keywords, kind),
        )
    _invalidate_matchers()


def update_category(category_id, keywords):
    """Replace a category's auto-match keyword rules."""
    with connect() as conn:
        conn.execute(
            "UPDATE categories SET keywords=? WHERE id=?",
            (_clean_keywords(keywords), category_id),
        )
    _invalidate_matchers()


def remove_category(category_id):
    with connect() as conn:
        conn.execute("DELETE FROM categories WHERE id=?", (category_id,))
    _invalidate_matchers()


def reorder_categories(ordered_ids):
    """Persist a new sidebar order. `ordered_ids` is the full list of category
    ids in the desired top-to-bottom order; each row's `sort` is set to its
    index. Ids not present keep their old sort (and sort after the listed ones)."""
    ids = []
    for cid in ordered_ids or []:
        try:
            ids.append(int(cid))
        except (TypeError, ValueError):
            continue
    if not ids:
        return
    with connect() as conn:
        for i, cid in enumerate(ids):
            conn.execute("UPDATE categories SET sort=? WHERE id=?", (i, cid))
    _invalidate_matchers()


def set_category_membership(category_name, asset_id, add=True):
    name = (category_name or "").strip()
    if not name:
        return
    with connect() as conn:
        conn.execute("INSERT OR IGNORE INTO categories(name) VALUES (?)", (name,))
        cat = conn.execute(
            "SELECT id, kind FROM categories WHERE name=?", (name,)
        ).fetchone()
        if add:
            # A category assignment is a MOVE, not an add: an asset lives in one
            # category at a time, so clear every other category it's in first.
            # (Was scoped to the same kind, which left an asset in two categories
            # whenever their kinds differed — e.g. a kind-less custom category.)
            conn.execute(
                "DELETE FROM asset_categories WHERE asset_id=?", (asset_id,))
            conn.execute(
                "INSERT OR IGNORE INTO asset_categories(category_id, asset_id) "
                "VALUES (?, ?)", (cat["id"], asset_id),
            )
        else:
            conn.execute(
                "DELETE FROM asset_categories WHERE category_id=? AND asset_id=?",
                (cat["id"], asset_id),
            )


def _clean_keywords(raw):
    """Normalise a keyword string/list into a comma-separated, lower-cased set."""
    if isinstance(raw, (list, tuple, set)):
        parts = raw
    else:
        parts = re.split(r"[,\n]+", str(raw or ""))
    seen = []
    for p in parts:
        p = p.strip().lower()
        if p and p not in seen:
            seen.append(p)
    return ",".join(seen)


def _invalidate_matchers():
    """Drop the cached keyword matchers so the next match rebuilds from the DB."""
    global _CATEGORY_MATCHERS
    _CATEGORY_MATCHERS = None


def _matchers(conn):
    """{category_id: (name, kind, set(keywords))} built once from the DB & cached.

    Cache is invalidated whenever categories are created/edited/removed, so
    user-defined categories take part in auto-classification just like the
    seeded ones. Categories with no keywords are skipped (manual-only). `kind`
    scopes a rule: "" matches any asset kind, otherwise only that kind.
    """
    global _CATEGORY_MATCHERS
    if _CATEGORY_MATCHERS is None:
        out = {}
        for r in conn.execute(
            "SELECT id, name, keywords, kind FROM categories"
        ).fetchall():
            kws = {k for k in (r["keywords"] or "").split(",") if k}
            if kws:
                out[r["id"]] = (r["name"], r["kind"] or "", kws)
        _CATEGORY_MATCHERS = out
    return _CATEGORY_MATCHERS


def _match_category_ids(path, asset_kind, matchers):
    """Best-match category id per kind scope for the given asset path.

    Splits the lower-cased path into word tokens and matches each keyword as a
    whole token (with simple singular/plural tolerance), so "car_sedan" hits
    Vehicles and a "vehicles" folder hits the "vehicle" keyword.

    Returns at most one category id per (kind) scope — the one with the most
    keyword hits — so auto-classification never places an asset in two sections
    of the same type view.
    """
    tokens = set(re.split(r"[^a-z0-9]+", path.lower()))
    tokens.discard("")

    def hit(kw):
        return (kw in tokens
                or (kw + "s") in tokens
                or (kw.endswith("s") and kw[:-1] in tokens))

    scored = []
    for cid, (_name, ckind, kws) in matchers.items():
        if not ckind or ckind == asset_kind:
            n = sum(1 for kw in kws if hit(kw))
            if n:
                scored.append((ckind, n, cid))
    # Keep only the best match per kind scope so one asset = one category.
    best: dict = {}
    for ckind, n, cid in scored:
        if ckind not in best or n > best[ckind][0]:
            best[ckind] = (n, cid)
    return [cid for _n, cid in best.values()]


def _auto_categorize(conn, asset_id, path, kind):
    """Attach every category whose keyword + kind rules match the asset.

    Runs inside the caller's transaction (shares `conn`) so the new asset row is
    visible to the foreign-key check. Adds links only; never removes membership a
    user set by hand.
    """
    for cid in _match_category_ids(path, kind, _matchers(conn)):
        conn.execute(
            "INSERT OR IGNORE INTO asset_categories(category_id, asset_id) VALUES (?, ?)",
            (cid, asset_id),
        )


def auto_categorize_all():
    """Re-apply keyword rules across the whole index (back-fill).

    Useful after adding/editing a category's keywords or importing assets that
    were indexed before a rule existed. Only adds memberships, so manual
    categorisation is preserved. Returns counts for a UI toast.
    """
    added = 0
    touched = set()
    with connect() as conn:
        matchers = _matchers(conn)
        if not matchers:
            return {"links_added": 0, "assets_matched": 0}
        for a in conn.execute(
            "SELECT id, path, kind FROM assets WHERE missing=0"
        ).fetchall():
            for cid in _match_category_ids(a["path"], a["kind"], matchers):
                cur = conn.execute(
                    "INSERT OR IGNORE INTO asset_categories(category_id, asset_id) "
                    "VALUES (?, ?)", (cid, a["id"]),
                )
                if cur.rowcount:
                    added += cur.rowcount
                    touched.add(a["id"])
    return {"links_added": added, "assets_matched": len(touched)}

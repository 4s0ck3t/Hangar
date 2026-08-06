"""Local SQLite store for Hangar.

Keeps the asset index, tags, collections, library folders and settings.
Everything lives under ~/.hangar so the tool is fully local and portable.
"""

import os
import re
import json
import shutil
import sqlite3
import stat
import time
from functools import lru_cache
from pathlib import Path

DATA_DIR = Path(os.environ.get("HANGAR_HOME", Path.home() / ".hangar"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
THUMB_DIR = DATA_DIR / "thumbs"
THUMB_DIR.mkdir(parents=True, exist_ok=True)
ORGANISE_RECEIPT_DIR = DATA_DIR / "organise_receipts"
ORGANISE_RECEIPT_DIR.mkdir(parents=True, exist_ok=True)
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
    blend_packed_texture_maps INTEGER NOT NULL DEFAULT 0,
    blend_packed_hdris INTEGER NOT NULL DEFAULT 0,
    blend_external_texture_maps INTEGER NOT NULL DEFAULT 0,
    blend_external_hdris INTEGER NOT NULL DEFAULT 0,
    subtype     TEXT NOT NULL DEFAULT '',
    resolution  TEXT NOT NULL DEFAULT '',
    author      TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    license     TEXT NOT NULL DEFAULT '',
    copyright   TEXT NOT NULL DEFAULT '',
    content_hash     TEXT NOT NULL DEFAULT '',
    content_hash_sig TEXT NOT NULL DEFAULT '',
    blend_corrupt    INTEGER NOT NULL DEFAULT 0,
    hidden      INTEGER NOT NULL DEFAULT 0,
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
                            "room", "kitchen", "stairs", "wall"]),
    ("Bathrooms",    "🚿", "model", ["bathroom", "bathrooms", "basin", "basins",
                            "toilet", "toilets", "bath", "bathtub", "baths",
                            "shower", "showers", "vanity", "vanities", "bidet"]),
    ("Kitchens",     "🍳", "model", ["kitchen", "kitchens", "countertop", "worktop",
                            "oven", "hob", "stove", "fridge", "refrigerator",
                            "dishwasher", "kitchenette"]),
    ("Bedrooms",     "🛏", "model", ["bedroom", "bedrooms", "bed", "beds", "wardrobe",
                            "nightstand", "bedside", "dresser"]),
    ("Living Rooms", "🛋", "model", ["living", "lounge", "sofa", "couch", "tv",
                            "television", "coffee", "console"]),
    ("Dining Rooms", "🍽", "model", ["dining", "dinner", "diningroom"]),
    ("Offices",      "💼", "model", ["office", "offices", "desk", "workstation",
                            "conference", "meeting"]),
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
    # Material categories mirror the surface taxonomy but stay separate from
    # single loose texture images.
    ("Wood materials",     "🪵", "material", ["wood", "wooden", "plank", "planks",
                            "parquet", "timber", "bark", "log"]),
    ("Brick materials",    "🧱", "material", ["brick", "bricks", "brickwall"]),
    ("Concrete materials", "⬜", "material", ["concrete", "cement"]),
    ("Metal materials",    "⚙", "material", ["metal", "metallic", "steel", "iron",
                            "rust", "rusty", "rusted", "aluminium", "aluminum",
                            "copper", "bronze", "brass"]),
    ("Stone materials",    "🪨", "material", ["stone", "cobble", "cobblestone",
                            "granite", "slate", "pebble", "pebbles"]),
    ("Tile materials",     "🔲", "material", ["tile", "tiles", "tiling"]),
    ("Fabric materials",   "🧵", "material", ["fabric", "cloth", "textile", "denim",
                            "wool", "cotton", "linen", "canvas"]),
    ("Ground materials",   "🟫", "material", ["ground", "dirt", "soil", "mud",
                            "terrain", "sand", "gravel", "moss"]),
    ("Plaster materials",  "🎨", "material", ["plaster", "stucco"]),
    ("Marble materials",   "🔘", "material", ["marble"]),
    ("Roof materials",     "🏠", "material", ["roof", "roofing", "shingle", "shingles"]),
    ("Leather materials",  "🟤", "material", ["leather", "hide"]),
    ("Plastic materials",  "🧴", "material", ["plastic", "rubber"]),
    ("Paper materials",    "📄", "material", ["paper", "cardboard"]),
    ("Asphalt materials",  "🛣", "material", ["asphalt", "tarmac"]),
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
            ("blend_packed_texture_maps", "ALTER TABLE assets ADD COLUMN blend_packed_texture_maps INTEGER NOT NULL DEFAULT 0"),
            ("blend_packed_hdris", "ALTER TABLE assets ADD COLUMN blend_packed_hdris INTEGER NOT NULL DEFAULT 0"),
            ("blend_external_texture_maps", "ALTER TABLE assets ADD COLUMN blend_external_texture_maps INTEGER NOT NULL DEFAULT 0"),
            ("blend_external_hdris", "ALTER TABLE assets ADD COLUMN blend_external_hdris INTEGER NOT NULL DEFAULT 0"),
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
            # Set by the .blend health check: file failed structural verification
            # (truncated / missing DNA block).
            ("blend_corrupt",    "ALTER TABLE assets ADD COLUMN blend_corrupt INTEGER NOT NULL DEFAULT 0"),
            # Soft-hide duplicate pack copies from normal browsing; files stay on disk
            # and can be restored from Hangar's duplicate-pack cleanup view.
            ("hidden",           "ALTER TABLE assets ADD COLUMN hidden INTEGER NOT NULL DEFAULT 0"),
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
        if not conn.execute(
                "SELECT 1 FROM settings WHERE key='author_source_repaired_v3'").fetchone():
            try:
                _repair_auto_source_authors(conn)
            except Exception:
                pass
            conn.execute(
                "INSERT OR REPLACE INTO settings(key, value) VALUES('author_source_repaired_v3','1')")
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
        if not conn.execute(
                "SELECT 1 FROM settings WHERE key='hdri_kind_repair_v1'").fetchone():
            try:
                _repair_hdri_texture_maps(conn)
            except Exception:
                pass
            conn.execute(
                "INSERT OR REPLACE INTO settings(key, value) VALUES('hdri_kind_repair_v1','1')")
        if not conn.execute(
                "SELECT 1 FROM settings WHERE key='material_kind_repair_v1'").fetchone():
            try:
                _repair_obvious_material_textures(conn)
            except Exception:
                pass
            conn.execute(
                "INSERT OR REPLACE INTO settings(key, value) VALUES('material_kind_repair_v1','1')")
        # One-time: after new default categories ship (e.g. the texture set),
        # back-fill auto-classification across the existing library so they're
        # populated without the user having to hit ⚡. Bumped flag = re-run once.
        need_reclassify = not conn.execute(
            "SELECT 1 FROM settings WHERE key='autoclassify_v2'").fetchone()
        need_bathrooms = not conn.execute(
            "SELECT 1 FROM settings WHERE key='bathroom_category_v2'").fetchone()
        need_rooms = not conn.execute(
            "SELECT 1 FROM settings WHERE key='room_categories_v1'").fetchone()
    _invalidate_matchers()
    if need_reclassify:
        try:
            auto_categorize_all()
        except Exception:
            pass
        set_setting("autoclassify_v2", "1")
    if need_bathrooms:
        try:
            promote_bathroom_category()
        except Exception:
            pass
        set_setting("bathroom_category_v2", "1")
    if need_rooms:
        try:
            promote_room_categories()
        except Exception:
            pass
        set_setting("room_categories_v1", "1")


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


def _repair_hdri_texture_maps(conn):
    """Move obvious HDR/EXR texture maps out of the HDRI kind bucket."""
    import scanner  # lazy: avoids an import cycle at module load
    hdri_cat_ids = [
        r["id"] for r in conn.execute(
            "SELECT id FROM categories WHERE kind='hdri'").fetchall()
    ]
    for r in conn.execute(
            "SELECT id, path, ext FROM assets "
            "WHERE missing=0 AND hidden=0 AND kind='hdri' "
            "AND ext IN ('.hdr', '.exr')").fetchall():
        folder = os.path.dirname(r["path"])
        name_noext = os.path.splitext(os.path.basename(r["path"]))[0]
        if scanner.classify_kind(r["ext"], folder, name_noext) != "texture":
            continue
        set_key, role, order = scanner.texture_set_info(folder, name_noext)
        subtype, resolution = scanner.texture_facets(folder, name_noext)
        conn.execute(
            "UPDATE assets SET kind='texture', set_key=?, map_role=?, "
            "map_order=?, subtype=?, resolution=? WHERE id=?",
            (set_key, role, order, subtype, resolution, r["id"]),
        )
        for cid in hdri_cat_ids:
            conn.execute(
                "DELETE FROM asset_categories WHERE asset_id=? AND category_id=?",
                (r["id"], cid),
            )
        _auto_categorize(conn, r["id"], r["path"], "texture")


def _repair_obvious_material_textures(conn):
    """Move existing obvious PBR material map sets from Textures to Materials."""
    import scanner  # lazy: avoids an import cycle at module load
    texture_cat_ids = [
        r["id"] for r in conn.execute(
            "SELECT id FROM categories WHERE kind='texture'").fetchall()
    ]
    rows = [
        dict(r) for r in conn.execute(
            "SELECT id, path, ext, set_key, map_role FROM assets "
            "WHERE missing=0 AND hidden=0 AND kind='texture'").fetchall()
    ]
    by_set = {}
    for r in rows:
        if scanner.is_model_pack_texture_sidecar(r["path"]):
            continue
        folder = os.path.dirname(r["path"])
        name_noext = os.path.splitext(os.path.basename(r["path"]))[0]
        set_key, role, order = scanner.texture_set_info(folder, name_noext)
        if not r.get("set_key") or r.get("set_key") != set_key:
            conn.execute(
                "UPDATE assets SET set_key=?, map_role=?, map_order=? WHERE id=?",
                (set_key, role, order, r["id"]),
            )
        r["set_key"], r["map_role"] = set_key, role
        by_set.setdefault(set_key, set()).add(role)

    for r in rows:
        if scanner.is_model_pack_texture_sidecar(r["path"]):
            continue
        folder = os.path.dirname(r["path"])
        name_noext = os.path.splitext(os.path.basename(r["path"]))[0]
        role = r.get("map_role") or ""
        if not role:
            continue
        folder_tokens = scanner._folder_tokens(folder)
        role_count = len({x for x in by_set.get(r["set_key"], set()) if x})
        if not (folder_tokens & scanner.MATERIAL_CONTAINER_DIRS or role_count >= 2):
            continue
        subtype, resolution = scanner.texture_facets(folder, name_noext)
        conn.execute(
            "UPDATE assets SET kind='material', subtype=?, resolution=? WHERE id=?",
            (subtype, resolution, r["id"]),
        )
        for cid in texture_cat_ids:
            conn.execute(
                "DELETE FROM asset_categories WHERE asset_id=? AND category_id=?",
                (r["id"], cid),
            )
        _auto_categorize(conn, r["id"], r["path"], "material")


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


def _path_like(prefix):
    """LIKE pattern (ESCAPE '!') matching every path under `prefix`. Trailing
    separators are stripped first — a drive-root library stores as "D:\\", and
    appending os.sep to that made a pattern ("D:\\\\%") that matched nothing.
    The prefix's own % and _ are escaped so a folder name containing them can't
    match a sibling's assets."""
    p = prefix.rstrip("/\\")
    p = p.replace("!", "!!").replace("%", "!%").replace("_", "!_")
    return p + os.sep + "%"


def remove_library(library_id):
    with connect() as conn:
        row = conn.execute("SELECT path FROM libraries WHERE id=?", (library_id,)).fetchone()
        if not row:
            return
        # Drop assets that lived under this library root.
        conn.execute("DELETE FROM assets WHERE path LIKE ? ESCAPE '!'",
                     (_path_like(row["path"]),))
        conn.execute("DELETE FROM libraries WHERE id=?", (library_id,))
    purge_orphan_assets()


def purge_orphan_assets():
    """Delete asset rows that don't live under ANY current library root — ghosts
    left behind when a root changed form (drive letter changed, folder moved and
    re-added) or by the pre-0.15.6 removal bug that missed a drive-root
    library's assets. Every asset comes from scanning a library, so with no
    libraries left the index should be empty. Returns how many were removed."""
    with connect() as conn:
        roots = [r["path"] for r in conn.execute("SELECT path FROM libraries")]
        if roots:
            cond = " OR ".join("path LIKE ? ESCAPE '!'" for _ in roots)
            cur = conn.execute(f"DELETE FROM assets WHERE NOT ({cond})",
                               [_path_like(r) for r in roots])
        else:
            cur = conn.execute("DELETE FROM assets")
        return cur.rowcount


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
    prefix = os.path.normpath(library_path or "")
    if prefix.endswith(os.sep):
        pattern = prefix + "%"
    else:
        pattern = prefix + os.sep + "%"
    with connect() as conn:
        rows = conn.execute(
            "SELECT id FROM assets WHERE path LIKE ?",
            (pattern,),
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
    """The source/vendor folder for an asset.

    Library roots can be broad (the user's D:\\ drive), so skip storage buckets
    such as 3D_Assets/Models and prefer known vendor folders when present:
    D:\\3D_Assets\\Models\\Bedroom\\iMeshh\\... -> iMeshh.
    String-based (not os.path) so it works on stored Windows paths regardless of
    host OS."""
    p = (path or "").replace("\\", "/")
    r = (root or "").replace("\\", "/").rstrip("/")
    if not r or not p.lower().startswith(r.lower() + "/"):
        return ""
    parts = [x for x in p[len(r) + 1:].split("/") if x]
    folders = parts[:-1] if parts else []
    for part in folders:
        if _source_token(part) in _KNOWN_SOURCE_FOLDERS:
            return part
    for part in folders:
        if _source_token(part) not in _SOURCE_BUCKET_FOLDERS:
            return part
    return r.split("/")[-1] or ""             # file directly in root -> root name


_SOURCE_BUCKET_FOLDERS = {
    "3dassets", "assets", "assetlibrary", "library", "models", "model",
    "allmodels", "textures", "texture", "materials", "material", "hdri", "hdris",
}
_KNOWN_SOURCE_FOLDERS = {
    "imeshh", "chocofur", "polyhaven", "kitbash3d",
}


def _source_token(s):
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


def _repair_auto_source_authors(conn):
    roots = [r["path"] for r in conn.execute("SELECT path FROM libraries")]
    roots.sort(key=len, reverse=True)
    auto_old = {"", "3D_Assets", "Models", "All_Models"}
    rows = conn.execute("SELECT id, path, author FROM assets").fetchall()
    for row in rows:
        author = row["author"] or ""
        inferred = next((s for s in (source_folder(row["path"], rt) for rt in roots) if s), "")
        if inferred and author in auto_old and author != inferred:
            conn.execute("UPDATE assets SET author=? WHERE id=?", (inferred, row["id"]))


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


def list_blend_missing_tex():
    """Present .blend assets currently flagged as referencing absent textures."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT id, path FROM assets WHERE missing=0 AND hidden=0 AND ext='.blend' "
            "AND blend_missing_textures>0").fetchall()
    return [dict(r) for r in rows]


def set_blend_missing_textures(asset_id, n):
    with connect() as conn:
        conn.execute("UPDATE assets SET blend_missing_textures=? WHERE id=?",
                     (max(0, int(n or 0)), asset_id))


def set_assets_author(ids, author):
    """Set the author on many assets at once (bulk re-attribution)."""
    if not ids:
        return 0
    with connect() as conn:
        placeholders = ",".join("?" * len(ids))
        cur = conn.execute(
            f"UPDATE assets SET author=? WHERE id IN ({placeholders})",
            [author or ""] + list(ids),
        )
        return cur.rowcount


def set_blend_meta(asset_id, text, missing_textures=None,
                   packed_tex=None, external_tex=None,
                   packed_texture_maps=None, packed_hdris=None,
                   external_texture_maps=None, external_hdris=None):
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
    if packed_texture_maps is not None:
        updates.append("blend_packed_texture_maps=?")
        params.append(max(0, int(packed_texture_maps or 0)))
    if packed_hdris is not None:
        updates.append("blend_packed_hdris=?")
        params.append(max(0, int(packed_hdris or 0)))
    if external_texture_maps is not None:
        updates.append("blend_external_texture_maps=?")
        params.append(max(0, int(external_texture_maps or 0)))
    if external_hdris is not None:
        updates.append("blend_external_hdris=?")
        params.append(max(0, int(external_hdris or 0)))
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
            "SELECT id, path, mtime FROM assets WHERE missing=0 AND hidden=0 AND ext='.blend'"
        ).fetchall()
    return [dict(r) for r in rows]


def existing_blend_names():
    """Set of lowercased base names (no extension) of every .blend asset in the
    library. Used to tell whether a marked datablock has its own .blend file."""
    with connect() as conn:
        return {
            r["name"].lower()
            for r in conn.execute(
                "SELECT name FROM assets WHERE ext='.blend' AND missing=0 AND hidden=0"
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
            "SELECT ext, COUNT(*) c FROM assets WHERE missing=0 AND hidden=0 AND kind='model' "
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
            "SELECT id, path, ext, kind, mtime FROM assets WHERE missing=0 AND hidden=0 "
            "ORDER BY CASE kind "
            "WHEN 'hdri' THEN 0 WHEN 'texture' THEN 1 "
            "WHEN 'material' THEN 2 WHEN 'model' THEN 3 ELSE 4 END, "
            "CASE ext WHEN '.hdr' THEN 0 WHEN '.exr' THEN 2 ELSE 1 END, id"
        ).fetchall()
    targets = [dict(r) for r in rows]
    try:
        import scanner  # lazy: avoids an import cycle at module load
        targets = [
            a for a in targets
            if not (a["kind"] == "texture" and scanner.is_model_pack_texture_sidecar(a["path"]))
        ]
    except Exception:
        pass
    return targets


def iter_dup_hash_targets():
    """Assets that still need content-hashing for the duplicates view. Only
    files whose byte size collides with another live file can possibly be exact
    duplicates, and of those only ones never hashed — or whose file changed
    since (the sig is size:mtime captured at hash time) — need work."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT id, path, size, mtime, content_hash, content_hash_sig "
            "FROM assets WHERE missing=0 AND hidden=0 AND size IN ("
            "SELECT size FROM assets WHERE missing=0 AND hidden=0 "
            "GROUP BY size HAVING COUNT(*) > 1)"
        ).fetchall()
    out = []
    for r in rows:
        sig = f"{r['size']}:{r['mtime']}"
        if not r["content_hash"] or r["content_hash_sig"] != sig:
            out.append({"id": r["id"], "path": r["path"], "sig": sig})
    return out


def list_blend_assets():
    """id + path of every live .blend, for the health check pass."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT id, path FROM assets WHERE missing=0 AND hidden=0 AND ext='.blend' "
            "ORDER BY path").fetchall()
    return [dict(r) for r in rows]


def donor_blend_candidates(path, limit=2000):
    """Healthy .blend files to borrow a DNA1 catalog from when repairing
    `path` — same-folder files first (an asset pack is usually saved by one
    Blender version), smallest first. Candidates are only header-peeked (12
    bytes) until one matches the damaged file's Blender version, so a large
    list is cheap."""
    folder = os.path.dirname(path)
    with connect() as conn:
        rows = conn.execute(
            "SELECT path FROM assets WHERE ext='.blend' AND missing=0 AND hidden=0 "
            "AND blend_corrupt=0 AND path!=? "
            "ORDER BY CASE WHEN path LIKE ? THEN 0 ELSE 1 END, size ASC LIMIT ?",
            (path, folder + os.sep + "%", limit)).fetchall()
    return [r["path"] for r in rows]


def list_corrupt_blends():
    """id + path of every .blend flagged damaged by the health check."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT id, path FROM assets WHERE missing=0 AND hidden=0 AND ext='.blend' "
            "AND blend_corrupt>0 ORDER BY path").fetchall()
    return [dict(r) for r in rows]


def list_restore_targets():
    """Everything a recovery-folder restore could fix: .blends flagged damaged
    by the health check, plus .blends whose file has vanished from disk
    entirely (the drive lost them; the index still knows where they lived)."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT id, path, blend_corrupt, missing FROM assets "
            "WHERE ext='.blend' AND (hidden=0 OR missing=1) AND (blend_corrupt>0 OR missing=1) "
            "ORDER BY path").fetchall()
    return [dict(r) for r in rows]


def all_blend_basenames():
    """Lowercase basename of every indexed .blend, missing ones included —
    the reference set for spotting recovered files the library never had."""
    with connect() as conn:
        rows = conn.execute("SELECT path FROM assets WHERE ext='.blend'").fetchall()
    return {os.path.basename(r["path"]).lower() for r in rows}


def set_blend_corrupt(asset_id, corrupt):
    with connect() as conn:
        conn.execute("UPDATE assets SET blend_corrupt=? WHERE id=?",
                     (1 if corrupt else 0, asset_id))


def refresh_asset_file(asset_id, path):
    """Re-stat a file after Hangar restored/replaced it on disk, so mtime/size
    match the new content (and the thumb cache keys off the new state)."""
    try:
        st = os.stat(path)
    except OSError:
        return
    with connect() as conn:
        conn.execute("UPDATE assets SET size=?, mtime=?, missing=0 WHERE id=?",
                     (st.st_size, st.st_mtime, asset_id))


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
                 linked=False, corrupt=False, author="", include_hidden=False):
    clauses = ["a.missing=1"] if missing else ["a.missing=0"]
    if not missing and not include_hidden:
        clauses.append("a.hidden=0")
    if corrupt:
        clauses.append("a.blend_corrupt>0")   # damaged .blend files (health check)
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
            "SELECT content_hash FROM assets WHERE missing=0 AND hidden=0 AND content_hash != '' "
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
    if author:
        clauses.append("a.author=? COLLATE NOCASE")
        where_params.append(author)
    if set_key:
        # Listing the individual files of one texture set — overrides grouping.
        clauses.append("a.set_key=?")
        where_params.append(set_key)
        group = ""
    if search:
        # Match the file name, the user's file-level metadata (author/
        # description), OR the aggregated .blend metadata (marked-asset names,
        # tags, author, catalog) — so search reaches all of it.
        terms = [t for t in re.split(r"\s+", search.strip()) if t]
        for term in terms:
            like = f"%{term}%"
            clauses.append(
                "("
                "a.name LIKE ? OR a.path LIKE ? OR a.ext LIKE ? OR a.kind LIKE ? "
                "OR a.blend_meta LIKE ? OR a.author LIKE ? OR a.description LIKE ? "
                "OR a.license LIKE ? OR a.copyright LIKE ? "
                "OR EXISTS (SELECT 1 FROM asset_tags sat "
                "           JOIN tags st ON st.id=sat.tag_id "
                "           WHERE sat.asset_id=a.id AND st.name LIKE ?) "
                "OR EXISTS (SELECT 1 FROM asset_categories sac "
                "           JOIN categories scat ON scat.id=sac.category_id "
                "           WHERE sac.asset_id=a.id AND scat.name LIKE ?) "
                "OR EXISTS (SELECT 1 FROM collection_assets sca "
                "           JOIN collections sc ON sc.id=sca.collection_id "
                "           WHERE sca.asset_id=a.id AND sc.name LIKE ?)"
                ")")
            where_params += [like] * 12
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
        clauses.append("a.path LIKE ? ESCAPE '!'")
        where_params.append(_path_like(folder))

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
    clauses = ["missing=0", "hidden=0"]
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
            "SELECT * FROM assets WHERE set_key=? AND missing=0 AND hidden=0 "
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
            "SELECT kind, COUNT(*) c FROM assets WHERE missing=0 AND hidden=0 GROUP BY kind"
        ).fetchall()
        total = conn.execute("SELECT COUNT(*) c FROM assets WHERE missing=0 AND hidden=0").fetchone()["c"]
        favs = conn.execute(
            "SELECT COUNT(*) c FROM assets WHERE missing=0 AND hidden=0 AND favorite=1"
        ).fetchone()["c"]
        ext_rows = conn.execute(
            "SELECT ext, COUNT(*) c FROM assets WHERE missing=0 AND hidden=0 AND kind='model' "
            "GROUP BY ext ORDER BY c DESC"
        ).fetchall()
    with connect() as conn2:
        missing_count = conn2.execute(
            "SELECT COUNT(*) c FROM assets WHERE missing=1"
        ).fetchone()["c"]
        blend_missing_textures = conn2.execute(
            "SELECT COUNT(*) c FROM assets "
            "WHERE missing=0 AND hidden=0 AND ext='.blend' AND blend_missing_textures>0"
        ).fetchone()["c"]
        blend_missing_texture_refs = conn2.execute(
            "SELECT COALESCE(SUM(blend_missing_textures), 0) c FROM assets "
            "WHERE missing=0 AND hidden=0 AND ext='.blend'"
        ).fetchone()["c"]
        blend_texture_health = conn2.execute(
            "SELECT "
            "COALESCE(SUM(blend_packed_texture_maps), 0) packed_texture_maps, "
            "COALESCE(SUM(blend_packed_hdris), 0) packed_hdris, "
            "COALESCE(SUM(blend_external_texture_maps), 0) external_texture_maps, "
            "COALESCE(SUM(blend_external_hdris), 0) external_hdris "
            "FROM assets WHERE missing=0 AND hidden=0 AND ext='.blend'"
        ).fetchone()
    return {
        "by_kind": {r["kind"]: r["c"] for r in rows},
        "total": total,
        "favorites": favs,
        "model_by_ext": {r["ext"]: r["c"] for r in ext_rows},
        "missing": missing_count,
        "blend_missing_textures": blend_missing_textures,
        "blend_missing_texture_refs": blend_missing_texture_refs,
        "blend_packed_texture_maps": blend_texture_health["packed_texture_maps"],
        "blend_packed_hdris": blend_texture_health["packed_hdris"],
        "blend_external_texture_maps": blend_texture_health["external_texture_maps"],
        "blend_external_hdris": blend_texture_health["external_hdris"],
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


def list_authors():
    """All non-empty authors represented by live assets, with asset counts."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT author name, COUNT(*) c FROM assets "
            "WHERE missing=0 AND hidden=0 AND author!='' AND author IS NOT NULL "
            "GROUP BY author COLLATE NOCASE ORDER BY author COLLATE NOCASE"
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
    """Useful grouping folders represented inside each category.

    The sidebar uses this to show e.g. Furniture > Beds, while keeping the
    existing category membership model unchanged. Counts are by indexed asset
    row, not by physical directory size. Many model packs place a single file in
    a self-named leaf folder (Tree/American_beech/American_beech.blend); those
    leaf asset folders are too noisy for the category tree, so model rows roll
    up to their parent when the folder name matches the asset stem.
    """
    with connect() as conn:
        rows = conn.execute(
            "SELECT cat.name category, cat.kind, a.path, a.ext "
            "FROM categories cat "
            "JOIN asset_categories ac ON ac.category_id=cat.id "
            "JOIN assets a ON a.id=ac.asset_id "
            "WHERE a.missing=0 AND a.hidden=0 "
            "ORDER BY cat.sort, cat.name COLLATE NOCASE, a.path"
        ).fetchall()
    by_key = {}
    for r in rows:
        folder = _category_display_folder(r["path"], r["ext"])
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


def category_author_counts():
    """Authors represented inside each category, for Category > Author browsing."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT cat.name category, cat.kind, a.author, COUNT(*) c "
            "FROM categories cat "
            "JOIN asset_categories ac ON ac.category_id=cat.id "
            "JOIN assets a ON a.id=ac.asset_id "
            "WHERE a.missing=0 AND a.hidden=0 AND a.author!='' AND a.author IS NOT NULL "
            "GROUP BY cat.name, cat.kind, a.author COLLATE NOCASE "
            "ORDER BY cat.sort, cat.name COLLATE NOCASE, a.author COLLATE NOCASE"
        ).fetchall()
    return [{
        "category": r["category"],
        "kind": r["kind"] or "",
        "author": r["author"],
        "count": r["c"],
    } for r in rows]


def _norm_folder_token(s):
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


def _category_display_folder(path, ext):
    folder = os.path.dirname(path or "")
    if not folder:
        return ""
    if (ext or "").lower() in {".blend", ".fbx", ".obj", ".gltf", ".glb", ".stl",
                               ".ply", ".usd", ".usda", ".usdc", ".usdz", ".abc",
                               ".dae", ".3ds"}:
        stem = _norm_folder_token(os.path.splitext(os.path.basename(path or ""))[0])
        while folder:
            leaf = _norm_folder_token(os.path.basename(folder))
            parent = os.path.dirname(folder)
            if not (parent and _looks_self_named_asset_folder(leaf, stem)):
                break
            folder = parent
    return folder


def _looks_self_named_asset_folder(leaf, stem):
    if not leaf or not stem:
        return False
    if leaf == stem:
        return True
    if leaf in stem or stem in leaf:
        shorter = min(len(leaf), len(stem))
        longer = max(len(leaf), len(stem))
        return shorter >= 12 and (shorter / longer) >= 0.65
    return False


def _split_storage_path(path):
    raw = (path or "").replace("\\", "/").strip()
    m = re.match(r"^([A-Za-z]:)/(.*)$", raw)
    if not m:
        return "", [], ""
    drive = m.group(1).upper()
    parts = [p for p in m.group(2).split("/") if p]
    if not parts:
        return drive + "\\", [], drive
    return drive + "\\" + parts[0], parts, drive


def _duplicate_pack_logical_parts(path):
    root, parts, _drive = _split_storage_path(path)
    if not root or len(parts) < 2:
        return root, [], []
    rel = parts[1:-1]  # under storage root, without filename
    while rel and _source_token(rel[0]) in {"3dassets", "models", "model"}:
        rel = rel[1:]
    return root, rel, parts[:-1]


def _duplicate_pack_key(path):
    _root, rel, _folders = _duplicate_pack_logical_parts(path)
    if not rel:
        return ""
    return "/".join(_source_token(p) for p in rel if p)


def _preferred_duplicate_root(roots):
    def score(root):
        token = _source_token(os.path.basename(root or ""))
        return (0 if token == "3dassets" else 1, (root or "").lower())
    return sorted(roots, key=score)[0] if roots else ""


def _folder_manifest(folder):
    folder = os.path.normpath(folder or "")
    if not folder or not os.path.isdir(folder):
        return None
    out = {}
    try:
        for dirpath, dirnames, filenames in os.walk(folder):
            dirnames.sort()
            for name in sorted(filenames):
                full = os.path.join(dirpath, name)
                try:
                    st = os.stat(full)
                except OSError:
                    return None
                rel = os.path.relpath(full, folder).replace("\\", "/").lower()
                out[rel] = int(st.st_size)
    except OSError:
        return None
    return out


def _folder_contains_all_files(keep_folder, extra_folder):
    keep = _folder_manifest(keep_folder)
    extra = _folder_manifest(extra_folder)
    if keep is None or extra is None:
        return False, 0
    missing = 0
    for rel, size in extra.items():
        if keep.get(rel) != size:
            missing += 1
    return missing == 0, missing


def _folder_manifests_match(source_folder, target_folder):
    source = _folder_manifest(source_folder)
    target = _folder_manifest(target_folder)
    if source is None or target is None:
        return False, {"source_files": 0, "target_files": 0, "missing": 0, "extra": 0, "changed": 0}
    missing = [rel for rel in source if rel not in target]
    extra = [rel for rel in target if rel not in source]
    changed = [rel for rel, size in source.items() if rel in target and target[rel] != size]
    return not missing and not extra and not changed, {
        "source_files": len(source),
        "target_files": len(target),
        "missing": len(missing),
        "extra": len(extra),
        "changed": len(changed),
        "bytes": sum(source.values()),
    }


def _folder_size(folder, cache=None):
    cache = cache if cache is not None else {}
    key = os.path.normcase(os.path.normpath(folder or ""))
    if key in cache:
        return cache[key]
    manifest = _folder_manifest(folder)
    size = sum(manifest.values()) if manifest is not None else 0
    cache[key] = size
    return size


def _disk_usage_for_path(path):
    probe = os.path.abspath(os.path.normpath(path or os.getcwd()))
    drive, _tail = os.path.splitdrive(probe)
    if drive and not os.path.exists(probe):
        probe = drive + os.sep
    while probe and not os.path.exists(probe):
        parent = os.path.dirname(probe)
        if parent == probe:
            break
        probe = parent
    try:
        usage = shutil.disk_usage(probe)
        return {"path": probe, "total": usage.total, "used": usage.used, "free": usage.free}
    except OSError:
        return {"path": probe, "total": 0, "used": 0, "free": 0}


def duplicate_pack_groups():
    """Same model-pack folder indexed from multiple storage roots."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT id, path, ext, size, hidden FROM assets "
            "WHERE missing=0 AND kind='model' "
            "ORDER BY path COLLATE NOCASE"
        ).fetchall()
    packs = {}
    for r in rows:
        key = _duplicate_pack_key(r["path"])
        if not key:
            continue
        root, rel, folders = _duplicate_pack_logical_parts(r["path"])
        if not root or not rel:
            continue
        pack = packs.setdefault(key, {
            "key": key,
            "label": rel[-1],
            "logical_path": "\\".join(rel),
            "roots": {},
        })
        _root, _parts, drive = _split_storage_path(r["path"])
        folder = (drive + "\\" + "\\".join(folders)) if folders and drive else os.path.dirname(r["path"])
        item = pack["roots"].setdefault(root, {
            "root": root,
            "folder": folder,
            "count": 0,
            "hidden": 0,
            "size": 0,
            "formats": set(),
        })
        item["count"] += 1
        item["hidden"] += 1 if r["hidden"] else 0
        item["size"] += int(r["size"] or 0)
        if r["ext"]:
            item["formats"].add(r["ext"])

    out = []
    for pack in packs.values():
        if len(pack["roots"]) < 2:
            continue
        preferred = _preferred_duplicate_root(pack["roots"].keys())
        roots = []
        for root, item in pack["roots"].items():
            d = dict(item)
            d["formats"] = sorted(d["formats"])
            d["preferred"] = root == preferred
            roots.append(d)
        roots.sort(key=lambda d: (not d["preferred"], d["root"].lower()))
        out.append({
            "key": pack["key"],
            "label": pack["label"],
            "logical_path": pack["logical_path"],
            "preferred_root": preferred,
            "roots": roots,
            "duplicate_count": sum(r["count"] for r in roots if r["root"] != preferred),
            "hidden_count": sum(r["hidden"] for r in roots),
            "total_count": sum(r["count"] for r in roots),
        })
    out.sort(key=lambda g: (-g["duplicate_count"], g["logical_path"].lower()))
    return out


def hide_duplicate_pack(group_key, keep_root):
    group_key = group_key or ""
    keep_root = (keep_root or "").rstrip("\\/")
    if not group_key or not keep_root:
        return 0
    groups = {g["key"]: g for g in duplicate_pack_groups()}
    if group_key not in groups:
        return {"changed": 0, "skipped": 0}
    keep_folder = next(
        (r["folder"] for r in groups[group_key]["roots"]
         if r["root"].rstrip("\\/").lower() == keep_root.lower()),
        "",
    )
    complete_roots = set()
    skipped = 0
    for root in groups[group_key]["roots"]:
        root_name = root["root"].rstrip("\\/").lower()
        if root_name == keep_root.lower():
            continue
        ok, missing = _folder_contains_all_files(keep_folder, root["folder"])
        if ok:
            complete_roots.add(root_name)
        else:
            skipped += root["count"]
    ids_hide, ids_show = [], []
    with connect() as conn:
        rows = conn.execute(
            "SELECT id, path FROM assets WHERE missing=0 AND kind='model'"
        ).fetchall()
        for r in rows:
            if _duplicate_pack_key(r["path"]) != group_key:
                continue
            root, _rel, _folders = _duplicate_pack_logical_parts(r["path"])
            if root.rstrip("\\/").lower() == keep_root.lower():
                ids_show.append(r["id"])
            elif root.rstrip("\\/").lower() in complete_roots:
                ids_hide.append(r["id"])
        changed = 0
        if ids_hide:
            ph = ",".join("?" * len(ids_hide))
            changed += conn.execute(
                f"UPDATE assets SET hidden=1 WHERE id IN ({ph})", ids_hide).rowcount
        if ids_show:
            ph = ",".join("?" * len(ids_show))
            changed += conn.execute(
                f"UPDATE assets SET hidden=0 WHERE id IN ({ph})", ids_show).rowcount
    return {"changed": changed, "skipped": skipped}


def hide_all_duplicate_pack_matches():
    """Hide non-preferred duplicate-pack rows only when the kept folder contains
    every file from the folder being hidden with the same relative path + size.

    This is intentionally stricter than the per-pack button: bulk cleanup should
    skip anything that looks like an incomplete or richer copy.
    """
    groups = duplicate_pack_groups()
    if not groups:
        return {"changed": 0, "groups": 0, "skipped": 0}
    group_keys = {g["key"]: g for g in groups}
    by_group = {}
    with connect() as conn:
        rows = conn.execute(
            "SELECT id, path, name, ext, size, hidden FROM assets "
            "WHERE missing=0 AND kind='model'"
        ).fetchall()
        for r in rows:
            key = _duplicate_pack_key(r["path"])
            if key in group_keys:
                root, _rel, _folders = _duplicate_pack_logical_parts(r["path"])
                by_group.setdefault(key, []).append({**dict(r), "root": root})

        ids_hide = []
        skipped = 0
        touched_groups = set()
        for key, items in by_group.items():
            preferred = group_keys[key]["preferred_root"].rstrip("\\/").lower()
            keep_folder = next(
                (r["folder"] for r in group_keys[key]["roots"]
                 if r["root"].rstrip("\\/").lower() == preferred),
                "",
            )
            if not keep_folder:
                skipped += len(items)
                continue
            complete_roots = set()
            for root in group_keys[key]["roots"]:
                root_name = root["root"].rstrip("\\/").lower()
                if root_name == preferred:
                    continue
                ok, missing = _folder_contains_all_files(keep_folder, root["folder"])
                if ok:
                    complete_roots.add(root_name)
                else:
                    skipped += root["count"]
            for i in items:
                if (i["root"] or "").rstrip("\\/").lower() == preferred:
                    continue
                if (i["root"] or "").rstrip("\\/").lower() in complete_roots:
                    if not i["hidden"]:
                        ids_hide.append(i["id"])
                        touched_groups.add(key)
        changed = 0
        if ids_hide:
            ph = ",".join("?" * len(ids_hide))
            changed = conn.execute(
                f"UPDATE assets SET hidden=1 WHERE id IN ({ph}) AND hidden=0", ids_hide).rowcount
    return {"changed": changed, "groups": len(touched_groups), "skipped": skipped}


def restore_hidden_duplicates(group_key=""):
    with connect() as conn:
        if not group_key:
            return conn.execute("UPDATE assets SET hidden=0 WHERE hidden=1").rowcount
        ids = [
            r["id"] for r in conn.execute(
                "SELECT id, path FROM assets WHERE hidden=1 AND missing=0 AND kind='model'"
            ).fetchall()
            if _duplicate_pack_key(r["path"]) == group_key
        ]
        if not ids:
            return 0
        ph = ",".join("?" * len(ids))
        return conn.execute(f"UPDATE assets SET hidden=0 WHERE id IN ({ph})", ids).rowcount


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
            "SELECT id, path, kind FROM assets WHERE missing=0 AND hidden=0"
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


_BATHROOM_KEYWORDS = {
    "bathroom", "bathrooms", "basin", "basins",
    "toilet", "toilets", "bath", "bathtub", "baths",
    "shower", "showers", "vanity", "vanities", "bidet",
}


def _path_has_keyword(path, keywords):
    tokens = set(re.split(r"[^a-z0-9]+", (path or "").lower()))
    tokens.discard("")
    for kw in keywords:
        if kw in tokens or (kw + "s") in tokens or (kw.endswith("s") and kw[:-1] in tokens):
            return True
    return False


def promote_bathroom_category():
    """Upgrade existing libraries so bathroom assets get their own category.

    Earlier starter taxonomy treated "bathroom" as an Architecture keyword.
    That made bathroom packs visible only as scattered folder groups instead of
    a single left-column category. This keeps the broad Architecture category,
    but moves obvious bathroom model assets into Bathrooms.
    """
    with connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO categories(name, icon, sort, keywords, kind) "
            "VALUES (?, ?, (SELECT COALESCE(MAX(sort), -1) + 1 FROM categories), ?, ?)",
            ("Bathrooms", "🚿", ",".join(sorted(_BATHROOM_KEYWORDS)), "model"),
        )
        conn.execute(
            "UPDATE categories SET keywords=?, kind='model' "
            "WHERE name='Bathrooms' AND (keywords='' OR kind='')",
            (",".join(sorted(_BATHROOM_KEYWORDS)),),
        )
        arch = conn.execute("SELECT id, keywords FROM categories WHERE name='Architecture'").fetchone()
        baths = conn.execute("SELECT id FROM categories WHERE name='Bathrooms'").fetchone()
        if not baths:
            return {"matched": 0}
        if arch and arch["keywords"]:
            kws = [k for k in (arch["keywords"] or "").split(",") if k and k not in {"bathroom", "bathrooms"}]
            conn.execute("UPDATE categories SET keywords=? WHERE id=?", (",".join(kws), arch["id"]))

        matched = 0
        for a in conn.execute(
            "SELECT id, path FROM assets WHERE missing=0 AND kind='model'"
        ).fetchall():
            if not _path_has_keyword(a["path"], _BATHROOM_KEYWORDS):
                conn.execute(
                    "DELETE FROM asset_categories WHERE category_id=? AND asset_id=?",
                    (baths["id"], a["id"]),
                )
                continue
            matched += 1
            conn.execute(
                "INSERT OR IGNORE INTO asset_categories(category_id, asset_id) VALUES (?, ?)",
                (baths["id"], a["id"]),
            )
            if arch:
                conn.execute(
                    "DELETE FROM asset_categories WHERE category_id=? AND asset_id=?",
                    (arch["id"], a["id"]),
                )
    _invalidate_matchers()
    return {"matched": matched}


_ROOM_CATEGORY_RULES = [
    ("Bathrooms", {"bathroom", "bathrooms", "basin", "basins", "toilet", "toilets",
                   "bath", "bathtub", "baths", "shower", "showers", "vanity",
                   "vanities", "bidet"}),
    ("Kitchens", {"kitchen", "kitchens", "countertop", "worktop", "oven", "hob",
                  "stove", "fridge", "refrigerator", "dishwasher", "kitchenette"}),
    ("Bedrooms", {"bedroom", "bedrooms", "bed", "beds", "wardrobe", "nightstand",
                  "bedside", "dresser"}),
    ("Living Rooms", {"living", "lounge", "sofa", "couch", "tv", "television",
                      "coffee", "console"}),
    ("Dining Rooms", {"dining", "dinner", "diningroom"}),
    ("Offices", {"office", "offices", "desk", "workstation", "conference", "meeting"}),
]


def promote_room_categories():
    """Back-fill newer room categories while leaving user categories intact."""
    total = 0
    with connect() as conn:
        existing = {
            r["name"]: r["id"]
            for r in conn.execute("SELECT id, name FROM categories").fetchall()
        }
        for name, kws in _ROOM_CATEGORY_RULES:
            conn.execute(
                "INSERT OR IGNORE INTO categories(name, icon, sort, keywords, kind) "
                "VALUES (?, ?, (SELECT COALESCE(MAX(sort), -1) + 1 FROM categories), ?, ?)",
                (name, "", ",".join(sorted(kws)), "model"),
            )
        cats = {
            r["name"]: r["id"]
            for r in conn.execute("SELECT id, name FROM categories").fetchall()
        }
        for a in conn.execute(
            "SELECT id, path FROM assets WHERE missing=0 AND kind='model'"
        ).fetchall():
            for name, kws in _ROOM_CATEGORY_RULES:
                if not _path_has_keyword(a["path"], kws):
                    continue
                cid = cats.get(name)
                if not cid:
                    continue
                cur = conn.execute(
                    "INSERT OR IGNORE INTO asset_categories(category_id, asset_id) VALUES (?, ?)",
                    (cid, a["id"]),
                )
                total += cur.rowcount
                break
    _invalidate_matchers()
    return {"links_added": total}


def _clean_path_part(value, fallback="Unknown"):
    text = re.sub(r"[<>:\"/\\|?*\x00-\x1f]+", " ", str(value or "")).strip(" .")
    text = re.sub(r"\s+", " ", text)
    return text or fallback


_PHYSICAL_CATEGORY_NAMES = {
    "Architecture": "Architectural",
}
_PHYSICAL_CATEGORY_PRIORITY = [
    "Furniture", "Bathrooms", "Kitchens", "Bedrooms", "Living Rooms",
    "Dining Rooms", "Offices", "Architecture", "Buildings", "Nature",
    "Vehicles", "Industrial", "Food", "Props", "Characters", "Sci-Fi",
    "Fantasy", "Weapons",
]


def _preferred_category_for_path(path, categories):
    present = {c for c in categories if c}
    for name in _PHYSICAL_CATEGORY_PRIORITY:
        if name in present:
            return name
    for name, kws in _ROOM_CATEGORY_RULES:
        if _path_has_keyword(path, kws):
            return name
    preferred = [c for c in categories if c not in {"Architecture", "Props"}]
    return preferred[0] if preferred else (categories[0] if categories else "Uncategorised")


def _physical_subcategory(category, subfolder, pack_name):
    """Prefer the useful browsing folder in the physical layout.

    Chocofur-style paths like Chocofur/Furniture/Beds otherwise become
    Models/Furniture/Furniture/Chocofur/Beds. The repeated broad folder is not
    helpful; the pack folder itself is the useful subcategory.
    """
    broad = {
        "furniture", "models", "model", "details", "assets", "3dassets",
        _source_token(category),
    }
    sub = _clean_path_part(subfolder, "")
    pack = _clean_path_part(pack_name, "")
    if _source_token(sub) in broad and pack:
        return pack
    return sub or pack or "General"


@lru_cache(maxsize=20000)
def _pack_parent_info(parent):
    try:
        names = os.listdir(parent)
    except OSError:
        names = []
    model_exts = {
        ".blend", ".fbx", ".obj", ".gltf", ".glb", ".stl", ".ply",
        ".usd", ".usda", ".usdc", ".usdz", ".abc", ".dae", ".3ds",
    }
    model_files = [
        n for n in names
        if os.path.isfile(os.path.join(parent, n))
        and os.path.splitext(n)[1].lower() in model_exts
    ]
    sidecar_dirs = {
        "texture", "textures", "map", "maps", "material", "materials",
        "preview", "previews", "render", "renders",
    }
    has_sidecars = any(
        os.path.isdir(os.path.join(parent, n))
        and _norm_folder_token(n) in sidecar_dirs
        for n in names
    )
    return len(model_files), has_sidecars


def _pack_parts_for_asset(path, target_root=None):
    parent = os.path.dirname(path or "")
    stem = os.path.splitext(os.path.basename(path or ""))[0]
    leaf = os.path.basename(parent)
    if target_root:
        try:
            rel = os.path.relpath(parent, os.path.join(os.path.normpath(target_root), "Models"))
            parts = [p for p in rel.split(os.sep) if p and p != os.curdir]
            if not rel.startswith("..") and len(parts) >= 4:
                return parent, parts[-3], leaf
        except (OSError, ValueError):
            pass
    if parent and _looks_self_named_asset_folder(_norm_folder_token(leaf), _norm_folder_token(stem)):
        return parent, os.path.basename(os.path.dirname(parent)), leaf
    model_count, has_sidecars = _pack_parent_info(parent)
    if has_sidecars or model_count > 1:
        return parent, os.path.basename(os.path.dirname(parent)), leaf
    return parent, os.path.basename(parent), stem


def organise_disk_plan(target_root="D:\\Hangar", limit=500, include_sizes=True):
    """Read-only plan for a clean physical disk layout.

    Proposes Models/Category/Author/Subfolder/PackName targets from the current
    index. No files or folders are created here.
    """
    target_root = os.path.normpath(target_root or "D:\\Hangar")
    with connect() as conn:
        rows = conn.execute(
            "SELECT a.id, a.path, a.name, a.ext, a.author, a.size, "
            "GROUP_CONCAT(cat.name, '|') categories "
            "FROM assets a "
            "LEFT JOIN asset_categories ac ON ac.asset_id=a.id "
            "LEFT JOIN categories cat ON cat.id=ac.category_id "
            "WHERE a.missing=0 AND a.hidden=0 AND a.kind='model' "
            "GROUP BY a.id ORDER BY a.path COLLATE NOCASE"
        ).fetchall()

    packs = {}
    for r in rows:
        pack_folder, subfolder, pack_name = _pack_parts_for_asset(r["path"], target_root)
        if not pack_folder:
            continue
        cats = [c for c in (r["categories"] or "").split("|") if c]
        cat = _preferred_category_for_path(r["path"], cats)
        author = r["author"] or source_folder(r["path"], os.path.splitdrive(r["path"])[0] + os.sep) or "Unknown"
        key = pack_folder.lower()
        item = packs.setdefault(key, {
            "source": pack_folder,
            "category": cat,
            "author": author,
            "subcategory": _physical_subcategory(cat, subfolder, pack_name),
            "pack": pack_name,
            "formats": set(),
            "count": 0,
            "size": 0,
        })
        item["count"] += 1
        item["size"] += int(r["size"] or 0)
        if r["ext"]:
            item["formats"].add(r["ext"])

    items = []
    size_cache = {}
    summary = {
        "packs": 0, "already_clean": 0, "collision": 0, "collisions": 0,
        "target_exists": 0, "move": 0,
        "bytes_to_organise": 0, "copy_stage_bytes": 0,
        "potential_duplicate_savings": 0,
    }
    seen_targets = {}
    for p in packs.values():
        disk_size = (_folder_size(p["source"], size_cache) or p["size"]) if include_sizes else int(p["size"] or 0)
        target = os.path.join(
            target_root, "Models", _clean_path_part(_PHYSICAL_CATEGORY_NAMES.get(p["category"], p["category"])),
            _clean_path_part(p["subcategory"]), _clean_path_part(p["author"]),
            _clean_path_part(p["pack"]),
        )
        status = "move"
        src_norm = os.path.normcase(os.path.normpath(p["source"]))
        dst_norm = os.path.normcase(os.path.normpath(target))
        if src_norm == dst_norm:
            status = "already_clean"
        elif dst_norm in seen_targets and seen_targets[dst_norm] != src_norm:
            status = "collision"
        elif os.path.exists(target):
            if os.path.isdir(p["source"]) and os.path.isdir(target):
                ok, _manifest = _folder_manifests_match(p["source"], target)
                status = "move" if ok else "target_exists"
            else:
                status = "target_exists"
        seen_targets[dst_norm] = src_norm
        summary["packs"] += 1
        summary[status if status in summary else "collisions"] = summary.get(status if status in summary else "collisions", 0) + 1
        if status == "move" and include_sizes:
            summary["bytes_to_organise"] += disk_size
            summary["copy_stage_bytes"] += disk_size
        items.append({
            "source": p["source"],
            "target": target,
            "category": p["category"],
            "author": p["author"],
            "subcategory": p["subcategory"],
            "pack": p["pack"],
            "formats": sorted(p["formats"]),
            "count": p["count"],
            "size": p["size"],
            "disk_size": disk_size,
            "status": status,
        })

    if include_sizes:
        saving_folders = set()
        for group in duplicate_pack_groups():
            keep = next((r for r in group["roots"] if r.get("preferred")), None)
            if not keep:
                continue
            for root in group["roots"]:
                if root.get("preferred"):
                    continue
                ok, _missing = _folder_contains_all_files(keep["folder"], root["folder"])
                folder_key = os.path.normcase(os.path.normpath(root["folder"]))
                if ok and folder_key not in saving_folders:
                    saving_folders.add(folder_key)
                    summary["potential_duplicate_savings"] += _folder_size(root["folder"], size_cache)

    if include_sizes:
        disk = _disk_usage_for_path(target_root)
        summary["target_total"] = disk["total"]
        summary["target_used"] = disk["used"]
        summary["target_free"] = disk["free"]
        summary["target_probe"] = disk["path"]
    items.sort(key=lambda x: (x["status"] != "move", x["category"].lower(), x["subcategory"].lower(), x["author"].lower(), x["pack"].lower()))
    return {"target_root": target_root, "summary": summary, "items": items[:max(1, int(limit or 500))], "total": len(items)}


def _update_asset_paths_for_folder(source_folder, target_folder, on_path_moved=None):
    source_folder = os.path.normpath(source_folder or "")
    target_folder = os.path.normpath(target_folder or "")
    if not source_folder or not target_folder:
        return 0
    rows = []
    with connect() as conn:
        for r in conn.execute(
            "SELECT id, path, kind, mtime FROM assets WHERE path LIKE ? ESCAPE '!'",
            (_path_like(source_folder),),
        ).fetchall():
            rel = os.path.relpath(r["path"], source_folder)
            new_path = os.path.normpath(os.path.join(target_folder, rel))
            rows.append((r["id"], r["path"], new_path, r["kind"], r["mtime"]))
        updated = 0
        for asset_id, old_path, new_path, kind, mtime in rows:
            if os.path.normcase(os.path.normpath(old_path)) == os.path.normcase(new_path):
                continue
            existing = conn.execute(
                "SELECT id FROM assets WHERE path=? AND id<>?", (new_path, asset_id)
            ).fetchone()
            if existing:
                continue
            if on_path_moved:
                try:
                    on_path_moved(
                        {"id": asset_id, "path": old_path, "kind": kind, "mtime": mtime},
                        {"id": asset_id, "path": new_path, "kind": kind, "mtime": mtime},
                    )
                except Exception:
                    pass
            conn.execute("UPDATE assets SET path=? WHERE id=?", (new_path, asset_id))
            updated += 1
    return updated


def apply_organise_disk_plan(target_root="D:\\Hangar", limit=100, progress=None, on_path_moved=None):
    """Copy planned model-pack folders into the clean layout and verify each copy.

    This deliberately does not delete the old source folders. The index is moved
    to the verified copy, and a receipt is written so later cleanup can be
    reviewed pack by pack.
    """
    limit = max(1, min(1000, int(limit or 100)))
    copied = skipped = failed = updated_assets = bytes_copied = 0
    results = []
    started = time.time()
    target_root = os.path.normpath(target_root or "D:\\Hangar")
    seen_targets = set()

    plan = organise_disk_plan(target_root, limit=10000, include_sizes=False)
    candidates = [i for i in plan.get("items", []) if i.get("status") == "move"]
    effective_limit = min(limit, len(candidates))
    if progress:
        progress({
            "phase": "planning",
            "limit": effective_limit,
            "candidate_packs": len(candidates),
            "copied": copied,
            "skipped": skipped,
            "failed": failed,
            "updated_assets": updated_assets,
            "bytes_copied": bytes_copied,
        })

    for item in candidates:
        if copied >= limit:
            break
        source = os.path.normpath(item.get("source") or "")
        src_key = os.path.normcase(source)
        target = os.path.normpath(item.get("target") or "")
        dst_key = os.path.normcase(target)
        status = "move"
        if src_key == dst_key or src_key.startswith(os.path.normcase(target_root) + os.sep):
            status = "already_clean"
        elif dst_key in seen_targets:
            status = "collision"
        elif os.path.exists(target):
            status = "target_exists"
        seen_targets.add(dst_key)
        result = {
            "source": source,
            "target": target,
            "pack": item.get("pack") or os.path.basename(source),
            "status": status,
        }
        if progress:
            progress({
                "phase": "copying",
                "limit": limit,
                "current_pack": result["pack"],
                "current_source": source,
                "current_target": target,
                "copied": copied,
                "skipped": skipped,
                "failed": failed,
                "updated_assets": updated_assets,
                "bytes_copied": bytes_copied,
            })
        if status != "move":
            skipped += 1
            result["result"] = "skipped"
            result["reason"] = status
            results.append(result)
            if progress:
                progress({
                    "phase": "copying",
                    "limit": limit,
                    "current_pack": result["pack"],
                    "copied": copied,
                    "skipped": skipped,
                    "failed": failed,
                    "updated_assets": updated_assets,
                    "bytes_copied": bytes_copied,
                })
            continue
        if not source or not os.path.isdir(source):
            failed += 1
            result["result"] = "failed"
            result["reason"] = "source_missing"
            results.append(result)
            if progress:
                progress({
                    "phase": "copying",
                    "limit": limit,
                    "current_pack": result["pack"],
                    "copied": copied,
                    "skipped": skipped,
                    "failed": failed,
                    "updated_assets": updated_assets,
                    "bytes_copied": bytes_copied,
                })
            continue
        src_norm = os.path.normcase(os.path.abspath(source))
        dst_norm = os.path.normcase(os.path.abspath(target))
        if dst_norm == src_norm or dst_norm.startswith(src_norm + os.sep):
            failed += 1
            result["result"] = "failed"
            result["reason"] = "target_inside_source"
            results.append(result)
            if progress:
                progress({
                    "phase": "copying",
                    "limit": limit,
                    "current_pack": result["pack"],
                    "copied": copied,
                    "skipped": skipped,
                    "failed": failed,
                    "updated_assets": updated_assets,
                    "bytes_copied": bytes_copied,
                })
            continue
        if os.path.exists(target):
            ok, manifest = _folder_manifests_match(source, target)
            result.update(manifest)
            if not ok:
                skipped += 1
                result["result"] = "skipped"
                result["reason"] = "target_exists"
                results.append(result)
                if progress:
                    progress({
                        "phase": "copying",
                        "limit": limit,
                        "current_pack": result["pack"],
                        "copied": copied,
                        "skipped": skipped,
                        "failed": failed,
                        "updated_assets": updated_assets,
                        "bytes_copied": bytes_copied,
                    })
                continue
            changed = _update_asset_paths_for_folder(source, target, on_path_moved=on_path_moved)
            copied += 1
            updated_assets += changed
            result["result"] = "copied"
            result["reason"] = "target_already_verified"
            result["assets_updated"] = changed
            results.append(result)
            if progress:
                progress({
                    "phase": "copying",
                    "limit": limit,
                    "current_pack": result["pack"],
                    "copied": copied,
                    "skipped": skipped,
                    "failed": failed,
                    "updated_assets": updated_assets,
                    "bytes_copied": bytes_copied,
                })
            continue
        try:
            os.makedirs(os.path.dirname(target), exist_ok=True)
            shutil.copytree(source, target, copy_function=shutil.copy2)
            ok, manifest = _folder_manifests_match(source, target)
            result.update(manifest)
            if not ok:
                shutil.rmtree(target, ignore_errors=True)
                failed += 1
                result["result"] = "failed"
                result["reason"] = "verify_failed"
                results.append(result)
                if progress:
                    progress({
                        "phase": "copying",
                        "limit": limit,
                        "current_pack": result["pack"],
                        "copied": copied,
                        "skipped": skipped,
                        "failed": failed,
                        "updated_assets": updated_assets,
                        "bytes_copied": bytes_copied,
                    })
                continue
            changed = _update_asset_paths_for_folder(source, target, on_path_moved=on_path_moved)
            copied += 1
            updated_assets += changed
            bytes_copied += int(manifest.get("bytes") or 0)
            result["result"] = "copied"
            result["assets_updated"] = changed
            results.append(result)
            if progress:
                progress({
                    "phase": "copying",
                    "limit": limit,
                    "current_pack": result["pack"],
                    "copied": copied,
                    "skipped": skipped,
                    "failed": failed,
                    "updated_assets": updated_assets,
                    "bytes_copied": bytes_copied,
                })
        except OSError as e:
            shutil.rmtree(target, ignore_errors=True)
            failed += 1
            result["result"] = "failed"
            result["reason"] = str(e)
            results.append(result)
            if progress:
                progress({
                    "phase": "copying",
                    "limit": limit,
                    "current_pack": result["pack"],
                    "copied": copied,
                    "skipped": skipped,
                    "failed": failed,
                    "updated_assets": updated_assets,
                    "bytes_copied": bytes_copied,
                })

    receipt = {
        "started_at": started,
        "finished_at": time.time(),
        "target_root": os.path.normpath(target_root or "D:\\Hangar"),
        "limit": limit,
        "copied": copied,
        "skipped": skipped,
        "failed": failed,
        "updated_assets": updated_assets,
        "bytes_copied": bytes_copied,
        "results": results,
    }
    name = time.strftime("organise-%Y%m%d-%H%M%S.json", time.localtime(receipt["finished_at"]))
    try:
        (ORGANISE_RECEIPT_DIR / name).write_text(json.dumps(receipt, indent=2), encoding="utf-8")
        receipt["receipt"] = str(ORGANISE_RECEIPT_DIR / name)
    except OSError:
        receipt["receipt"] = ""
    if copied:
        try:
            add_library(receipt["target_root"], Path(receipt["target_root"]).name or "Hangar")
        except OSError:
            pass
    if progress:
        progress({"phase": "done", **receipt})
    return receipt


def _is_same_or_child(path, root):
    path = os.path.normcase(os.path.abspath(os.path.normpath(path or "")))
    root = os.path.normcase(os.path.abspath(os.path.normpath(root or "")))
    return bool(path and root and (path == root or path.startswith(root + os.sep)))


def _organise_receipt_rows():
    rows = []
    try:
        files = sorted(ORGANISE_RECEIPT_DIR.glob("organise-*.json"),
                       key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return rows
    for p in files:
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for r in data.get("results") or []:
            if r.get("result") != "copied":
                continue
            source = os.path.normpath(r.get("source") or "")
            target = os.path.normpath(r.get("target") or "")
            if not source or not target:
                continue
            rows.append({
                "source": source,
                "target": target,
                "pack": r.get("pack") or os.path.basename(source),
                "receipt": str(p),
                "finished_at": data.get("finished_at") or 0,
            })
    return rows


def _indexed_count_under(folder):
    with connect() as conn:
        return conn.execute(
            "SELECT COUNT(*) c FROM assets WHERE missing=0 AND path LIKE ? ESCAPE '!'",
            (_path_like(folder),),
        ).fetchone()["c"]


def _delete_index_rows_under(folder):
    with connect() as conn:
        return conn.execute(
            "DELETE FROM assets WHERE path LIKE ? ESCAPE '!'",
            (_path_like(folder),),
        ).rowcount


def organise_cleanup_plan(target_root="D:\\Hangar", limit=500, progress=None):
    """Old source folders that can be removed after Organise copied them.

    A folder is "ready" only when the clean target still contains every file
    from the old source with the same relative path and byte size. If the old
    folder was re-indexed while it still existed, cleanup removes those stale
    rows after the folder is deleted.
    """
    target_root = os.path.normpath(target_root or "D:\\Hangar")
    limit = max(1, min(2000, int(limit or 500)))
    seen = {}
    size_cache = {}
    out = []
    receipt_rows = _organise_receipt_rows()
    total_rows = len(receipt_rows)
    checked = 0
    if progress:
        progress({
            "phase": "checking",
            "limit": total_rows,
            "checked": 0,
            "current_pack": "",
            "current_source": "",
        })
    for row in receipt_rows:
        checked += 1
        source = row["source"]
        target = row["target"]
        if progress and (checked == 1 or checked == total_rows or checked % 10 == 0):
            progress({
                "phase": "checking",
                "limit": total_rows,
                "checked": checked,
                "current_pack": row.get("pack") or os.path.basename(source),
                "current_source": source,
            })
        source_key = os.path.normcase(os.path.normpath(source))
        if source_key in seen:
            continue
        source_abs = os.path.normcase(os.path.abspath(os.path.normpath(source)))
        target_abs = os.path.normcase(os.path.abspath(os.path.normpath(target)))
        target_root_abs = os.path.normcase(os.path.abspath(os.path.normpath(target_root)))
        status = "ready"
        reason = ""
        missing = 0
        indexed = 0
        size = 0
        if source_abs == target_abs:
            status = "same_folder"
            reason = "Old and clean folders are the same"
        elif source_abs == target_root_abs:
            status = "target_root"
            reason = "Old folder is the clean target root"
        elif _is_same_or_child(target, source):
            status = "unsafe_nested"
            reason = "Target is inside the old source"
        elif not os.path.isdir(source):
            status = "gone"
            reason = "Old folder is already gone"
        elif not os.path.isdir(target):
            status = "target_missing"
            reason = "Clean target is missing"
        else:
            ok, missing = _folder_contains_all_files(target, source)
            if not ok:
                status = "target_incomplete"
                reason = f"{missing} file(s) are not present in the clean target"
            indexed = _indexed_count_under(source)
            size = _folder_size(source, size_cache)
        item = {
            **row,
            "status": status,
            "reason": reason,
            "missing": missing,
            "indexed": indexed,
            "bytes": size,
        }
        seen[source_key] = item
        out.append(item)
    out.sort(key=lambda x: (
        x["status"] != "ready",
        -(x.get("bytes") or 0),
        x["source"].lower(),
    ))
    summary = {
        "total": len(out),
        "ready": sum(1 for x in out if x["status"] == "ready"),
        "gone": sum(1 for x in out if x["status"] == "gone"),
        "blocked": sum(1 for x in out if x["status"] not in {"ready", "gone"}),
        "bytes_ready": sum(x.get("bytes") or 0 for x in out if x["status"] == "ready"),
    }
    return {"target_root": target_root, "summary": summary, "items": out[:limit]}


def _prune_empty_parents(start_folder, stop_roots):
    removed = []
    stops = {
        os.path.normcase(os.path.abspath(os.path.normpath(r)))
        for r in (stop_roots or []) if r
    }
    cur = os.path.abspath(os.path.normpath(start_folder or ""))
    while cur:
        key = os.path.normcase(cur)
        parent = os.path.dirname(cur)
        if key in stops or parent == cur:
            break
        try:
            os.rmdir(cur)
            removed.append(cur)
        except OSError:
            break
        cur = parent
    return removed


def _rmtree_allow_readonly(folder):
    def onerror(func, path, exc_info):
        try:
            os.chmod(path, stat.S_IWRITE)
            func(path)
        except OSError:
            raise exc_info[1]

    shutil.rmtree(folder, onerror=onerror)


def apply_organise_cleanup(target_root="D:\\Hangar", limit=100, sources=None, progress=None):
    """Delete verified old Organise source folders.

    This re-builds the cleanup plan immediately before deleting, so a stale UI
    cannot delete a folder that stopped being safe after the plan was shown.
    """
    limit = max(1, min(1000, int(limit or 100)))
    wanted = {
        os.path.normcase(os.path.normpath(s))
        for s in (sources or []) if s
    }
    plan = organise_cleanup_plan(target_root, limit=2000, progress=progress)
    roots = [r["path"] for r in list_libraries()]
    target_root = os.path.normpath(target_root or "D:\\Hangar")
    ready_total = sum(1 for i in plan["items"] if i["status"] == "ready")
    attempted = deleted = failed = skipped = bytes_deleted = index_deleted = 0
    results = []
    if progress:
        progress({
            "phase": "deleting",
            "limit": min(limit, ready_total),
            "attempted": attempted,
            "deleted": deleted,
            "failed": failed,
            "skipped": skipped,
            "bytes_deleted": bytes_deleted,
            "index_deleted": index_deleted,
            "current_pack": "",
            "current_source": "",
        })
    for item in plan["items"]:
        if attempted >= limit:
            break
        source_key = os.path.normcase(os.path.normpath(item["source"]))
        if wanted and source_key not in wanted:
            continue
        if not wanted and item["status"] != "ready":
            continue
        attempted += 1
        result = {
            "source": item["source"],
            "target": item["target"],
            "pack": item.get("pack") or os.path.basename(item["source"]),
            "bytes": item.get("bytes") or 0,
            "indexed": item.get("indexed") or 0,
            "status": item["status"],
        }
        if progress:
            progress({
                "phase": "deleting",
                "limit": min(limit, ready_total),
                "current_pack": result["pack"],
                "current_source": item["source"],
                "attempted": attempted,
                "deleted": deleted,
                "failed": failed,
                "skipped": skipped,
                "bytes_deleted": bytes_deleted,
                "index_deleted": index_deleted,
            })
        if item["status"] != "ready":
            skipped += 1
            result["result"] = "skipped"
            result["reason"] = item.get("reason") or item["status"]
            results.append(result)
            continue
        try:
            _rmtree_allow_readonly(item["source"])
            pruned = _prune_empty_parents(
                os.path.dirname(item["source"]),
                [target_root, *roots],
            )
            removed_rows = _delete_index_rows_under(item["source"])
            deleted += 1
            bytes_deleted += int(item.get("bytes") or 0)
            index_deleted += int(removed_rows or 0)
            result["result"] = "deleted"
            result["index_deleted"] = removed_rows
            result["pruned"] = pruned
            results.append(result)
        except OSError as e:
            failed += 1
            result["result"] = "failed"
            result["reason"] = str(e)
            results.append(result)
        if progress:
            progress({
                "phase": "deleting",
                "limit": min(limit, ready_total),
                "current_pack": result["pack"],
                "current_source": item["source"],
                "attempted": attempted,
                "deleted": deleted,
                "failed": failed,
                "skipped": skipped,
                "bytes_deleted": bytes_deleted,
                "index_deleted": index_deleted,
            })
    if progress:
        progress({
            "phase": "finalising",
            "limit": min(limit, ready_total),
            "current_pack": "",
            "current_source": "",
            "attempted": attempted,
            "deleted": deleted,
            "failed": failed,
            "skipped": skipped,
            "bytes_deleted": bytes_deleted,
            "index_deleted": index_deleted,
        })
    remaining = organise_cleanup_plan(target_root, limit=500)
    return {
        "ok": True,
        "target_root": target_root,
        "attempted": attempted,
        "deleted": deleted,
        "failed": failed,
        "skipped": skipped,
        "bytes_deleted": bytes_deleted,
        "index_deleted": index_deleted,
        "results": results,
        "remaining": remaining["summary"],
    }

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
    id    INTEGER PRIMARY KEY,
    name  TEXT UNIQUE NOT NULL,
    icon  TEXT NOT NULL DEFAULT '',
    sort  INTEGER NOT NULL DEFAULT 0
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

# Starter taxonomy seeded on first run. Each category carries a keyword list used
# to auto-suggest a category from an asset's folder/file name during scanning.
# Users can add their own categories; these are just a sensible base to build on.
DEFAULT_CATEGORIES = [
    # (name, icon, [keywords])
    ("Sci-Fi",       "🚀", ["scifi", "sci-fi", "spaceship", "spacecraft", "space",
                            "starship", "mech", "robot", "droid", "cyber",
                            "cyberpunk", "futuristic", "alien", "ufo", "laser"]),
    ("Buildings",    "🏢", ["building", "buildings", "house", "home", "tower",
                            "skyscraper", "apartment", "office"]),
    ("Architecture", "🏛", ["architecture", "interior", "exterior", "facade",
                            "room", "kitchen", "bathroom", "stairs", "wall"]),
    ("Vehicles",     "🚗", ["vehicle", "car", "cars", "truck", "tank", "plane",
                            "aircraft", "jet", "ship", "boat", "motorcycle",
                            "bike", "bicycle", "train", "bus"]),
    ("Characters",   "🧍", ["character", "char", "human", "person", "people",
                            "creature", "monster", "npc", "avatar", "zombie",
                            "soldier"]),
    ("Weapons",      "🗡", ["weapon", "weapons", "gun", "guns", "rifle", "pistol",
                            "sword", "blade", "knife", "axe", "firearm", "ammo",
                            "grenade"]),
    ("Nature",       "🌲", ["nature", "tree", "trees", "plant", "plants", "rock",
                            "rocks", "terrain", "foliage", "grass", "environment",
                            "landscape", "forest", "flower"]),
    ("Furniture",    "🛋", ["furniture", "chair", "table", "sofa", "couch", "desk",
                            "bed", "shelf", "cabinet", "lamp"]),
    ("Props",        "📦", ["prop", "props", "barrel", "crate", "box", "container"]),
    ("Industrial",   "🏭", ["industrial", "machine", "machinery", "pipe", "pipes",
                            "factory", "mechanical", "engine", "gear"]),
    ("Fantasy",      "🐉", ["fantasy", "medieval", "castle", "dragon", "magic",
                            "wizard", "knight", "dungeon"]),
    ("Food",         "🍎", ["food", "fruit", "drink", "meal", "vegetable", "bottle"]),
]
# name -> compiled whole-word keyword matchers (built lazily).
_CATEGORY_MATCHERS = None


def connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    with connect() as conn:
        conn.executescript(SCHEMA)
        # Sensible default tag palette so new users aren't staring at a blank wall.
        defaults = [
            ("hero", "#E8B04B"), ("wip", "#E87D3E"), ("approved", "#3DBE8B"),
            ("client", "#5B8DEF"), ("retopo-needed", "#C7596B"),
        ]
        for name, color in defaults:
            conn.execute(
                "INSERT OR IGNORE INTO tags(name, color) VALUES (?, ?)", (name, color)
            )
        # Seed the starter category taxonomy (Sci-Fi, Buildings, …).
        for sort, (name, icon, _kw) in enumerate(DEFAULT_CATEGORIES):
            conn.execute(
                "INSERT OR IGNORE INTO categories(name, icon, sort) VALUES (?, ?, ?)",
                (name, icon, sort),
            )


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
    return [dict(r) for r in rows]


# ---- assets ---------------------------------------------------------------

def upsert_asset(meta):
    """meta: dict with path, name, ext, kind, size, mtime."""
    with connect() as conn:
        existing = conn.execute(
            "SELECT id, mtime FROM assets WHERE path=?", (meta["path"],)
        ).fetchone()
        if existing:
            # If the file changed on disk, invalidate cached mesh stats.
            stats_reset = meta["mtime"] != existing["mtime"]
            conn.execute(
                "UPDATE assets SET name=?, ext=?, kind=?, size=?, mtime=?, missing=0"
                + (", stats_done=0, vertices=NULL, faces=NULL" if stats_reset else "")
                + " WHERE id=?",
                (meta["name"], meta["ext"], meta["kind"], meta["size"],
                 meta["mtime"], existing["id"]),
            )
            return existing["id"]
        cur = conn.execute(
            "INSERT INTO assets(path, name, ext, kind, size, mtime, added_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (meta["path"], meta["name"], meta["ext"], meta["kind"],
             meta["size"], meta["mtime"], time.time()),
        )
        # Auto-suggest categories for new model assets from their folder/file name.
        if meta["kind"] == "model":
            _auto_categorize(conn, cur.lastrowid, meta["path"])
        return cur.lastrowid


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


def save_stats(asset_id, vertices, faces):
    with connect() as conn:
        conn.execute(
            "UPDATE assets SET vertices=?, faces=?, stats_done=1 WHERE id=?",
            (vertices, faces, asset_id),
        )


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


def query_assets(search="", kind="", ext="", tag="", collection="", category="",
                 folder="", favorite=False, sort="name", limit=200, offset=0):
    clauses = ["a.missing=0"]
    params = []
    joins = ""
    if search:
        clauses.append("a.name LIKE ?")
        params.append(f"%{search}%")
    if kind:
        clauses.append("a.kind=?")
        params.append(kind)
    if ext:
        # ext may be comma-separated for grouped formats (e.g. ".glb,.gltf")
        exts = [e.strip() for e in ext.split(",") if e.strip()]
        if len(exts) == 1:
            clauses.append("a.ext=?")
            params.append(exts[0])
        elif len(exts) > 1:
            placeholders = ",".join("?" * len(exts))
            clauses.append(f"a.ext IN ({placeholders})")
            params.extend(exts)
    if favorite:
        clauses.append("a.favorite=1")
    if tag:
        joins += (" JOIN asset_tags fat ON fat.asset_id=a.id "
                  " JOIN tags ft ON ft.id=fat.tag_id AND ft.name=?")
        params.append(tag)
    if collection:
        joins += (" JOIN collection_assets fca ON fca.asset_id=a.id "
                  " JOIN collections fc ON fc.id=fca.collection_id AND fc.name=?")
        params.append(collection)
    if category:
        joins += (" JOIN asset_categories fac ON fac.asset_id=a.id "
                  " JOIN categories fcat ON fcat.id=fac.category_id AND fcat.name=?")
        params.append(category)
    if folder:
        # Match every asset living under this folder root (any depth).
        prefix = folder.rstrip("/\\")
        clauses.append("a.path LIKE ?")
        params.append(prefix + os.sep + "%")

    order = {
        "name": "a.name COLLATE NOCASE ASC",
        "recent": "a.added_at DESC",
        "size": "a.size DESC",
        "modified": "a.mtime DESC",
    }.get(sort, "a.name COLLATE NOCASE ASC")

    where = " AND ".join(clauses)
    sql = (f"SELECT DISTINCT a.* FROM assets a {joins} WHERE {where} "
           f"ORDER BY {order} LIMIT ? OFFSET ?")
    params.extend([limit, offset])

    with connect() as conn:
        rows = conn.execute(sql, params).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["tags"] = _tags_for(conn, r["id"])
            out.append(d)
        total = conn.execute(
            f"SELECT COUNT(DISTINCT a.id) c FROM assets a {joins} WHERE {where}",
            params[:-2],
        ).fetchone()["c"]
    return out, total


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
    return {
        "by_kind": {r["kind"]: r["c"] for r in rows},
        "total": total,
        "favorites": favs,
        "model_by_ext": {r["ext"]: r["c"] for r in ext_rows},
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
            "SELECT cat.id, cat.name, cat.icon, COUNT(ac.asset_id) c "
            "FROM categories cat "
            "LEFT JOIN asset_categories ac ON ac.category_id=cat.id "
            "GROUP BY cat.id ORDER BY cat.sort, cat.name COLLATE NOCASE"
        ).fetchall()
    return [dict(r) for r in rows]


def create_category(name, icon=""):
    name = (name or "").strip()
    if not name:
        return
    with connect() as conn:
        nxt = conn.execute("SELECT COALESCE(MAX(sort), -1) + 1 m FROM categories").fetchone()["m"]
        conn.execute(
            "INSERT OR IGNORE INTO categories(name, icon, sort) VALUES (?, ?, ?)",
            (name, icon, nxt),
        )


def remove_category(category_id):
    with connect() as conn:
        conn.execute("DELETE FROM categories WHERE id=?", (category_id,))


def set_category_membership(category_name, asset_id, add=True):
    name = (category_name or "").strip()
    if not name:
        return
    with connect() as conn:
        conn.execute("INSERT OR IGNORE INTO categories(name) VALUES (?)", (name,))
        cat = conn.execute("SELECT id FROM categories WHERE name=?", (name,)).fetchone()
        if add:
            conn.execute(
                "INSERT OR IGNORE INTO asset_categories(category_id, asset_id) "
                "VALUES (?, ?)", (cat["id"], asset_id),
            )
        else:
            conn.execute(
                "DELETE FROM asset_categories WHERE category_id=? AND asset_id=?",
                (cat["id"], asset_id),
            )


def _category_matchers():
    """name -> set of keywords, built once from DEFAULT_CATEGORIES."""
    global _CATEGORY_MATCHERS
    if _CATEGORY_MATCHERS is None:
        _CATEGORY_MATCHERS = {name: set(kws) for name, _icon, kws in DEFAULT_CATEGORIES}
    return _CATEGORY_MATCHERS


def _auto_categorize(conn, asset_id, path):
    """Attach seeded categories whose keywords appear in the asset's path.

    Splits the lower-cased path into word tokens and matches each keyword as a
    whole token (with simple singular/plural tolerance), so "car_sedan" hits
    Vehicles and a "vehicles" folder hits the "vehicle" keyword. Runs inside the
    caller's transaction (shares `conn`) so the new asset row is visible to the
    foreign-key check. Only touches the seeded default categories.
    """
    tokens = set(re.split(r"[^a-z0-9]+", path.lower()))
    tokens.discard("")

    def hit(kw):
        return (kw in tokens
                or (kw + "s") in tokens
                or (kw.endswith("s") and kw[:-1] in tokens))

    matched = [name for name, kws in _category_matchers().items()
               if any(hit(kw) for kw in kws)]
    if not matched:
        return
    placeholders = ",".join("?" * len(matched))
    rows = conn.execute(
        f"SELECT id FROM categories WHERE name IN ({placeholders})", matched
    ).fetchall()
    for r in rows:
        conn.execute(
            "INSERT OR IGNORE INTO asset_categories(category_id, asset_id) VALUES (?, ?)",
            (r["id"], asset_id),
        )

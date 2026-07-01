// Hangar frontend — vanilla JS, no build step, fully offline.

const KIND_COLORS = {
  model: "var(--k-model)", texture: "var(--k-texture)",
  hdri: "var(--k-hdri)", material: "var(--k-material)",
};
const KIND_LABELS = {
  all: "All assets", model: "Models", texture: "Textures",
  hdri: "HDRIs", material: "Materials",
};

// Groups of model extensions shown as sidebar subcategories.
const MODEL_EXT_GROUPS = [
  { label: "Blender", exts: [".blend"] },
  { label: "FBX",     exts: [".fbx"] },
  { label: "OBJ",     exts: [".obj"] },
  { label: "GLB / GLTF", exts: [".glb", ".gltf"] },
  { label: "USD",     exts: [".usd", ".usda", ".usdc", ".usdz"] },
  { label: "STL",     exts: [".stl"] },
  { label: "PLY",     exts: [".ply"] },
  { label: "ABC",     exts: [".abc"] },
  { label: "DAE",     exts: [".dae"] },
  { label: "3DS",     exts: [".3ds"] },
];

function loadCollapsed() {
  // Default (first run / no saved choice): only Models expanded.
  const DEFAULT = ["texture", "hdri", "material"];
  const saved = localStorage.getItem("hangar_collapsed");
  if (saved == null) return new Set(DEFAULT);
  try { return new Set(JSON.parse(saved)); }
  catch (_) { return new Set(DEFAULT); }
}
const state = {
  filter: { kind: "", ext: "", tag: "", collection: "", category: "", folder: "", favorite: false, duplicates: false },
  search: "", sort: "name", scanTimer: null, wasScanning: false,
  collapsed: loadCollapsed(),   // sidebar type sections the user has collapsed
};
const $ = (s) => document.querySelector(s);
function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (ch) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[ch]));
}
function safeColor(value, fallback = "#8A8F9A") {
  const s = String(value || "").trim();
  if (/^#[0-9a-f]{3}$/i.test(s)) {
    return "#" + [...s.slice(1)].map((ch) => ch + ch).join("");
  }
  if (/^#[0-9a-f]{6}$/i.test(s)) return s;
  if (/^var\(--[a-z0-9-]+\)$/i.test(s)) return s;
  return fallback;
}

// The four collapsible Library types.
const TYPE_KINDS = ["model", "texture", "hdri", "material"];

function persistCollapsed() {
  try { localStorage.setItem("hangar_collapsed", JSON.stringify([...state.collapsed])); }
  catch (_) { /* ignore */ }
}

function loadCatFolderExpanded() {
  try { return new Set(JSON.parse(localStorage.getItem("hangar_cat_folder_expanded") || "[]")); }
  catch (_) { return new Set(); }
}
let catFolderExpanded = loadCatFolderExpanded();
function persistCatFolderExpanded() {
  try { localStorage.setItem("hangar_cat_folder_expanded", JSON.stringify([...catFolderExpanded])); }
  catch (_) { /* ignore */ }
}
function catFolderKey(c) {
  return `${c.kind || ""}:${c.name}`;
}
function catFoldersExpanded(c) {
  return catFolderExpanded.has(catFolderKey(c));
}
function expandCatFolders(c) {
  catFolderExpanded.add(catFolderKey(c));
  persistCatFolderExpanded();
}
function toggleCatFolderCollapse(c) {
  const key = catFolderKey(c);
  if (catFolderExpanded.has(key)) catFolderExpanded.delete(key);
  else catFolderExpanded.add(key);
  persistCatFolderExpanded();
  loadState();
}

// Collapse/expand a Library type's nested categories + formats, and persist it.
function toggleCollapse(kind) {
  if (state.collapsed.has(kind)) state.collapsed.delete(kind);
  else state.collapsed.add(kind);
  persistCollapsed();
  loadState();  // re-render the sidebar
}
const thumbBust = {};
const thumbMtime = {};   // id -> asset mtime, so the URL changes when the file does
// Record each asset's mtime so thumbUrl() can stamp it into the query string.
// The stamp makes the tile URL content-addressed: it stays identical while the
// source file is unchanged (so the browser serves it straight from cache — see
// the long max-age on /api/thumb), and changes the moment a rescan picks up an
// edit, refetching without a manual bust.
function recordThumbMtimes(assets) {
  for (const a of assets || []) if (a && a.id != null) thumbMtime[a.id] = a.mtime;
}
function thumbUrl(id) {
  const parts = [];
  if (thumbMtime[id] != null) parts.push(`v=${thumbMtime[id]}`);
  if (thumbBust[id]) parts.push(`t=${thumbBust[id]}`);   // explicit rebake override
  return `/api/thumb/${id}` + (parts.length ? `?${parts.join("&")}` : "");
}

// ---- helpers --------------------------------------------------------------
function fmtSize(bytes) {
  if (bytes == null) return "—";
  const u = ["B", "KB", "MB", "GB"]; let i = 0; let n = bytes;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return `${n < 10 && i > 0 ? n.toFixed(1) : Math.round(n)} ${u[i]}`;
}
function fmtNum(n) { return n == null ? "—" : n.toLocaleString(); }
function baseName(p) { return (p || "").split(/[\\/]/).pop(); }
function api(path, opts) { return fetch("/api/" + path, opts).then((r) => r.json()); }
function post(path, body) {
  return api(path, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  });
}
let toastTimer;
function toast(msg, type) {
  const t = $("#toast");
  t.textContent = msg;
  t.className = "toast" + (type === "success" ? " toast-ok" : type === "error" ? " toast-err" : "");
  t.classList.remove("hidden");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.add("hidden"), 2600);
}

// ---- sidebar / state ------------------------------------------------------
async function loadState() {
  const s = await api("state");
  allCategories = s.categories || [];
  categoryFolders = s.category_folders || [];
  appCaps.blenderReady = !!s.blender_render;
  appCaps.renderExts = s.blender_render_exts || [];
  renderKindFilters(s.counts, allCategories);
  renderCollectionFilters(s.collections);
  renderLibraries(s.libraries);
  renderOfflineBanner(s.libraries);
  renderStatusBar(s.counts, s.version);
  renderMissingPanel(s.counts.missing || 0);
  return s;
}

// ---- missing-files sidebar panel ------------------------------------------
function renderMissingPanel(count) {
  const panel = $("#missingPanel");
  if (!panel) return;
  if (!count) {
    panel.classList.add("hidden");
    // If missing filter was active and everything's now clean, clear the filter.
    if (state.filter.missing) { resetFilter(); refresh(); }
    return;
  }
  panel.classList.remove("hidden");
  const badge = $("#missingCount"); if (badge) badge.textContent = count;
  const btn = $("#missingFilterBtn");
  const countEl = $("#missingFilterCount"); if (countEl) countEl.textContent = count;
  if (btn) {
    btn.classList.toggle("active", !!state.filter.missing);
    btn.onclick = () => {
      if (state.filter.missing) { resetFilter(); }
      else { resetFilter(); state.filter.missing = true; }
      refresh();
    };
  }
}

// Wire the purge button once on DOM load (it doesn't need re-wiring on each loadState).
const _missingPurge = $("#missingPurgeBtn");
if (_missingPurge) _missingPurge.onclick = async () => {
  if (!confirm("Remove all missing entries from the index? This can't be undone.\n\nTo restore them, rescan after reconnecting the drive/folder.")) return;
  let r;
  try { r = await fetch("/api/assets/missing", { method: "DELETE" }).then(x => x.json()); }
  catch (_) { r = null; }
  if (!r || !r.ok) { toast("Couldn't purge missing entries.", "error"); return; }
  toast(`Removed ${r.deleted} stale entr${r.deleted === 1 ? "y" : "ies"} from the index.`, "success");
  resetFilter();
  await loadState();
  refresh();
};

// Persistent notice when one or more added folders can't be reached, so missing
// models are explained rather than silently absent.
function renderOfflineBanner(libs) {
  const offline = (libs || []).filter((l) => l.available === false);
  let banner = $("#offlineBanner");
  if (!offline.length) { if (banner) banner.remove(); return; }
  if (!banner) {
    banner = document.createElement("div");
    banner.id = "offlineBanner";
    banner.className = "offline-banner";
    const main = document.querySelector(".main");
    main.insertBefore(banner, $("#grid"));
  }
  const names = offline.map((l) => l.name).join(", ");
  banner.innerHTML =
    `<span class="ob-ico">⚠</span> ` +
    `${offline.length === 1 ? "Folder" : offline.length + " folders"} not accessible: ` +
    `<strong>${esc(names)}</strong> — ${offline.length === 1 ? "its" : "their"} assets are kept ` +
    `indexed but can't be opened. Reconnect the drive/folder, then ` +
    `<button id="obRescan" class="ob-rescan">Rescan</button>.`;
  $("#obRescan").onclick = () => $("#rescanBtn").click();
}

function renderStatusBar(counts, version) {
  $("#statusSummary").textContent = `${counts.total.toLocaleString()} assets`;
  const parts = [];
  const bk = counts.by_kind || {};
  if (bk.model) parts.push(`${bk.model} models`);
  if (bk.texture) parts.push(`${bk.texture} textures`);
  if (bk.hdri) parts.push(`${bk.hdri} HDRIs`);
  if (bk.material) parts.push(`${bk.material} materials`);
  $("#statusBreakdown").textContent = parts.join("  ·  ");
  if (version) $("#statusVersion").textContent = `Hangar v${version}`;
}

function renderKindFilters(counts, cats) {
  cats = cats || [];
  const modelByExt = counts.model_by_ext || {};
  const items = [
    ["", "all", counts.total],
    ["model", "model", counts.by_kind.model || 0],
    ["texture", "texture", counts.by_kind.texture || 0],
    ["hdri", "hdri", counts.by_kind.hdri || 0],
    ["material", "material", counts.by_kind.material || 0],
  ];
  const ul = $("#kindFilters"); ul.innerHTML = "";
  for (const [kind, key, count] of items) {
    const kindCats = cats.filter((c) => (c.kind || "") === kind);
    const hasExt = kind === "model" && count > 0 &&
      MODEL_EXT_GROUPS.some((g) => g.exts.some((e) => (modelByExt[e] || 0) > 0));
    const hasChildren = kindCats.length > 0 || hasExt;
    const collapsed = state.collapsed.has(kind);

    const li = document.createElement("li");
    li.className = "kind-item";
    const isPlainKind = !state.filter.ext && !state.filter.favorite
      && !state.filter.tag && !state.filter.collection && !state.filter.category;
    const active = isPlainKind && state.filter.kind === kind;
    if (active) li.classList.add("active");
    const color = KIND_COLORS[kind] || "var(--mute)";
    const twisty = hasChildren
      ? `<span class="twisty">${collapsed ? "▸" : "▾"}</span>`
      : `<span class="twisty-spacer"></span>`;
    li.innerHTML = twisty +
      `<span class="dot" style="background:${color}"></span>` +
      `<span>${esc(KIND_LABELS[key])}</span><span class="count">${count}</span>`;
    li.onclick = () => {
      resetFilter();
      state.filter.kind = kind;
      // Accordion: selecting a type expands it and collapses the other types.
      if (TYPE_KINDS.includes(kind)) {
        state.collapsed = new Set(TYPE_KINDS.filter((k) => k !== kind));
        persistCollapsed();
      }
      refresh();
    };
    if (hasChildren) {
      li.querySelector(".twisty").onclick = (e) => {
        e.stopPropagation();
        toggleCollapse(kind);
      };
    }
    ul.appendChild(li);

    if (collapsed) continue;  // children hidden

    // Categories nested under their type, with their represented folders under
    // each category (e.g. Furniture > Beds).
    for (const c of kindCats) {
      const folders = foldersForCategory(c);
      ul.appendChild(buildCategoryItem(c, true, folders));
      if (catFoldersExpanded(c)) {
        for (const f of folders) ul.appendChild(buildCategoryFolderItem(c, f));
      }
    }

    // Model file-format subcategories, under Models below its categories.
    if (kind === "model" && count > 0) {
      for (const grp of MODEL_EXT_GROUPS) {
        const grpCount = grp.exts.reduce((s, e) => s + (modelByExt[e] || 0), 0);
        if (!grpCount) continue;
        const sub = document.createElement("li");
        sub.className = "sub-item";
        const extKey = grp.exts.join(",");
        const subActive = state.filter.kind === "model" && state.filter.ext === extKey
          && !state.filter.favorite && !state.filter.tag && !state.filter.collection;
        if (subActive) sub.classList.add("active");
        sub.innerHTML =
          `<span class="sub-dot"></span>` +
          `<span>${esc(grp.label)}</span><span class="count">${grpCount}</span>`;
        sub.onclick = (e) => {
          e.stopPropagation();
          resetFilter();
          state.filter.kind = "model";
          state.filter.ext = extKey;
          refresh();
        };
        ul.appendChild(sub);
      }
    }
  }

  const fav = document.createElement("li");
  fav.className = "kind-item";
  if (state.filter.favorite) fav.classList.add("active");
  fav.innerHTML =
    `<span class="dot" style="background:var(--signal)"></span>` +
    `<span>Favorites</span><span class="count">${counts.favorites}</span>`;
  fav.onclick = () => { resetFilter(); state.filter.favorite = true; refresh(); };
  ul.appendChild(fav);

  // Always-visible "add category" row — the small + in the header is easy to
  // miss, and several people couldn't find how to create a category.
  const addCat = document.createElement("li");
  addCat.className = "kind-item add-cat-row";
  addCat.title = "Create a category to organise assets";
  addCat.innerHTML =
    `<span class="twisty-spacer"></span>` +
    `<span class="dot" style="background:transparent">＋</span>` +
    `<span style="color:var(--signal)">New category…</span>`;
  addCat.onclick = async () => { if (await promptNewCategory()) refresh(); };
  ul.appendChild(addCat);
}

// Categories live nested under their asset type in the Library list (Poly
// Haven–style): shared categories under "All assets", scoped ones under their
// own type. See renderKindFilters. `kind` scope: "model"/"hdri"/"texture"/
// "material", or "" = shared.
function buildCategoryItem(c, nested, folders) {
    const li = document.createElement("li");
    li.className = "cat-item" + (nested ? " cat-sub" : "");
    if (state.filter.category === c.name) li.classList.add("active");
    folders = folders || foldersForCategory(c);
    const hasFolders = folders.length > 0;
    const folderCollapsed = !catFoldersExpanded(c);
    const folderToggle = hasFolders
      ? `<button class="cat-folder-toggle" title="${folderCollapsed ? "Show" : "Hide"} folders">${folderCollapsed ? "▸" : "▾"}</button>`
      : `<span class="cat-folder-spacer"></span>`;
    const icon = c.icon ? `<span class="cat-ico">${esc(c.icon)}</span>` : `<span class="dot" style="background:var(--k-model)"></span>`;
    const kwTitle = c.keywords
      ? `Auto-match keywords: ${c.keywords}\nClick to edit`
      : "No auto-match keywords yet — click to add";
    li.innerHTML =
      folderToggle +
      icon +
      `<span class="cat-name">${esc(c.name)}</span><span class="count">${c.c}</span>` +
      `<button class="cat-kw" title="${esc(kwTitle)}">✎</button>` +
      `<button class="cat-remove" title="Delete category">&times;</button>`;
    // Keep the current kind context when drilling into a category (Poly Haven
    // stays on the HDRI tab when you pick an HDRI category).
    li.onclick = () => {
      const k = state.filter.kind;
      resetFilter();
      state.filter.kind = (c.kind && c.kind === k) ? k : (c.kind || "");
      state.filter.category = c.name;
      if (hasFolders) expandCatFolders(c);
      refresh();
    };

    const toggle = li.querySelector(".cat-folder-toggle");
    if (toggle) {
      toggle.onclick = (e) => {
        e.stopPropagation();
        toggleCatFolderCollapse(c);
      };
    }

    li.querySelector(".cat-kw").onclick = async (e) => {
      e.stopPropagation();
      const next = prompt(
        `Auto-match keywords for "${c.name}" (comma-separated).\n` +
        "Any asset whose folder/file name contains one of these is filed here when you Auto-classify (⚡).",
        c.keywords || ""
      );
      if (next === null) return;
      await post(`categories/${c.id}/keywords`, { keywords: next });
      const r = await post("categories/auto", {});
      if (r.ok && r.links_added) toast(`Filed ${r.assets_matched} asset${r.assets_matched === 1 ? "" : "s"}`, "success");
      await loadState(); refresh();
    };

    li.querySelector(".cat-remove").onclick = async (e) => {
      e.stopPropagation();
      if (!confirm(`Delete category "${c.name}"? Assets stay; only the grouping is removed.`)) return;
      await api(`categories/${c.id}`, { method: "DELETE" });
      if (state.filter.category === c.name) resetFilter();
      await loadState(); refresh();
    };

    // Drop target: a dragged asset card gets filed into this category. (The
    // sidebar is sorted alphabetically now, so categories aren't drag-reordered.)
    li.dataset.catId = c.id;
    li.dataset.catKind = c.kind || "";
    li.addEventListener("dragover", (e) => {
      e.preventDefault();
      li.classList.add("drop-over");
    });
    li.addEventListener("dragleave", () => li.classList.remove("drop-over"));
    li.addEventListener("drop", async (e) => {
      e.preventDefault();
      li.classList.remove("drop-over");
      const assetId = e.dataTransfer.getData("text/x-hangar-asset-id");
      if (!assetId) return;
      await post(`assets/${assetId}/category`, { category: c.name, add: true });
      toast(`Added to ${c.icon || ""} ${c.name}`.trim(), "success");
      await loadState();
    });

    return li;
}

function foldersForCategory(c) {
  return (categoryFolders || [])
    .filter((f) => f.category === c.name && (f.kind || "") === (c.kind || ""))
    .sort((a, b) => a.name.localeCompare(b.name, undefined, { sensitivity: "base" }));
}

function destinationFolderForCategory(a, c) {
  const folders = foldersForCategory(c);
  if (folders.length) {
    const exact = folders.find((f) => f.name.localeCompare(c.name, undefined, { sensitivity: "base" }) === 0);
    if (exact) return exact;
    return [...folders].sort((x, y) => (y.count || 0) - (x.count || 0))[0];
  }
  const parts = (a.path || "").replace(/[\\/]+$/, "").split(/[\\/]/);
  if (parts.length < 3) return null;
  const currentFolder = parts.slice(0, -1).join("\\");
  const parentFolder = parts.slice(0, -2).join("\\");
  const sibling = parentFolder ? `${parentFolder}\\${c.name}` : "";
  if (!sibling || sibling.toLowerCase() === currentFolder.toLowerCase()) return null;
  return { name: c.name, path: sibling, inferred: true };
}

function buildCategoryFolderItem(c, f) {
  const li = document.createElement("li");
  li.className = "cat-folder-item";
  if (state.filter.category === c.name && state.filter.folder === f.path) li.classList.add("active");
  li.title = f.path;
  li.innerHTML =
    `<span class="folder-dot"></span>` +
    `<span class="cat-folder-name">${esc(f.name)}</span><span class="count">${f.count}</span>`;
  li.onclick = (e) => {
    e.stopPropagation();
    resetFilter();
    state.filter.kind = c.kind || "";
    state.filter.category = c.name;
    state.filter.folder = f.path;
    refresh();
  };
  return li;
}

function renderCollectionFilters(cols) {
  const ul = $("#collectionFilters"); ul.innerHTML = "";
  if (!cols.length) {
    ul.innerHTML = `<li style="color:var(--faint);cursor:default">No collections yet</li>`;
    return;
  }
  for (const c of cols) {
    const li = document.createElement("li");
    if (state.filter.collection === c.name) li.classList.add("active");
    li.innerHTML =
      `<span class="dot" style="background:var(--select)"></span>` +
      `<span>${esc(c.name)}</span><span class="count">${c.c}</span>`;
    li.onclick = () => { resetFilter(); state.filter.collection = c.name; refresh(); };

    // Drop target: accept dragged model cards.
    li.addEventListener("dragover", (e) => {
      e.preventDefault();
      li.classList.add("drop-over");
    });
    li.addEventListener("dragleave", () => li.classList.remove("drop-over"));
    li.addEventListener("drop", async (e) => {
      e.preventDefault();
      li.classList.remove("drop-over");
      const assetId = e.dataTransfer.getData("text/x-hangar-asset-id");
      if (!assetId) return;
      await post(`assets/${assetId}/collection`, { collection: c.name, add: true });
      toast(`Added to "${c.name}"`, "success");
      await loadState();
    });

    ul.appendChild(li);
  }
}

// Short label for the drive/volume a path lives on, shown beside each library
// folder. Windows drive letters ("C:") and UNC shares ("\\nas\share") get a
// label; POSIX paths have no drive-letter concept so the top-level root is used.
function driveLabel(path) {
  if (!path) return "";
  const win = path.match(/^([A-Za-z]):/);
  if (win) return win[1].toUpperCase() + ":";
  const unc = path.match(/^\\\\([^\\/]+)[\\/]+([^\\/]+)/);
  if (unc) return "\\\\" + unc[1] + "\\" + unc[2];
  const posix = path.match(/^\/([^\/]+)/);   // e.g. "/mnt/ext" → "/mnt"
  if (posix) return "/" + posix[1];
  return "";
}

function renderLibraries(libs) {
  const ul = $("#libraryList"); ul.innerHTML = "";
  if (!libs.length) {
    ul.innerHTML = `<li style="color:var(--faint)">No folders added</li>`;
    return;
  }
  for (const lib of libs) {
    const li = document.createElement("li");
    li.className = "lib-item" + (lib.available === false ? " lib-offline" : "");
    li.title = lib.available === false
      ? `${lib.path}\n⚠ Not accessible right now (drive disconnected, moved, or no permission).\nIts ${lib.asset_count || 0} assets stay indexed; reconnect the folder and Rescan.`
      : `${lib.path}\n(click to show contents)`;
    if (state.filter.folder === lib.path) li.classList.add("active");
    const dotColor = lib.available === false ? "var(--k-model)" : "var(--faint)";
    const warn = lib.available === false ? `<span class="lib-warn" title="Folder unavailable">⚠</span>` : "";
    const drv = driveLabel(lib.path);
    const drive = drv ? `<span class="lib-drive" title="On ${esc(drv)}">${esc(drv)}</span>` : "";
    li.innerHTML =
      `<span class="dot" style="background:${dotColor}"></span>` +
      `<span class="lib-name">${esc(lib.name)}</span>` + drive + warn +
      `<button class="lib-remove" title="Stop indexing this folder (files kept on disk)">&times;</button>`;
    // Click the folder to filter the grid to everything under it.
    li.onclick = () => { resetFilter(); state.filter.folder = lib.path; refresh(); };
    li.querySelector(".lib-remove").onclick = async (e) => {
      e.stopPropagation();
      if (!confirm(
        `Stop indexing the folder "${lib.name}"?\n\n` +
        `This only removes it from Hangar's library — your files on disk are NOT ` +
        `moved or deleted. You can add the folder again any time.`)) return;
      await api(`libraries/${lib.id}`, { method: "DELETE" });
      if (state.filter.folder === lib.path) resetFilter();
      await loadState(); refresh();
    };
    ul.appendChild(li);
  }
}

let _facetKindCache = {};  // kind → { subtypes, resolutions }, invalidated on filter reset

function resetFilter() {
  state.filter = { kind: "", ext: "", tag: "", collection: "", category: "", folder: "",
                   favorite: false, subtype: "", resolution: "", missing: false, duplicates: false };
  _facetKindCache = {};
}

// ---- clear-filter button visibility ---------------------------------------
function updateClearBtn() {
  const active = state.filter.kind || state.filter.ext || state.filter.tag
    || state.filter.collection || state.filter.category || state.filter.folder
    || state.filter.favorite || state.filter.missing || state.filter.subtype
    || state.filter.resolution || state.filter.duplicates || state.search;
  $("#clearFilterBtn").classList.toggle("hidden", !active);
}

// ---- faceted filter strip -------------------------------------------------
// Shows subtype (decal / atlas) and resolution (2k / 4k …) chips when the
// active kind has matching assets. Chips toggle — click again to clear.
// Fetched per kind and cached so navigation doesn't re-request on every sort.
async function updateFacetStrip() {
  const strip = $("#facetStrip");
  const kind = state.filter.kind || "";
  // Only texture and hdri kinds carry subtype/resolution facets currently.
  if (kind !== "texture" && kind !== "hdri") { strip.classList.add("hidden"); return; }
  if (!_facetKindCache[kind]) {
    _facetKindCache[kind] = await api(`facets?kind=${kind}`);
  }
  const { subtypes = [], resolutions = [] } = _facetKindCache[kind] || {};
  if (!subtypes.length && !resolutions.length) { strip.classList.add("hidden"); return; }

  const f = state.filter;
  const chip = (facet, val, count) =>
    `<button class="facet-chip${f[facet] === val ? " is-on" : ""}"
       data-facet="${esc(facet)}" data-val="${esc(val)}">${esc(val)}<span class="facet-c">${count}</span></button>`;

  let html = "";
  if (subtypes.length) {
    html += `<span class="facet-label">Type</span>`;
    html += subtypes.map(s => chip("subtype", s.value, s.count)).join("");
  }
  if (resolutions.length) {
    if (subtypes.length) html += `<span class="facet-div"></span>`;
    html += `<span class="facet-label">Resolution</span>`;
    html += resolutions.map(r => chip("resolution", r.value, r.count)).join("");
  }
  strip.innerHTML = html;
  strip.classList.remove("hidden");

  strip.querySelectorAll(".facet-chip").forEach(btn => {
    btn.onclick = () => {
      const facet = btn.dataset.facet;
      const val = btn.dataset.val;
      // Toggle off when tapping the already-active chip.
      state.filter[facet] = state.filter[facet] === val ? "" : val;
      refresh();
    };
  });
}

// ---- multi-select ---------------------------------------------------------
const selection = new Set(); // Set of asset IDs currently selected
let _lastSelectedIdx = -1;  // index into _currentAssets; anchor for shift-range
let _currentAssets = [];    // assets in DISPLAY order (grouped views concatenate sections)
let _displaySections = [];  // section key per _currentAssets index (display-order bookkeeping)

function updateBatchBar() {
  let bar = $("#batchBar");
  if (selection.size === 0) {
    if (bar) bar.remove();
    return;
  }
  if (!bar) {
    bar = document.createElement("div");
    bar.id = "batchBar";
    bar.className = "batch-bar";
    document.getElementById("app").appendChild(bar);
  }
  const tags = allTags.length
    ? allTags.map(t =>
        `<button class="batch-tag-btn" data-tag="${esc(t.name)}" style="border-color:${safeColor(t.color)}40;color:${safeColor(t.color)}">${esc(t.name)}</button>`
      ).join("")
    : '<span style="color:var(--faint);font-size:11px">No tags yet</span>';
  // "Send to Blender" only when the bridge has rendered at least once / Blender
  // is configured — otherwise the queue piles up with nothing reading it.
  const blendBtn = appCaps.blenderReady
    ? `<button class="batch-blend-btn" id="batchBlendBtn">⮞ Send to Blender</button>`
    : "";
  bar.innerHTML = `
    <span class="batch-count">${selection.size} selected</span>
    <div class="batch-sep"></div>
    <div class="batch-tags">${tags}</div>
    ${blendBtn}
    <button class="batch-coll-btn" id="batchCollBtn">+ Collection</button>
    <button class="batch-cat-btn" id="batchCatBtn">+ Category</button>
    <button class="batch-cat-btn" id="batchCatRemoveBtn">- Category</button>
    <button class="batch-del-btn" id="batchDelBtn">Remove from Hangar</button>
    <button class="batch-clear" id="batchClearBtn">✕</button>`;

  bar.querySelectorAll(".batch-tag-btn").forEach(btn => {
    btn.onclick = async () => {
      const tag = btn.dataset.tag;
      await post("assets/batch/tag", { ids: [...selection], tag });
      toast(`Tagged ${selection.size} asset${selection.size > 1 ? "s" : ""} "${tag}"`, "success");
      refresh(); loadState();
    };
  });
  $("#batchCollBtn").onclick = async () => {
    const name = prompt("Add to collection:"); if (!name) return;
    await post("assets/batch/collection", { ids: [...selection], collection: name });
    toast(`Added ${selection.size} asset${selection.size > 1 ? "s" : ""} to "${name}"`, "success");
    refresh(); loadState();
  };
  $("#batchCatBtn").onclick = (e) => _openBatchCatMenu(true, e);
  $("#batchCatRemoveBtn").onclick = (e) => _openBatchCatMenu(false, e);
  $("#batchDelBtn").onclick = async () => {
    if (!confirm(`Remove ${selection.size} asset${selection.size > 1 ? "s" : ""} from Hangar? Files stay on disk.`)) return;
    await post("assets/batch/remove", { ids: [...selection] });
    toast(`Removed ${selection.size} asset${selection.size > 1 ? "s" : ""} from index`, "success");
    clearSelection(); refresh(); loadState();
  };
  $("#batchClearBtn").onclick = clearSelection;
  const blend = $("#batchBlendBtn");
  if (blend) blend.onclick = async () => {
    blend.disabled = true; blend.textContent = "Sending…";
    let r; try { r = await post("assets/batch/send-blender", { ids: [...selection] }); }
    catch (_) { r = null; }
    blend.disabled = false; blend.textContent = "⮞ Send to Blender";
    if (r && r.ok) {
      const parts = [];
      if (r.model) parts.push(`${r.model} model${r.model > 1 ? "s" : ""}`);
      if (r.material) parts.push(`${r.material} material${r.material > 1 ? "s" : ""}`);
      if (r.hdri) parts.push(`${r.hdri} HDRI${r.hdri > 1 ? "s" : ""}`);
      toast(parts.length
        ? `Queued ${parts.join(", ")} for Blender.`
        : "Nothing sendable in the selection.", parts.length ? "success" : "error");
    } else {
      toast((r && r.error) || "Couldn't queue for Blender. Is the bridge connected?", "error");
    }
  };
}

function selectedAssets() {
  return [...selection]
    .map((id) => _currentAssets.find((a) => a.id === id) || currentAssets.find((a) => a.id === id))
    .filter(Boolean);
}

// Auto-filled category picker for the multi-select batch action. Instead of a
// blank prompt where you retype a name, this lists the existing categories to
// click: for "Add", every category that fits the selected assets' kinds (plus a
// New category… escape hatch); for "Remove", only the categories the selection
// is actually in. Anchored at (x, y) and dismissed like any context menu.
function showBatchCategoryMenu(add, x, y) {
  closeCtxMenu();
  const kinds = new Set(selectedAssets().map((a) => a.kind));
  let cats = (allCategories || []).filter((c) => !c.kind || !kinds.size || kinds.has(c.kind));
  if (!add) {
    // Remove mode: offer the categories the selection actually belongs to. Asset
    // rows only carry `.categories` in the grouped view, so also fold in the
    // category currently being browsed; if we still know nothing, fall back to
    // every applicable category rather than showing an empty menu.
    const inUse = new Set();
    for (const a of selectedAssets()) for (const cn of (a.categories || [])) inUse.add(cn);
    if (state.filter.category) inUse.add(state.filter.category);
    if (inUse.size) cats = cats.filter((c) => inUse.has(c.name));
  }
  const menu = document.createElement("div");
  menu.className = "ctx-menu";
  const title = document.createElement("div");
  title.className = "ctx-title";
  title.textContent = add ? `Add ${selection.size} to…` : `Remove ${selection.size} from…`;
  menu.appendChild(title);

  if (!cats.length) {
    const none = document.createElement("div");
    none.className = "ctx-empty";
    none.textContent = add ? "No categories yet — create one below"
                           : "Selection isn't in any category";
    menu.appendChild(none);
  }
  for (const c of cats) {
    const item = document.createElement("button");
    item.className = "ctx-item" + (add ? "" : " ctx-danger");
    item.innerHTML =
      `<span class="ctx-ico">${esc(c.icon || "📂")}</span>` +
      `<span class="ctx-name">${esc(c.name)}</span>`;
    item.onclick = async (e) => {
      e.stopPropagation(); closeCtxMenu(); await batchCategoryApply(add, c.name);
    };
    menu.appendChild(item);
  }
  if (add) {
    const sep = document.createElement("div"); sep.className = "ctx-sep"; menu.appendChild(sep);
    const mk = document.createElement("button");
    mk.className = "ctx-item";
    mk.innerHTML = `<span class="ctx-ico">＋</span><span class="ctx-name">New category…</span>`;
    mk.onclick = async (e) => {
      e.stopPropagation(); closeCtxMenu();
      const name = (prompt("New category name:") || "").trim();
      if (!name) return;
      const icon = prompt("Icon (emoji, optional — Cancel to skip):") || "";
      const kind = kinds.size === 1 ? [...kinds][0] : "";
      await post("categories", { name, icon, kind });
      await batchCategoryApply(true, name);
    };
    menu.appendChild(mk);
  }
  _mountCtxMenu(menu, x, y);
}

async function batchCategoryApply(add, name) {
  if (!name) return;
  await post("assets/batch/category", { ids: [...selection], category: name, add });
  toast(`${add ? "Added" : "Removed"} ${selection.size} asset${selection.size > 1 ? "s" : ""} ${add ? "to" : "from"} "${name}"`, "success");
  refresh(); loadState();
}

// Anchor the picker just above a batch-bar button (the bar sits at the bottom,
// so _mountCtxMenu's clamp lifts the menu on-screen).
function _openBatchCatMenu(add, ev) {
  const r = ev.currentTarget.getBoundingClientRect();
  showBatchCategoryMenu(add, r.left, r.top - 6);
}

function clearSelection() {
  selection.clear();
  _lastSelectedIdx = -1;
  document.querySelectorAll(".card.is-selected").forEach(c => c.classList.remove("is-selected"));
  updateBatchBar();
}

function toggleSelect(id, card, idx) {
  if (selection.has(id)) {
    selection.delete(id);
    card.classList.remove("is-selected");
  } else {
    selection.add(id);
    card.classList.add("is-selected");
  }
  if (idx !== undefined) _lastSelectedIdx = idx;
  updateBatchBar();
}

function rangeSelect(toIdx) {
  // Select the whole display-order range between the anchor and the target —
  // this spans category/folder sections, picking up exactly what's visible
  // between the two clicks. Safe because _currentAssets is in DISPLAY order
  // (grouped views concatenate their sections into it), so the range matches
  // the on-screen layout rather than the flat API order.
  const lo = Math.min(_lastSelectedIdx, toIdx);
  const hi = Math.max(_lastSelectedIdx, toIdx);
  for (let j = lo; j <= hi; j++) {
    const a = _currentAssets[j];
    if (!a) continue;
    selection.add(a.id);
    const c = document.querySelector(`#grid .card[data-id="${a.id}"]`);
    if (c) c.classList.add("is-selected");
  }
  updateBatchBar();
}

// ---- 3D viewer ------------------------------------------------------------
const VIEWER_EXTS = new Set(['.glb', '.gltf', '.fbx']);
let _viewerMod = null;
async function getViewerMod() {
  if (!_viewerMod) _viewerMod = await import('/viewer.js');
  return _viewerMod;
}
function destroyViewerIfActive() {
  if (_viewerMod) _viewerMod.destroyViewer();
}

// ---- hover quick-preview --------------------------------------------------
// Dwell on a model card and a small auto-rotating 3D preview floats beside it.
// Uses a SEPARATE viewer module instance so it never clobbers the drawer's
// viewer (or its thumbnail snapshot). One popup at a time; torn down on leave.
let _hoverMod = null;
let _hoverTimer = null;
let _hoverEl = null;
let _hoverForId = null;
const HOVER_DELAY = 480;  // ms of dwell before the preview spins up

async function getHoverMod() {
  // A second dynamic import shares the cached module, so we reuse one renderer
  // for hover. The drawer viewer and hover viewer never run simultaneously in
  // practice (hover is cancelled the moment the drawer opens).
  if (!_hoverMod) _hoverMod = await import('/viewer.js');
  return _hoverMod;
}

function _closeHoverPreview() {
  if (_hoverTimer) { clearTimeout(_hoverTimer); _hoverTimer = null; }
  if (_hoverMod) _hoverMod.destroyViewer();
  if (_hoverEl) { _hoverEl.remove(); _hoverEl = null; }
  _hoverForId = null;
}

function _openHoverPreview(a, card) {
  // Don't fight the drawer or a drag/selection gesture.
  if (isDrawerOpen() || selection.size > 0 || _dragScrollDir) return;
  _hoverForId = a.id;
  const pop = document.createElement("div");
  pop.className = "hover-preview";
  document.body.appendChild(pop);
  // Position to the card's right, flipping left near the viewport edge.
  const r = card.getBoundingClientRect();
  const W = 240, H = 240, gap = 10;
  let left = r.right + gap;
  if (left + W > window.innerWidth - 8) left = r.left - W - gap;
  let top = r.top + r.height / 2 - H / 2;
  top = Math.max(8, Math.min(top, window.innerHeight - H - 8));
  pop.style.left = `${left}px`;
  pop.style.top = `${top}px`;
  pop.style.width = `${W}px`;
  pop.style.height = `${H}px`;
  getHoverMod().then(mod => {
    // The user may have moved on before the module/import resolved.
    if (_hoverForId !== a.id || !_hoverEl) return;
    mod.startViewer(pop, a.id, a.ext, { autoRotate: true, noThumb: true });
  });
  _hoverEl = pop;
}

function bindHoverPreview(card, a) {
  if (!VIEWER_EXTS.has(a.ext)) return;  // only GLB/GLTF/FBX have a browser loader
  card.addEventListener("mouseenter", () => {
    if (_hoverTimer) clearTimeout(_hoverTimer);
    _hoverTimer = setTimeout(() => _openHoverPreview(a, card), HOVER_DELAY);
  });
  card.addEventListener("mouseleave", _closeHoverPreview);
  // Any click/drag intent should drop the preview immediately.
  card.addEventListener("mousedown", _closeHoverPreview);
}

// ---- grid -----------------------------------------------------------------
let currentAssets = [];  // last fetched asset list for drawer prev/next
let drawerIdx = -1;      // position of the open drawer asset in currentAssets
let drawerAssetId = null; // id of the asset currently shown in the drawer

function loadSectionCollapsed() {
  try { return new Set(JSON.parse(localStorage.getItem("hangar_section_collapsed") || "[]")); }
  catch (_) { return new Set(); }
}
let sectionCollapsed = loadSectionCollapsed();
function persistSectionCollapsed() {
  try { localStorage.setItem("hangar_section_collapsed", JSON.stringify([...sectionCollapsed])); }
  catch (_) {}
}
function sectionIsCollapsed(key) {
  return sectionCollapsed.has(key);
}
function toggleSectionCollapsed(key) {
  if (sectionCollapsed.has(key)) sectionCollapsed.delete(key);
  else sectionCollapsed.add(key);
  persistSectionCollapsed();
}
function sectionCollapseButton(key, label) {
  const collapsed = sectionIsCollapsed(key);
  const btn = document.createElement("button");
  btn.className = "section-toggle";
  btn.type = "button";
  btn.title = collapsed ? `Expand ${label}` : `Collapse ${label}`;
  btn.textContent = collapsed ? "▸" : "▾";
  btn.onclick = (e) => {
    e.stopPropagation();
    toggleSectionCollapsed(key);
    refresh();
  };
  return btn;
}

async function refresh() {
  const f = state.filter;
  // Duplicates view: every file whose name is shared by more than one file,
  // grouped by name. It's its own mode, so it overrides the auto-groupings below.
  const dupes = f.duplicates;
  // Grouped view: "All assets" or a plain type selection (Models/Textures/…)
  // with no other filter splits the grid into category sections.
  const grouped = !dupes && (!f.kind || TYPE_KINDS.includes(f.kind)) && !f.ext && !f.tag
    && !f.collection && !f.category && !f.folder && !f.favorite && !state.search
    && !f.subtype && !f.resolution;
  // Folder-grouped view: a library folder with no sub-filters groups by subfolder.
  const folderGrouped = !dupes && !!f.folder && !f.kind && !f.ext && !f.tag
    && !f.collection && !f.category && !f.favorite && !state.search
    && !f.subtype && !f.resolution;
  const categoryFolderGrouped = !dupes && !!f.category && !f.folder && !f.tag
    && !f.collection && !f.favorite && !state.search
    && !f.subtype && !f.resolution;

  const p = new URLSearchParams();
  if (f.kind) p.set("kind", f.kind);
  if (f.ext)  p.set("ext", f.ext);
  if (f.tag) p.set("tag", f.tag);
  if (f.collection) p.set("collection", f.collection);
  if (f.category) p.set("category", f.category);
  if (f.folder) p.set("folder", f.folder);
  if (f.favorite) p.set("favorite", "1");
  if (f.missing) p.set("missing", "1");
  if (f.subtype) p.set("subtype", f.subtype);
  if (f.resolution) p.set("resolution", f.resolution);
  if (state.search) p.set("search", state.search);
  p.set("sort", state.sort);
  if (dupes) { p.set("duplicates", "1"); p.set("limit", "2000"); }
  // Collapse texture-map sets (diffuse+normal+roughness+…) into one tile each —
  // but NOT in the duplicates view, where every individual copy must show.
  else p.set("group", "set");
  if (grouped) { p.set("with_categories", "1"); p.set("limit", "2000"); }
  if (folderGrouped) { p.set("limit", "2000"); }
  if (categoryFolderGrouped) { p.set("limit", "2000"); }

  const data = await api("assets?" + p.toString());
  recordThumbMtimes(data.assets);
  if (dupes) renderGroupedByName(data.assets);
  else if (folderGrouped) renderGroupedByFolder(data.assets, f.folder);
  else if (categoryFolderGrouped) renderGroupedByFolder(data.assets, "", { parentOnly: true });
  else if (grouped) renderGroupedGrid(data.assets, f.kind, data.total);
  else renderGrid(data.assets, data.total);
  // The renderers build _currentAssets in display order (grouped views reorder
  // by section); the drawer's prev/next must walk that same order, and the card
  // indices passed to openDrawer index into it.
  currentAssets = _currentAssets;
  await loadState();
  enqueueMissingThumbs(data.assets);   // fill in USD/Alembic tiles in the background
  updateActiveLabel(data.total);
  updateClearBtn();
  updateDupBtn();
  updateFacetStrip();
}

// Grid split into category sections (for a plain type view). Each category of
// the active kind becomes a labelled section; assets with no category land in
// "Uncategorized". Section headers are drop targets so you can drag cards in.
function renderGroupedGrid(assets, kind, total) {
  const grid = $("#grid"); const empty = $("#emptyState");
  _vAssets = []; _vRange = { start: -1, end: -1 };  // disable the virtual scroller
  _currentAssets = assets;
  grid.classList.remove("grouped");
  if (!assets.length) { renderGrid(assets, total); return; }
  empty.classList.add("hidden");
  grid.classList.add("grouped");
  bindGridDragScroll();   // scroll to off-screen categories while dragging a tile

  // "All assets" (no kind) groups by the four asset TYPES — one section each for
  // Models / Textures / HDRIs / Materials. A plain type view instead groups by
  // that type's categories, with Unclassified pinned to the top.
  const isAll = !kind;
  let sections;
  if (isAll) {
    sections = TYPE_KINDS
      .map((k) => ({
        cat: { name: KIND_LABELS[k], icon: "" }, typeKind: k,
        items: assets.filter((a) => a.kind === k),
      }))
      .filter((s) => s.items.length);
  } else {
    const cats = allCategories.filter((c) => (c.kind || "") === kind);
    const catNames = new Set(cats.map((c) => c.name));
    sections = cats.map((c) => ({
      cat: c, items: assets.filter((a) => (a.categories || []).includes(c.name)),
    }));
    const uncategorized = assets.filter(
      (a) => !(a.categories || []).some((n) => catNames.has(n)));
    if (uncategorized.length)
      sections.unshift({ cat: { name: "Unclassified", icon: "📂" }, items: uncategorized, uncat: true });
  }

  const ordered = [];   // assets in card-display order (section by section)
  const secOf = [];     // section key per ordered[] entry — shift-range stays within one
  let secKey = 0;
  const frag = document.createDocumentFragment();
  for (const s of sections) {
    // Empty named categories still render — as a labelled drop zone — so a tile
    // can be dragged into a category that has no assets yet. (Uncategorized is
    // only ever added when it has items, so it's never shown empty.)
    const section = document.createElement("div");
    const sectionKey = s.typeKind
      ? `type:${s.typeKind}`
      : `cat:${kind || "all"}:${s.uncat ? "__uncat__" : s.cat.name}`;
    const collapsed = sectionIsCollapsed(sectionKey);
    section.className = "grid-section" + (s.items.length ? "" : " is-empty")
      + (s.typeKind ? " type-section" : "") + (collapsed ? " collapsed" : "");
    const head = document.createElement("div");
    head.className = "section-head" + (s.uncat ? " uncat" : "") + (s.typeKind ? " type-head" : "");
    const ico = s.typeKind
      ? `<span class="kind-dot" style="background:${KIND_COLORS[s.typeKind]}"></span>`
      : `<span class="section-ico">${esc(s.cat.icon || "")}</span>`;
    head.innerHTML =
      ico +
      `<span class="section-name">${esc(s.cat.name)}</span>` +
      `<span class="section-count">${s.items.length}</span>`;
    head.prepend(sectionCollapseButton(sectionKey, s.cat.name));
    // In "All assets", a type header drills into that type's category view.
    if (s.typeKind) {
      head.title = `Open ${s.cat.name}`;
      head.onclick = () => {
        resetFilter();
        state.filter.kind = s.typeKind;
        state.collapsed = new Set(TYPE_KINDS.filter((k) => k !== s.typeKind));
        persistCollapsed();
        refresh();
      };
    }
    section.appendChild(head);
    const sgrid = document.createElement("div");
    sgrid.className = "section-grid";
    if (!collapsed && s.items.length) {
      for (const a of s.items) {
        const di = ordered.length;
        ordered.push(a); secOf.push(secKey);
        const card = buildCard(a, di);
        // srcCat: only category sections carry a drag-from-category origin.
        card.dataset.srcCat = (s.uncat || s.typeKind) ? "" : s.cat.name;
        sgrid.appendChild(card);
      }
    } else if (!collapsed) {
      const hint = document.createElement("div");
      hint.className = "section-empty";
      hint.textContent = "Drag or right-click a tile here to add it";
      sgrid.appendChild(hint);
    }
    section.appendChild(sgrid);

    // Category sections are drop targets — dragging a tile onto another category
    // MOVES it (added to target, removed from origin). Type sections in the
    // All-assets view are just groupings, not drop targets.
    if (!s.typeKind) {
      const targetCat = s.uncat ? "" : s.cat.name;
      section.title = s.uncat
        ? "Drop a tile here to remove it from its category"
        : `Drop a tile here to move it into ${s.cat.name}`;
      section.addEventListener("dragover", (e) => {
        e.preventDefault(); e.dataTransfer.dropEffect = "move";
        section.classList.add("drop-over");
      });
      section.addEventListener("dragleave", (e) => {
        if (!section.contains(e.relatedTarget)) section.classList.remove("drop-over");
      });
      section.addEventListener("drop", async (e) => {
        e.preventDefault(); section.classList.remove("drop-over");
        const id = e.dataTransfer.getData("text/x-hangar-asset-id");
        const srcCat = e.dataTransfer.getData("text/x-hangar-src-cat") || "";
        if (!id || srcCat === targetCat) return;     // dropped back where it was
        if (targetCat) await post(`assets/${id}/category`, { category: targetCat, add: true });
        if (srcCat) await post(`assets/${id}/category`, { category: srcCat, add: false });
        toast(targetCat
          ? `Moved to ${s.cat.icon || ""} ${s.cat.name}`.trim()
          : "Removed from category", "success");
        refresh(); loadState();
      });
    }
    frag.appendChild(section);
    secKey++;
  }

  // Inline "new category" affordance — only in a per-type view (categories are
  // type-scoped; the All-assets view groups by type, not category).
  if (!isAll) {
    const adder = document.createElement("button");
    adder.className = "section-add";
    adder.innerHTML = `<span class="sa-plus">＋</span> New ${esc(KIND_LABELS[kind] || kind)} category`;
    adder.onclick = async () => { if (await promptNewCategory(kind)) refresh(); };
    frag.appendChild(adder);
  }

  _currentAssets = ordered;
  _displaySections = secOf;
  grid.replaceChildren(frag);
  grid.scrollTop = 0;
}

// Grouped view for a library folder: one section per immediate parent directory.
function renderGroupedByFolder(assets, libraryPath, opts = {}) {
  const grid = $("#grid"); const empty = $("#emptyState");
  _vAssets = []; _vRange = { start: -1, end: -1 };
  _currentAssets = assets;
  grid.classList.remove("grouped");
  if (!assets.length) { renderGrid(assets, 0); return; }
  empty.classList.add("hidden");
  grid.classList.add("grouped");
  bindGridDragScroll();

  const libRoot = (libraryPath || "").replace(/[\\/]+$/, "");

  // Group by full parent directory path; compute display label relative to root.
  const groups = new Map();  // fullParentPath → {label, items[]}
  for (const a of assets) {
    const parentDir = (a.path || "").replace(/[\\/][^\\/]+$/, "");
    let label = parentDir;
    let subtitle = "";
    if (opts.parentOnly) {
      label = baseName(parentDir) || parentDir || "(root)";
      subtitle = parentDir;
    }
    if (libRoot && parentDir.toLowerCase().startsWith(libRoot.toLowerCase())) {
      label = parentDir.slice(libRoot.length).replace(/^[\\/]+/, "") || "(root)";
    }
    if (!groups.has(parentDir)) groups.set(parentDir, {
      label, subtitle, key: `folder:${parentDir}`, items: []
    });
    groups.get(parentDir).items.push(a);
  }

  const sections = [...groups.values()]
    .sort((a, b) => a.label.localeCompare(b.label, undefined, { sensitivity: "base" }));

  const ordered = [];
  const secOf = [];
  let secKey = 0;
  const frag = document.createDocumentFragment();
  for (const s of sections) {
    const section = document.createElement("div");
    const collapsed = sectionIsCollapsed(s.key);
    section.className = "grid-section" + (collapsed ? " collapsed" : "");
    const head = document.createElement("div");
    head.className = "section-head";
    head.innerHTML =
      `<span class="section-ico">📁</span>` +
      `<span class="section-name">${esc(s.label)}</span>` +
      (s.subtitle ? `<span class="section-subtitle">${esc(s.subtitle)}</span>` : "") +
      `<span class="section-count">${s.items.length}</span>`;
    head.prepend(sectionCollapseButton(s.key, s.label));
    section.appendChild(head);
    const sgrid = document.createElement("div");
    sgrid.className = "section-grid";
    if (!collapsed) {
      for (const a of s.items) {
        const di = ordered.length;
        ordered.push(a); secOf.push(secKey);
        sgrid.appendChild(buildCard(a, di));
      }
    }
    section.appendChild(sgrid);
    frag.appendChild(section);
    secKey++;
  }

  _currentAssets = ordered;
  _displaySections = secOf;
  grid.replaceChildren(frag);
  grid.scrollTop = 0;
}

// Duplicates view: one section per shared file name, each holding every copy so
// you can compare and clean them up. Backend already limits the set to names
// that occur more than once.
function renderGroupedByName(assets) {
  const grid = $("#grid"); const empty = $("#emptyState");
  _vAssets = []; _vRange = { start: -1, end: -1 };
  _currentAssets = assets;
  grid.classList.remove("grouped");
  if (!assets.length) { renderGrid(assets, 0); return; }
  empty.classList.add("hidden");
  grid.classList.add("grouped");
  bindGridDragScroll();

  const groups = new Map();  // lowercased file name → {label, key, items[]}
  for (const a of assets) {
    const fname = `${a.name}${a.ext}`;
    const key = fname.toLowerCase();
    if (!groups.has(key)) groups.set(key, { label: fname, key: `dup:${key}`, items: [] });
    groups.get(key).items.push(a);
  }

  const sections = [...groups.values()]
    .sort((a, b) => a.label.localeCompare(b.label, undefined, { sensitivity: "base" }));

  const ordered = [];
  const secOf = [];
  let secKey = 0;
  const frag = document.createDocumentFragment();
  for (const s of sections) {
    const section = document.createElement("div");
    const collapsed = sectionIsCollapsed(s.key);
    section.className = "grid-section" + (collapsed ? " collapsed" : "");
    const head = document.createElement("div");
    head.className = "section-head";
    head.innerHTML =
      `<span class="section-ico">⧉</span>` +
      `<span class="section-name">${esc(s.label)}</span>` +
      `<span class="section-count">${s.items.length}</span>`;
    head.prepend(sectionCollapseButton(s.key, s.label));
    section.appendChild(head);
    const sgrid = document.createElement("div");
    sgrid.className = "section-grid";
    if (!collapsed) {
      for (const a of s.items) {
        const di = ordered.length;
        ordered.push(a); secOf.push(secKey);
        sgrid.appendChild(buildCard(a, di));
      }
    }
    section.appendChild(sgrid);
    frag.appendChild(section);
    secKey++;
  }

  _currentAssets = ordered;
  _displaySections = secOf;
  grid.replaceChildren(frag);
  grid.scrollTop = 0;
}

function updateActiveLabel(total) {
  let label = state.filter.duplicates ? "⧉ Duplicates"
    : state.filter.favorite ? "Favorites"
    : state.filter.tag ? `#${state.filter.tag}`
    : state.filter.category ? state.filter.category
    : state.filter.folder ? `📁 ${baseName(state.filter.folder)}`
    : state.filter.collection ? state.filter.collection
    : state.filter.ext ? (() => {
        const grp = MODEL_EXT_GROUPS.find(g => g.exts.join(",") === state.filter.ext);
        return grp ? grp.label : state.filter.ext;
      })()
    : KIND_LABELS[state.filter.kind || "all"];
  $("#activeFilter").textContent = `${label} · ${total}`;
}

function buildCard(a, i) {
  const card = document.createElement("div");
  card.className = "card" + (a.favorite ? " is-fav" : "")
    + (selection.has(a.id) ? " is-selected" : "");
  card.dataset.id = a.id;
  const color = KIND_COLORS[a.kind] || "var(--mute)";
  const ext = a.ext.replace(".", "").toUpperCase();
  const tagDots = (a.tags || []).slice(0, 4)
    .map((t) => `<span class="tdot" style="background:${safeColor(t.color)}"></span>`).join("");
  // Texture sets collapse many maps into one tile — show how many it represents.
  const setBadge = (a.set_count > 1)
    ? `<span class="set-badge" title="${a.set_count} texture maps in this set">⛃ ${a.set_count} maps</span>`
    : "";
  // Immediate parent folder name, so a tile shows where on disk it lives.
  const parts = (a.path || "").replace(/[\\/]+$/, "").split(/[\\/]/);
  const folder = parts.length > 1 ? parts[parts.length - 2] : "";
  const folderLine = folder
    ? `<div class="card-folder" title="${esc(a.path || "")}">🗀 ${esc(folder)}</div>`
    : "";
  card.innerHTML = `
    <div class="card-thumb">
      <span class="kind-stripe" style="background:${color}"></span>
      <span class="fav-pin">●</span>
      ${setBadge}
      <div class="badge-tile">
        <span class="badge-ext" style="color:${color}">${esc(ext)}</span>
      </div>
    </div>
    <div class="card-meta">
      <div class="card-name" title="${esc(a.name)}">${esc(a.name)}</div>
      ${folderLine}
      <div class="card-line">
        <span class="card-ext" style="color:${color}">${esc(ext)}</span>
        <span>·</span><span>${fmtSize(a.size)}</span>
        <span class="card-tags">${tagDots}</span>
      </div>
    </div>`;
  const tile = card.querySelector(".badge-tile");
  const img = new Image();
  img.onload = () => { tile.replaceWith(img); };
  img.onerror = () => { /* keep placeholder tile */ };
  img.src = thumbUrl(a.id);
  img.alt = a.name;
  // Without this the browser drags the thumbnail picture itself instead of the
  // card, so the card's dragstart payload never reaches a category drop target.
  img.draggable = false;
  // Ctrl/Cmd-click toggles selection; Shift-click extends to a range;
  // once a selection is active a plain click keeps building it.
  // Otherwise a click opens the detail drawer.
  card.onclick = (e) => {
    if (e.shiftKey && _lastSelectedIdx >= 0) {
      e.preventDefault();
      rangeSelect(i);
      _lastSelectedIdx = i;
    } else if (e.ctrlKey || e.metaKey || selection.size > 0) {
      e.preventDefault();
      toggleSelect(a.id, card, i);
    } else {
      openDrawer(a.id, i);
    }
  };

  // Drag support — any asset can be dropped onto a sidebar category or
  // collection to file it there (works for models, textures, HDRIs, materials).
  card.draggable = true;
  card.addEventListener("dragstart", (e) => {
    e.dataTransfer.setData("text/x-hangar-asset-id", String(a.id));
    // srcCat is set by renderGroupedGrid; empty in the flat/sidebar views.
    e.dataTransfer.setData("text/x-hangar-src-cat", card.dataset.srcCat || "");
    e.dataTransfer.effectAllowed = "copyMove";
    card.classList.add("dragging");
  });
  card.addEventListener("dragend", () => card.classList.remove("dragging"));

  // Right-click → move this asset into a category (a reliable alternative to
  // dragging, and the only way to file into a category that has no tiles yet).
  card.addEventListener("contextmenu", (e) => {
    e.preventDefault();
    // With a multi-selection active, right-click acts on the whole selection.
    if (selection.size > 0) showBatchMenu(e.clientX, e.clientY);
    else showCategoryMenu(e.clientX, e.clientY, a);
  });
  // Hover dwell → floating auto-rotating 3D quick-preview (viewable models only).
  bindHoverPreview(card, a);
  return card;
}

// ---- right-click "move to category" menu ----------------------------------
let _ctxMenuEl = null;
function _onCtxOutside(e) { if (_ctxMenuEl && !_ctxMenuEl.contains(e.target)) closeCtxMenu(); }
function _onCtxKey(e) { if (e.key === "Escape") closeCtxMenu(); }
function closeCtxMenu() {
  if (!_ctxMenuEl) return;
  _ctxMenuEl.remove(); _ctxMenuEl = null;
  document.removeEventListener("mousedown", _onCtxOutside, true);
  document.removeEventListener("keydown", _onCtxKey, true);
}

// Place a built menu at (x, y), nudged back on-screen if it would overflow, and
// wire up dismiss-on-outside-click / Escape. Shared by every right-click menu.
function _mountCtxMenu(menu, x, y) {
  menu.style.visibility = "hidden";
  document.body.appendChild(menu);
  const r = menu.getBoundingClientRect();
  menu.style.left = Math.max(8, Math.min(x, window.innerWidth - r.width - 8)) + "px";
  menu.style.top = Math.max(8, Math.min(y, window.innerHeight - r.height - 8)) + "px";
  menu.style.visibility = "visible";
  _ctxMenuEl = menu;
  setTimeout(() => {
    document.addEventListener("mousedown", _onCtxOutside, true);
    document.addEventListener("keydown", _onCtxKey, true);
  }, 0);
}

function showCategoryMenu(x, y, a) {
  closeCtxMenu();
  // Categories that apply to this asset: its own type, plus shared ones (kind "").
  const cats = allCategories.filter((c) => !c.kind || c.kind === a.kind);
  const current = new Set(a.categories || []);
  const menu = document.createElement("div");
  menu.className = "ctx-menu";

  const title = document.createElement("div");
  title.className = "ctx-title";
  title.textContent = "Move to category";
  menu.appendChild(title);

  if (!cats.length) {
    const none = document.createElement("div");
    none.className = "ctx-empty";
    none.textContent = "No categories for this type yet";
    menu.appendChild(none);
  }
  for (const c of cats) {
    const item = document.createElement("button");
    item.className = "ctx-item" + (current.has(c.name) ? " on" : "");
    item.innerHTML =
      `<span class="ctx-ico">${esc(c.icon || "")}</span>` +
      `<span class="ctx-name">${esc(c.name)}</span>` +
      (current.has(c.name) ? `<span class="ctx-check">✓</span>` : "");
    item.onclick = async (e) => {
      e.stopPropagation(); closeCtxMenu();
      await moveAssetToCategory(a, c.name, c);
    };
    menu.appendChild(item);
  }

  const sep = document.createElement("div"); sep.className = "ctx-sep";
  menu.appendChild(sep);
  if (current.size) {
    const rm = document.createElement("button");
    rm.className = "ctx-item ctx-danger";
    rm.innerHTML = `<span class="ctx-ico">📂</span><span class="ctx-name">Remove from category</span>`;
    rm.onclick = async (e) => { e.stopPropagation(); closeCtxMenu(); await uncategorizeAsset(a); };
    menu.appendChild(rm);
  }
  const mk = document.createElement("button");
  mk.className = "ctx-item";
  mk.innerHTML = `<span class="ctx-ico">＋</span><span class="ctx-name">New category…</span>`;
  mk.onclick = async (e) => {
    e.stopPropagation(); closeCtxMenu();
    const name = (prompt("New category name:") || "").trim();
    if (!name) return;
    const icon = prompt("Icon (emoji, optional — press Cancel to skip):") || "";
    await post("categories", { name, icon, kind: a.kind });
    await moveAssetToCategory(a, name);
  };
  menu.appendChild(mk);

  // ---- asset actions (open / reveal / drop cached preview) ----
  const sep2 = document.createElement("div"); sep2.className = "ctx-sep";
  menu.appendChild(sep2);

  if (a.kind === "model") {
    const openBl = document.createElement("button");
    openBl.className = "ctx-item";
    openBl.innerHTML = `<span class="ctx-ico">🔶</span><span class="ctx-name">Open in Blender</span>`;
    openBl.onclick = async (e) => { e.stopPropagation(); closeCtxMenu(); await openAssetInBlender(a); };
    menu.appendChild(openBl);
  }

  const reveal = document.createElement("button");
  reveal.className = "ctx-item";
  reveal.innerHTML = `<span class="ctx-ico">📁</span><span class="ctx-name">Reveal in file manager</span>`;
  reveal.onclick = async (e) => { e.stopPropagation(); closeCtxMenu(); await revealAsset(a); };
  menu.appendChild(reveal);

  const renameItem = document.createElement("button");
  renameItem.className = "ctx-item";
  renameItem.innerHTML = `<span class="ctx-ico">✏</span><span class="ctx-name">Rename file</span>`;
  renameItem.onclick = (e) => { e.stopPropagation(); closeCtxMenu(); renameAsset(a, -1); };
  menu.appendChild(renameItem);

  // Only Blender-renderable models can have a fresh preview rendered; textures
  // and HDRIs already carry their own image. Mirrors the multi-select batch menu.
  if (a.kind === "model" && appCaps.renderExts.includes(a.ext)) {
    const regen = document.createElement("button");
    regen.className = "ctx-item";
    regen.innerHTML = `<span class="ctx-ico">🖼</span><span class="ctx-name">Regenerate preview</span>`;
    regen.onclick = async (e) => { e.stopPropagation(); closeCtxMenu(); await regenerateSelectedPreviews([a]); };
    menu.appendChild(regen);
  }

  const delPrev = document.createElement("button");
  delPrev.className = "ctx-item";
  delPrev.innerHTML = `<span class="ctx-ico">🗑</span><span class="ctx-name">Delete preview</span>`;
  delPrev.onclick = async (e) => { e.stopPropagation(); closeCtxMenu(); await clearAssetPreview(a); };
  menu.appendChild(delPrev);

  if (a.ext === ".blend") {
    const markSep = document.createElement("div"); markSep.className = "ctx-sep";
    menu.appendChild(markSep);
    const markObj = document.createElement("button");
    markObj.className = "ctx-item";
    markObj.innerHTML = `<span class="ctx-ico">📦</span><span class="ctx-name">Mark objects as assets</span>`;
    markObj.onclick = async (e) => {
      e.stopPropagation(); closeCtxMenu();
      toast("Marking objects as assets — Blender is running, please wait…");
      const r = await post(`assets/${a.id}/mark-assets`, { target: "objects" });
      if (r.ok) toast(`Marked ${r.marked || 0} objects. Preview gallery ready.`, "success");
      else toast(r.error || "Marking failed.", "error");
      if (isDrawerOpen() && drawerAssetId === a.id) renderBlendInfo(a);
    };
    menu.appendChild(markObj);
    const markCol = document.createElement("button");
    markCol.className = "ctx-item";
    markCol.innerHTML = `<span class="ctx-ico">📦</span><span class="ctx-name">Mark collections as assets</span>`;
    markCol.onclick = async (e) => {
      e.stopPropagation(); closeCtxMenu();
      toast("Marking collections as assets — Blender is running, please wait…");
      const r = await post(`assets/${a.id}/mark-assets`, { target: "collections" });
      if (r.ok) toast(`Marked ${r.marked || 0} collections. Preview gallery ready.`, "success");
      else toast(r.error || "Marking failed.", "error");
      if (isDrawerOpen() && drawerAssetId === a.id) renderBlendInfo(a);
    };
    menu.appendChild(markCol);
    const unmarkCol = document.createElement("button");
    unmarkCol.className = "ctx-item";
    unmarkCol.innerHTML = `<span class="ctx-ico">x</span><span class="ctx-name">Unmark collections as assets</span>`;
    unmarkCol.onclick = async (e) => {
      e.stopPropagation(); closeCtxMenu();
      if (!confirm("Remove the Asset Browser mark from collections in this .blend file? The file will be saved.")) return;
      toast("Unmarking collections - Blender is running, please wait...");
      const r = await post(`assets/${a.id}/unmark-assets`, { target: "collections" });
      if (r.ok) toast(`Unmarked ${r.unmarked || 0} collections.`, "success");
      else toast(r.error || "Unmarking failed.", "error");
      if (isDrawerOpen() && drawerAssetId === a.id) renderBlendInfo(a);
    };
    menu.appendChild(unmarkCol);
  }

  _mountCtxMenu(menu, x, y);
}

// Open the asset's file with the OS default application (double-click behaviour).
async function openAssetFile(a) {
  let r;
  try { r = await post(`assets/${a.id}/open-file`); }
  catch (_) { r = null; }
  if (!r || !r.ok) toast((r && r.error) || "Couldn't open the file.", "error");
}

// Rename an asset's file on disk. Prompts for a new base name (extension kept),
// then updates the drawer header, path, and grid in place.
async function renameAsset(a, idx) {
  const next = prompt(`Rename file (the ${a.ext} extension is kept):`, a.name);
  if (next === null) return;                       // cancelled
  const base = next.trim();
  if (!base || base === a.name) return;            // empty or unchanged
  let r;
  try { r = await post(`assets/${a.id}/rename`, { name: base }); }
  catch (_) { r = null; }
  if (!r || !r.ok) { toast((r && r.error) || "Couldn't rename the file.", "error"); return; }
  a.name = r.name; a.path = r.path;                // mutate the in-memory asset
  const nameEl = $("#dName");
  if (nameEl) nameEl.textContent = r.name;
  const pathEl = $("#dPath");
  if (pathEl) { pathEl.textContent = r.path; pathEl.title = `Open this file — ${r.path}`; }
  toast("File renamed.", "success");
  refresh();                                       // reflect the new name in the grid
}

// Fetch + render the .blend info panel: marked-asset gallery and missing textures.
async function renderBlendInfo(a) {
  const el = $("#dBlend");
  if (!el) return;
  el.innerHTML = `<div class="d-blend-loading">Loading .blend info…</div>`;
  let info;
  try { info = await api(`assets/${a.id}/blend-info`); }
  catch (_) { el.innerHTML = ""; return; }
  if (!info || info.error) { el.innerHTML = ""; return; }

  let html = "";

  // ── Preview source ─────────────────────────────────────────────────────────
  // Tell the user what the big preview is sourced from: the .blend's own 128px
  // embedded thumbnail, or a full Hangar render (and which engine — EEVEE, or
  // CYCLES after a GPU-crash CPU fallback).
  if (info.preview && info.preview.label) {
    const isRender = info.preview.source === "render";
    html += `<div class="d-preview-source" title="What this tile's image was generated from">`;
    html += `<span class="d-preview-source-ico">${isRender ? "🎬" : "🖼"}</span>`;
    html += `<span class="d-preview-source-label">Preview: ${esc(info.preview.label)}</span>`;
    html += `</div>`;
  }

  // ── Marked assets gallery ──────────────────────────────────────────────────
  if (info.assets && info.assets.length) {
    html += `<div class="d-section-label">Marked assets (${info.assets.length})</div>`;
    html += `<div class="d-asset-gallery">`;
    for (const asset of info.assets) {
      // Request the PNG by the exact manifest key (preview_name), which can differ
      // from the display name when the server matched via the normalized fallback.
      const safe = encodeURIComponent(asset.preview_name || asset.name);
      const thumbUrl = asset.has_thumb
        ? `/api/assets/${a.id}/blend-asset-thumb?name=${safe}&v=${asset.thumb_mtime || 0}`
        : null;
      const source = asset.preview_source || (asset.has_thumb
        ? "Hangar rendered asset preview cache"
        : "No rendered asset preview; showing type badge");
      const ownTip = asset.has_individual
        ? `\nHas its own ${esc(asset.name)}.blend in the library`
        : "";
      html += `<div class="d-asset-tile" title="${esc(asset.kind)}: ${esc(asset.name)}\n${esc(source)}${ownTip}">`;
      if (asset.has_individual) {
        html += `<span class="d-asset-tick" title="Saved as its own .blend file">✓</span>`;
      }
      if (thumbUrl) {
        html += `<img class="d-asset-img" src="${thumbUrl}" alt="${esc(asset.name)}" loading="lazy">`;
      } else {
        html += `<div class="d-asset-noimg"><span>${esc(asset.kind[0])}</span></div>`;
      }
      html += `<div class="d-asset-name">${esc(asset.name)}</div>`;
      if (asset.has_individual) {
        html += `<div class="d-asset-have">Own .blend ✓</div>`;
      } else {
        html += `<button class="d-extract-btn" data-name="${esc(asset.name)}" data-kind="${esc(asset.kind)}" title="Save “${esc(asset.name)}” to its own .blend file">Extract</button>`;
      }
      html += `</div>`;
    }
    html += `</div>`;
    const hasCollections = info.assets.some(x => x.kind === "Collection");
    html += `<div class="d-mark-actions">`;
    html += `<button class="d-mark-btn" id="dMarkObjects">Mark objects as assets</button>`;
    if (hasCollections) {
      html += `<button class="d-mark-btn d-danger-btn" id="dUnmarkCollections">Unmark collections as assets</button>`;
    }
    html += `<button class="d-mark-btn d-danger-btn" id="dUnmarkAll">Unmark all asset marks</button>`;
    html += `</div>`;
  } else if (info.count === 0) {
    html += `<div class="d-section-label">Marked assets</div>`;
    html += `<div class="d-blend-note">Nothing in this file is marked as an asset yet.</div>`;
    html += `<div class="d-mark-actions">`;
    html += `<button class="d-mark-btn" id="dMarkObjects">Mark objects as assets</button>`;
    html += `<button class="d-mark-btn" id="dMarkCollections">Mark collections as assets</button>`;
    html += `</div>`;
  }

  // ── Generate previews button ───────────────────────────────────────────────
  const needPreview = (info.assets || []).filter(x => !x.has_thumb).length;
  if (needPreview) {
    const label = needPreview === (info.assets || []).length
      ? `Generate previews (${needPreview})`
      : `Generate missing previews (${needPreview})`;
    html += `<button class="d-gen-previews-btn" id="dGenPreviews">${label}</button>`;
  }
  // Status line for long-running Blender jobs (marking / rendering).
  html += `<div class="d-blend-status" id="dBlendStatus" hidden></div>`;

  // ── Missing textures ───────────────────────────────────────────────────────
  // `missing_textures` is an array whenever the .blend parsed; show the empty
  // case explicitly ("all found") so the panel never just silently omits the
  // section and leaves the user unsure whether it ran.
  if (Array.isArray(info.missing_textures)) {
    if (info.missing_textures.length) {
      html += `<div class="d-section-label d-missing-label">Missing textures (${info.missing_textures.length})</div>`;
      html += `<div class="d-missing-textures">`;
      for (const t of info.missing_textures) {
        html += `<div class="d-missing-tex" title="${esc(t.path)}">`;
        html += `<span class="d-missing-ico">&#9724;</span>`;
        html += `<span class="d-missing-name">${esc(t.name)}</span>`;
        html += `<span class="d-missing-path">${esc(t.path)}</span>`;
        html += `</div>`;
      }
      html += `</div>`;
    } else {
      html += `<div class="d-section-label">Textures</div>`;
      html += `<div class="d-blend-note">✓ All referenced textures found on disk.</div>`;
    }
  }

  el.innerHTML = html;

  const status = $("#dBlendStatus");
  // Show an animated status while a Blender job runs; disable all buttons so the
  // user can't kick off a second job. `verb` is e.g. "Marking objects".
  const setBusy = (busy, verb) => {
    el.querySelectorAll(".d-mark-btn, .d-gen-previews-btn, .d-extract-btn").forEach(b => { b.disabled = busy; });
    if (!status) return;
    if (busy) {
      status.hidden = false;
      status.className = "d-blend-status d-blend-status-busy";
      status.innerHTML = `<span class="d-spinner"></span>${esc(verb)}… Blender is running, this can take a minute.`;
    }
  };
  const setDone = (msg, ok) => {
    if (!status) return;
    status.hidden = false;
    status.className = "d-blend-status " + (ok ? "d-blend-status-ok" : "d-blend-status-err");
    status.textContent = msg;
  };

  const markBtnObj = $("#dMarkObjects");
  const markBtnCol = $("#dMarkCollections");
  const unmarkBtnCol = $("#dUnmarkCollections");
  const unmarkBtnAll = $("#dUnmarkAll");
  const runMark = async (target, verb) => {
    setBusy(true, verb);
    let r;
    try { r = await post(`assets/${a.id}/mark-assets`, { target }); }
    catch (_) { r = null; }
    if (r && r.ok) {
      setDone(`Marked ${r.marked || 0} ${target}. Refreshing…`, true);
      renderBlendInfo(a);
    } else {
      setBusy(false);
      setDone((r && r.error) || "Marking failed — check last_render.log.", false);
    }
  };
  const runUnmark = async (target, label) => {
    if (!confirm(`Remove the Asset Browser mark from ${label} in this .blend file? The file will be saved.`)) return;
    setBusy(true, `Unmarking ${label}`);
    let r;
    try { r = await post(`assets/${a.id}/unmark-assets`, { target }); }
    catch (_) { r = null; }
    if (r && r.ok) {
      setDone(`Unmarked ${r.unmarked || 0} ${label}. Refreshing...`, true);
      renderBlendInfo(a);
    } else {
      setBusy(false);
      setDone((r && r.error) || "Unmarking failed - check last_render.log.", false);
    }
  };
  if (markBtnObj) markBtnObj.onclick = () => runMark("objects", "Marking objects");
  if (markBtnCol) markBtnCol.onclick = () => runMark("collections", "Marking collections");
  if (unmarkBtnCol) unmarkBtnCol.onclick = () => runUnmark("collections", "collections");
  if (unmarkBtnAll) unmarkBtnAll.onclick = () => runUnmark("all", "all asset marks");

  // Extract a marked datablock to its own .blend file. On success the file is
  // indexed and the tile flips to the green "own .blend" state.
  el.querySelectorAll(".d-extract-btn").forEach(btn => {
    btn.onclick = async () => {
      const name = btn.dataset.name, kind = btn.dataset.kind;
      setBusy(true, `Extracting ${name}`);
      let r;
      try { r = await post(`assets/${a.id}/extract-asset`, { name, kind }); }
      catch (_) { r = null; }
      if (r && r.ok) {
        setDone(`Saved ${r.extracted_name || name}.blend. Refreshing…`, true);
        refresh();                 // surface the new file in the grid
        renderBlendInfo(a);        // re-fetch so this tile gets its green tick
      } else {
        setBusy(false);
        setDone((r && r.error) || "Extract failed — check last_render.log.", false);
      }
    };
  });

  const genBtn = $("#dGenPreviews");
  if (genBtn) {
    genBtn.onclick = async () => {
      setBusy(true, `Rendering ${info.assets.length} preview${info.assets.length === 1 ? "" : "s"}`);
      let r;
      try { r = await post(`assets/${a.id}/generate-asset-previews`); }
      catch (_) { r = null; }
      if (r && r.ok) {
        setDone("Previews ready. Refreshing…", true);
        renderBlendInfo(a);
      } else {
        setBusy(false);
        setDone((r && r.error) || "Preview render failed — check last_render.log.", false);
      }
    };
  }
}

// Launch Blender on this asset (.blend opens directly; other models import into
// a fresh Blender). Distinct from "Send to Blender", which needs the add-on
// running in an already-open Blender.
async function openAssetInBlender(a) {
  let r;
  try { r = await post(`assets/${a.id}/open-blender`); }
  catch (_) { r = null; }
  if (!r || !r.ok) {
    toast((r && r.error) || "Couldn't open in Blender.", "error");
    return;
  }
  toast(`Opening ${a.name} in Blender…`, "success");
}

// Open the OS file manager with this asset's file selected.
async function revealAsset(a) {
  let r;
  try { r = await post(`assets/${a.id}/reveal`); }
  catch (_) { r = null; }
  if (!r || !r.ok) toast((r && r.error) || "Couldn't open the file manager.", "error");
}

// Drop the cached thumbnail and let Hangar re-bake it from source. For a .blend
// that re-reads the preview embedded in the file — the fix for a tile that
// cached blank/stale. Repaints the on-screen tile with the fresh image.
async function clearAssetPreview(a) {
  let r;
  try { r = await post(`assets/${a.id}/preview/clear`); }
  catch (_) { r = null; }
  if (!r || !r.ok) {
    toast((r && r.error) || "Couldn't delete the preview.", "error");
    return;
  }
  thumbBust[a.id] = Date.now();
  const ca = currentAssets.find((x) => x.id === a.id);
  if (ca) ca.has_thumb = !!r.rebaked;
  const card = $(`#grid .card[data-id="${a.id}"]`);
  if (card) {
    const cardImg = card.querySelector("img");
    if (cardImg) {
      cardImg.src = thumbUrl(a.id);                 // live <img>: just re-fetch
    } else if (r.rebaked) {                          // placeholder tile: swap one in
      const tile = card.querySelector(".badge-tile");
      if (tile) {
        const img = new Image();
        img.draggable = false; img.alt = a.name;
        img.onload = () => { if (tile.isConnected) tile.replaceWith(img); };
        img.src = thumbUrl(a.id);
      }
    }
  }
  toast(r.rebaked ? "Preview refreshed from source." : "Preview deleted.", "success");
}

// ---- right-click batch menu (shown when a multi-selection is active) -------
function showBatchMenu(x, y) {
  closeCtxMenu();
  const ids = [...selection];
  const menu = document.createElement("div");
  menu.className = "ctx-menu";

  const title = document.createElement("div");
  title.className = "ctx-title";
  title.textContent = `${ids.length} selected`;
  menu.appendChild(title);

  // Only Blender-renderable models can have a preview regenerated; textures and
  // HDRIs already carry their own image, so they're filtered out of the count.
  const renderable = ids
    .map((id) => _currentAssets.find((a) => a.id === id))
    .filter((a) => a && a.kind === "model" && appCaps.renderExts.includes(a.ext));

  if (!renderable.length) {
    const none = document.createElement("div");
    none.className = "ctx-empty";
    none.textContent = "No renderable models selected";
    menu.appendChild(none);
  } else {
    const regen = document.createElement("button");
    regen.className = "ctx-item";
    regen.innerHTML =
      `<span class="ctx-ico">🖼</span>` +
      `<span class="ctx-name">Regenerate previews (${renderable.length})</span>`;
    regen.onclick = async (e) => {
      e.stopPropagation(); closeCtxMenu();
      await regenerateSelectedPreviews(renderable);
    };
    menu.appendChild(regen);
  }

  const sep = document.createElement("div"); sep.className = "ctx-sep";
  menu.appendChild(sep);
  const addCat = document.createElement("button");
  addCat.className = "ctx-item";
  addCat.innerHTML = `<span class="ctx-ico">+</span><span class="ctx-name">Add to category…</span>`;
  addCat.onclick = (e) => { e.stopPropagation(); closeCtxMenu(); showBatchCategoryMenu(true, x, y); };
  menu.appendChild(addCat);
  const removeCat = document.createElement("button");
  removeCat.className = "ctx-item ctx-danger";
  removeCat.innerHTML = `<span class="ctx-ico">-</span><span class="ctx-name">Remove from category…</span>`;
  removeCat.onclick = (e) => { e.stopPropagation(); closeCtxMenu(); showBatchCategoryMenu(false, x, y); };
  menu.appendChild(removeCat);

  _mountCtxMenu(menu, x, y);
}

// Force a fresh full Blender render for each selected model — the manual escape
// hatch for .blend files whose embedded 128px thumbnail looks blurry. The work
// runs server-side in a background worker (ONE Blender at a time); we poll its
// status and drive the status-bar progress UI so the user sees live feedback.
// Firing it again while a run is going APPENDS to the queue (the total grows)
// rather than being rejected.
let _regenIds = new Set();   // every id queued in the current run, for the final repaint
async function regenerateSelectedPreviews(assets) {
  if (!appCaps.blenderReady) {
    toast("Blender not found — set its path first to regenerate previews.", "error");
    return;
  }
  const ids = assets.map((a) => a.id);
  let r;
  try { r = await post("assets/batch/render", { ids }); }
  catch (_) { r = null; }
  if (!r || !r.ok) {
    toast((r && r.error) || "Couldn't start preview regeneration.", "error");
    return;
  }
  ids.forEach((id) => _regenIds.add(id));
  toast(
    r.queued
      ? `Added ${ids.length} to the queue (${r.total} total)…`
      : `Regenerating ${ids.length} preview${ids.length === 1 ? "" : "s"}…`,
    "success");
  startRegenPolling();        // no-op if a poll is already running
}

function startRegenPolling() {
  if (state.regenTimer) return;
  $("#statusSummary").classList.add("hidden");
  $("#scanProgress").classList.remove("hidden");
  const tick = async () => {
    let s;
    try { s = await api("assets/batch/render/status"); } catch (_) { return; }
    $("#scanText").textContent = `Regenerating previews — ${s.done}/${s.total}`;
    $("#scanFill").style.width = (s.pct || 0) + "%";
    $("#scanPct").textContent = (s.pct || 0) + "%";
    if (s.current) {
      $("#scanFile").textContent = s.current.replace(/.*[\\/]/, "");
      $("#scanFile").title = s.current;
    } else {
      $("#scanFile").textContent = ""; $("#scanFile").title = "";
    }
    if (!s.running) {
      clearInterval(state.regenTimer); state.regenTimer = null;
      $("#scanProgress").classList.add("hidden");
      $("#statusSummary").classList.remove("hidden");
      // Bust caches so the freshly rendered tiles reload instead of showing the
      // browser-cached blurry version, then repaint the grid + open drawer.
      const ids = [..._regenIds];
      _regenIds = new Set();
      for (const id of ids) thumbBust[id] = Date.now();
      refresh();
      const a = _currentAssets.find((x) => x.id === drawerAssetId);
      if (a && ids.includes(a.id)) loadPreview(a);
      const ok = s.ok || 0, failed = s.failed || 0;
      toast(
        failed
          ? `Regenerated ${ok}, ${failed} failed${s.last_error ? " — " + s.last_error : ""}`
          : `Regenerated ${ok} preview${ok === 1 ? "" : "s"}`,
        failed ? "error" : "success");
    }
  };
  state.regenTimer = setInterval(tick, 400);
  tick();
}

// Move semantics: the asset ends up in exactly `name` among the categories that
// apply to its type — added to the target, removed from any sibling categories.
async function moveAssetToCategory(a, name, category = null) {
  const applicable = new Set(
    allCategories.filter((c) => !c.kind || c.kind === a.kind).map((c) => c.name));
  const targetCategory = category || allCategories.find((c) => c.name === name && (!c.kind || c.kind === a.kind));
  const dest = targetCategory ? destinationFolderForCategory(a, targetCategory) : null;
  if (dest) {
    let r;
    try { r = await post(`assets/${a.id}/move-to-folder`, { folder: dest.path }); }
    catch (_) { r = null; }
    if (!r || !r.ok) {
      toast((r && r.error) || `Couldn't move the file into ${dest.name}.`, "error");
      return;
    }
    a.path = r.path;
  }
  await post(`assets/${a.id}/category`, { category: name, add: true });
  for (const other of (a.categories || [])) {
    if (other !== name && applicable.has(other))
      await post(`assets/${a.id}/category`, { category: other, add: false });
  }
  a.categories = [name];
  toast(dest ? `Moved to ${name} folder` : `Moved to ${name}`, "success");
  if (drawerAssetId === a.id) renderDrawerCategoryEditor(a);
  refresh(); loadState();
}

// Physically move the asset's file into one of a category's folders, then file
// it under that category. Confirms first because it relocates a file on disk.
async function moveAssetToFolder(a, c, f) {
  const warn = a.ext === ".blend"
    ? "\n\n⚠ This is a .blend — if it references textures by relative path, moving it may break those links."
    : "";
  if (!confirm(`Move "${a.name}${a.ext}" into:\n${f.path}\n\nThis relocates the file on disk and files it under "${c.name}".${warn}`))
    return;
  let r;
  try { r = await post(`assets/${a.id}/move-to-folder`, { folder: f.path }); }
  catch (_) { r = null; }
  if (!r || !r.ok) { toast((r && r.error) || "Couldn't move the file.", "error"); return; }
  a.path = r.path;
  await moveAssetToCategory(a, c.name);   // file under the category (also refreshes)
  toast(`Moved into ${f.name}`, "success");
}

async function uncategorizeAsset(a) {
  for (const name of (a.categories || []))
    await post(`assets/${a.id}/category`, { category: name, add: false });
  a.categories = [];
  toast("Removed from category", "success");
  if (drawerAssetId === a.id) renderDrawerCategoryEditor(a);
  refresh(); loadState();
}

// ---- drag auto-scroll -----------------------------------------------------
// While dragging a tile, scroll the grid when the cursor nears its top/bottom
// edge, so a category section that's off-screen can still be reached as a drop
// target. Speed ramps up the closer the cursor gets to the edge.
const DRAG_SCROLL_ZONE = 80;   // px from an edge where auto-scroll kicks in
const DRAG_SCROLL_MAX = 22;    // px/frame at the very edge
let _dragScrollDir = 0;        // <0 up, >0 down, 0 idle
let _dragScrollRaf = 0;
let _dragScrollBound = false;

function _dragScrollStep() {
  const grid = $("#grid");
  if (_dragScrollDir && grid) {
    grid.scrollTop += _dragScrollDir;
    _dragScrollRaf = requestAnimationFrame(_dragScrollStep);
  } else {
    _dragScrollRaf = 0;
  }
}

function handleDragAutoScroll(clientY) {
  const grid = $("#grid");
  if (!grid) return;
  const r = grid.getBoundingClientRect();
  let speed = 0;
  if (clientY < r.top + DRAG_SCROLL_ZONE) {
    const f = Math.min(1, (r.top + DRAG_SCROLL_ZONE - clientY) / DRAG_SCROLL_ZONE);
    speed = -Math.ceil(f * DRAG_SCROLL_MAX);
  } else if (clientY > r.bottom - DRAG_SCROLL_ZONE) {
    const f = Math.min(1, (clientY - (r.bottom - DRAG_SCROLL_ZONE)) / DRAG_SCROLL_ZONE);
    speed = Math.ceil(f * DRAG_SCROLL_MAX);
  }
  _dragScrollDir = speed;
  if (speed && !_dragScrollRaf) _dragScrollRaf = requestAnimationFrame(_dragScrollStep);
}

function stopDragAutoScroll() {
  _dragScrollDir = 0;
  if (_dragScrollRaf) { cancelAnimationFrame(_dragScrollRaf); _dragScrollRaf = 0; }
}

// Bind once — #grid is a stable container (its children are replaced, not it).
function bindGridDragScroll() {
  if (_dragScrollBound) return;
  const grid = $("#grid");
  if (!grid) return;
  _dragScrollBound = true;
  grid.addEventListener("dragover", (e) => handleDragAutoScroll(e.clientY));
  document.addEventListener("dragend", stopDragAutoScroll, true);
  document.addEventListener("drop", stopDragAutoScroll, true);
}

// ---- virtual scrolling ----------------------------------------------------
// Only the cards near the viewport live in the DOM. Spacer divs span whole
// grid rows above and below the window so the scrollbar height (and thus the
// scroll position of every card) stays exact, while big libraries render and
// scroll in constant time. Mirrors .grid's CSS: 272px rows, 14px gap, etc.
const VROW = 272, VGAP = 14, VPAD_TOP = 20, VPAD_X = 22, VMIN_COL = 192, VBUFFER = 4;
let _vAssets = [];
let _vRange = { start: -1, end: -1 };
let _vRaf = 0;
let _vBound = false;

function vCols() {
  const avail = $("#grid").clientWidth - 2 * VPAD_X;
  return Math.max(1, Math.floor((avail + VGAP) / (VMIN_COL + VGAP)));
}

function renderWindow() {
  const grid = $("#grid");
  const n = _vAssets.length;
  if (!n) return;
  const cols = vCols();
  const stride = VROW + VGAP;
  const totalRows = Math.ceil(n / cols);
  const firstRow = Math.min(
    Math.max(0, totalRows - 1),
    Math.max(0, Math.floor((grid.scrollTop - VPAD_TOP) / stride) - VBUFFER)
  );
  const visRows = Math.ceil(grid.clientHeight / stride) + VBUFFER * 2;
  const start = firstRow * cols;
  const end = Math.min(n, start + visRows * cols);
  if (start === _vRange.start && end === _vRange.end) return; // window unchanged
  _vRange = { start, end };

  const lastRow = Math.ceil(end / cols);
  const frag = document.createDocumentFragment();
  if (firstRow > 0) {
    const top = document.createElement("div");
    top.className = "vspacer";
    top.style.gridRow = "span " + firstRow;
    frag.appendChild(top);
  }
  for (let i = start; i < end; i++) frag.appendChild(buildCard(_vAssets[i], i));
  const bottomRows = totalRows - lastRow;
  if (bottomRows > 0) {
    const bottom = document.createElement("div");
    bottom.className = "vspacer";
    bottom.style.gridRow = "span " + bottomRows;
    frag.appendChild(bottom);
  }
  grid.replaceChildren(frag);
}

function bindVirtual() {
  if (_vBound) return;
  _vBound = true;
  const grid = $("#grid");
  grid.addEventListener("scroll", () => {
    // Scrolling recycles cards out of the DOM, orphaning any hover popup.
    if (_hoverEl || _hoverTimer) _closeHoverPreview();
    if (_vRaf) return;
    _vRaf = requestAnimationFrame(() => { _vRaf = 0; renderWindow(); });
  }, { passive: true });
  // The grid's content width changes when the scrollbar appears/disappears or
  // the window resizes — either can change the column count, which invalidates
  // the spacer spans. A ResizeObserver catches both (a scrollbar toggle is not
  // a window 'resize' event), so the window is recomputed whenever width moves.
  new ResizeObserver(() => { _vRange = { start: -1, end: -1 }; renderWindow(); }).observe(grid);
}

function renderGrid(assets, total) {
  const grid = $("#grid"); const empty = $("#emptyState");
  grid.classList.remove("grouped");  // leave grouped-section layout
  if (!assets.length) {
    _vAssets = []; _vRange = { start: -1, end: -1 };
    _currentAssets = []; _displaySections = [];
    grid.replaceChildren();
    empty.classList.remove("hidden");
    empty.innerHTML = total === 0 && !state.search && !state.filter.tag
      ? `<h2>No assets indexed yet</h2>
         <p>Add a folder of models, textures and HDRIs and Hangar will index it.
         Your files are never moved or copied.</p>
         <button onclick="document.getElementById('addFolderBtn').click()">Add asset folder</button>`
      : `<h2>Nothing matches</h2><p>Try a different search or clear the active filter.</p>`;
    return;
  }
  empty.classList.add("hidden");
  _vAssets = assets;
  _currentAssets = assets;
  _displaySections = assets.map(() => "");   // one flat section → contiguous shift-range
  _vRange = { start: -1, end: -1 };
  grid.scrollTop = 0;
  bindVirtual();
  renderWindow();
}

// When the 3D viewer caches a thumbnail for an asset, refresh that grid card so
// the rendered preview replaces the format badge without a full reload.
window.onViewerThumbCached = (id) => {
  thumbBust[id] = Date.now();
  const card = $(`#grid .card[data-id="${id}"]`);
  const thumb = card && card.querySelector(".card-thumb");
  if (!thumb) return;
  const img = new Image();
  img.onload = () => {
    const existing = thumb.querySelector("img");
    const tile = thumb.querySelector(".badge-tile");
    if (existing) existing.src = img.src;
    else if (tile) tile.replaceWith(img);
  };
  img.src = thumbUrl(id);
  img.alt = "";
};

// ---- detail drawer --------------------------------------------------------
let allTags = [];
// Blender capabilities, refreshed by loadState — used by the right-click menu
// and the background preview renderer without re-fetching /state each time.
const appCaps = { blenderReady: false, renderExts: [] };

// Load the cached thumbnail into the drawer preview area (in place).
function loadPreview(a) {
  const pv = new Image();
  pv.onload = () => {
    const ph = $("#dPreview");
    if (!ph) return;
    ph.innerHTML = "";
    ph.appendChild(pv);
  };
  pv.src = thumbUrl(a.id);
}

// Render a one-off Blender preview for a model that has no thumbnail yet (USD,
// Alembic, …), the first time its drawer is opened. Shows an inline spinner and
// swaps in the image + grid tile when done. Stays a no-op on failure (the manual
// "Render preview" button remains available with full error feedback).
async function autoRenderModelPreview(a) {
  const ph = $("#dPreview");
  if (ph) {
    ph.innerHTML =
      `<div class="d-rendering"><span class="d-render-spin"></span>` +
      `<span>Rendering preview…</span></div>`;
  }
  try {
    const r = await post(`assets/${a.id}/render`);
    // Drawer may have moved on to another asset while Blender worked.
    if (drawerAssetId !== a.id) return;
    if (r && r.ok) {
      a.has_thumb = true;
      thumbBust[a.id] = Date.now();
      loadPreview(a);
      const cardImg = $(`#grid .card[data-id="${a.id}"] img`);
      if (cardImg) cardImg.src = thumbUrl(a.id);
    } else if (ph) {
      // Leave the format placeholder; the manual Render button can retry.
      ph.innerHTML = `<div class="d-rendering muted">Preview unavailable — use “Render preview”.</div>`;
    }
  } catch (_) {
    if (ph && drawerAssetId === a.id)
      ph.innerHTML = `<div class="d-rendering muted">Preview unavailable — use “Render preview”.</div>`;
  }
}

// Silently upgrade a .blend embedded thumbnail (128 px source, always blurry)
// to a full EEVEE render the first time the drawer opens. Keeps the existing
// blurry thumb visible while Blender works, then swaps in the sharp one.
// One upgrade per asset per session — subsequent drawer opens use the cached
// full render without touching Blender again.
const _blendUpgraded = new Set();
async function _upgradeBlendPreview(a) {
  if (_blendUpgraded.has(a.id)) return;
  _blendUpgraded.add(a.id);
  try {
    const r = await post(`assets/${a.id}/render`);
    if (drawerAssetId !== a.id) return;
    if (r && r.ok) {
      thumbBust[a.id] = Date.now();
      loadPreview(a);
      const cardImg = $(`#grid .card[data-id="${a.id}"] img`);
      if (cardImg) cardImg.src = thumbUrl(a.id);
      if (a.ext === ".blend") renderBlendInfo(a);  // refresh the "Preview:" source line
    }
  } catch (_) {}
}

// ---- background previews for Blender-only model formats (USD, Alembic…) ----
// These formats have no in-browser loader and trimesh can't decode them, so they
// land in the grid with just a format badge. We render their thumbnails in the
// background — but strictly ONE Blender process at a time (a queue, not a swarm),
// which is what made the old passive-on-scroll approach unusable. A sequence
// token cancels in-flight work the moment the view changes.
const _thumbQueue = [];
let _thumbWorking = false;
let _thumbSeq = 0;

function enqueueMissingThumbs(assets) {
  const seq = ++_thumbSeq;       // invalidate anything queued for the old view
  _thumbQueue.length = 0;
  if (!appCaps.blenderReady) return;
  const exts = new Set(appCaps.renderExts);
  for (const a of assets) {
    if (a.has_thumb !== false || a.kind !== "model") continue;
    if (VIEWER_EXTS.has(a.ext) || !exts.has(a.ext)) continue;  // GLB/GLTF/FBX use the viewer
    _thumbQueue.push({ id: a.id, seq });
  }
  if (_thumbQueue.length && !_thumbWorking) _drainThumbQueue();
}

async function _drainThumbQueue() {
  _thumbWorking = true;
  try {
    while (_thumbQueue.length) {
      const job = _thumbQueue.shift();
      if (job.seq !== _thumbSeq) continue;          // view moved on — drop it
      let r;
      try { r = await post(`assets/${job.id}/render`); }
      catch (_) { continue; }
      if (job.seq !== _thumbSeq || !r || !r.ok) continue;
      thumbBust[job.id] = Date.now();
      const ca = currentAssets.find((x) => x.id === job.id);
      if (ca) ca.has_thumb = true;
      // Swap the freshly rendered image into the live tile, if it's on screen.
      const card = $(`#grid .card[data-id="${job.id}"]`);
      const tile = card && card.querySelector(".badge-tile");
      if (tile) {
        const img = new Image();
        img.draggable = false; img.alt = "";
        img.onload = () => { if (tile.isConnected) tile.replaceWith(img); };
        img.src = thumbUrl(job.id);
      }
    }
  } finally {
    _thumbWorking = false;
  }
}

// Reflect a favourite toggle in the grid + cached list without rebuilding the
// drawer. Keeps the sidebar count fresh; drops the card if the Favourites
// filter is active and the asset was just unfavourited.
function syncFavoriteInGrid(id, fav) {
  const ca = currentAssets.find((x) => x.id === id);
  if (ca) ca.favorite = fav;
  const card = $(`#grid .card[data-id="${id}"]`);
  if (card) card.classList.toggle("is-fav", fav);
  if (state.filter.favorite && !fav) refresh();
  else loadState();
}

async function openDrawer(id, idx) {
  _closeHoverPreview();
  destroyViewerIfActive();
  // idx is the position in currentAssets — used for prev/next navigation.
  if (idx === undefined) idx = currentAssets.findIndex(a => a.id === id);
  drawerIdx = idx;
  drawerAssetId = id;
  const a = await api(`assets/${id}`);
  if (a && a.id != null) thumbMtime[a.id] = a.mtime;
  const st = await api("state");
  allTags = st.tags;
  allCategories = st.categories || [];
  const blenderReady = st.blender_render;
  const color = KIND_COLORS[a.kind] || "var(--mute)";
  const ext = a.ext.replace(".", "").toUpperCase();
  const canBlender = a.kind === "model";
  const canMaterial = a.kind === "texture" || a.kind === "material";
  const canWorld = a.kind === "hdri";
  // Any model format Blender can open/import gets an on-demand render button.
  const canRender = canBlender && (st.blender_render_exts || []).includes(a.ext);

  const hasPrev = idx > 0;
  const hasNext = idx >= 0 && idx < currentAssets.length - 1;

  $("#drawerBody").innerHTML = `
    <div class="d-preview" id="dPreview">
      <div class="d-thumb-placeholder">
        <span class="d-ph-stripe" style="background:${color}"></span>
        <span class="d-ph-ext" style="color:${color}">${esc(ext)}</span>
      </div>
    </div>
    <div class="d-nav">
      <button class="d-nav-btn" id="dPrev" ${hasPrev ? "" : "disabled"}>← Prev</button>
      <span class="d-nav-pos">${idx >= 0 ? idx + 1 : "?"} / ${currentAssets.length}</span>
      <button class="d-nav-btn" id="dNext" ${hasNext ? "" : "disabled"}>Next →</button>
    </div>
    <div class="d-body">
      <div class="d-name-row">
        <h2 class="d-name" id="dName">${esc(a.name)}</h2>
        <button class="d-rename-btn" id="dRename" title="Rename file">✏</button>
      </div>
      <div class="d-path clickable" id="dPath" title="Open this file — ${esc(a.path)}">${esc(a.path)}</div>
      ${a.exists === false ? `<div class="d-missing">⚠ This file isn't accessible right now — the drive/folder may be disconnected, moved, or deleted.</div>` : ""}
      <div class="d-format-row">
        <span class="d-format-badge" style="color:${color};border-color:${color}40">${esc(ext)}</span>
        <span class="d-kind-label">${esc(a.kind)}</span>
      </div>
      <div class="d-specs">
        <div><div class="spec-k">Size</div><div class="spec-v">${fmtSize(a.size)}</div></div>
        <div><div class="spec-k">Vertices</div><div class="spec-v">${fmtNum(a.vertices)}</div></div>
        <div><div class="spec-k">Faces</div><div class="spec-v">${fmtNum(a.faces)}</div></div>
        ${a.ext === ".blend" && a.blend_assets != null ? `
        <div><div class="spec-k">Marked assets</div><div class="spec-v">${fmtNum(a.blend_assets)}</div></div>` : ""}
      </div>
      <div id="dMaps"></div>
      ${a.ext === ".blend" ? `<div id="dBlend"></div>` : ""}
      <div class="d-section-label">Tags</div>
      <div class="tag-row" id="tagRow"></div>
      <div class="d-section-label">Category</div>
      <div id="dCatRow" class="d-cat-row"></div>
      ${(a.collections || []).length ? `
        <div class="d-section-label">Collections</div>
        <div class="d-collections">${(a.collections || []).map(c =>
          `<span class="chip on" style="border-color:var(--select)">${esc(c)}</span>`).join("")}
        </div>` : ""}
      <div class="d-actions">
        <button class="act" id="favAct">
          <span class="act-ico">${a.favorite ? "★" : "☆"}</span>
          <span class="act-label">${a.favorite ? "Remove from favorites" : "Add to favorites"}</span></button>
        ${canBlender ? `<button class="act primary" id="blenderAct">
          <span class="act-ico">⤴</span> <span class="act-label">Send to Blender</span></button>` : ""}
        ${canBlender ? `<button class="act" id="blenderCursorAct">
          <span class="act-ico">✛</span> <span class="act-label">Send at 3D cursor</span></button>` : ""}
        ${canMaterial ? `<button class="act primary" id="materialAct">
          <span class="act-ico">⬢</span> <span class="act-label">Build material in Blender</span></button>` : ""}
        ${canWorld ? `<button class="act primary" id="worldAct">
          <span class="act-ico">☀</span> <span class="act-label">Set as world HDRI</span></button>` : ""}
        ${canRender ? `<button class="act" id="renderAct">
          <span class="act-ico">◳</span> <span class="act-label">Render preview${blenderReady ? "" : " (Blender not found)"}</span></button>` : ""}
        ${canRender && !blenderReady ? `<button class="act" id="setBlenderAct">
          <span class="act-ico">⚙</span> <span class="act-label">Set Blender path…</span></button>` : ""}
        <button class="act" id="revealAct"><span class="act-ico">⊞</span> Reveal in file manager</button>
        <button class="act" id="copyAct"><span class="act-ico">⧉</span> Copy file path</button>
      </div>
    </div>`;

  // Load preview: 3D viewer for GLB/GLTF/FBX, thumbnail for everything else.
  // Once a thumbnail is cached we show it instantly and make the (heavier) 3D
  // viewer opt-in, so reopening an asset no longer re-generates the model every
  // time. The first open (no thumb yet) loads the viewer, which caches a poster.
  if (VIEWER_EXTS.has(a.ext)) {
    const pv = $("#dPreview");
    const start3D = () => {
      pv.classList.remove("has-poster");
      pv.style.backgroundImage = "";
      pv.innerHTML = "";
      getViewerMod().then(mod => mod.startViewer(pv, a.id, a.ext));
    };
    if (a.has_thumb) {
      pv.classList.add("has-poster");
      pv.style.backgroundImage = `url("${thumbUrl(a.id)}")`;
      pv.innerHTML = `<button class="view-3d" id="view3dBtn" title="Load the interactive 3D model">▸ View in 3D</button>`;
      $("#view3dBtn").onclick = start3D;
    } else {
      start3D();
    }
  } else {
    loadPreview(a);
    // Non-viewer models (USD/USDA/USDC, Alembic…) have no in-browser 3D loader
    // and trimesh can't decode them, so they arrive with no thumbnail. Render
    // one in Blender on demand — ONCE, here on open (never during a grid scroll)
    // — so a preview appears without the background-render lag.
    if (!a.has_thumb && a.kind === "model" && canRender && blenderReady) {
      autoRenderModelPreview(a);
    }
    // .blend embedded thumbnails are 128 px — always blurry at drawer size.
    // Silently upgrade to a full EEVEE render in the background (once per session).
    if (a.ext === ".blend" && canRender && blenderReady) {
      _upgradeBlendPreview(a);
    }
  }

  renderTagEditor(a);
  renderDrawerCategoryEditor(a);
  if (a.kind === "texture") renderTextureMaps(a);

  $("#favAct").onclick = async () => {
    const r = await post(`assets/${a.id}/favorite`, { value: !a.favorite });
    a.favorite = r.favorite;
    // Update the button + grid in place — the drawer stays open (no rebuild/flash).
    const btn = $("#favAct");
    btn.querySelector(".act-ico").textContent = a.favorite ? "★" : "☆";
    btn.querySelector(".act-label").textContent =
      a.favorite ? "Remove from favorites" : "Add to favorites";
    syncFavoriteInGrid(a.id, a.favorite);
    toast(a.favorite ? "Added to favorites ★" : "Removed from favorites", "success");
  };

  // Run a "send to Blender" action button: disable it, POST, toast the result.
  const wireSend = (id, path, body, sending, idle, okMsg) => {
    const btn = $("#" + id); if (!btn) return;
    const lbl = btn.querySelector(".act-label");
    btn.onclick = async () => {
      btn.disabled = true; lbl.textContent = sending;
      let r; try { r = await post(`assets/${a.id}/${path}`, body); } catch (_) { r = null; }
      btn.disabled = false; lbl.textContent = idle;
      if (r && r.ok) toast(typeof okMsg === "function" ? okMsg(r) : okMsg, "success");
      else toast((r && r.error) || "Couldn't queue for Blender. Is the bridge connected?", "error");
    };
  };

  if (canBlender) {
    wireSend("blenderAct", "send-blender", {}, "Sending…", "Send to Blender",
      "Queued — the Blender bridge will import it.");
    wireSend("blenderCursorAct", "send-blender", { place_at_cursor: true },
      "Sending…", "Send at 3D cursor", "Queued — imports at the 3D cursor.");
  }
  if (canMaterial) {
    wireSend("materialAct", "send-material", { to_selection: true },
      "Building…", "Build material in Blender",
      (r) => `Material queued (${(r.maps || []).join(", ") || "base colour"}) — applies to your selection.`);
  }
  if (canWorld) {
    wireSend("worldAct", "send-hdri", {}, "Sending…", "Set as world HDRI",
      "Queued — sets the scene's world lighting.");
  }

  if (canRender) {
    $("#renderAct").onclick = async () => {
      const btn = $("#renderAct"); const lbl = btn.querySelector(".act-label");
      const prev = lbl.textContent; btn.disabled = true;
      lbl.textContent = "Rendering in Blender…";
      toast("Rendering preview — this can take a moment.");
      const r = await post(`assets/${a.id}/render`);
      btn.disabled = false; lbl.textContent = prev;
      if (r.ok) {
        thumbBust[a.id] = Date.now();
        // Reload preview + grid thumbnail in place; drawer stays open.
        loadPreview(a);
        const cardImg = $(`#grid .card[data-id="${a.id}"] img`);
        if (cardImg) cardImg.src = thumbUrl(a.id);
        toast("Preview rendered.", "success");
      } else if (r.blender === false) {
        // Blender wasn't found — offer to point Hangar at it right away.
        toast(r.error || "Blender not found.", "error");
        if (confirm((r.error || "Blender wasn't found.") +
            "\n\nSet the path to your Blender executable now?")) {
          setBlenderPath(a.id, idx);
        }
      } else {
        toast(r.error || "Render failed.", "error");
      }
    };
  }

  const setBlenderBtn = $("#setBlenderAct");
  if (setBlenderBtn) setBlenderBtn.onclick = () => setBlenderPath(a.id, idx);

  // Clickable path → open the file with the OS default app.
  const dPath = $("#dPath");
  if (dPath) dPath.onclick = () => openAssetFile(a);

  // Rename the underlying file (keeps its extension and folder).
  const dRename = $("#dRename");
  if (dRename) dRename.onclick = () => renameAsset(a, idx);

  // .blend: list marked assets (names + previews) and missing textures.
  if (a.ext === ".blend") renderBlendInfo(a);

  $("#revealAct").onclick = async () => {
    const r = await post(`assets/${a.id}/reveal`);
    if (r.ok) toast("Opened in file manager.", "success");
    else toast(r.error || "Couldn't open file manager.", "error");
  };

  $("#copyAct").onclick = async () => {
    try {
      await navigator.clipboard.writeText(a.path);
      toast("Path copied to clipboard.", "success");
    } catch (_) {
      // Fallback for non-HTTPS / restricted contexts.
      const ta = document.createElement("textarea");
      ta.value = a.path;
      ta.style.cssText = "position:fixed;opacity:0";
      document.body.appendChild(ta);
      ta.focus(); ta.select();
      try { document.execCommand("copy"); toast("Path copied to clipboard.", "success"); }
      catch (e) { toast("Copy failed — check browser permissions.", "error"); }
      document.body.removeChild(ta);
    }
  };

  if (hasPrev) {
    $("#dPrev").onclick = () => openDrawer(currentAssets[idx - 1].id, idx - 1);
  }
  if (hasNext) {
    $("#dNext").onclick = () => openDrawer(currentAssets[idx + 1].id, idx + 1);
  }

  $("#drawer").classList.add("open");
  $("#scrim").classList.remove("hidden");
}

function renderTagEditor(a) {
  const row = $("#tagRow");
  const current = new Set((a.tags || []).map((t) => t.name));
  row.innerHTML = "";
  for (const t of allTags) {
    const chip = document.createElement("span");
    chip.className = "chip" + (current.has(t.name) ? " on" : "");
    chip.innerHTML = `<span class="tdot" style="background:${safeColor(t.color)}"></span>${esc(t.name)}`;
    chip.onclick = async () => {
      if (current.has(t.name)) current.delete(t.name); else current.add(t.name);
      await post(`assets/${a.id}/tags`, { tags: [...current] });
      a.tags = [...current].map((n) => allTags.find((x) => x.name === n) || { name: n, color: "#8A8F9A" });
      renderTagEditor(a); refresh();
    };
    row.appendChild(chip);
  }
  const add = document.createElement("span");
  add.className = "chip"; add.style.borderStyle = "dashed";
  add.textContent = "+ new tag";
  add.onclick = async () => {
    const name = prompt("Tag name:");
    if (!name) return;
    await post("tags", { name });
    allTags = (await api("state")).tags;
    current.add(name.trim());
    await post(`assets/${a.id}/tags`, { tags: [...current] });
    a.tags = [...current].map((n) => allTags.find((x) => x.name === n) || { name: n, color: "#8A8F9A" });
    renderTagEditor(a); refresh();
  };
  row.appendChild(add);
}

let allCategories = [];
let categoryFolders = [];

function renderDrawerCategoryEditor(a) {
  const row = $("#dCatRow");
  if (!row) return;
  const current = new Set(a.categories || []);
  row.innerHTML = "";
  for (const c of allCategories) {
    const chip = document.createElement("span");
    chip.className = "chip cat-chip" + (current.has(c.name) ? " on" : "");
    chip.innerHTML = `${c.icon ? `<span class="cat-ico">${esc(c.icon)}</span>` : ""}${esc(c.name)}`;
    chip.title = current.has(c.name) ? "Click to remove from this category" : "Click to add to this category";
    chip.onclick = async () => {
      const add = !current.has(c.name);
      if (add) current.add(c.name); else current.delete(c.name);
      await post(`assets/${a.id}/category`, { category: c.name, add });
      a.categories = [...current];
      renderDrawerCategoryEditor(a);
      loadState();
    };
    row.appendChild(chip);
  }
  const add = document.createElement("span");
  add.className = "chip"; add.style.borderStyle = "dashed";
  add.textContent = "+ new";
  add.onclick = async () => {
    const name = prompt("Category name (e.g. Vehicles):"); if (!name) return;
    const icon = prompt("Icon (emoji, optional — press Cancel to skip):") || "";
    await post("categories", { name, icon });
    allCategories = (await api("state")).categories || [];
    current.add(name.trim());
    await post(`assets/${a.id}/category`, { category: name.trim(), add: true });
    a.categories = [...current];
    renderDrawerCategoryEditor(a);
    loadState();
  };
  row.appendChild(add);
}

// Texture sets bundle several maps (diffuse, normal, roughness…). Show them all
// as a compact list under the preview; clicking a map swaps the preview image.
async function renderTextureMaps(a) {
  const host = $("#dMaps");
  if (!host) return;
  const r = await api(`assets/${a.id}/set`);
  const members = r.members || [];
  recordThumbMtimes(members);
  if (members.length <= 1) return;  // a lone texture isn't a set — nothing to show
  host.innerHTML =
    `<div class="d-section-label">Maps · ${members.length}</div>` +
    `<div class="d-maps">` +
    members.map((m) => {
      const role = m.map_role || "other";
      const mext = m.ext.replace(".", "").toUpperCase();
      const active = m.id === a.id ? " active" : "";
      return `<button class="map-row${active}" data-id="${m.id}" title="${esc(m.path)}">
        <img class="map-thumb" src="${thumbUrl(m.id)}" alt="" loading="lazy" />
        <span class="map-role">${esc(role)}</span>
        <span class="map-ext">${esc(mext)}</span>
        <span class="map-size">${fmtSize(m.size)}</span>
      </button>`;
    }).join("") +
    `</div>`;
  host.querySelectorAll(".map-row").forEach((btn) => {
    btn.onclick = () => {
      const id = Number(btn.dataset.id);
      const pv = new Image();
      pv.onload = () => {
        const ph = $("#dPreview");
        if (ph) { ph.innerHTML = ""; ph.appendChild(pv); }
      };
      pv.src = thumbUrl(id);
      host.querySelectorAll(".map-row").forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
    };
  });
}

function closeDrawer() {
  destroyViewerIfActive();
  $("#drawer").classList.remove("open");
  $("#scrim").classList.add("hidden");
  drawerIdx = -1;
  drawerAssetId = null;
}

function isDrawerOpen() { return $("#drawer").classList.contains("open"); }

// True when focus is in a text field — so shortcuts don't hijack typing.
function isTyping(el) {
  if (!el) return false;
  const tag = el.tagName;
  return tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || el.isContentEditable;
}

// ---- Blender path ---------------------------------------------------------
// Lets the user point Hangar at their Blender executable when auto-discovery
// failed, so on-demand "Render preview" works. Re-opens the drawer on success
// so the button state (and blender_render flag) refreshes.
async function setBlenderPath(reopenId, reopenIdx) {
  const eg = navigator.platform.startsWith("Win")
    ? "e.g. C:\\Program Files\\Blender Foundation\\Blender 4.2\\blender.exe"
    : "e.g. /usr/bin/blender  or  /Applications/Blender.app/Contents/MacOS/Blender";
  const path = prompt("Full path to your Blender executable:\n" + eg);
  if (path === null) return;
  const r = await post("settings/blender", { path: path.trim() });
  if (r.ok && r.available) {
    toast("Blender path set — render away.", "success");
    if (reopenId != null) openDrawer(reopenId, reopenIdx);
  } else if (r.ok) {
    toast("Saved, but that file isn't a working Blender.", "error");
  } else {
    toast(r.error || "That path doesn't exist.", "error");
  }
}

// ---- folder picking + scanning -------------------------------------------
async function chooseFolder() {
  if (window.pywebview && window.pywebview.api && window.pywebview.api.pick_folder) {
    try { return await window.pywebview.api.pick_folder(); } catch (e) { /* fall through */ }
  }
  const r = await post("pick-folder");
  if (r && r.path) return r.path;
  if (r && r.cancelled) return null;
  return prompt("Paste the full path to an asset folder:") || null;
}

function startScanPolling(warmOnly = false) {
  if (state.scanTimer) return;
  // warmOnly = boot-time pre-baking with no fresh index, so skip the
  // "indexed N assets" toast but still show the progress bar.
  state.wasScanning = !warmOnly;
  $("#statusSummary").classList.add("hidden");
  $("#scanProgress").classList.remove("hidden");
  $("#rescanBtn").disabled = true;
  state.scanTimer = setInterval(pollScan, 350);
  pollScan();
}

async function pollScan() {
  const s = await api("scan/status");
  const warm = s.warm || {};
  if (s.running) {
    $("#scanText").textContent =
      `${s.library || "library"} — ${s.scanned.toLocaleString()}/${s.total.toLocaleString()} files`;
    $("#scanFill").style.width = s.pct + "%";
    $("#scanPct").textContent = s.pct + "%";
    await loadState();
  } else if (warm.running) {
    // Indexing is done; previews are now pre-baking in the background. Keep the
    // bar up so the user sees thumbnails filling in rather than a frozen grid.
    if (state.wasScanning) {
      state.wasScanning = false;
      toast(`Indexed ${s.indexed.toLocaleString()} assets — checking previews…`, "success");
      refresh();
    }
    const scanText = $("#scanText");
    scanText.textContent =
      `Generating missing previews — ${warm.done.toLocaleString()}/${warm.total.toLocaleString()}`;
    if (warm.current) {
      const fname = warm.current.replace(/.*[\\/]/, "");
      $("#scanFile").textContent = fname;
      $("#scanFile").title = warm.current;
    } else {
      $("#scanFile").textContent = "";
      $("#scanFile").title = "";
    }
    $("#scanFill").style.width = warm.pct + "%";
    $("#scanPct").textContent = warm.pct + "%";
    // Periodically repaint so freshly-baked thumbnails replace badge tiles.
    if (warm.done % 40 === 0) refresh();
  } else {
    clearInterval(state.scanTimer); state.scanTimer = null;
    $("#scanProgress").classList.add("hidden");
    $("#statusSummary").classList.remove("hidden");
    $("#rescanBtn").disabled = false;
    if (state.wasScanning) {
      state.wasScanning = false;
      toast(`Done — ${s.indexed.toLocaleString()} assets indexed`, "success");
    }
    // USD/FBX/Alembic need Blender to preview — if the warm pass couldn't find
    // it, say so once instead of leaving the user with silent blank tiles.
    if (warm.failed && !warm.blender) {
      toast(`${warm.failed} model${warm.failed === 1 ? "" : "s"} need Blender to ` +
            `preview (USD/FBX/Alembic). Set its path in ⚙ to enable them.`, "error");
    }
    refresh();  // final repaint to pick up the last baked previews
  }
}

// ---- events ---------------------------------------------------------------
$("#addFolderBtn").onclick = async () => {
  const path = await chooseFolder();
  if (!path) return;
  const r = await post("libraries", { path });
  if (r.error) { toast(r.error, "error"); return; }
  await loadState();
  startScanPolling();
};

$("#rescanBtn").onclick = async () => {
  const r = await post("scan");
  if (r.error) { toast(r.error, "error"); return; }
  if (r.scanning) startScanPolling();
};

// ---- diagnostics / logs ---------------------------------------------------
async function openDiagnostics() {
  const ta = $("#diagText");
  ta.value = "Loading…";
  $("#diagModal").classList.remove("hidden");
  refreshFarm();
  if (_farmPoll) clearInterval(_farmPoll);
  _farmPoll = setInterval(() => {
    if ($("#diagModal").classList.contains("hidden")) { clearInterval(_farmPoll); _farmPoll = null; return; }
    refreshFarm();
  }, 2000);
  try {
    const d = await api("diagnostics");
    const parts = [d.info, ""];
    for (const [name, text] of Object.entries(d.logs || {})) {
      parts.push(`===== ${name} =====`, (text || "(empty)").trim(), "");
    }
    ta.value = parts.join("\n");
  } catch (e) {
    ta.value = "Couldn't load diagnostics: " + e;
  }
}

// ---- render farm panel ----------------------------------------------------
let _farmPoll = null;
async function refreshFarm() {
  let d;
  try { d = await api("farm/workers"); } catch (_) { return; }
  $("#farmChunkVal").textContent = d.chunk;
  const box = $("#farmWorkers");
  if (!d.workers.length) {
    box.innerHTML = `<div class="farm-empty">No workers connected.</div>`;
    return;
  }
  box.innerHTML = d.workers.map((w) => `
    <div class="farm-worker${w.online ? "" : " offline"}">
      <span class="fw-dot"></span>
      <span class="fw-name" title="${esc(w.id)}">${esc(w.name)}</span>
      <span class="fw-gpu" title="${esc(w.gpu)}">${esc(w.gpu)}</span>
      <span class="fw-stats">${w.claimed ? w.claimed + " active · " : ""}${w.done} done${w.failed ? " · " + w.failed + " failed" : ""}</span>
    </div>`).join("");
}
async function _setFarmChunk(delta) {
  const cur = parseInt($("#farmChunkVal").textContent, 10) || 30;
  const next = Math.max(1, Math.min(500, cur + delta));
  try {
    const r = await post("farm/chunk", { chunk: next });
    if (r && r.ok) $("#farmChunkVal").textContent = r.chunk;
  } catch (_) { /* ignore */ }
}
$("#farmChunkUp").onclick = () => _setFarmChunk(+5);
$("#farmChunkDown").onclick = () => _setFarmChunk(-5);
// Download the standalone render-worker bundle (pre-filled with this Hangar's
// address) to copy onto another machine — no full app install needed.
const _farmDl = $("#farmDownload");
if (_farmDl) _farmDl.onclick = () => { window.location.href = "/api/farm/worker-download"; };
// ---- in-app updater -------------------------------------------------------
let _updateInfo = null;
let _updatePoll = null;
let _updateReady = false;   // download finished, exe ready to launch
async function checkForUpdate() {
  try {
    const u = await api("update/check");
    if (!u || !u.ok || !u.update_available) return;
    _updateInfo = u;
    const pill = $("#updatePill");
    pill.textContent = `⬆ Update to v${u.latest}`;
    pill.classList.remove("hidden");
    // One click downloads in the background, then turns into a Restart button.
    pill.onclick = beginBackgroundUpdate;
  } catch (_) { /* offline — no banner */ }
}
function openUpdateModal() {
  if (!_updateInfo) return;
  $("#updateTitle").textContent = `Update available — v${_updateInfo.latest}`;
  $("#updateSub").textContent =
    `You're on v${_updateInfo.current}. This downloads and unpacks v${_updateInfo.latest} ` +
    `into a new folder (your current install is left untouched), then you can launch it.`;
  $("#updateNotes").value = (_updateInfo.notes || "Release notes unavailable.").trim();
  const btn = $("#updateDownloadBtn");
  if (_updateReady) {
    // Already downloaded — show the Restart button, not a fresh download prompt.
    $("#updateProgress").classList.add("hidden");
    $("#updateLaunchBtn").classList.remove("hidden");
    btn.disabled = true; btn.textContent = "Downloaded ✓";
  } else if (_updatePoll) {
    // A download is running in the background — show its progress.
    $("#updateProgress").classList.remove("hidden");
    $("#updateLaunchBtn").classList.add("hidden");
    btn.disabled = true; btn.textContent = "Downloading…";
  } else {
    $("#updateProgress").classList.add("hidden");
    $("#updateLaunchBtn").classList.add("hidden");
    btn.disabled = false; btn.textContent = "Download & install";
  }
  $("#updateModal").classList.remove("hidden");
}
// Closing the modal must NOT stop the download — it keeps running in the
// background and the status-bar pill tracks it, turning into a Restart button
// when it's ready. So we leave _updatePoll alone here.
$("#updateClose").onclick = () => { $("#updateModal").classList.add("hidden"); };
$("#updateModal").onclick = (e) => { if (e.target.id === "updateModal") $("#updateClose").onclick(); };

function _setPill(text, handler) {
  const pill = $("#updatePill");
  pill.textContent = text;
  pill.classList.remove("hidden");
  pill.onclick = handler;
}

// Show the background download on the shared status-bar progress bar, but never
// fight a scan or a regenerate pass that already owns it.
function _updateStatusBar(pct) {
  if (state.scanTimer || state.regenTimer) return;
  state.updateBar = true;
  $("#statusSummary").classList.add("hidden");
  $("#scanProgress").classList.remove("hidden");
  $("#scanText").textContent = `Downloading update v${_updateInfo.latest}`;
  $("#scanFill").style.width = pct + "%";
  $("#scanPct").textContent = pct + "%";
  $("#scanFile").textContent = ""; $("#scanFile").title = "";
}
function _clearUpdateStatusBar() {
  if (!state.updateBar) return;
  state.updateBar = false;
  if (state.scanTimer || state.regenTimer) return;   // someone else owns it now
  $("#scanProgress").classList.add("hidden");
  $("#statusSummary").classList.remove("hidden");
}

function startUpdatePolling() {
  if (_updatePoll) clearInterval(_updatePoll);
  _updatePoll = setInterval(async () => {
    let s; try { s = await api("update/status"); } catch (_) { return; }
    const pct = s.pct || 0;
    $("#updateFill").style.width = pct + "%";       // modal bar (if open)
    $("#updatePct").textContent = pct + "%";
    if (s.done) {
      clearInterval(_updatePoll); _updatePoll = null;
      _clearUpdateStatusBar();
      _updateReady = true;
      const btn = $("#updateDownloadBtn");
      btn.textContent = "Downloaded ✓"; btn.disabled = true;
      if (s.exe) $("#updateLaunchBtn").classList.remove("hidden");
      _setPill(`⟳ Restart to finish v${_updateInfo.latest}`, launchUpdate);
      toast(`v${_updateInfo.latest} is ready — click Restart when you're ready.`, "success");
    } else if (s.error) {
      clearInterval(_updatePoll); _updatePoll = null;
      _clearUpdateStatusBar();
      const btn = $("#updateDownloadBtn");
      btn.disabled = false; btn.textContent = "Retry download";
      $("#updateProgress").classList.add("hidden");
      _setPill(`⬆ Update to v${_updateInfo.latest}`, openUpdateModal);
      toast("Update failed: " + s.error, "error");
    } else {
      // Still downloading — show it on the status bar AND keep the always-visible
      // pill in sync so the user can close the modal and keep working.
      _updateStatusBar(pct);
      _setPill(`⬇ Downloading v${_updateInfo.latest}… ${pct}%`, openUpdateModal);
    }
  }, 500);
}

// Kick off (or resume) the download in the background — no modal step. Used by
// the manual "Check for updates" button and the status-bar pill, so the user
// just gets a "Restart to finish" button once it's downloaded.
function beginBackgroundUpdate() {
  if (!_updateInfo) return;
  if (_updateReady) { launchUpdate(); return; }      // already downloaded → restart now
  if (_updatePoll) { openUpdateModal(); return; }    // already downloading → show progress
  if (!_updateInfo.asset_url) {                       // no build attached → open releases page
    window.open(_updateInfo.html_url || "https://github.com/4s0ck3t/Hangar/releases", "_blank");
    return;
  }
  post("update/download", {
    url: _updateInfo.asset_url, name: _updateInfo.asset_name, version: _updateInfo.latest,
  });
  startUpdatePolling();
  _setPill(`⬇ Downloading v${_updateInfo.latest}… 0%`, openUpdateModal);
  toast(`Downloading v${_updateInfo.latest} in the background…`, "success");
}

async function startUpdateDownload() {
  if (!_updateInfo) return;
  if (!_updateInfo.asset_url) { window.open(_updateInfo.html_url || "https://github.com/4s0ck3t/Hangar/releases", "_blank"); return; }
  const btn = $("#updateDownloadBtn");
  btn.disabled = true; btn.textContent = "Downloading…";
  $("#updateProgress").classList.remove("hidden");
  await post("update/download", { url: _updateInfo.asset_url, name: _updateInfo.asset_name, version: _updateInfo.latest });
  startUpdatePolling();
}
$("#updateDownloadBtn").onclick = startUpdateDownload;

async function launchUpdate() {
  const r = await post("update/launch");
  if (r.ok) {
    toast("Restarting into the new version...", "success");
    setTimeout(() => { try { window.close(); } catch (_) {} }, 120);
  } else toast((r && r.error) || "Couldn't launch - open Hangar files and run it.", "error");
}
// Manual "Check for updates" — explicit feedback for every outcome so it's
// never a mystery whether a check ran (unlike the silent boot-time check).
async function manualCheckUpdate() {
  const btn = $("#checkUpdateBtn");
  const prev = btn.textContent;
  btn.disabled = true; btn.textContent = "Checking…";
  let u;
  // force=1 bypasses the hour-long release cache — an explicit click should
  // always query GitHub fresh, never report a stale "latest".
  try { u = await api("update/check?force=1"); }
  catch (e) { u = null; }
  btn.disabled = false; btn.textContent = prev;
  if (!u || !u.ok) {
    toast((u && u.error) || "Couldn't reach GitHub to check for updates.", "error");
    return;
  }
  if (u.update_available) {
    _updateInfo = u;
    // Start the download straight away in the background; the pill tracks it and
    // becomes a "Restart to finish" button when it's ready.
    beginBackgroundUpdate();
  } else {
    toast(`You're on the latest version (v${u.current}).`, "success");
  }
}
$("#checkUpdateBtn").onclick = manualCheckUpdate;
function updateDupBtn() {
  const b = $("#dupBtn");
  if (b) b.classList.toggle("active", state.filter.duplicates);
}
$("#dupBtn").onclick = () => {
  const on = !state.filter.duplicates;
  resetFilter();                 // duplicates is a standalone view
  state.filter.duplicates = on;
  state.search = ""; const s = $("#search"); if (s) s.value = "";
  updateDupBtn();
  refresh();
};

$("#updateLaunchBtn").onclick = async () => {
  const btn = $("#updateLaunchBtn");
  btn.disabled = true; btn.textContent = "Restarting…";
  await launchUpdate();
  btn.disabled = false; btn.textContent = "Restart into new version";
};

$("#diagBtn").onclick = openDiagnostics;
$("#dataDirBtn").onclick = async () => {
  const r = await post("open-data-dir");
  if (r && r.error) toast(r.error, "error");
};
$("#diagClose").onclick = () => $("#diagModal").classList.add("hidden");
$("#diagModal").onclick = (e) => { if (e.target.id === "diagModal") $("#diagModal").classList.add("hidden"); };
$("#diagCopy").onclick = async () => {
  const ta = $("#diagText");
  ta.select();
  try {
    await navigator.clipboard.writeText(ta.value);
    toast("Diagnostics copied — paste it to support.", "success");
  } catch (_) {
    try { document.execCommand("copy"); toast("Diagnostics copied.", "success"); }
    catch (e) { toast("Select the text and copy manually.", "error"); }
  }
};

$("#addCollectionBtn").onclick = async () => {
  const name = prompt("New collection name:"); if (!name) return;
  await post("collections", { name }); loadState();
};
// Create a category. When `prefKind` is one of the asset types we skip the
// "which type?" prompt and file it straight under that type (used by the inline
// "+ New category" button inside a grouped type view).
// Drag-reorder a category in the sidebar: drop `dragId` immediately before
// `targetCat`. Only allowed within the same kind group, since the list is
// rendered grouped by asset type.
async function reorderCategory(dragId, targetCat) {
  if (!dragId || dragId === targetCat.id) return;
  const drag = allCategories.find((c) => c.id === dragId);
  if (!drag) return;
  if ((drag.kind || "") !== (targetCat.kind || "")) {
    toast("Categories can only be reordered within the same asset type.", "error");
    return;
  }
  const ids = allCategories.map((c) => c.id).filter((id) => id !== dragId);
  const at = ids.indexOf(targetCat.id);
  if (at < 0) return;
  ids.splice(at, 0, dragId);
  const r = await post("categories/reorder", { order: ids });
  if (r && r.ok) await loadState();
}

// Curated icon set for the category dialog; the custom field accepts any emoji.
const CATEGORY_ICONS = [
  "🤖","🛸","🚀","🪐","👾","🛰️","🏠","🏛️","🏰","⛩️",
  "🌲","🌿","🪨","🌊","🔥","🌋","🏔️","🌌","☀️","🌙",
  "🚗","✈️","🚢","⚙️","🔧","🛠️","🧱","🪵","💎","🔩",
  "🛋️","🪑","🛏️","🚪","🪟","🖼️","💡","🕯️","📦","🎨",
  "🗿","🐉","🦴","👤","⚔️","🛡️","🔫","🎮","🔮","🎭",
];

// Modal dialog for creating a category: name, clickable icon grid (+ custom),
// asset-type scope, and optional auto-match keywords. Resolves to the form
// values, or null on cancel.
function categoryDialog(prefKind) {
  return new Promise((resolve) => {
    const kinds = [["", "Shared (any type)"], ["model", "Model"],
      ["hdri", "HDRI"], ["texture", "Texture"], ["material", "Material"]];
    const wrap = document.createElement("div");
    wrap.className = "cat-dialog";
    wrap.innerHTML = `
      <div class="cat-dlg-box">
        <div class="cat-dlg-head">New category</div>
        <div class="cat-dlg-body">
          <div>
            <label class="cat-dlg-label">Name</label>
            <input class="cat-dlg-input" id="cdName" placeholder="e.g. Robots">
          </div>
          <div>
            <label class="cat-dlg-label">Icon</label>
            <div class="cat-icon-grid" id="cdIcons">
              ${CATEGORY_ICONS.map((ic) => `<button type="button" class="cat-icon-opt" data-ic="${ic}">${ic}</button>`).join("")}
            </div>
            <input class="cat-dlg-input" id="cdIconCustom" placeholder="…or paste any emoji" maxlength="4" style="margin-top:6px">
          </div>
          <div>
            <label class="cat-dlg-label">Asset type</label>
            <select class="cat-dlg-select" id="cdKind">
              ${kinds.map(([v, l]) => `<option value="${v}" ${v === (prefKind || "") ? "selected" : ""}>${l}</option>`).join("")}
            </select>
          </div>
          <div>
            <label class="cat-dlg-label">Auto-match keywords (optional, comma-separated)</label>
            <input class="cat-dlg-input" id="cdKeywords" placeholder="robot, droid, mech">
          </div>
        </div>
        <div class="cat-dlg-foot">
          <button class="cat-dlg-btn" id="cdCancel">Cancel</button>
          <button class="cat-dlg-btn primary" id="cdSave">Create</button>
        </div>
      </div>`;
    document.body.appendChild(wrap);

    let icon = "";
    const custom = wrap.querySelector("#cdIconCustom");
    wrap.querySelectorAll(".cat-icon-opt").forEach((b) => {
      b.onclick = () => {
        wrap.querySelectorAll(".cat-icon-opt").forEach((x) => x.classList.remove("sel"));
        b.classList.add("sel"); icon = b.dataset.ic; custom.value = "";
      };
    });
    custom.oninput = () => {
      wrap.querySelectorAll(".cat-icon-opt").forEach((x) => x.classList.remove("sel"));
      icon = custom.value.trim();
    };

    const onKey = (e) => { if (e.key === "Escape") close(null); };
    const close = (val) => { wrap.remove(); document.removeEventListener("keydown", onKey); resolve(val); };
    document.addEventListener("keydown", onKey);
    wrap.addEventListener("mousedown", (e) => { if (e.target === wrap) close(null); });
    wrap.querySelector("#cdCancel").onclick = () => close(null);
    const save = () => {
      const name = wrap.querySelector("#cdName").value.trim();
      if (!name) { wrap.querySelector("#cdName").focus(); return; }
      close({ name, icon, kind: wrap.querySelector("#cdKind").value,
        keywords: wrap.querySelector("#cdKeywords").value });
    };
    wrap.querySelector("#cdSave").onclick = save;
    wrap.querySelector("#cdName").addEventListener("keydown", (e) => { if (e.key === "Enter") save(); });
    setTimeout(() => wrap.querySelector("#cdName").focus(), 30);
  });
}

async function promptNewCategory(prefKind) {
  const pk = ["model", "hdri", "texture", "material"].includes(prefKind) ? prefKind : "";
  const res = await categoryDialog(pk);
  if (!res) return false;
  await post("categories", res);
  if (res.keywords.trim()) await post("categories/auto", {});
  loadState();
  return true;
}

$("#addCategoryBtn").onclick = () => promptNewCategory();

$("#autoClassifyBtn").onclick = async () => {
  toast("Auto-classifying…");
  const r = await post("categories/auto", {});
  if (r.ok) {
    toast(r.links_added
      ? `Filed ${r.assets_matched} asset${r.assets_matched === 1 ? "" : "s"} (${r.links_added} new tags)`
      : "Everything already categorised", "success");
    loadState(); refresh();
  } else {
    toast("Auto-classify failed.", "error");
  }
};

let searchTimer;
$("#search").oninput = (e) => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => { state.search = e.target.value.trim(); refresh(); }, 220);
};
$("#sort").onchange = (e) => { state.sort = e.target.value; refresh(); };
$("#drawerClose").onclick = closeDrawer;
$("#scrim").onclick = closeDrawer;
$("#clearFilterBtn").onclick = () => { resetFilter(); state.search = ""; $("#search").value = ""; refresh(); };
// Suppress the host browser's (Edge WebView2 / Chrome --app) default right-click
// menu so the app feels native — no "Reload / Save as / Print / Inspect". Our own
// context menus (e.g. the card "Move to category" menu) call preventDefault in
// their own handlers and still show. Text fields keep their copy/paste menu.
document.addEventListener("contextmenu", (e) => {
  if (isTyping(e.target)) return;       // allow native copy/paste in inputs/textareas
  if (window.getSelection && String(window.getSelection())) return;  // selected text → allow copy
  e.preventDefault();
});

document.addEventListener("keydown", (e) => {
  // "/" jumps to the search box (unless you're already typing in a field).
  if (e.key === "/" && !isTyping(e.target) && !e.metaKey && !e.ctrlKey) {
    e.preventDefault();
    $("#search").focus();
    $("#search").select();
    return;
  }
  if (e.key === "Escape") {
    if (isDrawerOpen()) closeDrawer();
    else if (selection.size > 0) clearSelection();
    else if (document.activeElement === $("#search")) $("#search").blur();
    return;
  }
  // Arrow keys page through assets while the drawer is open.
  if (isDrawerOpen() && !isTyping(e.target)) {
    if (e.key === "ArrowLeft" && drawerIdx > 0) {
      e.preventDefault();
      openDrawer(currentAssets[drawerIdx - 1].id, drawerIdx - 1);
    } else if (e.key === "ArrowRight" && drawerIdx >= 0
               && drawerIdx < currentAssets.length - 1) {
      e.preventDefault();
      openDrawer(currentAssets[drawerIdx + 1].id, drawerIdx + 1);
    }
  }
});

// ---- boot -----------------------------------------------------------------
(async function boot() {
  const st = await loadState();
  await refresh();
  const s = await api("scan/status");
  if (s.running) startScanPolling();
  else if (s.warm && s.warm.running) startScanPolling(true);
  // Warn once if HDRIs are indexed but no backend can decode them.
  if (st && st.counts.by_kind.hdri &&
      st.hdri_backends && st.hdri_backends[0] === "none") {
    toast("HDR/EXR previews unavailable — install opencv-python-headless", "error");
  }
  checkForUpdate();  // surfaces the update pill if a newer release exists
})();

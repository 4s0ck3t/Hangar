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

const state = {
  filter: { kind: "", ext: "", tag: "", collection: "", favorite: false },
  search: "", sort: "name", scanTimer: null, wasScanning: false,
};
const $ = (s) => document.querySelector(s);
const thumbBust = {};
function thumbUrl(id) {
  return `/api/thumb/${id}` + (thumbBust[id] ? `?t=${thumbBust[id]}` : "");
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
  renderKindFilters(s.counts);
  renderTagFilters(s.tags);
  renderCollectionFilters(s.collections);
  renderLibraries(s.libraries);
  renderStatusBar(s.counts);
  return s;
}

function renderStatusBar(counts) {
  $("#statusSummary").textContent = `${counts.total.toLocaleString()} assets`;
  const parts = [];
  const bk = counts.by_kind || {};
  if (bk.model) parts.push(`${bk.model} models`);
  if (bk.texture) parts.push(`${bk.texture} textures`);
  if (bk.hdri) parts.push(`${bk.hdri} HDRIs`);
  if (bk.material) parts.push(`${bk.material} materials`);
  $("#statusBreakdown").textContent = parts.join("  ·  ");
}

function renderKindFilters(counts) {
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
    const li = document.createElement("li");
    const isModelSub = !state.filter.ext && !state.filter.favorite
      && !state.filter.tag && !state.filter.collection;
    const active = isModelSub && state.filter.kind === kind;
    if (active) li.classList.add("active");
    const color = KIND_COLORS[kind] || "var(--mute)";
    li.innerHTML =
      `<span class="dot" style="background:${color}"></span>` +
      `<span>${KIND_LABELS[key]}</span><span class="count">${count}</span>`;
    li.onclick = () => { resetFilter(); state.filter.kind = kind; refresh(); };
    ul.appendChild(li);

    // Render model subcategories under the Models item.
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
          `<span>${grp.label}</span><span class="count">${grpCount}</span>`;
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
  if (state.filter.favorite) fav.classList.add("active");
  fav.innerHTML =
    `<span class="dot" style="background:var(--signal)"></span>` +
    `<span>Favorites</span><span class="count">${counts.favorites}</span>`;
  fav.onclick = () => { resetFilter(); state.filter.favorite = true; refresh(); };
  ul.appendChild(fav);
}

function renderTagFilters(tags) {
  const ul = $("#tagFilters"); ul.innerHTML = "";
  if (!tags.length) {
    ul.innerHTML = `<li style="color:var(--faint);cursor:default">No tags yet</li>`;
    return;
  }
  for (const t of tags) {
    const li = document.createElement("li");
    if (state.filter.tag === t.name) li.classList.add("active");
    li.innerHTML =
      `<span class="dot" style="background:${t.color}"></span>` +
      `<span>${t.name}</span><span class="count">${t.c}</span>`;
    li.onclick = () => { resetFilter(); state.filter.tag = t.name; refresh(); };
    ul.appendChild(li);
  }
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
      `<span>${c.name}</span><span class="count">${c.c}</span>`;
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

function renderLibraries(libs) {
  const ul = $("#libraryList"); ul.innerHTML = "";
  if (!libs.length) {
    ul.innerHTML = `<li style="color:var(--faint)">No folders added</li>`;
    return;
  }
  for (const lib of libs) {
    const li = document.createElement("li");
    li.title = lib.path;
    li.innerHTML =
      `<span class="dot" style="background:var(--faint)"></span>` +
      `<span class="lib-name">${lib.name}</span>` +
      `<button class="lib-remove" title="Remove">&times;</button>`;
    li.querySelector(".lib-remove").onclick = async (e) => {
      e.stopPropagation();
      if (!confirm(`Remove "${lib.name}" from Hangar? Files stay on disk.`)) return;
      await api(`libraries/${lib.id}`, { method: "DELETE" });
      await loadState(); refresh();
    };
    ul.appendChild(li);
  }
}

function resetFilter() {
  state.filter = { kind: "", ext: "", tag: "", collection: "", favorite: false };
}

// ---- clear-filter button visibility ---------------------------------------
function updateClearBtn() {
  const active = state.filter.kind || state.filter.ext || state.filter.tag
    || state.filter.collection || state.filter.favorite || state.search;
  $("#clearFilterBtn").classList.toggle("hidden", !active);
}

// ---- grid -----------------------------------------------------------------
let currentAssets = [];  // last fetched asset list for drawer prev/next
let drawerIdx = -1;      // position of the open drawer asset in currentAssets

async function refresh() {
  const p = new URLSearchParams();
  if (state.filter.kind) p.set("kind", state.filter.kind);
  if (state.filter.ext)  p.set("ext", state.filter.ext);
  if (state.filter.tag) p.set("tag", state.filter.tag);
  if (state.filter.collection) p.set("collection", state.filter.collection);
  if (state.filter.favorite) p.set("favorite", "1");
  if (state.search) p.set("search", state.search);
  p.set("sort", state.sort);

  const data = await api("assets?" + p.toString());
  currentAssets = data.assets;
  renderGrid(data.assets, data.total);
  await loadState();
  updateActiveLabel(data.total);
  updateClearBtn();
}

function updateActiveLabel(total) {
  let label = state.filter.favorite ? "Favorites"
    : state.filter.tag ? `#${state.filter.tag}`
    : state.filter.collection ? state.filter.collection
    : state.filter.ext ? (() => {
        const grp = MODEL_EXT_GROUPS.find(g => g.exts.join(",") === state.filter.ext);
        return grp ? grp.label : state.filter.ext;
      })()
    : KIND_LABELS[state.filter.kind || "all"];
  $("#activeFilter").textContent = `${label} · ${total}`;
}

function renderGrid(assets, total) {
  const grid = $("#grid"); const empty = $("#emptyState");
  if (!assets.length) {
    grid.innerHTML = "";
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
  grid.innerHTML = "";
  assets.forEach((a, i) => {
    const card = document.createElement("div");
    card.className = "card" + (a.favorite ? " is-fav" : "");
    card.dataset.id = a.id;
    card.style.animationDelay = Math.min(i * 10, 220) + "ms";
    const color = KIND_COLORS[a.kind] || "var(--mute)";
    const ext = a.ext.replace(".", "").toUpperCase();
    const tagDots = (a.tags || []).slice(0, 4)
      .map((t) => `<span class="tdot" style="background:${t.color}"></span>`).join("");
    card.innerHTML = `
      <div class="card-thumb">
        <span class="kind-stripe" style="background:${color}"></span>
        <span class="fav-pin">●</span>
        <div class="badge-tile">
          <span class="badge-ext" style="color:${color}">${ext}</span>
        </div>
      </div>
      <div class="card-meta">
        <div class="card-name" title="${a.name}">${a.name}</div>
        <div class="card-line">
          <span class="card-ext" style="color:${color}">${ext}</span>
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
    card.onclick = () => openDrawer(a.id, i);

    // Drag support — let models be dropped into collections.
    if (a.kind === "model") {
      card.draggable = true;
      card.addEventListener("dragstart", (e) => {
        e.dataTransfer.setData("text/x-hangar-asset-id", String(a.id));
        e.dataTransfer.effectAllowed = "copy";
        card.classList.add("dragging");
      });
      card.addEventListener("dragend", () => card.classList.remove("dragging"));
    }

    grid.appendChild(card);
  });
}

// ---- detail drawer --------------------------------------------------------
let allTags = [];

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
  // idx is the position in currentAssets — used for prev/next navigation.
  if (idx === undefined) idx = currentAssets.findIndex(a => a.id === id);
  drawerIdx = idx;
  const a = await api(`assets/${id}`);
  const st = await api("state");
  allTags = st.tags;
  const blenderReady = st.blender_render;
  const color = KIND_COLORS[a.kind] || "var(--mute)";
  const ext = a.ext.replace(".", "").toUpperCase();
  const canBlender = a.kind === "model";
  // Any model format Blender can open/import gets an on-demand render button.
  const canRender = canBlender && (st.blender_render_exts || []).includes(a.ext);

  const hasPrev = idx > 0;
  const hasNext = idx >= 0 && idx < currentAssets.length - 1;

  $("#drawerBody").innerHTML = `
    <div class="d-preview" id="dPreview">
      <div class="d-thumb-placeholder">
        <span class="d-ph-stripe" style="background:${color}"></span>
        <span class="d-ph-ext" style="color:${color}">${ext}</span>
      </div>
    </div>
    <div class="d-nav">
      <button class="d-nav-btn" id="dPrev" ${hasPrev ? "" : "disabled"}>← Prev</button>
      <span class="d-nav-pos">${idx >= 0 ? idx + 1 : "?"} / ${currentAssets.length}</span>
      <button class="d-nav-btn" id="dNext" ${hasNext ? "" : "disabled"}>Next →</button>
    </div>
    <div class="d-body">
      <h2 class="d-name">${a.name}</h2>
      <div class="d-path" id="dPath" title="${a.path}">${a.path}</div>
      <div class="d-format-row">
        <span class="d-format-badge" style="color:${color};border-color:${color}40">${ext}</span>
        <span class="d-kind-label">${a.kind}</span>
      </div>
      <div class="d-specs">
        <div><div class="spec-k">Size</div><div class="spec-v">${fmtSize(a.size)}</div></div>
        <div><div class="spec-k">Vertices</div><div class="spec-v">${fmtNum(a.vertices)}</div></div>
        <div><div class="spec-k">Faces</div><div class="spec-v">${fmtNum(a.faces)}</div></div>
      </div>
      <div class="d-section-label">Tags</div>
      <div class="tag-row" id="tagRow"></div>
      ${(a.collections || []).length ? `
        <div class="d-section-label">Collections</div>
        <div class="d-collections">${(a.collections || []).map(c =>
          `<span class="chip on" style="border-color:var(--select)">${c}</span>`).join("")}
        </div>` : ""}
      <div class="d-actions">
        <button class="act" id="favAct">
          <span class="act-ico">${a.favorite ? "★" : "☆"}</span>
          <span class="act-label">${a.favorite ? "Remove from favorites" : "Add to favorites"}</span></button>
        ${canBlender ? `<button class="act primary" id="blenderAct">
          <span class="act-ico">⤴</span> <span class="act-label">Send to Blender</span></button>` : ""}
        ${canRender ? `<button class="act" id="renderAct">
          <span class="act-ico">◳</span> <span class="act-label">Render preview${blenderReady ? "" : " (Blender not found)"}</span></button>` : ""}
        <button class="act" id="revealAct"><span class="act-ico">⊞</span> Reveal in file manager</button>
        <button class="act" id="copyAct"><span class="act-ico">⧉</span> Copy file path</button>
      </div>
    </div>`;

  // Load thumbnail into preview area.
  loadPreview(a);

  renderTagEditor(a);

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

  if (canBlender) {
    $("#blenderAct").onclick = async () => {
      const btn = $("#blenderAct");
      const lbl = btn.querySelector(".act-label");
      btn.disabled = true; lbl.textContent = "Sending…";
      const r = await post(`assets/${a.id}/send-blender`);
      btn.disabled = false; lbl.textContent = "Send to Blender";
      if (r.ok) toast("Queued — the Blender bridge will import it.", "success");
      else toast(r.error || "Couldn't queue for Blender.", "error");
    };
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
      } else {
        toast(r.error || "Render failed.", "error");
      }
    };
  }

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
    chip.innerHTML = `<span class="tdot" style="background:${t.color}"></span>${t.name}`;
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

function closeDrawer() {
  $("#drawer").classList.remove("open");
  $("#scrim").classList.add("hidden");
  drawerIdx = -1;
}

function isDrawerOpen() { return $("#drawer").classList.contains("open"); }

// True when focus is in a text field — so shortcuts don't hijack typing.
function isTyping(el) {
  if (!el) return false;
  const tag = el.tagName;
  return tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || el.isContentEditable;
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

function startScanPolling() {
  if (state.scanTimer) return;
  state.wasScanning = true;
  $("#statusSummary").classList.add("hidden");
  $("#scanProgress").classList.remove("hidden");
  $("#rescanBtn").disabled = true;
  state.scanTimer = setInterval(pollScan, 350);
  pollScan();
}

async function pollScan() {
  const s = await api("scan/status");
  if (s.running) {
    $("#scanText").textContent =
      `${s.library || "library"} — ${s.scanned.toLocaleString()}/${s.total.toLocaleString()} files`;
    $("#scanFill").style.width = s.pct + "%";
    $("#scanPct").textContent = s.pct + "%";
    await loadState();
  } else {
    clearInterval(state.scanTimer); state.scanTimer = null;
    $("#scanProgress").classList.add("hidden");
    $("#statusSummary").classList.remove("hidden");
    $("#rescanBtn").disabled = false;
    if (state.wasScanning) {
      state.wasScanning = false;
      toast(`Done — ${s.indexed.toLocaleString()} assets indexed`, "success");
      refresh();
    }
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

$("#addTagBtn").onclick = async () => {
  const name = prompt("New tag name:"); if (!name) return;
  await post("tags", { name }); loadState();
};
$("#addCollectionBtn").onclick = async () => {
  const name = prompt("New collection name:"); if (!name) return;
  await post("collections", { name }); loadState();
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
  await loadState();
  await refresh();
  const s = await api("scan/status");
  if (s.running) startScanPolling();
})();

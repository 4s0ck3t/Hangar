// Hangar frontend — vanilla JS, no build step, fully offline.

const KIND_COLORS = {
  model: "var(--k-model)", texture: "var(--k-texture)",
  hdri: "var(--k-hdri)", material: "var(--k-material)",
};
const KIND_LABELS = {
  all: "All assets", model: "Models", texture: "Textures",
  hdri: "HDRIs", material: "Materials",
};

const state = {
  filter: { kind: "", tag: "", collection: "", favorite: false },
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
function toast(msg) {
  const t = $("#toast"); t.textContent = msg; t.classList.remove("hidden");
  clearTimeout(toastTimer); toastTimer = setTimeout(() => t.classList.add("hidden"), 2600);
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
    const active = !state.filter.favorite && state.filter.kind === kind
      && !state.filter.tag && !state.filter.collection;
    if (active) li.classList.add("active");
    const color = KIND_COLORS[kind] || "var(--mute)";
    li.innerHTML =
      `<span class="dot" style="background:${color}"></span>` +
      `<span>${KIND_LABELS[key]}</span><span class="count">${count}</span>`;
    li.onclick = () => { resetFilter(); state.filter.kind = kind; refresh(); };
    ul.appendChild(li);
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
    li.title = lib.path;                       // full path on hover
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
  state.filter = { kind: "", tag: "", collection: "", favorite: false };
}

// ---- grid -----------------------------------------------------------------
async function refresh() {
  const p = new URLSearchParams();
  if (state.filter.kind) p.set("kind", state.filter.kind);
  if (state.filter.tag) p.set("tag", state.filter.tag);
  if (state.filter.collection) p.set("collection", state.filter.collection);
  if (state.filter.favorite) p.set("favorite", "1");
  if (state.search) p.set("search", state.search);
  p.set("sort", state.sort);

  const data = await api("assets?" + p.toString());
  renderGrid(data.assets, data.total);
  await loadState();
  updateActiveLabel(data.total);
}

function updateActiveLabel(total) {
  let label = state.filter.favorite ? "Favorites"
    : state.filter.tag ? `#${state.filter.tag}`
    : state.filter.collection ? state.filter.collection
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
          <span class="badge-kind">${a.kind}</span>
        </div>
      </div>
      <div class="card-meta">
        <div class="card-name" title="${a.name}">${a.name}</div>
        <div class="card-line">
          <span>${ext}</span><span>·</span><span>${fmtSize(a.size)}</span>
          <span class="card-tags">${tagDots}</span>
        </div>
      </div>`;
    const tile = card.querySelector(".badge-tile");
    const img = new Image();
    img.onload = () => { tile.replaceWith(img); };
    img.src = thumbUrl(a.id);
    img.alt = a.name;
    card.onclick = () => openDrawer(a.id);
    grid.appendChild(card);
  });
}

// ---- detail drawer --------------------------------------------------------
let allTags = [];
async function openDrawer(id) {
  const a = await api(`assets/${id}`);
  const st = await api("state");
  allTags = st.tags;
  const blenderReady = st.blender_render;
  const color = KIND_COLORS[a.kind] || "var(--mute)";
  const ext = a.ext.replace(".", "").toUpperCase();
  const canBlender = a.kind === "model";
  const isBlend = a.ext === ".blend";

  $("#drawerBody").innerHTML = `
    <div class="d-preview" id="dPreview">
      <div class="badge-tile">
        <span class="badge-ext" style="color:${color};font-size:44px">${ext}</span>
        <span class="badge-kind">${a.kind}</span>
      </div>
    </div>
    <div class="d-body">
      <h2 class="d-name">${a.name}</h2>
      <div class="d-path" id="dPath" title="${a.path}">${a.path}</div>
      <div class="d-specs">
        <div><div class="spec-k">Format</div><div class="spec-v">${ext}</div></div>
        <div><div class="spec-k">Size</div><div class="spec-v">${fmtSize(a.size)}</div></div>
        <div><div class="spec-k">Vertices</div><div class="spec-v">${fmtNum(a.vertices)}</div></div>
        <div><div class="spec-k">Faces</div><div class="spec-v">${fmtNum(a.faces)}</div></div>
      </div>
      <div class="d-section-label">Tags</div>
      <div class="tag-row" id="tagRow"></div>
      <div class="d-actions">
        <button class="act" id="favAct">
          <span class="act-ico">${a.favorite ? "★" : "☆"}</span>
          ${a.favorite ? "Remove from favorites" : "Add to favorites"}</button>
        ${canBlender ? `<button class="act primary" id="blenderAct">
          <span class="act-ico">⤴</span> Send to Blender</button>` : ""}
        ${isBlend ? `<button class="act" id="renderAct">
          <span class="act-ico">◳</span> <span class="act-label">Render preview${blenderReady ? "" : " (Blender not found)"}</span></button>` : ""}
        <button class="act" id="revealAct"><span class="act-ico">⊞</span> Reveal in file manager</button>
        <button class="act" id="copyAct"><span class="act-ico">⧉</span> Copy file path</button>
      </div>
    </div>`;

  renderTagEditor(a);
  const pv = new Image();
  pv.onload = () => { $("#dPreview").innerHTML = ""; $("#dPreview").appendChild(pv); };
  pv.src = thumbUrl(a.id);

  $("#favAct").onclick = async () => {
    const r = await post(`assets/${a.id}/favorite`, { value: !a.favorite });
    a.favorite = r.favorite; openDrawer(id); refresh();
  };
  if (canBlender) $("#blenderAct").onclick = async () => {
    await post(`assets/${a.id}/send-blender`);
    toast("Queued for Blender — the bridge addon will import it.");
  };
  if (isBlend) $("#renderAct").onclick = async () => {
    const btn = $("#renderAct"); const lbl = btn.querySelector(".act-label");
    const prev = lbl.textContent; btn.disabled = true;
    lbl.textContent = "Rendering in Blender…";
    toast("Rendering preview in Blender — this can take a moment.");
    const r = await post(`assets/${a.id}/render-blend`);
    btn.disabled = false; lbl.textContent = prev;
    if (r.ok) {
      thumbBust[a.id] = Date.now();
      toast("Preview rendered.");
      openDrawer(id); refresh();
    } else {
      toast(r.error || "Render failed.");
    }
  };
  $("#revealAct").onclick = () => post(`assets/${a.id}/reveal`);
  $("#copyAct").onclick = () =>
    navigator.clipboard.writeText(a.path).then(() => toast("Path copied"));

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
}

// ---- folder picking + scanning -------------------------------------------
async function chooseFolder() {
  // Desktop build: native pywebview folder dialog.
  if (window.pywebview && window.pywebview.api && window.pywebview.api.pick_folder) {
    try { return await window.pywebview.api.pick_folder(); } catch (e) { /* fall through */ }
  }
  // Browser mode: native Tk dialog on the local server.
  const r = await post("pick-folder");
  if (r && r.path) return r.path;
  if (r && r.cancelled) return null;
  // Last resort if no native dialog is available.
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
    await loadState();           // live counts as the grid fills
  } else {
    clearInterval(state.scanTimer); state.scanTimer = null;
    $("#scanProgress").classList.add("hidden");
    $("#statusSummary").classList.remove("hidden");
    $("#rescanBtn").disabled = false;
    if (state.wasScanning) {
      state.wasScanning = false;
      toast(`Done — ${s.indexed.toLocaleString()} assets indexed`);
      refresh();
    }
  }
}

// ---- events ---------------------------------------------------------------
$("#addFolderBtn").onclick = async () => {
  const path = await chooseFolder();
  if (!path) return;
  const r = await post("libraries", { path });
  if (r.error) { toast(r.error); return; }
  await loadState();
  startScanPolling();
};

$("#rescanBtn").onclick = async () => {
  const r = await post("scan");
  if (r.error) { toast(r.error); return; }
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
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeDrawer(); });

// ---- boot -----------------------------------------------------------------
(async function boot() {
  await loadState();
  await refresh();
  const s = await api("scan/status");   // resume progress UI if a scan is mid-flight
  if (s.running) startScanPolling();
})();

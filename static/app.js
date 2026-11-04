"use strict";
const $ = (s) => document.querySelector(s);
const audio = $("#audio");
const st = {
  auth: localStorage.getItem("token") || "", cfg: null, book: null, segs: [], cur: -1,
  playing: false, playId: 0, speed: 1, cache: new Map(), reads: new Map(), poll: null, saveTimer: null,
  lastPage: 0, settings: null, models: {},
};

/* ---------- API ---------- */
function askToken() {
  const t = prompt("Jeton d'accès (API_TOKEN du serveur) :");
  if (!t) throw new Error("Jeton requis : recharge la page.");
  st.auth = t.trim();
  localStorage.setItem("token", st.auth);
}
async function api(path, opts = {}) {
  if (!st.auth) askToken();
  const res = await fetch("/api" + path, {
    ...opts, headers: { ...(opts.headers || {}), Authorization: "Bearer " + st.auth },
  });
  if (res.status === 401) { st.auth = ""; localStorage.removeItem("token"); return api(path, opts); }
  return res;
}
async function jget(path, opts) {
  const r = await api(path, opts);
  if (!r.ok) {
    const d = (await r.json().catch(() => ({}))).detail;
    const err = new Error(typeof d === "string" ? d : "Erreur " + r.status);
    err.status = r.status;
    throw err;
  }
  return r.json();
}
const jpost = (path, body, method = "POST") => jget(path, {
  method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body ?? {}),
});
const note = (msg) => { $("#book-status").textContent = msg; };

/* ---------- Navigation / thème ---------- */
function showView(name) {
  for (const v of document.querySelectorAll(".view")) v.hidden = v.id !== "view-" + name;
  for (const t of document.querySelectorAll(".tab")) t.classList.toggle("active", t.dataset.view === name);
  if (name === "library") loadLibrary().catch((e) => ($("#upload-msg").textContent = e.message));
  if (name === "settings") loadSettings().catch((e) => ($("#settings-form").textContent = e.message));
}
function applyTheme() {
  const saved = localStorage.getItem("dark");
  const dark = saved === null ? matchMedia("(prefers-color-scheme: dark)").matches : saved === "1";
  document.documentElement.classList.toggle("dark", dark);
}

/* ---------- Bibliothèque ---------- */
function el(tag, text, cls) {
  const e = document.createElement(tag);
  if (text !== undefined) e.textContent = text;
  if (cls) e.className = cls;
  return e;
}
async function loadLibrary() {
  const books = await jget("/books");
  const ul = $("#books");
  ul.replaceChildren();
  for (const b of books) {
    const li = el("li");
    const info = el("div");
    const title = el("strong", b.title);
    if (b.lang && b.lang !== "fr" && b.lang !== "auto") title.append(el("span", b.lang.toUpperCase(), "badge"));
    info.append(title, el("div",
      b.status === "processing" ? `Extraction ${b.pages_done}/${b.pages_total}`
        : b.status === "error" ? "Erreur : " + b.error
        : `${b.pages_total} p. · ${b.n_segments} passages`, "muted"));
    const actions = el("div", undefined, "actions");
    const open = el("button", "Ouvrir");
    open.onclick = () => openBook(b.id).catch((e) => ($("#upload-msg").textContent = e.message));
    const del = el("button", "🗑");
    del.onclick = async () => {
      if (!confirm(`Supprimer « ${b.title} » ?`)) return;
      await api("/books/" + b.id, { method: "DELETE" });
      if (st.book && st.book.id === b.id) closeBook();
      loadLibrary();
    };
    actions.append(open, del);
    li.append(info, actions);
    ul.append(li);
  }
  if (!books.length) ul.append(el("li", "Aucun livre pour l'instant.", "muted"));
}

$("#upload-form").onsubmit = async (ev) => {
  ev.preventDefault();
  const msg = $("#upload-msg");
  const fd = new FormData();
  fd.append("file", $("#file").files[0]);
  fd.append("title", $("#title").value);
  fd.append("lang", $("#lang").value);
  fd.append("translate", $("#translate").checked ? "1" : "0");
  msg.textContent = "Envoi en cours…";
  try {
    const { id } = await jget("/books", { method: "POST", body: fd });
    msg.textContent = "";
    ev.target.reset();
    await openBook(id);
  } catch (e) { msg.textContent = e.message; }
};

/* ---------- Lecteur ---------- */
function closeBook() {
  stopSound(); st.playId++; st.playing = false;
  st.book = null; st.segs = []; st.cur = -1; clearInterval(st.poll); st.reads.clear();
  $("#player").hidden = true; $("#reader-tools").hidden = true; $("#text").replaceChildren(); $("#book-title").textContent = "Aucun livre ouvert";
  note("");
}
async function openBook(id) {
  closeBook();
  st.book = await jget("/books/" + id);
  st.lastPage = 0; trimCache(-100);
  $("#book-title").textContent = st.book.title;
  $("#chk-translate").checked = !!st.book.translate;
  await loadMoreSegments();
  $("#player").hidden = false; $("#reader-tools").hidden = false;
  showView("reader");
  updateStatus();
  if (st.segs.length) highlight(Math.min(st.book.progress_idx, st.segs.length - 1), true);
  setupMediaSession();
  if (st.book.status === "processing") st.poll = setInterval(pollBook, 3000);
}
async function pollBook() {
  try {
    st.book = await jget("/books/" + st.book.id);
    await loadMoreSegments();
    updateStatus();
    if (st.book.status !== "processing") clearInterval(st.poll);
  } catch (e) { note(e.message); }
}
function updateStatus() {
  const b = st.book;
  note(b.status === "processing" ? `Extraction en cours : page ${b.pages_done}/${b.pages_total} (tu peux déjà lire)`
    : b.status === "error" ? "Erreur : " + b.error
    : `${b.pages_total} pages · ${st.segs.length} passages`);
}
async function loadMoreSegments() {
  for (;;) {
    const part = await jget(`/books/${st.book.id}/segments?offset=${st.segs.length}&limit=200`);
    for (const s of part) addSegment(s);
    if (part.length < 200) break;
  }
}
function addSegment(s) {
  const p = el("p");
  p.dataset.idx = s.idx;
  if (s.page !== st.lastPage) { p.append(el("span", "Page " + s.page, "page-tag")); st.lastPage = s.page; }
  p.append(el("span", "", "t"), el("span", "", "orig"));
  p.onclick = () => play(s.idx);
  $("#text").append(p);
  st.segs.push(s);
  paint(s.idx);
}
/* Affiche un segment : traduction française (ou original en italique tant qu'elle n'est pas faite) + original masquable. */
function paint(idx) {
  const s = st.segs[idx], p = document.querySelector(`#text p[data-idx="${idx}"]`);
  if (!s || !p) return;
  const t = p.querySelector(".t");
  t.textContent = s.text;
  t.classList.toggle("pending", !!s.pending);
  p.querySelector(".orig").textContent = (s.translated || s.pending) ? s.original : "";
  p.querySelector(".warn")?.remove();
  if (s.error) { const w = el("span", "⚠ " + s.error, "warn"); p.append(w); }
}
function readInfo(idx) {                          // traduction à la demande, mise en cache côté serveur ET ici
  if (!st.book || !st.segs[idx]) return Promise.resolve(null);
  if (!st.reads.has(idx)) {
    const bookId = st.book.id;
    const pr = jget(`/books/${bookId}/segments/${idx}/read`).then((r) => {
      if (st.book && st.book.id === bookId && st.segs[idx]) {
        Object.assign(st.segs[idx], { text: r.text, original: r.original, translated: r.translated,
                                      pending: false, error: r.error, lang: r.lang });
        paint(idx);
      }
      return r;
    });
    pr.catch(() => st.reads.delete(idx));
    st.reads.set(idx, pr);
  }
  return st.reads.get(idx);
}
function forgetReads() { st.reads.clear(); for (const [, p] of st.cache) p.then((u) => u && URL.revokeObjectURL(u)).catch(() => {}); st.cache.clear(); }
function highlight(idx, scroll) {
  document.querySelector("#text p.active")?.classList.remove("active");
  const p = document.querySelector(`#text p[data-idx="${idx}"]`);
  if (!p) return;
  p.classList.add("active");
  if (scroll !== false) p.scrollIntoView({ block: "center", behavior: "smooth" });
  st.cur = idx;
  $("#pos").textContent = `p.${st.segs[idx].page} · ${idx + 1}/${st.segs.length}`;
  clearTimeout(st.saveTimer);
  st.saveTimer = setTimeout(() => api(`/books/${st.book.id}/progress`, {
    method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ idx }),
  }).catch(() => {}), 1500);
}

/* ---------- Lecture audio ---------- */
function audioUrl(idx) {
  if (!st.segs[idx]) return Promise.resolve(null);
  if (!st.cache.has(idx)) {
    const bookId = st.book.id;
    const p = (async () => {
      const r = await api(`/books/${bookId}/segments/${idx}/audio`);
      if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || "Audio indisponible (" + r.status + ")");
      return URL.createObjectURL(await r.blob());
    })();
    p.catch(() => st.cache.delete(idx));
    st.cache.set(idx, p);
  }
  return st.cache.get(idx);
}
function trimCache(idx) {                       // garde peu de blobs en mémoire
  for (const [k, p] of st.cache) {
    if (k < idx - 1 || k > idx + 3) { st.cache.delete(k); p.then((u) => u && URL.revokeObjectURL(u)).catch(() => {}); }
  }
}
function stopSound() { audio.pause(); speechSynthesis.cancel(); }
function setPlayIcon() { $("#btn-play").textContent = st.playing ? "⏸" : "▶️"; }

async function play(idx) {
  if (!st.book || idx < 0 || idx >= st.segs.length) return;
  const id = ++st.playId;
  stopSound();
  st.playing = true; setPlayIcon(); highlight(idx);
  try {
    const pending = st.segs[idx].pending;
    if (pending) note("Traduction en cours…");
    const info = await readInfo(idx);                    // traduit si besoin
    if (id !== st.playId) return;
    if (pending) updateStatus();
    if (info.error) note("⚠ Traduction impossible (" + info.error + ") : lecture dans la langue d'origine");
    for (const k of [1, 2, 3]) readInfo(idx + k).catch(() => {});        // traduit les passages suivants en avance
    if (st.cfg.server_audio) {
      const url = await audioUrl(idx);
      if (id !== st.playId) return;
      audio.src = url; audio.playbackRate = st.speed;
      await audio.play();
      audioUrl(idx + 1).catch(() => {}); audioUrl(idx + 2).catch(() => {});   // préchargement
      trimCache(idx);
    } else {                                    // voix de l'appareil, aucun audio serveur
      const u = new SpeechSynthesisUtterance(info.text);
      u.lang = info.speak_tag || "fr-FR"; u.rate = st.speed;
      u.onend = () => { if (id === st.playId) next(); };
      u.onerror = (ev) => {
        if (id !== st.playId || ev.error === "canceled" || ev.error === "interrupted") return;
        st.playing = false; setPlayIcon(); note("Synthèse vocale indisponible : " + ev.error);
      };
      speechSynthesis.speak(u);
    }
  } catch (e) {
    if (e.name === "AbortError" || id !== st.playId) return;
    st.playing = false; setPlayIcon(); note(e.message);
  }
}
function togglePlay() {
  if (!st.book) return;
  if (st.playing) { st.playId++; st.playing = false; stopSound(); setPlayIcon(); return; }
  if (st.cfg.server_audio && audio.src && !audio.ended && audio.currentTime > 0) {
    st.playing = true; setPlayIcon(); audio.play(); return;   // reprise exacte
  }
  play(st.cur >= 0 ? st.cur : 0);
}
function next() {
  if (st.cur + 1 < st.segs.length) return play(st.cur + 1);
  if (st.book.status === "processing") {          // la suite arrive encore de l'extraction
    const id = st.playId;
    setTimeout(() => { if (st.playing && id === st.playId) next(); }, 2000);
    return;
  }
  st.playing = false; setPlayIcon();
}
const prev = () => play(Math.max(0, st.cur - 1));

function setupMediaSession() {                  // boutons écran verrouillé / casque
  if (!("mediaSession" in navigator)) return;
  navigator.mediaSession.metadata = new MediaMetadata({ title: st.book.title, artist: "Lecteur de livres" });
  navigator.mediaSession.setActionHandler("play", () => { if (!st.playing) togglePlay(); });
  navigator.mediaSession.setActionHandler("pause", () => { if (st.playing) togglePlay(); });
  navigator.mediaSession.setActionHandler("nexttrack", next);
  navigator.mediaSession.setActionHandler("previoustrack", prev);
}

/* ---------- Discussion ---------- */
$("#chat-form").onsubmit = async (ev) => {
  ev.preventDefault();
  const log = $("#chat-log");
  const q = $("#question").value.trim();
  if (!q) return;
  log.append(el("div", q, "msg user"));
  $("#question").value = "";
  if (!st.book) { log.append(el("div", "Ouvre d'abord un livre.", "msg")); return; }
  const bubble = el("div", "…", "msg");
  log.append(bubble);
  try {
    const r = await jget(`/books/${st.book.id}/chat`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ question: q }),
    });
    bubble.textContent = r.answer;
    for (const s of r.sources) {
      const b = el("button", `p.${s.page} ▶`, "src");
      b.title = s.text;
      b.onclick = () => { showView("reader"); play(s.idx); };
      bubble.append(el("br"), b);
    }
  } catch (e) {
    bubble.textContent = e.message;
    if (e.status === 409 && /indexation|index/i.test(e.message) && st.book.status === "ready") {
      const b = el("button", "⟳ Réindexer ce livre", "src");
      b.onclick = () => reindexBook().then(() => (bubble.textContent = "Réindexation lancée : réessaie dans un instant."));
      bubble.append(el("br"), b);
    }
  }
};
async function reindexBook() { await jpost(`/books/${st.book.id}/reindex`); }

/* ---------- Options du livre (traduction) ---------- */
$("#btn-orig").onclick = () => $("#text").classList.toggle("show-orig");
$("#chk-translate").onchange = async (e) => {
  try {
    await jpost(`/books/${st.book.id}/options`, { translate: e.target.checked }, "PUT");
    st.book.translate = e.target.checked ? 1 : 0;
    forgetReads(); st.segs = []; st.lastPage = 0; $("#text").replaceChildren();
    await loadMoreSegments(); updateStatus();
    if (st.cur >= 0) highlight(st.cur, false);
  } catch (err) { note(err.message); }
};
async function retranslate(idx) {
  try {
    await jpost(`/books/${st.book.id}/retranslate`, { idx });
    forgetReads();
    st.segs = []; st.lastPage = 0; $("#text").replaceChildren();
    await loadMoreSegments();
    if (idx !== null) { highlight(idx, false); if (st.playing) play(idx); }
    note(idx === null ? "Traductions effacées : elles seront refaites au fil de la lecture." : "Passage retraduit.");
  } catch (e) { note(e.message); }
}
$("#btn-retr").onclick = () => st.cur >= 0 && retranslate(st.cur);
$("#btn-retr-all").onclick = () => confirm("Effacer toutes les traductions de ce livre ?") && retranslate(null);
$("#btn-reindex").onclick = () => reindexBook().then(() => note("Réindexation lancée."), (e) => note(e.message));

/* ---------- Réglages ---------- */
const PROV_LABEL = { ollama: "Ollama (local, sur le VPS)", openrouter: "OpenRouter", omniroute: "OmniRoute" };
async function loadSettings() { st.settings = await jget("/settings"); renderSettings(); }
function field(label, input) {
  const l = el("label", undefined, "f");
  l.append(el("span", label), input);
  return l;
}
function input(id, type, value, ph) {
  const i = el("input"); i.id = id; i.type = type; i.value = value || ""; if (ph) i.placeholder = ph;
  i.autocomplete = "off"; i.spellcheck = false;
  return i;
}
function keyBlock(id, name, p) {
  const box = el("div");
  box.append(field("Clé API", input(id, "password", "", p.has_key ? `Enregistrée (${p.key_hint}) — laisse vide pour la garder` : "Colle ta clé ici")));
  if (p.has_key) {
    const c = el("label", undefined, "check"), cb = el("input"); cb.type = "checkbox"; cb.id = id + "-clear";
    c.append(cb, el("span", "Effacer la clé enregistrée"));
    box.append(c);
  }
  return box;
}
function renderSettings() {
  const s = st.settings, f = $("#settings-form");
  f.replaceChildren();

  const g1 = el("fieldset"); g1.append(el("legend", "Traduction"));
  const auto = el("label", undefined, "check"), acb = el("input"); acb.type = "checkbox"; acb.id = "set-auto"; acb.checked = s.auto_translate;
  auto.append(acb, el("span", "Traduire en français par défaut les nouveaux documents dans une autre langue"));
  g1.append(auto);

  const g2 = el("fieldset"); g2.append(el("legend", "Passerelles (fournisseurs)"));
  g2.append(field("Ollama — URL", input("set-ollama-url", "text", s.providers.ollama.base_url, "http://localhost:11434/v1")));
  g2.append(el("div", "OpenRouter", "muted"), keyBlock("set-or-key", "openrouter", s.providers.openrouter));
  g2.append(el("div", "OmniRoute", "muted"),
    field("URL (compatible OpenAI, se termine souvent par /v1)", input("set-om-url", "text", s.providers.omniroute.base_url, "https://…/v1")),
    keyBlock("set-om-key", "omniroute", s.providers.omniroute));

  const g3 = el("fieldset"); g3.append(el("legend", "Modèle par usage"));
  for (const role of ["translate", "chat", "embed"]) g3.append(roleBlock(role, s));
  g3.append(el("div", "Changer le modèle d'embeddings impose de réindexer chaque livre (bouton « Réindexer » dans l'onglet Lecture).", "muted"));

  const g4 = el("fieldset"); g4.append(el("legend", "Coûts"));
  const paid = el("label", undefined, "check"), pcb = el("input"); pcb.type = "checkbox"; pcb.id = "set-paid"; pcb.checked = s.allow_paid;
  paid.append(pcb, el("span", "Autoriser les modèles payants"));
  g4.append(paid, el("div", "Par défaut, seuls les modèles gratuits (Ollama local, OpenRouter « :free ») sont acceptés. " +
    "Active cette case uniquement si tu choisis volontairement un modèle payant : il sera facturé sur ton compte.", "warn-box"));

  const save = el("button", "Enregistrer"); save.type = "submit";
  const msg = el("p", "", "muted"); msg.id = "settings-msg";
  f.append(g1, g2, g3, g4, save, msg);
}
function roleBlock(role, s) {
  const r = s.roles[role], box = el("div");
  box.append(el("strong", s.role_labels[role]));
  const sel = el("select"); sel.id = `role-${role}-prov`;
  for (const p of ["ollama", "openrouter", "omniroute"]) {
    const o = el("option", PROV_LABEL[p]); o.value = p; o.selected = p === r.provider; sel.append(o);
  }
  const model = input(`role-${role}-model`, "text", r.model, "nom du modèle"); model.setAttribute("list", `dl-${role}`);
  const dl = el("datalist"); dl.id = `dl-${role}`;
  const free = el("label", undefined, "check"), fcb = el("input"); fcb.type = "checkbox"; fcb.id = `role-${role}-free`; fcb.checked = !s.allow_paid;
  free.append(fcb, el("span", "Afficher seulement les modèles gratuits"));
  const res = el("div", "", "role-res"); res.id = `role-${role}-res`;
  const load = el("button", "Charger les modèles"); load.type = "button";
  const test = el("button", "Tester"); test.type = "button";
  load.onclick = () => loadModels(role);
  test.onclick = () => testRole(role);
  fcb.onchange = () => fillModels(role);
  sel.onchange = () => { st.models[role] = null; dl.replaceChildren(); };
  const row = el("div", undefined, "row"); row.append(load, test);
  box.append(field("Fournisseur", sel), field("Modèle", model), dl, free, row, res);
  return box;
}
function fillModels(role) {
  const dl = $(`#dl-${role}`), list = st.models[role] || [], onlyFree = $(`#role-${role}-free`).checked;
  dl.replaceChildren();
  for (const m of list) {
    if (onlyFree && m.free === false) continue;
    const o = el("option"); o.value = m.id; o.label = (m.free ? "🆓 " : "") + m.id; dl.append(o);
  }
}
function setRes(role, text, ok) {
  const r = $(`#role-${role}-res`); r.textContent = text; r.className = "role-res " + (ok ? "ok" : "err");
}
async function loadModels(role) {
  setRes(role, "Enregistrement puis chargement…", true);
  try {
    if (!(await saveSettings(true))) return setRes(role, "Corrige d'abord les réglages ci-dessus.", false);
    const prov = $(`#role-${role}-prov`).value;
    st.models[role] = (await jget("/llm/models?provider=" + prov)).models;
    fillModels(role);
    setRes(role, `${st.models[role].length} modèles chargés : clique dans le champ Modèle pour les voir.`, true);
  } catch (e) { setRes(role, e.message, false); }
}
async function testRole(role) {
  setRes(role, "Enregistrement puis test en cours…", true);
  try {
    if (!(await saveSettings(true))) return setRes(role, "Corrige d'abord les réglages ci-dessus.", false);
    const r = await jpost("/llm/test", { role });
    setRes(role, `✔ ${r.model} — ${r.ms} ms — « ${r.sample.slice(0, 80)} »`, true);
  } catch (e) { setRes(role, "✘ " + e.message, false); }
}
function keyPatch(id) {
  if ($(id + "-clear")?.checked) return { clear_key: true };
  const v = $(id).value.trim();
  return v ? { api_key: v } : {};
}
async function saveSettings(quiet) {
  const msg = $("#settings-msg");
  if ($("#set-paid").checked && !st.settings.allow_paid && !confirm("Les modèles payants peuvent être facturés sur ton compte. Activer quand même ?")) {
    $("#set-paid").checked = false; return false;
  }
  const roles = {};
  for (const r of ["translate", "chat", "embed"])
    roles[r] = { provider: $(`#role-${r}-prov`).value, model: $(`#role-${r}-model`).value.trim() };
  const patch = {
    allow_paid: $("#set-paid").checked, auto_translate: $("#set-auto").checked,
    providers: {
      ollama: { base_url: $("#set-ollama-url").value.trim() },
      openrouter: keyPatch("#set-or-key"),
      omniroute: { base_url: $("#set-om-url").value.trim(), ...keyPatch("#set-om-key") },
    },
    roles,
  };
  try {
    st.settings = await jpost("/settings", patch, "PUT");
    const keep = { ...st.models };
    renderSettings(); st.models = keep;
    for (const r of ["translate", "chat", "embed"]) if (st.models[r]) fillModels(r);
    $("#settings-msg").textContent = quiet ? "" : "✔ Enregistré";
    if (st.cfg) st.cfg.auto_translate = st.settings.auto_translate;
    return true;
  } catch (e) { msg.textContent = "✘ " + e.message; return false; }
}
$("#settings-form").onsubmit = (ev) => { ev.preventDefault(); saveSettings(false); };

/* ---------- Câblage ---------- */
for (const t of document.querySelectorAll(".tab")) t.onclick = () => showView(t.dataset.view);
$("#btn-play").onclick = togglePlay;
$("#btn-next").onclick = next;
$("#btn-prev").onclick = prev;
$("#speed").onchange = (e) => {
  st.speed = parseFloat(e.target.value); audio.playbackRate = st.speed;
  if (st.playing && !st.cfg.server_audio) play(st.cur);       // le débit vocal ne change pas en cours de phrase
};
$("#btn-listen").onclick = () => $("#text").classList.toggle("listen-only");
$("#btn-theme").onclick = () => {
  const dark = !document.documentElement.classList.contains("dark");
  localStorage.setItem("dark", dark ? "1" : "0"); applyTheme();
};
audio.onended = () => next();

(async function init() {
  applyTheme();
  if ("serviceWorker" in navigator) navigator.serviceWorker.register("sw.js").catch(() => {});
  try {
    st.cfg = await jget("/config");
    for (const [code, label] of Object.entries(st.cfg.upload_langs)) {
      const o = el("option", label); o.value = code; $("#lang").append(o);
    }
    $("#translate").checked = st.cfg.auto_translate;
    await loadLibrary();
  }
  catch (e) { $("#upload-msg").textContent = e.message; }
})();

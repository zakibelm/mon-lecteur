"use strict";
const $ = (s) => document.querySelector(s);
const audio = $("#audio");
const st = {
  auth: localStorage.getItem("token") || "", cfg: null, book: null, segs: [], cur: -1,
  playing: false, playId: 0, speed: 1, cache: new Map(), reads: new Map(), poll: null, saveTimer: null,
  lastPage: 0, settings: null, models: {}, fresh: new Set(), libPoll: null,
  prepRate: {}, books: [], vocab: [],
};
// Noms réels des voix Piper (aucune voix inventée à l'écran)
const VOICE_NAMES = { fr: { femme: "siwis", homme: "tom" }, en: { femme: "lessac", homme: "ryan" }, ar: { homme: "kareem" } };

/* ---------- API ---------- */
// Jeton : saisi dans la page (zone « Connexion »), jamais par une fenêtre système qu'on peut fermer par erreur.
class NeedToken extends Error {}
function showLogin(msg) {
  $("#login").hidden = false;
  $("#login-msg").textContent = msg || "";
  for (const v of document.querySelectorAll(".view")) v.hidden = true;
  $("#login-token").focus();
}
async function api(path, opts = {}) {
  if (!st.auth) { showLogin(); throw new NeedToken("Jeton requis"); }
  const res = await fetch("/api" + path, {
    ...opts, headers: { ...(opts.headers || {}), Authorization: "Bearer " + st.auth },
  });
  if (res.status === 401) {
    st.auth = ""; localStorage.removeItem("token");
    showLogin("Jeton refusé par le serveur : vérifie-le (il a peut-être été changé).");
    throw new NeedToken("Jeton refusé");
  }
  return res;
}
$("#login").onsubmit = async (ev) => {
  ev.preventDefault();
  st.auth = $("#login-token").value.trim();
  if (!st.auth) return;
  localStorage.setItem("token", st.auth);
  $("#login-msg").textContent = "Vérification…";
  try {
    await jget("/config");
    $("#login").hidden = true; $("#login-token").value = "";
    start();
  } catch (e) { if (!(e instanceof NeedToken)) $("#login-msg").textContent = e.message; }
};
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
  if (!st.auth) { showLogin(); return; }
  $("#login").hidden = true;
  for (const v of document.querySelectorAll(".view")) v.hidden = v.id !== "view-" + name;
  for (const t of document.querySelectorAll(".tab")) t.classList.toggle("active", t.dataset.view === name);
  if (name === "library") loadLibrary().catch(libError);
  if (name === "settings") loadSettings().catch((e) => ($("#settings-form").textContent = e.message));
  if (name === "vocab") loadVocab().catch((e) => ($("#vocab-list").textContent = e.message));
  if (name === "chat") $("#chat-book").textContent = st.book ? `Questions sur « ${st.book.title} »` : "Ouvre d'abord un livre dans Lecture.";
  if (name === "reader" && st.book) refreshPrep().catch(() => {});
  $("#lib-search").hidden = name !== "library" || !st.searchOpen;
  window.scrollTo({ top: 0 });
}
function applyTheme() {                    // choix enregistré, sinon réglage clair/sombre du téléphone
  const saved = localStorage.getItem("dark");
  const dark = saved === null ? matchMedia("(prefers-color-scheme: dark)").matches : saved === "1";
  document.documentElement.classList.toggle("dark", dark);
  document.querySelector('meta[name="theme-color"]').content = dark ? "#121316" : "#f7f3ec";
}

/* ---------- Bibliothèque ---------- */
function readBook(id) {
  st.fresh.delete(id);
  showView("reader");
  return openBook(id).catch((e) => note(e.message));
}
function fillBookPick(books) {           // menu « Livre » de l'écran de lecture
  const sel = $("#book-pick");
  sel.replaceChildren(el("option", books.length ? "— choisir un livre —" : "Aucun livre : importe-en un", ""));
  sel.firstChild.value = "";
  for (const b of books) {
    const o = el("option", b.title + (b.status === "processing" ? " (extraction…)" : b.status === "error" ? " (erreur)" : ""));
    o.value = b.id; o.disabled = b.status === "error";
    if (st.book && st.book.id === b.id) o.selected = true;
    sel.append(o);
  }
}
function el(tag, text, cls) {
  const e = document.createElement(tag);
  if (text !== undefined) e.textContent = text;
  if (cls) e.className = cls;
  return e;
}
const LANG_SHORT = { fr: "FR", en: "EN", ar: "AR", zh: "ZH", "zh-tw": "ZH", ja: "JA", ko: "KO" };
function langBadge(b) {
  const l = LANG_SHORT[b.lang] || (b.lang || "").toUpperCase();
  return b.lang && b.lang !== "fr" && b.lang !== "auto" ? (b.translate ? `${l}→FR` : l) : "";
}
function pct(b) { return b.n_segments ? Math.min(100, Math.round((100 * (b.progress_idx + 1)) / b.n_segments)) : 0; }
function bar(p) { const d = el("div", undefined, "bar"), f = el("div"); f.style.width = p + "%"; d.append(f); return d; }
const STOP = new Set(["le", "la", "les", "l", "un", "une", "des", "du", "de", "d", "the", "a", "an", "of", "et", "and"]);
function initials(t) {                          // « La ville ancienne » → VA ; « Les saisons » → S
  const words = (t.match(/[\p{L}\p{N}]+/gu) || []).filter((w) => !STOP.has(w.toLowerCase()));
  return (words.length ? words.slice(0, 2).map((w) => w[0]).join("") : (t.trim()[0] || "?")).toUpperCase();
}

function libError(e) {
  if (e instanceof NeedToken) return;
  const m = $("#lib-msg"); m.hidden = false; m.textContent = "Bibliothèque indisponible : " + e.message;
  $("#import-card").open = true;
}
async function loadLibrary() {
  const books = await jget("/books");
  $("#lib-msg").hidden = true;
  st.books = books;
  fillBookPick(books);
  clearTimeout(st.libPoll);                       // extraction ou préparation en cours : la liste se met à jour seule
  if (books.some((b) => b.status === "processing" || b.preparing) && !$("#view-library").hidden)
    st.libPoll = setTimeout(() => loadLibrary().catch(() => {}), 5000);
  renderResume(books);
  if (!st.importTouched && !books.length) $("#import-card").open = true;   // ouvert seulement s'il n'y a aucun livre
  renderPrepCards(books.filter((b) => b.preparing)).catch(() => {});
  renderBooks();
}
function renderResume(books) {
  const box = $("#resume"); box.replaceChildren();
  const b = (st.book && books.find((x) => x.id === st.book.id)) || books.find((x) => x.status === "ready" && x.progress_idx > 0);
  if (!b) return;
  const c = el("div", undefined, "card resume");
  const t = el("h3", b.title, "book-title");
  const badge = langBadge(b); if (badge) t.append(el("span", badge, "badge"));
  const p = pct(b);
  const go = el("button", st.book && st.book.id === b.id && st.playing ? "Revenir à la lecture" : "Continuer la lecture", "btn-primary");
  go.onclick = () => readBook(b.id);
  c.append(el("span", "Reprendre la lecture", "card-title"), t,
           el("p", `Passage ${b.progress_idx + 1} sur ${b.n_segments} · ${p} %`, "muted"), bar(p), go);
  box.append(c);
}
async function renderPrepCards(list) {
  const box = $("#prep-cards");
  if (!list.length) { box.replaceChildren(); return; }
  if (!st.settings) st.settings = await jget("/settings").catch(() => null);
  const cards = [];
  for (const b of list) {
    const p = await jget(`/books/${b.id}/prepare`).catch(() => null);
    if (!p || !p.whole || p.ready >= p.total) continue;      // terminé : la carte disparaît
    cards.push(prepCard(b, p));
  }
  box.replaceChildren(...cards);
}
function eta(bookId, p) {                        // temps restant à partir de la vitesse MESURÉE (jamais supposée)
  const now = Date.now(), r = st.prepRate[bookId];
  if (!r || p.ready < r.ready) { st.prepRate[bookId] = { t0: now, ready0: p.ready, ready: p.ready }; return ""; }
  r.ready = p.ready;
  const done = p.ready - r.ready0, sec = (now - r.t0) / 1000;
  if (done < 2 || sec < 20) return " · temps restant : mesure en cours…";
  const left = ((p.total - p.ready) / done) * sec;
  const txt = left > 5400 ? `~${Math.round(left / 3600)} h` : `~${Math.max(1, Math.round(left / 60))} min`;
  return ` · ${txt} restantes (${(done / (sec / 60)).toFixed(1)} passages/min)`;
}
function prepCard(b, p) {
  const c = el("div", undefined, "card prep-card prep");
  const head = el("div", undefined, "prep-head");
  const lab = el("span", undefined, "card-title");
  lab.append(el("span", undefined, "dot" + (p.paused ? " pause" : "")),
             document.createTextNode(p.paused ? "Préparation en pause" : "Préparation en cours"));
  const percent = p.total ? Math.floor((100 * p.ready) / p.total) : 0;
  head.append(lab, el("span", percent + " %", "pill"));
  const r = st.settings && st.settings.roles.translate;
  const engine = el("div", undefined, "engine");
  engine.append(el("span", b.translate && b.lang !== "fr" && r ? `Traduction : ${r.provider} · ${r.model}` : "Audio seulement (pas de traduction)"),
                el("span", p.current ? `passage ${p.current.idx + 1} · ${p.current.seconds} s` : ""));
  c.append(head, el("h3", b.title, "book-title"),
           el("p", p.paused ? p.paused : `${p.ready} / ${p.total} passages prêts${eta(b.id, p)}`, "muted"), bar(percent), engine);
  return c;
}
function renderBooks() {
  const ul = $("#books"), q = ($("#lib-search").value || "").trim().toLowerCase();
  ul.replaceChildren();
  const list = st.books.filter((b) => !q || b.title.toLowerCase().includes(q));
  for (const b of list) {
    const li = el("li", undefined, "card book");
    if (st.fresh.has(b.id)) li.classList.add("fresh");
    const info = el("div");
    const h = el("h3", b.title); const badge = langBadge(b); if (badge) h.append(el("span", badge, "badge"));
    const meta = b.status === "processing" ? `Extraction : page ${b.pages_done}/${b.pages_total}`
      : b.status === "error" ? "Erreur : " + b.error
      : `${b.pages_total} p. · ${b.n_segments} passages` + (b.progress_idx > 0 ? ` · lu à ${pct(b)} %` : "") + (b.preparing ? " · préparation en cours" : "");
    info.append(h, el("p", meta, "muted"));
    if (b.status === "ready" && b.progress_idx > 0) info.append(bar(pct(b)));
    const actions = el("div", undefined, "actions");
    const open = el("button", st.book && st.book.id === b.id ? "Reprendre" : "Lire", "btn-primary");
    open.disabled = b.status === "error";
    open.onclick = () => readBook(b.id);
    const del = el("button", "🗑", "btn-danger"); del.title = "Supprimer"; del.setAttribute("aria-label", "Supprimer " + b.title);
    del.onclick = async () => {
      if (!confirm(`Supprimer « ${b.title} » ?`)) return;
      await api("/books/" + b.id, { method: "DELETE" });
      if (st.book && st.book.id === b.id) closeBook();
      loadLibrary();
    };
    actions.append(open, del);
    info.append(actions);
    li.append(el("div", initials(b.title), "cover"), info);
    ul.append(li);
  }
  if (!list.length) ul.append(el("li", q ? "Aucun livre ne correspond." : "Aucun livre pour l'instant : appuie sur « ＋ Importer des documents » ci-dessus pour ajouter un PDF.", "card muted empty"));
}
$("#lib-search").oninput = renderBooks;
$("#import-card > summary").addEventListener("click", () => { st.importTouched = true; });   // seul un appui de l'utilisateur compte
$("#btn-search").onclick = () => {
  st.searchOpen = !st.searchOpen;
  if (st.searchOpen) showView("library");
  $("#lib-search").hidden = !st.searchOpen;
  if (st.searchOpen) $("#lib-search").focus(); else { $("#lib-search").value = ""; renderBooks(); }
};

$("#upload-form").onsubmit = async (ev) => {
  ev.preventDefault();
  const msg = $("#upload-msg");
  const fd = new FormData();
  fd.append("file", $("#file").files[0]);
  fd.append("title", $("#title").value);
  fd.append("lang", $("#lang").value);
  fd.append("translate", $("#translate").checked ? "1" : "0");
  const files = [...$("#file").files];
  const max = (st.cfg && st.cfg.max_upload_mb) || 0;
  const tooBig = files.filter((f) => max && f.size > max * 1048576);   // refus immédiat, sans rien envoyer
  const todo = files.filter((f) => !tooBig.includes(f));
  const bar = $("#upload-bar"), btn = ev.target.querySelector("button[type=submit]");
  const done = [], failed = tooBig.map((f) => `${f.name} : trop volumineux (${mo(f.size)} Mo, max ${max} Mo)`);
  bar.hidden = false; btn.disabled = true;
  try {
    for (const [i, f] of todo.entries()) {        // l'un après l'autre : plus fiable sur une connexion mobile
      const one = new FormData();
      for (const [k, v] of fd.entries()) if (k !== "file" && k !== "title") one.append(k, v);
      one.append("file", f);
      one.append("title", todo.length === 1 && files.length === 1 ? $("#title").value : "");
      const head = todo.length > 1 ? `Fichier ${i + 1}/${todo.length} « ${f.name} » — ` : "";
      bar.value = 0;
      try {
        const { id } = await uploadWithProgress(one, (sent, total) => {
          bar.value = total ? sent / total : 0;
          msg.textContent = head + (sent < total
            ? `envoi ${Math.floor((100 * sent) / total)} % (${mo(sent)} / ${mo(total)} Mo) — garde cette page ouverte`
            : "reçu, enregistrement sur le serveur…");
        });
        done.push(id); st.fresh.add(id);
      } catch (e) { failed.push(`${f.name} : ${e.message}`); }
    }
  } finally { bar.hidden = true; btn.disabled = false; }
  ev.target.reset(); updateFileInfo();
  if (done.length === 1 && files.length === 1) { msg.textContent = ""; return readBook(done[0]); }   // un seul livre : on l'ouvre
  msg.textContent = (done.length ? `${done.length} livre(s) importé(s), surligné(s) ci-dessous : choisis celui à lire.` : "Aucun livre importé.")
    + (failed.length ? " Échecs — " + failed.join(" ; ") : "");
  await loadLibrary().catch(() => {});
};

function updateFileInfo() {
  const files = [...$("#file").files];
  const total = files.reduce((n, f) => n + f.size, 0);
  $("#title").disabled = files.length > 1;
  $("#file-info").textContent = files.length > 1
    ? `${files.length} fichiers sélectionnés (${mo(total)} Mo) : chacun devient un livre, titré d'après son nom.`
    : "Tu peux choisir plusieurs fichiers à la fois" + (st.cfg ? ` (jusqu'à ${st.cfg.max_upload_mb >= 1024 ? st.cfg.max_upload_mb / 1024 + " Go" : st.cfg.max_upload_mb + " Mo"} chacun).` : ".");
}
$("#file").onchange = updateFileInfo;
$("#book-pick").onchange = (e) => { if (e.target.value) openBook(e.target.value).catch((err) => note(err.message)); };

const mo = (n) => (n / 1048576).toFixed(n < 10485760 ? 1 : 0);

/* Envoi par XMLHttpRequest : contrairement à fetch, il donne la progression de l'envoi (gros PDF). */
function uploadWithProgress(fd, onProgress) {
  if (!st.auth) { showLogin(); return Promise.reject(new NeedToken("Jeton requis")); }
  return new Promise((resolve, reject) => {
    const x = new XMLHttpRequest();
    x.open("POST", "/api/books");
    x.setRequestHeader("Authorization", "Bearer " + st.auth);
    x.upload.onprogress = (e) => { if (e.lengthComputable) onProgress(e.loaded, e.total); };
    x.onerror = () => reject(new Error("Connexion interrompue pendant l'envoi : réessaie (Wi-Fi conseillé pour un gros fichier)."));
    x.onload = () => {
      let d = {};
      try { d = JSON.parse(x.responseText); } catch { /* réponse non JSON (proxy) */ }
      if (x.status === 401) {
        st.auth = ""; localStorage.removeItem("token");
        showLogin("Jeton refusé par le serveur : vérifie-le.");
        return reject(new NeedToken("Jeton refusé"));
      }
      if (x.status === 413) return reject(new Error(typeof d.detail === "string" ? d.detail : "Fichier trop volumineux pour le serveur."));
      if (x.status < 200 || x.status >= 300) return reject(new Error(typeof d.detail === "string" ? d.detail : "Erreur " + x.status));
      resolve(d);
    };
    x.send(fd);
  });
}

/* ---------- Lecteur ---------- */
function closeBook() {
  stopSound(); st.playId++; st.playing = false;
  st.book = null; st.segs = []; st.cur = -1; clearInterval(st.poll); st.reads.clear();
  $("#player").hidden = true; document.body.classList.remove("has-player");
  $("#reader-tools").hidden = true; $("#text").replaceChildren(); $("#book-title").textContent = "Aucun livre ouvert";
  note("");
}
async function openBook(id) {
  closeBook();
  st.book = await jget("/books/" + id);
  st.lastPage = 0; trimCache(-100); st.ahead = null; if (st.prepPlay) cancelPrepPlay();
  $("#book-title").textContent = st.book.title;
  $("#chk-translate").checked = !!st.book.translate;
  refreshPrep().catch(() => {});
  jget("/books").then(fillBookPick).catch(() => {});
  api(`/books/${id}/warmup`, { method: "POST" }).catch(() => {});   // modèle chargé avant qu'on en ait besoin
  await loadMoreSegments();
  showShortHint();
  $("#player").hidden = false; document.body.classList.add("has-player"); $("#reader-tools").hidden = false;
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
/* ---------- Préparation à l'avance (traduction + audio côté serveur) ---------- */
function showPrep(p) {
  const percent = p.total ? Math.floor((100 * p.ready) / p.total) : 0;
  $("#btn-prep").textContent = p.running && p.whole ? "⏹ Arrêter la préparation" : "⚡ Préparer tout le livre à l'avance";
  $("#prep-pct").textContent = percent + " %";
  $("#prep-fill").style.width = percent + "%";
  st.ahead = p.ahead; updateMiniPlayer();
  $("#prep-msg").textContent = (p.ahead !== undefined ? `Réserve : ${p.ahead} passage${p.ahead > 1 ? "s" : ""} prêt${p.ahead > 1 ? "s" : ""} devant toi · ` : "")
    + `${p.ready} / ${p.total} au total`
    + (p.paused ? ` — en pause : ${p.paused}` : p.current ? ` — passage ${p.current.idx + 1} en cours depuis ${p.current.seconds} s`
       : p.running ? (p.whole ? " — préparation en cours, même écran éteint ou app fermée" : " — les suivants se préparent") : "")
    + (p.running && st.book ? eta(st.book.id, p) : "")
    + (p.error ? ` — arrêtée : ${p.error}` : "");
  clearTimeout(st.prepPoll);
  if (p.running && st.book && (st.playing || !$("#view-reader").hidden))     // aussi pendant l'écoute, sur tout onglet
    st.prepPoll = setTimeout(() => refreshPrep().catch(() => {}), 5000);
}
const posIdx = () => (st.cur >= 0 ? st.cur : (st.book ? st.book.progress_idx : 0));
async function refreshPrep() { if (st.book) showPrep(await jget(`/books/${st.book.id}/prepare?from_idx=${posIdx()}`)); }
$("#btn-prep").onclick = async () => {
  if (!st.book) return;
  const running = $("#btn-prep").textContent.startsWith("⏹");
  try { showPrep(await jpost(`/books/${st.book.id}/prepare`, { stop: running, from_idx: posIdx() })); }
  catch (e) { $("#prep-msg").textContent = e.message; }
};
/* « Préparer et lire » : prépare dans l'ordre depuis la position, attend une petite réserve, puis lit pendant que
   la suite continue de se préparer. Le lecteur audio reste actif pendant l'attente (lecture sans 2e appui). */
$("#btn-prep-play").onclick = async () => {
  if (!st.book) return;
  if (st.prepPlay) { cancelPrepPlay(); return; }
  unlockAudio();                                          // dans le geste : le son pourra partir tout seul
  const from = posIdx(), translate = st.book.translate && st.book.lang !== "fr";
  const want = translate ? 4 : 2;
  st.prepPlay = true; $("#btn-prep-play").textContent = "✕ Annuler";
  try {
    let p = await jpost(`/books/${st.book.id}/prepare`, { from_idx: from });
    showPrep(p);
    for (;;) {
      const need = Math.min(want, p.total - from);
      note(`Préparation de la réserve : ${Math.min(p.ahead, need)}/${need} passage${need > 1 ? "s" : ""} — la lecture démarre dès qu'ils sont prêts`
        + (p.paused ? ` (en pause : ${p.paused})` : ""));
      st.waitMsg = p.paused ? "En pause : indexation en cours" : `Préparation ${Math.min(p.ahead, need)}/${need} · lecture dès que prêt`;
      updateMiniPlayer();
      if (p.ahead >= need || p.error) break;
      await new Promise((r) => setTimeout(r, 2000));
      if (!st.prepPlay) return;
      p = await jget(`/books/${st.book.id}/prepare?from_idx=${from}`);
      showPrep(p);
    }
    if (p.error) { note("Préparation arrêtée : " + p.error); return; }
    updateStatus(); play(from);
  } catch (e) { note(e.message); }
  finally { st.prepPlay = false; st.waitMsg = ""; updateMiniPlayer(); $("#btn-prep-play").textContent = "▶ Préparer et lire"; }
};
function cancelPrepPlay() {
  st.prepPlay = false; st.waitMsg = ""; updateMiniPlayer(); $("#btn-prep-play").textContent = "▶ Préparer et lire";
  if (!st.playing) stopSound();
  updateStatus();
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
  if (s.page !== st.lastPage) { $("#text").append(el("div", "Page " + s.page, "page-tag")); st.lastPage = s.page; }
  const c = el("article", undefined, "seg");
  c.dataset.idx = s.idx;
  const head = el("div", undefined, "seg-head");
  const live = el("span", undefined, "live");
  head.append(el("span", "Passage " + (s.idx + 1)), live);
  c.append(head, el("p", "", "orig"), el("div", undefined, "sep"), el("p", "", "t"));
  c.onclick = () => { if (String(getSelection()).trim()) return; unlockAudio(); play(s.idx); };
  $("#text").append(c);
  st.segs.push(s);
  paint(s.idx);
}
/* Carte bilingue : original (discret) puis traduction française en serif. Sans traduction : le texte seul. */
function paint(idx) {
  const s = st.segs[idx], c = segEl(idx);
  if (!s || !c) return;
  const t = c.querySelector(".t");
  t.textContent = s.text;
  t.classList.toggle("pending", !!s.pending);
  const bi = !!(s.translated || s.pending);
  c.classList.toggle("bi", bi);
  c.querySelector(".orig").textContent = bi ? s.original : "";
  c.querySelector(".warn")?.remove();
  if (s.error) c.append(el("span", "⚠ " + s.error, "warn"));
}
const segEl = (idx) => document.querySelector(`#text .seg[data-idx="${idx}"]`);
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
  document.querySelector("#text .seg.active")?.classList.remove("active");
  const p = segEl(idx);
  if (!p) return;
  p.classList.add("active");
  if (scroll !== false) p.scrollIntoView({ block: "center", behavior: "smooth" });
  st.cur = idx;
  $("#pos").textContent = `p.${st.segs[idx].page} · ${idx + 1}/${st.segs.length}`;
  updateMiniPlayer();
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
function voiceLabel() {
  const s = st.segs[st.cur];
  if (!st.cfg || !st.cfg.server_audio) return "Voix de l'appareil";
  // langue qui sera LUE : français si le passage est (ou sera) traduit, sinon sa propre langue
  const willTranslate = s && st.book.translate && !["fr", "und"].includes(s.lang);
  const lang = !s || s.translated || willTranslate || s.lang === "und" ? "fr" : s.lang;
  const names = VOICE_NAMES[lang];
  if (!names) return "Pas de voix serveur pour cette langue";
  const g = names[st.cfg.voice] ? st.cfg.voice : Object.keys(names)[0];
  return `Piper · ${names[g]} (${g}, ${lang.toUpperCase()})`;
}
function updateMiniPlayer() {
  if (!st.book) return;
  const s = st.segs[st.cur];
  $("#mp-title").textContent = st.book.title + (s ? ` · p.${s.page}` : "");
  $("#btn-mp-speed").textContent = st.speed + "×";
  $("#btn-mp-voice").textContent = st.cfg && st.cfg.voice === "homme" ? "👨" : "👩";
  $("#btn-mp-voice").hidden = !(st.cfg && st.cfg.server_audio);
  const sub = $("#mp-sub");
  if (st.waitMsg) { sub.replaceChildren(el("b", st.waitMsg)); return; }       // « Préparer et lire » en attente
  sub.replaceChildren(el("b", voiceLabel()), document.createTextNode(` · ${st.speed}×`
    + (st.ahead !== undefined && st.ahead !== null ? ` · réserve ${st.ahead}` : s ? ` · ${st.cur + 1}/${st.segs.length}` : "")));
  const live = document.querySelector("#text .seg.active .live");
  if (live) { live.replaceChildren(); if (st.playing) live.append(el("span", undefined, "ping"), document.createTextNode("Lecture audio en cours")); }
}
function setPlayIcon() {
  $("#ico-play").hidden = st.playing; $("#ico-pause").hidden = !st.playing;
  $("#btn-play").setAttribute("aria-label", st.playing ? "Pause" : "Lecture");
  updateMiniPlayer();
  if ("mediaSession" in navigator) navigator.mediaSession.playbackState = st.playing ? "playing" : "paused";
}

async function play(idx) {
  if (!st.book || idx < 0 || idx >= st.segs.length) return;
  const id = ++st.playId;
  speechSynthesis.cancel();
  if (st.cfg.server_audio) startFiller(); else audio.pause();      // jamais de silence entre deux passages
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
      const slow = setTimeout(() => { if (id === st.playId) note("Préparation de l'audio…"); }, 600);
      const url = await audioUrl(idx).finally(() => clearTimeout(slow));
      if (id !== st.playId) return;
      audio.loop = false; audio.src = url; audio.playbackRate = st.speed;
      try { await audio.play(); }
      catch (e) {
        if (e.name !== "NotAllowedError") throw e;
        st.playing = false; setPlayIcon();
        note("Le navigateur a bloqué le son : appuie de nouveau sur ▶️ (l'audio est prêt).");
        return;
      }
      if (!info.error) updateStatus();
      for (const k of [1, 2, 3]) audioUrl(idx + k).catch(() => {});         // 3 passages d'avance
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
    st.playing = false; setPlayIcon(); stopSound(); note(e.message);
  }
}
// Son de remplissage : 25 Hz à -50 dB (inaudible sur un haut-parleur de téléphone), joué en boucle
// 1) PENDANT l'appui : il autorise ce lecteur audio pour la suite (Safari/iOS refuse sinon un play() tardif) ;
// 2) ENTRE deux passages (synthèse/traduction du suivant) : la lecture ne s'interrompt jamais, donc Android/iOS
//    ne mettent pas l'app en sommeil écran éteint. Seul ⏸ (ici ou sur l'écran verrouillé) arrête la lecture.
const FILLER = (() => {
  const rate = 8000, n = rate, buf = new ArrayBuffer(44 + n * 2), v = new DataView(buf);
  const w = (o, t) => [...t].forEach((c, i) => v.setUint8(o + i, c.charCodeAt(0)));
  w(0, "RIFF"); v.setUint32(4, 36 + n * 2, true); w(8, "WAVEfmt "); v.setUint32(16, 16, true);
  v.setUint16(20, 1, true); v.setUint16(22, 1, true); v.setUint32(24, rate, true); v.setUint32(28, rate * 2, true);
  v.setUint16(32, 2, true); v.setUint16(34, 16, true); w(36, "data"); v.setUint32(40, n * 2, true);
  for (let i = 0; i < n; i++) v.setInt16(44 + i * 2, Math.round(100 * Math.sin((2 * Math.PI * 25 * i) / rate)), true);
  return URL.createObjectURL(new Blob([buf], { type: "audio/wav" }));
})();
const isFiller = () => audio.src === FILLER;
function startFiller() {        // à appeler dans le geste (déverrouillage) et entre deux passages
  if (!st.cfg || !st.cfg.server_audio) return;
  if (!isFiller()) { audio.loop = true; audio.src = FILLER; }
  const p = audio.play();
  if (p && p.catch) p.catch(() => {});
}
const unlockAudio = startFiller;
function togglePlay() {
  if (!st.book) return;
  unlockAudio();
  if (st.prepPlay) { cancelPrepPlay(); return; }
  if (st.playing) { st.playId++; st.playing = false; stopSound(); setPlayIcon(); return; }
  if (st.cfg.server_audio && audio.src && !isFiller() && !audio.ended && audio.currentTime > 0) {
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
  st.playing = false; setPlayIcon(); stopSound();
}
const prev = () => play(Math.max(0, st.cur - 1));

function setupMediaSession() {                  // boutons écran verrouillé / casque
  if (!("mediaSession" in navigator)) return;
  navigator.mediaSession.metadata = new MediaMetadata({ title: st.book.title, artist: "Lecteur de livres",
    artwork: [{ src: "icon-512.png", sizes: "512x512", type: "image/png" }] });
  navigator.mediaSession.setActionHandler("stop", () => { if (st.playing) togglePlay(); });
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
      b.onclick = () => reindexBook().then((r) => (bubble.textContent = `Indexation en cours (${r.done}/${r.total} passages déjà prêts) : réessaie dans quelques minutes.`), (e) => (bubble.textContent = e.message));
      bubble.append(el("br"), b);
    }
  }
};
async function reindexBook() { return jpost(`/books/${st.book.id}/reindex`); }

/* ---------- Options du livre (traduction) ---------- */
$("#btn-orig").onclick = (e) => {
  const mono = $("#text").classList.toggle("mono");
  e.currentTarget.setAttribute("aria-pressed", String(!mono));
  localStorage.setItem("mono", mono ? "1" : "0");
};
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
$("#btn-reindex").onclick = () => reindexBook().then((r) => note(`Indexation en cours : ${r.done}/${r.total} passages prêts.`), (e) => note(e.message));

/* ---------- Réglages ---------- */
const PROV_LABEL = { ollama: "Ollama (local, sur le VPS)", openrouter: "OpenRouter", omniroute: "OmniRoute" };
async function loadSettings() {
  st.settings = await jget("/settings"); st.models = {}; renderSettings();
  for (const r of ["translate", "chat", "embed"]) loadModels(r, { save: false });   // listes chargées d'office
}
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
  const model = el("select"); model.id = `role-${role}-model`; model.dataset.current = r.model;   // noms exacts seulement
  model.onchange = () => setRes(role, "", true);
  const free = el("label", undefined, "check"), fcb = el("input"); fcb.type = "checkbox"; fcb.id = `role-${role}-free`; fcb.checked = !s.allow_paid;
  free.append(fcb, el("span", "Afficher seulement les modèles gratuits"));
  const res = el("div", "", "role-res"); res.id = `role-${role}-res`;
  const load = el("button", "Charger les modèles"); load.type = "button";
  const test = el("button", "Tester"); test.type = "button";
  load.onclick = () => loadModels(role);
  test.onclick = () => testRole(role);
  fcb.onchange = () => fillModels(role);
  sel.onchange = () => { st.models[role] = null; loadModels(role); };   // nouvelle liste pour ce fournisseur
  const row = el("div", undefined, "row"); row.append(load, test);
  box.append(field("Fournisseur", sel), field("Modèle", model), free, row, res);
  fillModels(role);
  return box;
}
function fillModels(role) {     // liste déroulante : uniquement les noms renvoyés par le fournisseur
  const sel = $(`#role-${role}-model`);
  if (!sel) return;
  const cur = sel.value || sel.dataset.current || "", list = st.models[role], onlyFree = $(`#role-${role}-free`).checked;
  sel.replaceChildren();
  if (!list) {                  // pas encore chargée : on montre le choix actuel tel quel
    const o = el("option", cur ? `${cur} (actuel)` : "— aucun modèle —"); o.value = cur; sel.append(o);
    const h = el("option", "Charge la liste pour en choisir un autre"); h.disabled = true; sel.append(h);
    return;
  }
  const shown = list.filter((m) => !(onlyFree && m.free === false));
  if (cur && !list.some((m) => m.id === cur || m.id === cur + ":latest")) {
    const o = el("option", `⚠ ${cur} — introuvable chez ce fournisseur : choisis dans la liste`); o.value = cur; sel.append(o);
  } else if (!cur) {
    const o = el("option", "— choisis un modèle —"); o.value = ""; sel.append(o);
  }
  for (const m of shown) {
    const o = el("option", (m.free ? "🆓 " : m.free === false ? "💳 " : "") + m.id); o.value = m.id; sel.append(o);
  }
  const want = list.some((m) => m.id === cur) ? cur : list.some((m) => m.id === cur + ":latest") ? cur + ":latest" : cur;
  sel.value = want;
  if (sel.value !== want) {     // le modèle actuel est payant et masqué par le filtre : on le garde visible
    const o = el("option", "💳 " + want); o.value = want; sel.prepend(o); sel.value = want;
  }
}
function setRes(role, text, ok) {
  const r = $(`#role-${role}-res`); r.textContent = text; r.className = "role-res " + (ok ? "ok" : "err");
}
async function loadModels(role, { save = true } = {}) {
  setRes(role, "Chargement de la liste…", true);
  try {
    // n'enregistre que les URL et clés (pas encore le modèle : il peut ne pas exister chez le nouveau fournisseur)
    if (save && !(await saveSettings(true, { roles: false }))) return setRes(role, "Corrige d'abord les réglages ci-dessus.", false);
    const prov = $(`#role-${role}-prov`).value;
    st.models[role] = (await jget("/llm/models?provider=" + prov)).models;
    fillModels(role);
    const cur = $(`#role-${role}-model`).value;
    const ok = st.models[role].some((m) => m.id === cur);
    setRes(role, `${st.models[role].length} modèles chargés.` + (ok ? " Choisis dans la liste « Modèle »." :
      " ⚠ Le modèle actuel n'y figure pas : choisis-en un dans la liste, puis Enregistrer."), ok);
  } catch (e) { setRes(role, "Liste indisponible : " + e.message, false); }
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
async function saveSettings(quiet, { roles: withRoles = true } = {}) {
  const msg = $("#settings-msg");
  if ($("#set-paid").checked && !st.settings.allow_paid && !confirm("Les modèles payants peuvent être facturés sur ton compte. Activer quand même ?")) {
    $("#set-paid").checked = false; return false;
  }
  const roles = {}, keepProv = {};
  for (const r of ["translate", "chat", "embed"]) {
    keepProv[r] = { prov: $(`#role-${r}-prov`).value, model: $(`#role-${r}-model`).value };
    if (withRoles) roles[r] = { provider: keepProv[r].prov, model: keepProv[r].model.trim() };
  }
  const patch = {
    allow_paid: $("#set-paid").checked, auto_translate: $("#set-auto").checked,
    providers: {
      ollama: { base_url: $("#set-ollama-url").value.trim() },
      openrouter: keyPatch("#set-or-key"),
      omniroute: { base_url: $("#set-om-url").value.trim(), ...keyPatch("#set-om-key") },
    },
    ...(withRoles ? { roles } : {}),
  };
  try {
    st.settings = await jpost("/settings", patch, "PUT");
    if (!withRoles)             // choix en cours (pas encore enregistrés) conservés à l'écran
      for (const r of ["translate", "chat", "embed"]) st.settings.roles[r] = { provider: keepProv[r].prov, model: keepProv[r].model };
    renderSettings();
    for (const r of ["translate", "chat", "embed"]) fillModels(r);
    $("#settings-msg").textContent = quiet ? "" : "✔ Enregistré";
    if (st.cfg) st.cfg.auto_translate = st.settings.auto_translate;
    return true;
  } catch (e) { msg.textContent = "✘ " + e.message; return false; }
}
$("#settings-form").onsubmit = (ev) => { ev.preventDefault(); saveSettings(false); };

/* ---------- Passages trop courts → regroupement en paragraphes ---------- */
function showShortHint() {
  const n = st.segs.length, avg = n ? st.segs.reduce((t, x) => t + x.original.length, 0) / n : 0;
  const short = st.book.status === "ready" && n >= 20 && avg < 150;
  $("#short-hint").hidden = !short;
  if (short) $("#short-msg").textContent = `Les passages de ce livre sont très courts (${Math.round(avg)} caractères en moyenne, `
    + `environ ${Math.max(1, Math.round(avg / 15))} s d'écoute) : traduits phrase par phrase, ils perdent le contexte, et la `
    + "préparation ne peut pas suivre la lecture. Regroupe-les en paragraphes d'environ 30 s.";
}
async function resegment() {
  if (!st.book) return;
  const p = await jget(`/books/${st.book.id}/prepare`).catch(() => null);
  if (!confirm("Regrouper les passages en paragraphes ?\n\n• Ta position de lecture et ton vocabulaire sont conservés.\n"
    + `• Les traductions et l'audio déjà préparés${p ? ` (${p.ready} passages)` : ""} seront refaits, ainsi que l'index de la discussion.`)) return;
  clearTimeout(st.saveTimer);                 // ne pas renvoyer une position de l'ancien découpage
  stopSound(); st.playId++; st.playing = false; setPlayIcon();
  try {
    const r = await jpost(`/books/${st.book.id}/resegment`);
    const id = st.book.id;
    note(`Regroupement en cours (${r.before} passages avant)…`);
    for (let i = 0; i < 600; i++) {
      await new Promise((res) => setTimeout(res, 2000));
      const b = await jget("/books/" + id);
      if (b.status !== "processing") { await openBook(id); note(`Regroupé : ${r.before} → ${b.n_segments} passages.`); return; }
      note(`Regroupement en cours : page ${b.pages_done}/${b.pages_total}…`);
    }
  } catch (e) { note(e.message); }
}
$("#btn-reseg").onclick = resegment;
$("#btn-reseg2").onclick = resegment;

/* ---------- Vocabulaire ---------- */
function selectedInReader() {                  // { term, idx } si une sélection est faite dans une carte du texte
  const sel = getSelection(), term = String(sel).trim().replace(/\s+/g, " ");
  if (!term || term.length > 300 || !sel.rangeCount || !st.book) return null;
  const node = sel.getRangeAt(0).commonAncestorContainer;
  const card = (node.nodeType === 1 ? node : node.parentElement)?.closest("#text .seg");
  return card ? { term, idx: +card.dataset.idx } : null;
}
document.addEventListener("selectionchange", () => {
  const s = !$("#view-reader").hidden && selectedInReader();
  const b = $("#vocab-add");
  b.hidden = !s;
  if (s) b.textContent = `＋ Vocabulaire : « ${s.term.length > 24 ? s.term.slice(0, 24) + "…" : s.term} »`;
});
$("#vocab-add").onclick = async () => {
  const s = selectedInReader();
  if (!s) return;
  try {
    const r = await jpost("/vocab", { book_id: st.book.id, idx: s.idx, term: s.term });
    note(r.duplicate ? `« ${s.term} » est déjà dans ton vocabulaire.` : `« ${s.term} » ajouté au vocabulaire.`);
    getSelection().removeAllRanges();
  } catch (e) { note(e.message); }
  $("#vocab-add").hidden = true;
};
function withMark(text, term) {                 // contexte avec le terme surligné (sans HTML injecté)
  const f = document.createDocumentFragment(), i = text.toLowerCase().indexOf(term.toLowerCase());
  if (i < 0) { f.append(text); return f; }
  f.append(text.slice(0, i), el("mark", text.slice(i, i + term.length)), text.slice(i + term.length));
  return f;
}
async function loadVocab() {
  st.vocab = await jget("/vocab?q=" + encodeURIComponent($("#vocab-search").value.trim()));
  const ul = $("#vocab-list"); ul.replaceChildren();
  for (const v of st.vocab) {
    const li = el("li", undefined, "card voc");
    const q1 = el("blockquote"); q1.append(withMark(v.original, v.term));
    li.append(el("p", v.term, "term"), q1);
    if (v.translation) li.append(el("blockquote", v.translation, "tr"));
    if (v.note) li.append(el("p", v.note, "muted"));
    li.append(el("p", `${v.book_title} · p.${v.page}`, "muted"));
    const row = el("div", undefined, "row");
    const go = el("button", "Lire le passage", "btn-outline");
    go.onclick = async () => {
      if (!st.books.some((b) => b.id === v.book_id)) await jget("/books").then((b) => (st.books = b)).catch(() => {});
      if (!st.books.some((b) => b.id === v.book_id)) { go.textContent = "Livre supprimé"; go.disabled = true; return; }
      showView("reader");
      if (!st.book || st.book.id !== v.book_id) await openBook(v.book_id);
      highlight(v.idx);
    };
    const del = el("button", "🗑", "btn-danger"); del.setAttribute("aria-label", "Supprimer " + v.term);
    del.onclick = async () => { await api("/vocab/" + v.id, { method: "DELETE" }); loadVocab().catch(() => {}); };
    row.append(go, del); li.append(row);
    ul.append(li);
  }
  if (!st.vocab.length) ul.append(el("li", $("#vocab-search").value ? "Aucun mot ne correspond." : "Ton vocabulaire est vide pour l'instant.", "muted"));
}
let vocabTimer;
$("#vocab-search").oninput = () => { clearTimeout(vocabTimer); vocabTimer = setTimeout(() => loadVocab().catch(() => {}), 250); };
$("#btn-vocab-csv").onclick = async () => {
  const r = await api("/vocab.csv");
  if (!r.ok) return;
  const a = el("a"); a.href = URL.createObjectURL(await r.blob()); a.download = "vocabulaire.csv";
  document.body.append(a); a.click(); a.remove(); setTimeout(() => URL.revokeObjectURL(a.href), 5000);
};

/* ---------- Câblage ---------- */
for (const t of document.querySelectorAll(".tab")) t.onclick = () => showView(t.dataset.view);
$("#btn-play").onclick = togglePlay;
$("#voice").onchange = async (e) => {
  try { await jpost("/settings", { voice: e.target.value }, "PUT"); }
  catch (err) { note(err.message); return; }
  st.cfg.voice = e.target.value;
  for (const [, p] of st.cache) p.then((u) => u && URL.revokeObjectURL(u)).catch(() => {});
  st.cache.clear();                                     // les passages préparés l'étaient avec l'autre voix
  if (st.book && st.book.lang === "ar" && e.target.value === "femme") note("Arabe : seule une voix d'homme existe.");
  updateMiniPlayer();
  if (st.playing) { unlockAudio(); play(st.cur); }
};
$("#btn-next").onclick = () => { unlockAudio(); next(); };
$("#btn-prev").onclick = () => { unlockAudio(); prev(); };
$("#speed").onchange = (e) => {
  st.speed = parseFloat(e.target.value); audio.playbackRate = st.speed; updateMiniPlayer();
  if (st.playing && !st.cfg.server_audio) play(st.cur);       // le débit vocal ne change pas en cours de phrase
};
$("#btn-listen").onclick = (e) => {
  const on = $("#text").classList.toggle("listen-only");
  e.currentTarget.setAttribute("aria-pressed", String(on));
  if (on && st.cur >= 0) segEl(st.cur)?.scrollIntoView({ block: "center" });
};
$("#btn-theme").onclick = () => {
  const dark = !document.documentElement.classList.contains("dark");
  localStorage.setItem("dark", dark ? "1" : "0"); applyTheme();
};
// Mini-lecteur : passage précédent / suivant, vitesse et voix à un appui (mêmes réglages que l'écran Lecture)
$("#btn-mp-prev").onclick = () => { unlockAudio(); prev(); };
$("#btn-mp-next").onclick = () => { unlockAudio(); next(); };
const SPEEDS = ["0.8", "1", "1.25", "1.5", "2"];
$("#btn-mp-speed").onclick = () => {
  const sel = $("#speed"); sel.value = SPEEDS[(SPEEDS.indexOf(sel.value) + 1) % SPEEDS.length];
  sel.dispatchEvent(new Event("change"));
};
$("#btn-mp-voice").onclick = () => {
  const sel = $("#voice"); sel.value = sel.value === "homme" ? "femme" : "homme";
  sel.dispatchEvent(new Event("change"));
};
matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => { if (localStorage.getItem("dark") === null) applyTheme(); });
audio.onended = () => { if (!isFiller()) next(); };
// pause imposée par le système (appel, autre app audio) : on reflète l'état au lieu de croire que ça joue encore
audio.onpause = () => { if (st.playing && !isFiller() && !audio.ended && audio.currentTime < audio.duration) { st.playing = false; setPlayIcon(); } };

async function start() {
  for (const v of document.querySelectorAll(".view")) v.hidden = v.id !== "view-library";
  for (const t of document.querySelectorAll(".tab")) t.classList.toggle("active", t.dataset.view === "library");
  await init2();
}
(async function init() {
  applyTheme();
  if (localStorage.getItem("mono") === "1") { $("#text").classList.add("mono"); $("#btn-orig").setAttribute("aria-pressed", "false"); }
  if ("serviceWorker" in navigator) navigator.serviceWorker.register("sw.js").catch(() => {});
  if (!st.auth) { showLogin(); return; }
  await init2();
})();
async function init2() {
  try {
    st.cfg = await jget("/config");
    updateFileInfo();
    $("#voice").value = st.cfg.voice || "femme";
    $("#voice").hidden = !st.cfg.server_audio;
    jget("/books").then(fillBookPick).catch(() => {});
    $("#lang").replaceChildren();
    for (const [code, label] of Object.entries(st.cfg.upload_langs)) {
      const o = el("option", label); o.value = code; $("#lang").append(o);
    }
    $("#translate").checked = st.cfg.auto_translate;
    await loadLibrary();
  }
  catch (e) { libError(e); }
}

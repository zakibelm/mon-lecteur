"""API du lecteur de livres : upload, segments, audio à la demande, progression, chat RAG + PWA."""
import logging
import re
import secrets
import shutil
import threading
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import BackgroundTasks, Depends, FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import config, db as database, detect, extract, prepare, rag, reading, settings
from .db import db
from .llm import LLMError
from . import tts
from .tts import get_provider

log = logging.getLogger("lecteur")
ALLOWED = {".pdf", ".png", ".jpg", ".jpeg"}
UPLOAD_LANGS = {"auto": "Détection automatique", "fr": "Français", "en": "Anglais", "ar": "Arabe",
                "zh": "Chinois simplifié", "zh-tw": "Chinois traditionnel", "ja": "Japonais", "ko": "Coréen"}
_HAS_LETTER = re.compile(r"[^\W\d_]", re.UNICODE)
STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
DISK_MARGIN = 500 * 2**20         # espace libre minimal laissé après la copie d'un document


@asynccontextmanager
async def lifespan(_: FastAPI):
    config.validate()      # échec fermé : pas de démarrage avec un jeton faible
    database.init()
    with db() as c:      # un redémarrage tue les tâches de fond : on ne laisse pas de livre « en cours » fantôme
        c.execute("UPDATE books SET status='error', error=? WHERE status='processing'",
                  ("Traitement interrompu par un redémarrage : réimporte le livre",))
    threading.Thread(target=_resume_indexing, name="reprise-index", daemon=True).start()
    yield


def _resume_indexing() -> None:
    """Au démarrage : termine les index de discussion interrompus (ex. redémarrage pendant l'indexation)."""
    with db() as c:
        ids = [r["id"] for r in c.execute("SELECT id FROM books WHERE status='ready' AND indexed=0 ORDER BY created_at")]
    for book_id in ids:
        log.info("Reprise de l'indexation de %s", book_id)
        _index_quietly(book_id)


app = FastAPI(title="Lecteur de livres audio", lifespan=lifespan)


def auth(authorization: str = Header(default="")):
    if not config.API_TOKEN:
        raise HTTPException(503, "API_TOKEN non configuré sur le serveur")
    if not secrets.compare_digest(authorization.encode(), f"Bearer {config.API_TOKEN}".encode()):
        raise HTTPException(401, "Token invalide")


class Progress(BaseModel):
    idx: int = Field(ge=0)


class Question(BaseModel):
    question: str = Field(min_length=1, max_length=1000)


class Options(BaseModel):
    translate: bool


class Retranslate(BaseModel):
    idx: int | None = Field(default=None, ge=0)     # None = tout le livre


class VocabIn(BaseModel):
    book_id: str = Field(min_length=1, max_length=40)
    idx: int = Field(ge=0)
    term: str = Field(min_length=1, max_length=300)
    note: str = Field(default="", max_length=1000)


class Prepare(BaseModel):
    stop: bool = False
    from_idx: int | None = Field(default=None, ge=0)     # position de lecture (sinon : progression enregistrée)


class RoleTest(BaseModel):
    role: str


def _book_or_404(c, book_id: str):
    row = c.execute("SELECT * FROM books WHERE id=?", (book_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Livre introuvable")
    return row


def _finalize_langs(book_id: str, chosen: str) -> None:
    """Langue du livre = choix explicite sinon langue majoritaire ; les segments trop courts ou indécidables
    (titres, numéros) héritent de cette langue pour ne pas déclencher de traduction inutile."""
    with db() as c:
        rows = c.execute("SELECT idx, text, lang FROM segments WHERE book_id=?", (book_id,)).fetchall()
        dom = detect.dominant_lang([(r["lang"], len(r["text"])) for r in rows])
        base = "zh" if chosen == "zh-tw" else chosen
        book_lang = base if base != "auto" else (dom if dom != "und" else "fr")
        for r in rows:
            if r["lang"] in ("zh", "ja", "ko", "ar"):
                continue
            if _HAS_LETTER.search(r["text"]) and (r["lang"] == "und" or len(r["text"]) < 40):
                c.execute("UPDATE segments SET lang=? WHERE book_id=? AND idx=?", (book_lang, book_id, r["idx"]))
        c.execute("UPDATE books SET lang=? WHERE id=?", (book_lang, book_id))


def process_book(book_id: str, src: Path, lang: str = "auto", remap: dict | None = None) -> None:
    """Tâche de fond : extraction page par page (lisible dès la page 1), détection de langue, puis indexation RAG."""
    _cover_and_meta(book_id, src)
    try:
        with db() as c:
            c.execute("UPDATE books SET pages_total=? WHERE id=?", (extract.count_pages(src), book_id))
        idx = 0
        for page_no, segs in extract.iter_pages(src, lang):
            with db() as c:
                for s in segs:
                    c.execute("INSERT INTO segments(book_id, idx, page, text, lang) VALUES(?,?,?,?,?)",
                              (book_id, idx, page_no, s, detect.detect_lang(s)))
                    idx += 1
                c.execute("UPDATE books SET pages_done=? WHERE id=?", (page_no, book_id))
        if idx:
            _finalize_langs(book_id, lang)
        with db() as c:
            if idx == 0:
                c.execute("UPDATE books SET status='error', error=? WHERE id=?",
                          ("Aucun texte extrait du document", book_id))
            else:
                c.execute("UPDATE books SET status='ready' WHERE id=?", (book_id,))
        if remap and idx:
            _remap_after_resegment(book_id, remap)
    except Exception as e:
        log.exception("Échec du traitement de %s", book_id)
        with db() as c:
            c.execute("UPDATE books SET status='error', error=? WHERE id=?", (str(e)[:300], book_id))
        return
    with db() as c:                              # démarrage instantané : 10 passages d'avance depuis la position
        row = c.execute("SELECT progress_idx, status FROM books WHERE id=?", (book_id,)).fetchone()
    if row and row["status"] == "ready":
        prepare.preparer.ahead(book_id, row["progress_idx"] - 1, 10)
    _index_quietly(book_id)


def _cover_and_meta(book_id: str, src: Path) -> Path | None:
    """Couverture (première page) + titre/auteur du PDF quand aucun titre n'a été donné à l'import."""
    out = src.parent / "cover.jpg"
    if not out.exists():
        data = extract.cover_jpeg(src)
        if data:
            out.write_bytes(data)
    t, a = extract.pdf_meta(src)
    with db() as c:
        if t:
            c.execute("UPDATE books SET title=?, title_auto=0 WHERE id=? AND title_auto=1", (t[:200], book_id))
        if a:
            c.execute("UPDATE books SET author=? WHERE id=? AND author=''", (a[:120], book_id))
    return out if out.exists() else None


def _remap_after_resegment(book_id: str, remap: dict) -> None:
    """Après un redécoupage : position de lecture = passage qui contient le même endroit du texte (en caractères) ;
    vocabulaire = passage qui contient son contexte d'origine (sinon le terme)."""
    with db() as c:
        rows = c.execute("SELECT idx, text FROM segments WHERE book_id=? ORDER BY idx", (book_id,)).fetchall()
        offset, new_pos = 0, 0
        for r in rows:
            if offset + len(r["text"]) > remap["offset"]:
                new_pos = r["idx"]; break
            offset += len(r["text"]); new_pos = r["idx"]
        c.execute("UPDATE books SET progress_idx=? WHERE id=?", (new_pos, book_id))
        for v in c.execute("SELECT id, term, original FROM vocab WHERE book_id=?", (book_id,)).fetchall():
            probe = (v["original"] or "")[:40]
            hit = next((r for r in rows if probe and probe in r["text"]), None) \
                or next((r for r in rows if v["term"].lower() in r["text"].lower()), None)
            if hit:
                c.execute("UPDATE vocab SET idx=? WHERE id=?", (hit["idx"], v["id"]))


def _index_quietly(book_id: str) -> None:
    try:
        rag.index_book(book_id)
    except Exception as e:  # le chat est optionnel : la lecture fonctionne sans LLM
        log.warning("Indexation RAG impossible pour %s : %s", book_id, e)


@app.get("/api/config", dependencies=[Depends(auth)])
def get_config():
    p = get_provider()
    return {"tts": p.name, "server_audio": p.server_audio, "voice_langs": list(config.PIPER_VOICES),
            "voices": {lg: tts.available_genders(lg) for lg in config.PIPER_VOICES},
            "voice": settings.load()["voice"],
            "upload_langs": UPLOAD_LANGS, "auto_translate": settings.load()["auto_translate"],
            "max_upload_mb": config.MAX_UPLOAD_MB}


@app.post("/api/books", dependencies=[Depends(auth)])
def upload(background: BackgroundTasks, file: UploadFile = File(...),
           title: str = Form(""), lang: str = Form("auto"), translate: str = Form("")):
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in ALLOWED:
        raise HTTPException(400, "Formats acceptés : PDF, PNG, JPG")
    if lang not in UPLOAD_LANGS:
        raise HTTPException(400, f"Langue non supportée : {lang}")
    do_translate = settings.load()["auto_translate"] if translate == "" else translate.lower() in ("1", "true", "on", "yes")
    expected = file.size or 0                       # taille annoncée (le fichier est déjà reçu, en tampon disque)
    if expected > config.MAX_UPLOAD_MB * 1024 * 1024:
        raise HTTPException(413, f"Fichier trop volumineux (max {config.MAX_UPLOAD_MB} Mo)")
    free = shutil.disk_usage(config.DATA_DIR).free
    if free < expected + DISK_MARGIN:               # copie + images OCR + audio : on garde de la marge
        raise HTTPException(507, f"Espace disque insuffisant sur le serveur : {free // 2**20} Mo libres, "
                                 f"{(expected + DISK_MARGIN) // 2**20} Mo nécessaires")
    book_id = uuid.uuid4().hex[:12]
    folder = config.DATA_DIR / "books" / book_id
    folder.mkdir(parents=True)
    src = folder / f"source{suffix}"
    limit, size, too_big = config.MAX_UPLOAD_MB * 1024 * 1024, 0, False
    with src.open("wb") as out:
        while chunk := file.file.read(1024 * 1024):
            size += len(chunk)
            if size > limit:
                too_big = True
                break
            out.write(chunk)
    if too_big:
        shutil.rmtree(folder, ignore_errors=True)
        raise HTTPException(413, f"Fichier trop volumineux (max {config.MAX_UPLOAD_MB} Mo)")
    name = title.strip() or Path(file.filename).stem
    with db() as c:
        c.execute("INSERT INTO books(id, title, lang, translate, title_auto) VALUES(?,?,?,?,?)",
                  (book_id, name[:200], lang, int(do_translate), int(not title.strip())))
    background.add_task(process_book, book_id, src, lang)
    return {"id": book_id}


@app.get("/api/books", dependencies=[Depends(auth)])
def list_books():
    with db() as c:
        rows = c.execute(
            "SELECT b.*, (SELECT COUNT(*) FROM segments s WHERE s.book_id=b.id) AS n_segments "
            "FROM books b ORDER BY created_at DESC").fetchall()
    # « préparing » = préparation complète demandée (⚡), pas la simple avance de lecture
    return [{**dict(r), "preparing": prepare.preparer.busy(r["id"]) and r["id"] in prepare.preparer.whole} for r in rows]


@app.get("/api/books/{book_id}", dependencies=[Depends(auth)])
def get_book(book_id: str):
    with db() as c:
        row = _book_or_404(c, book_id)
        n = c.execute("SELECT COUNT(*) FROM segments WHERE book_id=?", (book_id,)).fetchone()[0]
    return {**dict(row), "n_segments": n}


@app.get("/api/books/{book_id}/segments", dependencies=[Depends(auth)])
def get_segments(book_id: str, offset: int = 0, limit: int = 200):
    limit = max(1, min(limit, 500))
    with db() as c:
        book = _book_or_404(c, book_id)
        rows = c.execute(
            "SELECT s.idx, s.page, s.text AS original, s.lang, t.text AS tr FROM segments s "
            "LEFT JOIN translations t ON t.book_id=s.book_id AND t.idx=s.idx "
            "WHERE s.book_id=? AND s.idx>=? ORDER BY s.idx LIMIT ?",
            (book_id, max(0, offset), limit)).fetchall()
    out = []
    for r in rows:
        needs = reading.needs_translation(book, {"lang": r["lang"], "text": r["original"]})
        shown = r["tr"] if (needs and r["tr"]) else r["original"]
        out.append({"idx": r["idx"], "page": r["page"], "text": shown, "original": r["original"],
                    "lang": r["lang"], "translated": bool(needs and r["tr"]),
                    "pending": bool(needs and not r["tr"]),
                    "est_seconds": max(1, round(len(shown) / 15))})
    return out


@app.get("/api/books/{book_id}/segments/{idx}/read", dependencies=[Depends(auth)])
def read_segment(book_id: str, idx: int):
    """Texte à lire pour ce segment (traduit en français si besoin, avec cache) + étiquette de voix."""
    res = reading.resolve(book_id, idx)
    if res is None:
        raise HTTPException(404, "Segment introuvable")
    prepare.preparer.ahead(book_id, idx)
    return res


@app.get("/api/books/{book_id}/segments/{idx}/audio", dependencies=[Depends(auth)])
def get_audio(book_id: str, idx: int):
    if not get_provider().server_audio:
        return Response(status_code=204)          # la PWA lit le texte avec la voix de l'appareil
    try:
        path = prepare.audio_file(book_id, idx)
    except prepare.AudioError as e:
        raise HTTPException(e.status, str(e))
    prepare.preparer.ahead(book_id, idx)          # les passages suivants se préparent pendant l'écoute
    return FileResponse(path, media_type="audio/mpeg")


@app.post("/api/books/{book_id}/prepare", dependencies=[Depends(auth)])
def prepare_book(book_id: str, body: Prepare):
    """« Préparer tout le livre » : traduction + audio jusqu'à la fin, en tâche de fond (ou arrêt)."""
    with db() as c:
        book = _book_or_404(c, book_id)
    if book["status"] != "ready":
        raise HTTPException(409, "Le livre n'est pas prêt")
    start = book["progress_idx"] if body.from_idx is None else max(0, body.from_idx)
    if body.stop:
        prepare.preparer.stop(book_id)
    else:
        prepare.preparer.whole_book(book_id, start)   # dans l'ordre, à partir de la position de lecture
    return prepare.status(book_id, start)


@app.get("/api/books/{book_id}/prepare", dependencies=[Depends(auth)])
def prepare_status(book_id: str, from_idx: int | None = None):
    with db() as c:
        _book_or_404(c, book_id)
    return prepare.status(book_id, from_idx)


@app.get("/api/books/{book_id}/cover", dependencies=[Depends(auth)])
def get_cover(book_id: str):
    """Miniature de la première page ; fabriquée à la demande pour les livres importés avant cette version."""
    with db() as c:
        _book_or_404(c, book_id)
    folder = config.DATA_DIR / "books" / book_id
    path = folder / "cover.jpg"
    if not path.exists():
        src = next(folder.glob("source.*"), None)
        if not src or not _cover_and_meta(book_id, src):
            raise HTTPException(404, "Pas de couverture")
    return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=86400"})


@app.put("/api/books/{book_id}/progress", dependencies=[Depends(auth)])
def set_progress(book_id: str, body: Progress):
    with db() as c:
        _book_or_404(c, book_id)
        n = c.execute("SELECT COUNT(*) FROM segments WHERE book_id=?", (book_id,)).fetchone()[0]
        if body.idx >= max(1, n):      # position d'un ancien découpage (ex. envoyée pendant un regroupement) : ignorée
            return {"ok": False, "reason": f"position {body.idx} hors du livre ({n} passages)"}
        c.execute("UPDATE books SET progress_idx=? WHERE id=?", (body.idx, book_id))
    return {"ok": True}


@app.post("/api/books/{book_id}/chat", dependencies=[Depends(auth)])
def chat(book_id: str, body: Question):
    with db() as c:
        book = _book_or_404(c, book_id)
    if not book["indexed"]:
        done, total = rag.progress(book_id)
        if book["status"] == "processing":
            msg = "Discussion indisponible : extraction du livre en cours, l'indexation suivra."
        elif rag.is_running(book_id):
            msg = (f"Indexation en cours : {done}/{total} passages ({100 * done // max(1, total)} %). "
                   "Réessaie dans quelques minutes.")
        else:
            err = rag.last_error.get(book_id)
            msg = (f"Index incomplet ({done}/{total} passages)"
                   + (f" — dernière erreur : {err}" if err else "") + ". Appuie sur « Réindexer » pour reprendre.")
        raise HTTPException(409, msg)
    try:
        return rag.answer(book_id, body.question)
    except rag.StaleIndex as e:
        raise HTTPException(409, str(e))
    except PermissionError as e:
        raise HTTPException(403, str(e))
    except LLMError as e:
        raise HTTPException(502, f"Modèle d'IA : {e}")
    except Exception:
        log.exception("Chat en échec")
        raise HTTPException(502, "Le modèle d'IA est injoignable")


@app.put("/api/books/{book_id}/options", dependencies=[Depends(auth)])
def set_options(book_id: str, body: Options):
    with db() as c:
        _book_or_404(c, book_id)
        c.execute("UPDATE books SET translate=? WHERE id=?", (int(body.translate), book_id))
    return {"ok": True, "translate": body.translate}


@app.post("/api/books/{book_id}/retranslate", dependencies=[Depends(auth)])
def retranslate(book_id: str, body: Retranslate):
    """Efface la traduction en cache (un segment ou tout le livre) : elle sera refaite avec le modèle actuel."""
    with db() as c:
        _book_or_404(c, book_id)
        if body.idx is None:
            c.execute("DELETE FROM translations WHERE book_id=?", (book_id,))
        else:
            c.execute("DELETE FROM translations WHERE book_id=? AND idx=?", (book_id, body.idx))
    audio = config.DATA_DIR / "books" / book_id / "audio"
    if audio.exists():
        for f in audio.glob("*.mp3" if body.idx is None else f"{body.idx}-*.mp3"):
            f.unlink(missing_ok=True)
    return {"ok": True}


@app.post("/api/books/{book_id}/resegment", dependencies=[Depends(auth)])
def resegment(book_id: str, background: BackgroundTasks):
    """Redécoupe un livre déjà importé en paragraphes (depuis le fichier d'origine). Les traductions, l'audio et
    l'index de ce livre sont refaits ; la position de lecture et le vocabulaire sont recalés sur le texte."""
    with db() as c:
        book = _book_or_404(c, book_id)
        if book["status"] != "ready":
            raise HTTPException(409, "Le livre n'est pas prêt")
        rows = c.execute("SELECT text FROM segments WHERE book_id=? AND idx<? ORDER BY idx",
                         (book_id, book["progress_idx"])).fetchall()
        before = c.execute("SELECT COUNT(*) FROM segments WHERE book_id=?", (book_id,)).fetchone()[0]
    folder = config.DATA_DIR / "books" / book_id
    src = next(folder.glob("source.*"), None)
    if not src:
        raise HTTPException(404, "Fichier d'origine introuvable : réimporte le livre")
    prepare.preparer.stop(book_id)
    if rag.is_running(book_id):
        raise HTTPException(409, "Indexation en cours : réessaie dans quelques minutes")
    with db() as c:
        for t in ("embeddings", "translations", "segments"):
            c.execute(f"DELETE FROM {t} WHERE book_id=?", (book_id,))
        c.execute("UPDATE books SET status='processing', pages_done=0, indexed=0, embed_sig='', error=NULL WHERE id=?", (book_id,))
    shutil.rmtree(folder / "audio", ignore_errors=True)
    background.add_task(process_book, book_id, src, book["lang"] if book["lang"] in UPLOAD_LANGS else "auto",
                        {"offset": sum(len(r["text"]) for r in rows)})
    return {"ok": True, "before": before}


@app.post("/api/books/{book_id}/warmup", dependencies=[Depends(auth)])
def warmup(book_id: str):
    """Réveille le modèle de traduction (Ollama le décharge après 5 min sans demande ; rechargement mesuré :
    ~3 min). Ne change pas la configuration d'Ollama : une simple demande de chargement, en tâche de fond."""
    with db() as c:
        book = _book_or_404(c, book_id)
    r = settings.load()["roles"]["translate"]
    if not book["translate"] or book["lang"] in ("fr", "und") or r["provider"] != "ollama":
        return {"ok": True, "warming": False}
    threading.Thread(target=_warm_ollama, args=(r["model"],), daemon=True).start()
    return {"ok": True, "warming": True, "model": r["model"]}


def _warm_ollama(model: str) -> None:
    import httpx
    base = settings.load()["providers"]["ollama"]["base_url"].rstrip("/")
    root = base[:-3] if base.endswith("/v1") else base
    try:              # requête vide de l'API native : charge le modèle sans rien générer
        httpx.post(f"{root}/api/generate", json={"model": model, "keep_alive": "15m"}, timeout=600)
    except httpx.HTTPError as e:
        log.info("Réveil du modèle %s impossible : %s", model, e)


@app.post("/api/books/{book_id}/reindex", dependencies=[Depends(auth)])
def reindex(book_id: str, background: BackgroundTasks):
    """Reconstruit l'index de discussion (à faire après un changement de modèle d'embeddings)."""
    with db() as c:
        book = _book_or_404(c, book_id)
    if book["status"] != "ready":
        raise HTTPException(409, "Le livre n'est pas prêt")
    # reprend là où l'index s'est arrêté ; repart de zéro seulement si le modèle d'embeddings a changé
    with db() as c:
        c.execute("UPDATE books SET indexed=0 WHERE id=? AND embed_sig != ?", (book_id, settings.embed_signature()))
    if not rag.is_running(book_id):
        background.add_task(_index_quietly, book_id)
    done, total = rag.progress(book_id)
    return {"ok": True, "done": done, "total": total, "running": True}


# ---------- Réglages & fournisseurs LLM ----------
@app.get("/api/settings", dependencies=[Depends(auth)])
def get_settings():
    return settings.public_view()


@app.put("/api/settings", dependencies=[Depends(auth)])
def put_settings(patch: dict):
    try:
        return settings.update(patch)
    except PermissionError as e:
        raise HTTPException(403, str(e))
    except (ValueError, KeyError, TypeError) as e:
        raise HTTPException(400, str(e))
    except LLMError as e:
        raise HTTPException(502, str(e))


@app.get("/api/llm/models", dependencies=[Depends(auth)])
def llm_models(provider: str):
    """Liste des modèles proposés par un fournisseur (pour la liste déroulante des Réglages)."""
    try:
        models = settings.client_for_provider(provider).list_models()
    except LLMError as e:
        raise HTTPException(502, str(e))
    if provider == "ollama":
        for m in models:
            m["free"] = True
    elif provider == "openrouter":
        for m in models:
            m["free"] = bool(m["free"]) or m["id"].endswith(":free")
    return {"provider": provider, "models": models}


@app.post("/api/llm/test", dependencies=[Depends(auth)])
def llm_test(body: RoleTest):
    """Essai réel du modèle configuré pour un usage (traduction / discussion / embeddings)."""
    import time
    if body.role not in settings.ROLES:
        raise HTTPException(400, "Usage inconnu")
    t0 = time.time()
    try:
        client, model, label = settings.client_for(body.role)
        if body.role == "translate":
            from . import translate as tr
            sample = tr.clean_output(client.chat(model, [
                {"role": "system", "content": tr.SYSTEM},
                {"role": "user", "content": "Langue source : chinois.\n\nTexte à traduire :\n今年销量增长了3.5万台。"}],
                temperature=0.1))
        elif body.role == "chat":
            sample = client.chat(model, [{"role": "user", "content": "Réponds uniquement par le mot : OK"}]).strip()[:80]
        else:
            sample = f"{client.embed(model, ['test']).shape[1]} dimensions"
    except PermissionError as e:
        raise HTTPException(403, str(e))
    except LLMError as e:
        raise HTTPException(502, str(e))
    return {"ok": True, "model": label, "sample": sample, "ms": int((time.time() - t0) * 1000)}


# ---------- Vocabulaire ----------
@app.get("/api/vocab", dependencies=[Depends(auth)])
def vocab_list(q: str = ""):
    with db() as c:
        rows = c.execute("SELECT * FROM vocab WHERE term LIKE ? OR original LIKE ? OR translation LIKE ? "
                         "ORDER BY id DESC LIMIT 2000", (f"%{q}%", f"%{q}%", f"%{q}%")).fetchall()
    return [dict(r) for r in rows]


@app.post("/api/vocab", dependencies=[Depends(auth)])
def vocab_add(body: VocabIn):
    term = " ".join(body.term.split())
    with db() as c:
        book = _book_or_404(c, body.book_id)
        seg = c.execute("SELECT s.page, s.text, t.text AS tr FROM segments s LEFT JOIN translations t "
                        "ON t.book_id=s.book_id AND t.idx=s.idx WHERE s.book_id=? AND s.idx=?",
                        (body.book_id, body.idx)).fetchone()
        if not seg:
            raise HTTPException(404, "Passage introuvable")
        dup = c.execute("SELECT id FROM vocab WHERE book_id=? AND idx=? AND term=?", (body.book_id, body.idx, term)).fetchone()
        if dup:
            return {"id": dup["id"], "duplicate": True}
        cur = c.execute("INSERT INTO vocab(term, book_id, book_title, idx, page, original, translation, note) "
                        "VALUES(?,?,?,?,?,?,?,?)", (term, body.book_id, book["title"], body.idx, seg["page"],
                                                     seg["text"], seg["tr"] or "", body.note.strip()))
    return {"id": cur.lastrowid, "duplicate": False}


@app.delete("/api/vocab/{vocab_id}", dependencies=[Depends(auth)])
def vocab_delete(vocab_id: int):
    with db() as c:
        if not c.execute("DELETE FROM vocab WHERE id=?", (vocab_id,)).rowcount:
            raise HTTPException(404, "Entrée introuvable")
    return {"ok": True}


@app.get("/api/vocab.csv", dependencies=[Depends(auth)])
def vocab_csv():
    import csv
    import io
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(["terme", "passage d'origine", "traduction", "livre", "page", "note", "ajouté le"])
    with db() as c:
        for r in c.execute("SELECT * FROM vocab ORDER BY id"):
            w.writerow([r["term"], r["original"], r["translation"], r["book_title"], r["page"], r["note"], r["created_at"]])
    return Response("\ufeff" + buf.getvalue(), media_type="text/csv; charset=utf-8",      # BOM : accents corrects dans Excel
                    headers={"Content-Disposition": 'attachment; filename="vocabulaire.csv"'})


@app.delete("/api/books/{book_id}", dependencies=[Depends(auth)])
def delete_book(book_id: str):
    with db() as c:
        _book_or_404(c, book_id)
        for t in ("embeddings", "translations", "segments"):
            c.execute(f"DELETE FROM {t} WHERE book_id=?", (book_id,))
        c.execute("DELETE FROM books WHERE id=?", (book_id,))
    shutil.rmtree(config.DATA_DIR / "books" / book_id, ignore_errors=True)
    return {"ok": True}


app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")

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


class Prepare(BaseModel):
    stop: bool = False


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


def process_book(book_id: str, src: Path, lang: str = "auto") -> None:
    """Tâche de fond : extraction page par page (lisible dès la page 1), détection de langue, puis indexation RAG."""
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
    except Exception as e:
        log.exception("Échec du traitement de %s", book_id)
        with db() as c:
            c.execute("UPDATE books SET status='error', error=? WHERE id=?", (str(e)[:300], book_id))
        return
    prepare.preparer.ahead(book_id, -1, 10)       # démarrage instantané : les 10 premiers passages d'avance
    _index_quietly(book_id)


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
        c.execute("INSERT INTO books(id, title, lang, translate) VALUES(?,?,?,?)",
                  (book_id, name[:200], lang, int(do_translate)))
    background.add_task(process_book, book_id, src, lang)
    return {"id": book_id}


@app.get("/api/books", dependencies=[Depends(auth)])
def list_books():
    with db() as c:
        rows = c.execute(
            "SELECT b.*, (SELECT COUNT(*) FROM segments s WHERE s.book_id=b.id) AS n_segments "
            "FROM books b ORDER BY created_at DESC").fetchall()
    return [dict(r) for r in rows]


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
    if body.stop:
        prepare.preparer.stop(book_id)
    else:
        prepare.preparer.whole_book(book_id, book["progress_idx"])
    return prepare.status(book_id)


@app.get("/api/books/{book_id}/prepare", dependencies=[Depends(auth)])
def prepare_status(book_id: str):
    with db() as c:
        _book_or_404(c, book_id)
    return prepare.status(book_id)


@app.put("/api/books/{book_id}/progress", dependencies=[Depends(auth)])
def set_progress(book_id: str, body: Progress):
    with db() as c:
        _book_or_404(c, book_id)
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

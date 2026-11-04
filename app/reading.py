"""Texte « à lire » d'un segment : original ou traduction française (mise en cache en base)."""
import logging
import re
import threading

from . import translate as tr
from .db import db
from .detect import SPEAK_TAG
from .llm import LLMError

log = logging.getLogger("reading")
_lock = threading.Lock()          # une traduction à la fois : ménage Ollama et les quotas gratuits
_HAS_LETTER = re.compile(r"[^\W\d_]", re.UNICODE)
NO_TRANSLATE = ("fr", "und")


def _row(c, book_id: str, idx: int):
    book = c.execute("SELECT * FROM books WHERE id=?", (book_id,)).fetchone()
    seg = c.execute("SELECT idx, page, text, lang FROM segments WHERE book_id=? AND idx=?",
                    (book_id, idx)).fetchone()
    return book, seg


def _cached(c, book_id: str, idx: int):
    return c.execute("SELECT text, model FROM translations WHERE book_id=? AND idx=?",
                     (book_id, idx)).fetchone()


def needs_translation(book, seg) -> bool:
    return bool(book["translate"]) and seg["lang"] not in NO_TRANSLATE and bool(_HAS_LETTER.search(seg["text"]))


def _result(seg, *, text, translated, model="", error="", voice_lang=None):
    lang = seg["lang"]
    vl = voice_lang or ("fr" if translated else (lang if lang != "und" else "fr"))
    return {"idx": seg["idx"], "page": seg["page"], "text": text, "original": seg["text"], "lang": lang,
            "translated": translated, "model": model, "error": error, "voice_lang": vl,
            "speak_tag": SPEAK_TAG.get(vl, vl)}


def resolve(book_id: str, idx: int) -> dict | None:
    """None si le segment n'existe pas. En cas d'échec de traduction : original + message d'erreur."""
    with db() as c:
        book, seg = _row(c, book_id, idx)
        if not book or not seg:
            return None
        if not needs_translation(book, seg):
            return _result(seg, text=seg["text"], translated=False)
        hit = _cached(c, book_id, idx)
    if hit:
        return _result(seg, text=hit["text"], translated=True, model=hit["model"])
    with _lock:
        with db() as c:                                # un autre appel a pu finir pendant l'attente
            hit = _cached(c, book_id, idx)
            prev_src = c.execute("SELECT text FROM segments WHERE book_id=? AND idx=?",
                                 (book_id, idx - 1)).fetchone()
            prev_tr = _cached(c, book_id, idx - 1) if idx > 0 else None
        if hit:
            return _result(seg, text=hit["text"], translated=True, model=hit["model"])
        prev = (prev_src["text"], prev_tr["text"]) if prev_src and prev_tr else None
        try:
            out, label = tr.translate(seg["text"], seg["lang"], prev)
        except (LLMError, PermissionError) as e:
            log.warning("Traduction impossible (%s #%s) : %s", book_id, idx, e)
            return _result(seg, text=seg["text"], translated=False, error=str(e))
        except Exception as e:                         # filet de sécurité : la lecture ne doit jamais planter
            log.exception("Traduction en échec (%s #%s)", book_id, idx)
            return _result(seg, text=seg["text"], translated=False, error=str(e)[:200])
        with db() as c:
            c.execute("INSERT OR REPLACE INTO translations VALUES(?,?,?,?)", (book_id, idx, out, label))
    return _result(seg, text=out, translated=True, model=label)

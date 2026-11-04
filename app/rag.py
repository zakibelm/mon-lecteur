"""Discussion avec le livre : embeddings + recherche par similarité + LLM (fournisseur choisi dans Réglages)."""
import logging
import threading

import numpy as np

from . import settings
from .db import db

log = logging.getLogger("rag")
SYSTEM = (
    "Tu réponds aux questions sur un livre en t'appuyant UNIQUEMENT sur les extraits fournis. "
    "Si la réponse n'y figure pas, dis-le clairement. Réponds dans la langue de la question, "
    "de façon concise, et cite le numéro de page entre parenthèses quand c'est utile."
)


class StaleIndex(RuntimeError):
    """L'index a été construit avec un autre modèle d'embeddings : il faut réindexer."""


_running: set[str] = set()
_run_lock = threading.Lock()
last_error: dict[str, str] = {}


def is_running(book_id: str) -> bool:
    return book_id in _running


def progress(book_id: str) -> tuple[int, int]:
    """(passages indexés, passages du livre)."""
    with db() as c:
        done = c.execute("SELECT COUNT(*) FROM embeddings WHERE book_id=?", (book_id,)).fetchone()[0]
        total = c.execute("SELECT COUNT(*) FROM segments WHERE book_id=?", (book_id,)).fetchone()[0]
    return done, total


def index_book(book_id: str, batch: int = 16) -> None:
    """Embeddings du texte ORIGINAL (un modèle multilingue comme bge-m3 gère la recherche inter-langues).
    Reprend là où une indexation interrompue s'est arrêtée (redémarrage, panne d'Ollama) ; ne recommence de
    zéro que si le modèle d'embeddings a changé. Une seule indexation à la fois par livre."""
    with _run_lock:
        if book_id in _running:
            return
        _running.add(book_id)
    try:
        _index(book_id, batch)
        last_error.pop(book_id, None)
    except Exception as e:
        last_error[book_id] = str(e)[:200]
        raise
    finally:
        _running.discard(book_id)


def _index(book_id: str, batch: int) -> None:
    client, model, label = settings.client_for("embed")
    with db() as c:
        sig = c.execute("SELECT embed_sig FROM books WHERE id=?", (book_id,)).fetchone()["embed_sig"]
        if sig != label:                     # autre modèle (ou jamais indexé) : on repart de zéro
            c.execute("DELETE FROM embeddings WHERE book_id=?", (book_id,))
            c.execute("UPDATE books SET indexed=0, embed_sig=? WHERE id=?", (label, book_id))
        rows = c.execute(
            "SELECT idx, text FROM segments WHERE book_id=? AND idx NOT IN "
            "(SELECT idx FROM embeddings WHERE book_id=?) ORDER BY idx", (book_id, book_id)).fetchall()
    for i in range(0, len(rows), batch):
        part = rows[i:i + batch]
        vecs = client.embed(model, [r["text"] for r in part])
        with db() as c:
            c.executemany("INSERT OR REPLACE INTO embeddings VALUES(?,?,?)",
                          [(book_id, r["idx"], v.tobytes()) for r, v in zip(part, vecs)])
    with db() as c:
        c.execute("UPDATE books SET indexed=1, embed_sig=? WHERE id=?", (label, book_id))


def reset_index(book_id: str) -> None:
    with db() as c:
        c.execute("DELETE FROM embeddings WHERE book_id=?", (book_id,))
        c.execute("UPDATE books SET indexed=0, embed_sig='' WHERE id=?", (book_id,))


def answer(book_id: str, question: str, k: int = 6) -> dict:
    with db() as c:
        book = c.execute("SELECT embed_sig FROM books WHERE id=?", (book_id,)).fetchone()
        emb = c.execute("SELECT idx, vec FROM embeddings WHERE book_id=? ORDER BY idx", (book_id,)).fetchall()
    if not emb:
        raise LookupError("Livre non indexé")
    if book["embed_sig"] != settings.embed_signature():
        raise StaleIndex(f"L'index a été créé avec « {book['embed_sig']} » ; le modèle d'embeddings a changé. "
                         "Relance l'indexation du livre.")
    m = np.vstack([np.frombuffer(r["vec"], dtype=np.float32) for r in emb])
    ec, em, _ = settings.client_for("embed")
    q = ec.embed(em, [question])[0]
    top = np.argsort(-(m @ q))[:k]
    idxs = sorted(int(emb[i]["idx"]) for i in top)
    with db() as c:
        segs = [c.execute(
            "SELECT s.idx, s.page, s.text AS original, COALESCE(t.text, s.text) AS text "
            "FROM segments s LEFT JOIN translations t ON t.book_id=s.book_id AND t.idx=s.idx "
            "WHERE s.book_id=? AND s.idx=?", (book_id, i)).fetchone() for i in idxs]
    context = "\n\n".join(f"[p.{s['page']}] {s['text']}" for s in segs)
    cc, cm, _ = settings.client_for("chat")
    reply = cc.chat(cm, [{"role": "system", "content": SYSTEM},
                         {"role": "user", "content": f"Extraits :\n{context}\n\nQuestion : {question}"}],
                    temperature=0.2)
    return {"answer": reply,
            "sources": [{"idx": s["idx"], "page": s["page"], "text": s["text"][:160]} for s in segs]}

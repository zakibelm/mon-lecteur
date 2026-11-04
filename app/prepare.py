"""Préparation EN AVANCE : traduction (par paragraphe) puis audio des passages à venir, en tâche de fond.

But : qu'au moment où la lecture arrive sur un passage, sa traduction et son audio soient déjà prêts.
- ahead(livre, idx)     : appelé à chaque lecture d'un passage → prépare les LOOKAHEAD suivants ;
- whole_book(livre)     : bouton « Préparer tout le livre » (ex. la nuit) → prépare jusqu'à la fin.
Un seul travailleur (le CPU du VPS est partagé avec Ollama et Piper) ; la position de lecture la plus récente
passe toujours en premier.
"""
import hashlib
import logging
import threading
import time
from pathlib import Path

from . import config, rag, reading, settings, tts
from .db import db

log = logging.getLogger("prepare")
LOOKAHEAD = 6


class AudioError(RuntimeError):
    def __init__(self, status: int, msg: str):
        super().__init__(msg)
        self.status = status


def audio_file(book_id: str, idx: int) -> Path:
    """Chemin du MP3 du passage (généré s'il manque). Lève AudioError(statut HTTP, message)."""
    provider = tts.get_provider()
    res = reading.resolve(book_id, idx)
    if res is None:
        raise AudioError(404, "Segment introuvable")
    if res["error"]:
        raise AudioError(502, f"Traduction impossible : {res['error']}")
    voice = res["voice_lang"]
    if voice not in config.PIPER_VOICES:
        raise AudioError(422, f"Pas de voix serveur pour « {voice} » : active la traduction vers le français "
                              "ou lis avec la voix de l'appareil")
    gender = settings.load()["voice"]
    model = tts.voice_path(voice, gender)
    key = hashlib.sha1(f"{model}|{res['text']}".encode()).hexdigest()[:10]   # autre texte ou autre voix → autre fichier
    path = config.DATA_DIR / "books" / book_id / "audio" / f"{idx}-{key}.mp3"
    if not path.exists():
        path.parent.mkdir(exist_ok=True)
        try:
            provider.synthesize(res["text"], voice, path, gender)
        except Exception as e:
            log.exception("TTS en échec (%s #%s)", book_id, idx)
            raise AudioError(500, "Échec de la synthèse vocale") from e
    return path


class Preparer:
    def __init__(self):
        self._cv = threading.Condition()
        self._jobs: dict[str, list[int]] = {}       # livre -> [prochain idx, fin exclue]
        self._order: list[str] = []                 # livre le plus récemment lu en premier
        self.whole: set[str] = set()                # livres en préparation complète
        self.errors: dict[str, str] = {}
        self._thread: threading.Thread | None = None
        self.enabled = True                         # tests : désactivé pour garder des comptages déterministes
        self.current: tuple[str, int, float] | None = None   # (livre, passage, début) : ce qui tourne maintenant
        self.paused_for = ""                         # livre dont l'indexation passe avant la préparation

    def _start(self):
        if not self._thread or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._run, name="preparation", daemon=True)
            self._thread.start()

    def _push(self, book_id: str, start: int, end: int):
        if not self.enabled:
            return
        with self._cv:
            cur = self._jobs.get(book_id)
            keep_end = cur[1] if cur and book_id in self.whole else 0
            self._jobs[book_id] = [start, max(end, keep_end)]
            if book_id in self._order:
                self._order.remove(book_id)
            self._order.insert(0, book_id)
            self._cv.notify()
        self._start()

    def ahead(self, book_id: str, idx: int, n: int = LOOKAHEAD):
        self._push(book_id, idx + 1, idx + 1 + n)

    def whole_book(self, book_id: str, from_idx: int = 0):
        with db() as c:
            total = c.execute("SELECT COUNT(*) FROM segments WHERE book_id=?", (book_id,)).fetchone()[0]
        self.whole.add(book_id)
        self.errors.pop(book_id, None)
        self._push(book_id, from_idx, total)

    def stop(self, book_id: str):
        with self._cv:
            self._jobs.pop(book_id, None)
            self.whole.discard(book_id)
            if book_id in self._order:
                self._order.remove(book_id)

    def busy(self, book_id: str) -> bool:
        return book_id in self._jobs

    def _next(self):
        with self._cv:
            while True:
                # la discussion d'abord : pendant une indexation, Ollama est saturé (mesuré : 350 → 10 passages/min)
                if rag._running and any(j[0] < j[1] for j in self._jobs.values()):
                    self.paused_for = next(iter(rag._running), "")
                    self._cv.wait(timeout=2)
                    continue
                self.paused_for = ""
                for b in self._order:
                    job = self._jobs.get(b)
                    if job and job[0] < job[1]:
                        idx = job[0]; job[0] += 1
                        return b, idx
                    self._jobs.pop(b, None)
                self._order = [b for b in self._order if b in self._jobs]
                self.whole &= set(self._jobs)
                self._cv.wait(timeout=30)

    def _run(self):
        while True:
            book_id, idx = self._next()
            self.current = (book_id, idx, time.time())
            try:
                with db() as c:
                    book = c.execute("SELECT status FROM books WHERE id=?", (book_id,)).fetchone()
                if not book:
                    self.stop(book_id); continue
                if tts.get_provider().server_audio:
                    audio_file(book_id, idx)              # traduit (par paragraphe) puis fabrique l'audio
                else:
                    reading.resolve(book_id, idx)         # voix de l'appareil : la traduction suffit
            except AudioError as e:
                if e.status in (502, 500):                # modèle ou Piper en panne : on n'insiste pas
                    self.errors[book_id] = str(e); self.stop(book_id)
            except Exception as e:  # noqa: BLE001 — la préparation ne doit jamais tuer le serveur
                log.warning("Préparation impossible (%s #%s) : %s", book_id, idx, e)
                self.errors[book_id] = str(e)[:200]; self.stop(book_id)
            finally:
                self.current = None


preparer = Preparer()


def status(book_id: str, from_idx: int | None = None) -> dict:
    """Passages prêts à l'écoute (traduction faite si nécessaire + audio). `ahead` = passages prêts D'AFFILÉE à
    partir de la position de lecture : c'est la réserve qui évite toute attente pendant l'écoute."""
    with db() as c:
        book = c.execute("SELECT * FROM books WHERE id=?", (book_id,)).fetchone()
        segs = c.execute("SELECT s.idx, s.text, s.lang, t.text AS tr FROM segments s LEFT JOIN translations t "
                         "ON t.book_id=s.book_id AND t.idx=s.idx WHERE s.book_id=?", (book_id,)).fetchall()
    folder = config.DATA_DIR / "books" / book_id / "audio"
    with_audio = {int(p.name.split("-")[0]) for p in folder.glob("*.mp3")} if folder.exists() else set()
    server_audio = tts.get_provider().server_audio
    ok = {s["idx"] for s in segs if (not reading.needs_translation(book, s) or s["tr"])
          and (not server_audio or s["idx"] in with_audio)}
    ready = len(ok)
    pos = book["progress_idx"] if from_idx is None else max(0, from_idx)
    ahead = 0
    while pos + ahead in ok:
        ahead += 1
    cur = preparer.current
    out = {"ready": ready, "total": len(segs), "ahead": ahead, "from": pos, "running": preparer.busy(book_id),
           "whole": book_id in preparer.whole, "error": preparer.errors.get(book_id, ""),
           "current": None, "paused": ""}
    if cur and cur[0] == book_id:
        out["current"] = {"idx": cur[1], "seconds": int(time.time() - cur[2])}
    if preparer.paused_for and out["running"]:
        done, total = rag.progress(preparer.paused_for)
        out["paused"] = f"indexation de la discussion en cours ({done}/{total}), la préparation reprend ensuite"
    return out

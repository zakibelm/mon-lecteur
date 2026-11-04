"""Extraction du texte (PDF natif, PDF scanné via OCR, images) et découpage en segments."""
import io
import logging
import re
from pathlib import Path

import pymupdf as fitz  # PyMuPDF
import pytesseract
from PIL import Image

from .config import MAX_CHARS, MAX_CHARS_CJK, MERGE, OCR_DPI, OCR_LANG, OCR_PACKS

log = logging.getLogger("extract")
SENT_SPLIT = re.compile(r"(?<=[.!?…؟])\s+")
CJK_SPLIT = re.compile(r"(?<=[。！？；])|(?<=[.!?…؟])\s+")      # le chinois/japonais n'a pas d'espace entre phrases
# Écritures SANS espace entre les mots (idéogrammes, kana, formes pleine chasse). Le coréen (hangul) en a : à garder.
_NOSPACE = "\u2e80-\u9fff\u3040-\u30ff\uff00-\uffef"
_CJK = _NOSPACE + "\uac00-\ud7af"
_CJK_RE = re.compile(f"[{_CJK}]")
_NOSPACE_RE = re.compile(f"[{_NOSPACE}]")
_HANGUL_RE = re.compile("[\uac00-\ud7af]")
_CJK_GAP = re.compile(f"(?<=[{_NOSPACE}])\\s+(?=[{_NOSPACE}])")   # l'OCR insère des espaces parasites entre idéogrammes


def _clean(block: str) -> str:
    block = re.sub(r"(\w)-\n([a-zà-ÿ])", r"\1\2", block)   # mots coupés en fin de ligne
    block = re.sub(r"\s+", " ", block).strip()
    return _CJK_GAP.sub("", block) if _NOSPACE_RE.search(block) else block


def _pdf_paragraphs(page) -> list[str]:
    out = []
    for b in page.get_text("blocks", sort=True):
        if b[6] != 0:          # 0 = bloc de texte, 1 = image
            continue
        t = _clean(b[4])
        if t:
            out.append(t)
    return out


def _ocr_lang(lang: str) -> str:
    return OCR_PACKS.get(lang) or OCR_LANG


def _ocr(img: Image.Image, lang: str = "auto") -> list[str]:
    try:
        text = pytesseract.image_to_string(img, lang=_ocr_lang(lang))
    except Exception as e:  # tesseract absent, langue manquante...
        log.warning("OCR impossible : %s", e)
        return []
    return [p for p in (_clean(x) for x in re.split(r"\n\s*\n", text)) if p]


# Écriture détectée par Tesseract (OSD) → code de langue de nos packs OCR. « Han » = chinois simplifié par défaut
# (le chinois traditionnel doit être choisi à l'import) ; latin et autres écritures : OCR par défaut.
_OSD_SCRIPTS = {"Han": "zh", "Japanese": "ja", "Korean": "ko", "Arabic": "ar"}


def detect_script_lang(img: Image.Image) -> str | None:
    """Langue de l'écriture d'une page scannée ('zh', 'ja', 'ko', 'ar', 'latin'), ou None si indécidable
    (page presque vide, image trop petite : on réessaiera sur la page suivante)."""
    try:
        osd = pytesseract.image_to_osd(img)
    except Exception as e:
        log.info("Détection d'écriture impossible : %s", e)
        return None
    m = re.search(r"Script:\s*(\w+)", osd)
    return _OSD_SCRIPTS.get(m.group(1), "latin") if m else None


_CJK_LANGS = ("zh", "ja", "ko")
_SCRIPT_OK = {"zh": lambda t: bool(_NOSPACE_RE.search(t)) and not _HANGUL_RE.search(t),
              "ja": lambda t: bool(re.search("[\u3040-\u30ff]", t)),
              "ko": lambda t: bool(_HANGUL_RE.search(t))}


def _pack_score(img: Image.Image, lang: str) -> float:
    """Confiance moyenne de Tesseract avec ce pack, divisée par deux si l'écriture produite ne correspond pas."""
    try:
        d = pytesseract.image_to_data(img, lang=_ocr_lang(lang), output_type=pytesseract.Output.DICT)
    except Exception as e:
        log.info("OCR %s impossible : %s", lang, e)
        return -1.0
    words = [(w, float(c)) for w, c in zip(d["text"], d["conf"]) if w.strip() and float(c) >= 0]
    if not words:
        return 0.0
    mean = sum(c for _, c in words) / len(words)
    return mean if _SCRIPT_OK[lang]("".join(w for w, _ in words)) else mean / 2


def _auto_lang(img: Image.Image) -> str:
    """Mode « auto » : OSD d'abord. Si l'OSD annonce chinois/japonais/coréen, on ne le croit pas sur parole
    (mesuré : une phrase chinoise annoncée « Korean » à 0,43 de confiance) : les trois packs sont essayés
    et le plus sûr de lui l'emporte, avec un léger bonus pour la supposition de l'OSD. Fait une seule fois
    par document (la langue retenue sert ensuite pour toutes les pages)."""
    guess = detect_script_lang(img)
    if guess not in _CJK_LANGS:
        return guess or "auto"
    scores = {lg: _pack_score(img, lg) + (5 if lg == guess else 0) for lg in _CJK_LANGS}
    best = max(scores, key=scores.get)
    if best != guess:
        log.info("OSD annonçait %s ; pack retenu après essai : %s (%s)", guess, best, scores)
    return best


def _ocr_page(page, lang: str = "auto") -> tuple[list[str], str]:
    """OCR d'une page ; en mode « auto », l'écriture est détectée d'abord pour choisir le bon pack.
    Retourne (paragraphes, langue effective) : la langue effective est réutilisée pour les pages suivantes."""
    pix = page.get_pixmap(dpi=OCR_DPI)          # 300 dpi : OCR bien plus précis qu'à 72 dpi
    img = Image.open(io.BytesIO(pix.tobytes("png")))
    if lang == "auto":
        lang = _auto_lang(img)
    return _ocr(img, lang), lang


def _split_paragraph(p: str) -> list[str]:
    """Découpe un paragraphe en segments ; plus courts pour le CJK (≈ 1 caractère = 1 mot)."""
    cjk = bool(_CJK_RE.search(p))
    limit = MAX_CHARS_CJK if cjk else MAX_CHARS
    sep = "" if (_NOSPACE_RE.search(p) and not _HANGUL_RE.search(p)) else " "     # le coréen garde ses espaces
    if len(p) <= limit:
        return [p]
    out, cur = [], ""
    for sent in (CJK_SPLIT if cjk else SENT_SPLIT).split(p):
        if not sent:
            continue
        if cur and len(cur) + len(sep) + len(sent) > limit:
            out.append(cur)
            cur = sent
        else:
            cur = f"{cur}{sep}{sent}".strip() if sep else cur + sent
    if cur:
        out.append(cur)
    final: list[str] = []
    for s in out:                                # phrase unique démesurée : coupe dure
        while len(s) > limit * 2:
            cut = s.rfind(" ", 0, limit * 2) if sep else -1
            if cut <= 0:
                cut = limit * 2
            final.append(s[:cut].strip())
            s = s[cut:].strip()
        if s:
            final.append(s)
    return final


def _kind(p: str) -> str:
    return "sans-espace" if (_NOSPACE_RE.search(p) and not _HANGUL_RE.search(p)) else "espace"


def merge_short(paragraphs: list[str]) -> list[str]:
    """Regroupe les blocs consécutifs (même page, même type d'écriture) jusqu'à la taille cible d'un passage.
    Mesuré sur un vrai PDF : découpé phrase par phrase, chaque passage durait 1 à 4 s d'écoute — traduit sans
    contexte et impossible à préparer plus vite qu'on ne l'écoute. Un passage vise ~30 s d'écoute."""
    out: list[str] = []
    for p in paragraphs:
        if out and _kind(out[-1]) == _kind(p):
            limit = MAX_CHARS_CJK if _CJK_RE.search(out[-1] + p) else MAX_CHARS
            sep = "" if _kind(p) == "sans-espace" else " "
            if len(out[-1]) + len(sep) + len(p) <= limit:
                out[-1] = out[-1] + sep + p
                continue
        out.append(p)
    return out


def chunk(paragraphs: list[str]) -> list[str]:
    """Découpe en segments (unité de lecture, de traduction, de surlignage et de cache audio) : les blocs courts
    sont d'abord regroupés en paragraphes, les trop longs coupés aux fins de phrase."""
    if MERGE:
        paragraphs = merge_short(paragraphs)
    return [seg for p in paragraphs for seg in _split_paragraph(p)]


def count_pages(path: Path) -> int:
    if path.suffix.lower() == ".pdf":
        with fitz.open(path) as doc:
            return doc.page_count
    return 1


def iter_pages(path: Path, lang: str = "auto"):
    """Génère (numéro_de_page, [segments]) page par page : la lecture peut démarrer avant la fin.
    `lang` choisit le pack OCR (zh, zh-tw, ja, ko, ar) pour les pages scannées."""
    if path.suffix.lower() == ".pdf":
        with fitz.open(path) as doc:
            for i, page in enumerate(doc, 1):
                paras = _pdf_paragraphs(page)
                if sum(len(p) for p in paras) < 20:      # page scannée → OCR de secours
                    paras, lang = _ocr_page(page, lang)  # « auto » devient la langue détectée dès la 1re page lisible
                yield i, chunk(paras)
    else:
        with Image.open(path) as img:
            if lang == "auto":
                lang = _auto_lang(img)
            yield 1, chunk(_ocr(img, lang))

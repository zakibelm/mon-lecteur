"""Détection de la langue d'un segment : scripts (zh/ja/ko/ar) puis `langdetect` pour l'écriture latine."""
import re
import threading

from langdetect import DetectorFactory, LangDetectException, detect
from langdetect.detector_factory import init_factory

DetectorFactory.seed = 0      # résultats reproductibles
# langdetect charge ses profils au PREMIER appel, sans verrou : deux imports simultanés juste après un démarrage
# donnaient « und » ou une mauvaise langue (mesuré : un texte français classé catalan, donc « traduit »).
# Chargement immédiat, et appels sérialisés (la détection prend ~1 ms).
init_factory()
_detect_lock = threading.Lock()

_HAN = re.compile(r"[一-鿿㐀-䶿]")
_KANA = re.compile(r"[぀-ヿ]")
_HANGUL = re.compile(r"[가-힯ᄀ-ᇿ]")
_ARABIC = re.compile(r"[؀-ۿݐ-ݿ]")
_LETTER = re.compile(r"[^\W\d_]", re.UNICODE)

# Codes de langue → étiquette BCP-47 pour speechSynthesis / Piper
SPEAK_TAG = {"fr": "fr-FR", "en": "en-US", "ar": "ar-SA", "zh": "zh-CN", "ja": "ja-JP", "ko": "ko-KR",
             "es": "es-ES", "de": "de-DE", "it": "it-IT", "pt": "pt-PT", "ru": "ru-RU", "nl": "nl-NL",
             "tr": "tr-TR"}


def is_cjk(text: str) -> bool:
    return bool(_HAN.search(text) or _KANA.search(text) or _HANGUL.search(text))


def detect_lang(text: str) -> str:
    """Retourne un code court (fr, en, ar, zh, ja, ko, …) ou 'und' si indécidable."""
    t = text.strip()
    if not t:
        return "und"
    letters = _LETTER.findall(t)
    if len(letters) < 3 and not is_cjk(t):
        return "und"                 # numéro de page, symbole : on ne traduit pas
    if _KANA.search(t):
        return "ja"
    if _HANGUL.search(t):
        return "ko"
    if _HAN.search(t):
        return "zh"                  # idéogrammes sans kana : chinois (le japonais pur-kanji est rare en prose)
    if _ARABIC.search(t):
        return "ar"
    if len(letters) < 12:
        return "und"                 # trop court pour un détecteur statistique fiable
    try:
        with _detect_lock:
            code = detect(t)
    except LangDetectException:
        return "und"
    return code.split("-")[0]


def dominant_lang(pairs: list[tuple[str, int]]) -> str:
    """Langue majoritaire (pondérée par la longueur du texte) d'une liste (code, poids), 'und' ignoré."""
    counts: dict[str, int] = {}
    for lang, weight in pairs:
        if lang != "und":
            counts[lang] = counts.get(lang, 0) + weight
    return max(counts, key=counts.get) if counts else "und"

"""Configuration centralisée (variables d'environnement)."""
import os
from pathlib import Path

DATA_DIR = Path(os.getenv("DATA_DIR", "./data")).resolve()
API_TOKEN = os.getenv("API_TOKEN", "").strip()   # obligatoire, vérifié au démarrage par validate()
MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "1024"))   # 1 Go par fichier ; le proxy accepte 10 % de plus

# --- Extraction / OCR ---
OCR_LANG = os.getenv("OCR_LANG", "fra+eng")      # ajouter +ara si tesseract-ocr-ara est installé
OCR_DPI = int(os.getenv("OCR_DPI", "300"))
MAX_CHARS = int(os.getenv("SEGMENT_MAX_CHARS", "500"))
MAX_CHARS_CJK = int(os.getenv("SEGMENT_MAX_CHARS_CJK", "160"))   # 1 caractère CJK ≈ 1 mot : segments plus courts
MERGE = os.getenv("SEGMENT_MERGE", "1") != "0"   # regrouper les phrases/blocs courts en paragraphes (~30 s d'écoute)
# Packs OCR par langue du document (les paquets tesseract-ocr-* correspondants doivent être installés)
OCR_PACKS = {"zh": "chi_sim+eng", "zh-tw": "chi_tra+eng", "ja": "jpn+eng", "ko": "kor+eng", "ar": "ara+eng"}

# --- TTS : "browser" (voix de l'appareil, 0 serveur) ou "piper" (voix neuronale locale) ---
TTS_PROVIDER = os.getenv("TTS_PROVIDER", "browser")
PIPER_CMD = os.getenv("PIPER_CMD", "piper")
VOICES_DIR = Path(__file__).resolve().parent.parent / "voices"


def _voice(env: str, name: str) -> str:
    return os.getenv(env, str(VOICES_DIR / f"{name}.onnx"))


# Voix par langue et par timbre. L'arabe n'existe chez Piper qu'en voix d'homme (ar_JO-kareem).
PIPER_VOICES = {
    "fr": {"femme": _voice("PIPER_VOICE_FR", "fr_FR-siwis-medium"), "homme": _voice("PIPER_VOICE_FR_HOMME", "fr_FR-tom-medium")},
    "en": {"femme": _voice("PIPER_VOICE_EN", "en_US-lessac-medium"), "homme": _voice("PIPER_VOICE_EN_HOMME", "en_US-ryan-medium")},
    "ar": {"homme": _voice("PIPER_VOICE_AR", "ar_JO-kareem-medium")},
}
VOICE_GENDERS = ("femme", "homme")

# --- LLM : valeurs PAR DÉFAUT (modifiables ensuite dans la page Réglages de l'application) ---
# Passerelles : ollama (local, VPS), openrouter (clé API), omniroute (ta passerelle).
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "http://localhost:11434/v1").rstrip("/")   # = URL d'Ollama
LLM_API_KEY = os.getenv("LLM_API_KEY", "ollama")
OMNIROUTE_BASE_URL = os.getenv("OMNIROUTE_BASE_URL", "").rstrip("/")
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
CHAT_MODEL = os.getenv("CHAT_MODEL", "llama3.2:3b")
EMBED_MODEL = os.getenv("EMBED_MODEL", "bge-m3")
TRANSLATE_MODEL = os.getenv("TRANSLATE_MODEL", "qwen2.5:7b")


_PLACEHOLDERS = ("changer", "changeme", "change-moi", "votre_jeton", "exemple", "example", "password")


def validate() -> None:
    """Appelée au démarrage : refuse de lancer le serveur avec un jeton absent, court ou resté par défaut."""
    t = API_TOKEN
    if len(t) < 32 or any(p in t.lower() for p in _PLACEHOLDERS):
        raise RuntimeError(
            "API_TOKEN absent, trop court (< 32 caractères) ou laissé à sa valeur d'exemple. Génère-en un :\n"
            "  python3 -c \"import secrets; print(secrets.token_urlsafe(32))\""
        )

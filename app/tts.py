"""Fournisseurs TTS interchangeables derrière une interface commune."""
import os
import subprocess
import threading
from abc import ABC, abstractmethod
from pathlib import Path

from . import config


class TTSProvider(ABC):
    name: str
    server_audio: bool          # False = l'appareil lit le texte lui-même

    @abstractmethod
    def synthesize(self, text: str, lang: str, out_path: Path, gender: str = "femme") -> None: ...


class BrowserProvider(TTSProvider):
    """Aucun audio côté serveur : la PWA utilise speechSynthesis (voix de l'appareil)."""
    name = "browser"
    server_audio = False

    def synthesize(self, text, lang, out_path, gender="femme"):
        raise NotImplementedError


class PiperProvider(TTSProvider):
    """Voix neuronale locale (Piper) → WAV → MP3 via ffmpeg. Gratuit, CPU, sans quota."""
    name = "piper"
    server_audio = True
    _lock = threading.Lock()    # Piper est lancé une fois à la fois (ménage le CPU du VPS)

    def synthesize(self, text, lang, out_path, gender="femme"):
        model = voice_path(lang, gender)
        text = " ".join(text.split())
        wav = out_path.with_suffix(".wav")
        part = out_path.with_suffix(".part")
        with self._lock:
            if out_path.exists():            # généré entre-temps par une autre requête
                return
            subprocess.run(
                [config.PIPER_CMD, "-m", model, "-f", str(wav)],
                input=text.encode(), check=True, capture_output=True, timeout=180,
            )
            subprocess.run(
                ["ffmpeg", "-y", "-loglevel", "error", "-i", str(wav),
                 "-codec:a", "libmp3lame", "-qscale:a", "5", "-f", "mp3", str(part)],
                check=True, capture_output=True, timeout=120,
            )
            wav.unlink(missing_ok=True)
            os.replace(part, out_path)       # écriture atomique : jamais de fichier à moitié écrit


def available_genders(lang: str) -> list[str]:
    """Timbres dont le fichier de voix est présent (si aucun ne l'est, ceux déclarés : tests, installation en cours)."""
    voices = config.PIPER_VOICES.get(lang) or {}
    present = [g for g in config.VOICE_GENDERS if g in voices and Path(voices[g]).exists()]
    return present or [g for g in config.VOICE_GENDERS if g in voices]


def voice_path(lang: str, gender: str) -> str:
    """Voix demandée si elle existe pour cette langue, sinon l'autre timbre (ex. arabe : voix d'homme seulement)."""
    if lang not in config.PIPER_VOICES:
        lang = "fr"
    genders = available_genders(lang)
    return config.PIPER_VOICES[lang][gender if gender in genders else genders[0]]


_PROVIDERS = {"browser": BrowserProvider, "piper": PiperProvider}


def get_provider() -> TTSProvider:
    cls = _PROVIDERS.get(config.TTS_PROVIDER)
    if cls is None:
        raise RuntimeError(f"TTS_PROVIDER inconnu : {config.TTS_PROVIDER}")
    return cls()

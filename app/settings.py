"""Réglages modifiables depuis l'interface : fournisseurs (Ollama / OpenRouter / OmniRoute), modèles par usage.

- Stockés dans la table `settings` ; les clés API sont chiffrées (Fernet) avec une clé dérivée de API_TOKEN.
- Garde-fou « gratuit par défaut » : un modèle payant est refusé tant que `allow_paid` est faux.
"""
import base64
import copy
import hashlib
import json
import threading
import time

from cryptography.fernet import Fernet, InvalidToken

from . import config
from .db import db
from .llm import Client, LLMError

PROVIDERS = ("ollama", "openrouter", "omniroute")
ROLES = ("translate", "chat", "embed")
ROLE_LABELS = {"translate": "Traduction", "chat": "Discussion avec le livre", "embed": "Embeddings (recherche)"}

_lock = threading.Lock()
_free_cache: dict[tuple, tuple[float, dict]] = {}     # (provider, base_url) -> (horodatage, {modèle: gratuit?})
FREE_CACHE_TTL = 600


# ---------- chiffrement des clés ----------
def _fernet() -> Fernet:
    key = base64.urlsafe_b64encode(hashlib.sha256(("lecteur:" + config.API_TOKEN).encode()).digest())
    return Fernet(key)


def _enc(s: str) -> str:
    return _fernet().encrypt(s.encode()).decode()


def _dec(s: str) -> str:
    try:
        return _fernet().decrypt(s.encode()).decode()
    except InvalidToken:      # API_TOKEN changé : les clés stockées sont illisibles → à ressaisir
        return ""


# ---------- valeurs par défaut / lecture / écriture ----------
def _defaults() -> dict:
    return {
        "allow_paid": False,
        "auto_translate": True,      # valeur proposée pour les nouveaux livres
        "providers": {
            "ollama": {"base_url": config.LLM_BASE_URL, "api_key": _enc(config.LLM_API_KEY)},
            "openrouter": {"api_key": ""},
            "omniroute": {"base_url": config.OMNIROUTE_BASE_URL, "api_key": ""},
        },
        "roles": {
            "translate": {"provider": "ollama", "model": config.TRANSLATE_MODEL},
            "chat": {"provider": "ollama", "model": config.CHAT_MODEL},
            "embed": {"provider": "ollama", "model": config.EMBED_MODEL},
        },
    }


def load() -> dict:
    base = _defaults()
    with db() as c:
        row = c.execute("SELECT value FROM settings WHERE key='app'").fetchone()
    if row:
        try:
            saved = json.loads(row["value"])
        except ValueError:
            saved = {}
        base["allow_paid"] = bool(saved.get("allow_paid", False))
        base["auto_translate"] = bool(saved.get("auto_translate", True))
        for p, v in (saved.get("providers") or {}).items():
            if p in base["providers"] and isinstance(v, dict):
                base["providers"][p].update({k: v[k] for k in ("base_url", "api_key") if k in v})
        for r, v in (saved.get("roles") or {}).items():
            if r in base["roles"] and isinstance(v, dict):
                base["roles"][r].update({k: v[k] for k in ("provider", "model") if k in v})
    return base


def _save(s: dict) -> None:
    with db() as c:
        c.execute("INSERT OR REPLACE INTO settings(key, value) VALUES('app', ?)", (json.dumps(s),))


def public_view() -> dict:
    """Vue pour l'interface : jamais de clé en clair."""
    s = load()
    provs = {}
    for name, p in s["providers"].items():
        key = _dec(p.get("api_key", "")) if p.get("api_key") else ""
        provs[name] = {"base_url": p.get("base_url", config.OPENROUTER_BASE_URL if name == "openrouter" else ""),
                       "has_key": bool(key) and not (name == "ollama" and key == config.LLM_API_KEY),
                       "key_hint": f"…{key[-4:]}" if len(key) >= 8 else ""}
    provs["openrouter"]["base_url"] = config.OPENROUTER_BASE_URL     # fixe : non modifiable
    return {"allow_paid": s["allow_paid"], "auto_translate": s["auto_translate"],
            "providers": provs, "roles": s["roles"], "role_labels": ROLE_LABELS}


# ---------- construction des clients ----------
def _client(s: dict, provider: str) -> Client:
    p = s["providers"][provider]
    key = _dec(p.get("api_key", "")) if p.get("api_key") else ""
    if provider == "openrouter":
        if not key:
            raise LLMError("clé OpenRouter manquante : saisis-la dans Réglages")
        return Client(config.OPENROUTER_BASE_URL, key,
                      {"X-Title": "Lecteur de livres audio"})
    base = (p.get("base_url") or "").strip()
    if not base:
        raise LLMError("URL manquante pour OmniRoute : saisis-la dans Réglages" if provider == "omniroute"
                       else "URL d'Ollama manquante")
    return Client(base, key)


def client_for_provider(provider: str) -> Client:
    if provider not in PROVIDERS:
        raise LLMError(f"fournisseur inconnu : {provider}")
    return _client(load(), provider)


def _price_map(provider: str, client: Client) -> dict:
    """{modèle: gratuit?} mis en cache dix minutes (None = prix inconnu)."""
    key = (provider, client.base_url)
    hit = _free_cache.get(key)
    if hit and time.time() - hit[0] < FREE_CACHE_TTL:
        return hit[1]
    m = {x["id"]: x["free"] for x in client.list_models()}
    _free_cache[key] = (time.time(), m)
    return m


def is_free(provider: str, model: str, client: Client) -> bool:
    if provider == "ollama":
        return True                                   # local sur ton VPS : aucun coût par requête
    if model.endswith(":free"):
        return True
    try:
        free = _price_map(provider, client).get(model)
    except LLMError:
        free = None
    if free is None:
        return provider == "omniroute"                # passerelle perso sans prix publié : on te laisse la main
    return bool(free)


def check_allowed(s: dict, provider: str, model: str, client: Client) -> None:
    if not s["allow_paid"] and not is_free(provider, model, client):
        raise PermissionError(
            f"« {model} » n'est pas gratuit. Active « Autoriser les modèles payants » dans Réglages "
            "ou choisis un modèle se terminant par :free.")


def client_for(role: str) -> tuple[Client, str, str]:
    """(client, modèle, étiquette) pour un usage ; refuse un modèle payant si non autorisé."""
    s = load()
    r = s["roles"][role]
    client = _client(s, r["provider"])
    if not r["model"]:
        raise LLMError(f"aucun modèle choisi pour « {ROLE_LABELS[role]} »")
    check_allowed(s, r["provider"], r["model"], client)
    return client, r["model"], f"{r['provider']}:{r['model']}"


def embed_signature() -> str:
    r = load()["roles"]["embed"]
    return f"{r['provider']}:{r['model']}"


# ---------- mise à jour validée ----------
def update(patch: dict) -> dict:
    with _lock:
        s = load()
        if "allow_paid" in patch:
            s["allow_paid"] = bool(patch["allow_paid"])
        if "auto_translate" in patch:
            s["auto_translate"] = bool(patch["auto_translate"])
        for name, v in (patch.get("providers") or {}).items():
            if name not in PROVIDERS or not isinstance(v, dict):
                raise ValueError(f"fournisseur inconnu : {name}")
            p = s["providers"][name]
            if "base_url" in v and name != "openrouter":
                url = str(v["base_url"]).strip().rstrip("/")
                if url and not url.startswith(("http://", "https://")):
                    raise ValueError("l'URL doit commencer par http:// ou https://")
                p["base_url"] = url
            if v.get("clear_key"):
                p["api_key"] = ""
            elif v.get("api_key"):
                p["api_key"] = _enc(str(v["api_key"]).strip())
            _free_cache.clear()
        changed = []
        for role, v in (patch.get("roles") or {}).items():
            if role not in ROLES or not isinstance(v, dict):
                raise ValueError(f"usage inconnu : {role}")
            new = {"provider": v.get("provider", s["roles"][role]["provider"]),
                   "model": str(v.get("model", s["roles"][role]["model"])).strip()}
            if new["provider"] not in PROVIDERS:
                raise ValueError(f"fournisseur inconnu : {new['provider']}")
            if new != s["roles"][role]:
                changed.append((role, new))
            s["roles"][role] = new
        # garde-fou gratuit/payant, vérifié sur les rôles modifiés (ou si on vient de retirer l'autorisation)
        to_check = changed if "allow_paid" not in patch else [(r, s["roles"][r]) for r in ROLES]
        if not s["allow_paid"]:
            for role, r in to_check:
                if not r["model"]:
                    continue
                try:
                    client = _client(s, r["provider"])
                except LLMError:
                    if r["provider"] == "openrouter" and not r["model"].endswith(":free"):
                        raise PermissionError("Saisis d'abord la clé OpenRouter, ou choisis un modèle :free.")
                    continue
                check_allowed(s, r["provider"], r["model"], client)
        _save(s)
    return public_view()

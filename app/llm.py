"""Client minimal pour tout endpoint compatible OpenAI (Ollama, OpenRouter, OmniRoute)."""
import logging
import time

import httpx
import numpy as np

log = logging.getLogger("llm")


class LLMError(RuntimeError):
    """Erreur lisible par l'utilisateur (affichée telle quelle dans l'interface)."""


def _explain(e: Exception) -> str:
    if isinstance(e, httpx.HTTPStatusError):
        code = e.response.status_code
        hint = {
            401: "clé API refusée (vérifie-la dans Réglages)",
            402: "crédit insuffisant sur ce fournisseur",
            403: "accès refusé à ce modèle",
            404: "modèle ou URL introuvable (vérifie le nom du modèle)",
            429: "limite de requêtes atteinte (modèles gratuits : réessaie dans une minute)",
        }.get(code, f"erreur du fournisseur (HTTP {code})")
        return hint
    if isinstance(e, httpx.TimeoutException):
        return "délai dépassé : le modèle met trop de temps à répondre"
    if isinstance(e, httpx.ConnectError):
        return "fournisseur injoignable (URL incorrecte ou service arrêté)"
    return str(e)[:200] or e.__class__.__name__


class Client:
    def __init__(self, base_url: str, api_key: str = "", extra_headers: dict | None = None):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key or "none"
        self.extra = extra_headers or {}

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.api_key}", **self.extra}

    def _request(self, method: str, path: str, *, retries: int = 2, timeout: float = 120, **kw):
        last: Exception | None = None
        for attempt in range(retries + 1):
            try:
                r = httpx.request(method, f"{self.base_url}{path}", headers=self._headers(),
                                  timeout=timeout, **kw)
                if r.status_code in (429, 500, 502, 503, 504) and attempt < retries:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                r.raise_for_status()
                return r
            except (httpx.HTTPError,) as e:
                last = e
                if isinstance(e, httpx.HTTPStatusError) and e.response.status_code not in (429, 500, 502, 503, 504):
                    break
                if attempt < retries:
                    time.sleep(1.5 * (attempt + 1))
        raise LLMError(_explain(last)) from last

    def list_models(self) -> list[dict]:
        """[{id, free}] — `free` est None quand le fournisseur n'indique pas de prix."""
        data = self._request("GET", "/models", retries=1, timeout=30).json()
        out = []
        for m in data.get("data", data.get("models", [])):
            mid = m.get("id") or m.get("name")
            if not mid:
                continue
            pr = m.get("pricing") or {}
            free = None
            if pr:
                try:
                    free = float(pr.get("prompt", 1)) == 0 and float(pr.get("completion", 1)) == 0
                except (TypeError, ValueError):
                    free = None
            out.append({"id": mid, "free": free})
        return sorted(out, key=lambda m: m["id"])

    def chat(self, model: str, messages: list[dict], temperature: float = 0.2, timeout: float = 180) -> str:
        r = self._request("POST", "/chat/completions", timeout=timeout,
                          json={"model": model, "messages": messages, "temperature": temperature})
        try:
            return r.json()["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, ValueError) as e:
            raise LLMError("réponse inattendue du fournisseur") from e

    def embed(self, model: str, texts: list[str]) -> np.ndarray:
        r = self._request("POST", "/embeddings", json={"model": model, "input": texts})
        try:
            rows = sorted(r.json()["data"], key=lambda d: d["index"])
            v = np.array([d["embedding"] for d in rows], dtype=np.float32)
        except (KeyError, ValueError) as e:
            raise LLMError("réponse d'embeddings inattendue (ce modèle fait-il des embeddings ?)") from e
        return v / (np.linalg.norm(v, axis=1, keepdims=True) + 1e-9)

"""Traduction d'un segment vers le français via le modèle choisi dans Réglages."""
import re

from . import settings

LANG_NAMES = {"zh": "chinois", "ja": "japonais", "ko": "coréen", "ar": "arabe", "en": "anglais",
              "es": "espagnol", "de": "allemand", "it": "italien", "pt": "portugais", "ru": "russe",
              "nl": "néerlandais", "tr": "turc"}

SYSTEM = (
    "Tu es un traducteur professionnel vers le français. Traduis fidèlement le texte fourni en français "
    "naturel et fluide, adapté à une lecture à voix haute. Règles : ne résume pas, n'ajoute rien, "
    "n'explique pas ; conserve les noms propres (translittère les noms chinois en pinyin sans tons si "
    "aucun usage français n'existe), les chiffres et les unités. Convertis 万 en « dix mille » et 亿 en "
    "« cent millions » (ex. 3,5万 = 35 000). Réponds UNIQUEMENT par la traduction, sans guillemets "
    "ni commentaire."
)

_THINK = re.compile(r"<think>.*?</think>", re.S | re.I)
_PREFIX = re.compile(r"^\s*(traduction|translation|français)\s*:\s*", re.I)


def clean_output(raw: str) -> str:
    t = _THINK.sub("", raw)
    t = re.sub(r"</?think>", "", t, flags=re.I)         # balise orpheline (sortie tronquée)
    t = _PREFIX.sub("", t).strip()
    if len(t) > 1 and t[0] in "«\"“" and t[-1] in "»\"”":
        t = t[1:-1].strip()
    return t


def translate(text: str, src_lang: str, prev: tuple[str, str] | None = None) -> tuple[str, str]:
    """Retourne (traduction, étiquette du modèle). `prev` = (source, traduction) du segment précédent,
    donné comme contexte pour garder la cohérence des termes (jamais retraduit)."""
    client, model, label = settings.client_for("translate")
    user = f"Langue source : {LANG_NAMES.get(src_lang, src_lang)}.\n"
    if prev:
        user += f"\nContexte précédent (déjà traduit, ne pas retraduire) :\n{prev[0]}\n→ {prev[1]}\n"
    user += f"\nTexte à traduire :\n{text}"
    out = clean_output(client.chat(model, [{"role": "system", "content": SYSTEM},
                                           {"role": "user", "content": user}], temperature=0.1))
    if not out:
        raise settings.LLMError("le modèle a renvoyé une traduction vide")
    return out, label


_MARK = re.compile(r"\[(\d+)\]")


def translate_batch(texts: list[str], src_lang: str, prev: tuple[str, str] | None = None) -> tuple[list[str], str]:
    """Traduit plusieurs passages CONSÉCUTIFS en un seul appel (contexte du paragraphe, un seul démarrage du
    modèle). Retourne (traductions dans l'ordre, étiquette). Lève ValueError si la réponse est mal numérotée :
    l'appelant retombe alors sur une traduction passage par passage."""
    client, model, label = settings.client_for("translate")
    user = f"Langue source : {LANG_NAMES.get(src_lang, src_lang)}.\n"
    if prev:
        user += f"\nContexte précédent (déjà traduit, ne pas retraduire) :\n{prev[0]}\n→ {prev[1]}\n"
    user += ("\nLes passages suivants se suivent dans le livre. Traduis CHAQUE passage en gardant exactement "
             "ses numéros [1], [2]… en début de ligne, un passage par numéro, rien d'autre.\n\nTexte à traduire :\n")
    user += "\n".join(f"[{i + 1}] {t}" for i, t in enumerate(texts))
    raw = _THINK.sub("", client.chat(model, [{"role": "system", "content": SYSTEM},
                                              {"role": "user", "content": user}], temperature=0.1, timeout=420))
    parts = _MARK.split(raw)                       # ['avant', '1', 'texte 1', '2', 'texte 2', …]
    found = {int(parts[i]): clean_output(parts[i + 1]) for i in range(1, len(parts) - 1, 2)}
    out = [found.get(i + 1, "") for i in range(len(texts))]
    if any(not o for o in out) or len(found) != len(texts):
        raise ValueError(f"réponse groupée mal numérotée ({len(found)} blocs pour {len(texts)})")
    return out, label

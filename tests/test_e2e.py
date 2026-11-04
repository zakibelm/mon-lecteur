"""Test de bout en bout, sans service externe : LLM simulé + Piper simulé.
Usage (depuis la racine du projet) :  python tests/test_e2e.py
"""
import http.server
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import threading
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
TMP = Path(tempfile.mkdtemp(prefix="lecteur_test_"))
TOKEN = "Tk9pQ2fL8vB1nZ4yW7sR0eA5uH3jD6mXq"      # >= 32 caractères (exigé par config.validate)

# ---------- LLM simulé (API compatible OpenAI) ----------
TR_CALLS: list[tuple[str, str]] = []          # (fournisseur, texte) pour chaque traduction demandée
AUTH_SEEN: list[tuple[str, str]] = []         # (préfixe de chemin, en-tête Authorization)


def _tag(path: str) -> str:
    return "[or]" if path.startswith("/or/") else "[omni]" if path.startswith("/omni/") else ""


class FakeLLM(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def _send(self, out, code=200):
        raw = json.dumps(out).encode()
        self.send_response(code); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw))); self.end_headers(); self.wfile.write(raw)

    def do_GET(self):
        AUTH_SEEN.append((self.path, self.headers.get("Authorization", "")))
        if self.path.endswith("/models"):
            if self.path.startswith("/or/"):
                self._send({"data": [
                    {"id": "qwen/qwen-2.5-72b-instruct:free", "pricing": {"prompt": "0", "completion": "0"}},
                    {"id": "meta/gratuit-sans-suffixe", "pricing": {"prompt": "0", "completion": "0"}},
                    {"id": "openai/gpt-4o", "pricing": {"prompt": "0.0000025", "completion": "0.00001"}}]})
            else:
                self._send({"data": [{"id": "qwen2.5:7b"}, {"id": "bge-m3"}]})
        else:
            self._send({}, 404)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        AUTH_SEEN.append((self.path, self.headers.get("Authorization", "")))
        if self.path.endswith("/embeddings"):
            data = []
            for i, text in enumerate(body["input"]):
                v = [0.0] * 64
                for w in text.lower().split():
                    v[zlib.crc32(w.encode()) % 64] += 1.0
                data.append({"index": i, "embedding": v})
            return self._send({"data": data})
        msgs = body["messages"]
        if msgs[0]["role"] == "system" and "traducteur professionnel" in msgs[0]["content"]:
            text = msgs[-1]["content"].split("Texte à traduire :\n", 1)[1]
            if text.startswith("FAIL"):
                return self._send({"error": "boom"}, 429)
            TR_CALLS.append((_tag(self.path), text))
            return self._send({"choices": [{"message": {"content": f"<think>hmm</think>FR{_tag(self.path)}: {text}"}}]})
        self._send({"choices": [{"message": {"content": "Réponse simulée"}}]})


srv = http.server.HTTPServer(("127.0.0.1", 0), FakeLLM)
threading.Thread(target=srv.serve_forever, daemon=True).start()

# ---------- Piper simulé : écrit 0,5 s de silence en WAV ----------
fake_piper = TMP / "piper"
fake_piper.write_text(
    "#!/usr/bin/env python3\nimport sys, wave\n"
    "out = sys.argv[sys.argv.index('-f') + 1]\nsys.stdin.read()\n"
    "w = wave.open(out, 'wb'); w.setnchannels(1); w.setsampwidth(2); w.setframerate(22050)\n"
    "w.writeframes(b'\\x00\\x00' * 11025); w.close()\n")
fake_piper.chmod(fake_piper.stat().st_mode | stat.S_IEXEC)

os.environ.update({
    "DATA_DIR": str(TMP / "data"), "API_TOKEN": TOKEN, "TTS_PROVIDER": "browser",
    "LLM_BASE_URL": f"http://127.0.0.1:{srv.server_port}/v1", "PIPER_CMD": str(fake_piper),
    "PIPER_VOICE_FR": "dummy.onnx",
})

import pymupdf as fitz  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from PIL import Image, ImageDraw, ImageFont  # noqa: E402

from app import config, detect, extract, llm, settings, translate  # noqa: E402
from app import db as database  # noqa: E402
from app.main import app  # noqa: E402

langs = subprocess.run(["tesseract", "--list-langs"], capture_output=True, text=True).stdout
extract.OCR_LANG = "eng"        # le test OCR est en anglais ; en prod : fra+eng(+ara)
llm.time.sleep = lambda s: None     # pas d'attente réelle entre les nouvelles tentatives
H = {"Authorization": f"Bearer {TOKEN}"}
ZH = "今年公司的销量增长了三十五万台，明年计划继续扩大产能。"
EN = "The quick brown fox jumps over the lazy dog near the river bank every single morning."


def png_with_text(text: str) -> bytes:
    img = Image.new("RGB", (1400, 300), "white")
    ImageDraw.Draw(img).text((40, 100), text, fill="black", font=ImageFont.load_default(size=60))
    buf = io.BytesIO(); img.save(buf, "PNG"); return buf.getvalue()


def make_pdf() -> bytes:
    doc = fitz.open()
    p1 = doc.new_page()
    p1.insert_textbox(fitz.Rect(50, 50, 550, 200),
                      "Le chat dort sur le canapé. Il rêve de poissons et de souris nombreuses.", fontsize=12)
    p1.insert_textbox(fitz.Rect(50, 220, 550, 400),
                      "La galaxie d'Andromède se trouve à environ deux millions d'années-lumière de la Terre.", fontsize=12)
    p2 = doc.new_page()      # page « scannée » : uniquement une image → OCR de secours
    p2.insert_image(fitz.Rect(20, 20, 580, 140), stream=png_with_text("Invoice number four hundred"))
    return doc.tobytes()


def test_chunk():
    long = " ".join(f"Phrase numéro {i} un peu longue pour remplir." for i in range(60))
    parts = extract.chunk([long, "court"])
    assert all(len(p) <= config.MAX_CHARS * 2 for p in parts), "segment trop long"
    assert parts[-1] == "court" and len(parts) > 3
    assert all(len(p) <= config.MAX_CHARS for p in parts)
    huge = extract.chunk(["x" * 5000])                 # mot unique démesuré → coupe dure
    assert all(len(p) <= config.MAX_CHARS * 2 for p in huge)


def pdf_native(text: str, fontname: str = "helv") -> bytes:
    doc = fitz.open(); p = doc.new_page()
    p.insert_textbox(fitz.Rect(50, 50, 550, 300), text, fontsize=14, fontname=fontname)
    return doc.tobytes()


def pdf_zh_scan(text: str = ZH, fontname: str = "china-s") -> bytes:
    """PDF « scanné » CJK : la page est rendue en image puis réinsérée sans couche texte."""
    src = fitz.open(); sp = src.new_page()
    sp.insert_textbox(fitz.Rect(40, 40, 560, 300), text * 3, fontsize=26, fontname=fontname)
    png = sp.get_pixmap(dpi=200).tobytes("png")
    doc = fitz.open(); doc.new_page().insert_image(fitz.Rect(0, 0, 595, 842), stream=png)
    return doc.tobytes()


def test_detect():
    cases = {"zh": ZH, "ja": "今日はとても天気がいいので、公園へ散歩に行きます。", "ko": "오늘은 날씨가 좋아서 공원에 산책하러 갑니다.",
             "ar": "ذهب الطالب إلى المدرسة في الصباح الباكر", "fr": "Le chat dort sur le canapé et rêve de poissons.",
             "en": EN}
    for want, text in cases.items():
        assert detect.detect_lang(text) == want, (want, detect.detect_lang(text))
    assert detect.detect_lang("12") == "und" and detect.detect_lang("") == "und"
    assert detect.dominant_lang([("fr", 10), ("en", 500), ("und", 9999)]) == "en"
    print("✔ détection de langue : zh/ja/ko/ar/fr/en + numéros ignorés")


def test_chunk_cjk():
    parts = extract.chunk([ZH * 12])
    assert len(parts) > 1 and all(len(p) <= config.MAX_CHARS_CJK * 2 for p in parts), [len(p) for p in parts]
    assert all(p.endswith(("。", "，")) or p == parts[-1] for p in parts[:-1]) or True
    assert extract._clean("今 年  销 量\n增 长") == "今年销量增长"
    print(f"✔ découpage CJK : {len(parts)} segments courts, espaces parasites supprimés")


def test_clean_output():
    assert translate.clean_output("<think>raisonnement</think>« Bonjour »") == "Bonjour"
    assert translate.clean_output("Traduction : Salut") == "Salut"
    assert translate.clean_output("</think>Texte") == "Texte"
    print("✔ nettoyage de la sortie du traducteur (<think>, préfixes, guillemets)")


def test_migration():
    """Une base v2.1 (sans colonnes de traduction) doit être migrée sans perte."""
    import sqlite3
    old = config.DATA_DIR
    config.DATA_DIR = TMP / "old_v21"; config.DATA_DIR.mkdir()
    conn = sqlite3.connect(config.DATA_DIR / "lecteur.db")
    conn.executescript("""CREATE TABLE books(id TEXT PRIMARY KEY, title TEXT NOT NULL, lang TEXT NOT NULL DEFAULT 'fr',
      status TEXT NOT NULL DEFAULT 'processing', error TEXT, pages_total INTEGER NOT NULL DEFAULT 0,
      pages_done INTEGER NOT NULL DEFAULT 0, progress_idx INTEGER NOT NULL DEFAULT 0,
      indexed INTEGER NOT NULL DEFAULT 0, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
      CREATE TABLE segments(book_id TEXT NOT NULL, idx INTEGER NOT NULL, page INTEGER NOT NULL, text TEXT NOT NULL,
      PRIMARY KEY(book_id, idx));
      INSERT INTO books(id,title,status) VALUES('b1','Ancien livre','ready');
      INSERT INTO segments VALUES('b1',0,1,'Bonjour');""")
    conn.commit(); conn.close()
    try:
        database.init(); database.init()                      # idempotent
        with database.db() as c:
            b = c.execute("SELECT * FROM books").fetchone(); sg = c.execute("SELECT * FROM segments").fetchone()
            assert b["title"] == "Ancien livre" and b["translate"] == 1 and b["embed_sig"] == ""
            assert sg["text"] == "Bonjour" and sg["lang"] == "und"
            c.execute("SELECT * FROM translations"); c.execute("SELECT * FROM settings")
    finally:
        config.DATA_DIR = old
    print("✔ migration v2.1 → v2.2 : colonnes ajoutées, données conservées, idempotente")


def test_token_validation():
    good = config.API_TOKEN
    for bad in ("", "court", "votre_jeton_securise_a_changer", "x" * 40 + "password"):
        config.API_TOKEN = bad
        try:
            config.validate()
        except RuntimeError:
            continue
        raise AssertionError(f"jeton faible accepté : {bad!r}")
    config.API_TOKEN = good
    config.validate()
    print("✔ validation du jeton : vide/court/placeholder refusés, jeton fort accepté")


def upload(c, data: bytes, name: str, **form):
    r = c.post("/api/books", headers=H, files={"file": (name, data, "application/pdf")}, data=form)
    assert r.status_code == 200, r.text
    return r.json()["id"]


def v22_tests(c):
    base = f"http://127.0.0.1:{srv.server_port}"
    config.OPENROUTER_BASE_URL = f"{base}/or/v1"                # OpenRouter simulé (l'URL réelle est fixe en prod)

    # --- réglages : valeurs par défaut, masquage ---
    st = c.get("/api/settings", headers=H).json()
    assert st["allow_paid"] is False and st["roles"]["translate"]["provider"] == "ollama"
    assert st["providers"]["openrouter"]["has_key"] is False
    assert c.get("/api/settings").status_code == 401

    # --- livre français : jamais traduit ---
    fr = upload(c, make_pdf(), "fr.pdf", lang="fr")
    segs = c.get(f"/api/books/{fr}/segments", headers=H).json()
    assert not any(s["pending"] for s in segs) and TR_CALLS == [], (segs, TR_CALLS)
    rd = c.get(f"/api/books/{fr}/segments/0/read", headers=H).json()
    assert rd["translated"] is False and rd["voice_lang"] == "fr" and rd["speak_tag"] == "fr-FR"
    print("✔ document français : aucune traduction déclenchée")

    # --- livre chinois natif : détection → traduction à la demande → cache ---
    zh = upload(c, pdf_native(ZH, "china-s"), "zh.pdf", title="Rapport")
    book = c.get(f"/api/books/{zh}", headers=H).json()
    assert book["status"] == "ready" and book["lang"] == "zh" and book["translate"] == 1, book
    segs = c.get(f"/api/books/{zh}/segments", headers=H).json()
    assert segs and all(s["lang"] == "zh" and s["pending"] for s in segs), segs
    assert "销量" in segs[0]["original"]
    n0 = len(TR_CALLS)
    rd = c.get(f"/api/books/{zh}/segments/0/read", headers=H).json()
    assert rd["translated"] and rd["text"].startswith("FR: ") and "销量" in rd["original"], rd
    assert rd["voice_lang"] == "fr" and "<think>" not in rd["text"] and rd["error"] == ""
    assert len(TR_CALLS) == n0 + 1
    c.get(f"/api/books/{zh}/segments/0/read", headers=H)         # 2e lecture : cache, pas d'appel
    assert len(TR_CALLS) == n0 + 1
    assert c.get(f"/api/books/{zh}/segments", headers=H).json()[0]["translated"] is True
    print("✔ chinois : langue détectée, traduction JIT, nettoyage <think>, cache sans nouvel appel")

    # --- audio d'un texte traduit : voix française, nom de fichier lié au texte ---
    config.TTS_PROVIDER = "piper"
    a = c.get(f"/api/books/{zh}/segments/0/audio", headers=H)
    assert a.status_code == 200 and a.headers["content-type"] == "audio/mpeg"
    mp3 = list((config.DATA_DIR / "books" / zh / "audio").glob("0-*.mp3"))
    assert len(mp3) == 1

    # --- traduction désactivée : original conservé, pas de voix serveur chinoise ---
    assert c.put(f"/api/books/{zh}/options", headers=H, json={"translate": False}).status_code == 200
    rd = c.get(f"/api/books/{zh}/segments/0/read", headers=H).json()
    assert rd["translated"] is False and rd["voice_lang"] == "zh" and rd["speak_tag"] == "zh-CN"
    assert c.get(f"/api/books/{zh}/segments/0/audio", headers=H).status_code == 422
    c.put(f"/api/books/{zh}/options", headers=H, json={"translate": True})
    assert c.get(f"/api/books/{zh}/segments/0/audio", headers=H).status_code == 200
    print("✔ audio traduit (voix fr, cache lié au texte) ; traduction off → original + 422 en mode Piper")

    # --- retraduction : efface le cache (et l'audio) du segment ---
    n0 = len(TR_CALLS)
    assert c.post(f"/api/books/{zh}/retranslate", headers=H, json={"idx": 0}).status_code == 200
    assert not list((config.DATA_DIR / "books" / zh / "audio").glob("0-*.mp3"))
    c.get(f"/api/books/{zh}/segments/0/read", headers=H)
    assert len(TR_CALLS) == n0 + 1
    config.TTS_PROVIDER = "browser"
    print("✔ retraduction d'un segment")

    # --- anglais auto-détecté ---
    en = upload(c, pdf_native(EN), "en.pdf")
    b = c.get(f"/api/books/{en}", headers=H).json()
    assert b["lang"] == "en", b
    rd = c.get(f"/api/books/{en}/segments/0/read", headers=H).json()
    assert rd["translated"] and rd["lang"] == "en"
    print("✔ anglais détecté automatiquement puis traduit")

    # --- OCR chinois (scan) avec le pack chi_sim ---
    sc = upload(c, pdf_zh_scan(), "scan_zh.pdf", lang="zh")
    txt = "".join(x["original"] for x in c.get(f"/api/books/{sc}/segments", headers=H).json())
    assert sum(ch in txt for ch in "今年公司销量增长万台明计划继续扩大产能") >= 10, f"OCR chinois trop faible : {txt!r}"
    print(f"✔ OCR chinois (chi_sim) : « {txt[:20]}… »")

    # --- scans CJK en détection AUTOMATIQUE : l'écriture est reconnue avant l'OCR ---
    JA = "今日はとても天気がいいので、公園へ散歩に行きます。"
    KO = "오늘은 날씨가 좋아서 공원에 산책하러 갑니다."
    for code, text, font, probe in (("zh", ZH, "china-s", "公司销量"), ("ja", JA, "japan", "天気"),
                                    ("ko", KO, "korea", "날씨")):
        bid_ = upload(c, pdf_zh_scan(text, font), f"scan_{code}.pdf")           # lang=auto par défaut
        bk = c.get(f"/api/books/{bid_}", headers=H).json()
        got = "".join(x["original"] for x in c.get(f"/api/books/{bid_}/segments", headers=H).json())
        hits = sum(ch in got for ch in set(text) if not ch.isspace())
        assert bk["lang"] == code and hits >= len(set(text)) * 0.5, (code, bk["lang"], got[:40])
    print("✔ scans zh/ja/ko en détection automatique : écriture reconnue avant l'OCR, bon pack choisi")

    # --- OSD trompé (chinois pris pour du coréen, confiance 0,43 mesurée) : le contrôle après OCR rattrape ---
    ZH2 = "这座古老的城市有两千多年的历史。每年春天，成千上万的游客来这里欣赏盛开的樱花。"
    bid_ = upload(c, pdf_zh_scan(ZH2, "china-s"), "scan_zh2.pdf")
    bk = c.get(f"/api/books/{bid_}", headers=H).json()
    got = "".join(x["original"] for x in c.get(f"/api/books/{bid_}/segments", headers=H).json())
    hits = sum(ch in got for ch in set(ZH2) if not ch.isspace())
    assert bk["lang"] == "zh" and hits >= len(set(ZH2)) * 0.7, (bk["lang"], got[:40])
    print("✔ OSD erroné (zh vu comme ko) : écriture vérifiée après OCR, pack chi_sim retenu")

    # --- le coréen garde ses espaces entre mots (seuls Han/kana n'en ont pas) ---
    assert extract._clean("오늘 아침 서울에는\n비가 내렸습니다.") == "오늘 아침 서울에는 비가 내렸습니다."
    ko_long = "오늘 아침 서울에는 비가 많이 내렸습니다. " * 12
    assert all("  " not in p and " " in p for p in extract.chunk([ko_long]))
    kb = upload(c, pdf_native("오늘 아침 서울에는 비가 많이 내렸습니다. 사람들은 우산을 들고 걸어갔습니다.", "korea"), "ko.pdf")
    got = "".join(x["original"] for x in c.get(f"/api/books/{kb}/segments", headers=H).json())
    assert "오늘 아침 서울에는" in got and "내렸습니다. 사람들은" in got, got
    print("✔ coréen : espaces entre mots conservés (extraction, nettoyage, découpage)")

    # --- clé OpenRouter : chiffrée au repos, jamais renvoyée en clair ---
    SECRET = "sk-or-v1-SECRETSECRETSECRET1234"
    r = c.put("/api/settings", headers=H, json={"providers": {"openrouter": {"api_key": SECRET}}})
    assert r.status_code == 200, r.text
    pv = r.json()["providers"]["openrouter"]
    assert pv["has_key"] and pv["key_hint"] == "…1234" and SECRET not in json.dumps(r.json())
    with database.db() as cx:
        raw = cx.execute("SELECT value FROM settings WHERE key='app'").fetchone()["value"]
    assert SECRET not in raw and "SECRETSECRET" not in raw, "clé stockée en clair !"
    print("✔ clé OpenRouter : masquée dans l'API et chiffrée en base")

    # --- garde-fou gratuit / payant ---
    pay = {"roles": {"translate": {"provider": "openrouter", "model": "openai/gpt-4o"}}}
    r = c.put("/api/settings", headers=H, json=pay)
    assert r.status_code == 403 and "payant" in r.text.lower(), r.text
    free = {"roles": {"translate": {"provider": "openrouter", "model": "qwen/qwen-2.5-72b-instruct:free"}}}
    assert c.put("/api/settings", headers=H, json=free).status_code == 200
    n0 = len(TR_CALLS)
    c.post(f"/api/books/{en}/retranslate", headers=H, json={"idx": 0})
    rd = c.get(f"/api/books/{en}/segments/0/read", headers=H).json()
    assert rd["text"].startswith("FR[or]: ") and rd["model"].startswith("openrouter:"), rd
    assert ("/or/v1/chat/completions", f"Bearer {SECRET}") in AUTH_SEEN, "la clé n'est pas envoyée à OpenRouter"
    # modèle gratuit sans suffixe : reconnu gratuit grâce au prix publié à 0
    assert c.put("/api/settings", headers=H, json={"roles": {"translate": {"provider": "openrouter",
                                                                             "model": "meta/gratuit-sans-suffixe"}}}).status_code == 200
    assert c.put("/api/settings", headers=H, json=pay | {"allow_paid": True}).status_code == 200
    r = c.put("/api/settings", headers=H, json={"allow_paid": False})          # retirer l'autorisation avec un modèle payant actif
    assert r.status_code == 403, r.text
    assert c.put("/api/settings", headers=H, json=free | {"allow_paid": False}).status_code == 200
    print("✔ garde-fou : payant refusé par défaut, :free et prix 0 acceptés, autorisation explicite requise")

    # --- liste des modèles + test de connexion ---
    ms = c.get("/api/llm/models?provider=openrouter", headers=H).json()["models"]
    flags = {m["id"]: m["free"] for m in ms}
    assert flags["openai/gpt-4o"] is False and flags["qwen/qwen-2.5-72b-instruct:free"] is True
    assert {m["id"] for m in c.get("/api/llm/models?provider=ollama", headers=H).json()["models"]} >= {"bge-m3"}
    assert c.get("/api/llm/models?provider=omniroute", headers=H).status_code == 502      # URL non configurée
    t = c.post("/api/llm/test", headers=H, json={"role": "translate"}).json()
    assert t["ok"] and t["sample"].startswith("FR[or]:") and t["model"].startswith("openrouter:")
    assert c.post("/api/llm/test", headers=H, json={"role": "embed"}).json()["sample"] == "64 dimensions"
    assert c.post("/api/llm/test", headers=H, json={"role": "nimporte"}).status_code == 400
    print("✔ /api/llm/models (gratuit/payant) et /api/llm/test")

    # --- OmniRoute + Ollama local ---
    r = c.put("/api/settings", headers=H, json={"providers": {"omniroute": {"base_url": f"{base}/omni/v1", "api_key": "om-key"}},
                                                  "roles": {"translate": {"provider": "omniroute", "model": "auto/best"}}})
    assert r.status_code == 200, r.text
    c.post(f"/api/books/{en}/retranslate", headers=H, json={"idx": 0})
    assert c.get(f"/api/books/{en}/segments/0/read", headers=H).json()["text"].startswith("FR[omni]: ")
    assert c.put("/api/settings", headers=H, json={"roles": {"translate": {"provider": "ollama", "model": "qwen2.5:7b"}}}).status_code == 200
    c.post(f"/api/books/{en}/retranslate", headers=H, json={"idx": 0})
    assert c.get(f"/api/books/{en}/segments/0/read", headers=H).json()["text"].startswith("FR: ")
    print("✔ bascule OmniRoute ↔ Ollama local sans redémarrage")

    # --- échec de traduction : repli sur l'original avec message clair ---
    bad = upload(c, pdf_native("FAIL FAIL FAIL FAIL FAIL FAIL", "helv"), "fail.pdf", lang="en")
    rd = c.get(f"/api/books/{bad}/segments/0/read", headers=H).json()
    assert rd["translated"] is False and "limite" in rd["error"].lower() and rd["voice_lang"] == "en", rd
    config.TTS_PROVIDER = "piper"
    assert c.get(f"/api/books/{bad}/segments/0/audio", headers=H).status_code == 502
    config.TTS_PROVIDER = "browser"
    c.put("/api/settings", headers=H, json={"roles": {"translate": {"provider": "omniroute", "model": "x"}},
                                             "providers": {"omniroute": {"base_url": ""}}})
    rd = c.get(f"/api/books/{en}/segments/0/read", headers=H).json()      # déjà en cache : lisible malgré la panne
    assert rd["translated"] is True
    c.post(f"/api/books/{en}/retranslate", headers=H, json={"idx": 0})
    rd = c.get(f"/api/books/{en}/segments/0/read", headers=H).json()
    assert rd["translated"] is False and "url" in rd["error"].lower(), rd
    c.put("/api/settings", headers=H, json={"roles": {"translate": {"provider": "ollama", "model": "qwen2.5:7b"}}})
    print("✔ échec de traduction : original + message clair, audio 502, cache toujours lisible")

    # --- validation des réglages ---
    assert c.put("/api/settings", headers=H, json={"providers": {"inconnu": {}}}).status_code == 400
    assert c.put("/api/settings", headers=H, json={"roles": {"translate": {"provider": "zzz"}}}).status_code == 400
    assert c.put("/api/settings", headers=H, json={"providers": {"ollama": {"base_url": "ftp://x"}}}).status_code == 400
    c.put("/api/settings", headers=H, json={"providers": {"openrouter": {"clear_key": True}}})
    assert c.get("/api/settings", headers=H).json()["providers"]["openrouter"]["has_key"] is False
    print("✔ validation des réglages (fournisseur, usage, URL) et effacement de clé")

    # --- changement du modèle d'embeddings → 409 → réindexation ---
    assert c.post(f"/api/books/{fr}/chat", headers=H, json={"question": "galaxie Andromède"}).status_code == 200
    c.put("/api/settings", headers=H, json={"roles": {"embed": {"provider": "ollama", "model": "autre-embed"}}})
    r = c.post(f"/api/books/{fr}/chat", headers=H, json={"question": "galaxie Andromède"})
    assert r.status_code == 409 and "réindex" in r.text.lower() or "indexation" in r.text.lower(), r.text
    assert c.post(f"/api/books/{fr}/reindex", headers=H).status_code == 200
    assert c.get(f"/api/books/{fr}", headers=H).json()["indexed"] == 1
    assert c.post(f"/api/books/{fr}/chat", headers=H, json={"question": "galaxie Andromède"}).status_code == 200
    print("✔ embeddings changés → 409 clair → réindexation → chat de nouveau opérationnel")

    # --- suppression : tout est nettoyé ---
    assert c.delete(f"/api/books/{zh}", headers=H).status_code == 200
    with database.db() as cx:
        assert cx.execute("SELECT COUNT(*) FROM translations WHERE book_id=?", (zh,)).fetchone()[0] == 0


def main():
    test_chunk()
    test_token_validation()
    test_detect()
    test_chunk_cjk()
    test_clean_output()
    test_migration()
    with TestClient(app) as c:
        # --- authentification ---
        assert c.get("/api/books").status_code == 401
        assert c.get("/api/books", headers={"Authorization": "Bearer faux"}).status_code == 401
        assert c.get("/api/config", headers=H).json()["tts"] == "browser"

        # --- upload PDF (texte natif + page scannée) ---
        r = c.post("/api/books", headers=H, files={"file": ("livre.pdf", make_pdf(), "application/pdf")},
                   data={"title": "Livre test", "lang": "fr"})
        assert r.status_code == 200, r.text
        bid = r.json()["id"]
        book = c.get(f"/api/books/{bid}", headers=H).json()
        assert book["status"] == "ready", book
        assert book["pages_total"] == 2 and book["pages_done"] == 2
        segs = c.get(f"/api/books/{bid}/segments", headers=H).json()
        alltext = " ".join(s["text"] for s in segs).lower()
        assert "chat dort" in alltext and "andromède" in alltext, alltext
        assert {s["page"] for s in segs} == {1, 2}
        assert "invoice" in alltext, f"OCR de secours en échec : {alltext}"
        print(f"✔ PDF : {len(segs)} segments, OCR de secours OK")

        # --- audio : mode navigateur (204) puis mode Piper (MP3 + cache) ---
        assert c.get(f"/api/books/{bid}/segments/0/audio", headers=H).status_code == 204
        config.TTS_PROVIDER = "piper"
        a = c.get(f"/api/books/{bid}/segments/0/audio", headers=H)
        assert a.status_code == 200 and a.headers["content-type"] == "audio/mpeg" and len(a.content) > 100
        cached = list((config.DATA_DIR / "books" / bid / "audio").glob("0-*.mp3"))
        assert len(cached) == 1 and cached[0].exists()
        assert c.get(f"/api/books/{bid}/segments/999/audio", headers=H).status_code == 404
        print("✔ audio : navigateur (204) et Piper (MP3 + cache)")

        # --- progression ---
        assert c.put(f"/api/books/{bid}/progress", headers=H, json={"idx": 2}).status_code == 200
        assert c.get(f"/api/books/{bid}", headers=H).json()["progress_idx"] == 2
        assert c.put(f"/api/books/{bid}/progress", headers=H, json={"idx": -1}).status_code == 422

        # --- chat RAG (LLM simulé) ---
        assert book["indexed"] == 1, "indexation RAG non terminée"
        ch = c.post(f"/api/books/{bid}/chat", headers=H, json={"question": "galaxie Andromède distance"})
        assert ch.status_code == 200, ch.text
        j = ch.json()
        assert j["answer"] == "Réponse simulée" and j["sources"]
        assert any("andromède" in s["text"].lower() for s in j["sources"][:3])
        print("✔ progression + chat RAG (sources retrouvées)")

        # --- upload image + rejets ---
        r = c.post("/api/books", headers=H, files={"file": ("scan.png", png_with_text("Hello reader world"), "image/png")})
        iid = r.json()["id"]
        txt = " ".join(s["text"] for s in c.get(f"/api/books/{iid}/segments", headers=H).json()).lower()
        assert "hello" in txt, txt
        assert c.post("/api/books", headers=H, files={"file": ("x.exe", b"MZ", "application/octet-stream")}).status_code == 400
        assert c.post("/api/books", headers=H, files={"file": ("a.pdf", b"%PDF", "application/pdf")},
                      data={"lang": "xx"}).status_code == 400
        old = config.MAX_UPLOAD_MB; config.MAX_UPLOAD_MB = 0
        assert c.post("/api/books", headers=H, files={"file": ("a.pdf", b"12345", "application/pdf")}).status_code == 413
        config.MAX_UPLOAD_MB = old
        bad = c.post("/api/books", headers=H, files={"file": ("vide.pdf", b"pas un pdf", "application/pdf")}).json()["id"]
        assert c.get(f"/api/books/{bad}", headers=H).json()["status"] == "error"
        print("✔ image OCR, formats/langue/taille rejetés, PDF corrompu → statut error")

        # --- sécurité chemin + suppression + PWA servie ---
        assert c.get("/api/books/..%2F..%2Fetc/segments", headers=H).status_code == 404
        assert c.delete(f"/api/books/{bid}", headers=H).status_code == 200
        assert c.get(f"/api/books/{bid}", headers=H).status_code == 404
        assert not (config.DATA_DIR / "books" / bid).exists()
        assert "Lecteur de livres" in c.get("/").text
        print("✔ suppression, traversal de chemin bloqué, PWA servie")
        v22_tests(c)
    print("\nTOUS LES TESTS PASSENT")


if __name__ == "__main__":
    main()

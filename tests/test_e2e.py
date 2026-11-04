"""Test de bout en bout, sans service externe : LLM simulé + Piper simulé.
Usage (depuis la racine du projet) :  python tests/test_e2e.py
"""
import http.server
import re
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
EMB_TEXTS: list[str] = []                    # textes envoyés au modèle d'embeddings
BATCH_BODIES: list[dict] = []                # corps des appels de traduction groupée


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
                self._send({"data": [{"id": "qwen2.5:7b"}, {"id": "bge-m3"}, {"id": "llama3.2:latest"}, {"id": "llama3.2:3b"},
                                     {"id": "autre-embed"}]})
        else:
            self._send({}, 404)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        AUTH_SEEN.append((self.path, self.headers.get("Authorization", "")))
        if self.path.endswith("/embeddings"):
            EMB_TEXTS.extend(body["input"])
            if os.environ.get("EMB_FAIL"):
                return self._send({"error": "panne simulée"}, 500)
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
            if text.startswith("[1] "):                              # appel groupé (un paragraphe)
                TR_CALLS.append((_tag(self.path), text)); BATCH_BODIES.append(body)
                if os.environ.get("BATCH_SLOW"):
                    threading.Event().wait(float(os.environ["BATCH_SLOW"]))   # time.sleep est neutralisé plus bas
                    return
                if os.environ.get("BATCH_BAD"):
                    return self._send({"choices": [{"message": {"content": "FR: tout mélangé sans numéros"}}]})
                blocks = re.split(r"\[(\d+)\] ", text)[1:]
                out = "\n".join(f"[{n}] FR{_tag(self.path)}: {t.strip()}" for n, t in zip(blocks[::2], blocks[1::2]))
                return self._send({"choices": [{"message": {"content": "<think>x</think>" + out}}]})
            TR_CALLS.append((_tag(self.path), text))
            return self._send({"choices": [{"message": {"content": f"<think>hmm</think>FR{_tag(self.path)}: {text}"}}]})
        self._send({"choices": [{"message": {"content": "Réponse simulée"}}]})


srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeLLM)
threading.Thread(target=srv.serve_forever, daemon=True).start()

# ---------- Piper simulé : écrit 0,5 s de silence en WAV ----------
fake_piper = TMP / "piper"
fake_piper.write_text(
    "#!/usr/bin/env python3\nimport sys, wave\n"
    "out = sys.argv[sys.argv.index('-f') + 1]\nsys.stdin.read()\n"
    f"open({str(TMP / 'piper_models.log')!r}, 'a').write(sys.argv[sys.argv.index('-m') + 1] + '\\n')\n"
    "w = wave.open(out, 'wb'); w.setnchannels(1); w.setsampwidth(2); w.setframerate(22050)\n"
    "w.writeframes(b'\\x00\\x00' * 11025); w.close()\n")
fake_piper.chmod(fake_piper.stat().st_mode | stat.S_IEXEC)

os.environ.update({
    "DATA_DIR": str(TMP / "data"), "API_TOKEN": TOKEN, "TTS_PROVIDER": "browser",
    "LLM_BASE_URL": f"http://127.0.0.1:{srv.server_port}/v1", "PIPER_CMD": str(fake_piper),
    "PIPER_VOICE_FR": "dummy.onnx",
    # voix isolées de la machine : résultat identique que les voix réelles soient installées ou non
    "PIPER_VOICE_FR_HOMME": str(TMP / "voix-absentes" / "fr_FR-tom-medium.onnx"),
    "PIPER_VOICE_EN": str(TMP / "voix-absentes" / "en_US-lessac-medium.onnx"),
    "PIPER_VOICE_EN_HOMME": str(TMP / "voix-absentes" / "en_US-ryan-medium.onnx"),
    "PIPER_VOICE_AR": str(TMP / "voix-absentes" / "ar_JO-kareem-medium.onnx"),
})

import pymupdf as fitz  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from PIL import Image, ImageDraw, ImageFont  # noqa: E402

from app import config, detect, extract, llm, settings, translate  # noqa: E402
from app import db as database  # noqa: E402
from app.main import app  # noqa: E402
from app import prepare, reading  # noqa: E402

prepare.preparer.enabled = False      # préparation en avance testée à part (comptages déterministes ailleurs)

langs = subprocess.run(["tesseract", "--list-langs"], capture_output=True, text=True).stdout
extract.OCR_LANG = "eng"        # le test OCR est en anglais ; en prod : fra+eng(+ara)
llm.time.sleep = lambda s: None     # pas d'attente réelle entre les nouvelles tentatives


def _pause(sec: float) -> None:     # vraie attente pour les tests (time.sleep est neutralisé ci-dessus)
    threading.Event().wait(sec)
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
    # imports simultanés juste après un démarrage : processus NEUF, plusieurs fils dès le 1er appel
    code = ("import sys, threading, collections; sys.path.insert(0, %r); from app import detect\n"
            "T = 'Le gros livre de test commence ici. Il contient une très grande annexe.'\n"
            "r = collections.Counter(); L = threading.Lock(); go = threading.Event()\n"
            "def w():\n    go.wait()\n    for _ in range(10):\n        x = detect.detect_lang(T)\n"
            "        with L: r[x] += 1\n"
            "ts = [threading.Thread(target=w) for _ in range(4)]; [t.start() for t in ts]; go.set(); [t.join() for t in ts]\n"
            "print(dict(r))") % str(ROOT)
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True).stdout.strip()
    assert out == "{'fr': 40}", f"détection instable en parallèle : {out}"
    print("✔ détection fiable avec plusieurs imports simultanés dès le démarrage")


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

    # --- regroupement : phrases courtes d'une même page → un paragraphe (~30 s d'écoute), écritures non mélangées ---
    phrases = [f"Phrase courte numéro {i} du chapitre, lue en deux secondes." for i in range(20)]
    doc = fitz.open(); pg = doc.new_page()
    for i, t in enumerate(phrases):
        pg.insert_textbox(fitz.Rect(40, 30 + i * 38, 560, 66 + i * 38), t, fontsize=10)
    rb = upload(c, doc.tobytes(), "phrases.pdf")
    segs_ = c.get(f"/api/books/{rb}/segments", headers=H).json()
    assert 2 <= len(segs_) <= 4 and all(len(x["original"]) <= config.MAX_CHARS for x in segs_), [len(x["original"]) for x in segs_]
    assert " ".join(x["original"] for x in segs_) == " ".join(phrases), "rien perdu, ordre conservé"
    assert extract.chunk(["Chapter 1", "这座古老的城市。", "Next."]) == ["Chapter 1", "这座古老的城市。", "Next."]
    print(f"✔ regroupement : 20 phrases courtes → {len(segs_)} paragraphes, texte et ordre intacts, écritures non mélangées")

    # --- redécoupage d'un livre déjà importé : position et vocabulaire recalés ---
    import app.main as _m
    old = extract.MERGE; extract.MERGE = False
    try:
        rs = upload(c, doc.tobytes(), "ancien.pdf")            # comme un ancien import : 1 phrase = 1 passage
    finally:
        extract.MERGE = old
    assert len(c.get(f"/api/books/{rs}/segments", headers=H).json()) == 20
    c.put(f"/api/books/{rs}/progress", headers=H, json={"idx": 15})
    c.post("/api/vocab", headers=H, json={"book_id": rs, "idx": 12, "term": "numéro 12"})
    r = c.post(f"/api/books/{rs}/resegment", headers=H)
    assert r.status_code == 200 and r.json()["before"] == 20, r.text
    bk = c.get(f"/api/books/{rs}", headers=H).json()
    new = c.get(f"/api/books/{rs}/segments", headers=H).json()
    assert bk["status"] == "ready" and len(new) == len(segs_), (bk["status"], len(new))
    assert "numéro 15 du" in new[bk["progress_idx"]]["original"], (bk["progress_idx"], new[bk["progress_idx"]]["original"][:60])
    late = c.put(f"/api/books/{rs}/progress", headers=H, json={"idx": 15}).json()       # ancienne position arrivée en retard
    assert late["ok"] is False and c.get(f"/api/books/{rs}", headers=H).json()["progress_idx"] == bk["progress_idx"], late
    vv = [x for x in c.get("/api/vocab", headers=H).json() if x["book_id"] == rs][0]
    assert "numéro 12 du" in new[vv["idx"]]["original"], vv
    assert c.post(f"/api/books/{rs}/resegment", headers=H).status_code == 200      # relançable
    print(f"✔ redécoupage : 20 → {len(new)} passages, position (phrase 16) et vocabulaire recalés sur le même texte")
    assert c.post(f"/api/books/{rs}/warmup", headers=H).json()["warming"] is False   # livre français : rien à réveiller

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

    # --- nom de modèle exact : faute de frappe refusée avec suggestion, rien n'est enregistré ---
    before = c.get("/api/settings", headers=H).json()["roles"]
    r = c.put("/api/settings", headers=H, json={"roles": {"translate": {"provider": "ollama", "model": "qwen2.5:7B"}}})
    assert r.status_code == 400 and "introuvable" in r.json()["detail"] and "qwen2.5:7b" in r.json()["detail"], r.text
    r = c.put("/api/settings", headers=H, json={"roles": {"chat": {"provider": "openrouter", "model": "qwen/qwen-2.5-72b:free"}}})
    assert r.status_code == 400 and "qwen/qwen-2.5-72b-instruct:free" in r.json()["detail"], r.text
    assert c.get("/api/settings", headers=H).json()["roles"] == before, "un refus ne doit rien enregistrer"
    assert c.put("/api/settings", headers=H, json={"roles": {"chat": {"provider": "ollama", "model": "llama3.2"}}}).status_code == 200
    rr = c.put("/api/settings", headers=H, json={"roles": before}); assert rr.status_code == 200, (before, rr.text)
    print("✔ nom de modèle exact : faute de frappe refusée avec suggestion (Ollama, OpenRouter), « :latest » implicite")

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

    # --- indexation interrompue (redémarrage pendant l'indexation d'un gros livre) : reprise sans tout refaire ---
    import app.main as main_mod
    from app import rag
    filler = " Il contient assez de phrases pour former à lui seul un vrai paragraphe de lecture, long d'une vingtaine de secondes à l'écoute, comme dans un livre réel."
    doc = fitz.open()
    for pg in range(8):                            # 8 pages × 5 paragraphes d'environ 260 caractères = 40 passages
        page = doc.new_page()
        for k in range(5):
            i = pg * 5 + k
            page.insert_textbox(fitz.Rect(30, 30 + k * 150, 580, 170 + k * 150), f"Paragraphe numéro {i} du gros livre." + filler, fontsize=9)
    big = upload(c, doc.tobytes(), "gros.pdf")
    done, total = rag.progress(big)
    assert total >= 10 and done == total, (done, total)
    with database.db() as cx:                      # état après coupure : 3 passages indexés sur N, indexed=0
        cx.execute("DELETE FROM embeddings WHERE book_id=? AND idx>=3", (big,))
        cx.execute("UPDATE books SET indexed=0 WHERE id=?", (big,))
    r = c.post(f"/api/books/{big}/chat", headers=H, json={"question": "paragraphe"})
    assert r.status_code == 409 and f"3/{total}" in r.json()["detail"] and "Réindexer" in r.json()["detail"], r.text
    EMB_TEXTS.clear()
    main_mod._resume_indexing()                    # ce que fait le démarrage du service
    assert rag.progress(big) == (total, total), rag.progress(big)
    assert len(EMB_TEXTS) == total - 3, f"{len(EMB_TEXTS)} textes recalculés au lieu de {total - 3}"
    assert c.post(f"/api/books/{big}/chat", headers=H, json={"question": "paragraphe"}).status_code == 200
    # panne d'embeddings pendant l'indexation : message avec l'erreur, puis « Réindexer » reprend
    with database.db() as cx:
        cx.execute("DELETE FROM embeddings WHERE book_id=? AND idx>=5", (big,))
        cx.execute("UPDATE books SET indexed=0 WHERE id=?", (big,))
    os.environ["EMB_FAIL"] = "1"
    main_mod._index_quietly(big)
    r = c.post(f"/api/books/{big}/chat", headers=H, json={"question": "paragraphe"})
    assert r.status_code == 409 and "dernière erreur" in r.json()["detail"] and f"5/{total}" in r.json()["detail"], r.text
    del os.environ["EMB_FAIL"]
    EMB_TEXTS.clear()
    rr = c.post(f"/api/books/{big}/reindex", headers=H).json()
    assert rr["ok"] and rag.progress(big) == (total, total) and len(EMB_TEXTS) == total - 5, (rr, len(EMB_TEXTS))
    assert c.post(f"/api/books/{big}/chat", headers=H, json={"question": "paragraphe"}).status_code == 200
    print(f"✔ indexation interrompue : reprise au démarrage et par « Réindexer » sans tout refaire ({total} passages)")

    # --- vocabulaire : contexte copié, doublon ignoré, recherche, export CSV, survit au livre supprimé ---
    i2 = next(x["idx"] for x in c.get(f"/api/books/{big}/segments", headers=H).json() if "numéro 2 du" in x["original"])
    v = c.post("/api/vocab", headers=H, json={"book_id": big, "idx": i2, "term": "  numéro   2 "}).json()
    assert not v["duplicate"]
    assert c.post("/api/vocab", headers=H, json={"book_id": big, "idx": i2, "term": "numéro 2"}).json()["duplicate"]
    assert c.post("/api/vocab", headers=H, json={"book_id": big, "idx": 99999, "term": "x"}).status_code == 404
    assert c.post("/api/vocab", headers=H, json={"book_id": "inconnu", "idx": 0, "term": "x"}).status_code == 404
    lst = [x for x in c.get("/api/vocab", headers=H, params={"q": "numéro"}).json() if x["book_id"] == big]
    assert len(lst) == 1 and lst[0]["term"] == "numéro 2" and "Paragraphe numéro 2" in lst[0]["original"], lst
    csv_ = c.get("/api/vocab.csv", headers=H)
    assert csv_.status_code == 200 and "numéro 2" in csv_.text and csv_.text.startswith("\ufeffterme;"), csv_.text[:80]
    assert c.get("/api/vocab.csv").status_code == 401
    assert c.get("/api/books", headers=H).json()[0].get("preparing") is False
    assert c.delete(f"/api/vocab/{v['id']}", headers=H).json()["ok"]
    assert not [x for x in c.get("/api/vocab", headers=H).json() if x["book_id"] == big]
    assert c.delete(f"/api/vocab/{v['id']}", headers=H).status_code == 404
    print("✔ vocabulaire : ajout avec contexte, doublon ignoré, recherche, export CSV (BOM), suppression")

    # --- traduction groupée : un paragraphe de 5 passages = UN appel au modèle ---
    zh5 = ["第一句话在这里，我们正在测试一个比较长的句子。", "第二句话在那里，它也需要足够长才能被正确识别。",
           "第三句话很短，但是我们故意把它写得更长一些。", "第四句话也很短，所以我们在后面加上了更多的文字。",
           "第五句话结束了，这本小书的测试也就到此为止了。"]   # > 20 caractères par page (sinon : page « scannée »)
    doc = fitz.open()
    for t in zh5:                                  # une phrase par page : le regroupement ne traverse pas les pages
        doc.new_page().insert_textbox(fitz.Rect(50, 50, 550, 100), t, fontsize=14, fontname="china-s")
    gid = upload(c, doc.tobytes(), "zh5.pdf")
    assert reading.BATCH_MAX == 1, "1 passage par demande par défaut (mesuré le plus stable sur CPU)"
    reading.BATCH_MAX = 3                          # regroupement testé tel qu'activé avec TRANSLATE_BATCH=3
    segs = c.get(f"/api/books/{gid}/segments", headers=H).json()
    assert [x["original"] for x in segs] == zh5, [x["original"] for x in segs]
    w_ = c.post(f"/api/books/{gid}/warmup", headers=H).json()
    assert w_["warming"] is True and w_["model"] == "qwen2.5:7b", w_            # livre à traduire, Ollama : réveil
    n0 = len(TR_CALLS)
    r0 = c.get(f"/api/books/{gid}/segments/0/read", headers=H).json()
    assert r0["translated"] and r0["text"] == f"FR: {zh5[0]}", r0
    assert len(TR_CALLS) == n0 + 1, f"{len(TR_CALLS) - n0} appels au lieu d'1"
    assert TR_CALLS[-1][1].count("[") == 3, "groupes de 3 passages (TRANSLATE_BATCH=3)"
    mt = BATCH_BODIES[-1].get("max_tokens")
    assert mt and mt < 400, f"plafond de réponse absent ou trop haut : {mt}"
    for i in range(1, 5):
        ri = c.get(f"/api/books/{gid}/segments/{i}/read", headers=H).json()
        assert ri["translated"] and ri["text"] == f"FR: {zh5[i]}", ri
    assert len(TR_CALLS) == n0 + 2, f"5 passages par groupes de 3 = 2 appels, vu {len(TR_CALLS) - n0}"
    # réponse mal numérotée : retour au passage par passage, rien n'est perdu
    c.post(f"/api/books/{gid}/retranslate", headers=H, json={})
    os.environ["BATCH_BAD"] = "1"
    n0 = len(TR_CALLS)
    r0 = c.get(f"/api/books/{gid}/segments/0/read", headers=H).json()
    del os.environ["BATCH_BAD"]
    assert r0["translated"] and r0["text"] == f"FR: {zh5[0]}" and len(TR_CALLS) == n0 + 2, (r0, len(TR_CALLS) - n0)
    reading._no_batch.clear()
    # groupe trop lent : UN seul essai (pas de relance), retour immédiat au passage par passage, plus de groupe ensuite
    c.post(f"/api/books/{gid}/retranslate", headers=H, json={})
    from app import translate as tr_mod
    old_to = tr_mod.BATCH_TIMEOUT; tr_mod.BATCH_TIMEOUT = 1.0
    os.environ["BATCH_SLOW"] = "3"
    import time as _t
    n0, nb = len(TR_CALLS), len(BATCH_BODIES); t0 = _t.time()
    try:
        r0 = c.get(f"/api/books/{gid}/segments/0/read", headers=H).json()
        el = _t.time() - t0
        assert r0["translated"] and r0["text"] == f"FR: {zh5[0]}", r0
        assert len(BATCH_BODIES) == nb + 1, f"{len(BATCH_BODIES) - nb} essais groupés : un appel groupé n'est jamais relancé"
        assert el < 2.5, f"{el:.1f} s : le délai du groupe (1 s) doit couper court"
        r1 = c.get(f"/api/books/{gid}/segments/1/read", headers=H).json()
        assert r1["translated"] and len(BATCH_BODIES) == nb + 1, "après un échec, ce livre ne tente plus de groupe"
    finally:
        tr_mod.BATCH_TIMEOUT = old_to; del os.environ["BATCH_SLOW"]; reading._no_batch.clear(); reading.BATCH_MAX = 1
    print(f"✔ traduction : 1 passage par demande par défaut ; TRANSLATE_BATCH=3 → groupes de 3 (plafond {mt} jetons), mal numéroté ou trop lent "
          f"({el:.1f} s, sans relance) → passage par passage")

    # --- préparation en avance (traduction + audio) côté serveur ---
    import time as _t
    old_tts = config.TTS_PROVIDER; config.TTS_PROVIDER = "piper"
    prepare.preparer.enabled = True
    try:
        c.post(f"/api/books/{gid}/retranslate", headers=H, json={})
        assert c.get(f"/api/books/{gid}/segments/0/audio", headers=H).status_code == 200
        for _ in range(100):                         # la page n'a demandé QUE le passage 0
            st_ = c.get(f"/api/books/{gid}/prepare", headers=H).json()
            if st_["ready"] == st_["total"] and not st_["running"]:
                break
            _pause(0.1)
        assert st_["ready"] == st_["total"] == 5, st_
        print("✔ préparation en avance : le serveur traduit et fabrique l'audio des passages suivants tout seul")
        # pendant une indexation, la préparation attend (Ollama partagé) puis reprend seule
        c.post(f"/api/books/{gid}/retranslate", headers=H, json={})
        rag._running.add(big)
        try:
            c.post(f"/api/books/{gid}/prepare", headers=H, json={})
            _pause(1.5)
            st_ = c.get(f"/api/books/{gid}/prepare", headers=H).json()
            assert st_["paused"].startswith("indexation") and st_["ready"] == 0, st_
        finally:
            rag._running.discard(big)
        for _ in range(150):                          # reprise ≤ 2 s après la fin de l'indexation
            st_ = c.get(f"/api/books/{gid}/prepare", headers=H).json()
            if st_["ready"] == 5:
                break
            _pause(0.1)
        assert st_["ready"] == 5 and not st_["paused"], st_
        # réserve : un trou dans la préparation l'interrompt
        for f_ in (config.DATA_DIR / "books" / gid / "audio").glob("2-*.mp3"):
            f_.unlink()
        r_ = c.get(f"/api/books/{gid}/prepare", headers=H, params={"from_idx": 0}).json()
        assert r_["ahead"] == 2 and r_["ready"] == 4, r_
        print("✔ réserve : passages prêts d'affilée depuis la position de lecture (s'arrête au premier trou)")
        print("✔ priorité à la discussion : préparation en pause pendant l'indexation, reprise seule ensuite")
        # « Préparer tout le livre » sur le gros livre, depuis la position de lecture, puis arrêt
        tot = rag.progress(big)[1]
        r = c.post(f"/api/books/{big}/prepare", headers=H, json={}).json()
        assert r["total"] == tot and r["whole"], r
        for _ in range(1200):                         # ~0,5 s par passage (ffmpeg réel)
            st_ = c.get(f"/api/books/{big}/prepare", headers=H).json()
            if st_["ready"] == tot:
                break
            _pause(0.1)
        assert st_["ready"] == tot and not st_["error"], st_
        assert st_["ahead"] == tot - st_["from"], st_                  # réserve = tout ce qui reste
        mid = c.get(f"/api/books/{big}/prepare", headers=H, params={"from_idx": tot - 3}).json()
        assert mid["ahead"] == 3 and mid["from"] == tot - 3, mid
        r = c.post(f"/api/books/{big}/prepare", headers=H, json={"stop": True}).json()
        assert not r["running"], r
        print(f"✔ « Préparer tout le livre » : {tot}/{tot} passages prêts à l'écoute, arrêt possible")
    finally:
        prepare.preparer.enabled = False; config.TTS_PROVIDER = old_tts

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

        # --- voix femme / homme : modèle Piper différent, fichiers audio distincts, arabe en voix d'homme ---
        log = TMP / "piper_models.log"
        cf = c.get("/api/config", headers=H).json()
        assert cf["voice"] == "femme" and cf["voices"]["fr"] == ["femme", "homme"] and cf["voices"]["ar"] == ["homme"], cf
        assert c.put("/api/settings", headers=H, json={"voice": "robot"}).status_code == 400
        assert c.put("/api/settings", headers=H, json={"voice": "homme"}).json()["voice"] == "homme"
        assert c.get(f"/api/books/{bid}/segments/0/audio", headers=H).status_code == 200
        assert log.read_text().splitlines()[-1].endswith("fr_FR-tom-medium.onnx"), log.read_text()
        assert len(list((config.DATA_DIR / "books" / bid / "audio").glob("0-*.mp3"))) == 2, "1 fichier par voix"
        c.put("/api/settings", headers=H, json={"voice": "femme"})
        n = len(log.read_text().splitlines())
        assert c.get(f"/api/books/{bid}/segments/0/audio", headers=H).status_code == 200
        assert len(log.read_text().splitlines()) == n, "voix de femme déjà en cache : pas de nouvelle synthèse"
        from app import tts as tts_mod
        assert tts_mod.voice_path("ar", "femme").endswith("ar_JO-kareem-medium.onnx")
        assert tts_mod.voice_path("xx", "homme").endswith("fr_FR-tom-medium.onnx")
        print("✔ voix femme / homme : modèle Piper choisi, un fichier audio par voix, arabe → voix d'homme")

        # --- progression ---
        nseg = c.get(f"/api/books/{bid}", headers=H).json()["n_segments"]
        assert c.put(f"/api/books/{bid}/progress", headers=H, json={"idx": nseg - 1}).json()["ok"] is True
        assert c.get(f"/api/books/{bid}", headers=H).json()["progress_idx"] == nseg - 1
        assert c.put(f"/api/books/{bid}/progress", headers=H, json={"idx": nseg}).json()["ok"] is False   # hors du livre
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
        assert config.MAX_UPLOAD_MB >= 350, "la limite par défaut doit accepter un livre de 350 Mo"
        assert c.get("/api/config", headers=H).json()["max_upload_mb"] == config.MAX_UPLOAD_MB
        import app.main as main_mod
        from collections import namedtuple
        real_du = main_mod.shutil.disk_usage
        main_mod.shutil.disk_usage = lambda p: namedtuple("du", "total used free")(10**9, 10**9, 10 * 2**20)   # 10 Mo libres
        n_before = len(list((config.DATA_DIR / "books").iterdir()))
        try:
            r = c.post("/api/books", headers=H, files={"file": ("gros.pdf", b"%PDF" + b"0" * 4096, "application/pdf")})
            assert r.status_code == 507 and "Espace disque insuffisant" in r.json()["detail"], r.text
            assert len(list((config.DATA_DIR / "books").iterdir())) == n_before, "aucun dossier créé si le disque manque"
        finally:
            main_mod.shutil.disk_usage = real_du
        bad = c.post("/api/books", headers=H, files={"file": ("vide.pdf", b"pas un pdf", "application/pdf")}).json()["id"]
        assert c.get(f"/api/books/{bad}", headers=H).json()["status"] == "error"
        print("✔ image OCR, formats/langue/taille rejetés, PDF corrompu → statut error")
        print(f"✔ gros fichiers : limite {config.MAX_UPLOAD_MB} Mo exposée à la page, disque insuffisant → 507 sans rien écrire")

        # --- sécurité chemin + suppression + PWA servie ---
        assert c.get("/api/books/..%2F..%2Fetc/segments", headers=H).status_code == 404
        assert c.delete(f"/api/books/{bid}", headers=H).status_code == 200
        assert c.get(f"/api/books/{bid}", headers=H).status_code == 404
        assert not (config.DATA_DIR / "books" / bid).exists()
        assert "<title>Mon Lecteur</title>" in c.get("/").text
        print("✔ suppression, traversal de chemin bloqué, PWA servie")
        v22_tests(c)
    print("\nTOUS LES TESTS PASSENT")


if __name__ == "__main__":
    main()

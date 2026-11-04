"""Validations RÉELLES contre une instance en service (vrai Piper, vrai Tesseract, vrais LLM).

Contrairement à tests/test_e2e.py (tout simulé), ce script parle au vrai serveur et aux vrais modèles.
Chaque étape écrit une preuve (sortie mesurée) ; rien n'est supposé.

Usage (depuis /opt/lecteur, avec le venv du projet) :
  venv/bin/python tools/validate_real.py piper                 # 6a  synthèse fr / en / ar
  venv/bin/python tools/validate_real.py fr                    # 6b  PDF natif français + audio + chat RAG
  venv/bin/python tools/validate_real.py cjk                   # 6c  3 PDF natifs + 3 PDF scannés zh/ja/ko
  venv/bin/python tools/validate_real.py bench [--omni-model M]  # 6d  qwen2.5:7b (Ollama) vs OmniRoute
  venv/bin/python tools/validate_real.py lot [ID|auto] [N]     # mesure : groupes de traduction 1 / 3 / 8
  venv/bin/python tools/validate_real.py snapshot FICHIER      # 6g  état avant redémarrage
  venv/bin/python tools/validate_real.py compare FICHIER       # 6g  comparaison après redémarrage
Variables : LECTEUR_URL (défaut http://127.0.0.1:<APP_PORT du .env>), API_TOKEN (sinon lu dans .env),
            OMNI_BASE_URL, OMNI_KEY, OMNI_MODEL (benchmark OmniRoute).
"""
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx
import pymupdf as fitz

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _env_file() -> dict:
    out = {}
    f = ROOT / ".env"
    if f.exists():
        for line in f.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip()
    return out


ENV = {**_env_file(), **{k: v for k, v in os.environ.items() if k in ("API_TOKEN", "PIPER_CMD")}}
for _k, _v in ENV.items():          # les modules de l'app (importés plus bas) lisent leur configuration ici
    os.environ.setdefault(_k, _v)
URL = os.getenv("LECTEUR_URL", f"http://127.0.0.1:{ENV.get('APP_PORT') or 8000}").rstrip("/")
TOKEN = ENV.get("API_TOKEN", "")
H = {"Authorization": f"Bearer {TOKEN}"}
OK, KO = "✔", "✖"


def api(method: str, path: str, timeout: float = 60, **kw):
    r = httpx.request(method, URL + path, headers=H, timeout=timeout, **kw)
    return r


def upload(name: str, data: bytes, lang: str = "auto") -> str:
    r = api("POST", "/api/books", files={"file": (name, data, "application/pdf")},
            data={"title": name, "lang": lang})
    r.raise_for_status()
    return r.json()["id"]


def wait_ready(book_id: str, timeout: float = 900, indexed: bool = False) -> dict:
    t0 = time.time()
    while time.time() - t0 < timeout:
        b = api("GET", f"/api/books/{book_id}").json()
        if b["status"] == "error":
            return b
        if b["status"] == "ready" and (not indexed or b["indexed"]):
            return b
        time.sleep(2)
    return api("GET", f"/api/books/{book_id}").json()


def segments(book_id: str) -> list[dict]:
    return api("GET", f"/api/books/{book_id}/segments?limit=500").json()


# ---------- 6a : Piper ----------
def cmd_piper() -> int:
    cmd = ENV.get("PIPER_CMD", str(ROOT / "venv/bin/piper"))
    voices = {"fr": (ENV.get("PIPER_VOICE_FR"), "Bonjour, ceci est une vraie synthèse vocale en français."),
              "en": (ENV.get("PIPER_VOICE_EN"), "Hello, this is a real English speech synthesis test."),
              "ar": (ENV.get("PIPER_VOICE_AR"), "مرحبا، هذا اختبار حقيقي لتحويل النص إلى كلام.")}
    bad = 0
    for lang, (voice, text) in voices.items():
        out = Path(tempfile.mkdtemp()) / f"{lang}.wav"
        t0 = time.time()
        p = subprocess.run([cmd, "-m", voice or "", "-f", str(out)], input=text.encode(), capture_output=True)
        dt = time.time() - t0
        if p.returncode or not out.exists() or out.stat().st_size < 10000:
            bad += 1
            print(f"{KO} Piper {lang} ({voice}) : code {p.returncode} — {p.stderr.decode()[-300:]}")
            continue
        vol = subprocess.run(["ffmpeg", "-hide_banner", "-i", str(out), "-af", "volumedetect", "-f", "null", "-"],
                             capture_output=True, text=True).stderr
        mean = next((ln.split("mean_volume:")[1].strip() for ln in vol.splitlines() if "mean_volume" in ln), "?")
        dur = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(out)],
                             capture_output=True, text=True).stdout.strip()
        print(f"{OK} Piper {lang} : {Path(voice).name}, {out.stat().st_size} octets, durée {dur} s, "
              f"volume moyen {mean}, synthèse en {dt:.1f} s")
    return bad


# ---------- 6b : PDF natif français ----------
FR_TEXT = [
    "Le phare de Kerbrat se dresse sur un rocher battu par les vagues. Son gardien, Yann Le Goff, "
    "y vit seul depuis vingt-trois ans et allume la lanterne chaque soir à la tombée de la nuit.",
    "Chaque matin, Yann note dans un carnet la direction du vent, la hauteur de la houle et le nombre "
    "de bateaux de pêche aperçus. Il a ainsi rempli quarante-sept carnets.",
    "Un jour d'hiver, une tempête arracha la porte de la réserve de pétrole. Yann répara la lanterne "
    "à la lueur d'une bougie et le phare ne s'éteignit pas une seule nuit.",
]


def cmd_fr() -> int:
    doc = fitz.open()
    p = doc.new_page()
    y = 60
    for para in FR_TEXT:
        p.insert_textbox(fitz.Rect(50, y, 550, y + 150), para, fontsize=12)
        y += 160
    bid = upload("validation-fr.pdf", doc.tobytes())
    b = wait_ready(bid, timeout=600)
    if b["status"] == "ready":
        b = wait_ready(bid, indexed=True, timeout=240)     # embeddings bge-m3 : quelques secondes en principe
    print(f"Livre {bid} : statut={b['status']} langue={b['lang']} indexé={b['indexed']} segments={b['n_segments']}"
          + (f" erreur={b['error']}" if b.get("error") else ""))
    bad = 0
    segs = segments(bid)
    tr = [s for s in segs if s["translated"] or s["pending"]]
    if b["lang"] == "fr" and not tr:
        print(f"{OK} aucune traduction déclenchée ({len(segs)} segments, tous lang ∈ {sorted({s['lang'] for s in segs})})")
    else:
        bad += 1
        print(f"{KO} traduction déclenchée ou langue ≠ fr : {tr[:2]}")
    rd = api("GET", f"/api/books/{bid}/segments/0/read").json()
    print(f"   /read #0 : translated={rd['translated']} voice_lang={rd['voice_lang']}")
    t0 = time.time()
    a = api("GET", f"/api/books/{bid}/segments/0/audio", timeout=300)
    if a.status_code == 200 and a.headers.get("content-type", "").startswith("audio/mpeg") and len(a.content) > 5000:
        print(f"{OK} audio généré : {len(a.content)} octets MP3 en {time.time() - t0:.1f} s")
    else:
        bad += 1
        print(f"{KO} audio : HTTP {a.status_code} {a.text[:200]}")
    if not b["indexed"]:
        bad += 1
        print(f"{KO} livre non indexé (embeddings) : chat impossible — vérifier `ollama list` (bge-m3)")
        return bad
    t0 = time.time()
    c = api("POST", f"/api/books/{bid}/chat", json={"question": "Combien de carnets le gardien a-t-il remplis ?"},
            timeout=400)
    if c.status_code == 200 and c.json().get("sources"):
        j = c.json()
        print(f"{OK} chat RAG en {time.time() - t0:.1f} s, {len(j['sources'])} sources "
              f"(pages {sorted({s['page'] for s in j['sources']})})")
        print(f"   Réponse : {j['answer'].strip()[:400]}")
        print(f"   1re source : {j['sources'][0]['text'][:120]}…")
    else:
        bad += 1
        print(f"{KO} chat : HTTP {c.status_code} {c.text[:300]}")
    return bad


# ---------- 6c : CJK natifs et scannés ----------
CJK = {
    "zh": ("china-s", "今年公司的销量增长了3.5万台，明年计划继续扩大产能。我们相信技术创新是企业发展的核心动力。",
           "这座古老的城市有两千多年的历史。每年春天，成千上万的游客来这里欣赏盛开的樱花。"),
    "ja": ("japan", "昨日、私は友達と一緒に京都の古いお寺を訪れました。秋の紅葉がとても美しかったです。",
           "この町の図書館は毎朝九時に開きます。子どもたちは放課後にここで宿題をします。"),
    "ko": ("korea", "오늘 아침 서울에는 비가 많이 내렸습니다. 사람들은 우산을 들고 지하철역으로 서둘러 걸어갔습니다.",
           "우리 할머니는 매일 아침 정원에서 꽃에 물을 주십니다. 정원에는 장미와 국화가 가득합니다."),
}
LANG_FR = {"zh": "chinois", "ja": "japonais", "ko": "coréen"}


def _native(text: str, font: str) -> bytes:
    d = fitz.open()
    d.new_page().insert_textbox(fitz.Rect(50, 50, 550, 400), text, fontsize=16, fontname=font)
    return d.tobytes()


def _scanned(text: str, font: str) -> tuple[bytes, bytes]:
    src = fitz.open()
    sp = src.new_page()
    sp.insert_textbox(fitz.Rect(40, 40, 560, 500), text, fontsize=24, fontname=font)
    png = sp.get_pixmap(dpi=200).tobytes("png")
    d = fitz.open()
    d.new_page().insert_image(fitz.Rect(0, 0, 595, 842), stream=png)
    return d.tobytes(), png


def _overlap(src: str, got: str) -> float:
    chars = [ch for ch in src if ch.isalpha()]
    return sum(ch in got for ch in chars) / max(1, len(chars))


def cmd_cjk() -> int:
    import io
    from PIL import Image
    from app import extract
    bad, rows = 0, []
    for lang, (font, native_text, scan_text) in CJK.items():
        for kind, text in (("natif", native_text), ("scanné", scan_text)):
            if kind == "natif":
                data, script = _native(text, font), "—"
            else:
                data, png = _scanned(text, font)
                img = Image.open(io.BytesIO(png))
                script = extract.detect_script_lang(img) or "indécidable"
                chosen = extract._auto_lang(img)          # même décision que le serveur (OSD + essai des packs)
            bid = upload(f"validation-{lang}-{kind}.pdf", data)
            b = wait_ready(bid, timeout=600)
            segs = segments(bid)
            orig = "".join(s["original"] for s in segs)
            ov = _overlap(text, orig)
            ok = b["status"] == "ready" and b["lang"] == lang and ov >= (0.98 if kind == "natif" else 0.7)
            pack = f"OSD={script} → pack retenu {extract._ocr_lang(chosen)}" if kind == "scanné" else "pas d'OCR (couche texte)"
            print(f"{OK if ok else KO} {lang} {kind} : statut={b['status']} langue détectée={b['lang']} "
                  f"{pack} ; texte retrouvé {ov:.0%}")
            bad += not ok
            for s in segs:
                t0 = time.time()
                r = api("GET", f"/api/books/{bid}/segments/{s['idx']}/read", timeout=600).json()
                dt = time.time() - t0
                if r["error"] or not r["translated"]:
                    bad += 1
                    print(f"   {KO} #{s['idx']} traduction : {r['error'] or 'non traduit'}")
                else:
                    print(f"   {OK} #{s['idx']} traduit par {r['model']} en {dt:.1f} s")
                rows.append({"doc": f"{lang} {kind}", "original": r["original"], "fr": r["text"] if r["translated"]
                             else f"(échec : {r['error']})", "model": r.get("model", ""), "s": round(dt, 1)})
            again = segments(bid)
            n_tr = sum(x["translated"] for x in again)
            shown_orig = all(x["original"] and x["original"] != x["text"] for x in again if x["translated"])
            print(f"   {OK if shown_orig else KO} original affichable : {n_tr}/{len(again)} segments traduits, "
                  "texte d'origine conservé à côté de chaque traduction")
            bad += not shown_orig
    print("\n| Document | Texte d'origine | Traduction française | Modèle | s |\n|---|---|---|---|---|")
    for r in rows:
        print(f"| {r['doc']} | {r['original']} | {r['fr']} | {r['model']} | {r['s']} |")
    return bad


# ---------- 6d : benchmark ----------
PASSAGES = [
    ("zh", "这家工厂去年生产了12.8万辆电动汽车，比前年增长了百分之四十。工人们说，新的生产线让工作变得更加安全。"),
    ("zh", "他推开窗户，看见远处的山上还覆盖着白雪。母亲在厨房里煮着热腾腾的饺子，屋子里充满了香味。"),
    ("ja", "駅前の小さな喫茶店は、五十年前から同じ場所で営業している。店主は毎朝自分でコーヒー豆を焙煎する。"),
    ("ko", "그는 오랜 고민 끝에 회사를 그만두고 고향으로 돌아가 작은 빵집을 열기로 결심했다."),
    ("en", "The committee postponed its decision until the spring, arguing that the budget figures "
           "were incomplete and that several key witnesses had not yet been heard."),
]


def _bench_one(label: str, client, model: str) -> list[dict]:
    from app import translate as tr
    out = []
    for lang, text in PASSAGES:
        t0 = time.time()
        try:
            res = tr.clean_output(client.chat(model, [
                {"role": "system", "content": tr.SYSTEM},
                {"role": "user", "content": f"Langue source : {tr.LANG_NAMES[lang]}.\n\nTexte à traduire :\n{text}"}],
                temperature=0.1, timeout=600))
        except Exception as e:  # noqa: BLE001 — on rapporte toute erreur telle quelle
            res = f"ERREUR : {e}"
        out.append({"engine": label, "lang": lang, "s": round(time.time() - t0, 1), "out": res, "src": text})
        print(f"   {label} [{lang}] {out[-1]['s']} s")
    return out


def cmd_bench(args: list[str]) -> int:
    from app.llm import Client
    ollama = ENV.get("LLM_BASE_URL", "http://localhost:11434/v1")
    results = _bench_one("ollama:qwen2.5:7b", Client(ollama, "ollama"), "qwen2.5:7b")
    omni = os.getenv("OMNI_BASE_URL", "")
    model = os.getenv("OMNI_MODEL", "")
    if "--omni-model" in args:
        model = args[args.index("--omni-model") + 1]
    if omni and model:
        results += _bench_one(f"omniroute:{model}", Client(omni, os.getenv("OMNI_KEY", "")), model)
    else:
        print(f"{KO} OmniRoute non comparé : OMNI_BASE_URL={omni or '(vide)'} OMNI_MODEL={model or '(vide)'}")
    print("\n| # | Langue | Source | Moteur | Temps (s) | Traduction |\n|---|---|---|---|---|---|")
    for i, r in enumerate(results):
        print(f"| {i % len(PASSAGES) + 1} | {r['lang']} | {r['src'][:60]}… | {r['engine']} | {r['s']} | {r['out']} |")
    for eng in sorted({r["engine"] for r in results}):
        ts = [r["s"] for r in results if r["engine"] == eng and not r["out"].startswith("ERREUR")]
        errs = sum(r["engine"] == eng and r["out"].startswith("ERREUR") for r in results)
        print(f"{eng} : {len(ts)} réussis, {errs} erreurs, moyenne {sum(ts) / max(1, len(ts)):.1f} s/passage")
    return 0


# ---------- mesure : taille des groupes de traduction ----------
def cmd_lot(args: list[str]) -> int:
    """Chronomètre la traduction des MÊMES passages d'un livre avec des groupes de 1, 3 et 8 (rien n'est
    enregistré). À lancer préparation arrêtée, pour ne pas partager le processeur."""
    from app import translate as tr
    from app.db import db
    n = int(args[1]) if len(args) > 1 else 24
    with db() as c:
        if args and args[0] != "auto":
            book = c.execute("SELECT * FROM books WHERE id=?", (args[0],)).fetchone()
        else:
            book = c.execute("SELECT * FROM books WHERE translate=1 AND lang!='fr' AND status='ready' "
                             "ORDER BY created_at DESC").fetchone()
        if not book:
            print("Aucun livre à traduire trouvé (donne son identifiant)."); return 2
        segs = c.execute("SELECT idx, text, lang FROM segments WHERE book_id=? AND idx>=? AND lang NOT IN ('fr','und') "
                         "ORDER BY idx LIMIT ?", (book["id"], book["progress_idx"], n)).fetchall()
    print(f"Livre « {book['title']} » ({book['id']}), {len(segs)} passages à partir du n° {segs[0]['idx'] + 1}, "
          f"{sum(len(x['text']) for x in segs)} caractères ; modèle : {tr.settings.load()['roles']['translate']}")
    old = tr.BATCH_TIMEOUT
    tr.BATCH_TIMEOUT = 600                       # pour mesurer le vrai temps (l'app coupe à 90 s)
    rows = []
    try:
        for size in (1, 3, 8):
            t0, calls, slow, fails, worst = time.time(), 0, 0, 0, 0.0
            for i in range(0, len(segs), size):
                part = segs[i:i + size]
                t1 = time.time()
                try:
                    if size == 1:
                        tr.translate(part[0]["text"], part[0]["lang"])
                    else:
                        tr.translate_batch([x["text"] for x in part], part[0]["lang"])
                except Exception as e:  # noqa: BLE001 — on compte et on continue
                    fails += 1; print(f"   groupe de {size} n° {i // size + 1} : échec ({str(e)[:80]})")
                dt = time.time() - t1; calls += 1; worst = max(worst, dt); slow += dt > 90
                print(f"   groupe de {size}, appel {calls} : {dt:.1f} s", flush=True)
            total = time.time() - t0
            rows.append((size, calls, total, total / len(segs), worst, slow, fails))
    finally:
        tr.BATCH_TIMEOUT = old
    print("\n| Groupe | Appels | Durée totale | s / passage | Appel le plus long | > 90 s | Échecs |\n|---|---|---|---|---|---|---|")
    for size, calls, total, per, worst, slow, fails in rows:
        print(f"| {size} | {calls} | {total:.0f} s | {per:.1f} | {worst:.0f} s | {slow} | {fails} |")
    best = min(rows, key=lambda r: (r[6], r[5], r[3]))
    print(f"\nLe plus rapide sans échec ni dépassement : groupes de {best[0]} → mets TRANSLATE_BATCH={best[0]} dans .env")
    return 0


# ---------- 6g : persistance ----------
def _state() -> dict:
    books = api("GET", "/api/books").json()
    st = {"settings": api("GET", "/api/settings").json(), "books": {}}
    for b in books:
        segs = segments(b["id"])
        st["books"][b["id"]] = {"title": b["title"], "status": b["status"], "lang": b["lang"],
                                "n_segments": b["n_segments"], "indexed": b["indexed"],
                                "translated": sum(s["translated"] for s in segs)}
    return st


def cmd_snapshot(path: str) -> int:
    st = _state()
    Path(path).write_text(json.dumps(st, ensure_ascii=False, indent=1))
    n_tr = sum(b["translated"] for b in st["books"].values())
    print(f"{OK} état enregistré : {len(st['books'])} livres, {n_tr} segments traduits, réglages "
          f"{json.dumps(st['settings']['roles'], ensure_ascii=False)}")
    return 0


def cmd_compare(path: str) -> int:
    before = json.loads(Path(path).read_text())
    after = _state()
    diffs = []
    for bid, b in before["books"].items():
        a = after["books"].get(bid)
        if a != b:
            diffs.append((bid, b, a))
    same_settings = before["settings"] == after["settings"]
    n_tr = sum(b["translated"] for b in after["books"].values())
    if not diffs and same_settings:
        print(f"{OK} après redémarrage : {len(after['books'])} livres identiques, {n_tr} traductions, réglages identiques")
        return 0
    for d in diffs:
        print(f"{KO} livre {d[0]} : avant={d[1]} après={d[2]}")
    if not same_settings:
        print(f"{KO} réglages différents après redémarrage")
    return 1


def main() -> int:
    if not TOKEN:
        print("API_TOKEN introuvable (.env)")
        return 2
    cmd, rest = (sys.argv[1], sys.argv[2:]) if len(sys.argv) > 1 else ("", [])
    if cmd == "piper":
        return cmd_piper()
    if cmd == "fr":
        return cmd_fr()
    if cmd == "cjk":
        return cmd_cjk()
    if cmd == "bench":
        return cmd_bench(rest)
    if cmd == "lot":
        return cmd_lot(rest)
    if cmd == "snapshot":
        return cmd_snapshot(rest[0])
    if cmd == "compare":
        return cmd_compare(rest[0])
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())

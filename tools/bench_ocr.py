"""Mesure l'OCR sur des scans difficiles fabriqués à partir d'un texte connu (page tournée, penchée, bruitée,
à l'envers, chinois). Compare : OCR sans préparation, OCR de l'app (remise à l'endroit + redressement), et
OCRmyPDF s'il est installé. Usage : venv/bin/python tools/bench_ocr.py"""
import io, sys, time, subprocess, difflib, random, re, os, shutil, tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent)); os.environ.setdefault("API_TOKEN", "x" * 40)
import pymupdf as fitz
from PIL import Image, ImageFilter
from app import extract
D = tempfile.mkdtemp(prefix="bench_ocr_")
OCRMYPDF = shutil.which("ocrmypdf")
FR = ("Le vent soufflait fort sur la lande ce soir-là, et les arbres pliaient sous les rafales. Marie referma la porte "
      "de la ferme et alluma une bougie près de la fenêtre. Au loin, elle aperçut une lumière qui avançait lentement "
      "sur le chemin creux. C'était son frère, revenu de la ville après trois longues années d'absence, fatigué mais souriant. "
      "Ils parlèrent jusqu'à l'aube des récoltes, des voisins et de leur mère, qui les attendait depuis si longtemps.")
ZH = "这座古老的城市有两千多年的历史。每年春天，成千上万的游客来这里欣赏盛开的樱花。河边有许多小餐馆和茶馆，晚上街道上灯火通明，非常热闹。"
def page_img(text, font, angle=0.0, noise=False, flip=False):
    d = fitz.open(); p = d.new_page(); p.insert_textbox(fitz.Rect(60, 60, 535, 780), text, fontsize=13 if font == "helv" else 16, fontname=font)
    img = Image.open(io.BytesIO(p.get_pixmap(dpi=200).tobytes("png"))).convert("L")
    if angle: img = img.rotate(angle, resample=Image.BICUBIC, expand=True, fillcolor=255)
    if flip: img = img.rotate(180)
    if noise:
        random.seed(1); px = img.load(); w, h = img.size
        for y in range(0, h):
            for x in range(0, w, 1):
                v = px[x, y]; px[x, y] = max(0, min(255, int(v * 0.82 + 30 + random.randint(-28, 28))))
        img = img.filter(ImageFilter.GaussianBlur(0.7))
    return img
def scan_pdf(img, path):
    buf = io.BytesIO(); img.save(buf, "PNG"); d = fitz.open(); p = d.new_page(width=img.width * 72 / 200, height=img.height * 72 / 200)
    p.insert_image(p.rect, stream=buf.getvalue()); d.save(path)
def norm(t): return re.sub(r"\s+", "" if any("一" <= c <= "鿿" for c in t) else " ", t).strip().lower()
def score(ref, got): return difflib.SequenceMatcher(None, norm(ref), norm(got)).ratio()
cases = [("tourné de 90°", FR, "helv", dict(angle=90), "fra+eng", "auto"), ("penchée 3,5°", FR, "helv", dict(angle=3.5), "fra+eng", "auto"),
         ("penchée + fond bruité + flou", FR, "helv", dict(angle=2.5, noise=True), "fra+eng", "auto"),
         ("à l'envers (180°)", FR, "helv", dict(flip=True), "fra+eng", "auto"),
         ("chinois penché 3°", ZH, "china-s", dict(angle=3.0), "chi_sim+eng", "auto"),
         ("chinois penché + bruité", ZH, "china-s", dict(angle=2.0, noise=True), "chi_sim+eng", "auto")]
print("| Scan | App avant | App + remise à l'endroit + redressement | OCRmyPDF puis app |\n|---|---|---|---|")
for n, (name, text, font, kw, langs, applang) in enumerate(cases):
    src = f"{D}/cas{n}.pdf"; scan_pdf(page_img(text, font, **kw), src)
    extract.PREPARE_SCANS = False
    t0 = time.time(); got_a = " ".join(s for _, segs in extract.iter_pages(__import__("pathlib").Path(src), applang) for s in segs); ta = time.time() - t0
    extract.PREPARE_SCANS = True
    t0 = time.time(); got_c = " ".join(s for _, segs in extract.iter_pages(__import__("pathlib").Path(src), applang) for s in segs); tc = time.time() - t0
    out = src.replace(".pdf", "-ocrmypdf.pdf"); t0 = time.time()
    if not OCRMYPDF:
        print(f"| {name} | {score(text, got_a):.0%} en {ta:.1f} s | **{score(text, got_c):.0%}** en {tc:.1f} s | (non installé) |"); continue
    r = subprocess.run([OCRMYPDF, "-q", "--deskew", "--rotate-pages", "--clean", "-l", langs, "--output-type", "pdf", src, out], capture_output=True, text=True)
    tb = time.time() - t0
    got_b = " ".join(s for _, segs in extract.iter_pages(__import__("pathlib").Path(out), applang) for s in segs) if r.returncode == 0 else "ÉCHEC " + r.stderr[-200:]
    print(f"| {name} | {score(text, got_a):.0%} en {ta:.1f} s | **{score(text, got_c):.0%}** en {tc:.1f} s | {score(text, got_b):.0%} en {tb:.1f} s |")
    if "envers" in name or "bruité" in name:
        print(f"|   ↳ extrait app : | {got_a[:70]!r} | {got_b[:70]!r} |")

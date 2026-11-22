"""Выпрямление перспективы фото страницы: Apple Vision находит четырёхугольник листа
(VNDetectDocumentSegmentationRequest), Core Image (CIPerspectiveCorrection) натягивает его
на прямоугольник; по желанию CIDocumentEnhancer выравнивает освещение (тени, серый фон).

Зачем: dots.mocr на фото теряет 13–15 п. (2605.07492, 2603.28130). Фото с телефона под углом —
трапеция (keystone): строки текста веером, верхние наклонены в одну сторону, нижние — в другую.
Гомография по 4 углам листа это убирает; изгиб страницы (книжный корешок) — нет.

Что известно из статей (перепроверка 15.10.2026): «ошибка втрое ниже» (2609.15503, 2610.15445) —
это CER классического Tesseract на изогнутых и смятых страницах DocUNet; для VLM и для плоских
фото под углом данных нет, гомография по 4 углам нигде не измерена; «наклон 2–6° стоит 14 %»
(2606.26041) — потеря точности ответов в VQA-пилоте, не OCR. Единственное свидетельство для
dots.mocr — наш A/B (замер автора 15.10.2026). Тон и шум (CIDocumentEnhancer,
параметр enhance) не трогаем: по статьям (2610.11818, т.7) такие фильтры перед VLM чаще вредят.

Когда НЕ трогаем картинку: Vision не уверен (conf < MIN_CONF; замер 15.10.2026 на 21 странице:
conf = 0 на 11 страницах — сканы и картинки без рамки листа, четырёхугольник равен кадру),
четырёхугольник почти совпадает с кадром и рёбра почти горизонтальны (skew < MIN_SKEW_DEG), или
«лист» слишком мал (< MIN_AREA кадра — это не страница, а объект на столе). Исключение, которое
пороги не отсекают: скан-таблица (сестринский лист, conf 0,93, наклон 4,3°) выправлялся и стал
втрое дороже; на длинных числах выпрямление тоже может ошибиться (ноль в номере счёта на
синтетическом фото) — по замерам автора 15.10.2026.
~0,1–0,5 с на страницу. Работает только с pyobjc (Vision, Quartz).
"""
from __future__ import annotations
import math
import os
import tempfile
from PIL import Image

MIN_CONF = 0.5
MIN_SKEW_DEG = 1.5     # ниже — лист и так почти прямой, перерисовывать незачем (порог выбран без отдельного замера; на наших фото наклон 2,6–6,4°)
MIN_INSET = 0.04       # хотя бы один угол отстоит от края кадра на столько — есть что обрезать
MIN_AREA = 0.25        # четырёхугольник меньше четверти кадра — не страница
THUMB = 2600           # детектору хватает уменьшенной копии; коррекция делается на полной


def _cg_from_path(path: str):
    import Quartz
    from Foundation import NSURL
    src = Quartz.CGImageSourceCreateWithURL(NSURL.fileURLWithPath_(str(path)), None)
    return Quartz.CGImageSourceCreateImageAtIndex(src, 0, None)


def _save_cg(cg, path: str):
    import Quartz
    from Foundation import NSURL
    dest = Quartz.CGImageDestinationCreateWithURL(NSURL.fileURLWithPath_(str(path)), "public.png", 1, None)
    Quartz.CGImageDestinationAddImage(dest, cg, None)
    Quartz.CGImageDestinationFinalize(dest)


def _with_tmp_png(img: Image.Image, max_side: int | None):
    im = img.convert("RGB")
    if max_side:
        im = im.copy()
        im.thumbnail((max_side, max_side))
    t = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
    im.save(t.name)
    t.close()
    return t.name


def detect(img: Image.Image) -> dict | None:
    """Четырёхугольник листа в нормированных координатах Vision (начало — левый нижний угол):
    {"conf", "tl", "tr", "bl", "br", "area", "skew_deg", "inset"} или None, если pyobjc нет
    или Vision ничего не нашёл."""
    try:
        import Vision
    except Exception:  # noqa: BLE001
        return None
    p = _with_tmp_png(img, THUMB)
    try:
        cg = _cg_from_path(p)
        req = Vision.VNDetectDocumentSegmentationRequest.alloc().init()
        h = Vision.VNImageRequestHandler.alloc().initWithCGImage_options_(cg, None)
        h.performRequests_error_([req], None)
        res = list(req.results() or [])
    finally:
        try:
            os.unlink(p)
        except OSError:
            pass
    if not res:
        return None
    o = max(res, key=lambda r: float(r.confidence()))
    W, H = img.size
    q = {k: (float(getattr(o, m)().x), float(getattr(o, m)().y))
         for k, m in (("tl", "topLeft"), ("tr", "topRight"), ("bl", "bottomLeft"), ("br", "bottomRight"))}

    def ang(a, b):
        dx = (b[0] - a[0]) * W
        dy = (b[1] - a[1]) * H
        return math.degrees(math.atan2(dy, dx))
    top, bottom = ang(q["tl"], q["tr"]), ang(q["bl"], q["br"])
    left = ang(q["bl"], q["tl"]) - 90.0
    right = ang(q["br"], q["tr"]) - 90.0
    skew = max(abs(top), abs(bottom), abs(left), abs(right))
    # площадь по формуле шнурков (в нормированных координатах = доля кадра)
    pts = [q["tl"], q["tr"], q["br"], q["bl"]]
    area = abs(sum(pts[i][0] * pts[(i + 1) % 4][1] - pts[(i + 1) % 4][0] * pts[i][1] for i in range(4))) / 2
    inset = max(q["tl"][0], 1 - q["tr"][0], q["bl"][0], 1 - q["br"][0],
                1 - q["tl"][1], 1 - q["tr"][1], q["bl"][1], q["br"][1])
    q.update(conf=round(float(o.confidence()), 3), area=round(area, 3), skew_deg=round(skew, 2), inset=round(inset, 3))
    return q


def should_fix(q: dict | None) -> bool:
    if not q or q["conf"] < MIN_CONF or q["area"] < MIN_AREA:
        return False
    return q["skew_deg"] >= MIN_SKEW_DEG or q["inset"] >= MIN_INSET


def correct(img: Image.Image, q: dict, enhance: float = 0.0) -> Image.Image:
    """Натянуть четырёхугольник на прямоугольник; enhance > 0 — ещё и CIDocumentEnhancer
    (0..10, 1.0 — как в «Сканировании документов» Заметок)."""
    import Quartz
    p = _with_tmp_png(img, None)
    out_p = p[:-4] + "_fix.png"
    try:
        cg = _cg_from_path(p)
        W, H = Quartz.CGImageGetWidth(cg), Quartz.CGImageGetHeight(cg)
        ci = Quartz.CIImage.imageWithCGImage_(cg)
        f = Quartz.CIFilter.filterWithName_("CIPerspectiveCorrection")
        f.setValue_forKey_(ci, "inputImage")
        for key, pt in (("inputTopLeft", q["tl"]), ("inputTopRight", q["tr"]),
                        ("inputBottomLeft", q["bl"]), ("inputBottomRight", q["br"])):
            f.setValue_forKey_(Quartz.CIVector.vectorWithX_Y_(pt[0] * W, pt[1] * H), key)
        out = f.outputImage()
        if enhance:
            e = Quartz.CIFilter.filterWithName_("CIDocumentEnhancer")
            e.setValue_forKey_(out, "inputImage")
            e.setValue_forKey_(float(enhance), "inputAmount")
            out = e.outputImage()
        ctx = Quartz.CIContext.contextWithOptions_(None)
        cg2 = ctx.createCGImage_fromRect_(out, out.extent())
        _save_cg(cg2, out_p)
        fixed = Image.open(out_p).convert("RGB")
        fixed.load()
        return fixed
    finally:
        for x in (p, out_p):
            try:
                os.unlink(x)
            except OSError:
                pass


def fix(img: Image.Image, enhance: float = 0.0) -> tuple[Image.Image, dict | None]:
    """(картинка, что нашёл детектор); картинка та же, если править нечего."""
    q = detect(img)
    if should_fix(q):
        return correct(img, q, enhance), q
    return img, q


if __name__ == "__main__":
    import sys
    for f in sys.argv[1:]:
        q = detect(Image.open(f))
        print(f"{os.path.basename(f):40s} fix={should_fix(q)} {q}")

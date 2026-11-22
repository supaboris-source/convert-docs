"""Определение ориентации страницы через Apple Vision (pyobjc), ~0,3 с на страницу.

Идея: VNRecognizeTextRequest читает текст при любом повороте, но координаты строк
отдаёт в системе исходной картинки (начало — левый нижний угол) и в порядке чтения.
Поэтому:
  * строки «широкие» (w > 1.5h)  → текст горизонтален: первые строки сверху (y высокий) — страница прямая,
                                    снизу (y низкий) — вверх ногами;
  * строки «узкие» (w < h)       → текст вертикален: первые строки слева (x низкий) — верх страницы слева,
                                    справа (x высокий) — верх справа.
Возвращает угол, на который надо ПОВЕРНУТЬ картинку по часовой стрелке (0, 90, 180, 270),
и уверенность (0..1). Для страниц без текста (< MIN_LINES строк) — 0 и уверенность 0.
"""
from __future__ import annotations
import sys
from pathlib import Path
from PIL import Image

MIN_LINES = 6
THUMB = 1400

def _observations(png_path: str, langs=("ru-RU", "en-US")):
    import Vision, Quartz
    from Foundation import NSURL
    url = NSURL.fileURLWithPath_(str(png_path))
    src = Quartz.CGImageSourceCreateWithURL(url, None)
    img = Quartz.CGImageSourceCreateImageAtIndex(src, 0, None)
    req = Vision.VNRecognizeTextRequest.alloc().init()
    req.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
    req.setUsesLanguageCorrection_(False)
    req.setRecognitionLanguages_(list(langs))
    h = Vision.VNImageRequestHandler.alloc().initWithCGImage_options_(img, None)
    h.performRequests_error_([req], None)
    out = []
    for o in (req.results() or []):
        c = o.topCandidates_(1)
        if not c:
            continue
        bb = o.boundingBox()
        out.append((bb.origin.x, bb.origin.y, bb.size.width, bb.size.height,
                    float(c[0].confidence()), str(c[0].string())))
    return out

def text_rect_count(img: Image.Image, tmpdir: str = "/tmp") -> int:
    """Сколько областей текста находит детектор Vision (VNDetectTextRectanglesRequest).
    Это не распознавание, а поиск «похожих на текст» участков (слова, строки, рукопись), 0,1 с.
    На снимках помещения 0–4 области, на странице с рукописью 15–20, на печатной 30+ (замер
    17.10.2026). Нужен правилу «снимок без текста» в dots_engine. Бросает исключение без pyobjc."""
    import tempfile, os
    import Vision, Quartz
    from Foundation import NSURL
    im = img.convert("RGB").copy()
    im.thumbnail((2200, 2200))
    with tempfile.NamedTemporaryFile(suffix=".png", dir=tmpdir, delete=False) as t:
        im.save(t.name); p = t.name
    try:
        src = Quartz.CGImageSourceCreateWithURL(NSURL.fileURLWithPath_(p), None)
        cg = Quartz.CGImageSourceCreateImageAtIndex(src, 0, None)
        req = Vision.VNDetectTextRectanglesRequest.alloc().init()
        req.setReportCharacterBoxes_(False)
        h = Vision.VNImageRequestHandler.alloc().initWithCGImage_options_(cg, None)
        ok = h.performRequests_error_([req], None)
        if not ok or not ok[0]:
            raise RuntimeError("VNDetectTextRectanglesRequest не выполнился")
        return len(req.results() or [])
    finally:
        try: os.unlink(p)
        except OSError: pass

def detect(img: Image.Image, tmpdir: str = "/tmp") -> tuple[int, float, dict]:
    """(поворот по часовой, уверенность, детали)."""
    import tempfile, os
    im = img.convert("RGB").copy()
    im.thumbnail((THUMB, THUMB))
    with tempfile.NamedTemporaryFile(suffix=".png", dir=tmpdir, delete=False) as t:
        im.save(t.name); p = t.name
    try:
        obs = _observations(p)
    finally:
        try: os.unlink(p)
        except OSError: pass
    n = len(obs)
    if n < MIN_LINES:
        return 0, 0.0, {"lines": n, "reason": "мало строк"}
    wide = sum(1 for x, y, w, h, c, s in obs if w > h * 1.5) / n
    k = max(3, min(8, n // 4))
    head = obs[:k]
    ys = sum(y + h / 2 for x, y, w, h, c, s in head) / k
    xs = sum(x + w / 2 for x, y, w, h, c, s in head) / k
    conf = sum(c for *_, c, s in obs) / n
    if wide >= 0.5:
        rot = 0 if ys >= 0.5 else 180
        sure = abs(ys - 0.5) * 2 * wide
    else:
        # верх страницы слева → крутить по часовой 90; верх справа → против часовой (270)
        rot = 90 if xs < 0.5 else 270
        sure = abs(xs - 0.5) * 2 * (1 - wide)
    return rot, round(min(1.0, sure), 2), {"lines": n, "wide": round(wide, 2), "head_y": round(ys, 2), "head_x": round(xs, 2), "conf": round(conf, 2)}

def fix(img: Image.Image, rot: int) -> Image.Image:
    """Повернуть по часовой на rot градусов."""
    return img if rot == 0 else img.rotate(-rot, expand=True)

if __name__ == "__main__":
    for f in sys.argv[1:]:
        rot, sure, d = detect(Image.open(f))
        print(f"{Path(f).name:40s} rot_cw={rot:3d} sure={sure:.2f} {d}")

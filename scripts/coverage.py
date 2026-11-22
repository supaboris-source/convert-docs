"""Проверка полноты выхода OCR по строкам Apple Vision и «дочитывание» пропущенных областей.

Беда, которую это лечит: на фото под углом dots.mocr молча пропускает куски текста
(«дыры» на 9 страницах из 58 в договоре реального дела), а петля/обрыв оставляет страницу
без конца. Apple Vision (VNRecognizeTextRequest) не выдумывает строк по построению:
если он видит строку, которой нет в выходе VLM, этот кусок стоит перечитать.

Как отличить пропуск печатного текста от каши, которую Vision выдаёт на рукописи и
печатях: орфография. Встроенный NSSpellChecker (язык ru) знает русские слова;
строка «правдоподобна», если ≥ CREDIBLE_FRAC её слов (≥ 4 букв) — настоящие слова.
Замер 15.10.2026 на 16 страницах: чистые сканы — правдоподобных строк 98–100 %,
непокрытых правдоподобных 0 (ложные тревоги 0–3 на странице — там, где Vision сам
ошибся в буквах); фото договора под углом — 3–16 непокрытых правдоподобных строк,
и это реальные пропуски; рукопись — непокрытые строки в основном неправдоподобны.

Что делает `bands()`: непокрытые правдоподобные строки группируются по вертикали в
полосы (с запасом в строку сверху и снизу, на всю ширину страницы) — их потом
перечитывает движок промптом «просто текст», а `novel()` решает, добавил ли
дочитанный кусок что-то новое, или это Vision ошибся (тогда кусок выбрасывается).
"""
from __future__ import annotations
import difflib
import os
import re
import tempfile
from PIL import Image

try:
    import orientation  # соседний модуль
except Exception:  # noqa: BLE001
    orientation = None

CREDIBLE_FRAC = 0.5    # доля настоящих слов в строке, чтобы считать её правдоподобной
MIN_WORDS = 2          # слов ≥ 4 букв в строке, меньше — не оцениваем
MIN_CONF = 0.3         # уверенность Vision (0,3 / 0,5 / 1,0)
MATCH_RATIO = 0.8      # нечёткое совпадение строки с текстом OCR
WORD_HIT = 0.7         # или доля слов строки, найденных в тексте
BAND_GAP = 2.0         # строки ближе, чем столько высот строки, — одна полоса
BAND_MARGIN = 1.0      # запас полосы сверху и снизу (в высотах строки)
MAX_BANDS = 4          # больше — страница не «с дырами», а прочитана заново целиком
THUMB = 2200

_spell = None


def _checker():
    global _spell
    if _spell is None:
        try:
            import AppKit
            _spell = (AppKit.NSSpellChecker.sharedSpellChecker(), AppKit.NSNotFound)
        except Exception:  # noqa: BLE001
            _spell = False
    return _spell


def word_ok(w: str) -> bool:
    sp = _checker()
    if not sp:
        return True
    sc, nf = sp
    r = sc.checkSpellingOfString_startingAt_language_wrap_inSpellDocumentWithTag_wordCount_(w, 0, "ru", False, 0, None)
    return r[0].location == nf


def credibility(line: str) -> float | None:
    """Доля настоящих русских слов среди слов ≥ 4 букв; None — оценивать нечего."""
    ws = re.findall(r"[А-Яа-яЁё]{4,}", line)
    if len(ws) < MIN_WORDS:
        return None
    good = sum(1 for w in ws if word_ok(w.lower()) or word_ok(w))
    return good / len(ws)


def _norm(s: str) -> str:
    s = s.lower().replace("ё", "е")
    s = re.sub(r"<[^>]+>", " ", s)
    s = re.sub(r"[^a-zа-я0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def supported(line: str, ref: str) -> bool:
    """Есть ли строка Vision в (нормализованном) тексте OCR."""
    t = _norm(line)
    if len(t) < 5 or t in ref:
        return True
    L = len(t)
    step = max(1, L // 5)
    best = 0.0
    for i in range(0, max(1, len(ref) - L + 1), step):
        r = difflib.SequenceMatcher(None, t, ref[i:i + L + 3]).ratio()
        if r > best:
            best = r
        if best >= MATCH_RATIO:
            return True
    ws = [w for w in t.split() if len(w) >= 4]
    return bool(ws) and sum(1 for w in ws if w in ref) / len(ws) >= WORD_HIT


def vision_lines(img: Image.Image) -> list[tuple]:
    """[(x, y, w, h, conf, text)] в нормированных координатах Vision (начало — левый нижний угол)."""
    if orientation is None:
        return []
    im = img.convert("RGB").copy()
    im.thumbnail((THUMB, THUMB))
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as t:
        im.save(t.name)
        p = t.name
    try:
        return orientation._observations(p)
    finally:
        try:
            os.unlink(p)
        except OSError:
            pass


def check(img: Image.Image, ocr_text: str, obs: list[tuple] | None = None) -> dict:
    """Сводка полноты: сколько строк видит Vision, сколько из них правдоподобны и не
    покрыты текстом OCR, и полосы (в долях высоты, от верха) для дочитывания."""
    obs = vision_lines(img) if obs is None else obs
    ref = _norm(ocr_text)
    rows = []
    for x, y, w, h, c, s in obs:
        cr = credibility(s)
        if cr is None or c < MIN_CONF:
            continue
        rows.append((x, y, w, h, c, s, cr, supported(s, ref)))
    cred = [r for r in rows if r[6] >= CREDIBLE_FRAC]
    missing = [r for r in cred if not r[7]]
    out = {"vision_text": "\n".join(o[5] for o in obs),
           "vision_lines": len(obs), "scored": len(rows), "credible": len(cred),
           "non_credible": len(rows) - len(cred), "missing": len(missing),
           "missing_lines": [r[5][:80] for r in missing[:12]], "bands": []}
    if missing:
        out["bands"] = bands(missing)
        # текст Vision внутри каждой полосы — для подтверждения дочитанного куска
        out["band_texts"] = []
        for top, bot in out["bands"]:
            inside = [r[5] for r in rows if top <= 1 - (r[1] + r[3] / 2) <= bot]
            out["band_texts"].append(" ".join(inside))
    return out


def bands(missing: list[tuple]) -> list[tuple[float, float]]:
    """Полосы (top, bottom) в долях высоты картинки от ВЕРХА, с запасом."""
    hs = sorted(r[3] for r in missing)
    lh = hs[len(hs) // 2] if hs else 0.02
    lh = max(lh, 0.008)
    # Vision: y — низ строки от нижнего края; переводим в «от верха»
    spans = sorted((1 - (r[1] + r[3]), 1 - r[1]) for r in missing)
    merged: list[list[float]] = []
    for top, bot in spans:
        if merged and top - merged[-1][1] <= BAND_GAP * lh:
            merged[-1][1] = max(merged[-1][1], bot)
        else:
            merged.append([top, bot])
    out = [(max(0.0, t - BAND_MARGIN * lh), min(1.0, b + BAND_MARGIN * lh)) for t, b in merged]
    if len(out) > MAX_BANDS:
        # слишком много дыр — одна полоса от первой до последней
        out = [(out[0][0], out[-1][1])]
    return [(round(t, 4), round(b, 4)) for t, b in out]


def crop_band(img: Image.Image, band: tuple[float, float]) -> Image.Image:
    W, H = img.size
    top, bot = band
    return img.crop((0, int(top * H), W, int(bot * H)))


def corroborated(piece: str, vision_text: str, min_frac: float = 0.15) -> bool:
    """Дочитанный кусок принимается, только если ≥ min_frac его слов (≥ 4 букв) есть в
    независимом чтении Vision (полоса + вся страница). Замер 15.10.2026 на 6 дочитанных
    страницах: настоящие пропуски — договор стр. 29 0,16, размытый скан 0,41; выдумка на
    рукописи 0,07 и 0,23, на размытом фото 0,00. Порог 0,15 отсекает худшее, но не всё:
    Vision на фото под углом сам читает плохо, поэтому блок всегда помечен и попадает в
    _ПРОВЕРИТЬ.md."""
    vw = set(_norm(vision_text).split())
    ws = [w for w in _norm(piece).split() if len(w) >= 4]
    if len(ws) < 3:
        return False
    return sum(1 for w in ws if w in vw) / len(ws) >= min_frac


def novel(piece: str, main_text: str) -> bool:
    """Добавляет ли дочитанный кусок новые слова (иначе это была ошибка Vision)."""
    ref = _norm(main_text)
    ws = [w for w in _norm(piece).split() if len(w) >= 4]
    if len(ws) < 3:
        return False
    new = sum(1 for w in ws if w not in ref)
    return new / len(ws) >= 0.5


if __name__ == "__main__":
    import sys
    import json
    img = Image.open(sys.argv[1])
    text = open(sys.argv[2]).read() if len(sys.argv) > 2 else ""
    print(json.dumps(check(img, text), ensure_ascii=False, indent=1))

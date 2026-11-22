"""Дешёвые признаки картинки страницы для postcheck.py: синяя ручка и резкость.

    features(img) -> {"ink": {"blue_px": int}, "sharp": float}

Зачем. Слепая зона OCR — рукописные графы в печатной форме: Apple Vision читает печатную
часть уверенно, второе чтение расхождений не видит, а рукопись VLM выдумывает (реальное
дело, 16.10.2026: 8 страниц материала проверки с синей ручкой остались без единого
флажка). Число синих пикселей отделяет такие страницы от печатных — но только на бумаге без подписей и печатей: на подписанных
договорах, скриншотах и снимках помещения синего тоже много (на реальном деле 120 страниц из 130), поэтому postcheck.py
использует его лишь вместе с другими признаками рукописи.

`blue_px` — число «синих» пикселей (b−r > 35, b−g > 15, b > 90, как в разметке B2), приведённое
к странице 496 × 702 px (A4 при 60 dpi, 348 192 px): доля синих пикселей × 348 192. Поля по 5 %
с каждой стороны не считаются: у фото там синеватые тени сгиба и края листа (на фото договоров
они давали сотни ложных «чернил»). Порог рукописи — `postcheck.BLUE_HAND_PX` (15).
Почему не «уменьшить до 700 px»: в замере B2 «60 dpi» значило 1500 × 3334 px (страницы PDF-фото
1800 × 4000 pt), а не A4; при усреднении до 700 px бледная синяя паста растворяется в белом
(в 20–40 раз меньше пикселей, порог 150 не работает). Доля синих пикселей от разрешения почти
не зависит, пока картинка не меньше ≈ 750 px по ширине, поэтому ужимаем только до 2000 px по
длинной стороне (меньшие не растягиваем) и считаем долю.

`sharp` — дисперсия лапласиана серой картинки (длинная сторона ≤ 1400 px). Сама по себе мало что
говорит (растёт с густотой текста), нужна для сравнения снимков одного листа: из двух фото резче
то, у которого она больше (postcheck.py ставит на чтение глазами только самый резкий снимок).
Порядок «кто резче» на 14 парах реального дела не зависел от размера (700–2000 px) и совпал со вторым
показателем (энергия градиента).

CLI — дозаполнить старые выходы, где этих полей ещё нет:

    ~/ocr-env2/bin/python -I page_features.py <корень _md> [--force]

Для каждого `_ocr.json` по исходнику (поле `src`) рендерятся страницы (render_page.render,
200 dpi, с поворотом из `rotation`), в `page_stats` дописываются `ink`, `sharp` и
`vision_text` (чтение Apple Vision той же картинки, нужно для поиска повторных снимков
одного листа). Что уже есть — не пересчитывается (`--force` — пересчитать всё).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
from PIL import Image

INK_SIDE = 2000           # длинная сторона при подсчёте синего (только ужимаем)
SHARP_SIDE = 1400         # и при подсчёте резкости
A4_60DPI = 496 * 702      # к такой площади приводится число синих пикселей
MARGIN = 0.05             # поля, где у фото тени и края листа
BLUE_DR, BLUE_DG, BLUE_MIN = 35, 15, 90   # синий: b-r > 35, b-g > 15, b > 90 (как в B2/blue.py)


def _norm(img: Image.Image, side: int) -> Image.Image:
    im = img.convert("RGB")
    w, h = im.size
    k = side / max(w, h)
    if k >= 1:
        return im          # меньше нормы — не растягиваем: пикселей не прибавится
    # BOX: усреднение по площади, без «звона» на краях букв
    return im.resize((max(1, round(w * k)), max(1, round(h * k))), Image.BOX)


def blue_px(img: Image.Image) -> int:
    a = np.asarray(_norm(img, INK_SIDE)).astype(np.int32)
    h, w = a.shape[:2]
    my, mx = int(h * MARGIN), int(w * MARGIN)
    a = a[my:h - my, mx:w - mx] if h > 2 * my + 1 and w > 2 * mx + 1 else a
    r, g, b = a[..., 0], a[..., 1], a[..., 2]
    blue = ((b - r) > BLUE_DR) & ((b - g) > BLUE_DG) & (b > BLUE_MIN)
    return int(round(float(blue.mean()) * A4_60DPI))


def sharpness(img: Image.Image) -> float:
    gray = np.asarray(_norm(img, SHARP_SIDE).convert("L")).astype(np.float64)
    if min(gray.shape) < 3:
        return 0.0
    lap = (gray[1:-1, 1:-1] * 4 - gray[:-2, 1:-1] - gray[2:, 1:-1]
           - gray[1:-1, :-2] - gray[1:-1, 2:])
    return round(float(lap.var()), 1)


def features(img: Image.Image) -> dict:
    return {"ink": {"blue_px": blue_px(img)}, "sharp": sharpness(img)}


def _src_path(d: dict, js: Path) -> Path | None:
    """Исходник из `src`; если папку дела переименовали — рядом с `_md/`."""
    src = Path(d.get("src") or "")
    if src.exists():
        return src
    for base in (js.parent, js.parent.parent, js.parent.parent.parent):
        cand = base / src.name
        if cand.exists():
            return cand
    return None


def fill(root: Path, force: bool = False, log=print) -> int:
    """Дозаполнить `_ocr.json` под root. Возвращает число обновлённых страниц."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import render_page
    try:
        import coverage          # vision_lines: чтение Apple Vision, общая функция скилла
    except Exception:  # noqa: BLE001
        coverage = None
    total = 0
    for js in sorted(root.rglob("*_ocr.json")):
        try:
            d = json.loads(js.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        stats = d.get("page_stats") or []
        todo = [ps for ps in stats if force or not ("ink" in ps and "sharp" in ps and "vision_text" in ps)]
        if not todo:
            continue
        src = _src_path(d, js)
        if src is None:
            log(f"  {js.name}: нет исходника ({d.get('src')}), пропуск")
            continue
        n = 0
        for ps in todo:
            try:
                img = render_page.render(src, int(ps.get("page") or 1), int(ps.get("rotation") or 0), 200)
                f = features(img)
                ps["ink"], ps["sharp"] = f["ink"], f["sharp"]
                if coverage is not None and (force or "vision_text" not in ps):
                    ps["vision_text"] = "\n".join(o[5] for o in coverage.vision_lines(img))
                n += 1
            except Exception as e:  # noqa: BLE001
                log(f"  {js.name} стр. {ps.get('page')}: {type(e).__name__}: {e}")
        js.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
        total += n
        log(f"  {js.name}: дозаполнено страниц {n} из {len(stats)}")
    return total


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("root")
    ap.add_argument("--force", action="store_true", help="пересчитать и те страницы, где поля уже есть")
    a = ap.parse_args()
    root = Path(a.root).expanduser()
    if not root.exists():
        print(f"нет пути: {root}", file=sys.stderr)
        return 2
    n = fill(root, a.force)
    print(f"страниц дозаполнено: {n}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

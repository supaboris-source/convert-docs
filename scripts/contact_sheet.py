"""Листы миниатюр страниц: быстро увидеть, где рукопись, печати, пустые и повторные страницы.

    ~/ocr-env2/bin/python -I contact_sheet.py <исходник> <папка> [--pages 1-30] [--per 12] [--ocr-json путь]
    ~/ocr-env2/bin/python -I contact_sheet.py --plan <папка запуска>/plan.json <папка>   # все документы OCR сразу

Каждая миниатюра в ячейке 4 × 3 (по умолчанию 12 на лист), номер страницы — в жёлтом квадрате
в левом верхнем углу. Листы `<папка>/<имя исходника>_лист_N.png`, пути печатаются по одному в строке.
Нужно для дешёвого отбора: модель с просмотром изображений смотрит 5 листов вместо 60 страниц и называет
номера страниц с рукописью — только их потом читают глазами по 200 dpi.

Поворот страниц берётся из `_ocr.json` этого исходника (поле `rotation`), если он нашёлся рядом
(`<исходник>/../_md/**/<имя>_ocr.json`, у которого поле `src` или `orig` совпадает с исходником) или указан
`--ocr-json`; иначе страницы не поворачиваются (с предупреждением).
Режим `--plan`: миниатюры всех распознанных страниц всех OCR-документов запуска подряд (снимки без
текста пропускаются), поворот — из их `_ocr.json` в рабочей папке. Подпись миниатюры «Д<k>·<стр>»,
расшифровка «Д<k>» — в `<папка>/_ЛЕГЕНДА.md`. Так отбор рукописи дёшев и для 50 отдельных фото
(5 листов, а не 50).
`--pages`: «5», «1-30», «1-5,9,12-14». Миниатюры рендерятся с низким dpi, зависящим от размера
страницы, поэтому лист из 12 страниц делается за секунды даже для PDF с огромными страницами.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, str(Path(__file__).resolve().parent))
import render_page  # noqa: E402

COLS, ROWS = 4, 3
CW, CH = 400, 520          # ячейка миниатюры, как в разметке B2 (рукопись на A4 различима)
FONTS = ("/System/Library/Fonts/Helvetica.ttc", "/System/Library/Fonts/Supplemental/Arial.ttf")


def parse_pages(spec: str | None, total: int) -> list[int]:
    if not spec:
        return list(range(1, total + 1))
    out: list[int] = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            out += range(int(a), int(b) + 1)
        else:
            out.append(int(part))
    return [p for p in dict.fromkeys(out) if 1 <= p <= total]


def page_count(src: Path) -> int:
    if src.suffix.lower() == ".pdf":
        import pymupdf
        with pymupdf.open(src) as doc:
            return len(doc)
    if src.suffix.lower() in (".tif", ".tiff"):
        with Image.open(src) as im:
            return getattr(im, "n_frames", 1)
    return 1


def thumb_dpi(src: Path, page: int) -> int:
    """dpi, при котором длинная сторона страницы ≈ 1,6 высоты ячейки (запас для сжатия без рывков)."""
    if src.suffix.lower() != ".pdf":
        return 72
    import pymupdf
    with pymupdf.open(src) as doc:
        r = doc[page - 1].rect
    long_in = max(r.width, r.height) / 72
    return int(max(20, min(100, CH * 1.6 / long_in)))


def _same_file(a: str | None, b: Path) -> bool:
    if not a:
        return False
    try:
        pa = Path(a).expanduser()
        return pa.resolve() == b.resolve() or (pa.exists() and b.exists() and pa.samefile(b))
    except OSError:
        return False


def rotations(src: Path, explicit: str | None) -> dict[int, int]:
    """Поворот страниц из `_ocr.json`. Явный `--ocr-json` берётся как есть; иначе ищется `<имя>_ocr.json` под
    `<исходник>/../_md`, и берётся только тот, у которого `src` или `orig` совпадает с этим исходником (одноимённый
    файл другого документа в соседней папке дал бы чужие повороты). Не нашёлся — поворот 0 и предупреждение."""
    cands: list[Path] = []
    if explicit:
        cands.append(Path(explicit))
    else:
        md = src.parent / "_md"
        if md.exists():
            cands += md.rglob(f"{src.stem}_ocr.json")
    for js in cands:
        try:
            d = json.loads(js.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        if not explicit and not (_same_file(d.get("src"), src) or _same_file(d.get("orig"), src)):
            continue
        return {int(p["page"]): int(p.get("rotation") or 0) for p in d.get("page_stats", [])}
    if cands or not explicit:
        print(f"предупреждение: _ocr.json этого исходника не найден"
              + (f" (одноимённых json — {len(cands)}, но поле src/orig у них другое)" if cands else "")
              + "; страницы не поворачиваются. Укажите --ocr-json", file=sys.stderr)
    return {}


def font(size: int):
    for f in FONTS:
        try:
            return ImageFont.truetype(f, size)
        except Exception:  # noqa: BLE001
            continue
    return ImageFont.load_default()


def make_sheets(src: Path, outdir: Path, pages: list[int], per: int, rots: dict[int, int]) -> list[Path]:
    items = [(src, p, rots.get(p, 0), str(p)) for p in pages]
    return draw_sheets(items, outdir, per, src.stem)


def draw_sheets(items: list[tuple[Path, int, int, str]], outdir: Path, per: int, stem: str) -> list[Path]:
    """items: (исходник, страница, поворот, подпись) — по миниатюре на элемент, 4 × 3 на лист."""
    outdir.mkdir(parents=True, exist_ok=True)
    per = max(1, min(per, COLS * ROWS))
    fnt = font(34)
    paths: list[Path] = []
    for si in range(0, len(items), per):
        chunk = items[si:si + per]
        sheet = Image.new("RGB", (CW * COLS, CH * ROWS), "white")
        d = ImageDraw.Draw(sheet)
        for i, (src, p, rot, label) in enumerate(chunk):
            x, y = (i % COLS) * CW, (i // COLS) * CH
            try:
                im = render_page.render(src, p, rot, thumb_dpi(src, p)).convert("RGB")
                im.thumbnail((CW - 6, CH - 6))
                sheet.paste(im, (x + 3, y + 3))
            except Exception as e:  # noqa: BLE001
                d.text((x + 10, y + 60), f"{label}: {type(e).__name__}", fill="red", font=font(20))
            d.rectangle([x + 3, y + 3, x + 12 + 22 * len(label) + 30, y + 44], fill="yellow")
            d.text((x + 8, y + 6), label, fill="red", font=fnt)
            d.rectangle([x, y, x + CW - 1, y + CH - 1], outline="#bbbbbb")
        out = outdir / f"{stem}_лист_{si // per + 1}.png"
        sheet.save(out)
        paths.append(out)
    return paths


def plan_items(plan: Path) -> tuple[list[tuple[Path, int, int, str]], list[str]]:
    """Все распознанные страницы OCR-документов запуска: поворот из их _ocr.json, без снимков без текста."""
    rows = [r for r in json.loads(plan.read_text(encoding="utf-8")).get("plan", []) if r.get("engine") == "ocr"]
    items: list[tuple[Path, int, int, str]] = []
    legend: list[str] = []
    for k, r in enumerate(rows, 1):
        src, out = Path(r["src"]), Path(r["out"])
        js = out.with_name(out.stem + "_ocr.json")
        try:
            stats = json.loads(js.read_text(encoding="utf-8")).get("page_stats", [])
        except Exception:  # noqa: BLE001
            stats = []
        if stats:
            pages = [(int(ps["page"]), int(ps.get("rotation") or 0)) for ps in stats if ps.get("mode") != "empty"]
        else:
            print(f"предупреждение: нет {js.name} — страницы {r.get('rel')} без поворота", file=sys.stderr)
            pages = [(p, 0) for p in (r.get("only_pages") or range(1, page_count(src) + 1))]
        legend.append(f"- Д{k} — `{r.get('rel', src.name)}`" + (f" (страницы {', '.join(str(p) for p, _ in pages)})"
                                                                 if r.get("only_pages") else ""))
        items += [(src, p, rot, f"Д{k}·{p}") for p, rot in pages]
    return items, legend


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("args", nargs="+", metavar="ПУТЬ", help="<исходник> <папка> или, с --plan, только <папка>")
    ap.add_argument("--plan", default=None, help="plan.json запуска: листы по всем OCR-документам")
    ap.add_argument("--pages", default=None, help="например 1-30 или 1-5,9,12-14")
    ap.add_argument("--per", type=int, default=12, help="миниатюр на лист (не больше 12)")
    ap.add_argument("--ocr-json", default=None, help="_ocr.json исходника: поворот страниц")
    a = ap.parse_args()
    if a.plan:
        outdir = Path(a.args[-1]).expanduser()
        items, legend = plan_items(Path(a.plan).expanduser())
        if not items:
            print("в плане нет распознанных страниц", file=sys.stderr)
            return 2
        for p in draw_sheets(items, outdir, a.per, "запуск"):
            print(p)
        (outdir / "_ЛЕГЕНДА.md").write_text("Подписи миниатюр «Д<k>·<страница>»:\n\n" + "\n".join(legend) + "\n",
                                            encoding="utf-8")
        print(outdir / "_ЛЕГЕНДА.md")
        return 0
    if len(a.args) != 2:
        ap.error("нужны <исходник> и <папка> (или --plan и <папка>)")
    src = Path(a.args[0]).expanduser()
    if not src.exists():
        print(f"нет файла: {src}", file=sys.stderr)
        return 2
    pages = parse_pages(a.pages, page_count(src))
    if not pages:
        print("нет страниц в заданном диапазоне", file=sys.stderr)
        return 2
    for p in make_sheets(src, Path(a.args[1]).expanduser(), pages, a.per, rotations(src, a.ocr_json)):
        print(p)
    return 0


if __name__ == "__main__":
    sys.exit(main())

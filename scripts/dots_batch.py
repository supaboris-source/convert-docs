#!/usr/bin/env python3
"""Пакетный OCR сканов через dots.mocr с ОДНОЙ загрузкой модели.

Исполнитель контракта plan.json от route.py:

    ~/ocr-env2/bin/python dots_batch.py --plan plan.json

Ручной режим (кладёт всё плоско в out_root):

    ~/ocr-env2/bin/python dots_batch.py --out <out_root> файл1.pdf фото2.jpg …

Результат — <папка>/<имя>.md и рядом <имя>_ocr.json со статистикой по
страницам. Строки плана берутся с движком «ocr» (или «marker» — старое имя).

Что делается с каждой страницей (подробно в dots_engine.py): выпрямление
перспективы фото (Apple Vision + Core Image), автоповорот (Vision), разметка
dots.mocr с ранней остановкой петли, запасной промпт, запасные модели при петле
(Xiaomi-OCR-0, HunyuanOCR-1.5 через llama.cpp, GLM-OCR), дочитывание областей,
которые Vision видит, а OCR пропустил, резервное чтение Vision, QR-коды; затем
сверка имён и чисел вторым независимым чтением (crosscheck.py) — непроверенное
попадает в пометку ⚠ и в _ocr.json, откуда postcheck.py собирает список
«что сверить глазами».

Формат <имя>_ocr.json:
  {"src", "orig"?, "out", "pages": N, "seconds", "engine", "page_stats": [ {
      "page", "mode", "warnings": [...], "prompt_tokens", "gen_tokens", "seconds",
      "blocks", "image": [w, h], "rotation", "engine", "crosscheck": {...},
      "dewarp": {"conf", "skew_deg", "area", "inset"} | null,
      "coverage": {"vision_lines", "credible", "non_credible", "missing", "missing_lines", "bands", "added"} | null,
      "qr": ["…"],
      "vision_text": "полный текст Apple Vision этой страницы", "vision_conf": средняя уверенность строк,
      "ink": {"blue_px"}, "sharp": резкость (из page_features.py, если модуль есть),
      "notext": {"lines", "rects", "paper"} (если проверялось правило «снимок без текста») } ]}
Строка плана с `only_pages` (сканы внутри PDF с текстом): распознаются только эти страницы, каждая
встаёт на место своего раздела «## Страница N» в .md быстрой ветки; в _ocr.json тогда
"pages" — число распознанных страниц, "only_pages" — их номера, "doc_pages" — всего страниц.
`orig` — исходник, если в OCR отдана его копия (Word из сканов → PDF рядом с .md): в заголовок
.md и в _ocr.json идёт его имя, а не имя PDF. Страница без текста (mode «empty») в OCR не отдаётся,
флаг --no-skip-empty это отключает.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import re
import sys
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import dots_engine  # noqa: E402
from common import unread_pages  # noqa: E402
try:
    import crosscheck  # noqa: E402
    import orientation  # noqa: E402
except Exception:  # noqa: BLE001
    crosscheck = orientation = None

try:
    import pymupdf as fitz  # noqa: E402
except ImportError:  # старые окружения
    import fitz  # noqa: E402
from PIL import Image, ImageOps  # noqa: E402

# Три подряд упавших файла — отказ среды, а не беда документа (модель не
# грузится, кончилась память). Останавливаемся, остаток в retry-план.
FAIL_IN_ROW = 3
ORIG: dict[str, str] = {}   # src (PDF-копия) → путь исходника из плана (Word из сканов)
ONLY: dict[str, list[int]] = {}   # out → страницы-сканы внутри PDF с текстом (остальное сделала быстрая ветка)
PAGE_HEAD = re.compile(r"(?m)^## Страница (\d+)\s*$")
TRAILER = re.compile(r"(?m)^---\nРаспознано:")
SCAN_NOTE = "> ⚠ Страница без текстового слоя (скан) — текст распознан OCR, сверять с оригиналом"
RASTER = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tif", ".tiff", ".webp"}
OCR_ENGINES = {"ocr", "marker", "dots"}


def jobs_from_plan(plan_path: Path) -> list[tuple[Path, Path]]:
    data = json.loads(plan_path.read_text())
    rows = [p for p in data.get("plan", []) if p.get("engine") in OCR_ENGINES]
    ORIG.update({p["src"]: p["orig"] for p in rows if p.get("orig")})
    ONLY.update({p["out"]: [int(x) for x in p["only_pages"]] for p in rows if p.get("only_pages")})
    return [(Path(p["src"]), Path(p["out"])) for p in rows]


def page_images(src: Path, only: list[int] | None = None):
    """Генератор (номер, PIL-картинка, всего страниц). `only` — только эти страницы PDF (сканы внутри
    PDF с текстом); номера остаются номерами страниц документа."""
    if src.suffix.lower() == ".pdf":
        doc = fitz.open(str(src))
        n = len(doc)
        for num in (only or range(1, n + 1)):
            if not 1 <= num <= n:
                continue
            pix = doc[num - 1].get_pixmap(dpi=dots_engine.RENDER_DPI)
            img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
            yield num, img, n
        doc.close()
    else:
        img = Image.open(src)
        # многостраничный TIFF (сканер «в один файл»): каждая страница — отдельный кадр;
        # у анимированных GIF/WEBP кадры не страницы, их не перебираем
        n = getattr(img, "n_frames", 1) if src.suffix.lower() in (".tif", ".tiff") else 1
        if n <= 1:
            yield 1, ImageOps.exif_transpose(img), 1
            return
        for i in range(n):
            img.seek(i)
            yield i + 1, ImageOps.exif_transpose(img.copy()), n


@contextlib.contextmanager
def cached_vision(img, vision):
    """crosscheck.check читает страницу Apple Vision ещё раз; подставляем чтение, которое движок
    уже сделал по этой же картинке (0,3–1 с на страницу). Другие картинки идут как раньше."""
    if crosscheck is None or vision is None or not hasattr(crosscheck, "vision_read"):
        yield
        return
    orig = crosscheck.vision_read
    crosscheck.vision_read = lambda im: vision if im is img else orig(im)
    try:
        yield
    finally:
        crosscheck.vision_read = orig


def merge_scan_pages(out: Path, shown: str, done: list[tuple[int, str]], trailer: str) -> None:
    """Вставить OCR страниц-сканов на их место в .md быстрой ветки (разделы «## Страница N»).
    Раздела нет — страница дописывается в конец; прежняя концовка «Распознано: …» заменяется новой."""
    md = out.read_text(encoding="utf-8") if out.exists() else f"# {shown}\n"
    t = TRAILER.search(md)
    if t:
        md = md[:t.start()]
    for num, text in done:
        block = f"\n\n{SCAN_NOTE}\n\n{text.strip()}\n\n"
        heads = list(PAGE_HEAD.finditer(md))
        h = next((x for x in heads if int(x.group(1)) == num), None)
        if h is None:
            md = md.rstrip("\n") + f"\n\n## Страница {num}{block}"
            continue
        nxt = next((x for x in heads if x.start() > h.start()), None)
        md = md[:h.end()] + block + (md[nxt.start():] if nxt else "")
    out.write_text(md.rstrip("\n") + "\n\n" + trailer + "\n", encoding="utf-8")


def convert(engine: dots_engine.DotsEngine, src: Path, out: Path, log, do_check: bool = True) -> dict:
    t0 = time.time()
    shown = Path(ORIG.get(str(src), src)).name   # имя в заголовке .md: исходник, а не PDF-копия
    pages: list[dict] = []
    texts: list[str] = []
    nums: list[int] = []
    total = 0
    only = ONLY.get(str(out))
    for num, img, total in page_images(src, only):
        nums.append(num)
        r = engine.ocr_page(img)
        text = dots_engine.annotate(r, num)
        cc = None
        if do_check and crosscheck is not None and r.markdown and r.mode != "empty":
            try:
                fixed = r.image if r.image is not None else (
                    orientation.fix(img, r.rotation) if (orientation and r.rotation) else img)
                with cached_vision(fixed, r.vision):
                    cc = crosscheck.check(fixed, r.markdown)
                n = crosscheck.note(cc)
                if n:
                    text += "\n\n" + n
            except Exception as e:  # noqa: BLE001
                cc = {"error": f"{type(e).__name__}: {e}"[:200]}
        texts.append(text)
        pages.append({"page": num, "mode": r.mode, "warnings": r.warnings,
                      "prompt_tokens": r.prompt_tokens, "gen_tokens": r.gen_tokens,
                      "seconds": round(r.seconds, 1), "blocks": r.blocks,
                      "image": list(r.image_size), "rotation": r.rotation, "engine": r.engine,
                      "crosscheck": cc,
                      "dewarp": ({k: r.dewarp.get(k) for k in ("conf", "skew_deg", "area", "inset")} if r.dewarp else None),
                      "coverage": r.coverage, "qr": [c["payload"] for c in (r.qr or [])]})
        if r.vision is not None:
            pages[-1]["vision_text"] = r.vision[0]
            pages[-1]["vision_conf"] = round(r.vision[1], 2)
        if r.features:
            pages[-1]["ink"], pages[-1]["sharp"] = r.features.get("ink"), r.features.get("sharp")
        if r.notext:
            pages[-1]["notext"] = r.notext
        r.image = r.vision_obs = None
        flag = (" ⚠ " + ",".join(r.warnings)) if r.warnings else ""
        extra = ""
        if cc and not cc.get("error") and (cc.get("names_unconfirmed") or cc.get("numbers_unconfirmed")):
            extra = f"  сверить: имён {len(cc['names_unconfirmed'])}, чисел {len(cc['numbers_unconfirmed'])}"
        if r.coverage and r.coverage.get("added"):
            extra += f"  дочитано областей: {r.coverage['added']}"
        log(f"    [{num}/{total}] {r.seconds:5.1f} с  {r.mode:16s} {r.gen_tokens:5d} ток.{flag}{extra}")
    out.parent.mkdir(parents=True, exist_ok=True)
    dur = time.time() - t0
    if only:
        merge_scan_pages(out, shown, list(zip(nums, texts)),
                         f"---\nРаспознано: OCR стр. {', '.join(map(str, nums))} из {total} (сканы без текстового слоя), "
                         f"{dur:.0f} с, dots.mocr 8-bit MLX; остальные страницы — текстовый слой PDF")
        stats = {"src": str(src), "out": str(out), "pages": len(pages), "only_pages": nums, "doc_pages": total,
                 "seconds": round(dur, 1), "engine": "dots.mocr-8bit", "page_stats": pages}
        failed_pages = unread_pages(pages)
        if failed_pages:
            stats["failed_pages"] = failed_pages
        out.with_name(out.stem + "_ocr.json").write_text(
            json.dumps(stats, ensure_ascii=False, indent=1), encoding="utf-8")
        return stats
    body = [f"# {shown}", ""]
    for num, t in enumerate(texts, 1):
        if total > 1:
            body += [f"## Страница {num}", ""]
        body += [t, ""]
    bad = sum(1 for p in pages if any(not w.split(":")[0] in dots_engine.SILENT for w in p["warnings"]))
    skipped = sum(1 for p in pages if p["mode"] == "empty")
    body += ["---", f"Распознано: {total} стр., {dur:.0f} с, dots.mocr 8-bit MLX"
             + (f", без OCR (снимки без текста): {skipped}" if skipped else "")
             + (f", страниц с пометками: {bad}" if bad else "")]
    out.write_text("\n".join(body), encoding="utf-8")
    stats = {"src": str(src), **({"orig": ORIG[str(src)]} if str(src) in ORIG else {}),
             "out": str(out), "pages": total, "seconds": round(dur, 1),
             "engine": "dots.mocr-8bit", "page_stats": pages}
    failed_pages = unread_pages(pages)
    if failed_pages:
        stats["failed_pages"] = failed_pages
    out.with_name(out.stem + "_ocr.json").write_text(
        json.dumps(stats, ensure_ascii=False, indent=1), encoding="utf-8")
    return stats


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--plan", help="plan.json от route.py")
    ap.add_argument("--out", help="корень вывода для ручного режима")
    ap.add_argument("--max-pixels", type=int, default=dots_engine.MAX_PIXELS)
    ap.add_argument("--timeout", type=int, default=dots_engine.PAGE_TIMEOUT)
    ap.add_argument("--no-rotate", action="store_true", help="не определять ориентацию страниц")
    ap.add_argument("--no-check", action="store_true", help="не сверять имена и числа вторым чтением")
    ap.add_argument("--second", default=None, help="запасные движки через запятую (xiaomi,hunyuan15,glm); пусто — без них")
    ap.add_argument("--dewarp", action="store_true", help="выпрямлять перспективу фото (по умолчанию выключено: см. ДВИЖОК.md)")
    ap.add_argument("--no-reread", action="store_true", help="не дочитывать области, пропущенные OCR")
    ap.add_argument("--no-qr", action="store_true", help="не искать QR-коды")
    ap.add_argument("--no-skip-empty", action="store_true",
                    help="не пропускать снимки без текста (Vision < 3 строк, ≤ 5 областей текста, не бумага, нет QR): читать их dots")
    ap.add_argument("files", nargs="*")
    a = ap.parse_args()

    if a.plan:
        jobs = jobs_from_plan(Path(a.plan))
    elif a.out and a.files:
        root = Path(a.out)
        jobs = [(Path(f), root / Path(f).stem / f"{Path(f).stem}.md") for f in a.files]
    else:
        ap.error("нужен --plan либо --out вместе со списком файлов")

    if not jobs:
        print("dots: нечего распознавать")
        return 0

    log = lambda m: print(m, flush=True)  # noqa: E731
    log(f"dots: {len(jobs)} файл(ов), загружаю модель…")
    second = None if a.second is None else [s for s in a.second.split(",") if s]
    engine = dots_engine.DotsEngine(max_pixels=a.max_pixels, page_timeout=a.timeout, log=log,
                                    auto_rotate=not a.no_rotate, second_opinions=second,
                                    auto_dewarp=a.dewarp, reread=not a.no_reread, qr=not a.no_qr,
                                    skip_empty=not a.no_skip_empty)
    engine.load()

    rc, in_row = 0, 0
    failed: list[tuple[Path, Path]] = []
    partial: list[tuple[Path, list[int]]] = []
    done_pages = 0
    t_start = time.time()
    try:
        for i, (src, out) in enumerate(jobs, 1):
            if not src.is_file():
                log(f"[{i}/{len(jobs)}] НЕТ ФАЙЛА: {src}")
                rc = 1
                failed.append((src, out))
                continue
            log(f"[{i}/{len(jobs)}] {src.name}")
            try:
                st = convert(engine, src, out, log, do_check=not a.no_check)
                done_pages += st["pages"]
                bad_pages = st.get("failed_pages") or []
                if bad_pages and len(bad_pages) >= st["pages"]:
                    rc = 1
                    in_row += 1
                    failed.append((src, out))
                    log(f"[{i}/{len(jobs)}] ОШИБКА: не распознана ни одна страница (таймаут/сбой движка)")
                    if in_row >= FAIL_IN_ROW:
                        log(f"\ndots: {in_row} файла подряд не распознаны — похоже, отказ среды. "
                            "Останавливаюсь, остаток уйдёт в retry-план.")
                        failed.extend(jobs[i:])
                        break
                    continue
                if bad_pages:
                    rc = 1
                    partial.append((src, bad_pages))
                    log(f"    ⚠ не распознаны стр. {', '.join(map(str, bad_pages))} — прочитать глазами или повторить")
                in_row = 0
                el = time.time() - t_start
                log(f"    готово за {st['seconds']:.0f} с; всего {done_pages} стр. за {el/60:.0f} мин "
                    f"({el/max(1, done_pages):.0f} с/стр)")
            except Exception as e:  # noqa: BLE001
                rc = 1
                in_row += 1
                failed.append((src, out))
                log(f"[{i}/{len(jobs)}] ОШИБКА: {type(e).__name__}: {e}")
                traceback.print_exc()
                if in_row >= FAIL_IN_ROW:
                    log(f"\ndots: {in_row} файла подряд упали — похоже, отказ среды. "
                        "Останавливаюсь, остаток уйдёт в retry-план.")
                    failed.extend(jobs[i:])
                    break
    finally:
        engine.unload()

    if failed:
        log(f"\ndots: готово {len(jobs) - len(failed)} из {len(jobs)}. НЕ СДЕЛАНЫ ({len(failed)}):")
        for src, _ in failed:
            log(f"  ✗ {src.name}")
        if a.plan:
            retry = Path(a.plan).with_suffix(".retry.json")
            srcs = {str(s) for s, _ in failed}
            data = json.loads(Path(a.plan).read_text())
            data["plan"] = [p for p in data.get("plan", []) if p.get("src") in srcs]
            retry.write_text(json.dumps(data, ensure_ascii=False, indent=2))
            log(f"\nПовтор: ~/ocr-env2/bin/python {Path(__file__).name} --plan {retry}")
    if partial:
        log(f"\ndots: частично распознаны ({len(partial)}):")
        for src, pg in partial:
            log(f"  ⚠ {src.name}: не распознаны стр. {', '.join(map(str, pg))}")
    if not failed and not partial:
        log(f"dots: готово, все {len(jobs)}")
    return rc


if __name__ == "__main__":
    sys.exit(main())

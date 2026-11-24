#!/usr/bin/env python3
"""Проверка логики convert-docs без GPU: подставной движок вместо dots.mocr.

    ~/ocr-env2/bin/python -I tests/test_logic.py [--keep]

Движок-заглушка отвечает по заданному сценарию: «распознано», «таймаут», «таймаут, но спасло резервное
чтение Vision», «ошибка». Через настоящие скрипты (route.py, dots_batch.py, postcheck.py, merge_eyes.py,
workspace.py, contact_sheet.py, install.sh) проверяются: код выхода и план повтора при нераспознанных
страницах; отказ выдачи; выдача новой версии без перезаписи; очистка только по точному разрешению;
сканы внутри PDF с текстом; многостраничный TIFF; HEIC и неподдерживаемые форматы; постпроверка (сбой
сверки, исправленное число, пустые страницы); формулы; сумма цифрами и прописью; листы миниатюр по плану; установщик.
Модель не загружается, GPU не нужен, около минуты. Код выхода 0 — всё прошло.
"""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
SKILL = HERE.parent
SCRIPTS = SKILL / "scripts"
sys.path.insert(0, str(SCRIPTS))

import pymupdf  # noqa: E402
from PIL import Image, ImageDraw  # noqa: E402

import common  # noqa: E402
import dots_batch  # noqa: E402
import dots_engine  # noqa: E402

PY = "python3"          # route/workspace/postcheck/merge_eyes работают системным python3, как у агента
RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok, note: str = "") -> None:
    RESULTS.append((name, bool(ok), note))
    print(f"{'OK  ' if ok else 'FAIL'} {name}" + (f" — {note}" if note and not ok else ""), flush=True)


def run(args: list, **kw) -> subprocess.CompletedProcess:
    return subprocess.run([str(a) for a in args], capture_output=True, text=True, **kw)


# ---------- подставной движок ----------
class FakeEngine:
    queue: list[str] = []
    calls = 0

    def __init__(self, **kw):
        pass

    def load(self):
        pass

    def unload(self):
        pass

    def ocr_page(self, img):
        FakeEngine.calls += 1
        what = FakeEngine.queue.pop(0) if FakeEngine.queue else "ok"
        size = img.size
        if what == "ok":
            return dots_engine.PageResult(f"ТЕКСТ-СТРАНИЦЫ-{FakeEngine.calls} распознан подставным движком",
                                          "layout", [], image_size=size)
        if what == "timeout":
            return dots_engine.PageResult("", "timeout", ["TIMEOUT"], image_size=size)
        if what == "vfb":
            return dots_engine.PageResult("резервное чтение Vision", "timeout", ["TIMEOUT", "VISION_FALLBACK"],
                                          image_size=size, engine="apple-vision")
        return dots_engine.PageResult("", "error", ["ERROR:Тест:сбой"], image_size=size)


def batch(plan: Path, queue: list[str]) -> tuple[int, str]:
    FakeEngine.queue, FakeEngine.calls = list(queue), 0
    dots_batch.dots_engine.DotsEngine = FakeEngine
    dots_batch.ONLY.clear()
    dots_batch.ORIG.clear()
    sys.argv = ["dots_batch.py", "--plan", str(plan), "--no-check"]
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
        rc = dots_batch.main()
    return rc, buf.getvalue()


# ---------- тестовые файлы ----------
def scan_img(label: str, w: int = 1000, h: int = 1400) -> Image.Image:
    img = Image.new("RGB", (w, h), "white")
    d = ImageDraw.Draw(img)
    for y in range(120, h - 120, 60):
        d.rectangle([100, y, w - 100, y + 18], fill=(30, 30, 30))
    d.text((100, 40), label, fill="black")
    return img


def img_bytes(img: Image.Image, fmt: str = "JPEG") -> bytes:
    b = io.BytesIO()
    img.save(b, fmt)
    return b.getvalue()


LOREM = ("Договор поставки № 12345 от 01.02.2025. Lorem ipsum dolor sit amet, consectetur adipiscing elit, "
         "sed do eiusmod tempor incididunt ut labore et dolore magna aliqua 98765.")


def pdf(path: Path, pages: list[str]) -> Path:
    """pages: 'text' — текстовый слой; 'scan' — картинка страницы; 'blank' — пустой лист;
    'logo' — 40 знаков текста и маленькая картинка (не скан)."""
    doc = pymupdf.open()
    for i, kind in enumerate(pages, 1):
        pg = doc.new_page(width=595, height=842)
        if kind == "text":
            pg.insert_textbox(pg.rect + (50, 50, -50, -50), f"Page {i}. " + LOREM.encode("ascii", "ignore").decode() * 3,
                              fontsize=11)
        elif kind == "scan":
            pg.insert_image(pg.rect, stream=img_bytes(scan_img(f"scan {i}")))
        elif kind == "logo":
            pg.insert_text((60, 80), "Page with a small logo only, 40 chars.", fontsize=11)
            pg.insert_image(pymupdf.Rect(60, 100, 160, 200), stream=img_bytes(scan_img("logo", 100, 100)))
    doc.save(path)
    return path


def plan_file(path: Path, rows: list[dict]) -> Path:
    path.write_text(json.dumps({"plan": rows}, ensure_ascii=False), encoding="utf-8")
    return path


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


# ---------- сценарии ----------
def t_exit_codes(tmp: Path) -> None:
    out = tmp / "out"
    src = pdf(tmp / "три_страницы.pdf", ["scan", "scan", "scan"])
    row = {"src": str(src), "rel": src.name, "out": str(out / "a/a.md"), "engine": "ocr", "pages": 3}
    p = plan_file(tmp / "plan.json", [row])
    retry = p.with_suffix(".retry.json")

    rc, log = batch(p, ["timeout"] * 3)
    js = json.loads((out / "a/a_ocr.json").read_text(encoding="utf-8"))
    check("код выхода 1, если не распознана ни одна страница", rc == 1, f"rc={rc}")
    check("такой файл попадает в план повтора", retry.exists() and json.loads(retry.read_text())["plan"][0]["src"] == str(src))
    check("в журнале: «не распознана ни одна страница»", "не распознана ни одна страница" in log)
    check("в _ocr.json failed_pages = [1, 2, 3]", js.get("failed_pages") == [1, 2, 3], str(js.get("failed_pages")))
    check("нет ложного «готово, все»", "готово, все" not in log)

    retry.unlink(missing_ok=True)
    rc, log = batch(p, ["ok", "timeout", "ok"])
    js = json.loads((out / "a/a_ocr.json").read_text(encoding="utf-8"))
    check("частично: код выхода 1", rc == 1, f"rc={rc}")
    check("частично: failed_pages = [2]", js.get("failed_pages") == [2], str(js.get("failed_pages")))
    check("частично: в журнале названа стр. 2", "не распознаны стр. 2" in log)
    check("частично: файл не в плане повтора (страницы — на чтение глазами)", not retry.exists())

    rc, log = batch(p, ["ok", "vfb", "ok"])
    js = json.loads((out / "a/a_ocr.json").read_text(encoding="utf-8"))
    check("таймаут, спасённый резервным чтением Vision, — не провал (код 0)", rc == 0 and "failed_pages" not in js, f"rc={rc}")

    rc, _ = batch(p, ["ok", "error", "ok"])
    check("ошибка движка на странице — код 1", rc == 1)

    rows = []
    for k in range(4):
        s = tmp / f"фото{k}.png"
        scan_img(f"p{k}").save(s)
        rows.append({"src": str(s), "rel": s.name, "out": str(out / f"f{k}/f{k}.md"), "engine": "ocr", "pages": 1})
    p4 = plan_file(tmp / "plan4.json", rows)
    rc, log = batch(p4, ["timeout"] * 4)
    r4 = json.loads(p4.with_suffix(".retry.json").read_text(encoding="utf-8"))["plan"]
    check("три файла подряд без результата — остановка («отказ среды»)", "отказ среды" in log and FakeEngine.calls == 3,
          f"вызовов движка {FakeEngine.calls}")
    check("остаток пачки — в плане повтора (4 файла)", len(r4) == 4, f"в повторе {len(r4)}")

    tif = tmp / "три_кадра.tif"
    frames = [scan_img(f"k{i}").convert("L") for i in range(3)]
    frames[0].save(tif, "TIFF", save_all=True, append_images=frames[1:])
    pt = plan_file(tmp / "plan_tif.json", [{"src": str(tif), "rel": tif.name, "out": str(out / "t/t.md"),
                                            "engine": "ocr", "pages": 3}])
    rc, _ = batch(pt, ["ok"] * 3)
    md = (out / "t/t.md").read_text(encoding="utf-8")
    check("многостраничный TIFF: распознаны все 3 кадра", rc == 0 and FakeEngine.calls == 3 and "## Страница 3" in md)
    check("tiff_pages без PIL: 3 кадра", common.tiff_pages(tif) == 3)
    bad = tmp / "не_tiff.tif"
    bad.write_bytes(b"not a tiff at all")
    cut = tmp / "обрезанный.tif"
    cut.write_bytes(tif.read_bytes()[:12])
    check("tiff_pages: не TIFF и обрезанный файл — 1, без сбоя", common.tiff_pages(bad) == 1 and common.tiff_pages(cut) == 1)


def prepare(target: Path, out: Path, service: Path, *extra) -> tuple[int, Path | None, str]:
    r = run([PY, SCRIPTS / "workspace.py", "prepare", target, "--out", out, "--service-root", service, *extra])
    runs = sorted((service / "runs").glob("*")) if (service / "runs").exists() else []
    return r.returncode, (runs[-1] if runs else None), r.stdout + r.stderr


def t_publish(tmp: Path) -> None:
    target, out, service = tmp / "дело", tmp / "выдача", tmp / "служебное"
    target.mkdir()
    pdf(target / "скан.pdf", ["scan", "scan"])
    rc, run_dir, _ = prepare(target, out, service)
    check("prepare: скан в план OCR", rc == 0 and run_dir is not None)
    work, plan = run_dir / "documents", run_dir / "plan.json"
    rc, _ = batch(plan, ["ok", "timeout"])
    run([PY, SCRIPTS / "postcheck.py", work])
    r = run([PY, SCRIPTS / "workspace.py", "publish", run_dir])
    check("publish отказывает при нераспознанной и непрочитанной странице", r.returncode == 1
          and "не распознаны и не прочитаны глазами стр. 2" in r.stderr, r.stderr.strip()[-150:])
    check("при отказе документ не выдан", not list(out.rglob("скан*.md")) if out.exists() else True)

    eye = next(work.rglob("скан.md")).parent / "_ЧТЕНИЕ-скан-стр-2.md"
    eye.write_text("# Чтение глазами: скан.pdf, стр. 2\n\nПрочитано по картинке: ГЛАЗАМИ-777.\n", encoding="utf-8")
    r = run([PY, SCRIPTS / "workspace.py", "publish", run_dir])
    check("publish отказывает, пока чтение глазами не вставлено", r.returncode == 1
          and "объединить ручное чтение" in (r.stdout + r.stderr))

    run([PY, SCRIPTS / "merge_eyes.py", work, "--keep"])
    run([PY, SCRIPTS / "postcheck.py", work])
    r = run([PY, SCRIPTS / "workspace.py", "publish", run_dir])
    md = next(out.rglob("скан.md"), None)
    check("после чтения глазами publish выдаёт документ", r.returncode == 0 and md is not None, r.stderr.strip()[-150:])
    check("в выдаче — вставленное чтение глазами", md is not None and "ГЛАЗАМИ-777" in md.read_text(encoding="utf-8"))
    first = sha(md)

    r = run([PY, SCRIPTS / "workspace.py", "publish", run_dir])
    check("повторная выдача без изменений не плодит версий", len(list(out.rglob("скан*.md"))) == 1)
    wmd = next(work.rglob("скан.md"))
    wmd.write_text(wmd.read_text(encoding="utf-8") + "\nдописано\n", encoding="utf-8")
    run([PY, SCRIPTS / "workspace.py", "publish", run_dir])
    versions = [p.name for p in out.rglob("скан*.md")]
    check("изменённый результат выдаётся новой версией, прежний не тронут",
          len(versions) == 2 and any("(версия " in v for v in versions) and sha(md) == first, str(versions))

    data = json.loads(plan.read_text(encoding="utf-8"))
    data["plan"][0]["pages"] = 5
    plan.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    r = run([PY, SCRIPTS / "workspace.py", "publish", run_dir])
    check("publish отказывает, если число страниц OCR расходится с планом", r.returncode == 1
          and "расходится с планом" in r.stderr)
    data["plan"][0]["pages"] = 2
    plan.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    run([PY, SCRIPTS / "workspace.py", "publish", run_dir])

    r = run([PY, SCRIPTS / "workspace.py", "cleanup-plan", run_dir])
    manifest = sorted(run_dir.glob("cleanup-plan-*.json"))[-1]
    check("cleanup-plan ничего не удаляет", r.returncode == 0 and run_dir.exists())
    r = run([PY, SCRIPTS / "workspace.py", "cleanup", run_dir, "--manifest", manifest, "--approved-path", tmp])
    check("cleanup отказывает при неточном разрешённом пути", r.returncode == 1 and run_dir.exists())
    latest = json.loads(sorted(run_dir.glob("delivery-*.json"), key=lambda p: p.stat().st_mtime_ns)[-1]
                        .read_text(encoding="utf-8"))["files"][0]
    keep = Path(latest).read_bytes()
    Path(latest).write_bytes(keep + b"\n")
    r = run([PY, SCRIPTS / "workspace.py", "cleanup", run_dir, "--manifest", manifest, "--approved-path", run_dir])
    check("cleanup отказывает, если выданный файл изменился", r.returncode == 1 and run_dir.exists()
          and "изменился" in r.stderr)
    Path(latest).write_bytes(keep)
    r = run([PY, SCRIPTS / "workspace.py", "cleanup", run_dir, "--manifest", manifest, "--approved-path", run_dir])
    check("cleanup по точному разрешению удаляет только папку запуска", r.returncode == 0 and not run_dir.exists()
          and (target / "скан.pdf").exists() and md.exists(), r.stderr.strip()[-150:])


def t_hybrid(tmp: Path) -> None:
    target, out, service = tmp / "иск", tmp / "выдача", tmp / "служебное"
    target.mkdir()
    pdf(target / "иск.pdf", ["text", "scan", "text"])
    rc, run_dir, log = prepare(target, out, service)
    data = json.loads((run_dir / "plan.json").read_text(encoding="utf-8"))["plan"]
    engines = [r["engine"] for r in data]
    check("PDF «текст — скан — текст»: быстрая ветка + OCR только стр. 2",
          len(data) == 2 and engines[1] == "ocr" and data[1].get("only_pages") == [2], str(engines))
    r = run([PY, SCRIPTS / "workspace.py", "publish", run_dir])
    check("до OCR сканов publish отказывает (страница не потеряется)", r.returncode == 1
          and "OCR не завершён" in r.stderr, r.stderr.strip()[-150:])
    rc, _ = batch(run_dir / "plan.json", ["ok"])
    md = next((run_dir / "documents").rglob("иск.md")).read_text(encoding="utf-8")
    secs = md.split("## Страница ")
    check("скан встал на место стр. 2, стр. 1 и 3 — из текстового слоя",
          rc == 0 and len(secs) == 4 and "ТЕКСТ-СТРАНИЦЫ-1" in secs[2] and "Lorem" in secs[1] and "Lorem" in secs[3])
    check("у стр. 2 пометка «без текстового слоя», итог «OCR стр. 2 из 3»",
          "Страница без текстового слоя" in secs[2] and "Распознано: OCR стр. 2 из 3" in md)
    batch(run_dir / "plan.json", ["ok"])
    md2 = next((run_dir / "documents").rglob("иск.md")).read_text(encoding="utf-8")
    check("повторный OCR не дублирует страницу и концовку",
          md2.count("Страница без текстового слоя") == 1 and md2.count("Распознано:") == 1)
    run([PY, SCRIPTS / "postcheck.py", run_dir / "documents"])
    r = run([PY, SCRIPTS / "workspace.py", "publish", run_dir])
    check("после OCR сканов publish выдаёт документ", r.returncode == 0 and next(out.rglob("иск.md"), None) is not None,
          r.stderr.strip()[-150:])


def t_route(tmp: Path) -> None:
    target = tmp / "разное"
    (target / "вложено").mkdir(parents=True)
    pdf(target / "смесь.pdf", ["text", "scan", "blank", "logo", "scan"])
    pdf(target / "все_сканы.pdf", ["scan", "scan"])
    jpg = tmp / "x.jpg"
    scan_img("heic").save(jpg)
    subprocess.run(["sips", "-s", "format", "heic", str(jpg), "--out", str(target / "вложено/фото.heic")],
                   check=True, capture_output=True)
    (target / "архив.zip").write_bytes(b"PK\x05\x06" + bytes(18))
    (target / ".скрытый.txt").write_text("x")
    (target / "~$черновик.docx").write_bytes(b"x")
    (target / "служебное.json").write_text("{}")
    out, plan = tmp / "out", tmp / "plan.json"
    r = run([PY, SCRIPTS / "route.py", target, "--out", out, "--json", plan])
    rows = {r_["rel"]: r_ for r_ in json.loads(plan.read_text(encoding="utf-8"))["plan"] if r_["engine"] != "ocr"}
    ocr = {r_["rel"]: r_ for r_ in json.loads(plan.read_text(encoding="utf-8"))["plan"] if r_["engine"] == "ocr"}
    import unicodedata
    ocr = {unicodedata.normalize("NFC", k): v for k, v in ocr.items()}
    check("сканы внутри PDF: в OCR стр. 2 и 5; пустой лист и страница с логотипом — нет",
          ocr.get("смесь.pdf", {}).get("only_pages") == [2, 5], str(ocr.get("смесь.pdf", {}).get("only_pages")))
    check("PDF только из сканов — целиком в OCR, без only_pages",
          "все_сканы.pdf" in ocr and not ocr["все_сканы.pdf"].get("only_pages"))
    h = ocr.get("вложено/фото.heic", {})
    check("HEIC: OCR по JPEG-копии, orig — исходник", h.get("src", "").endswith(".jpg") and Path(h.get("src", "")).exists()
          and h.get("orig", "").endswith(".heic"), str(h)[:150])
    check("архив назван в плане как пропуск", rows.get("архив.zip", {}).get("engine") == "пропуск")
    names = set(rows) | set(ocr)
    check("скрытые, временные и служебные файлы в план не попадают",
          not any(n.startswith((".", "~$")) or n.endswith(".json") for n in names), str(sorted(names)))
    soft = target.parent / "переносы"
    soft.mkdir()
    doc = pymupdf.open()
    pg = doc.new_page(width=595, height=842)
    pg.insert_textbox(pg.rect + (50, 50, -50, -50), ("Шифр 091\u00ad8065. Юриди\u00adческий адрес сторон договора. " * 8),
                      fontsize=11, fontfile="/System/Library/Fonts/Supplemental/Arial.ttf", fontname="ar")
    doc.save(soft / "переносы.pdf")
    run([PY, SCRIPTS / "route.py", soft, "--out", tmp / "out_soft", "--json", tmp / "plan_soft.json"])
    mdp = next((tmp / "out_soft").rglob("*.md"), None)
    md = mdp.read_text(encoding="utf-8") if mdp else ""
    check("текстовый слой: мягкий перенос между цифрами → дефис, внутри слова убран",
          "091-8065" in md and "Юридический" in md and "\u00ad" not in md, md[:80].replace("\n", " "))
    import route
    for fmt in ("PNG", "JPEG", "GIF", "BMP"):
        im = scan_img("px", 1234, 567)
        data = img_bytes(im if fmt != "GIF" else im.convert("P"), fmt)
        check(f"image_px по заголовку {fmt}", route.image_px(data) == 1234 * 567, str(route.image_px(data)))


def t_postcheck(tmp: Path) -> None:
    root = tmp / "пост"
    good_cc = {"vision_conf": 0.9, "vision_lines": 20, "reliable": True, "names_unconfirmed": [],
               "numbers_unconfirmed": ["28.07.2025"], "names_rare": []}
    text = "Пациентка, дата рождения указана в шапке. " * 10
    docs = {"ошибка_сверки": ({"error": "VisionError: тест"}, text),
            "исправлено": (good_cc, text + " Дата рождения 28.07.1966."),
            "не_исправлено": (good_cc, text + " Дата рождения 28.07.2025.")}
    for name, (cc, body) in docs.items():
        d = root / name
        d.mkdir(parents=True)
        (d / f"{name}.md").write_text(f"# {name}.pdf\n\n{body}\n\n---\nРаспознано: 1 стр., 1 с, dots.mocr 8-bit MLX",
                                      encoding="utf-8")
        (d / f"{name}_ocr.json").write_text(json.dumps({"src": "x", "pages": 1, "page_stats": [
            {"page": 1, "mode": "layout", "warnings": [], "gen_tokens": 900, "crosscheck": cc,
             "coverage": {"missing": 0, "scored": 0, "credible": 0}, "rotation": 0}]}, ensure_ascii=False), encoding="utf-8")
    e = root / "пустая_страница"
    e.mkdir()
    (e / "пустая_страница.md").write_text("## Страница 1\n\n" + text + "\n\n## Страница 2\n\n\n\n## Страница 3\n\n" + text,
                                          encoding="utf-8")
    run([PY, SCRIPTS / "postcheck.py", root])
    rep = (root / "_ПРОВЕРИТЬ.md").read_text(encoding="utf-8")
    block = lambda n: rep.split(f"### {n}/{n}.md")[1].split("### ")[0] if f"### {n}/{n}.md" in rep else ""  # noqa: E731
    check("сбой сверки Vision вынесен в «сверить»", "сверка вторым чтением не выполнена" in block("ошибка_сверки"))
    check("исправленное число не повторяется в «сверить»", "28.07.2025" not in block("исправлено"))
    check("неисправленное число остаётся в «сверить»", "28.07.2025" in block("не_исправлено"))
    check("пустая страница названа («ПУСТЫЕ СТРАНИЦЫ — стр. 2»)", "ПУСТЫЕ СТРАНИЦЫ — стр. 2" in rep)


def t_formula() -> None:
    f = dots_engine._formula
    check("формула-арифметика → обычный текст",
          f(r"2\,034+350 = 2384 \text{ (тыс. руб.)}") == "2 034+350 = 2384 (тыс. руб.)")
    check("десятичная запятая 5{,}5 и знаки ×, %", f(r"$$ 1025 \cdot 5{,}5\% $$") == "1025 × 5,5%")
    check("дробь и степень остаются LaTeX", f(r"\frac{a}{b}").startswith("$$") and f("x^2").startswith("$$"))


def t_amounts() -> None:
    import requisites
    a = requisites.check_amounts
    r = a("1 174 268,70 руб. (один миллион сто сорок семь тысяч двести шестьдесят восемь рублей 70 копеек)")
    check("сумма прописью с копейками цифрами: расхождение найдено",
          bool(r) and "1 147 268" in r[0][1], str(r))
    check("сумма прописью с копейками цифрами: верная — без находки",
          not a("1 174 268,70 руб. (один миллион сто семьдесят четыре тысячи двести шестьдесят восемь рублей 70 копеек)"))
    check("«руб. 07 коп.» против ,70 — расхождение копеек",
          bool(a("1 174 268,70 руб. (один миллион сто семьдесят четыре тысячи двести шестьдесят восемь руб. 07 коп.)")))
    check("«рублей 00 копеек» при ,00 — без находки", not a("65 000,00 руб. (шестьдесят пять тысяч рублей 00 копеек)"))
    check("копейки только в скобках — находка", bool(a("500 руб. (пятьсот рублей 50 копеек)")))
    check("прежнее: «75 000 (семьдесят тысяч)» — находка", bool(a("Итого: 75 000 руб. (семьдесят тысяч)")))
    check("прежнее: «10 (десяти) дней», «10 (2 экз.)» — без находок",
          not a("в течение 10 (десяти) дней") and not a("передано 10 (2 экз.) копий"))
    check("прежнее: копейки словами", not a("2,10 руб. (два рубля десять копеек)")
          and bool(a("2,10 руб. (два рубля двадцать копеек)")))


def t_contact_sheet(tmp: Path) -> None:
    out = tmp / "cs"
    a = pdf(tmp / "a.pdf", ["scan", "scan"])
    b = tmp / "b.png"
    scan_img("b").save(b)
    rows = [{"src": str(a), "rel": "a.pdf", "out": str(out / "a/a.md"), "engine": "ocr", "pages": 2},
            {"src": str(b), "rel": "b.png", "out": str(out / "b/b.md"), "engine": "ocr", "pages": 1}]
    (out / "a").mkdir(parents=True)
    (out / "a/a_ocr.json").write_text(json.dumps({"page_stats": [{"page": 1, "mode": "layout", "rotation": 90},
                                                                  {"page": 2, "mode": "empty", "rotation": 0}]}))
    p = plan_file(tmp / "cs_plan.json", rows)
    r = run([sys.executable, "-I", SCRIPTS / "contact_sheet.py", "--plan", p, tmp / "sheets"])
    legend = (tmp / "sheets/_ЛЕГЕНДА.md").read_text(encoding="utf-8") if (tmp / "sheets/_ЛЕГЕНДА.md").exists() else ""
    check("листы миниатюр по плану: лист и легенда «Д1 — a.pdf», «Д2 — b.png»",
          r.returncode == 0 and (tmp / "sheets/запуск_лист_1.png").exists() and "Д1 — `a.pdf`" in legend
          and "Д2 — `b.png`" in legend, r.stderr[-150:])


def t_install(tmp: Path) -> None:
    home = tmp / "home"
    (home / ".claude").mkdir(parents=True)
    env = {**os.environ, "HOME": str(home)}
    r = run(["bash", SKILL / "install.sh"], env=env)
    check("install.sh: только Claude Code, если нет Codex", r.returncode == 0
          and (home / ".claude/skills/convert-docs/SKILL.md").exists() and not (home / ".agents").exists())
    (home / ".codex/skills/convert-docs").mkdir(parents=True)
    (home / ".codex/skills/convert-docs/SKILL.md").write_text("старая версия")
    r = run(["bash", SKILL / "install.sh", "--dry-run"], env=env)
    check("install.sh --dry-run ничего не меняет", (home / ".codex/skills/convert-docs/SKILL.md").read_text() == "старая версия")
    r = run(["bash", SKILL / "install.sh"], env=env)
    backups = list((home / ".local/share/convert-docs/skill-backups").glob("*"))
    check("install.sh: обновляет прежний ~/.codex/skills, не создаёт дубль в ~/.agents",
          r.returncode == 0 and (home / ".codex/skills/convert-docs/scripts/workspace.py").exists()
          and not (home / ".agents/skills/convert-docs").exists())
    check("install.sh: прежние копии — вне папок скиллов", len(backups) == 2
          and not list((home / ".claude/skills").glob("convert-docs.*")), str([b.name for b in backups]))
    check("install.sh: в установку не попадают README, install.sh, tests",
          not (home / ".claude/skills/convert-docs/README.md").exists()
          and not (home / ".claude/skills/convert-docs/tests").exists())


def main() -> int:
    keep = "--keep" in sys.argv
    tmp = Path(tempfile.mkdtemp(prefix="convert-docs-logic-"))
    print(f"папка проверки: {tmp}\n")
    for name, fn in (("коды выхода", t_exit_codes), ("выдача", t_publish), ("сканы внутри PDF", t_hybrid),
                     ("маршрутизация", t_route), ("постпроверка", t_postcheck), ("листы миниатюр", t_contact_sheet),
                     ("установщик", t_install)):
        print(f"--- {name}")
        d = tmp / name.replace(" ", "_")
        d.mkdir()
        try:
            fn(d)
        except Exception as e:  # noqa: BLE001
            check(f"{name}: сценарий без исключений", False, f"{type(e).__name__}: {e}")
    print("--- формулы")
    t_formula()
    print("--- сумма цифрами и прописью")
    t_amounts()
    ok = sum(1 for _, g, _ in RESULTS if g)
    print(f"\nИТОГ: {ok} из {len(RESULTS)} проверок" + ("" if ok == len(RESULTS) else " — есть провалы"))
    if keep or ok != len(RESULTS):
        print(f"папка оставлена: {tmp}")
    else:
        shutil.rmtree(tmp, ignore_errors=True)
    return 0 if ok == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())

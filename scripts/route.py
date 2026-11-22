#!/usr/bin/env python3
"""Маршрутизация документов между markitdown и marker + прогон markitdown-ветки.

    python3 route.py <цель> --out <корень_вывода> [--json plan.json] [--force]

Цель — папка (рекурсивно) или один файл. Скрипт:
  1. раскладывает файлы по движкам,
  2. для PDF решает НЕ по гаданию, а по факту: гонит markitdown (секунды)
     и смотрит на его собственный результат — не рассыпалась ли структура,
  3. markitdown-ветку сразу пишет в указанную рабочую папку,
  4. сканы и картинки отдаёт списком в plan.json (engine «ocr») для dots_batch.py.

Путь вывода один на оба движка:
    <out>/<относительная папка исходника>/<имя без расширения>/<имя>.md

Это низкоуровневый шаг. Для обычной конвертации используй workspace.py prepare,
который направляет служебные файлы вне пользовательской папки результата.

Office из сканов (17.10.2026): Word или презентация, в которой страницы вставлены
картинками (договор на реальном деле: 5 сканов, markitdown дал .md из пяти
ссылок на картинки). Если после markitdown текста меньше 300 символов, а внутри картинки
от 30 КБ — документ переводится в PDF (`<имя> (из docx).pdf` рядом с .md: soffice, а когда его
нет — сборка из картинок документа в порядке их появления) и уходит в OCR; строка плана:
"src" — этот PDF, "orig" — исходник. Текст есть, но внутри картинки от 100 КБ — остаётся
markitdown и пометка «внутри N картинок, их текст не распознан».

Пороги ниже подобраны на 32 PDF реального дела (письма, счета, платёжки,
бланки, сканы) — по замерам автора. Менять только вместе с перепроверкой.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import unicodedata
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import BAD_SUFFIX, TIFF_EXTS, find_md, tiff_pages   # пометка «плохое качество» в имени .md — общая для скриптов

# окружения: по умолчанию как в УСТАНОВКА.md; другое место — переменные CONVERT_DOCS_LIGHT_VENV / CONVERT_DOCS_OCR_VENV
LIGHT_VENV = Path(os.environ.get("CONVERT_DOCS_LIGHT_VENV", "~/Projects/markitdown-local/.venv")).expanduser()
MARKITDOWN = LIGHT_VENV / "bin/markitdown"
MD_PYTHON = MARKITDOWN.parent / "python"  # в этом venv есть openpyxl

# --- пороги (по замерам автора) ---
MIN_CHARS_PER_PAGE = 80   # меньше — текстового слоя фактически нет, это скан

# Сколько линий-разделителей таблицы нужно, чтобы отдать файл pdfplumber.
# Он режет страницу по НАРИСОВАННЫМ линиям, поэтому силён ровно там, где есть
# сетка: выписки ЕГРН, бланки, формы, платёжки. На сплошной прозе он бесполезен.
MIN_TABLE_ROWS = 4

# Вшитый OCR-слой низкого качества (Adobe Scan, FineReader) проходит все проверки
# ниже: символов много, ToUnicode есть, кириллицы полно — а текст в нём каша
# («ПОЛIIОСlt311MCll01131tltC»). Замер 15.10.2026 на 40 PDF трёх дел: у здоровых
# слоёв 0 слов со смесью алфавитов на тысячу (у одного Adobe Scan с хорошим слоем
# 10,5), у негодных 62–295 (одно дело) и 350–440 (другое). Порог 40 — с запасом.
GARBAGE_MIXED_PER_1000 = 40   # выше — слой негоден, файл в OCR
GARBAGE_NOTE_PER_1000 = 10    # выше — слой с шумом, оставляем, но помечаем
SCRIPTS = Path(__file__).resolve().parent
OCR_PYTHON = next((p for p in ([Path(os.environ["CONVERT_DOCS_OCR_VENV"]).expanduser() / "bin/python"]
                               if os.environ.get("CONVERT_DOCS_OCR_VENV") else [])
                   + [Path.home() / "ocr-env2/bin/python", Path.home() / "ocr-env/bin/python"] if p.exists()), None)
MIXED_WORD = re.compile(r"(?<![A-Za-zА-Яа-яЁё])(?=[A-Za-zА-Яа-яЁё]*[А-Яа-яЁё])(?=[A-Za-zА-Яа-яЁё]*[A-Za-z])[A-Za-zА-Яа-яЁё]{3,}(?![A-Za-zА-Яа-яЁё])")
ANY_WORD = re.compile(r"[A-Za-zА-Яа-яЁё]{3,}")

NATIVE = {  # структура берётся из самого файла, markitdown тут точнее marker
    ".docx", ".xlsx", ".pptx", ".html", ".htm", ".epub",
    ".csv", ".txt", ".msg",
}
RASTER = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tif", ".tiff", ".webp"}

# markitdown эти форматы не читает — нужен мост через soffice/textutil.
# Цель моста выбирается по смыслу: таблица должна стать таблицей, а не текстом.
# `.xls` сюда попал не сразу: он выглядит «нативным», но в venv markitdown нет
# `xlrd`, и файл валился с ModuleNotFoundError.
LEGACY = {
    ".doc": "docx", ".rtf": "docx", ".odt": "docx",
    ".xls": "xlsx", ".ods": "xlsx",
    ".ppt": "pptx", ".odp": "pptx",
}
SPREADSHEET = {".xlsx", ".xls", ".ods"}  # для них дополнительно выгружаем CSV

# Office из сканов: страницы вставлены в документ картинками, текста нет (см. docstring).
SCAN_EXTS = {".docx", ".pptx", ".doc", ".rtf", ".odt", ".ppt", ".odp"}
SCAN_TEXT_MIN = 300         # символов текста (без ссылок на картинки и разметки) — меньше: документ из сканов
SCAN_TEXT_NONE = 50         # меньше — текста практически нет: OCR в любом случае (PDF из картинок тоже годится);
                            # от 50 до SCAN_TEXT_MIN — в OCR только через soffice (его PDF несёт и текст письма)
                            # и при картинках на NOTE_IMG_MIN и больше суммарно (короткое письмо с подписью — не скан)
SCAN_IMG_MIN = 30_000       # байт: картинка размером со страницу скана (логотип и подпись меньше)
NOTE_IMG_MIN = 100_000      # байт: при тексте и таких картинках — пометка «текст картинок не распознан»
NOTE_IMG_PX = 1_000_000     # пикселей: или картинка размером со страницу (чистый скан в PNG весит меньше 100 КБ)
IMG_EXTS = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp", ".gif", ".webp"}
IMG_LINK = re.compile(r"!\[[^\]]*\]\([^)]*\)")      # ![alt](data:… или путь)
OCR_FOOTER = re.compile(r"^Распознано: \d+ стр\.", re.M)   # подпись dots_batch.py в конце .md
MEDIA = {".mp3", ".m4a", ".wav", ".aac", ".ogg", ".mp4", ".mov", ".avi", ".mkv"}
HEIC = {".heic", ".heif"}   # фото с iPhone: PIL их не открывает, OCR идёт по JPEG-копии (sips, встроен в macOS)
QUIET_EXT = {".ini", ".lnk", ".tmp", ".db", ".plist", ".json", ".log"}   # служебное: не перечислять как пропущенное
SKIP_NAMES = {"_КАТАЛОГ.md", "_ЗАМЕТКИ.md", "_ПРОГНОЗЫ.md", "CLAUDE.md", "AGENTS.md", "AGENTS.override.md", ".DS_Store"}
# Папки с уже готовыми конвертациями и артефакты marker внутри них.
SKIP_DIRS = {"_md", ".git", ".codex", ".agents", ".claude", "node_modules", "__pycache__", ".venv"}


def is_artifact(p: Path) -> bool:
    """Результат прошлой конвертации, а не исходник."""
    if any(part in SKIP_DIRS for part in p.parts):
        return True
    n = p.name
    # скрытые файлы и «двойники» macOS (._скан.pdf на флешке или из архива — по расширению PDF, внутри
    # мусор), временные файлы Office (~$документ.docx) — не документы
    if any(part.startswith(".") for part in p.parts) or n.startswith("~$"):
        return True
    return n.endswith("_meta.json") or n.startswith(("_page_", "_pdf_"))


def sh(cmd: list[str]) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=120).stdout
    except Exception:
        return ""


def pdf_probe(f: Path) -> tuple[int, int, int]:
    """(страниц, шрифтов, символов на первых 5 страницах)"""
    info = sh(["pdfinfo", str(f)])
    pages = 0
    for line in info.splitlines():
        if line.startswith("Pages:"):
            pages = int(line.split()[1])
            break
    fonts = len([l for l in sh(["pdffonts", str(f)]).splitlines()[2:] if l.strip()])
    chars = len("".join(sh(["pdftotext", "-f", "1", "-l", "5", str(f), "-"]).split()))
    return pages, fonts, chars


SCAN_IMAGE_MIN_PX = 500 * 500   # картинка меньше — логотип, печать, подпись, а не скан страницы


def scan_pages(f: Path, pages: int) -> list[int]:
    """Страницы без текстового слоя внутри PDF, где текст в целом есть: иск или ответ с приложенными
    сканами. Признак — меньше MIN_CHARS_PER_PAGE знаков текста и картинка не меньше 500×500 px
    (пустой лист-разделитель и страница с одной подписью в OCR не идут). Решение «скан или текст»
    по первым пяти страницам такие страницы теряло молча: быстрая ветка оставляла их пустыми."""
    parts = sh(["pdftotext", str(f), "-"]).split("\f")
    if parts and not parts[-1].strip():
        parts = parts[:-1]
    if not pages or len(parts) != pages:
        return []
    low = {i for i, t in enumerate(parts, 1) if len("".join(t.split())) < MIN_CHARS_PER_PAGE}
    if not low:
        return []
    big: set[int] = set()
    for line in sh(["pdfimages", "-list", str(f)]).splitlines()[2:]:
        c = line.split()
        if len(c) > 4 and c[0].isdigit() and c[3].isdigit() and c[4].isdigit() \
                and int(c[3]) * int(c[4]) >= SCAN_IMAGE_MIN_PX:
            big.add(int(c[0]))
    return sorted(low & big)


def pdf_has_broken_font(f: Path) -> bool:
    """В файле есть шрифт, чей текст принципиально нечитаем.

    Экспорт из CAD (межевые планы, чертежи, схемы) кладёт шрифт с кодировкой
    Identity-H и без карты ToUnicode. Коды символов тогда ничему не
    соответствуют, и любой читатель текстового слоя — pdftotext, pdfminer,
    marker — выдаёт кракозябры вида «ɍɫɥɨɜɧɵɟɨɛɨɡɧɚɱɟɧɢɹ» вместо
    «Условные обозначения».

    pdffonts печатает колонку `uni` (есть ли ToUnicode) третьей с конца.
    """
    for line in sh(["pdffonts", str(f)]).splitlines()[2:]:
        parts = line.split()
        if len(parts) < 4 or "Identity-H" not in line:
            continue
        if parts[-3] == "no":
            return True
    return False


CYRILLIC_RANGE = re.compile(r"[а-яА-ЯёЁ]")
MIN_READABLE_CYRILLIC = 200   # кириллических букв на страницу, ниже — слой бесполезен


def readable_cyrillic_per_page(f: Path) -> int:
    """Сколько ЧИТАЕМОЙ кириллицы даёт текстовый слой в расчёте на страницу.

    Одного наличия битого шрифта мало: им может быть набран только заголовок.
    Проверено на газетной полосе — шрифт без ToUnicode в шапке, а основной
    текст читается прекрасно. Принудительный OCR по этой плотной газетной
    полосе ОТНЯЛ больше половины содержимого: 4732 кириллических символа
    против 1933, название населённого пункта с 16 упоминаний до 5.

    Мерить долю кракозябр в выводе pdftotext нельзя — на битом шрифте он
    отдаёт не «ɋɏȿɆȺ», а цифры или пустоту (кракозябры в этом диапазоне даёт
    уже pdfium внутри marker). Поэтому считаем противоположное: много ли
    осталось нормально читаемого.

    Замер на семи файлах реального дела разделяет их начисто:

        газетная полоса              2368 букв/стр.  → обычный путь
        чертёж, схема …                0 букв/стр.  → force_ocr
    """
    pages = max(1, pdf_probe(f)[0])
    text = sh(["pdftotext", str(f), "-"])
    return len(CYRILLIC_RANGE.findall(text)) // pages


def text_layer_garbage(f: Path) -> dict:
    """Сколько слов со смесью алфавитов на тысячу слов даёт pdftotext (первые 5 стр.)."""
    text = sh(["pdftotext", "-l", "5", str(f), "-"])
    words = ANY_WORD.findall(text)
    if len(words) < 30:
        return {"words": len(words), "mixed_per_1000": 0.0}
    return {"words": len(words), "mixed_per_1000": 1000 * len(MIXED_WORD.findall(text)) / len(words)}


def pdf_orientation(f: Path, fix: Path | None = None) -> dict | None:
    """Ориентация страниц по Apple Vision (pdf_orient.py в OCR-окружении).
    Вернёт {"pages", "checked", "rot": {"1": 90, …}, "turned", "fixed"?} или None."""
    if OCR_PYTHON is None or not (SCRIPTS / "pdf_orient.py").exists():
        return None
    cmd = [str(OCR_PYTHON), str(SCRIPTS / "pdf_orient.py"), str(f)]
    if fix is not None:
        cmd += ["--all", "--fix", str(fix)]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        line = [l for l in r.stdout.splitlines() if l.startswith("{")]
        return json.loads(line[-1]) if line else None
    except Exception:
        return None


TABLE_ROWS = r'''
import sys, pdfplumber
n = 0
with pdfplumber.open(sys.argv[1]) as pdf:
    for page in pdf.pages[:5]:                 # пяти страниц хватает на вердикт
        for t in page.extract_tables():
            n += len(t)
print(n)
'''


def pdfplumber_table_rows(f: Path) -> int:
    """Сколько строк таблиц pdfplumber видит на первых пяти страницах.

    Он ищет НАРИСОВАННЫЕ линии, а не выравнивание текста, поэтому находит
    сетку там, где она есть физически: выписки ЕГРН, бланки, формы, платёжки.
    На сплошной прозе вернёт около нуля — и файл уйдёт в pymupdf4llm.
    """
    if not MD_PYTHON.exists():
        return 0
    try:
        r = subprocess.run([str(MD_PYTHON), "-c", TABLE_ROWS, str(f)],
                           capture_output=True, text=True, timeout=180)
    except Exception:
        return 0
    out = r.stdout.strip().splitlines()
    return int(out[-1]) if out and out[-1].isdigit() else 0


FAST_PDF = r'''
import sys
from pathlib import Path
src, dst, eng = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
if eng == "pymupdf4llm":
    import pymupdf4llm
    # по страницам, как у pdfplumber: на место пустой страницы-скана потом встанет её OCR
    chunks = pymupdf4llm.to_markdown(str(src), show_progress=False, page_chunks=True)
    md = "".join(f"\n\n## Страница {i}\n\n" + (c.get("text") or "") for i, c in enumerate(chunks, 1))
else:
    import pdfplumber, re as _re
    norm = lambda s: _re.sub(r"\s+", " ", s or "").strip()
    parts = []
    with pdfplumber.open(src) as pdf:
        for i, page in enumerate(pdf.pages, 1):
            parts.append(f"\n\n## Страница {i}\n")
            seen = set()
            for t in page.extract_tables():
                for row in t:
                    cells = [norm(c) for c in row]
                    if not any(cells):
                        continue
                    parts.append("| " + " | ".join(cells) + " |")
                    seen.update(c for c in cells if len(c) > 3)
            # Текст вне таблиц терять нельзя — подписи, отметки, примечания.
            # Но и дублировать уже попавшее в ячейки незачем: это раздувает
            # файл вдвое и заставляет читателя гадать, одно это место или два.
            tail = [ln for ln in (page.extract_text() or "").splitlines()
                    if norm(ln) and not any(norm(ln) in s or s in norm(ln) for s in seen)]
            if tail:
                parts.append("\n" + "\n".join(tail))
    md = "\n".join(parts)
dst.parent.mkdir(parents=True, exist_ok=True)
dst.write_text(md, encoding="utf-8")
'''


def run_fast_pdf(src: Path, dst: Path, engine: str) -> bool:
    """Быстрая ветка: PDF с живым текстовым слоем, без всякого OCR."""
    if not MD_PYTHON.exists():
        return False
    try:
        r = subprocess.run([str(MD_PYTHON), "-c", FAST_PDF, str(src), str(dst), engine],
                           capture_output=True, text=True, timeout=900)
    except Exception:
        return False
    return r.returncode == 0 and dst.exists() and dst.stat().st_size > 0


NUM_TOKEN = re.compile(r"\b\d{1,3}(?:[  ]\d{3})+\b|\b\d{4,}\b")


def num_tokens(text: str) -> set[str]:
    """Числа как токены. Убирать все пробелы нельзя — склеятся соседние ячейки."""
    return {re.sub(r"[  ]", "", m) for m in NUM_TOKEN.findall(text)}


def clean_invisible(text: str) -> str:
    """Невидимые символы текстового слоя мешают поиску: мягкий перенос (U+00AD) из PDF, свёрстанных с
    автопереносами («юриди\u00adческий»), и пробелы нулевой ширины. Между цифрами мягкий перенос —
    это дефис, перепутанный шрифтом («091\u00ad8065» → «091-8065»); внутри слова он просто убирается."""
    text = re.sub(r"(?<=\d)\u00ad(?=\d)", "-", text)
    return re.sub(r"[\u00ad\u200b\u200c\u200d\u2060\ufeff]", "", text)


def pick_fast_engine(f: Path, dst: Path, pages: int) -> tuple[str | None, int, str]:
    """Прогнать pdfplumber и pymupdf4llm, оставить того, кто сохранил больше цифр.

    Возвращает (движок, строк, причина) либо (None, 0, "") если оба провалились.

    Критерий — потерянные числовые токены относительно `pdftotext`. При равенстве
    берётся тот, у кого больше кириллицы: одинаково точные, но более полный.
    """
    want = num_tokens(sh(["pdftotext", str(f), "-"]))
    results = []
    with tempfile.TemporaryDirectory() as td:
        for eng in ("pdfplumber", "pymupdf4llm"):
            tmp = Path(td) / f"{eng}.md"
            if not run_fast_pdf(f, tmp, eng):
                continue
            text = tmp.read_text(errors="replace")
            if not text.strip():
                continue
            results.append((len(want - num_tokens(text)),
                            -len(CYRILLIC_RANGE.findall(text)), eng, text))
        if not results:
            return None, 0, ""
        lost, neg_cyr, eng, text = min(results)
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(clean_invisible(text), encoding="utf-8")
    n = md_quality(dst)[0]
    rival = ", ".join(f"{e} терял {l}" for l, _, e, _ in sorted(results) if e != eng)
    why = f"{pages} с., потеряно чисел {lost}" + (f" ({rival})" if rival else "")
    return eng, n, why


def md_quality(md_path: Path) -> tuple[int, float, int]:
    """(непустых строк, медиана длины, строк-разделителей markdown-таблиц)"""
    if not md_path.exists():
        return 0, 0.0, 0
    body = [l.rstrip() for l in md_path.read_text(errors="replace").splitlines()]
    ne = [l for l in body if l.strip()]
    if not ne:
        return 0, 0.0, 0
    seps = sum(
        1 for l in ne
        if l.startswith("|") and set(l.replace("|", "").strip()) <= set("-: ")
    )
    return len(ne), statistics.median(len(l) for l in ne), seps


def legacy_bridge(src: Path, workdir: Path) -> Path | None:
    """Старый формат → современный, который markitdown уже читает.

    Цель берётся из LEGACY: текст → .docx, таблица → .xlsx, презентация →
    .pptx. Гнать таблицу в .docx нельзя — она перестанет быть таблицей.

    Порядок мостов важен: soffice сохраняет таблицы, textutil их разбирает в
    поток абзацев (проверено на акте сверки — soffice дал корректную таблицу,
    textutil ту же таблицу расплющил). textutil — только запасной вариант,
    когда LibreOffice не установлен, и только для текстовых форматов.
    """
    target = LEGACY[src.suffix.lower()]
    dst = workdir / f"{src.stem}.{target}"
    if shutil.which("soffice"):
        subprocess.run(
            ["soffice", "--headless", "--convert-to", target,
             "--outdir", str(workdir), str(src)],
            capture_output=True, text=True, timeout=600,
        )
        if dst.exists():
            return dst
    if target == "docx" and src.suffix.lower() in {".doc", ".rtf", ".odt"}:
        r = subprocess.run(
            ["textutil", "-convert", "docx", "-output", str(dst), str(src)],
            capture_output=True, text=True, timeout=300,
        )
        if r.returncode == 0 and dst.exists():
            return dst
    return None


XLSX_DUMP = r'''
import csv, sys, unicodedata
from pathlib import Path
from openpyxl import load_workbook

src, out_dir = Path(sys.argv[1]), Path(sys.argv[2])
out_dir.mkdir(parents=True, exist_ok=True)
wb = load_workbook(src, data_only=True)          # data_only: значения, не формулы
made = []
for ws in wb.worksheets:
    rows = list(ws.iter_rows(values_only=True))
    while rows and all(c is None for c in rows[-1]):
        rows.pop()
    if not rows:
        continue
    safe = "".join(ch for ch in ws.title if ch.isalnum() or ch in " -_()")[:60].strip()
    dst = out_dir / f"{safe or 'Лист'}.csv"
    with dst.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        for r in rows:
            w.writerow(["" if c is None else str(c) for c in r])
    made.append(dst.name)
print("|".join(made))
'''


def xlsx_to_csv(src: Path, out_dir: Path) -> list[str]:
    """Каждый лист книги — отдельный CSV рядом с .md.

    Markdown-таблица для данных плохой носитель: объединённая шапка даёт
    «Unnamed: 1…N», пустые ячейки — «NaN», а строки разной длины ломают
    выравнивание столбцов (проверено на реестре дел: 125 «NaN», 20 «Unnamed»,
    строки по 5, 6, 12 и 17 столбцов в одной таблице).

    CSV лишён всех трёх бед: строка = запись, разделитель однозначен, пустая
    ячейка пустая. Модели он и читается точнее. .md всё равно делаем — для
    беглого чтения глазами, — но считать нужно по CSV, а сверять по оригиналу.
    """
    if not MD_PYTHON.exists():
        return []
    try:
        r = subprocess.run([str(MD_PYTHON), "-c", XLSX_DUMP, str(src), str(out_dir)],
                           capture_output=True, text=True, timeout=300)
    except Exception:
        return []
    if r.returncode != 0:
        return []
    return [n for n in r.stdout.strip().split("|") if n]


def run_markitdown(src: Path, dst: Path) -> bool:
    dst.parent.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(
        [str(MARKITDOWN), str(src), "-o", str(dst)],
        capture_output=True, text=True, timeout=600,
    )
    return r.returncode == 0 and dst.exists()


def done_md(dst: Path) -> Path | None:
    """Готовый результат: <имя>.md либо <имя> (ПЛОХОЕ КАЧЕСТВО).md (так документ переименовывает
    постпроверка); учитывается и переименованная папка."""
    parent = dst.parent
    for folder in (parent, parent.with_name(parent.name + BAD_SUFFIX)):
        found = find_md(folder, dst.stem)
        if found:
            return found
    return None


def drop_old_bad(dst: Path) -> None:
    """`--force`: документ переделывается, значит его старый «<имя> (ПЛОХОЕ КАЧЕСТВО).md» (если лежит рядом
    с будущим <имя>.md) устарел: убрать, чтобы рядом не оказалось двух версий."""
    old = dst.with_name(f"{dst.stem}{BAD_SUFFIX}.md")
    if old.exists():
        old.unlink()
        print(f"  --force: удалён старый «{old.name}»")


def visible_text_len(md_text: str) -> int:
    """Сколько букв и цифр в markdown без ссылок на картинки и без разметки."""
    t = re.sub(r"<[^>]+>", " ", IMG_LINK.sub(" ", md_text))
    return len(re.findall(r"[^\W_]", t))


def office_media(f: Path) -> list[int]:
    """Размеры (байт) растровых картинок внутри docx/pptx: word/media, ppt/media."""
    try:
        with zipfile.ZipFile(f) as z:
            return [i.file_size for i in z.infolist()
                    if re.match(r"(word|ppt)/media/", i.filename) and Path(i.filename).suffix.lower() in IMG_EXTS]
    except Exception:
        return []


def image_px(data: bytes) -> int:
    """Площадь картинки в пикселях по заголовку PNG, JPEG, GIF или BMP (без PIL: route.py работает
    системным python3). Неизвестный формат — 0."""
    import struct
    try:
        if data[:8] == b"\x89PNG\r\n\x1a\n":
            w, h = struct.unpack(">II", data[16:24])
            return w * h
        if data[:6] in (b"GIF87a", b"GIF89a"):
            w, h = struct.unpack("<HH", data[6:10])
            return w * h
        if data[:2] == b"BM":
            w, h = struct.unpack("<ii", data[18:26])
            return abs(w * h)
        if data[:2] == b"\xff\xd8":
            i = 2
            while i + 9 < len(data):
                if data[i] != 0xFF:
                    i += 1
                    continue
                marker = data[i + 1]
                if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
                    i += 2
                    continue
                size = struct.unpack(">H", data[i + 2:i + 4])[0]
                if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                    h, w = struct.unpack(">HH", data[i + 5:i + 9])
                    return w * h
                i += 2 + size
    except Exception:  # noqa: BLE001
        return 0
    return 0


def office_heavy_images(f: Path) -> int:
    """Сколько картинок внутри docx/pptx могут нести текст: от NOTE_IMG_MIN байт или от NOTE_IMG_PX пикселей."""
    n = 0
    try:
        with zipfile.ZipFile(f) as z:
            for i in z.infolist():
                if re.match(r"(word|ppt)/media/", i.filename) and Path(i.filename).suffix.lower() in IMG_EXTS:
                    if i.file_size >= NOTE_IMG_MIN or image_px(z.read(i.filename)[:65536]) >= NOTE_IMG_PX:
                        n += 1
    except Exception:  # noqa: BLE001
        return 0
    return n


def soffice_bin() -> str | None:
    found = shutil.which("soffice")
    if found:
        return found
    for p in ("/Applications/LibreOffice.app/Contents/MacOS/soffice", "/opt/homebrew/bin/soffice"):
        if Path(p).exists():
            return p
    return None


# Без soffice: PDF из картинок документа в порядке появления (docx — word/document.xml, pptx — слайды
# по порядку). Одна картинка = одна страница; размер страницы такой, чтобы рендер в 200 dpi (как
# в dots_batch) дал исходные пиксели. Картинки меньше порога (логотипы, подписи) пропускаются.
IMG_PDF = r'''
import io, posixpath, re, sys, zipfile
from xml.etree import ElementTree
import pymupdf
from PIL import Image, ImageOps
src, dst, min_size = sys.argv[1], sys.argv[2], int(sys.argv[3])
z = zipfile.ZipFile(src)
names = set(z.namelist())
def rels(part):
    d, b = posixpath.split(part)
    rp = posixpath.join(d, "_rels", b + ".rels")
    out = {}
    if rp in names:
        for r in ElementTree.fromstring(z.read(rp)):
            t = r.get("Target") or ""
            if r.get("TargetMode") == "External":
                continue
            out[r.get("Id")] = t.lstrip("/") if t.startswith("/") else posixpath.normpath(posixpath.join(d, t))
    return out
def embeds(part):
    xml = z.read(part).decode("utf-8", "replace")
    rl = rels(part)
    return [rl[m] for m in re.findall(r'r:(?:embed|id)="([^"]+)"', xml) if m in rl]
if "word/document.xml" in names:
    parts = ["word/document.xml"]
else:
    pr = rels("ppt/presentation.xml")
    xml = z.read("ppt/presentation.xml").decode("utf-8", "replace")
    parts = [pr[m] for m in re.findall(r'<p:sldId [^>]*r:id="([^"]+)"', xml) if m in pr]
paths = []
for part in parts:
    for t in embeds(part):
        if re.match(r"(word|ppt)/media/", t) and t.lower().rsplit(".", 1)[-1] in ("jpg", "jpeg", "png", "tif", "tiff", "bmp", "gif", "webp") \
                and t in names and z.getinfo(t).file_size >= min_size:
            paths.append(t)
doc = pymupdf.open()
for t in paths:
    data = z.read(t)
    im = Image.open(io.BytesIO(data))
    turned = im.getexif().get(0x0112, 1) not in (0, 1)       # EXIF-поворот: без него страница ляжет боком
    if turned or t.lower().rsplit(".", 1)[-1] not in ("jpg", "jpeg", "png"):
        buf = io.BytesIO()
        fixed = ImageOps.exif_transpose(im).convert("RGB")
        fixed.save(buf, "JPEG", quality=95)
        data, im = buf.getvalue(), fixed
    w, h = im.size
    page = doc.new_page(width=w * 72 / 200, height=h * 72 / 200)
    page.insert_image(page.rect, stream=data)
if len(doc) == 0:
    sys.exit(3)
doc.save(dst)
print(len(doc))
'''


def pdf_pages(pdf: Path) -> int:
    for line in sh(["pdfinfo", str(pdf)]).splitlines():
        if line.startswith("Pages:"):
            return int(line.split()[1])
    return 0


def office_pdf(orig: Path, readable: Path, pdf: Path, need_text: bool = False) -> tuple[str | None, int]:
    """PDF для OCR из Office-файла. (способ, страниц) или (None, 0).
    Готовый PDF не пересобирается, если он новее исходника (повторный запуск route.py).
    need_text: в PDF должен быть и текст документа (а не только картинки) — годится лишь soffice:
    готовый PDF неизвестного происхождения не берём, PDF из картинок не собираем."""
    pdf.parent.mkdir(parents=True, exist_ok=True)
    if not need_text and pdf.exists() and pdf.stat().st_mtime >= orig.stat().st_mtime and pdf_pages(pdf):
        return "готовый PDF", pdf_pages(pdf)
    pdf.unlink(missing_ok=True)
    so = soffice_bin()
    if so:
        with tempfile.TemporaryDirectory() as td:
            try:
                subprocess.run([so, "--headless", "--convert-to", "pdf", "--outdir", td, str(orig)],
                               capture_output=True, text=True, timeout=600)
            except Exception:
                pass
            made = Path(td) / f"{orig.stem}.pdf"
            if made.exists() and pdf_pages(made):
                shutil.move(str(made), pdf)
                return "soffice", pdf_pages(pdf)
    if not need_text and MD_PYTHON.exists() and readable.suffix.lower() in (".docx", ".pptx"):
        try:
            r = subprocess.run([str(MD_PYTHON), "-c", IMG_PDF, str(readable), str(pdf), str(SCAN_IMG_MIN)],
                               capture_output=True, text=True, timeout=300)
        except Exception:
            r = None
        if r is not None and r.returncode == 0 and pdf.exists() and pdf_pages(pdf):
            return "картинки документа", pdf_pages(pdf)
    pdf.unlink(missing_ok=True)
    return None, 0


def scan_office(orig: Path, readable: Path, md: Path, pdf: Path) -> dict | None:
    """Проверка результата markitdown для Office. `readable` — файл, который читал markitdown
    (docx/pptx; для .doc/.rtf/.odt/.ppt/.odp — результат моста), `md` — его вывод.
    Вернёт None (обычный путь), {"note": …} (оставить markitdown, но предупредить) либо
    {"row": …} — поля строки плана для OCR."""
    text_len = visible_text_len(md.read_text(errors="replace"))
    media = office_media(readable)
    big = [m for m in media if m >= SCAN_IMG_MIN]
    # Короткое письмо с картинкой подписи/печати/бланка — не скан: текст письма в OCR терять нельзя.
    # 50–300 знаков идут в OCR только через soffice (его PDF содержит и текст, и картинки) и если картинок
    # суммарно на NOTE_IMG_MIN и больше; без soffice остаётся текст markitdown с пометкой.
    mid = SCAN_TEXT_NONE <= text_len < SCAN_TEXT_MIN
    if mid and big and sum(media) >= NOTE_IMG_MIN and not soffice_bin():
        return {"note": f"внутри {len(big)} картинок, их текст не распознан (текста {text_len} симв.; "
                        f"без soffice PDF для OCR несёт только картинки и текст документа потерялся бы)"}
    if text_len < SCAN_TEXT_MIN and big and (not mid or sum(media) >= NOTE_IMG_MIN):
        method, pages = office_pdf(orig, readable, pdf, need_text=mid)
        kind = "презентация" if orig.suffix.lower() in (".pptx", ".ppt", ".odp") else "Word"
        if method:
            return {"row": {"src": str(pdf), "orig": str(orig), "engine": "ocr", "pages": pages,
                            "why": f"{kind} из сканов → OCR ({pages} с.)",
                            "note": f"текста {text_len} симв., картинок {len(big)}; PDF «{pdf.name}» ({method}) лежит рядом с .md"
                                    + ("; короткий текст документа в OCR не попадёт" if method == "картинки документа" and text_len > 20 else "")}}
        if mid:
            return {"note": f"внутри {len(big)} картинок, их текст не распознан (текста {text_len} симв.; "
                            f"soffice не собрал PDF для OCR)"}
        return {"note": f"внутри {len(big)} картинок, текста {text_len} симв. — похоже на сканы, но в PDF для OCR "
                        f"документ не конвертируется (нет soffice, картинки не собрались); их текст не распознан"}
    heavy = office_heavy_images(readable)
    if heavy:
        return {"note": f"внутри {heavy} картинок, их текст не распознан"}
    return None


def stale_scan_md(done: Path, orig: Path) -> bool:
    """Готовый .md — вывод markitdown по Office из сканов (текста нет, картинки есть), а не OCR.
    Нужен, чтобы повторный запуск без --force подхватил такой файл (старые прогоны)."""
    if orig.suffix.lower() not in SCAN_EXTS:
        return False
    try:
        text = done.read_text(errors="replace")
    except OSError:
        return False
    if OCR_FOOTER.search(text) or visible_text_len(text) >= SCAN_TEXT_MIN:
        return False
    if orig.suffix.lower() in (".docx", ".pptx"):
        return any(m >= SCAN_IMG_MIN for m in office_media(orig))
    with tempfile.TemporaryDirectory() as td:
        conv = legacy_bridge(orig, Path(td))
        return bool(conv) and any(m >= SCAN_IMG_MIN for m in office_media(conv))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("target")
    ap.add_argument("--out", required=True)
    ap.add_argument("--existing-out", help="папка выданных Markdown: только чтение, пропустить готовые")
    ap.add_argument("--json", default=None)
    ap.add_argument("--force", action="store_true", help="переделать уже готовые .md")
    ap.add_argument("--exclude", action="append", default=[], metavar="ПОДСТРОКА",
                    help="пропустить файлы, чей относительный путь содержит подстроку "
                         "(повторяемый). Для содержательных дублей, которые побайтовый "
                         "хеш не ловит: несжатые копии, вырезки из того же PDF")
    a = ap.parse_args()

    target, out_root = Path(a.target).resolve(), Path(a.out).resolve()
    published_root = Path(a.existing_out).expanduser().resolve() if a.existing_out else None
    if not MARKITDOWN.exists():
        print(f"НЕТ markitdown: {MARKITDOWN}", file=sys.stderr)
        return 2

    if target.is_file():
        files, base = [target], target.parent
    else:
        files = [p for p in sorted(target.rglob("*"))
                 if p.is_file() and out_root not in p.parents
                 and (published_root is None or published_root not in p.parents)
                 and unicodedata.normalize("NFC", p.name) not in SKIP_NAMES and not p.name.startswith("~$")
                 and not is_artifact(p.relative_to(target))]
        base = target

    plan: list[dict] = []
    media: list[str] = []

    # Один и тот же файл под разными именами («…(1).pdf», копия в соседней
    # папке) — не редкость в выгрузках Росреестра и ЕПГУ. Гнать его через
    # marker дважды значит потратить минуты вдвойне и получить ДВА РАЗНЫХ
    # результата: OCR недетерминирован, на реальном деле два прогона одной
    # выписки разошлись в номере сертификата ЭЦП на одну цифру.
    # Считаем хеш и оставляем один экземпляр — первый по алфавиту.
    # Хранителем считаем файл с самым «чистым» именем: без хвоста «(1)», а при
    # прочих равных — короче и выше по алфавиту. Иначе оригинал «ЧЕРТЕЖ.pdf»
    # объявляется дублем своей же копии «ЧЕРТЕЖ (1).pdf».
    copy_tail = re.compile(r"[ _-]?\(\d+\)$")

    def name_rank(p: Path) -> tuple[int, int, str]:
        return (1 if copy_tail.search(p.stem) else 0, len(p.stem), str(p))

    by_digest: dict[str, list[Path]] = {}
    for f in files:
        if f.suffix.lower() in MEDIA:
            continue
        try:
            h = hashlib.blake2b(f.read_bytes(), digest_size=16).hexdigest()
        except OSError:
            continue
        by_digest.setdefault(h, []).append(f)

    dup_of: dict[Path, Path] = {}
    for group in by_digest.values():
        if len(group) < 2:
            continue
        keeper, *rest = sorted(group, key=name_rank)
        for other in rest:
            dup_of[other] = keeper

    # Два исходника с одним именем, но разными расширениями («иск.pdf» и
    # «иск.docx») дали бы один путь вывода и затёрли друг друга. Такие
    # различаем расширением в имени папки.
    seen: dict[tuple[str, str], int] = {}
    for f in files:
        if f.suffix.lower() in MEDIA:
            continue
        key = (str(f.relative_to(base).parent), f.stem.lower())
        seen[key] = seen.get(key, 0) + 1
    clashing = {k for k, v in seen.items() if v > 1}

    for f in files:
        ext = f.suffix.lower()
        rel = f.relative_to(base)
        stem = f.stem
        if (str(rel.parent), stem.lower()) in clashing:
            stem = f"{stem} ({ext.lstrip('.')})"
        out_dir = out_root / rel.parent / stem
        dst = out_dir / f"{stem}.md"
        row = {"src": str(f), "rel": str(rel), "out": str(dst), "ext": ext}

        if ext in MEDIA:
            media.append(str(rel))
            continue
        if ext == ".md" and not a.force:
            continue
        # Имена на APFS лежат в NFD, а подстрока из командной строки — в NFC:
        # «й» и «ё» без нормализации молча не совпадают.
        rel_nfc = unicodedata.normalize("NFC", str(rel))
        hit = next((x for x in a.exclude if unicodedata.normalize("NFC", x) in rel_nfc), None)
        if hit:
            plan.append({**row, "engine": "пропуск", "why": f"исключён по --exclude «{hit}»"})
            continue
        if f in dup_of:
            twin = dup_of[f].relative_to(base)
            plan.append({**row, "engine": "дубль", "why": f"побайтово равен «{twin}»",
                         "same_as": str(twin)})
            continue
        if ext in SKIP_NAMES or ext not in (NATIVE | RASTER | HEIC | set(LEGACY) | {".pdf"}):
            # Раньше любой неизвестный формат (архив, фото HEIC) пропадал молча — даже не попадал в план.
            if ext and ext not in QUIET_EXT and not f.name.startswith((".", "~$")):
                plan.append({**row, "engine": "пропуск",
                             "why": f"формат {ext} не поддерживается — не конвертирован (распаковать или пересохранить)"})
            continue
        published = find_md(published_root / rel.parent / stem, stem) if published_root else None
        if published and not a.force:
            plan.append({**row, "engine": "готово", "published_out": str(published),
                         "why": "уже есть итоговый Markdown; повторная обработка не нужна"})
            continue
        done = done_md(dst)
        if done and not a.force and not (done == dst and stale_scan_md(done, f)):
            heavy = office_heavy_images(f) if ext in (".docx", ".pptx") and done == dst \
                and not OCR_FOOTER.search(done.read_text(errors="replace")) else 0
            plan.append({**row, "engine": "готово",
                         "why": "уже сконвертирован" + (" (помечен ПЛОХОЕ КАЧЕСТВО)" if done != dst and BAD_SUFFIX in done.name else ""),
                         **({"note": f"внутри {heavy} картинок, их текст не распознан"} if heavy else {})})
            continue
        if a.force:
            drop_old_bad(dst)
        pdf_copy = out_dir / f"{stem} (из {ext.lstrip('.')}).pdf"

        if ext in HEIC:
            jpg = out_dir / f"{stem} (из {ext.lstrip('.')}).jpg"
            out_dir.mkdir(parents=True, exist_ok=True)
            if not jpg.exists() or jpg.stat().st_mtime < f.stat().st_mtime:
                ok = subprocess.run(["sips", "-s", "format", "jpeg", str(f), "--out", str(jpg)],
                                    capture_output=True).returncode == 0
                if not ok:
                    jpg.unlink(missing_ok=True)
            if jpg.exists():
                plan.append({**row, "src": str(jpg), "orig": str(f), "engine": "ocr", "pages": 1,
                             "why": "фото HEIC (iPhone) — JPEG-копия для OCR, исходник не меняется"})
            else:
                plan.append({**row, "engine": "ОШИБКА", "why": "фото HEIC не удалось перевести в JPEG (sips)"})
        elif ext in RASTER:
            n = tiff_pages(f) if ext in TIFF_EXTS else 1
            plan.append({**row, "engine": "ocr", "pages": n,
                         "why": "растр, нужен OCR" + (f" (многостраничный TIFF, {n} стр.)" if n > 1 else "")})
        elif ext in LEGACY:
            with tempfile.TemporaryDirectory() as td:
                conv = legacy_bridge(f, Path(td))
                if conv and run_markitdown(conv, dst):
                    sc = scan_office(f, conv, dst, pdf_copy) if ext in SCAN_EXTS else None
                    if sc and sc.get("row"):
                        plan.append({**row, **sc["row"]})
                        continue
                    n, med, seps = md_quality(dst)
                    extra, csvs = "", []
                    if ext in SPREADSHEET:
                        csvs = xlsx_to_csv(conv, dst.parent)
                        extra = f", CSV листов {len(csvs)}" if csvs else ", CSV не вышел"
                    plan.append({**row, "engine": "markitdown",
                                 "why": f"{ext}→{LEGACY[ext]}, таблиц {seps}{extra}",
                                 "lines": n, **({"csv": csvs} if csvs else {}),
                                 **({"note": sc["note"]} if sc and sc.get("note") else {})})
                else:
                    plan.append({**row, "engine": "ОШИБКА",
                                 "why": f"{ext}: ни soffice, ни textutil не открыли файл"})
        elif ext in NATIVE:
            ok = run_markitdown(f, dst)
            n, med, seps = md_quality(dst)
            if not ok or n < 1:
                # (Office из сканов — другое дело: см. scan_office, там уходит PDF-копия.)
                # В marker office НЕ отдаём ни при каких условиях: там текст лежит
                # готовым в разметке, а marker вместо чтения рендерит страницы и
                # распознаёт их заново — это и медленно, и открывает дорогу ошибкам
                # OCR там, где ошибиться было негде. Проверено на реальном деле: договор
                # .docx на 92 страницы ушёл в marker, отработал минуты, и результат
                # всё равно заменили выводом markitdown.
                plan.append({**row, "engine": "ОШИБКА",
                             "why": f"markitdown не осилил {ext}; marker тут не замена — разобрать вручную"})
                dst.unlink(missing_ok=True)
            else:
                sc = scan_office(f, f, dst, pdf_copy) if ext in SCAN_EXTS else None
                if sc and sc.get("row"):
                    plan.append({**row, **sc["row"]})
                    continue
                extra, csvs = "", []
                if ext in SPREADSHEET:
                    csvs = xlsx_to_csv(f, dst.parent)
                    extra = f", CSV листов {len(csvs)}" if csvs else ", CSV не вышел"
                plan.append({**row, "engine": "markitdown",
                             "why": f"нативная разметка, таблиц {seps}{extra}",
                             "lines": n, **({"csv": csvs} if csvs else {}),
                             **({"note": sc["note"]} if sc and sc.get("note") else {})})
        else:  # .pdf
            pages, fonts, chars = pdf_probe(f)
            per_page = chars / max(1, min(pages or 1, 5))
            # Проверка кодировки идёт ПЕРВОЙ. Чертёж с битым шрифтом даёт мало
            # символов на страницу и иначе был бы записан в «сканы» — тоже в
            # marker, но без force_ocr, а значит marker снова прочитал бы битый
            # текстовый слой и снова выдал кракозябры.
            if pdf_has_broken_font(f):
                cyr = readable_cyrillic_per_page(f)
                if cyr < MIN_READABLE_CYRILLIC:
                    plan.append({**row, "engine": "ocr", "force_ocr": True, "pages": pages,
                                 "why": f"шрифт без ToUnicode, читаемого текста нет "
                                        f"({cyr} букв/стр.) — принудительный OCR ({pages} с.)"})
                    continue
                # Битый шрифт есть, но им набран только заголовок: основной текст
                # читается. Идём обычным путём — force_ocr тут отнял бы половину
                # содержимого. Остаточные кракозябры отметит постпроверка.
                row = {**row, "note": f"битый шрифт в части текста; читаемого {cyr} букв/стр."}
            if fonts == 0 or per_page < MIN_CHARS_PER_PAGE:
                plan.append({**row, "engine": "ocr", "pages": pages,
                             "why": f"скан, текстового слоя нет ({pages} с.)"})
                continue
            # Слой есть, но это чужой плохой OCR: в OCR заново (см. GARBAGE_MIXED_PER_1000).
            garbage = text_layer_garbage(f)
            if garbage["mixed_per_1000"] >= GARBAGE_MIXED_PER_1000:
                plan.append({**row, "engine": "ocr", "pages": pages,
                             "why": f"вшитый OCR-слой негоден ({garbage['mixed_per_1000']:.0f}‰ слов "
                                    f"со смесью алфавитов) — распознать заново ({pages} с.)"})
                continue
            if garbage["mixed_per_1000"] >= GARBAGE_NOTE_PER_1000:
                row = {**row, "note": (row.get("note", "") + "; " if row.get("note") else "")
                       + f"слой с шумом ({garbage['mixed_per_1000']:.0f}‰ слов со смесью алфавитов), для точных цитат — в OCR"}
            # Страницы, лежащие боком или вверх ногами: слой цел, но читается в неверном
            # порядке. Проверяем ориентацию по Vision и читаем повёрнутую копию
            # (исходник не трогаем). Так на реальном деле 14.10.2026 спасли 6 выписок.
            src_for_fast = f
            orient = pdf_orientation(f)
            if orient and orient.get("turned", 0) >= max(1, orient.get("checked", 1) // 2):
                fixed = Path(tempfile.mkdtemp(prefix="convert-rot-")) / f.name
                res = pdf_orientation(f, fix=fixed)
                if res and res.get("fixed") and fixed.exists():
                    src_for_fast = fixed
                    row = {**row, "rotated": res.get("rot", {}),
                           "note": (row.get("note", "") + "; " if row.get("note") else "")
                           + f"страницы лежали боком, прочитана повёрнутая копия ({res.get('turned')} из {res.get('pages')})"}
            # Текстовый слой цел — значит marker тут не нужен. Он умеет ровно
            # одно, чего не умеют остальные: читать пиксели. Когда буквы уже
            # лежат в файле, его пересборка макета только вредит — замеры на
            # выписке ЕГРН (потерял кадастровую стоимость) и на проекте
            # реконструкции (потерял 52 числа и две трети текста) это показали.
            #
            # Выбор внутри быстрой ветки — по наличию НАРИСОВАННОЙ сетки:
            #   есть линии таблицы  → pdfplumber, он режет по ним и не путает
            #                         поле со значением (выписки, бланки, формы);
            #   нет                 → pymupdf4llm, он лучше держит порядок
            #                         чтения и колонки в сплошном тексте.
            # Оба движка быстрой ветки работают секунды, поэтому выбирать
            # эвристикой незачем — гоняем оба и берём тот, что сохранил больше
            # чисел из текстового слоя. Гадание тут уже подводило: на проекте
            # реконструкции сетка насчиталась в 84 строки, pdfplumber выглядел
            # очевидным выбором и потерял 41 число против 24 у pymupdf4llm.
            eng, n, why = pick_fast_engine(src_for_fast, dst, pages)
            if eng is None:
                plan.append({**row, "engine": "ocr", "pages": pages,
                             "why": f"быстрая ветка не справилась — в OCR ({pages} с.)"})
                dst.unlink(missing_ok=True)
            else:
                scans = scan_pages(f, pages)
                if scans and len(scans) < pages:
                    short = ", ".join(map(str, scans[:12])) + (" …" if len(scans) > 12 else "")
                    plan.append({**row, "engine": eng, "why": why, "lines": n, "scan_pages": scans,
                                 "note": (row.get("note", "") + "; " if row.get("note") else "")
                                 + f"стр. {short} — сканы без текста, идут в OCR"})
                    plan.append({**row, "engine": "ocr", "pages": len(scans), "only_pages": scans,
                                 "why": f"сканы внутри PDF с текстом: стр. {short} — OCR этих страниц "
                                        "и вставка на их место в .md"})
                else:
                    plan.append({**row, "engine": eng, "why": why, "lines": n})
            continue

    w = max([len(p["rel"]) for p in plan] + [12])
    print(f"{'#':>3}  {'файл':<{w}}  {'движок':<11}  причина")
    for i, p in enumerate(plan, 1):
        print(f"{i:>3}  {p['rel']:<{w}}  {p['engine']:<11}  {p['why']}" + (f"  [{p['note']}]" if p.get('note') else ""))

    by = {}
    for p in plan:
        by[p["engine"]] = by.get(p["engine"], 0) + 1
    print("\nИтого: " + ", ".join(f"{k} — {v}" for k, v in sorted(by.items())))
    if media:
        print("Не конвертируется (медиа): " + ", ".join(media))
    ocr_pages = sum(p.get("pages", 1) for p in plan if p["engine"] == "ocr")
    if ocr_pages:
        # dots.mocr на M4 Pro: ~30–45 с на A4-скан, ~55–60 с на фото-страницу; петли теперь обрываются рано
        print(f"OCR: {ocr_pages} стр., dots.mocr займёт ориентировочно "
              f"{ocr_pages // 2}–{ocr_pages * 4 // 3} мин.")

    if a.json:
        Path(a.json).write_text(
            json.dumps({"plan": plan, "media": media}, ensure_ascii=False, indent=2)
        )
        print(f"План: {a.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

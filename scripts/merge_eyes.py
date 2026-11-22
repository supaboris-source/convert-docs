#!/usr/bin/env python3
"""Вставить расшифровки «глазами» в основной .md и удалить отдельные файлы.

    python3 merge_eyes.py "<корень _md>" [--keep]

Для каждого `<папка>/_ЧТЕНИЕ-<имя>-стр-N.md` раздел `## Страница N` в `<папка>/<имя>.md`
заменяется расшифровкой (OCR-текст страницы и его пометки уходят), сверху пометка
«✎ Прочитано глазами». В `<имя>_ocr.json` у страницы ставится `"eyes": "<дата>"` —
postcheck.py такие страницы больше не выносит в «прочитать глазами» и сверку.
Файл `_ЧТЕНИЕ-…` после вставки удаляется (`--keep` — оставить).
Файлы `_ЧТЕНИЕ-*` без номера страницы в имени не трогаются и перечисляются в конце.
Основной файл ищется и под именем `<имя> (ПЛОХОЕ КАЧЕСТВО).md` (так его переименовывает finalize.py).
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import BAD_SUFFIX, find_md

EYE_RE = re.compile(r"^_ЧТЕНИЕ-(?P<stem>.+)-стр-(?P<page>\d+)\.md$")
PAGE_RE = re.compile(r"(?m)^## Страница (\d+)\s*$")
TRAILER_RE = re.compile(r"(?m)^---\nРаспознано:")
MARK = ("> ✎ Прочитано глазами по изображению вместо OCR ({date}). `слово[?]` — прочитано неуверенно, "
        "`[неразборчиво]` — не разобрать, `[…]` — за краем кадра. Фамилии, даты и номера сверять с оригиналом.")


def parse_eye(text: str) -> tuple[str, str]:
    """(примечание чтеца, тело). Первая строка «# Чтение глазами…» отбрасывается,
    вводный абзац в скобках становится примечанием без фраз про OCR и .md."""
    lines = text.strip("\n").splitlines()
    if lines and lines[0].startswith("# Чтение глазами"):
        lines = lines[1:]
    while lines and not lines[0].strip():
        lines = lines[1:]
    note = ""
    if lines and lines[0].lstrip().startswith("("):
        para = []
        while lines and lines[0].strip():
            para.append(lines.pop(0))
        note = " ".join(x.strip() for x in para).strip()
        if note.startswith("(") and note.endswith(")"):
            note = note[1:-1]
        sents = re.split(r"(?<=[.!?])\s+", note)
        note = " ".join(s for s in sents if "OCR" not in s and ".md" not in s).strip()
    body = []
    for ln in lines:
        if PAGE_RE.match(ln):
            ln = "#" + ln
        elif ln.startswith("# "):
            ln = "#" + ln
        body.append(ln)
    return note, "\n".join(body).strip("\n")


def replace_page(md: str, page: int, block: str) -> str | None:
    heads = list(PAGE_RE.finditer(md))
    if not heads and page == 1:
        # Одностраничный документ (фото .jpg): разделов «## Страница N» нет, текст идёт
        # сразу под заголовком «# имя.jpg» и до хвоста «---\nРаспознано:».
        title = re.match(r"# [^\n]*\n", md)
        start = title.end() if title else 0
        t = TRAILER_RE.search(md, start)
        end = t.start() if t else len(md)
        return md[:start] + "\n" + block.rstrip("\n") + "\n\n" + md[end:]
    for i, m in enumerate(heads):
        if int(m.group(1)) != page:
            continue
        end = heads[i + 1].start() if i + 1 < len(heads) else None
        if end is None:
            t = TRAILER_RE.search(md, m.end())
            end = t.start() if t else len(md)
        return md[:m.start()] + block.rstrip("\n") + "\n\n" + md[end:]
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("root")
    ap.add_argument("--keep", action="store_true", help="не удалять _ЧТЕНИЕ-файлы после вставки")
    a = ap.parse_args()
    root = Path(a.root).expanduser()
    today = dt.date.today().strftime("%d.%m.%Y")
    done, skipped = [], []
    for eye in sorted(root.rglob("_ЧТЕНИЕ-*.md")):
        m = EYE_RE.match(eye.name)
        md_path = find_md(eye.parent, m.group("stem")) if m else None
        if not m or md_path is None:
            skipped.append(f"{eye.relative_to(root)}: нет номера страницы или основного .md")
            continue
        page = int(m.group("page"))
        note, body = parse_eye(eye.read_text(encoding="utf-8"))
        block = f"## Страница {page}\n\n" + MARK.format(date=today) + (f"\n> Примечание: {note}" if note else "") \
                + "\n\n" + body + "\n"
        md = md_path.read_text(encoding="utf-8")
        new = replace_page(md, page, block)
        if new is None:
            skipped.append(f"{eye.relative_to(root)}: в .md нет раздела «## Страница {page}»")
            continue
        md_path.write_text(new, encoding="utf-8")
        js = eye.parent / f"{m.group('stem').removesuffix(BAD_SUFFIX)}_ocr.json"
        if js.exists():
            d = json.loads(js.read_text(encoding="utf-8"))
            for ps in d.get("page_stats", []):
                if ps.get("page") == page:
                    ps["eyes"] = today
            js.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
        if not a.keep:
            eye.unlink()
        done.append(f"{md_path.relative_to(root)}: стр. {page}")
    print(f"вставлено страниц: {len(done)}")
    for x in done:
        print("  " + x)
    if skipped:
        print(f"не вставлено: {len(skipped)}")
        for x in skipped:
            print("  " + x)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

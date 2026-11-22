#!/usr/bin/env python3
"""Последний шаг ручной конвертации без workspace.py: пометить документы плохого качества и убрать
служебные `_ocr.json`. В обычном порядке работы (SKILL.md) его не вызывают: `workspace.py publish`
сам помечает плохое качество и выдаёт только `.md`, а служебные файлы остаются в папке запуска.
Отсюда workspace.py берёт `insert_mark`.

    python3 finalize.py "<корень _md>" [--keep-json] [--dry-run]

Порядок шагов: OCR → чтение глазами → `merge_eyes.py` → `postcheck.py` → `finalize.py`.

Что делает:
  1. Для каждого документа, у которого postcheck записал в `<имя>_ocr.json` поле
     `quality: {"bad": true, "why": …}`, переименовывает `<имя>.md` в `<имя> (ПЛОХОЕ КАЧЕСТВО).md` и вставляет
     под заголовком строку
         > ⚠ ПЛОХОЕ КАЧЕСТВО ИСХОДНИКА: <почему>. Текст изучен не полностью: фамилии, цифры и даты сверять с оригиналом.
     В `_ПРОВЕРИТЬ.md` имя файла заменяется на новое. Повторный запуск ничего не удваивает.
  2. Удаляет все `*_ocr.json` (нужен только `.md` на документ и `_ПРОВЕРИТЬ.md`) — но только если
     `_ПРОВЕРИТЬ.md` записан после последнего изменения этих json. Если после postcheck что-то правилось
     (например, запускался `merge_eyes.py`), отказ приходит ДО любых записей (ничего не переименовано и не
     удалено): сначала заново `postcheck.py`.
     Если рядом с `<имя>.md` лежит старый `<имя> (ПЛОХОЕ КАЧЕСТВО).md` (остался после `route.py --force`),
     актуален `<имя>.md`: он переименовывается поверх старого.
     `--keep-json` — json оставить. Файлы `<имя> (из docx).pdf` (скан-копия Office-документа) не удаляются: они нужны,
     пока страницы не прочитаны глазами.
После удаления `postcheck.py` по этой папке уже не пересобрать.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import BAD_SUFFIX, md_for_json
MARK_KEY = "ПЛОХОЕ КАЧЕСТВО ИСХОДНИКА"
MARK = "> ⚠ ПЛОХОЕ КАЧЕСТВО ИСХОДНИКА: {why}. Текст изучен не полностью: фамилии, цифры и даты сверять с оригиналом."


def insert_mark(text: str, why: str) -> str:
    """Строка-предупреждение под первым заголовком `# …` (или в самом начале файла)."""
    if MARK_KEY in text:
        return text
    line = MARK.format(why=why.rstrip(". "))
    m = re.match(r"\A(\s*# [^\n]*\n)", text)
    if m:
        return m.group(1) + "\n" + line + "\n\n" + text[m.end():].lstrip("\n")
    return line + "\n\n" + text


def replace_path(text: str, old_rel: str, new_rel: str) -> str:
    """Заменить в тексте путь `old_rel` на `new_rel` — только целым путём: слева начало строки, пробел или
    обратная кавычка (не «/» и не буква: «Старое Дело/Договор/Договор.md» — другой файл), справа не буква/цифра и не «.расширение»."""
    return re.sub(r"(?<![^\s`])" + re.escape(old_rel) + r"(?!\w|\.\w)", lambda _m: new_rel, text)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("root")
    ap.add_argument("--keep-json", action="store_true", help="не удалять *_ocr.json")
    ap.add_argument("--dry-run", action="store_true", help="только показать, что будет сделано")
    a = ap.parse_args()
    root = Path(a.root).expanduser().resolve()
    if not root.exists():
        print(f"нет пути: {root}", file=sys.stderr)
        return 2

    jsons = sorted(root.rglob("*_ocr.json"))
    check = root / "_ПРОВЕРИТЬ.md"
    check_text = check.read_text(encoding="utf-8") if check.exists() else ""
    deleting = bool(jsons) and not (a.keep_json or a.dry_run)
    # Свежесть списка проверяем ДО любых записей: ниже finalize сам переписывает _ПРОВЕРИТЬ.md
    # (новые имена файлов), и его время изменения уже ничего не доказывало бы.
    if deleting:
        if not check.exists():
            print("ничего не сделано: нет _ПРОВЕРИТЬ.md — сначала postcheck.py")
            return 1
        newest_json = max(js.stat().st_mtime for js in jsons)
        if check.stat().st_mtime + 1 < newest_json:
            print("ничего не сделано (ни переименований, ни удаления): _ПРОВЕРИТЬ.md старше json — "
                  "после postcheck.py они менялись (merge_eyes.py?); сначала заново postcheck.py")
            return 1
    renamed: list[tuple[str, str]] = []
    marked = 0

    for js in jsons:
        try:
            d = json.loads(js.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        q = d.get("quality") or {}
        if not q.get("bad"):
            continue
        md = md_for_json(js)
        if md is None:
            print(f"  нет .md для {js.name}: пропуск")
            continue
        text = md.read_text(encoding="utf-8")
        new_text = insert_mark(text, q.get("why") or "исходник плохого качества")
        target = md
        replace_old = False
        if not md.stem.endswith(BAD_SUFFIX):
            target = md.with_name(f"{md.stem}{BAD_SUFFIX}.md")
            # рядом лежит «(ПЛОХОЕ КАЧЕСТВО).md» от прошлого прогона (после route.py --force):
            # актуален <имя>.md, он и переименовывается, заменяя старый
            replace_old = target.exists()
        if a.dry_run:
            print(f"  [проба] {md.relative_to(root)} → {target.name}"
                  + (" (заменит старый файл с этим именем)" if replace_old else "")
                  + ("" if new_text != text else " (предупреждение уже вставлено)"))
            continue
        if new_text != text:
            md.write_text(new_text, encoding="utf-8")
            marked += 1
        if target != md:
            old_rel, new_rel = str(md.relative_to(root)), str(target.relative_to(root))
            if replace_old:
                print(f"  {target.name} уже был — заменён актуальным {md.name}")
            md.replace(target)
            renamed.append((old_rel, new_rel))
            check_text = replace_path(check_text, old_rel, new_rel)

    if renamed and check.exists() and not a.dry_run:
        check.write_text(check_text, encoding="utf-8")
    print(f"помечено документов плохого качества: {marked}, переименовано: {len(renamed)}")
    for old, new in renamed:
        print(f"  {old} → {Path(new).name}")

    if a.keep_json or a.dry_run:
        print("json оставлены" + (" (--keep-json)" if a.keep_json else " (проба)"))
        return 0
    if not jsons:
        print("служебных json нет")
        return 0
    for js in jsons:
        js.unlink()
    print(f"удалено служебных json: {len(jsons)}")
    pdfs = sorted(root.rglob("* (из docx).pdf")) + sorted(root.rglob("* (из pptx).pdf"))
    if pdfs:
        print(f"оставлены PDF-копии Office-сканов ({len(pdfs)}): после чтения глазами всех страниц их можно удалить")
    return 0


if __name__ == "__main__":
    sys.exit(main())

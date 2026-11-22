#!/usr/bin/env python3
"""Проверка сложного набора (tests/make_hard_set.py) после прогона скилла.

    python3 tests/check_hard_set.py <папка теста> <папка запуска> <папка выдачи> [--plan-only]

<папка теста> — там, где expected.json и «набор/». Проверяется:
  * план: движки по каждому файлу, страницы-сканы (`only_pages`), причины и пометки, дубль, медиа, пропуск;
  * выдача (без --plan-only): на каждой странице все контрольные значения (шифр, сумма, дата, счёт,
    фамилия, три числа таблицы); число разделов страниц; пустые страницы названы в _ПРОВЕРИТЬ.md;
    снимок без текста не отдан в OCR; в выдаче нет служебных файлов.
Сравнение без пробелов, «ё» = «е». Код выхода 0 — всё совпало, 1 — есть расхождения.
"""
from __future__ import annotations

import json
import re
import sys
import unicodedata
from pathlib import Path

PAGE_RE = re.compile(r"(?m)^## Страница (\d+)\s*$")


def nfc(s: str) -> str:
    """Имена на APFS хранятся в NFD («й» распадается на «и» + знак): сравнивать только в NFC."""
    return unicodedata.normalize("NFC", s)


def squash(s: str) -> str:
    return re.sub(r"[\s  ]+", "", s).replace("ё", "е").replace("Ё", "Е")


def sections(md: str) -> dict[int, str]:
    heads = list(PAGE_RE.finditer(md))
    if not heads:
        return {1: md}
    return {int(h.group(1)): md[h.end(): heads[i + 1].start() if i + 1 < len(heads) else len(md)]
            for i, h in enumerate(heads)}


def main() -> int:
    test, run, out = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3])
    plan_only = "--plan-only" in sys.argv
    exp = json.loads((test / "expected.json").read_text(encoding="utf-8"))
    data = json.loads((run / "plan.json").read_text(encoding="utf-8"))
    rows = [{**r, "rel": nfc(r.get("rel", ""))} for r in data.get("plan", [])]
    media = [nfc(str(m)) for m in data.get("media", [])]
    work = run / "documents"
    check = (work / "_ПРОВЕРИТЬ.md").read_text(encoding="utf-8") if (work / "_ПРОВЕРИТЬ.md").exists() else ""
    results, ok_all = [], True

    def res(name: str, ok: bool, note: str = "") -> None:
        nonlocal ok_all
        ok_all &= ok
        results.append({"проверка": name, "ok": ok, "note": note})

    total_c = found_c = 0
    for rel, e in exp.items():
        rel = nfc(rel)
        mine = [r for r in rows if r.get("rel") == rel]
        engines = [r["engine"] for r in mine]
        want = e["engines"]
        if want == ["медиа"]:
            res(f"{rel}: назван как медиа", rel in media, f"медиа в плане: {media}")
            continue
        okeng = len(engines) == len(want) and all(w == g or g in w.split("|") for w, g in zip(want, engines))
        res(f"{rel}: маршрут {' + '.join(want)}", okeng, f"в плане: {engines}")
        if "dup_pair" in e:
            pair = [r["engine"] for r in rows if r.get("rel") == nfc(e["dup_pair"])]
            res(f"{rel}: из пары с «{e['dup_pair']}» ровно один дубль",
                (engines == ["дубль"]) != (pair == ["дубль"]), f"здесь {engines}, там {pair}")
            if engines == ["дубль"]:
                continue
        if "only_pages" in e:
            got = next((r.get("only_pages") for r in mine if r.get("only_pages")), None)
            res(f"{rel}: в OCR только стр. {e['only_pages']}", got == e["only_pages"], f"в плане: {got}")
        if "why" in e:
            res(f"{rel}: причина «{e['why']}»", any(e["why"] in (r.get("why") or "") for r in mine),
                "; ".join(r.get("why", "") for r in mine)[:150])
        if "note" in e:
            res(f"{rel}: пометка «{e['note']}…»", any(e["note"] in (r.get("note") or "") for r in mine),
                "; ".join(r.get("note", "") for r in mine)[:150])
        if plan_only or "pages" not in e and not e.get("no_text"):
            continue
        stem = Path(rel).stem
        mds = [p for p in out.rglob("*.md") if nfc(p.stem).startswith(stem)]
        md = next((p for p in mds if nfc(p.stem) == stem), mds[0] if mds else None)
        if e.get("no_text"):
            js = next((j for j in work.rglob("*_ocr.json") if nfc(j.name) == f"{stem}_ocr.json"), None)
            modes = [p.get("mode") for p in json.loads(js.read_text(encoding="utf-8"))["page_stats"]] if js else []
            res(f"{rel}: снимок без текста не отдан в OCR", modes == ["empty"], f"режимы: {modes}")
            continue
        if md is None:
            res(f"{rel}: есть итоговый .md", False, "нет в выдаче")
            continue
        text = md.read_text(encoding="utf-8")
        secs = sections(text)
        if "sections" in e:
            res(f"{rel}: разделов страниц {e['sections']}", len(secs) == e["sections"], f"есть: {len(secs)}")
        for page, ctrl in e["pages"].items():
            body = squash(secs.get(int(page), "") if len(secs) > 1 else text)
            miss = [c for c in ctrl if squash(c) not in body]
            total_c += len(ctrl)
            found_c += len(ctrl) - len(miss)
            res(f"{rel}: стр. {page} — контрольные значения", not miss,
                f"{len(ctrl) - len(miss)}/{len(ctrl)}" + (f", нет: {', '.join(miss)}" if miss else ""))
        if "empty_pages_flag" in e:
            pages = ", ".join(map(str, e["empty_pages_flag"]))
            res(f"{rel}: пустой лист назван в _ПРОВЕРИТЬ.md", f"ПУСТЫЕ СТРАНИЦЫ — стр. {pages}" in check)
    if not plan_only:
        leaked = [p.name for p in out.rglob("*") if p.is_file()
                  and (p.name.endswith("_ocr.json") or p.name.startswith("_") or p.suffix in (".csv", ".json", ".pdf", ".jpg"))]
        res("в выдаче нет служебных файлов", not leaked, ", ".join(leaked[:6]))
    for r in results:
        print(f"{'OK  ' if r['ok'] else 'FAIL'} {r['проверка']}" + (f" — {r['note']}" if r["note"] else ""))
    summary = {"проверок": len(results), "пройдено": sum(r["ok"] for r in results),
               "контрольных_значений": total_c, "найдено": found_c}
    print(f"\nИТОГ: {summary['пройдено']} из {summary['проверок']} проверок"
          + (f"; контрольных значений найдено {found_c} из {total_c}" if total_c else ""))
    (test / ("проверка-плана.json" if plan_only else "проверка-выдачи.json")).write_text(
        json.dumps({"итог": summary, "проверки": results}, ensure_ascii=False, indent=1), encoding="utf-8")
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(main())

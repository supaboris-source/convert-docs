"""Общее для скриптов скилла: пометка «плохое качество» в имени `.md`, поиск готового `.md`,
число страниц TIFF и признак нераспознанной страницы.

Импортируют postcheck.py, finalize.py, merge_eyes.py, route.py, dots_batch.py, workspace.py. Скрипты запускаются по пути
(`python3 scripts/x.py`, `~/ocr-env2/bin/python -I scripts/x.py`), поэтому перед импортом каждый добавляет
свою папку в sys.path: `sys.path.insert(0, str(Path(__file__).resolve().parent))`.
"""
from __future__ import annotations

from pathlib import Path

# finalize.py переименовывает `<имя>.md` плохого качества в `<имя> (ПЛОХОЕ КАЧЕСТВО).md`
BAD_SUFFIX = " (ПЛОХОЕ КАЧЕСТВО)"


def find_md(folder: Path, stem: str) -> Path | None:
    """`<имя>.md` либо `<имя> (ПЛОХОЕ КАЧЕСТВО).md` в папке. Если есть оба (после `route.py --force`
    рядом с новым остался старый переименованный) — актуален `<имя>.md`."""
    stem = stem.removesuffix(BAD_SUFFIX)
    for name in (f"{stem}.md", f"{stem}{BAD_SUFFIX}.md"):
        p = folder / name
        if p.exists():
            return p
    return None


def md_for_json(js: Path) -> Path | None:
    """`.md` документа по имени его `_ocr.json` (в том числе переименованный finalize.py)."""
    return find_md(js.parent, js.name[: -len("_ocr.json")])


TIFF_EXTS = {".tif", ".tiff"}


def tiff_pages(path: Path) -> int:
    """Число страниц (кадров верхнего уровня) многостраничного TIFF без PIL: route.py работает
    системным python3, где PIL нет. Обходит цепочку IFD, как PIL `n_frames`. Не TIFF или битый
    файл — 1 (дальше движок сам скажет, если картинка не открывается)."""
    import struct
    try:
        with open(path, "rb") as f:
            head = f.read(16)
            if head[:2] == b"II":
                e = "<"
            elif head[:2] == b"MM":
                e = ">"
            else:
                return 1
            magic = struct.unpack(e + "H", head[2:4])[0]
            if magic == 42:
                big, off = False, struct.unpack(e + "I", head[4:8])[0]
            elif magic == 43 and len(head) == 16:        # BigTIFF
                big, off = True, struct.unpack(e + "Q", head[8:16])[0]
            else:
                return 1
            n, seen = 0, set()
            while off and off not in seen and n < 100000:
                seen.add(off)
                f.seek(off)
                if big:
                    raw = f.read(8)
                    if len(raw) < 8:
                        break
                    f.seek(struct.unpack(e + "Q", raw)[0] * 20, 1)
                    nxt = f.read(8)
                    off = struct.unpack(e + "Q", nxt)[0] if len(nxt) == 8 else 0
                else:
                    raw = f.read(2)
                    if len(raw) < 2:
                        break
                    f.seek(struct.unpack(e + "H", raw)[0] * 12, 1)
                    nxt = f.read(4)
                    off = struct.unpack(e + "I", nxt)[0] if len(nxt) == 4 else 0
                n += 1
            return max(n, 1)
    except OSError:
        return 1


def unread_pages(pages: list[dict], skip_eyes: bool = False) -> list[int]:
    """Страницы без текста из-за сбоя: таймаут или ошибка движка, и резервное чтение Apple Vision
    их не спасло. Такой результат нельзя считать готовым: раньше пачка, где все страницы упали
    по таймауту, завершалась кодом 0 и словами «готово, все». `skip_eyes` — не считать страницы,
    уже прочитанные глазами (`eyes` в `_ocr.json` ставит merge_eyes.py)."""
    return [p["page"] for p in pages
            if p.get("mode") in ("timeout", "error")
            and "VISION_FALLBACK" not in {w.split(":")[0] for w in p.get("warnings") or []}
            and not (skip_eyes and p.get("eyes"))]

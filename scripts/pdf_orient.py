#!/usr/bin/env python3
"""Ориентация страниц PDF по Apple Vision: печатает JSON {"pages": N, "rot": {"1": 90, ...}, "turned": k}.
Запускать интерпретатором OCR-окружения (там pymupdf и pyobjc):
    ~/ocr-env2/bin/python pdf_orient.py file.pdf [--max-pages 3] [--fix out.pdf]
--fix пишет копию PDF, где каждая страница повёрнута так, чтобы текст лежал прямо
(исходник не трогается); это нужно быстрой ветке, которая читает текстовый слой.
"""
from __future__ import annotations
import argparse, json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import orientation  # noqa: E402
try:
    import pymupdf as fitz  # noqa: E402
except ImportError:
    import fitz  # noqa: E402
from PIL import Image  # noqa: E402

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("pdf"); ap.add_argument("--max-pages", type=int, default=3); ap.add_argument("--fix", default=None)
    ap.add_argument("--all", action="store_true", help="проверить все страницы (для --fix)")
    a = ap.parse_args()
    doc = fitz.open(a.pdf)
    n = len(doc)
    idx = range(n) if (a.all or a.fix) else range(min(n, a.max_pages))
    rot = {}
    for i in idx:
        pix = doc[i].get_pixmap(dpi=100)
        img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
        r, sure, _ = orientation.detect(img)
        if r and sure >= 0.3:
            rot[str(i + 1)] = r
    out = {"pages": n, "checked": len(list(idx)), "rot": rot, "turned": len(rot)}
    if a.fix and rot:
        for k, r in rot.items():
            p = doc[int(k) - 1]
            p.set_rotation((p.rotation + r) % 360)
        doc.save(a.fix)
        out["fixed"] = a.fix
    doc.close()
    print(json.dumps(out, ensure_ascii=False))
    return 0

if __name__ == "__main__":
    sys.exit(main())

"""Картинка одной страницы для чтения глазами (шаг 4б): так же, как её видел движок.

PDF — рендер страницы, многостраничный TIFF — нужный кадр, фото — с учётом EXIF; затем поворот по часовой на угол из `_ocr.json`
(поле `rotation` страницы), как его применил движок.

    ~/ocr-env2/bin/python -I render_page.py <исходник> <стр> <поворот> <выход.png> [dpi]
"""
from __future__ import annotations
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageOps


def render(src: Path, page: int, rot: int, dpi: int = 200) -> Image.Image:
    if src.suffix.lower() == ".pdf":
        import pymupdf
        with pymupdf.open(src) as doc:
            pix = doc[page - 1].get_pixmap(dpi=dpi)
            img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    else:
        if src.suffix.lower() in (".heic", ".heif"):   # PIL без плагина HEIC не открывает
            tmp = Path(tempfile.mkdtemp()) / "page.png"
            subprocess.run(["sips", "-s", "format", "png", str(src), "--out", str(tmp)], check=True, capture_output=True)
            src = tmp
        img = Image.open(src)
        if page > 1 and src.suffix.lower() in (".tif", ".tiff"):   # страница многостраничного TIFF
            img.seek(page - 1)
            img = img.copy()
        img = ImageOps.exif_transpose(img).convert("RGB")
    return img if rot % 360 == 0 else img.rotate(-rot, expand=True)


def main() -> int:
    if len(sys.argv) < 5:
        print(__doc__)
        return 2
    src, page, rot, out = Path(sys.argv[1]), int(sys.argv[2]), int(sys.argv[3]), Path(sys.argv[4])
    dpi = int(sys.argv[5]) if len(sys.argv) > 5 else 200
    render(src, page, rot, dpi).save(out)
    print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())

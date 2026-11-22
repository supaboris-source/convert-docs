"""QR-коды и штрихкоды на странице через Apple Vision (VNDetectBarcodesRequest).

Зачем: на судебных актах, ответах госорганов и квитанциях стоит QR с ссылкой на
карточку дела, номером отправления или реквизитами платежа. OCR-модели читают его
как картинку и теряют. Vision декодирует его за доли секунды; содержимое дописывается
в конец страницы строками «‹QR› …», чтобы ссылка/номер были в тексте и искались.
"""
from __future__ import annotations
import os
import tempfile
from PIL import Image

MAX_SIDE = 2600


def decode(img: Image.Image) -> list[dict]:
    """[{"symbology", "payload"}] — пусто без pyobjc или без кодов."""
    try:
        import Vision
        import Quartz
        from Foundation import NSURL
    except Exception:  # noqa: BLE001
        return []
    im = img.convert("RGB").copy()
    im.thumbnail((MAX_SIDE, MAX_SIDE))
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as t:
        im.save(t.name)
        p = t.name
    try:
        src = Quartz.CGImageSourceCreateWithURL(NSURL.fileURLWithPath_(p), None)
        cg = Quartz.CGImageSourceCreateImageAtIndex(src, 0, None)
        req = Vision.VNDetectBarcodesRequest.alloc().init()
        h = Vision.VNImageRequestHandler.alloc().initWithCGImage_options_(cg, None)
        h.performRequests_error_([req], None)
        out = []
        seen = set()
        for o in (req.results() or []):
            payload = o.payloadStringValue()
            if not payload or payload in seen:
                continue
            seen.add(payload)
            out.append({"symbology": str(o.symbology()).replace("VNBarcodeSymbology", ""), "payload": str(payload)})
        return out
    except Exception:  # noqa: BLE001
        return []
    finally:
        try:
            os.unlink(p)
        except OSError:
            pass


def lines(codes: list[dict]) -> str:
    return "\n".join(f"‹QR› {c['payload']}" if "QR" in c["symbology"] else f"‹{c['symbology']}› {c['payload']}" for c in codes)


if __name__ == "__main__":
    import sys
    for f in sys.argv[1:]:
        print(f, decode(Image.open(f)))

"""Self-check for compress_pdf's guard chain.

Runs standalone: `python3 test_pdf_compress.py` (needs ghostscript + pdfinfo).
Fails if a guard stops holding — e.g. a signed file gets re-encoded, page count
drifts, or an inflated result is returned instead of None.
"""

import io
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pdf_compress import SkipReason, compress_pdf, compress_pdf_verbose, tools_available


def _scan_pdf(pages=3, size=(2550, 3300)) -> bytes:
    """A 300-DPI scanned-document lookalike: white page, text-like rows, paper
    grain. This is the shape the 200-DPI downsample is calibrated for — pure
    noise would be incompressible and prove nothing.
    """
    from PIL import Image, ImageDraw

    random.seed(7)
    imgs = []
    for p in range(pages):
        img = Image.new("RGB", size, (252, 251, 248))
        draw = ImageDraw.Draw(img)
        y = 240
        while y < size[1] - 240:
            # a "line of text": a run of short dark blocks of varying width
            x = 240
            while x < size[0] - 240:
                w = random.randint(40, 180)
                draw.rectangle([x, y, x + w, y + 34], fill=(28, 26, 30))
                x += w + random.randint(18, 40)
            y += 78
        # paper grain: gentle, spatially correlated — not white noise
        grain = Image.effect_noise((size[0] // 8, size[1] // 8), 22).resize(size)
        img = Image.blend(img, Image.merge("RGB", (grain, grain, grain)), 0.12)
        imgs.append(img)
    buf = io.BytesIO()
    imgs[0].save(buf, format="PDF", save_all=True, append_images=imgs[1:], resolution=300.0)
    return buf.getvalue()


def demo():
    assert tools_available(), "install ghostscript and poppler-utils"

    big = _scan_pdf()
    assert len(big) > 1024 * 1024, f"fixture too small ({len(big)} bytes)"
    # Floor is a parameter so the happy path can use a small fixture; the
    # production default is asserted separately below.
    floor = 1024 * 1024

    # The happy path: genuinely smaller, still a PDF, same page count.
    out, reason = compress_pdf_verbose(big, min_bytes=floor)
    assert reason is None, reason
    assert out and len(out) < len(big), (len(out) if out else None, len(big))
    assert out[:5] == b"%PDF-"
    from pdf_compress import page_count
    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as fh:
        fh.write(out)
        assert page_count(fh.name) == 3, "page count must survive"
    os.unlink(fh.name)
    print(f"  compressed {len(big) / 2**20:.1f} MiB -> {len(out) / 2**20:.1f} MiB "
          f"({100 * (1 - len(out) / len(big)):.0f}% off)")

    # Guards: each must return None rather than a file.
    assert compress_pdf(big) is None, "default 5 MiB floor must reject a 1.7 MiB file"
    assert compress_pdf_verbose(b"x" * 6_000_000)[1] == SkipReason.NOT_PDF
    assert compress_pdf_verbose(b"")[1] == SkipReason.TOO_SMALL

    signed = big[:100] + b"/ByteRange[0 100 200 300]" + big[100:]
    assert compress_pdf_verbose(signed, min_bytes=floor)[1] == SkipReason.SIGNED, \
        "signed PDFs must be skipped"

    # Already-compressed file: a second pass has nothing left to take, so we
    # must decline rather than hand back a same-size (or larger) re-encode.
    # This also makes the pipeline idempotent — rerunning never degrades a file.
    assert compress_pdf_verbose(out, min_bytes=1024)[1] == SkipReason.NOT_SMALLER, \
        "re-compressing must return None, not a bigger file"

    print("ok — compresses scans, declines everything else")


if __name__ == "__main__":
    demo()

"""Downsample images inside a PDF without changing the file's identity.

One function, used in two places: the upload path
(``S3Operations.upload_files_to_s3_with_key``) and the Phase 2 backlog script.
No frappe imports, so the backlog script can run standalone.

Settings were calibrated against real production scans: 200 DPI / JPEGQ 90
saves ~32% at ~41 dB PSNR (visually lossless). The popular ``-dPDFSETTINGS=
/ebook`` saves more but lands at 22-27 dB, which is visible on passports and
transcripts — do not "optimize" this by switching to a preset.

The DPI target is corrected for page size (see TARGET_LONG_EDGE_PX): ghostscript
resolves DPI against the page box, so a flat 200 leaves an A4 scan alone but
guts a photo pasted on a 6x4 inch page.

Requires: ghostscript, poppler-utils (pdfinfo).
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile

# Below this, re-encoding is not worth the risk or the runtime: the sub-1 MB
# bands are mostly text-only or already-optimized PDFs.
MIN_BYTES = 5 * 1024 * 1024

# Wall-clock ceiling for one file. A pathological PDF must not wedge a worker.
TIMEOUT_SECONDS = 300

# Ghostscript resolves its DPI target against the PDF page box, so the same
# "200 DPI" that is right for A4 is catastrophic on a small page: a 40-megapixel
# photo on a 6x4 inch page comes back at 982x799. Measured on 60 real production
# PDFs, every page is a standard 11.0-11.7 inch document. Hold the long-edge
# pixel budget constant instead of the DPI, so an unusual page size gets the same
# absolute resolution a normal page already gets.
# 2200 is US Letter (11.0in) at 200 DPI — the *smaller* of the two standard page
# sizes, so both Letter and A4 clamp to exactly 200 and no ordinary document
# changes at all. Verified: 0 of 60 production files differ from flat 200 DPI.
TARGET_LONG_EDGE_PX = 2200

_GS_FIXED_ARGS = [
    "-sDEVICE=pdfwrite",
    "-dCompatibilityLevel=1.5",
    "-dNOPAUSE",
    "-dQUIET",
    "-dBATCH",
    "-dSAFER",
    "-dDownsampleColorImages=true",
    "-dDownsampleGrayImages=true",
    "-dDownsampleMonoImages=true",
    "-dColorImageDownsampleType=/Bicubic",
    "-dGrayImageDownsampleType=/Bicubic",
    "-dAutoFilterColorImages=false",
    "-dColorImageFilter=/DCTEncode",
    "-dAutoFilterGrayImages=false",
    "-dGrayImageFilter=/DCTEncode",
    "-dJPEGQ=90",
]


def resolution_args(page_long_edge_inches):
    """The three -d*ImageResolution flags, corrected for page size.

    ``max()``, not a clamp on both ends: small pages get more resolution and
    nothing ever gets less than today. The sample had pages up to 39.5 inches,
    which an uncorrected formula would drop to 56 DPI — more aggressive than
    anything that has been calibrated.
    """
    dpi = 200
    if page_long_edge_inches:
        dpi = max(dpi, int(round(TARGET_LONG_EDGE_PX / page_long_edge_inches)))
    return [
        "-dColorImageResolution=%d" % dpi,
        "-dGrayImageResolution=%d" % dpi,
        # Mono (1-bit) images are line art; they keep the same 1.5x they had.
        "-dMonoImageResolution=%d" % int(dpi * 1.5),
    ]


class SkipReason:
    TOO_SMALL = "too_small"
    NOT_PDF = "not_pdf"
    SIGNED = "signed"
    NO_TOOLS = "no_tools"
    FAILED = "gs_failed"
    PAGE_MISMATCH = "page_mismatch"
    NOT_SMALLER = "not_smaller"


def tools_available() -> bool:
    return bool(shutil.which("gs") and shutil.which("pdfinfo"))


def is_pdf(data: bytes) -> bool:
    return data[:5] == b"%PDF-"


def is_signed(data: bytes) -> bool:
    """Ghostscript re-encoding invalidates digital signatures, so signed files
    are left alone. A signature dictionary cannot live inside an object stream
    (its /ByteRange must be directly addressable), so a raw scan is reliable."""
    return b"/ByteRange" in data or b"/Sig" in data


def pdf_info(path: str):
    """(page count, long edge in inches). Either may be None on a parse miss.

    One pdfinfo run for both — the page size decides the DPI target and the
    page count is the damage check, and they are needed at the same moment.
    """
    try:
        out = subprocess.run(
            ["pdfinfo", path], capture_output=True, timeout=60, check=False
        ).stdout.decode("utf-8", "replace")
    except (OSError, subprocess.SubprocessError):
        return None, None
    m = re.search(r"^Pages:\s+(\d+)", out, re.MULTILINE)
    pages = int(m.group(1)) if m else None
    m = re.search(r"^Page size:\s+([\d.]+) x ([\d.]+) pts", out, re.MULTILINE)
    inches = max(float(m.group(1)), float(m.group(2))) / 72.0 if m else None
    return pages, inches


def compress_pdf(data: bytes, min_bytes: int = MIN_BYTES) -> bytes | None:
    """Smaller PDF bytes, or None meaning "keep the original".

    None covers every reason not to replace the file — under the floor, not a
    PDF, signed, tooling missing, ghostscript failed, page count changed, or
    the output simply wasn't smaller. Callers never have to distinguish; use
    ``compress_pdf_verbose`` when you want the reason for a log.
    """
    result, _reason = compress_pdf_verbose(data, min_bytes=min_bytes)
    return result


def compress_pdf_verbose(data: bytes, min_bytes: int = MIN_BYTES):
    """(bytes | None, reason). Reason is None on success."""
    if not data or len(data) < min_bytes:
        return None, SkipReason.TOO_SMALL
    if not is_pdf(data):
        return None, SkipReason.NOT_PDF
    if is_signed(data):
        return None, SkipReason.SIGNED
    if not tools_available():
        return None, SkipReason.NO_TOOLS

    tmpdir = tempfile.mkdtemp(prefix="pdfz-")
    src = os.path.join(tmpdir, "in.pdf")
    dst = os.path.join(tmpdir, "out.pdf")
    try:
        with open(src, "wb") as fh:
            fh.write(data)

        before, page_inches = pdf_info(src)
        try:
            proc = subprocess.run(
                ["gs", *_GS_FIXED_ARGS, *resolution_args(page_inches),
                 f"-sOutputFile={dst}", src],
                capture_output=True,
                timeout=TIMEOUT_SECONDS,
                check=False,
            )
        except subprocess.SubprocessError:
            return None, SkipReason.FAILED
        if proc.returncode != 0 or not os.path.exists(dst):
            return None, SkipReason.FAILED

        # A changed page count means ghostscript dropped or merged something.
        after, _ = pdf_info(dst)
        if before is None or after is None or before != after:
            return None, SkipReason.PAGE_MISMATCH

        with open(dst, "rb") as fh:
            out = fh.read()
        # Re-encoding genuinely inflates some files; keep the original then.
        if not is_pdf(out) or len(out) >= len(data):
            return None, SkipReason.NOT_SMALLER
        return out, None
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def _self_check():
    """The DPI correction must be a no-op on normal documents."""
    calibrated = [
        "-dColorImageResolution=200",
        "-dGrayImageResolution=200",
        "-dMonoImageResolution=300",
    ]
    # Both standard page sizes, and an unreadable page box, keep the
    # calibrated settings byte-for-byte.
    assert resolution_args(11.7) == calibrated, "A4 must not change"
    assert resolution_args(11.0) == calibrated, "US Letter must not change"
    assert resolution_args(None) == calibrated
    # Bigger pages must never get *less* than the calibrated resolution.
    assert resolution_args(37.6) == calibrated
    # The Photo.pdf case: 432x288 pts == 6 inches. A flat 200 DPI gave it
    # 1200px on the long edge and destroyed the photo; it must get ~2200.
    assert resolution_args(6.0) == [
        "-dColorImageResolution=367",
        "-dGrayImageResolution=367",
        "-dMonoImageResolution=550",
    ], "a small page must get the same pixel budget as a normal one"
    print("pdf_compress self-check ok")


if __name__ == "__main__":
    _self_check()

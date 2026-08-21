#!/usr/bin/env python3
"""
split_grids.py — split 2x2 AI-generated grid images into Instagram-ready panels.

Pipeline per grid:
    detect gutters (pixel analysis) -> slice 4 panels -> trim residual white
    -> centre-crop to 4:5 and/or 9:16 -> upscale to exact IG dimensions
    -> strip all metadata -> save JPEG q95 4:4:4

See README.md for usage.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import shutil
import string
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

try:
    from PIL import ImageCms
except Exception:  # pragma: no cover - optional
    ImageCms = None  # type: ignore

try:
    LANCZOS = Image.Resampling.LANCZOS
except AttributeError:  # Pillow < 9.1
    LANCZOS = Image.LANCZOS  # type: ignore

# Instagram target sizes.
TARGETS: Dict[str, Tuple[int, int]] = {
    "45": (1080, 1350),   # feed post, 4:5
    "916": (1080, 1920),  # reel / story, 9:16
}
FORMAT_LABEL = {"45": "4:5", "916": "9:16"}

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp"}
VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".webm"}

# Cooperative cancel for the local web UI. CLI never sets this.
_cancel_event = None  # type: Optional[threading.Event]
_active_procs = []    # type: List[subprocess.Popen]


class Cancelled(Exception):
    """Raised when the web UI asks a run to stop."""


def set_cancel_event(event):
    # type: (Optional[threading.Event]) -> None
    global _cancel_event
    _cancel_event = event


def is_cancelled():
    return bool(_cancel_event is not None and _cancel_event.is_set())


def kill_active_subprocesses():
    """Best-effort: kill ffmpeg / ProPainter children started via run_cmd."""
    for proc in list(_active_procs):
        try:
            proc.kill()
        except Exception:
            pass


def check_cancel():
    if is_cancelled():
        raise Cancelled("cancelled")


def run_cmd(cmd, timeout=3600, cwd=None, check=False):
    """subprocess.run stand-in that can be killed from the web UI."""
    check_cancel()
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=cwd)
    _active_procs.append(proc)
    try:
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            stdout, stderr = proc.communicate()
            raise
        check_cancel()

        class _R(object):
            pass
        r = _R()
        r.returncode = proc.returncode
        r.stdout = stdout
        r.stderr = stderr
        if check and r.returncode != 0:
            raise subprocess.CalledProcessError(r.returncode, cmd, stdout, stderr)
        return r
    finally:
        try:
            _active_procs.remove(proc)
        except ValueError:
            pass


# Panels in reading order: top-left, top-right, bottom-left, bottom-right.
PANEL_NAMES = ["p1_tl", "p2_tr", "p3_bl", "p4_br"]


# --------------------------------------------------------------------------
# terminal output
# --------------------------------------------------------------------------

class Log:
    """Coloured, indent-aware logger with a --quiet mode."""

    RESET = "\033[0m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    RED = "\033[31m"
    CYAN = "\033[36m"
    DIM = "\033[2m"
    BOLD = "\033[1m"

    def __init__(self, quiet: bool = False, color: bool = True):
        self.quiet = quiet
        self.color = color and sys.stdout.isatty() and os.environ.get("NO_COLOR") is None
        self.warnings = 0
        self.errors = 0

    def _c(self, text: str, code: str) -> str:
        return "%s%s%s" % (code, text, self.RESET) if self.color else text

    def _emit(self, text: str, stream=None) -> None:
        print(text, file=stream or sys.stdout, flush=True)

    def info(self, text: str, indent: int = 0) -> None:
        if not self.quiet:
            self._emit(" " * indent + text)

    def ok(self, text: str, indent: int = 0) -> None:
        if not self.quiet:
            self._emit(" " * indent + self._c("OK   ", self.GREEN) + text)

    def step(self, text: str, indent: int = 0) -> None:
        if not self.quiet:
            self._emit(" " * indent + self._c("  -  ", self.DIM) + text)

    def warn(self, text: str, indent: int = 0) -> None:
        self.warnings += 1
        if not self.quiet:
            self._emit(" " * indent + self._c("WARN ", self.YELLOW) + self._c(text, self.YELLOW))

    def error(self, text: str, indent: int = 0) -> None:
        self.errors += 1
        # Errors are shown even in quiet mode.
        self._emit(" " * indent + self._c("ERROR ", self.RED) + self._c(text, self.RED), sys.stderr)

    def header(self, text: str) -> None:
        if not self.quiet:
            self._emit(self._c(text, self.BOLD + self.CYAN))

    def bold(self, text: str) -> str:
        return self._c(text, self.BOLD)

    def dim_text(self, text: str) -> str:
        return self._c(text, self.DIM)


# --------------------------------------------------------------------------
# gutter detection
# --------------------------------------------------------------------------

@dataclass
class AxisSplit:
    """Where to cut one axis of the grid."""
    lo_end: int        # end of the first cell (exclusive)
    hi_start: int      # start of the second cell
    content_start: int # first non-border pixel
    content_end: int   # last non-border pixel (exclusive)
    detected: bool     # False => fell back to a 50/50 split
    gutter_runs: List[Tuple[int, int]] = field(default_factory=list)

    @property
    def gutter(self) -> Tuple[int, int]:
        return (self.lo_end, self.hi_start)


def _runs(mask: np.ndarray) -> List[Tuple[int, int]]:
    """Contiguous [start, end) runs of True in a 1-D boolean array."""
    out: List[Tuple[int, int]] = []
    start: Optional[int] = None
    for i, v in enumerate(mask):
        if v and start is None:
            start = i
        elif not v and start is not None:
            out.append((start, i))
            start = None
    if start is not None:
        out.append((start, len(mask)))
    return out


def detect_axis(gray: np.ndarray, axis: int, white_thr: float,
                var_thr: float, min_gutter: int, tol_frac: float) -> AxisSplit:
    """
    Find the interior gutter along one axis.

    axis=0 scans rows (horizontal gutter), axis=1 scans columns (vertical gutter).
    A line is a gutter line when it is near-white *and* low-variance across its
    full span, so a panel whose edge happens to be white but textured is not
    mistaken for a gutter.
    """
    means = gray.mean(axis=1 - axis)
    stds = gray.std(axis=1 - axis)
    mask = (means >= white_thr) & (stds <= var_thr)
    length = gray.shape[axis]
    runs = _runs(mask)

    # Outer border runs touch an edge; they are trimmed, not split on.
    content_start = 0
    content_end = length
    for r in runs:
        if r[0] == 0:
            content_start = r[1]
        if r[1] == length:
            content_end = r[0]
    if content_end <= content_start:  # degenerate: image is basically blank
        content_start, content_end = 0, length

    mid = length / 2.0
    interior = [
        r for r in runs
        if (r[1] - r[0]) >= min_gutter
        and r[0] > content_start
        and r[1] < content_end
        and abs((r[0] + r[1]) / 2.0 - mid) <= tol_frac * length
    ]

    if interior:
        best = min(interior, key=lambda r: abs((r[0] + r[1]) / 2.0 - mid))
        return AxisSplit(best[0], best[1], content_start, content_end, True, runs)

    half = int(round(length / 2.0))
    return AxisSplit(half, half, content_start, content_end, False, runs)


def trim_white_edges(arr: np.ndarray, white_thr: float, var_thr: float,
                     max_frac: float = 0.25) -> Tuple[int, int, int, int]:
    """
    Residual white border of a sliced panel, as (top, bottom, left, right).

    Trimming is capped at max_frac of each dimension so a panel with a
    genuinely bright/empty region is never eaten alive.
    """
    gray = arr.mean(axis=2) if arr.ndim == 3 else arr
    h, w = gray.shape
    edge_var = var_thr * 1.5

    def scan(lines_mean: np.ndarray, lines_std: np.ndarray, limit: int) -> int:
        n = 0
        while n < limit and lines_mean[n] >= white_thr and lines_std[n] <= edge_var:
            n += 1
        return n

    row_mean, row_std = gray.mean(axis=1), gray.std(axis=1)
    col_mean, col_std = gray.mean(axis=0), gray.std(axis=0)
    vlimit = max(0, int(h * max_frac))
    hlimit = max(0, int(w * max_frac))

    top = scan(row_mean, row_std, vlimit)
    bottom = scan(row_mean[::-1], row_std[::-1], vlimit)
    left = scan(col_mean, col_std, hlimit)
    right = scan(col_mean[::-1], col_std[::-1], hlimit)
    return top, bottom, left, right


# --------------------------------------------------------------------------
# cropping
# --------------------------------------------------------------------------

def centre_crop(img: Image.Image, target_w: int, target_h: int) -> Tuple[Image.Image, str]:
    """Centre-crop to the target aspect ratio. Returns (image, axis trimmed)."""
    w, h = img.size
    target_ratio = target_w / float(target_h)
    cur_ratio = w / float(h)

    if abs(cur_ratio - target_ratio) < 1e-6:
        return img, "none"

    if cur_ratio > target_ratio:
        # too wide -> trim left/right
        new_w = max(1, int(round(h * target_ratio)))
        x0 = (w - new_w) // 2
        return img.crop((x0, 0, x0 + new_w, h)), "left/right"

    # too tall -> trim top/bottom
    new_h = max(1, int(round(w / target_ratio)))
    y0 = (h - new_h) // 2
    return img.crop((0, y0, w, y0 + new_h)), "top/bottom"


# --------------------------------------------------------------------------
# upscaling
# --------------------------------------------------------------------------

@dataclass
class Upscaler:
    kind: str                       # "realesrgan" | "lanczos"
    binary: Optional[str] = None
    model: str = "realesrgan-x4plus"
    native_scale: int = 4
    models_dir: Optional[str] = None
    detail: str = ""


# Names to try on PATH, plus fixed locations for GUI apps that ship the binary.
REALESRGAN_CANDIDATES = [
    "realesrgan-ncnn-vulkan",
    "upscayl-bin",
    "realesrgan-ncnn-py",
    "realesrgan",
]

BUNDLED_CANDIDATES = [
    "/Applications/Upscayl.app/Contents/Resources/bin/upscayl-bin",
    os.path.expanduser("~/Applications/Upscayl.app/Contents/Resources/bin/upscayl-bin"),
]


def guess_models_dir(binary: str) -> Optional[str]:
    """ncnn builds need -m when the models folder is not the working directory."""
    here = Path(binary).resolve().parent
    for cand in (here / "models", here.parent / "models",
                 here.parent / "Resources" / "models"):
        if cand.is_dir():
            return str(cand)
    return None


def available_models(models_dir: Optional[str]) -> List[str]:
    """
    Model names an ncnn build will accept, best-for-photos first.

    x4 models rank first (we need ~2.1-2.5x), and within those the general
    purpose ones beat stylised models like remacri/ultrasharp, which would
    give photographic panels a painterly look.
    """
    if not models_dir or not os.path.isdir(models_dir):
        return []
    names = {Path(f).stem for f in os.listdir(models_dir) if f.endswith(".param")}

    def rank(name: str) -> Tuple[int, int, str]:
        low = name.lower()
        not_x4 = 0 if ("x4" in low or "4x" in low) else 1
        not_general = 0 if any(k in low for k in
                               ("realesrgan", "standard", "general")) else 1
        return (not_x4, not_general, name)

    return sorted(names, key=rank)


def build_command(up: Upscaler, src: str, dst: str, model: Optional[str] = None) -> List[str]:
    cmd = [up.binary, "-i", src, "-o", dst,
           "-s", str(up.native_scale), "-n", model or up.model]
    if up.models_dir:
        cmd += ["-m", up.models_dir]
    return cmd


def detect_upscaler(log: Log, forced_bin: Optional[str], disable: bool,
                    model: str, scale: int,
                    models_dir: Optional[str] = None) -> Upscaler:
    """Find a working Real-ESRGAN binary, verifying it with a tiny probe render."""
    if disable:
        return Upscaler("lanczos", detail="disabled by --no-realesrgan")

    if forced_bin:
        candidates = [forced_bin]
    else:
        candidates = REALESRGAN_CANDIDATES + BUNDLED_CANDIDATES

    for name in candidates:
        if not name:
            continue
        path = name if os.path.sep in name and os.path.exists(name) else shutil.which(name)
        if not path:
            continue

        mdir = models_dir or guess_models_dir(path)
        probe = Upscaler("realesrgan", binary=path, model=model,
                         native_scale=scale, models_dir=mdir, detail=path)

        # Try the requested model, then whatever the models folder actually has —
        # Upscayl and upstream ncnn ship different model names.
        tried = [model] + [m for m in available_models(mdir) if m != model][:4]
        for candidate_model in tried:
            if _probe_realesrgan(probe, candidate_model):
                if candidate_model != model:
                    log.warn("model %r unavailable; using %r instead"
                             % (model, candidate_model))
                probe.model = candidate_model
                return probe

        log.warn("found %s but no model rendered successfully%s; ignoring it"
                 % (path, " (models dir: %s)" % mdir if mdir else
                    " (no models folder found — pass --realesrgan-models DIR)"))

    if forced_bin:
        log.warn("--realesrgan-bin %r not usable; falling back to Lanczos" % forced_bin)
    return Upscaler("lanczos", detail="Real-ESRGAN not found on PATH")


def _probe_realesrgan(up: Upscaler, model: str) -> bool:
    """Render an 8x8 test tile so we only claim availability if it truly works."""
    try:
        with tempfile.TemporaryDirectory() as td:
            src = os.path.join(td, "probe.png")
            dst = os.path.join(td, "probe_out.png")
            Image.new("RGB", (8, 8), (128, 90, 40)).save(src)
            proc = subprocess.run(
                build_command(up, src, dst, model),
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120,
            )
            return proc.returncode == 0 and os.path.exists(dst) and os.path.getsize(dst) > 0
    except Exception:
        return False


def upscale(img: Image.Image, target_w: int, target_h: int, up: Upscaler,
            log: Log, indent: int) -> Tuple[Image.Image, str]:
    """Resize to exactly target_w x target_h. Returns (image, method label)."""
    w, h = img.size
    factor = max(target_w / float(w), target_h / float(h))

    if factor <= 1.0:
        out = img.resize((target_w, target_h), LANCZOS)
        return out, "lanczos-downsample"

    if up.kind == "realesrgan":
        big = _run_realesrgan(img, up, log, indent)
        if big is not None:
            out = big.resize((target_w, target_h), LANCZOS)
            return out, "real-esrgan x%d + lanczos fit" % up.native_scale
        log.warn("Real-ESRGAN failed on this panel; using Lanczos for it", indent)

    out = img.resize((target_w, target_h), LANCZOS)
    out = out.filter(ImageFilter.UnsharpMask(radius=1.2, percent=60, threshold=3))
    return out, "lanczos + unsharp mask"


def _run_realesrgan(img: Image.Image, up: Upscaler, log: Log,
                    indent: int) -> Optional[Image.Image]:
    try:
        with tempfile.TemporaryDirectory() as td:
            src = os.path.join(td, "in.png")
            dst = os.path.join(td, "out.png")
            img.save(src)
            proc = subprocess.run(
                build_command(up, src, dst),
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=600,
            )
            if proc.returncode != 0 or not os.path.exists(dst):
                tail = proc.stderr.decode("utf-8", "replace").strip().splitlines()
                if tail:
                    log.warn("real-esrgan: %s" % tail[-1], indent)
                return None
            with Image.open(dst) as out:
                return out.convert("RGB").copy()
    except Exception as exc:
        log.warn("real-esrgan: %s" % exc, indent)
        return None


# --------------------------------------------------------------------------
# metadata
# --------------------------------------------------------------------------

C2PA_SIGNATURES = [b"c2pa", b"jumb", b"jumd", b"contentcredentials",
                   b"urn:uuid:", b"http://ns.adobe.com/xap/"]


def to_srgb(img: Image.Image, log: Log, indent: int) -> Image.Image:
    """Convert to sRGB using the embedded profile, then drop the profile."""
    icc = img.info.get("icc_profile")
    if img.mode in ("RGBA", "LA", "P"):
        base = img.convert("RGBA")
        flat = Image.new("RGBA", base.size, (255, 255, 255, 255))
        flat.alpha_composite(base)
        img = flat.convert("RGB")
    elif img.mode != "RGB":
        img = img.convert("RGB")

    if icc and ImageCms is not None:
        try:
            src_profile = ImageCms.ImageCmsProfile(io.BytesIO(icc))
            desc = (ImageCms.getProfileDescription(src_profile) or "").strip()
            if "srgb" not in desc.lower():
                dst_profile = ImageCms.createProfile("sRGB")
                img = ImageCms.profileToProfile(img, src_profile, dst_profile,
                                                outputMode="RGB")
                log.step("colour: converted %r -> sRGB, profile dropped" % desc, indent)
            else:
                log.step("colour: source profile already sRGB (%r), dropped" % desc, indent)
        except Exception as exc:
            log.warn("ICC conversion failed (%s); using raw RGB values" % exc, indent)
    elif icc:
        log.warn("ICC profile present but ImageCms unavailable; profile dropped "
                 "without conversion", indent)
    return img


def strip_metadata(img: Image.Image) -> Image.Image:
    """Rebuild the image from raw pixels so no info/EXIF/ICC survives the save."""
    clean = Image.frombytes(img.mode, img.size, img.tobytes())
    clean.info = {}
    return clean


def jpeg_metadata_segments(path: Path) -> List[str]:
    """Return names of any metadata-bearing JPEG segments still in the file."""
    found: List[str] = []
    data = path.read_bytes()
    if data[:2] != b"\xff\xd8":
        return ["not-a-jpeg"]

    i = 2
    n = len(data)
    while i < n - 1:
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker == 0xFF:
            i += 1
            continue
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        if marker in (0xDA, 0xD9):  # start of scan / end of image
            break
        if i + 4 > n:
            break
        seg_len = int.from_bytes(data[i + 2:i + 4], "big")
        if seg_len < 2:
            break
        if marker == 0xE0:
            pass  # APP0/JFIF is a container header, not metadata
        elif 0xE1 <= marker <= 0xEF:
            found.append("APP%d" % (marker - 0xE0))
        elif marker == 0xFE:
            found.append("COM")
        i += 2 + seg_len

    lowered = data.lower()
    for sig in C2PA_SIGNATURES:
        if sig in lowered:
            found.append("payload:%s" % sig.decode())
    return found


# --------------------------------------------------------------------------
# metadata inspection and lossless cleaning (no resizing, no splitting)
# --------------------------------------------------------------------------

PNG_KEEP_CHUNKS = {b"IHDR", b"PLTE", b"IDAT", b"IEND", b"tRNS",
                   b"gAMA", b"cHRM", b"sRGB", b"bKGD"}
PNG_META_CHUNKS = {b"tEXt": "text", b"zTXt": "compressed text",
                   b"iTXt": "international text / XMP", b"eXIf": "EXIF",
                   b"iCCP": "ICC profile", b"tIME": "timestamp",
                   b"caBX": "C2PA (JUMBF)", b"prVW": "preview"}

JPEG_APP_LABELS = {
    1: "EXIF / XMP", 2: "ICC profile / MPF", 11: "JUMBF (C2PA)",
    13: "IPTC / Photoshop", 14: "Adobe",
}


def iter_png_chunks(data: bytes):
    """Yield (type, start, total_len) for each PNG chunk."""
    i = 8
    n = len(data)
    while i + 8 <= n:
        length = int.from_bytes(data[i:i + 4], "big")
        ctype = data[i + 4:i + 8]
        total = 12 + length
        if length < 0 or i + total > n:
            break
        yield ctype, i, total
        i += total
        if ctype == b"IEND":
            break


def jpeg_segments_detailed(data: bytes):
    """Metadata-bearing JPEG segments as (label, byte length)."""
    out = []
    i, n = 2, len(data)
    while i < n - 1:
        if data[i] != 0xFF:
            i += 1
            continue
        m = data[i + 1]
        if m == 0xFF:
            i += 1
            continue
        if m in (0xD8, 0x01) or 0xD0 <= m <= 0xD7:
            i += 2
            continue
        if m in (0xDA, 0xD9):
            break
        if i + 4 > n:
            break
        seg = int.from_bytes(data[i + 2:i + 4], "big")
        if seg < 2:
            break
        if 0xE1 <= m <= 0xEF:
            k = m - 0xE0
            out.append(("APP%d (%s)" % (k, JPEG_APP_LABELS.get(k, "application")),
                        seg + 2))
        elif m == 0xFE:
            out.append(("COM (comment)", seg + 2))
        i += 2 + seg
    return out


def inspect_image_metadata(path: Path) -> List[Finding]:
    """Every removable metadata item in an image, whatever the container."""
    findings: List[Finding] = []
    try:
        data = path.read_bytes()
    except OSError as exc:
        return [Finding("error", "unreadable", str(exc))]

    if data[:2] == b"\xff\xd8":
        for name, size in jpeg_segments_detailed(data):
            findings.append(Finding("segment", name, "", size))
    elif data[:8] == b"\x89PNG\r\n\x1a\n":
        for ctype, off, total in iter_png_chunks(data):
            if ctype in PNG_META_CHUNKS:
                body = data[off + 8:off + total - 4]
                findings.append(Finding(
                    "chunk", "%s chunk (%s)" % (ctype.decode("ascii", "replace"),
                                                PNG_META_CHUNKS[ctype]),
                    " ".join(printable_runs(body, 4, 2)), total))
            elif ctype not in PNG_KEEP_CHUNKS:
                findings.append(Finding(
                    "chunk", "%s chunk" % ctype.decode("ascii", "replace"), "", total))
    elif data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        i = 12
        while i + 8 <= len(data):
            fourcc = data[i:i + 4]
            size = int.from_bytes(data[i + 4:i + 8], "little")
            if fourcc in (b"EXIF", b"XMP ", b"ICCP"):
                findings.append(Finding("chunk", "%s chunk"
                                        % fourcc.decode("ascii", "replace"),
                                        "", size + 8))
            i += 8 + size + (size & 1)

    try:
        with Image.open(path) as im:
            im.load()
            for key in ("exif", "icc_profile", "photoshop", "adobe",
                        "comment", "XML:com.adobe.xmp", "xmp"):
                val = im.info.get(key)
                if val:
                    findings.append(Finding(
                        "info", "Pillow info: %s" % key, "",
                        len(val) if isinstance(val, (bytes, str)) else 0))
    except Exception:
        pass

    low = data.lower()
    for sig, label in PROVENANCE_SIGNATURES:
        n = low.count(sig)
        if n:
            findings.append(Finding("signature", label,
                                    "%d occurrence(s) of %r" % (n, sig.decode())))
    return findings


def clean_image_file(src: Path, dst: Path) -> dict:
    """
    Strip metadata without touching the picture.

    JPEG and PNG are rewritten at the container level: the compressed image
    data is copied byte-for-byte and only metadata records are dropped, so
    there is no generational quality loss. Other formats are re-encoded.
    """
    data = src.read_bytes()
    before = len(data)
    dst.parent.mkdir(parents=True, exist_ok=True)
    notes = []

    if data[:2] == b"\xff\xd8":
        out = bytearray(b"\xff\xd8")
        i, n = 2, len(data)
        while i < n - 1:
            if data[i] != 0xFF:
                i += 1
                continue
            m = data[i + 1]
            if m == 0xFF:
                i += 1
                continue
            if m in (0xD8, 0x01) or 0xD0 <= m <= 0xD7:
                i += 2
                continue
            if m == 0xDA:
                out += data[i:]
                break
            if m == 0xD9:
                out += data[i:i + 2]
                break
            seg = int.from_bytes(data[i + 2:i + 4], "big")
            if seg < 2:
                break
            if not ((0xE1 <= m <= 0xEF) or m == 0xFE):
                out += data[i:i + 2 + seg]
            i += 2 + seg
        dst.write_bytes(bytes(out))
        mode = "lossless (JPEG segments rewritten)"

    elif data[:8] == b"\x89PNG\r\n\x1a\n":
        out = bytearray(data[:8])
        for ctype, off, total in iter_png_chunks(data):
            if ctype in PNG_KEEP_CHUNKS:
                out += data[off:off + total]
            elif ctype == b"iCCP":
                notes.append("dropped ICC profile; use the split pipeline if you "
                             "need proper conversion to sRGB")
        dst.write_bytes(bytes(out))
        mode = "lossless (PNG chunks rewritten)"

    elif data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        # RIFF chunk surgery: drop the metadata chunks, keep the coded image
        # bitstream untouched, and clear the VP8X flags that advertised them.
        body = bytearray()
        i = 12
        while i + 8 <= len(data):
            fourcc = data[i:i + 4]
            size = int.from_bytes(data[i + 4:i + 8], "little")
            total = 8 + size + (size & 1)          # chunks are even-padded
            if fourcc in (b"ICCP", b"EXIF", b"XMP "):
                if fourcc == b"ICCP":
                    notes.append("dropped ICC profile; use the split pipeline if "
                                 "you need proper conversion to sRGB")
            else:
                chunk = bytearray(data[i:i + total])
                if fourcc == b"VP8X" and size >= 1:
                    # bit 0x20 ICC, 0x08 EXIF, 0x04 XMP
                    chunk[8] &= ~0x2C & 0xFF
                body += chunk
            i += total
        out = bytearray(b"RIFF" + (4 + len(body)).to_bytes(4, "little") + b"WEBP")
        out += body
        dst.write_bytes(bytes(out))
        mode = "lossless (WebP chunks rewritten)"

    else:
        with Image.open(src) as im:
            im.load()
            fmt = (im.format or "PNG").upper()
            clean = Image.frombytes(im.mode, im.size, im.tobytes())
            clean.info = {}
            clean.save(dst, format=fmt)
        mode = "re-encoded (%s)" % fmt

    return {"before": before, "after": dst.stat().st_size,
            "mode": mode, "notes": notes}


# --------------------------------------------------------------------------
# AI-origin and SynthID evidence
# --------------------------------------------------------------------------

# Strings that identify a generator when they appear in a metadata field.
GENERATOR_NAMES = [
    "midjourney", "dall-e", "dall·e", "dalle", "openai", "gpt image", "gpt-image",
    "stable diffusion", "stablediffusion", "automatic1111", "comfyui", "invokeai",
    "novelai", "adobe firefly", "firefly", "imagen", "veo", "gemini", "deepmind",
    "flux", "black forest labs", "leonardo.ai", "ideogram", "runway", "pika labs",
    "kling", "sora", "grok", "playground ai", "nightcafe", "starryai", "recraft",
    "krea", "luma", "hailuo", "seedream", "qwen-image", "nano banana",
]

# PNG text keys that generators write prompts and settings into.
GENERATOR_PNG_KEYS = {
    "parameters": "Stable Diffusion / A1111 generation parameters",
    "prompt": "generation prompt",
    "workflow": "ComfyUI workflow",
    "negative_prompt": "negative prompt",
    "sd-metadata": "Stable Diffusion metadata",
    "invokeai_metadata": "InvokeAI metadata",
    "dream": "InvokeAI dream string",
    "aigc": "AIGC marker",
    "generation_data": "generation data",
}

# EXIF tags a real camera writes. Their presence is evidence of a capture
# pipeline; their absence proves nothing on its own.
CAMERA_EXIF_TAGS = {
    271: "camera make", 272: "camera model", 42036: "lens model",
    33434: "exposure time", 33437: "f-number", 34855: "ISO",
    36867: "capture date", 37386: "focal length", 34853: "GPS",
}

# Output sizes characteristic of image generators rather than cameras.
GENERATOR_SIZES = {
    (1024, 1024), (512, 512), (768, 768), (1024, 1536), (1536, 1024),
    (1024, 1792), (1792, 1024), (1152, 896), (896, 1152), (1216, 832),
    (832, 1216), (1344, 768), (768, 1344), (1440, 1440), (2048, 2048),
    (1080, 1920), (1920, 1080), (720, 1280), (1280, 720),
}


@dataclass
class Evidence:
    weight: str      # "declared" (conclusive) | "circumstantial" | "camera"
    label: str
    detail: str = ""


@dataclass
class OriginReport:
    verdict: str            # ai-declared | ai-likely | camera-like | unknown
    headline: str
    explain: str
    evidence: List[Evidence] = field(default_factory=list)
    synthid: str = "not-declared"     # declared | not-declared
    synthid_note: str = ""


def _text_blobs(path: Path, findings: List[Finding]) -> List[Tuple[str, str]]:
    """(where, text) pairs worth searching for generator names."""
    out = [(f.label, f.detail or "") for f in findings]
    try:
        with Image.open(path) as im:
            for k, v in (im.info or {}).items():
                if isinstance(v, str):
                    out.append(("PNG/text: %s" % k, v))
            ex = im.getexif()
            for tag in (271, 272, 305, 270, 42036):
                val = ex.get(tag)
                if isinstance(val, str) and val.strip():
                    out.append(("EXIF tag %d" % tag, val))
    except Exception:
        pass
    return out


def assess_origin(path: Path, findings: List[Finding],
                  width: int = 0, height: int = 0,
                  is_video: bool = False) -> OriginReport:
    """
    Weigh up what the file itself says about where it came from.

    This reads declarations — C2PA assertions, generator tags, prompt records.
    It does not analyse pixels, because reliable pixel-level detection of
    AI-generated imagery is an unsolved problem and a plausible-looking guess
    would be worse than no answer.
    """
    ev: List[Evidence] = []
    blobs = _text_blobs(path, findings) if not is_video else \
        [(f.label, f.detail or "") for f in findings]
    haystack = " ".join(("%s %s" % (a, b)).lower() for a, b in blobs)
    try:
        raw = path.read_bytes()[:6_000_000].lower()
    except OSError:
        raw = b""

    # --- conclusive: an explicit machine-readable declaration ---------------
    if b"trainedalgorithmicmedia" in raw:
        ev.append(Evidence("declared", "C2PA assertion: trainedAlgorithmicMedia",
                           "the file states it was produced by a generative model"))
    if b"compositewithtrainedalgorithmicmedia" in raw:
        ev.append(Evidence("declared", "C2PA assertion: composite with AI media",
                           "part of this file was generated"))
    for f in findings:
        low = (f.label + " " + (f.detail or "")).lower()
        if "c2pa" in low and "manifest" in low:
            ev.append(Evidence("declared", "C2PA / Content Credentials manifest",
                               "signed provenance record is attached"))
            break

    for where, text in blobs:
        low = text.lower()
        for name in GENERATOR_NAMES:
            if name in low:
                ev.append(Evidence("declared", "Generator named in metadata",
                                   "%s → %s" % (where, text.strip()[:80])))
                break

    if not is_video:
        try:
            with Image.open(path) as im:
                for key, meaning in GENERATOR_PNG_KEYS.items():
                    for k in (im.info or {}):
                        if k.lower() == key:
                            ev.append(Evidence("declared", "Generation record embedded",
                                               "%s (%s)" % (meaning, k)))
                            break
        except Exception:
            pass

    # --- SynthID declaration ------------------------------------------------
    synthid = "declared" if (b"synthid" in raw or "synthid" in haystack) \
        else "not-declared"

    # --- camera evidence ----------------------------------------------------
    cam: List[str] = []
    if not is_video:
        try:
            with Image.open(path) as im:
                ex = im.getexif()
                for tag, name in CAMERA_EXIF_TAGS.items():
                    if ex.get(tag) not in (None, "", 0):
                        cam.append(name)
        except Exception:
            pass
    if len(cam) >= 3:
        ev.append(Evidence("camera", "Camera capture data present",
                           ", ".join(sorted(cam)[:6])))

    # --- circumstantial -----------------------------------------------------
    if width and height and (width, height) in GENERATOR_SIZES and not cam:
        ev.append(Evidence("circumstantial", "Dimensions typical of a generator",
                           "%dx%d, and no camera data" % (width, height)))
    if not is_video and not cam and not findings:
        ev.append(Evidence("circumstantial", "No metadata at all",
                           "consistent with a stripped or exported file"))

    declared = [e for e in ev if e.weight == "declared"]
    circumstantial = [e for e in ev if e.weight == "circumstantial"]
    camera = [e for e in ev if e.weight == "camera"]

    if declared:
        verdict = "ai-declared"
        headline = "AI-generated — the file says so itself"
        explain = ("This is a declaration carried inside the file, not a guess. "
                   "Removing metadata deletes the declaration but does not change "
                   "where the file came from.")
    elif len(circumstantial) >= 2:
        verdict = "ai-likely"
        headline = "Possibly AI-generated"
        explain = ("Nothing in the file declares its origin. This is circumstantial "
                   "only — plenty of ordinary images look like this too.")
    elif camera:
        verdict = "camera-like"
        headline = "Looks like a camera photograph"
        explain = ("Camera settings are recorded, which generators normally do not "
                   "write. EXIF can be copied from another file, so this is strong "
                   "but not proof.")
    else:
        verdict = "unknown"
        headline = "No origin markers found"
        explain = ("Nothing in this file says where it came from — in either "
                   "direction. This is the expected result for any file whose "
                   "metadata has been stripped, including by this tool.")

    note = ""
    if synthid == "declared":
        note = ("The metadata declares a SynthID pixel watermark. That watermark "
                "stays in the picture even after every byte of metadata is removed.")
    elif is_video or verdict == "ai-declared":
        note = ("No SynthID declaration found. That does not mean there is no "
                "watermark: a SynthID mark is invisible, survives metadata "
                "removal, and can only be confirmed by Google's own detector.")
    else:
        note = ("No SynthID declaration found. Only Google's SynthID Detector can "
                "confirm whether the pixels carry a mark.")

    return OriginReport(verdict, headline, explain, ev, synthid, note)


# --------------------------------------------------------------------------
# video: provenance inspection
# --------------------------------------------------------------------------

# ISOBMFF top-level uuid boxes used to carry provenance payloads.
ISOBMFF_UUIDS = {
    "d8fec3d61b0e483c92975828877ec481": "C2PA / Content Credentials manifest",
    "be7acfcb97a942e89c71999491e3afac": "XMP packet",
}

# Byte signatures worth reporting wherever they appear in the container.
PROVENANCE_SIGNATURES = [
    (b"c2pa", "C2PA claim data"),
    (b"jumb", "JUMBF box (C2PA container)"),
    (b"contentcredential", "Content Credentials"),
    (b"trainedalgorithmicmedia", "C2PA assertion: AI-generated"),
    (b"synthid", "SynthID reference"),
    (b"http://ns.adobe.com/xap", "XMP packet"),
]

# Container plumbing, not provenance — reported separately so the real
# findings do not get lost in noise.
BENIGN_TAGS = {"major_brand", "minor_version", "compatible_brands",
               "handler_name", "vendor_id", "language"}

# Muxer options that stop ffmpeg writing its own tags back in as it rebuilds
# the container. -bitexact suppresses version strings, -empty_hdlr_name blanks
# the handler names in mdia/minf.
FFMPEG_CLEAN_OPTS = ["-map_metadata", "-1", "-map_chapters", "-1",
                     "-bitexact", "-empty_hdlr_name", "1"]

ISO_CONTAINERS = {b"moov", b"udta", b"meta", b"ilst", b"trak", b"mdia",
                  b"minf", b"stbl", b"edts"}


@dataclass
class Finding:
    kind: str      # "box" | "tag" | "signature"
    label: str
    detail: str = ""
    nbytes: int = 0


def iter_iso_boxes(data: bytes, start: int = 0, end: Optional[int] = None,
                   depth: int = 0):
    """Walk ISOBMFF (mp4/mov) boxes, recursing into known container boxes."""
    end = len(data) if end is None else end
    i = start
    while i + 8 <= end:
        size = int.from_bytes(data[i:i + 4], "big")
        typ = data[i + 4:i + 8]
        hdr = 8
        if size == 1:
            if i + 16 > end:
                break
            size = int.from_bytes(data[i + 8:i + 16], "big")
            hdr = 16
        elif size == 0:
            size = end - i
        if size < hdr or i + size > end:
            break
        yield typ, i, size, hdr, depth
        if typ in ISO_CONTAINERS:
            body = i + hdr + (4 if typ == b"meta" else 0)
            for sub in iter_iso_boxes(data, body, i + size, depth + 1):
                yield sub
        i += size


def printable_runs(payload: bytes, minlen: int = 6, limit: int = 3) -> List[str]:
    """Readable strings out of a binary payload, for the log."""
    ok = set((string.ascii_letters + string.digits + " ._:/@{}\"'[],-").encode())
    runs, cur = [], bytearray()
    for b in payload:
        if b in ok:
            cur.append(b)
        else:
            if len(cur) >= minlen:
                runs.append(cur.decode("ascii", "replace"))
            cur = bytearray()
    if len(cur) >= minlen:
        runs.append(cur.decode("ascii", "replace"))
    runs.sort(key=len, reverse=True)
    return runs[:limit]


def ffprobe_info(path: Path) -> dict:
    """Container/stream info as a dict; {} if ffprobe cannot read the file."""
    try:
        proc = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json",
             "-show_format", "-show_streams", str(path)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120,
        )
        if proc.returncode != 0:
            return {}
        return json.loads(proc.stdout.decode("utf-8", "replace"))
    except Exception:
        return {}


def scan_video_provenance(path: Path, info: dict) -> Tuple[List[Finding], List[Finding]]:
    """
    Everything removable that identifies this file's origin.

    Returns (findings, benign) — findings are what gets stripped, benign is
    container plumbing that is rewritten regardless.
    """
    findings: List[Finding] = []
    benign: List[Finding] = []
    data = path.read_bytes()

    # ISOBMFF boxes (mp4/mov). Matroska/webm has no box structure to walk.
    if len(data) > 8 and data[4:8] == b"ftyp":
        for typ, off, size, hdr, _ in iter_iso_boxes(data):
            if typ == b"uuid":
                uid = data[off + hdr:off + hdr + 16].hex()
                label = ISOBMFF_UUIDS.get(uid, "unknown uuid box (%s)" % uid[:16])
                payload = data[off + hdr + 16:off + size]
                findings.append(Finding("box", label,
                                        " | ".join(printable_runs(payload)), size))
            elif typ in (b"udta", b"ilst", b"XMP_"):
                findings.append(Finding(
                    "box", "%s box" % typ.decode("ascii", "replace"), "", size))

    # Container and stream tags.
    for tag, value in (info.get("format", {}).get("tags", {}) or {}).items():
        target = benign if tag.lower() in BENIGN_TAGS else findings
        target.append(Finding("tag", "container tag %s" % tag, str(value)))
    for idx, stream in enumerate(info.get("streams", []) or []):
        for tag, value in (stream.get("tags", {}) or {}).items():
            target = benign if tag.lower() in BENIGN_TAGS else findings
            target.append(Finding("tag", "stream %d tag %s" % (idx, tag), str(value)))

    # Raw signature sweep — catches payloads in places we did not parse.
    low = data.lower()
    for sig, label in PROVENANCE_SIGNATURES:
        n = low.count(sig)
        if n:
            findings.append(Finding("signature", label,
                                    "%d occurrence(s) of %r" % (n, sig.decode())))
    return findings, benign


def has_synthid_marker(findings: Sequence[Finding]) -> bool:
    return any("synthid" in (f.label + f.detail).lower() for f in findings)


# --------------------------------------------------------------------------
# video: cleaning and re-encode
# --------------------------------------------------------------------------

def sanitize_iso_boxes(path: Path) -> Tuple[int, int]:
    """
    Blank the metadata boxes ffmpeg rebuilds anyway, in place.

    Boxes are converted to 'free' padding and their payload zeroed *without
    changing any box size*, so every chunk offset in stco/co64 stays valid and
    the file needs no reindexing. Also blanks the avc1 compressor-name field,
    which is where 'encoder = Lavc libx264' actually lives.

    Returns (boxes_neutralised, names_blanked).
    """
    data = bytearray(path.read_bytes())
    if len(data) < 8 or bytes(data[4:8]) != b"ftyp":
        return (0, 0)

    boxes = list(iter_iso_boxes(bytes(data)))
    n_boxes = 0
    for typ, off, size, hdr, _ in boxes:
        if typ in (b"udta", b"uuid") and size > hdr:
            data[off + 4:off + 8] = b"free"
            data[off + hdr:off + size] = b"\x00" * (size - hdr)
            n_boxes += 1

    # avc1/hvc1 compressorname: a 32-byte Pascal string inside stsd.
    n_names = 0
    for typ, off, size, hdr, _ in boxes:
        if typ != b"stsd":
            continue
        body = bytes(data[off:off + size])
        for marker in (b"Lavc", b"libx264", b"x264", b"AVC Coding"):
            pos = body.find(marker)
            if pos <= 0:
                continue
            start = pos - 1               # the Pascal length byte
            if 0 < start and start + 32 <= size:
                data[off + start:off + start + 32] = b"\x00" * 32
                n_names += 1
                break

    path.write_bytes(bytes(data))
    return (n_boxes, n_names)


def even(n: int) -> int:
    """yuv420p needs even dimensions."""
    n = int(round(n))
    return n - (n % 2)


def video_crop_filter(src_w: int, src_h: int, tw: int, th: int) -> Tuple[str, str]:
    """Centre-crop filter to reach the target aspect. Returns (filter, note)."""
    target = tw / float(th)
    cur = src_w / float(src_h)
    if abs(cur - target) < 1e-6:
        return "", "none"
    if cur > target:
        cw, ch, axis = even(src_h * target), even(src_h), "left/right"
    else:
        cw, ch, axis = even(src_w), even(src_w / target), "top/bottom"
    x, y = (src_w - cw) // 2, (src_h - ch) // 2
    return "crop=%d:%d:%d:%d" % (cw, ch, x, y), axis


def sample_frames(path: Path, count: int = 24) -> Optional["np.ndarray"]:
    """Decode `count` evenly spread frames as an (N, H, W, 3) uint8 array."""
    info = ffprobe_info(path)
    vs = [s for s in info.get("streams", []) if s.get("codec_type") == "video"]
    if not vs:
        return None
    w, h = int(vs[0]["width"]), int(vs[0]["height"])
    dur = float(info.get("format", {}).get("duration", 0) or 0)
    fps = max(0.5, count / dur) if dur > 0 else 2.0
    try:
        proc = subprocess.run(
            ["ffmpeg", "-v", "error", "-i", str(path), "-vf", "fps=%.4f" % fps,
             "-frames:v", str(count), "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=300)
        buf = proc.stdout
        n = len(buf) // (w * h * 3)
        if n < 2:
            return None
        return np.frombuffer(buf[:n * w * h * 3], dtype=np.uint8).reshape(n, h, w, 3)
    except Exception:
        return None


def _largest_blob(mask: "np.ndarray") -> Optional[Tuple[int, int, int, int]]:
    """Bounding box (x, y, w, h) of the biggest connected True region."""
    try:
        import cv2
    except ImportError:
        rows = np.where(mask.any(axis=1))[0]
        cols = np.where(mask.any(axis=0))[0]
        if not len(rows) or not len(cols):
            return None
        return (int(cols[0]), int(rows[0]),
                int(cols[-1]) - int(cols[0]) + 1, int(rows[-1]) - int(rows[0]) + 1)
    n, _, stats, _ = cv2.connectedComponentsWithStats(
        mask.astype(np.uint8), connectivity=8)
    if n <= 1:
        return None
    idx = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
    x, y, w, h, _ = stats[idx]
    return (int(x), int(y), int(w), int(h))


def detect_logo_candidates(frames: "np.ndarray") -> List[Tuple[str, Tuple[int, int, int, int], float]]:
    """
    Every corner that holds a plausible burned-in mark, best-scoring first.

    A mark barely changes between frames while the picture behind it moves, so
    it reads as a still, bright island in a frame corner. The brightness cut is
    taken per corner from that corner's own content, and a corner only counts
    if its still pixels form one small, compact blob.

    This CANNOT reliably tell a generator badge from a poster, caption or light
    fitting -- they are static and bright too. That is why every candidate is
    returned rather than one guess, and why acting on a guess needs consent.
    """
    g = frames.mean(axis=3).astype(np.float32)
    n, h, w = g.shape
    still = g.std(axis=0)
    mean_img = g.mean(axis=0)
    moving = float(np.median(still))
    if moving < 1.5:
        return []

    quiet = still < max(1.0, moving * 0.60)
    qh, qw = int(h * 0.22), int(w * 0.30)
    corners = {
        "top-left": (slice(0, qh), slice(0, qw)),
        "top-right": (slice(0, qh), slice(w - qw, w)),
        "bottom-left": (slice(h - qh, h), slice(0, qw)),
        "bottom-right": (slice(h - qh, h), slice(w - qw, w)),
    }

    out: List[Tuple[str, Tuple[int, int, int, int], float]] = []
    for name, (ys, xs) in corners.items():
        sub_mean = mean_img[ys, xs]
        thr = max(90.0, float(np.percentile(sub_mean, 90)))
        sub = quiet[ys, xs] & (sub_mean > thr)
        if sub.sum() < 40:
            continue
        blob = _largest_blob(sub)
        if blob is None:
            continue
        x0, y0, bw, bh = blob
        if bw < 8 or bh < 8:
            continue
        if bw > w * 0.22 or bh > h * 0.14:
            continue
        if max(bw, bh) / float(min(bw, bh)) > 2.5:
            continue
        if float(sub[y0:y0 + bh, x0:x0 + bw].mean()) < 0.10:
            continue
        mass = float(sub[y0:y0 + bh, x0:x0 + bw].sum())
        # Pad past the anti-aliased rim, or delogo leaves a halo (measured:
        # 24.6 dB unpadded vs 51.6 dB padded).
        pw, ph = max(6, int(bw * 0.25)), max(6, int(bh * 0.25))
        ax0, ay0 = max(0, xs.start + x0 - pw), max(0, ys.start + y0 - ph)
        ax1 = min(w, xs.start + x0 + bw + pw)
        ay1 = min(h, ys.start + y0 + bh + ph)
        out.append((name, (ax0, ay0, ax1 - ax0, ay1 - ay0), mass))

    out.sort(key=lambda c: -c[2])
    return out


def detect_logo_box(frames: "np.ndarray") -> Optional[Tuple[int, int, int, int]]:
    """Highest-scoring candidate, or None. See detect_logo_candidates."""
    c = detect_logo_candidates(frames)
    return c[0][1] if c else None


def write_detection_preview(src: Path, candidates, dst: Path) -> Optional[Path]:
    """Draw the candidate boxes on a frame so they can be eyeballed."""
    try:
        with tempfile.TemporaryDirectory() as td:
            shot = os.path.join(td, "f.png")
            subprocess.run(
                ["ffmpeg", "-hide_banner", "-v", "error", "-y", "-i", str(src),
                 "-vf", "select='eq(n\\,10)'", "-vframes", "1", shot],
                check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                timeout=300)
            im = Image.open(shot).convert("RGB")
        d = ImageDraw.Draw(im)
        for i, (name, (x, y, bw, bh), _) in enumerate(candidates):
            colour = (255, 40, 40) if i == 0 else (255, 190, 0)
            for k in range(3):
                d.rectangle([x - k, y - k, x + bw + k, y + bh + k], outline=colour)
            d.text((max(2, x), max(2, y - 12)),
                   "%d: %s %d:%d:%d:%d" % (i + 1, name, x, y, bw, bh), fill=colour)
        dst.parent.mkdir(parents=True, exist_ok=True)
        im.save(dst)
        return dst
    except Exception:
        return None


def border_detail(frames: "np.ndarray", box: Tuple[int, int, int, int]) -> float:
    """
    Mean gradient in the ring just outside the box.

    delogo rebuilds the box by interpolating inward from this ring, so the
    ring's busyness predicts the result: smooth ring -> clean fill, busy ring
    -> smear. Measured on fixtures: ~2 gave 51 dB, ~30 gave 15 dB.
    """
    x, y, bw, bh = box
    g = frames.mean(axis=3).astype(np.float32)
    n, h, w = g.shape
    m = 10
    x0, y0 = max(0, x - m), max(0, y - m)
    x1, y1 = min(w, x + bw + m), min(h, y + bh + m)
    ring = g[:, y0:y1, x0:x1]
    if ring.shape[1] < 4 or ring.shape[2] < 4:
        return 99.0
    gx = np.abs(np.diff(ring, axis=2)).mean()
    gy = np.abs(np.diff(ring, axis=1)).mean()
    return float((gx + gy) / 2.0)


def suggest_crop(src_w: int, src_h: int, box: Tuple[int, int, int, int],
                 tw: int, th: int) -> Optional[Tuple[int, int, int, int]]:
    """
    Largest crop at exactly tw:th that leaves `box` outside the frame.

    The ratio is reduced to its smallest integer form (1080x1920 -> 9:16) and
    the crop is built as a whole multiple of it, so the result is exact rather
    than a rounded approximation, and both sides stay even for yuv420p.
    """
    from math import gcd
    bx, by, bw, bh = box
    d = gcd(tw, th)
    uw, uh = tw // d, th // d                     # e.g. 9 and 16

    best = None
    # Four ways to exclude the box; each caps one dimension.
    for w_max, h_max, vertical in (
            (src_w, by, True),                     # finish above it
            (src_w, src_h - (by + bh), True),      # start below it
            (bx, src_h, False),                    # finish left of it
            (src_w - (bx + bw), src_h, False)):
        if w_max < 32 or h_max < 32:
            continue
        k = min(w_max // uw, h_max // uh)
        while k > 0 and ((uw * k) % 2 or (uh * k) % 2):
            k -= 1                                 # keep both sides even
        if k <= 0:
            continue
        w, h = uw * k, uh * k
        if w < 32 or h < 32:
            continue
        if best is None or w * h > best[0] * best[1]:
            best = (w, h, vertical)

    if best is None:
        return None
    w, h, vertical = best
    if vertical:
        y = 0 if by >= h else src_h - h
        x = max(0, min(src_w - w, (src_w - w) // 2))
    else:
        x = 0 if bx >= w else src_w - w
        y = max(0, min(src_h - h, (src_h - h) // 2))
    return (int(x), int(y), int(w), int(h))


def temporal_inpaint(src: Path, dst: Path, box: Tuple[int, int, int, int],
                     log: Log, indent: int, radius: int = 45,
                     topk: int = 3) -> Optional[dict]:
    """
    Fill the box using pixels the camera exposed in *other* frames.

    This is the mechanism behind ML video inpainters: estimate motion between
    frames, pull the hidden content from frames where it was visible, and only
    fall back to invention where it never was. Candidates are scored on how
    well they agree with the known ring around the hole, so a badly aligned or
    unexposed frame is rejected rather than averaged in.

    Returns a stats dict, or None if OpenCV is unavailable.
    """
    try:
        import cv2
    except ImportError:
        log.warn("--logo-method temporal needs OpenCV (pip install opencv-python)",
                 indent)
        return None

    x, y, bw, bh = box
    cap = cv2.VideoCapture(str(src))
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    W, H = int(cap.get(3)), int(cap.get(4))
    frames = []
    while True:
        ok, f = cap.read()
        if not ok:
            break
        frames.append(f)
    cap.release()
    n = len(frames)
    if n < 8:
        log.warn("clip too short for temporal inpainting (%d frames)" % n, indent)
        return None

    gray = [cv2.cvtColor(f, cv2.COLOR_BGR2GRAY).astype(np.float32) for f in frames]
    m = 18
    rx0, ry0 = max(0, x - m), max(0, y - m)
    rx1, ry1 = min(W, x + bw + m), min(H, y + bh + m)
    ring = np.ones((ry1 - ry0, rx1 - rx0), bool)
    ring[(y - ry0):(y - ry0) + bh, (x - rx0):(x - rx0) + bw] = False

    by0, by1 = (0, H // 2) if y > H // 2 else (H // 2, H)
    band = (slice(by0 + 20, by1 - 20), slice(20, W - 20))

    tmp = dst.with_suffix(".raw.avi")
    writer = cv2.VideoWriter(str(tmp), cv2.VideoWriter_fourcc(*"FFV1"), fps, (W, H))
    recovered = 0
    ring_psnr: List[float] = []
    for t in range(n):
        cands = []
        for k in [d for r in range(3, radius + 1) for d in (-r, r)]:
            s = t + k
            if s < 0 or s >= n:
                continue
            (dx, dy), _ = cv2.phaseCorrelate(gray[t][band], gray[s][band])
            if abs(dx) < bw * 0.5 and abs(dy) < bh * 0.5:
                continue                       # mark still covers its own content
            M = np.float32([[1, 0, -dx], [0, 1, -dy]])
            warped = cv2.warpAffine(frames[s], M, (W, H), flags=cv2.INTER_CUBIC,
                                    borderMode=cv2.BORDER_REFLECT)
            sx, sy = int(round(x - dx)), int(round(y - dy))
            if not (sx + bw < x or sx > x + bw or sy + bh < y or sy > y + bh):
                continue                       # source mark lands on our hole
            a = warped[ry0:ry1, rx0:rx1].astype(np.float32)
            b = frames[t][ry0:ry1, rx0:rx1].astype(np.float32)
            cands.append((float(np.mean((a[ring] - b[ring]) ** 2)),
                          warped[y:y + bh, x:x + bw].astype(np.float32)))
            if len(cands) >= topk * 3:
                break

        out = frames[t].copy()
        if cands:
            cands.sort(key=lambda c: c[0])
            best = np.median(np.stack([c[1] for c in cands[:topk]]), axis=0)
            out[y:y + bh, x:x + bw] = np.clip(best, 0, 255).astype(np.uint8)
            recovered += 1
            # Ring agreement of the best candidate predicts patch accuracy:
            # if the known pixels around the hole line up, the hole does too.
            ring_psnr.append(10 * np.log10(255.0 ** 2 / max(cands[0][0], 1e-6)))
        else:
            ring_psnr.append(0.0)
        writer.write(out)
    writer.release()

    if not tmp.exists() or tmp.stat().st_size == 0:
        return None
    frac = recovered / float(n)
    conf = np.array(ring_psnr) if ring_psnr else np.zeros(1)
    # Calibrated against ground truth: frames scoring >=18 dB here had
    # median true accuracy 40.1 dB; those below, 14.5 dB. r=0.81.
    good = int((conf >= 18).sum())
    gfrac = good / float(n)
    log.step("temporal inpaint: %d/%d frames rebuilt, %d (%.0f%%) with a strong "
             "match (median confidence %.1f dB)"
             % (recovered, n, good, gfrac * 100, float(np.median(conf))), indent)
    if gfrac < 0.80:
        log.warn("%.0f%% of frames had no well-matched source frame — the hidden "
                 "area was never exposed there. Those frames will still show the "
                 "mark or a soft patch; inspect the output."
                 % ((1 - gfrac) * 100), indent)
    return {"path": tmp, "frames": n, "recovered": recovered, "fraction": frac,
            "good": good, "good_fraction": gfrac}



PROPAINTER_DIR = ".propainter"
PROPAINTER_VENV = ".propainter-venv/bin/python"


def find_propainter(root: Path) -> Optional[Tuple[Path, Path]]:
    """Locate the ProPainter checkout and its venv interpreter."""
    for base in (root, Path(__file__).resolve().parent):
        repo = base / PROPAINTER_DIR
        py = base / PROPAINTER_VENV
        if (repo / "inference_propainter.py").exists() and py.exists():
            return (repo, py)
    return None


def propainter_inpaint(src: Path, box: Tuple[int, int, int, int], src_w: int,
                       src_h: int, fps: float, log: Log, indent: int,
                       root: Path) -> Optional[Path]:
    """
    Fill the box with ProPainter (flow-guided transformer video inpainting).

    Only a region around the mark is sent to the model, not the whole frame:
    it keeps memory and runtime sane and still gives the network the moving
    context it needs. ProPainter leaves everything outside the mask untouched
    (verified: mean change 0.00), so the region composites straight back.
    """
    found = find_propainter(root)
    if not found:
        log.warn("ProPainter not installed — expected %s/ and %s"
                 % (PROPAINTER_DIR, PROPAINTER_VENV), indent)
        return None
    repo, py = found

    x, y, bw, bh = box
    # Context margin around the mark, clamped to the frame and kept even.
    mx, my = max(80, bw), max(80, bh)
    rx0, ry0 = max(0, x - mx), max(0, y - my)
    rx1, ry1 = min(src_w, x + bw + mx), min(src_h, y + bh + my)
    rw, rh = even(rx1 - rx0), even(ry1 - ry0)
    if rw < bw + 8 or rh < bh + 8:
        log.warn("no room for context around the logo box", indent)
        return None

    tmp = Path(tempfile.mkdtemp(prefix="propainter_"))
    frames_dir = tmp / "frames"
    frames_dir.mkdir()
    try:
        run_cmd(
            ["ffmpeg", "-hide_banner", "-v", "error", "-y", "-i", str(src),
             "-vf", "crop=%d:%d:%d:%d" % (rw, rh, rx0, ry0),
             "-start_number", "0", str(frames_dir / "%05d.png")],
            check=True, timeout=1800)

        mask = Image.new("L", (rw, rh), 0)
        ImageDraw.Draw(mask).rectangle(
            [x - rx0, y - ry0, x - rx0 + bw, y - ry0 + bh], fill=255)
        mask_path = tmp / "mask.png"
        mask.save(mask_path)

        n_in = len(list(frames_dir.glob("*.png")))
        log.step("ProPainter: %d frames, %dx%d region around the mark — "
                 "this takes a few minutes" % (n_in, rw, rh), indent)

        proc = run_cmd(
            [str(py), "inference_propainter.py", "-i", str(frames_dir),
             "-m", str(mask_path), "-o", str(tmp / "out"),
             "--subvideo_length", "40", "--save_frames"],
            cwd=str(repo), timeout=7200)
        out_frames = tmp / "out" / "frames" / "frames"
        if proc.returncode != 0 or not out_frames.is_dir():
            tail = proc.stderr.decode("utf-8", "replace").strip().splitlines()
            log.warn("ProPainter failed: %s" % (tail[-1] if tail else "unknown"),
                     indent)
            return None

        n_out = len(list(out_frames.glob("*.png")))
        if n_out < n_in:
            log.warn("ProPainter returned %d of %d frames" % (n_out, n_in), indent)
            return None

        # Composite the region back over the untouched original, losslessly.
        dst = tmp / "inpainted.mkv"
        run_cmd(
            ["ffmpeg", "-hide_banner", "-v", "error", "-y", "-i", str(src),
             "-framerate", "%.6f" % (fps or 30.0), "-i", str(out_frames / "%04d.png"),
             "-filter_complex", "[0:v][1:v]overlay=%d:%d" % (rx0, ry0),
             "-an", "-c:v", "ffv1", str(dst)],
            check=True, timeout=3600)
        log.step("ProPainter: %d frames rebuilt, region composited back" % n_out,
                 indent)
        return dst
    except Exception as exc:
        log.warn("ProPainter: %s" % exc, indent)
        return None


def parse_box(spec: str) -> Tuple[int, int, int, int]:
    parts = spec.replace(",", ":").split(":")
    if len(parts) != 4:
        raise ValueError("expected x:y:w:h, got %r" % spec)
    x, y, bw, bh = (int(p) for p in parts)
    if bw <= 0 or bh <= 0:
        raise ValueError("width and height must be positive")
    return (x, y, bw, bh)


@dataclass
class VideoPlan:
    """One planned video output."""
    label: str          # "4:5", "9:16" or "native"
    key: str            # filename suffix key: "45", "916" or ""
    out_w: int
    out_h: int
    chain: List[str]
    note: str


def plan_video_outputs(src_w: int, src_h: int, fit: str,
                       formats: Sequence[str]) -> List[VideoPlan]:
    """
    Work out what to render.

    fit="none"  -> one output at the source aspect, scaled to fit inside the
                   1080x1920 Instagram box. Nothing is cut off.
    fit="pad"   -> one output per format, whole frame fitted inside the target
                   and padded with black. Nothing is cut off.
    fit="crop"  -> one output per format, centre-cropped to the target aspect.
                   This is the only mode that discards picture.
    """
    box_w, box_h = 1080, 1920

    if fit == "none":
        s = min(box_w / float(src_w), box_h / float(src_h))
        ow, oh = even(src_w * s), even(src_h * s)
        chain = ["scale=%d:%d:flags=lanczos" % (ow, oh)] if (ow, oh) != (src_w, src_h) else []
        return [VideoPlan("native", "", ow, oh, chain,
                          "no crop, no padding — full frame kept, %.2fx" % s)]

    plans: List[VideoPlan] = []
    for fmt in formats:
        tw, th = TARGETS[fmt]
        if fit == "pad":
            s = min(tw / float(src_w), th / float(src_h))
            iw, ih = even(src_w * s), even(src_h * s)
            chain = ["scale=%d:%d:flags=lanczos" % (iw, ih),
                     "pad=%d:%d:%d:%d:black" % (tw, th, (tw - iw) // 2, (th - ih) // 2)]
            note = "padded to %dx%d — full frame kept" % (tw, th)
        else:
            crop, axis = video_crop_filter(src_w, src_h, tw, th)
            chain = ([crop] if crop else []) + ["scale=%d:%d:flags=lanczos" % (tw, th)]
            note = "centre-cropped (%s)" % axis if crop else "no crop needed"
        plans.append(VideoPlan(FORMAT_LABEL[fmt], fmt, tw, th, chain, note))
    return plans


def build_ffmpeg_command(src: Path, dst: Path, chain: Sequence[str],
                         crf: int, has_audio: bool,
                         audio_src: Optional[Path] = None) -> List[str]:
    """audio_src carries the original soundtrack when the video was rebuilt
    frame-by-frame (the inpainted intermediate has no audio track)."""
    cmd = ["ffmpeg", "-hide_banner", "-v", "error", "-y", "-i", str(src)]
    if has_audio and audio_src is not None:
        cmd += ["-i", str(audio_src)]
    cmd += ["-map", "0:v:0"]
    if has_audio:
        cmd += ["-map", "1:a:0" if audio_src is not None else "0:a:0"]
    if chain:
        cmd += ["-vf", ",".join(chain)]
    cmd += [
        "-c:v", "libx264", "-profile:v", "high", "-pix_fmt", "yuv420p",
        "-crf", str(crf), "-preset", "slow",
        "-movflags", "+faststart",
    ] + FFMPEG_CLEAN_OPTS
    if has_audio:
        cmd += ["-c:a", "aac", "-b:a", "192k", "-ar", "48000"]
    cmd.append(str(dst))
    return cmd


def process_video(path: Path, input_root: Path, output_root: Path, args,
                  log: Log, stats: Stats, index: int, total: int) -> None:
    check_cancel()
    prefix = log.bold("[%d/%d]" % (index, total))
    log.info("")
    log.info("%s %s %s" % (prefix, log.bold(str(path.relative_to(input_root))),
                           log.dim_text("(video)")))

    info = ffprobe_info(path)
    if not info:
        raise RuntimeError("ffprobe could not read this file")

    vstreams = [s for s in info.get("streams", []) if s.get("codec_type") == "video"]
    astreams = [s for s in info.get("streams", []) if s.get("codec_type") == "audio"]
    if not vstreams:
        raise RuntimeError("no video stream found")
    v = vstreams[0]
    src_w, src_h = int(v["width"]), int(v["height"])
    duration = float(info.get("format", {}).get("duration", 0) or 0)
    fps_raw = v.get("avg_frame_rate", "0/1")
    try:
        num, den = fps_raw.split("/")
        fps = float(num) / float(den) if float(den) else 0.0
    except Exception:
        fps = 0.0

    log.step("source: %dx%d  %.2f fps  %.2fs  %s%s  %.0f KB"
             % (src_w, src_h, fps, duration, v.get("codec_name", "?"),
                "/" + astreams[0].get("codec_name", "?") if astreams else " (no audio)",
                path.stat().st_size / 1024.0), 6)

    findings, benign = scan_video_provenance(path, info)

    # --- report everything found, before touching it ---
    if findings:
        log.warn("provenance / credentials found in source (%d item(s)):"
                 % len(findings), 6)
        for f in findings:
            size = "  [%d bytes]" % f.nbytes if f.nbytes else ""
            log.info("          %-42s %s%s"
                     % (f.label, f.detail[:88], size), 1)
    else:
        log.step("no C2PA / XMP / provenance tags found in source", 6)
    for f in benign:
        log.step("container plumbing (rewritten): %s = %s" % (f.label, f.detail), 8)

    synthid = has_synthid_marker(findings)

    # --- optional watermark / logo removal -------------------------------
    logo_filter = ""
    encode_src = path
    if args.remove_logo or args.detect_logo:
        box: Optional[Tuple[int, int, int, int]] = None
        from_detection = False

        if args.remove_logo and args.remove_logo != "auto":
            box = parse_box(args.remove_logo)
            log.step("logo box (given): x=%d y=%d w=%d h=%d" % box, 6)
        else:
            frames = sample_frames(path)
            cands = detect_logo_candidates(frames) if frames is not None else []
            if not cands:
                log.warn("no static overlay detected — pass "
                         "--remove-logo x:y:w:h explicitly", 6)
            else:
                from_detection = True
                box = cands[0][1]
                log.warn("detection is a GUESS: posters, captions and lights "
                         "look just like a watermark to it. Check the preview "
                         "before letting anything paint.", 6)
                for i, (name, b, _) in enumerate(cands):
                    log.info("          %d: %-13s --remove-logo %d:%d:%d:%d%s"
                             % (i + 1, name, b[0], b[1], b[2], b[3],
                                "   <- would be used" if i == 0 else ""), 1)
                preview = write_detection_preview(
                    path, cands,
                    output_root / ("_detected_%s.png" % path.stem))
                if preview:
                    log.info("          preview: %s" % preview, 1)

        if box:
            frames = sample_frames(path)
            if frames is not None:
                detail = border_detail(frames, box)
                if detail < 6:
                    log.step("surrounding detail %.1f — clean fill expected" % detail, 6)
                elif detail < 15:
                    log.warn("surrounding detail %.1f — fill will be soft; "
                             "check the result" % detail, 6)
                else:
                    log.warn("surrounding detail %.1f — too busy for delogo to "
                             "reconstruct; use --logo-method propainter" % detail, 6)

            if args.suggest_crop:
                for fmt in (["45", "916"] if not args.format or args.format == "both"
                            else [args.format]):
                    tw, th = TARGETS[fmt]
                    sug = suggest_crop(src_w, src_h, box, tw, th)
                    if sug:
                        log.ok("%s: --video-crop %d:%d:%d:%d  (keeps %.0f%% of "
                               "width, ratio %.4f)"
                               % (FORMAT_LABEL[fmt], sug[0], sug[1], sug[2], sug[3],
                                  100.0 * sug[2] / src_w, sug[2] / float(sug[3])), 6)
                    else:
                        log.warn("%s: no crop at this ratio can exclude that box"
                                 % FORMAT_LABEL[fmt], 6)
                return

            if args.detect_logo and not args.remove_logo:
                log.step("--detect-logo only: nothing painted.", 6)
            elif from_detection and not args.accept_detected:
                # Painting the wrong region is destructive and slow to discover.
                # A guess never gets to act on its own.
                log.warn("NOT removing anything: the box above was guessed, not "
                         "given. Check the preview, then re-run with the correct "
                         "--remove-logo x:y:w:h — or --accept-detected to use "
                         "the guess as-is. Metadata and scaling still applied.", 6)
            else:
                removed = False
                if args.logo_method == "propainter" and not args.dry_run:
                    check_cancel()
                    res = propainter_inpaint(path, box, src_w, src_h, fps, log, 6,
                                             Path.cwd())
                    if res:
                        encode_src = res
                        removed = True
                    else:
                        log.warn("falling back to delogo", 6)
                elif args.logo_method == "temporal" and not args.dry_run:
                    tmp_base = Path(tempfile.gettempdir()) / ("_inpaint_%s" % os.getpid())
                    res = temporal_inpaint(path, tmp_base, box, log, 6)
                    if res:
                        encode_src = res["path"]
                        removed = True
                    else:
                        log.warn("temporal inpainting unavailable; using delogo", 6)
                if not removed:
                    logo_filter = "delogo=x=%d:y=%d:w=%d:h=%d" % box
                stats.logo_removed.append(str(path.relative_to(input_root)))

    formats, reason = route_formats(path, input_root, args.format)

    # An explicit crop changes the frame the rest of the plan works from, so
    # it is applied to the dimensions *before* output sizes are computed.
    crop_chain = None
    plan_w, plan_h = src_w, src_h
    if args.video_crop:
        cx, cy, cw, ch = parse_box(args.video_crop)
        if cx + cw > src_w or cy + ch > src_h:
            raise RuntimeError("--video-crop %s falls outside the %dx%d frame"
                               % (args.video_crop, src_w, src_h))
        log.step("crop: %dx%d at (%d,%d) — keeps %.0f%% of width, ratio %.4f "
                 "(9:16 = 0.5625)"
                 % (cw, ch, cx, cy, 100.0 * cw / src_w, cw / float(ch)), 6)
        crop_chain = "crop=%d:%d:%d:%d" % (cw, ch, cx, cy)
        plan_w, plan_h = cw, ch

    plans = plan_video_outputs(plan_w, plan_h, args.video_fit, formats)
    if crop_chain:
        for plan in plans:
            plan.chain.insert(0, crop_chain)
    if logo_filter:
        for plan in plans:
            plan.chain.insert(0, logo_filter)
    if args.video_fit == "none":
        log.step("fit: none — source aspect kept, no cropping "
                 "(--format ignored for video)", 6)
    else:
        log.step("format: %s (%s), fit=%s"
                 % (", ".join(FORMAT_LABEL[f] for f in formats), reason,
                    args.video_fit), 6)

    out_dir = resolve_out_dir(path, input_root, output_root, args.layout)
    log.step("output dir: %s" % out_dir, 6)
    if not args.dry_run:
        out_dir.mkdir(parents=True, exist_ok=True)

    multi_output = len(plans) > 1
    if args.layout == "flat":
        stats.counter += 1
        number = stats.counter
    else:
        number = 1

    for plan in plans:
        suffix = "_%s" % plan.key if (multi_output and plan.key) else ""
        out_path = out_dir / ("%s%d%s.mp4" % (args.video_prefix, number, suffix))
        cmd = build_ffmpeg_command(
            encode_src, out_path, plan.chain, args.video_crf, bool(astreams),
            audio_src=path if encode_src != path else None)

        if args.dry_run:
            log.step("%s: %dx%d -> %dx%d  (%s)  x264 crf%d "
                     "[dry-run, not written] -> %s"
                     % (plan.label, src_w, src_h, plan.out_w, plan.out_h,
                        plan.note, args.video_crf, out_path), 9)
            stats.panels += 1
            continue

        proc = run_cmd(cmd, timeout=3600)
        if proc.returncode != 0 or not out_path.exists():
            err = proc.stderr.decode("utf-8", "replace").strip().splitlines()
            raise RuntimeError("ffmpeg failed: %s" % (err[-1] if err else "unknown"))

        log.step("%s: %dx%d -> %dx%d  (%s)  x264 crf%d, re-encoded"
                 % (plan.label, src_w, src_h, plan.out_w, plan.out_h,
                    plan.note, args.video_crf), 9)

        n_boxes, n_names = sanitize_iso_boxes(out_path)
        if n_boxes or n_names:
            log.step("container sweep: blanked %d leftover box(es), %d encoder "
                     "name field(s)" % (n_boxes, n_names), 9)

        # --- verify what actually survived ---
        out_info = ffprobe_info(out_path)
        left, _ = scan_video_provenance(out_path, out_info)
        removed = len(findings) - len(left)
        if left:
            log.warn("still present after cleaning: %s"
                     % "; ".join("%s (%s)" % (f.label, f.detail[:40]) for f in left), 9)
        else:
            log.step("metadata: removed all %d item(s); output scan clean "
                     "(no C2PA/XMP/JUMBF/tags)" % removed, 9)
        log.ok("%s  (%.0f KB)" % (out_path, out_path.stat().st_size / 1024.0), 5)
        stats.panels += 1

    if encode_src != path:
        try:
            Path(encode_src).unlink()
        except OSError:
            pass

    if synthid:
        stats.synthid_files.append(str(path.relative_to(input_root)))
        log.warn("SynthID: this file declares a SynthID pixel watermark. "
                 "Container metadata above is gone, but the watermark itself is "
                 "embedded in the image data and is NOT removed by re-encoding.", 6)

    stats.videos += 1


# --------------------------------------------------------------------------
# format routing
# --------------------------------------------------------------------------

RE_916 = re.compile(r"(?<!\d)916(?!\d)|9x16|\breel\b|reels|\bstory\b|stories", re.I)
RE_45 = re.compile(r"(?<!\d)45(?!\d)|4x5|\bpost\b|posts|\bfeed\b", re.I)


def route_formats(path: Path, input_root: Path,
                  flag: Optional[str]) -> Tuple[List[str], str]:
    """Decide 4:5 / 9:16 / both. Returns (formats, reason)."""
    if flag:
        fmts = ["45", "916"] if flag == "both" else [flag]
        return fmts, "--format %s" % flag

    stem = path.stem
    hit_916 = RE_916.search(stem)
    hit_45 = RE_45.search(stem)
    if hit_916 and not hit_45:
        return ["916"], "filename matched %r" % hit_916.group(0)
    if hit_45 and not hit_916:
        return ["45"], "filename matched %r" % hit_45.group(0)
    if hit_916 and hit_45:
        return ["45", "916"], "filename matched both tokens"

    try:
        parts = [p.lower() for p in path.relative_to(input_root).parent.parts]
    except ValueError:
        parts = []
    if any(p in ("reels", "reel", "stories", "story") for p in parts):
        return ["916"], "input subfolder"
    if any(p in ("posts", "post", "feed") for p in parts):
        return ["45"], "input subfolder"

    return ["45", "916"], "default"


# --------------------------------------------------------------------------
# per-file processing
# --------------------------------------------------------------------------

@dataclass
class Stats:
    grids: int = 0
    videos: int = 0
    panels: int = 0
    counter: int = 0  # running panel number, used by --layout flat
    failures: List[Tuple[str, str]] = field(default_factory=list)
    skipped: List[str] = field(default_factory=list)
    used_dirs: Dict[str, str] = field(default_factory=dict)
    synthid_files: List[str] = field(default_factory=list)
    logo_removed: List[str] = field(default_factory=list)


def resolve_out_dir(path: Path, input_root: Path, output_root: Path,
                    layout: str) -> Path:
    """Output folder for one grid, per --layout."""
    if layout == "flat":
        return output_root
    if layout == "mirror":
        return output_root / path.relative_to(input_root).parent / path.stem
    return output_root / path.stem  # "grid" (default)




VERDICT_STYLE = {
    "ai-declared": ("AI-GENERATED", "warn"),
    "ai-likely": ("POSSIBLY AI", "warn"),
    "camera-like": ("CAMERA PHOTO", "ok"),
    "unknown": ("UNKNOWN", "step"),
}


def report_origin(path: Path, input_root: Path, log: Log, index: int,
                  total: int) -> None:
    """Print the AI-origin and SynthID assessment for one file."""
    prefix = log.bold("[%d/%d]" % (index, total))
    log.info("")
    log.info("%s %s" % (prefix, log.bold(str(path.relative_to(input_root)))))

    is_video = path.suffix.lower() in VIDEO_EXTS
    w = h = 0
    if is_video:
        probe = ffprobe_info(path)
        vs = [s for s in probe.get("streams", []) if s.get("codec_type") == "video"]
        if vs:
            w, h = int(vs[0]["width"]), int(vs[0]["height"])
        findings, _ = scan_video_provenance(path, probe) if probe else ([], [])
    else:
        try:
            with Image.open(path) as im:
                w, h = im.width, im.height
        except Exception as exc:
            raise RuntimeError("not readable: %s" % exc)
        findings = inspect_image_metadata(path)

    rep = assess_origin(path, findings, w, h, is_video)
    label, style = VERDICT_STYLE.get(rep.verdict, ("UNKNOWN", "step"))
    line = "%-14s %s" % (label, rep.headline)
    getattr(log, "warn" if style == "warn" else "ok" if style == "ok" else "step")(line, 6)
    log.info("          %s" % rep.explain, 1)

    if rep.evidence:
        log.info("")
        log.info("          %s" % log.bold("evidence"), 1)
        for e in rep.evidence:
            tag = {"declared": "conclusive  ", "circumstantial": "circumstantial",
                   "camera": "camera      "}[e.weight]
            log.info("          %s  %-42s %s" % (tag, e.label, e.detail[:60]), 1)
    else:
        log.info("          (no evidence either way)", 1)

    log.info("")
    if rep.synthid == "declared":
        log.warn("SynthID: DECLARED in metadata", 6)
    else:
        log.step("SynthID: no declaration found", 6)
    log.info("          %s" % rep.synthid_note, 1)


def process_image_clean_only(path: Path, input_root: Path, output_root: Path,
                             args, log: Log, stats: Stats, index: int,
                             total: int) -> None:
    """Report and strip metadata. The picture itself is left exactly alone."""
    check_cancel()
    prefix = log.bold("[%d/%d]" % (index, total))
    log.info("")
    log.info("%s %s %s" % (prefix, log.bold(str(path.relative_to(input_root))),
                           log.dim_text("(clean only)")))
    try:
        with Image.open(path) as im:
            w, h, fmt, mode = im.width, im.height, im.format, im.mode
    except Exception as exc:
        raise RuntimeError("not a readable image: %s" % exc)
    log.step("source: %dx%d  %s  %s  %.0f KB"
             % (w, h, fmt, mode, path.stat().st_size / 1024.0), 6)

    findings = inspect_image_metadata(path)
    if findings:
        log.warn("metadata found (%d item(s)):" % len(findings), 6)
        for f in findings:
            size = "  [%d bytes]" % f.nbytes if f.nbytes else ""
            log.info("          %-42s %s%s" % (f.label, f.detail[:70], size), 1)
    else:
        log.ok("already clean — no metadata found", 6)

    if args.layout == "flat":
        out_dir = output_root
    else:
        out_dir = output_root / path.relative_to(input_root).parent
    out_path = out_dir / path.name

    if args.dry_run:
        log.step("[dry-run, not written] -> %s" % out_path, 9)
        stats.panels += 1
        stats.grids += 1
        return

    res = clean_image_file(path, out_path)
    for note in res["notes"]:
        log.warn(note, 9)
    left = inspect_image_metadata(out_path)
    # Signature hits inside compressed pixel data are coincidence, not metadata.
    left = [f for f in left if f.kind != "signature"]
    log.step("%s  %.0f KB -> %.0f KB" % (res["mode"], res["before"] / 1024.0,
                                         res["after"] / 1024.0), 9)
    if left:
        log.warn("still present: %s" % ", ".join(f.label for f in left), 9)
    else:
        log.step("verified clean (no EXIF/XMP/IPTC/ICC/C2PA records)", 9)

    with Image.open(out_path) as im2:
        if (im2.width, im2.height) != (w, h):
            log.warn("dimensions changed! %dx%d -> %dx%d" % (w, h, im2.width, im2.height), 9)
        else:
            log.step("pixels untouched: still %dx%d" % (im2.width, im2.height), 9)

    log.ok("%s" % out_path, 5)
    stats.panels += 1
    stats.grids += 1


def process_grid(path: Path, input_root: Path, output_root: Path, args,
                 up: Upscaler, log: Log, stats: Stats, index: int, total: int) -> None:
    check_cancel()
    prefix = log.bold("[%d/%d]" % (index, total))
    log.info("")
    log.info("%s %s" % (prefix, log.bold(str(path.relative_to(input_root)))))

    with Image.open(path) as raw:
        raw.load()
        src_w, src_h = raw.size
        src_mode = raw.mode
        img = to_srgb(raw, log, 6)

    log.step("source: %dx%d  mode=%s  %.0f KB"
             % (src_w, src_h, src_mode, path.stat().st_size / 1024.0), 6)

    arr = np.asarray(img, dtype=np.float32)
    gray = arr.mean(axis=2)

    rows = detect_axis(gray, 0, args.white_threshold, args.var_threshold,
                       args.min_gutter, args.gutter_tolerance)
    cols = detect_axis(gray, 1, args.white_threshold, args.var_threshold,
                       args.min_gutter, args.gutter_tolerance)

    for split, label in ((rows, "horizontal"), (cols, "vertical")):
        if split.detected:
            log.step("gutter %s: rows/cols %d-%d (%dpx wide), content span %d-%d"
                     % (label, split.lo_end, split.hi_start,
                        split.hi_start - split.lo_end,
                        split.content_start, split.content_end), 6)
        else:
            log.warn("no %s gutter detected — FALLING BACK TO 50/50 SPLIT at %d "
                     "(panels may be misaligned; try lowering --white-threshold "
                     "or raising --var-threshold)" % (label, split.lo_end), 6)

    formats, reason = route_formats(path, input_root, args.format)
    log.step("format: %s (%s)"
             % (", ".join(FORMAT_LABEL[f] for f in formats), reason), 6)

    # Panel boxes in (left, upper, right, lower), reading order.
    boxes = [
        (cols.content_start, rows.content_start, cols.lo_end, rows.lo_end),
        (cols.hi_start, rows.content_start, cols.content_end, rows.lo_end),
        (cols.content_start, rows.hi_start, cols.lo_end, rows.content_end),
        (cols.hi_start, rows.hi_start, cols.content_end, rows.content_end),
    ]

    out_dir = resolve_out_dir(path, input_root, output_root, args.layout)
    log.step("output dir: %s" % out_dir, 6)

    # Two grids sharing a stem in different input subfolders would overwrite
    # each other under --layout grid; --layout mirror keeps them apart.
    if args.layout == "grid":
        key = str(out_dir)
        previous = stats.used_dirs.get(key)
        if previous:
            log.warn("output folder %s was already used by %s — files will be "
                     "overwritten; use --layout mirror to keep them separate"
                     % (out_dir.name, previous), 6)
        stats.used_dirs[key] = str(path.relative_to(input_root))

    if not args.dry_run:
        out_dir.mkdir(parents=True, exist_ok=True)

    # Ratio suffix is only needed when both ratios land in the same folder.
    multi_format = len(formats) > 1

    for pi, box in enumerate(boxes):
        name = PANEL_NAMES[pi]
        left, upper, right, lower = box
        if right - left < 16 or lower - upper < 16:
            log.warn("%s: slice is degenerate (%dx%d), skipping panel"
                     % (name, right - left, lower - upper), 6)
            continue

        if args.layout == "flat":
            stats.counter += 1
            number = stats.counter
        else:
            number = pi + 1

        panel = img.crop(box)
        sliced_w, sliced_h = panel.size

        parr = np.asarray(panel, dtype=np.float32)
        t, b, l, r = trim_white_edges(parr, args.white_threshold, args.var_threshold)
        if t or b or l or r:
            panel = panel.crop((l, t, sliced_w - r, sliced_h - b))
        trimmed_w, trimmed_h = panel.size

        log.info("     %s (%s)  sliced %dx%d  trim(t%d b%d l%d r%d) -> %dx%d"
                 % (log.bold("%s%d" % (args.name_prefix, number)), name,
                    sliced_w, sliced_h, t, b, l, r, trimmed_w, trimmed_h), 1)

        for fmt in formats:
            tw, th = TARGETS[fmt]
            cropped, axis_trimmed = centre_crop(panel, tw, th)
            cw, ch = cropped.size
            factor = max(tw / float(cw), th / float(ch))

            suffix = "_%s" % fmt if multi_format else ""
            out_path = out_dir / ("%s%d%s.jpg" % (args.name_prefix, number, suffix))

            if args.dry_run:
                log.step("%s: crop %dx%d (cut %s) -> %dx%d  %.2fx via %s "
                         "[dry-run, not written] -> %s"
                         % (FORMAT_LABEL[fmt], cw, ch, axis_trimmed, tw, th,
                            factor, up.kind, out_path), 9)
                stats.panels += 1
                continue

            final, method = upscale(cropped, tw, th, up, log, 9)
            final = strip_metadata(final)
            final.save(out_path, format="JPEG", quality=95, subsampling=0,
                       optimize=True, progressive=False)

            leftover = jpeg_metadata_segments(out_path)
            log.step("%s: crop %dx%d (cut %s) -> %dx%d  %.2fx via %s"
                     % (FORMAT_LABEL[fmt], cw, ch, axis_trimmed, tw, th,
                        factor, method), 9)
            if leftover:
                log.warn("metadata still present in output: %s"
                         % ", ".join(sorted(set(leftover))), 9)
            else:
                log.step("metadata: clean (no EXIF/XMP/IPTC/ICC/C2PA segments)", 9)
            log.ok("%s  (%.0f KB)" % (out_path, out_path.stat().st_size / 1024.0), 5)
            stats.panels += 1

    stats.grids += 1


# --------------------------------------------------------------------------
# cli
# --------------------------------------------------------------------------

def collect_inputs(root: Path) -> Tuple[List[Path], List[str]]:
    files: List[Path] = []
    skipped: List[str] = []
    for p in sorted(root.rglob("*")):
        if p.is_dir() or p.name.startswith("."):
            continue
        if p.suffix.lower() in IMAGE_EXTS or p.suffix.lower() in VIDEO_EXTS:
            files.append(p)
        else:
            skipped.append(str(p.relative_to(root)))
    return files, skipped


def have_ffmpeg() -> bool:
    return bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="split_grids.py",
        description="Prep images and videos for Instagram: lossless metadata strip, 2x2 grid split to 4:5 / 9:16, video clean/scale, watermark detect and inpaint.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""examples:
  # images: split 2x2 grids into Instagram panels
  python split_grids.py ./grids ./out
  python split_grids.py ./grids ./out --format 916 --layout flat

  # video: clean metadata, keep the source aspect, no cropping
  python split_grids.py ./clips ./out

  # video: find a burned-in watermark and report whether it can be removed
  python split_grids.py ./clips ./out --detect-logo --dry-run

  # video: remove the watermark (best quality)
  python split_grids.py ./clips ./out --remove-logo 566:1127:66:68 \\
      --logo-method propainter

  # video: crop the watermark out of frame instead, staying exactly 9:16
  python split_grids.py ./clips ./out --remove-logo 566:1127:66:68 --suggest-crop
  python split_grids.py ./clips ./out --video-crop 45:0:630:1120
""",
    )
    p.add_argument("input", type=Path, help="folder of images and/or videos (searched recursively)")
    p.add_argument("output", type=Path, help="folder to write results into")
    p.add_argument("--format", choices=["45", "916", "both"], default=None,
                   help="force output ratio; overrides filename and folder routing")
    p.add_argument("--layout", choices=["grid", "flat", "mirror"], default="grid",
                   help="output structure: 'grid' = OUT/<grid-name>/image1.jpg "
                        "(default); 'flat' = OUT/image1.jpg with numbering "
                        "continuing across the batch; 'mirror' = also reproduce "
                        "the input subfolders, so same-named grids in reels/ and "
                        "posts/ stay separate")
    p.add_argument("--name-prefix", default="image", metavar="PREFIX",
                   help="output filename prefix (default: image -> image1.jpg)")
    p.add_argument("--check-origin", action="store_true",
                   help="report whether each file declares that it is AI-generated, "
                        "and whether it declares a SynthID watermark. Reads only; "
                        "writes nothing")
    p.add_argument("--clean-only", action="store_true",
                   help="only inspect and strip metadata: no splitting, no "
                        "cropping, no resizing, no re-encoding. JPEG and PNG are "
                        "rewritten losslessly so the picture is bit-identical")
    p.add_argument("--dry-run", action="store_true",
                   help="analyse and log everything without writing any file")
    p.add_argument("--quiet", action="store_true",
                   help="suppress per-file output (errors and summary still shown)")
    p.add_argument("--no-color", action="store_true", help="disable ANSI colour")

    g = p.add_argument_group("gutter detection")
    g.add_argument("--white-threshold", type=float, default=243.0,
                   help="mean brightness (0-255) for a line to count as gutter "
                        "(default: 243)")
    g.add_argument("--var-threshold", type=float, default=6.0,
                   help="max std-dev along a line for it to count as gutter "
                        "(default: 6)")
    g.add_argument("--min-gutter", type=int, default=4,
                   help="minimum gutter thickness in px (default: 4)")
    g.add_argument("--gutter-tolerance", type=float, default=0.20,
                   help="how far from centre a gutter may sit, as a fraction of "
                        "the axis length (default: 0.20)")

    v = p.add_argument_group("video (requires ffmpeg)")
    v.add_argument("--remove-logo", default=None, metavar="X:Y:W:H",
                   help="remove a burned-in watermark/logo from the given box "
                        "using ffmpeg delogo; pass 'auto' to detect it. Quality "
                        "depends entirely on what is behind the mark — keep the "
                        "box snug, a larger box gives a worse result")
    v.add_argument("--logo-method",
                   choices=["delogo", "temporal", "propainter"], default="delogo",
                   help="how to fill the box. 'delogo' (default) interpolates "
                        "from the border: instant, invisible over smooth areas, "
                        "a blurred smear over detail. 'temporal' recovers real "
                        "pixels from frames where the camera exposed the area; "
                        "needs motion behind the mark. 'propainter' is ML video "
                        "inpainting and gives the best result on any background, "
                        "but needs the .propainter install and takes minutes")
    v.add_argument("--accept-detected", action="store_true",
                   help="allow an auto-detected box to be painted without you "
                        "confirming it. Off by default: detection cannot tell a "
                        "watermark from a poster or caption, and painting the "
                        "wrong region is destructive")
    v.add_argument("--detect-logo", action="store_true",
                   help="locate a static overlay and report the box (and whether "
                        "removal will look clean) without removing it")
    v.add_argument("--video-crop", default=None, metavar="X:Y:W:H",
                   help="explicit crop applied before scaling, e.g. to cut a "
                        "corner watermark out of frame. Use --suggest-crop to "
                        "have the box computed for you")
    v.add_argument("--suggest-crop", action="store_true",
                   help="print the largest crop at the target ratio that excludes "
                        "--remove-logo's box, then exit without writing")
    v.add_argument("--video-fit", choices=["none", "pad", "crop"], default="none",
                   help="how video is framed: 'none' (default) keeps the source "
                        "aspect and crops nothing, scaling to fit 1080x1920; "
                        "'pad' letterboxes to the target ratio; 'crop' "
                        "centre-crops to the target ratio and does discard picture")
    v.add_argument("--video-crf", type=int, default=18, metavar="N",
                   help="x264 quality for video output, lower is better "
                        "(default: 18)")
    v.add_argument("--video-prefix", default="video", metavar="PREFIX",
                   help="output filename prefix for video (default: video)")

    u = p.add_argument_group("upscaling")
    u.add_argument("--realesrgan-bin", default=None,
                   help="explicit path to a Real-ESRGAN executable")
    u.add_argument("--realesrgan-model", default="realesrgan-x4plus",
                   help="model name passed to -n (default: realesrgan-x4plus); "
                        "if unavailable, a model found in the models folder is used")
    u.add_argument("--realesrgan-models", default=None, metavar="DIR",
                   help="models folder passed to -m; auto-detected next to the "
                        "binary when not given")
    u.add_argument("--realesrgan-scale", type=int, default=4,
                   help="native scale passed to -s (default: 4)")
    u.add_argument("--no-realesrgan", action="store_true",
                   help="force the Lanczos + unsharp path")
    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    log = Log(quiet=args.quiet, color=not args.no_color)
    started = time.perf_counter()

    input_root: Path = args.input.expanduser().resolve()
    output_root: Path = args.output.expanduser().resolve()

    if not input_root.is_dir():
        log.error("input folder does not exist: %s" % input_root)
        return 2
    if output_root == input_root:
        log.error("output folder must differ from the input folder")
        return 2

    files, skipped = collect_inputs(input_root)
    stats = Stats(skipped=skipped)

    log.header("split_grids — images + video -> Instagram-ready frames")
    log.info("input : %s" % input_root)
    log.info("output: %s%s" % (output_root, "  (DRY RUN — nothing written)"
                               if args.dry_run else ""))
    log.info("found : %d file(s), %d unsupported file(s) skipped"
             % (len(files), len(skipped)))

    up = detect_upscaler(log, args.realesrgan_bin, args.no_realesrgan,
                         args.realesrgan_model, args.realesrgan_scale,
                         args.realesrgan_models)
    if up.kind == "realesrgan":
        log.ok("upscaler: Real-ESRGAN (%s, model=%s, x%d%s), Lanczos fit to exact size"
               % (up.binary, up.model, up.native_scale,
                  ", models=%s" % up.models_dir if up.models_dir else ""))
    else:
        log.warn("upscaler: Real-ESRGAN unavailable (%s) — using Lanczos + light "
                 "unsharp mask" % up.detail)

    if not files:
        log.warn("no supported files found (%s)" % ", ".join(sorted(IMAGE_EXTS | VIDEO_EXTS)))

    videos = [f for f in files if f.suffix.lower() in VIDEO_EXTS]
    ffmpeg_ok = have_ffmpeg()
    if videos and not ffmpeg_ok:
        log.error("%d video file(s) found but ffmpeg/ffprobe are not installed "
                  "— install with 'brew install ffmpeg'. Videos will be skipped."
                  % len(videos))
    elif videos:
        log.info("video  : %d file(s), ffmpeg present" % len(videos))

    for i, path in enumerate(files, start=1):
        try:
            if args.check_origin:
                report_origin(path, input_root, log, i, len(files))
                stats.grids += 1
                continue
            if args.clean_only and path.suffix.lower() in IMAGE_EXTS:
                process_image_clean_only(path, input_root, output_root, args,
                                         log, stats, i, len(files))
                continue
            if path.suffix.lower() in VIDEO_EXTS:
                if not ffmpeg_ok:
                    stats.failures.append((str(path.relative_to(input_root)),
                                           "ffmpeg not installed"))
                    continue
                process_video(path, input_root, output_root, args, log, stats,
                              i, len(files))
                continue
            process_grid(path, input_root, output_root, args, up, log, stats, i, len(files))
        except KeyboardInterrupt:
            log.error("interrupted by user")
            break
        except Exception as exc:
            rel = str(path.relative_to(input_root))
            stats.failures.append((rel, "%s: %s" % (type(exc).__name__, exc)))
            log.error("%s — %s: %s" % (rel, type(exc).__name__, exc))

    elapsed = time.perf_counter() - started
    print("")
    print(log.bold("summary"))
    print("  grids processed : %d" % stats.grids)
    print("  videos cleaned  : %d" % stats.videos)
    print("  outputs written : %d%s" % (stats.panels, " (dry run)" if args.dry_run else ""))
    print("  non-images      : %d" % len(stats.skipped))
    print("  warnings        : %d" % log.warnings)
    print("  failures        : %d" % len(stats.failures))
    for rel, msg in stats.failures:
        print("      %s — %s" % (rel, msg))
    print("  upscaler        : %s" % ("Real-ESRGAN + Lanczos fit"
                                      if up.kind == "realesrgan"
                                      else "Lanczos + unsharp mask"))
    print("  logo removed    : %d video(s)" % len(stats.logo_removed))
    print("  total time      : %.2fs" % elapsed)

    if stats.synthid_files:
        print("")
        print(log.bold("  NOT REMOVED — SynthID pixel watermark"))
        print("  Container metadata (C2PA, XMP, tags) was stripped and verified,")
        print("  but SynthID is embedded in the picture itself and survives")
        print("  re-encoding, cropping and scaling. These files still carry it:")
        for rel in stats.synthid_files:
            print("      %s" % rel)

    return 1 if stats.failures else 0


if __name__ == "__main__":
    sys.exit(main())

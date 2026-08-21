#!/usr/bin/env python3
"""
Framewipe — local browser front-end for split_grids.py.

Everything runs on this machine: files are processed in a temporary folder and
nothing is uploaded anywhere. Start it with the launcher (Framewipe.command) or
directly:

    python3 webapp.py
"""

from __future__ import annotations

import io
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
import uuid
import webbrowser
import zipfile
from pathlib import Path

try:
    from flask import Flask, jsonify, request, send_file, Response, abort
except ImportError:
    sys.exit(
        "Flask is missing. Create a venv and install requirements:\n\n"
        "    python3 -m venv .venv\n"
        "    .venv/bin/python -m pip install -r requirements.txt\n"
        "    .venv/bin/python webapp.py\n"
    )

import split_grids as sg

APP_NAME = "Framewipe"
APP_ROOT = Path(__file__).resolve().parent
WORK = Path(tempfile.gettempdir()) / "framewipe"
OLD_WORK = Path(tempfile.gettempdir()) / "cleanroom"
WORK.mkdir(exist_ok=True)

JOB_TTL_SEC = 2 * 60 * 60  # 2 hours
CAPS_TTL_SEC = 5 * 60
SUPPORTED_EXTS = set(sg.IMAGE_EXTS) | set(sg.VIDEO_EXTS)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 4 * 1024 * 1024 * 1024  # 4 GB per request
app.config["PROPAGATE_EXCEPTIONS"] = False

JOBS: dict = {}  # job id -> {"path": Path, "created": float}
RUNS: dict = {}  # run id -> status dict

_CAPS_LOCK = threading.Lock()
_CAPS = {"data": None, "ts": 0.0, "probed": False}


# --------------------------------------------------------------------------
# env
# --------------------------------------------------------------------------

def _load_dotenv() -> None:
    """Tiny .env loader. No extra dependency. Does not override existing env."""
    path = APP_ROOT / ".env"
    if not path.is_file():
        return
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip("'").strip('"')
        if key and key not in os.environ:
            os.environ[key] = val


_load_dotenv()


def env_port(default: int = 8765) -> int:
    raw = (os.environ.get("FRAMEWIPE_PORT") or str(default)).strip()
    try:
        port = int(raw)
    except ValueError:
        return default
    if 1 <= port <= 65535:
        return port
    return default


def site_url() -> str:
    return (os.environ.get("FRAMEWIPE_SITE_URL") or "").strip().rstrip("/")


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def human(n: float) -> str:
    if n < 1024:
        return "%d B" % n
    for unit in ("KB", "MB", "GB"):
        n /= 1024.0
        if n < 1024 or unit == "GB":
            return "%.1f %s" % (n, unit) if n < 10 else "%.0f %s" % (n, unit)
    return "%.0f GB" % n


def unique_dest(folder: Path, name: str, used: set) -> str:
    """Pick a filename that does not collide with an existing or in-batch name."""
    base = os.path.basename(name) or "upload"
    stem, ext = os.path.splitext(base)
    candidate = base
    n = 1
    while candidate.lower() in used or (folder / candidate).exists():
        candidate = "%s_%d%s" % (stem, n, ext)
        n += 1
    used.add(candidate.lower())
    return candidate


def safe_under(base: Path, rel: str) -> Path:
    """Resolve rel against base; raise ValueError if it escapes."""
    root = base.resolve()
    target = (root / rel).resolve()
    try:
        target.relative_to(root)
    except ValueError:
        raise ValueError("path escapes job directory")
    return target


def reap_jobs() -> None:
    """Drop job records and temp dirs older than JOB_TTL_SEC."""
    now = time.time()
    dead = []
    for jid, entry in list(JOBS.items()):
        created = entry.get("created") or 0
        path = entry.get("path")
        age = now - created
        if path is not None:
            try:
                age = max(age, now - path.stat().st_mtime)
            except OSError:
                age = JOB_TTL_SEC + 1
        if age > JOB_TTL_SEC:
            if path is not None:
                shutil.rmtree(path, ignore_errors=True)
            dead.append(jid)
    for jid in dead:
        JOBS.pop(jid, None)
    for root in (WORK, OLD_WORK):
        if not root.is_dir():
            continue
        try:
            children = list(root.iterdir())
        except OSError:
            continue
        live = {entry["path"].resolve() for entry in JOBS.values()
                if entry.get("path") is not None}
        for p in children:
            if not p.is_dir():
                continue
            try:
                if p.resolve() in live:
                    continue
                if now - p.stat().st_mtime > JOB_TTL_SEC:
                    shutil.rmtree(p, ignore_errors=True)
            except OSError:
                continue


def delete_job(job_id: str) -> bool:
    entry = JOBS.pop(job_id, None)
    if not entry:
        return False
    path = entry.get("path")
    if path is not None:
        shutil.rmtree(path, ignore_errors=True)
    for run in RUNS.values():
        if run.get("job") == job_id and run.get("status") == "running":
            ev = run.get("cancel")
            if ev is not None:
                ev.set()
            sg.kill_active_subprocesses()
    return True


def save_uploads(files):
    """Save uploads under a new job dir. Returns (job, saved_names, rejected)."""
    job = WORK / uuid.uuid4().hex[:12]
    (job / "in").mkdir(parents=True)
    (job / "out").mkdir(parents=True)
    used = set()
    saved = []
    rejected = []
    for f in files:
        name = os.path.basename(f.filename or "")
        if not name:
            continue
        ext = Path(name).suffix.lower()
        if ext not in SUPPORTED_EXTS:
            rejected.append(name)
            continue
        dest = unique_dest(job / "in", name, used)
        f.save(str(job / "in" / dest))
        saved.append(dest)
    return job, saved, rejected


def findings_json(items) -> list:
    return [{"label": f.label, "detail": f.detail, "bytes": f.nbytes,
             "kind": f.kind} for f in items]


def inspect_any(path: Path) -> dict:
    """Metadata report plus AI-origin assessment for one file."""
    ext = path.suffix.lower()
    size = path.stat().st_size
    info = {"name": path.name, "size": size, "size_h": human(size), "ext": ext}
    is_video = ext in sg.VIDEO_EXTS
    found = []
    w = h = 0

    if is_video:
        info["kind"] = "video"
        probe = sg.ffprobe_info(path)
        if not probe:
            info["error"] = (
                "Could not read this video. ffmpeg may be missing, "
                "or the file may be damaged."
            )
            info["findings"] = []
            return info
        vs = [s for s in probe.get("streams", []) if s.get("codec_type") == "video"]
        aud = [s for s in probe.get("streams", []) if s.get("codec_type") == "audio"]
        if vs:
            w, h = int(vs[0]["width"]), int(vs[0]["height"])
            info["dimensions"] = "%d x %d" % (w, h)
            info["codec"] = vs[0].get("codec_name", "?")
        info["duration"] = float(probe.get("format", {}).get("duration", 0) or 0)
        info["audio"] = aud[0].get("codec_name") if aud else None
        found, _benign = sg.scan_video_provenance(path, probe)
    else:
        info["kind"] = "image"
        try:
            with sg.Image.open(path) as im:
                w, h = im.width, im.height
                info["dimensions"] = "%d x %d" % (w, h)
                info["codec"] = im.format
        except Exception:
            info["error"] = (
                "Not a readable image. The file may be damaged or in an "
                "unsupported format."
            )
            info["findings"] = []
            return info
        found = sg.inspect_image_metadata(path)

    info["findings"] = findings_json(found)
    rep = sg.assess_origin(path, found, w, h, is_video)
    info["origin"] = {
        "verdict": rep.verdict,
        "headline": rep.headline,
        "explain": rep.explain,
        "evidence": [{"weight": e.weight, "label": e.label, "detail": e.detail}
                     for e in rep.evidence],
        "synthid": rep.synthid,
        "synthid_note": rep.synthid_note,
    }
    info["synthid"] = rep.synthid == "declared"
    return info


def zip_dir(folder: Path) -> io.BytesIO:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for p in sorted(folder.rglob("*")):
            if p.is_file():
                z.write(p, p.relative_to(folder))
    buf.seek(0)
    return buf


class RunLog(sg.Log):
    """Streams the pipeline log into a list the browser can poll."""

    def __init__(self, sink: list):
        super().__init__(quiet=False, color=False)
        self.sink = sink

    def _emit(self, text, stream=None):
        self.sink.append(text.rstrip())


def _realesrgan_binary_present() -> bool:
    """PATH / bundled-app check only — no 120s probe render."""
    names = list(getattr(sg, "REALESRGAN_CANDIDATES", [])) + list(
        getattr(sg, "BUNDLED_CANDIDATES", []))
    for name in names:
        if not name:
            continue
        if os.path.sep in name and os.path.exists(name):
            return True
        if shutil.which(name):
            return True
    return False


def _caps_snapshot(include_probe_status: str) -> dict:
    return {
        "app": APP_NAME,
        "ffmpeg": sg.have_ffmpeg(),
        "propainter": sg.find_propainter(APP_ROOT) is not None,
        "realesrgan": _realesrgan_binary_present(),
        "realesrgan_status": include_probe_status,
    }


def _probe_realesrgan_bg() -> None:
    try:
        up = sg.detect_upscaler(sg.Log(quiet=True), None, False,
                                "realesrgan-x4plus", 4)
        ok = up.kind == "realesrgan"
    except Exception:
        ok = False
    with _CAPS_LOCK:
        data = _CAPS["data"] or _caps_snapshot("ready")
        data = dict(data)
        data["realesrgan"] = ok
        data["realesrgan_status"] = "ready"
        _CAPS["data"] = data
        _CAPS["ts"] = time.time()
        _CAPS["probed"] = True


def friendly_error(exc: BaseException) -> str:
    """User-facing error; never a traceback."""
    if isinstance(exc, sg.Cancelled):
        return "Cancelled."
    name = type(exc).__name__
    if name in ("RequestEntityTooLarge",):
        return "That file is too large. The limit is 4 GB per upload."
    msg = str(exc).strip() or name
    # Strip common traceback crumbs if a library stuffed them in.
    if "Traceback" in msg:
        msg = msg.split("Traceback", 1)[0].strip() or "Processing failed."
    if len(msg) > 280:
        msg = msg[:277] + "…"
    return msg


# --------------------------------------------------------------------------
# routes
# --------------------------------------------------------------------------

@app.errorhandler(413)
def too_large(_e):
    return jsonify({
        "error": "That file is too large. The limit is 4 GB per upload."
    }), 413


@app.errorhandler(404)
def not_found(_e):
    if request.path.startswith("/api/"):
        return jsonify({"error": "Not found."}), 404
    return Response("Not found.", status=404, mimetype="text/plain")


@app.route("/")
def index():
    reap_jobs()
    html = (APP_ROOT / "webapp.html").read_text(encoding="utf-8")
    return Response(html, mimetype="text/html; charset=utf-8")


@app.route("/favicon.svg")
def favicon_svg():
    path = APP_ROOT / "branding" / "favicon.svg"
    if not path.is_file():
        abort(404)
    return send_file(str(path), mimetype="image/svg+xml")


@app.route("/favicon.ico")
def favicon_ico():
    path = APP_ROOT / "branding" / "favicon.ico"
    if not path.is_file():
        abort(404)
    return send_file(str(path), mimetype="image/x-icon")


@app.route("/branding/<path:name>")
def branding_file(name):
    try:
        target = safe_under(APP_ROOT / "branding", name)
    except ValueError:
        abort(404)
    if not target.is_file():
        abort(404)
    mime = None
    if target.suffix == ".js":
        mime = "application/javascript; charset=utf-8"
    elif target.suffix == ".css":
        mime = "text/css; charset=utf-8"
    elif target.suffix == ".svg":
        mime = "image/svg+xml"
    return send_file(str(target), mimetype=mime)


@app.route("/api/capabilities")
def api_capabilities():
    reap_jobs()
    now = time.time()
    start_probe = False
    with _CAPS_LOCK:
        if _CAPS["data"] is not None and now - _CAPS["ts"] < CAPS_TTL_SEC:
            data = dict(_CAPS["data"])
        else:
            status = "ready" if _CAPS["probed"] else "pending"
            data = _caps_snapshot(status)
            if _CAPS["probed"] and _CAPS["data"]:
                data["realesrgan"] = _CAPS["data"].get("realesrgan", data["realesrgan"])
                data["realesrgan_status"] = "ready"
            _CAPS["data"] = data
            _CAPS["ts"] = now
            if not _CAPS["probed"]:
                start_probe = True
    if start_probe:
        threading.Thread(target=_probe_realesrgan_bg, daemon=True).start()
    return jsonify(data)


@app.route("/api/inspect", methods=["POST"])
def api_inspect():
    reap_jobs()
    files = request.files.getlist("files")
    if not files:
        return jsonify({"error": "No files were received."}), 400
    job, saved, rejected = save_uploads(files)
    if not saved:
        shutil.rmtree(job, ignore_errors=True)
        if rejected:
            return jsonify({
                "error": (
                    "This file type isn't supported. Use PNG, JPG, WebP, "
                    "MP4, MOV or WebM."
                ),
                "rejected": rejected,
            }), 400
        return jsonify({"error": "No files were received."}), 400
    reports = [inspect_any(p) for p in sorted((job / "in").iterdir()) if p.is_file()]
    for name in rejected:
        reports.append({
            "name": name,
            "kind": "unknown",
            "error": (
                "This file type isn't supported. Use PNG, JPG, WebP, "
                "MP4, MOV or WebM."
            ),
            "findings": [],
        })
    JOBS[job.name] = {"path": job, "created": time.time()}
    return jsonify({"job": job.name, "reports": reports, "rejected": rejected})


@app.route("/api/detect", methods=["POST"])
def api_detect():
    """Watermark candidates as structured data, plus an annotated preview."""
    reap_jobs()
    entry = JOBS.get(request.form.get("job", ""))
    if not entry:
        return jsonify({"error": "Session expired — please add your files again."}), 400
    job = entry["path"]
    if not sg.have_ffmpeg():
        return jsonify({
            "error": "Video features need ffmpeg. Install it with: brew install ffmpeg"
        }), 400
    results = []
    for path in sorted((job / "in").iterdir()):
        if path.suffix.lower() not in sg.VIDEO_EXTS:
            continue
        frames = sg.sample_frames(path)
        cands = sg.detect_logo_candidates(frames) if frames is not None else []
        preview_name = None
        if cands:
            dst = job / "out" / ("_detected_%s.png" % path.stem)
            if sg.write_detection_preview(path, cands, dst):
                preview_name = dst.name
        results.append({
            "name": path.name,
            "preview": preview_name,
            "candidates": [{
                "corner": c[0],
                "box": "%d:%d:%d:%d" % c[1],
                "w": c[1][2], "h": c[1][3],
            } for c in cands],
        })
    return jsonify({"results": results})


@app.route("/api/suggest-crop", methods=["POST"])
def api_suggest_crop():
    """Use split_grids.suggest_crop — do not reimplement the geometry."""
    reap_jobs()
    entry = JOBS.get(request.form.get("job", ""))
    if not entry:
        return jsonify({"error": "Session expired — please add your files again."}), 400
    box_s = (request.form.get("box") or "").strip()
    if not box_s:
        return jsonify({"error": "No watermark box was given."}), 400
    try:
        box = sg.parse_box(box_s)
    except Exception:
        return jsonify({"error": "The watermark box must look like x:y:w:h."}), 400
    job = entry["path"]
    out = []
    for path in sorted((job / "in").iterdir()):
        if path.suffix.lower() not in sg.VIDEO_EXTS:
            continue
        probe = sg.ffprobe_info(path)
        if not probe:
            out.append({
                "name": path.name,
                "error": (
                    "Could not read this video. ffmpeg may be missing, "
                    "or the file may be damaged."
                ),
            })
            continue
        vs = [s for s in probe.get("streams", []) if s.get("codec_type") == "video"]
        if not vs:
            out.append({"name": path.name, "error": "No video stream found."})
            continue
        src_w, src_h = int(vs[0]["width"]), int(vs[0]["height"])
        suggestions = []
        for key, (tw, th) in sg.TARGETS.items():
            sug = sg.suggest_crop(src_w, src_h, box, tw, th)
            if sug is None:
                suggestions.append({
                    "format": key,
                    "label": sg.FORMAT_LABEL[key],
                    "ok": False,
                    "detail": "No crop at this ratio can exclude that box.",
                })
            else:
                x, y, w, h = sug
                suggestions.append({
                    "format": key,
                    "label": sg.FORMAT_LABEL[key],
                    "ok": True,
                    "crop": "%d:%d:%d:%d" % (x, y, w, h),
                    "keep_w": round(100.0 * w / src_w, 1) if src_w else 0,
                    "ratio": round(w / float(h), 4) if h else 0,
                    "size": "%dx%d" % (w, h),
                })
        out.append({
            "name": path.name,
            "source": "%dx%d" % (src_w, src_h),
            "box": box_s,
            "suggestions": suggestions,
        })
    if not out:
        return jsonify({"error": "No videos in this session."}), 400
    return jsonify({"results": out})


def _run_pipeline(run_id: str, job: Path, argv: list, log_sink: list,
                  cancel: threading.Event) -> None:
    out = job / "out"
    log = RunLog(log_sink)
    sg.set_cancel_event(cancel)
    try:
        args = sg.build_parser().parse_args(argv)
        stats = sg.Stats()
        in_root = Path(argv[0])
        files, skipped = sg.collect_inputs(in_root)
        stats.skipped = skipped
        if cancel.is_set():
            raise sg.Cancelled("cancelled")
        up = sg.detect_upscaler(log, args.realesrgan_bin, args.no_realesrgan,
                                args.realesrgan_model, args.realesrgan_scale,
                                args.realesrgan_models)
        for i, path in enumerate(files, start=1):
            if cancel.is_set():
                raise sg.Cancelled("cancelled")
            RUNS[run_id]["current"] = path.name
            RUNS[run_id]["done_count"] = i - 1
            RUNS[run_id]["total"] = len(files)
            try:
                if args.clean_only and path.suffix.lower() in sg.IMAGE_EXTS:
                    sg.process_image_clean_only(path, in_root, out, args, log,
                                                stats, i, len(files))
                elif path.suffix.lower() in sg.VIDEO_EXTS:
                    sg.process_video(path, in_root, out, args, log, stats,
                                     i, len(files))
                else:
                    sg.process_grid(path, in_root, out, args, up, log, stats,
                                    i, len(files))
            except sg.Cancelled:
                raise
            except Exception as exc:
                stats.failures.append((path.name, friendly_error(exc)))
                log.error("%s — %s" % (path.name, friendly_error(exc)))

        produced = []
        for p in sorted(out.rglob("*")):
            if p.is_file() and not p.name.startswith("_detected_"):
                produced.append({
                    "name": str(p.relative_to(out)),
                    "size_h": human(p.stat().st_size),
                    "ext": p.suffix.lower(),
                })
        RUNS[run_id].update({
            "status": "done",
            "result": {
                "files": produced,
                "warnings": log.warnings,
                "failures": [list(f) for f in stats.failures],
                "synthid": stats.synthid_files,
                "elapsed": round(time.time() - RUNS[run_id]["started"], 1),
            },
        })
    except sg.Cancelled:
        log.info("Cancelled.")
        RUNS[run_id].update({"status": "cancelled", "error": "Cancelled."})
    except Exception as exc:
        log.error(friendly_error(exc))
        RUNS[run_id].update({"status": "error", "error": friendly_error(exc)})
    finally:
        sg.set_cancel_event(None)


@app.route("/api/run", methods=["POST"])
def api_run():
    reap_jobs()
    entry = JOBS.get(request.form.get("job", ""))
    if not entry:
        return jsonify({"error": "Session expired — please add your files again."}), 400
    job = entry["path"]
    action = request.form.get("action", "clean")
    out = job / "out"
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True)

    argv = [str(job / "in"), str(out)]
    label = ""
    if action == "clean":
        argv.append("--clean-only")
        label = "Removing metadata"
    elif action == "split":
        argv += ["--format", request.form.get("format", "both"),
                 "--layout", request.form.get("layout", "grid")]
        label = "Splitting grids"
    elif action == "video":
        if not sg.have_ffmpeg():
            return jsonify({
                "error": "Video features need ffmpeg. Install it with: brew install ffmpeg"
            }), 400
        argv += ["--video-fit", request.form.get("fit", "none")]
        crop = request.form.get("crop", "").strip()
        if crop:
            argv += ["--video-crop", crop]
        label = "Cleaning and resizing video"
    elif action == "cropwm":
        if not sg.have_ffmpeg():
            return jsonify({
                "error": "Video features need ffmpeg. Install it with: brew install ffmpeg"
            }), 400
        box = request.form.get("box", "").strip()
        if not box:
            return jsonify({"error": "No watermark box was given."}), 400
        crop = request.form.get("crop", "").strip()
        if crop:
            argv += ["--video-crop", crop]
            label = "Cropping watermark out of frame"
        else:
            argv += ["--remove-logo", box, "--suggest-crop"]
            fmt = request.form.get("format")
            if fmt in ("45", "916", "both"):
                argv += ["--format", fmt]
            label = "Suggesting a watermark-free crop"
    elif action == "removelogo":
        if not sg.have_ffmpeg():
            return jsonify({
                "error": "Video features need ffmpeg. Install it with: brew install ffmpeg"
            }), 400
        box = request.form.get("box", "").strip()
        if not box:
            return jsonify({"error": "No watermark box was given."}), 400
        method = request.form.get("method", "delogo")
        argv += ["--remove-logo", box, "--logo-method", method]
        label = "Removing watermark (%s)" % method
    else:
        return jsonify({"error": "Unknown action."}), 400

    run_id = uuid.uuid4().hex[:12]
    sink = []
    cancel = threading.Event()
    RUNS[run_id] = {
        "status": "running", "log": sink, "started": time.time(),
        "label": label, "job": job.name, "current": "",
        "done_count": 0, "total": 0, "cancel": cancel,
    }
    threading.Thread(target=_run_pipeline, args=(run_id, job, argv, sink, cancel),
                     daemon=True).start()
    return jsonify({"run": run_id, "label": label})


@app.route("/api/cancel/<run_id>", methods=["POST"])
def api_cancel(run_id):
    run = RUNS.get(run_id)
    if not run:
        return jsonify({"error": "Unknown run."}), 404
    ev = run.get("cancel")
    if ev is not None:
        ev.set()
    sg.kill_active_subprocesses()
    if run.get("status") == "running":
        run["status"] = "cancelling"
    return jsonify({"ok": True, "status": run.get("status")})


@app.route("/api/status/<run_id>")
def api_status(run_id):
    run = RUNS.get(run_id)
    if not run:
        return jsonify({"error": "Unknown run."}), 404
    since = int(request.args.get("since", 0) or 0)
    return jsonify({
        "status": run["status"],
        "label": run["label"],
        "lines": run["log"][since:],
        "next": len(run["log"]),
        "elapsed": round(time.time() - run["started"], 1),
        "current": run.get("current", ""),
        "done_count": run.get("done_count", 0),
        "total": run.get("total", 0),
        "result": run.get("result"),
        "error": run.get("error"),
        "job": run["job"],
    })


@app.route("/api/reset", methods=["POST"])
def api_reset():
    reap_jobs()
    job_id = request.form.get("job") or ""
    if not job_id and request.is_json:
        job_id = (request.get_json(silent=True) or {}).get("job") or ""
    if not job_id:
        return jsonify({"error": "No session to reset."}), 400
    delete_job(job_id)
    return jsonify({"ok": True})


@app.route("/api/download/<job_id>")
def api_download(job_id):
    entry = JOBS.get(job_id)
    if not entry:
        return jsonify({"error": "Session expired."}), 400
    out = entry["path"] / "out"
    files = [p for p in out.rglob("*")
             if p.is_file() and not p.name.startswith("_detected_")]
    if not files:
        return jsonify({"error": "There are no results to download."}), 400
    cleanup = request.args.get("cleanup") in ("1", "true", "yes")
    if len(files) == 1:
        resp = send_file(str(files[0]), as_attachment=True,
                         download_name=files[0].name)
    else:
        resp = send_file(zip_dir(out), as_attachment=True,
                         download_name="framewipe-results.zip",
                         mimetype="application/zip")
    if cleanup:
        # Defer deletion so the response can finish sending.
        def _later():
            time.sleep(2)
            delete_job(job_id)
        threading.Thread(target=_later, daemon=True).start()
    return resp


@app.route("/api/file/<job_id>/<path:name>")
def api_file(job_id, name):
    entry = JOBS.get(job_id)
    if not entry:
        return jsonify({"error": "Session expired."}), 400
    try:
        target = safe_under(entry["path"] / "out", name)
    except ValueError:
        return jsonify({"error": "Not found."}), 404
    if not target.is_file():
        return jsonify({"error": "Not found."}), 404
    return send_file(str(target))


# --------------------------------------------------------------------------
# entry point
# --------------------------------------------------------------------------

def free_port(preferred: int = 8765) -> int:
    for port in range(preferred, preferred + 40):
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    return 0


def main() -> int:
    preferred = env_port()
    port = free_port(preferred)
    if not port:
        sys.stderr.write("No free port found near %d on 127.0.0.1.\n" % preferred)
        return 1
    url = "http://127.0.0.1:%d/" % port
    print("\n  %s" % APP_NAME)
    print("  " + "-" * 52)
    print("  Open:  %s" % url)
    print("  Files are processed on this machine; nothing is uploaded.")
    print("  Press Ctrl+C here (or close this window) to stop.\n")
    threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    try:
        app.run(host="127.0.0.1", port=port, debug=False, threaded=True)
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())

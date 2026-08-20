#!/usr/bin/env python3
"""
Cleanroom — local browser front-end for split_grids.py.

Everything runs on this machine: files are processed in a temporary folder and
nothing is uploaded anywhere. Start it with the launcher (Cleanroom.command) or
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
    from flask import Flask, jsonify, request, send_file, Response
except ImportError:
    sys.exit("Flask is missing. Install it with:\n\n    "
             "python3 -m pip install --user flask\n")

import split_grids as sg

APP_NAME = "Cleanroom"
APP_ROOT = Path(__file__).resolve().parent
WORK = Path(tempfile.gettempdir()) / "cleanroom"
WORK.mkdir(exist_ok=True)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 4 * 1024 * 1024 * 1024   # 4 GB per request

JOBS: dict = {}     # job id -> {"path": Path}
RUNS: dict = {}     # run id -> {"status", "log", "result", "started", "label"}


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


def save_uploads(files) -> Path:
    job = WORK / uuid.uuid4().hex[:12]
    (job / "in").mkdir(parents=True)
    (job / "out").mkdir(parents=True)
    for f in files:
        name = os.path.basename(f.filename or "")
        if name:
            f.save(str(job / "in" / name))
    return job


def findings_json(items) -> list:
    return [{"label": f.label, "detail": f.detail, "bytes": f.nbytes,
             "kind": f.kind} for f in items]


def inspect_any(path: Path) -> dict:
    """Metadata report plus AI-origin assessment for one file."""
    ext = path.suffix.lower()
    size = path.stat().st_size
    info: dict = {"name": path.name, "size": size, "size_h": human(size),
                  "ext": ext}
    is_video = ext in sg.VIDEO_EXTS
    found: list = []
    w = h = 0

    if is_video:
        info["kind"] = "video"
        probe = sg.ffprobe_info(path)
        if not probe:
            info["error"] = ("Could not read this video. ffmpeg may be missing, "
                             "or the file may be damaged.")
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
        except Exception as exc:
            info["error"] = "Not a readable image (%s)." % exc
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


# --------------------------------------------------------------------------
# routes
# --------------------------------------------------------------------------

@app.route("/")
def index():
    return Response((APP_ROOT / "webapp.html").read_text(), mimetype="text/html")


@app.route("/api/capabilities")
def api_capabilities():
    return jsonify({
        "app": APP_NAME,
        "ffmpeg": sg.have_ffmpeg(),
        "propainter": sg.find_propainter(APP_ROOT) is not None,
        "realesrgan": sg.detect_upscaler(sg.Log(quiet=True), None, False,
                                        "realesrgan-x4plus", 4).kind == "realesrgan",
    })


@app.route("/api/inspect", methods=["POST"])
def api_inspect():
    files = request.files.getlist("files")
    if not files:
        return jsonify({"error": "No files were received."}), 400
    job = save_uploads(files)
    reports = [inspect_any(p) for p in sorted((job / "in").iterdir()) if p.is_file()]
    JOBS[job.name] = {"path": job}
    return jsonify({"job": job.name, "reports": reports})


@app.route("/api/detect", methods=["POST"])
def api_detect():
    """Watermark candidates as structured data, plus an annotated preview."""
    entry = JOBS.get(request.form.get("job", ""))
    if not entry:
        return jsonify({"error": "Session expired — please add your files again."}), 400
    job = entry["path"]
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


def _run_pipeline(run_id: str, job: Path, argv: list, log_sink: list) -> None:
    out = job / "out"
    log = RunLog(log_sink)
    try:
        args = sg.build_parser().parse_args(argv)
        stats = sg.Stats()
        in_root = Path(argv[0])
        files, skipped = sg.collect_inputs(in_root)
        stats.skipped = skipped
        up = sg.detect_upscaler(log, args.realesrgan_bin, args.no_realesrgan,
                                args.realesrgan_model, args.realesrgan_scale,
                                args.realesrgan_models)
        for i, path in enumerate(files, start=1):
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
            except Exception as exc:
                stats.failures.append((path.name, "%s: %s" % (type(exc).__name__, exc)))
                log.error("%s — %s" % (path.name, exc))

        produced = []
        for p in sorted(out.rglob("*")):
            if p.is_file() and not p.name.startswith("_detected_"):
                produced.append({"name": str(p.relative_to(out)),
                                 "size_h": human(p.stat().st_size)})
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
    except Exception as exc:
        log.error("%s: %s" % (type(exc).__name__, exc))
        RUNS[run_id].update({"status": "error", "error": str(exc)})


@app.route("/api/run", methods=["POST"])
def api_run():
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
        argv += ["--video-fit", request.form.get("fit", "none")]
        crop = request.form.get("crop", "").strip()
        if crop:
            argv += ["--video-crop", crop]
        label = "Cleaning and resizing video"
    elif action == "removelogo":
        box = request.form.get("box", "").strip()
        if not box:
            return jsonify({"error": "No watermark box was given."}), 400
        method = request.form.get("method", "delogo")
        argv += ["--remove-logo", box, "--logo-method", method]
        label = "Removing watermark (%s)" % method
    else:
        return jsonify({"error": "Unknown action."}), 400

    run_id = uuid.uuid4().hex[:12]
    sink: list = []
    RUNS[run_id] = {"status": "running", "log": sink, "started": time.time(),
                    "label": label, "job": job.name, "current": "",
                    "done_count": 0, "total": 0}
    threading.Thread(target=_run_pipeline, args=(run_id, job, argv, sink),
                     daemon=True).start()
    return jsonify({"run": run_id, "label": label})


@app.route("/api/status/<run_id>")
def api_status(run_id):
    run = RUNS.get(run_id)
    if not run:
        return jsonify({"error": "Unknown run."}), 404
    since = int(request.args.get("since", 0))
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
    if len(files) == 1:
        return send_file(str(files[0]), as_attachment=True,
                         download_name=files[0].name)
    return send_file(zip_dir(out), as_attachment=True,
                     download_name="cleanroom-results.zip",
                     mimetype="application/zip")


@app.route("/api/file/<job_id>/<path:name>")
def api_file(job_id, name):
    entry = JOBS.get(job_id)
    if not entry:
        return jsonify({"error": "Session expired."}), 400
    base = (entry["path"] / "out").resolve()
    target = (base / name).resolve()
    if not str(target).startswith(str(base)) or not target.is_file():
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
    port = free_port()
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

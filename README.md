# Framewipe

**Prep frames locally. Nothing uploaded.**

Local Mac/Python tool for getting AI stills and clips Instagram-ready. A Flask UI on `127.0.0.1` plus the `split_grids.py` engine.

- **Inspect** — EXIF, XMP, IPTC, ICC, C2PA / Content Credentials
- **Origin** — whether a file *declares* it is AI-generated, and whether it *declares* SynthID
- **Clean** — strip all of that. Lossless for JPEG, PNG, WebP (pixels bit-identical)
- **Split** — 2×2 grids into 4:5 (1080×1350) and 9:16 (1080×1920)
- **Video** — clean metadata, scale, optional watermark crop or inpaint

Nothing is uploaded. No accounts, no database, no paid APIs.

![App screenshot](docs/screenshot-app.png)

*Add a screenshot at `docs/screenshot-app.png` when you have one.*

---

## The app

Double-click **`Framewipe.command`**. First run creates `.venv` next to the script, installs `requirements.txt` into it, starts a local server, and opens your browser. Close the Terminal window to stop.

```bash
chmod +x Framewipe.command
open Framewipe.command
```

Or:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python webapp.py
```

Always binds **127.0.0.1**. `debug=False`. Default port `8765` (next free port if busy).

---

## Prerequisites

- macOS, Python 3.9+ (system 3.9.6 is fine)
- [Homebrew](https://brew.sh) for ffmpeg
- ffmpeg for video: `brew install ffmpeg`

Optional:

- Real-ESRGAN binary on PATH, or Upscayl.app — sharper still upscales
- ProPainter checkout in `.propainter/` — best watermark inpaint (separate venv; see below)

Do **not** `pip install --user` into system Python. The launcher uses `.venv`.

---

## Install

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
brew install ffmpeg
```

Dev tests:

```bash
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
.venv/bin/python -m py_compile split_grids.py webapp.py
```

Optional extra (not required): `requirements-optional.txt`.

### Optional: Real-ESRGAN

No Homebrew formula. Either a [prebuilt ncnn binary](https://github.com/xinntao/Real-ESRGAN/releases) or `brew install --cask upscayl`. Without it, stills use Lanczos + unsharp mask.

### Optional: ProPainter

```bash
git clone --depth 1 https://github.com/sczhou/ProPainter.git .propainter
python3 -m venv .propainter-venv
.propainter-venv/bin/python -m pip install torch torchvision av addict einops future numpy scipy opencv-python matplotlib scikit-image imageio-ffmpeg pyyaml requests timm yapf
```

Isolated from Framewipe’s `.venv`. Delete both directories to remove it.

---

## Environment

Copy `.env.example` to `.env` if you want to override defaults. No secrets.

| Variable | Default | Meaning |
| --- | --- | --- |
| `FRAMEWIPE_PORT` | `8765` | Preferred local port |
| `FRAMEWIPE_SITE_URL` | empty | Canonical URL for the **landing** site after you buy a domain. The local app ignores it. |

`.env` is gitignored.

---

## CLI

Same engine as the app:

```bash
python3 split_grids.py ./media ./out --clean-only
python3 split_grids.py ./grids ./out --format both --layout mirror
python3 split_grids.py ./clips ./out --video-fit none
python3 split_grids.py ./clips ./out --remove-logo 566:1127:66:68 --suggest-crop
```

Input is a folder of **images and/or videos**, searched recursively. `split_grids.py -h` lists every flag.

Lossless clean, origin check, gutter detection, video fit/crop, and watermark methods are unchanged from the engine. SynthID in pixels is **not** removed; a declaration is metadata and *is* stripped.

---

## Architecture

| Piece | Role |
| --- | --- |
| `webapp.html` + `branding/app.js` | Local UI |
| `webapp.py` | Flask, 127.0.0.1, job temp dirs, cancel, TTL |
| `split_grids.py` | Engine (do not rename) |
| `tempfile/framewipe/` | Per-job `in/` and `out/` (legacy `cleanroom/` dirs are reaped) |
| `landing/` | Static marketing/SEO site (Cloudflare Pages / GitHub Pages) |
| `branding/` | Mark, wordmark, favicons, OG image |

Jobs older than ~2 hours are deleted on new requests. **Start over** calls `POST /api/reset` and deletes the temp dir, not only JS state.

---

## Production

This is a **local app** plus an optional **static landing page**. It is not a multi-tenant web service.

- Do not bind `0.0.0.0`.
- Do not deploy the processor to Fly/Render — privacy (other people’s media) and cost (ffmpeg, CPU, RAM).
- Landing: Cloudflare Pages from `landing/`, free. Domain later: Cloudflare Registrar for `framewipe.com` (~$10.46/year). See `DEPLOY.md`.
- Estimated monthly cost: **$0**. Domain ~$10/year after you buy it.

Checklist: `PRODUCTION_CHECKLIST.md`.

---

## Tests and lint

```bash
python3 -m py_compile split_grids.py webapp.py
python3 -m pytest -q
```

Tests cover lossless JPEG/PNG/WebP clean and the gutter 50/50 fallback. No ProPainter, no network.

---

## License

MIT. Copyright (c) 2026 Shivam Chahar.

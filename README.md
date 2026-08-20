# Cleanroom

Prep AI-generated media for Instagram. Two ways to use it:

- **Cleanroom** — a local web app. Double-click `Cleanroom.command` and work in your browser.
- **`split_grids.py`** — the same engine as a command-line tool, for batches and scripting.

What it does:

- **Inspect** — show every piece of metadata in an image or video: EXIF, XMP, IPTC, ICC, C2PA.
- **Check origin** — say whether a file declares that it is AI-generated, and whether it declares a SynthID watermark.
- **Clean** — strip all of it. Lossless for JPEG, PNG and WebP: the pixels come out bit-identical.
- **Split** — cut 2×2 grid images into individual Instagram-ready panels.
- **Video** — clean metadata, resize to Instagram dimensions, and optionally remove a burned-in watermark.

Nothing is uploaded anywhere. Everything runs on your machine.

---

## Contents

- [The app](#the-app)
- [Install](#install)
- [Quick reference](#quick-reference)
- [Is it AI-generated?](#is-it-ai-generated)
- [Remove metadata only](#remove-metadata-only)
- [Images: splitting grids](#images-splitting-grids)
- [Video: cleaning and resizing](#video-cleaning-and-resizing)
- [Video: removing a watermark](#video-removing-a-watermark)
- [Video: cropping the watermark out instead](#video-cropping-the-watermark-out-instead)
- [What is and isn't removed](#what-is-and-isnt-removed)
- [Output layout and naming](#output-layout-and-naming)
- [All flags](#all-flags)
- [How it works](#how-it-works)
- [Troubleshooting](#troubleshooting)

---

## The app

**Double-click `Cleanroom.command`.** It installs anything missing the first
time, starts a local server and opens your browser. Close the Terminal window to
stop it.

It walks you through four steps:

**1 · Add your files** — drag images and videos in, or click to browse. Mixed
batches are fine.

**2 · What's inside** — every file gets two badges: an origin verdict
(`AI-generated`, `Camera photo`, `Origin unknown`) and a metadata count
(`already clean` or `6 items found`). Expand it for the evidence behind the
verdict, a SynthID line, and a table of every metadata record with sizes and
contents.

**3 · Choose what to do** — five actions as cards. Actions that don't apply are
greyed out with the reason on them (*"No videos loaded"*, *"Needs ffmpeg"*), and
when your files carry metadata, **Remove metadata only** is badged `SUGGESTED`.
Each action shows its own options and a plain-English note about what will
happen.

**4 · Results** — a live progress bar with elapsed time and which file is being
worked on, then a green summary, thumbnails, and a download button. The raw
technical log is there too, collapsed, if you want it.

### Removing a watermark, without the copy-paste

Pick **Find watermark**, and you get the candidate boxes *and* a picture of your
own frame with each one drawn and numbered on it. Every candidate has a
**Use this one** button — clicking it fills in the box, switches to
**Remove watermark**, and confirms which box is loaded.

You never type coordinates, and you can see what you are about to erase before
you erase it. If you press Run without a box, it stops and tells you why.

### Running it manually

```bash
python3 webapp.py
```

If double-clicking opens the file in a text editor instead of running it:

```bash
chmod +x Cleanroom.command
```

The launcher picks a free port automatically, so a stale server won't block a
new one.

---

## Install

```bash
pip install -r requirements.txt
```

That covers images. Video needs ffmpeg:

```bash
brew install ffmpeg
```

### Optional: Real-ESRGAN (better image upscaling)

**There is no Homebrew formula** — `brew install realesrgan-ncnn-vulkan` will fail. Two routes that work:

**1. Upstream prebuilt binary** (~52 MB, no GUI):

```bash
curl -L -o /tmp/resrgan.zip https://github.com/xinntao/Real-ESRGAN/releases/download/v0.2.5.0/realesrgan-ncnn-vulkan-20220424-macos.zip && mkdir -p ~/tools/realesrgan && unzip -o /tmp/resrgan.zip -d ~/tools/realesrgan && xattr -dr com.apple.quarantine ~/tools/realesrgan
```

It's unsigned, hence the `xattr` call. The build dates from 2022 and is x86_64, so on Apple Silicon it runs under Rosetta (`softwareupdate --install-rosetta` if it won't start).

**2. Upscayl** (GUI app that bundles a maintained ncnn build):

```bash
brew install --cask upscayl
```

`/Applications/Upscayl.app` is searched automatically. Its models are named differently (`upscayl-standard-4x`); the script detects that and prefers general-purpose x4 models over stylised ones like `remacri`.

Without either, images fall back to Lanczos + unsharp mask, which is decent at the ~2.1–2.5× these panels need.

### Optional: ProPainter (best watermark removal)

Already installed in this project as `.propainter/` and `.propainter-venv/`. To set it up elsewhere:

```bash
git clone --depth 1 https://github.com/sczhou/ProPainter.git .propainter
```

```bash
/opt/homebrew/bin/python3.13 -m venv .propainter-venv && .propainter-venv/bin/python -m pip install torch torchvision av addict einops future numpy scipy opencv-python matplotlib scikit-image imageio-ffmpeg pyyaml requests timm yapf
```

Model weights (~190 MB total) download automatically on first run. It uses an isolated venv, so your system Python is untouched — delete both directories to remove it completely. On Apple Silicon it runs on the MPS GPU backend.

---

## Quick reference

| Goal | Command |
| --- | --- |
| **Check if a file is AI-generated** | `python3 split_grids.py ./photos /tmp/none --check-origin` |
| **Just remove metadata** | `python3 split_grids.py ./photos ./clean --clean-only` |
| Split grids | `python3 split_grids.py ./grids ./out` |
| Split grids, reels only | `python3 split_grids.py ./grids ./out --format 916` |
| Clean a video, no cropping | `python3 split_grids.py ./clips ./out` |
| Find a watermark | `python3 split_grids.py ./clips ./out --detect-logo --dry-run` |
| Remove a watermark, best quality | `python3 split_grids.py ./clips ./out --remove-logo X:Y:W:H --logo-method propainter` |
| Work out a watermark-free crop | `python3 split_grids.py ./clips ./out --remove-logo X:Y:W:H --suggest-crop` |
| Crop the watermark out of frame | `python3 split_grids.py ./clips ./out --video-crop 45:0:630:1120` |
| Preview without writing | add `--dry-run` |
| Silence the log | add `--quiet` |

Images and videos can sit in the same input folder and are handled in one pass.

---

## Images: splitting grids

```bash
python3 split_grids.py ./grids ./out
```

Per grid: detect the gutters by pixel analysis → slice four panels → trim residual white → centre-crop to 4:5 and/or 9:16 → upscale to exact Instagram dimensions → strip all metadata → save JPEG q95 4:4:4.

Targets are **1080×1350** (4:5 feed) and **1080×1920** (9:16 reel/story).

```bash
# force one ratio
python3 split_grids.py ./grids ./out --format 45
python3 split_grids.py ./grids ./out --format 916
python3 split_grids.py ./grids ./out --format both

# everything in one folder, numbered across the batch
python3 split_grids.py ./grids ./out --layout flat

# rename outputs shot1.jpg, shot2.jpg ...
python3 split_grids.py ./grids ./out --name-prefix shot

# force the Lanczos path even if Real-ESRGAN is installed
python3 split_grids.py ./grids ./out --no-realesrgan
```

### Format routing

4:5 vs 9:16 is decided per file, first match wins:

1. `--format 45 | 916 | both`
2. Filename token — `916`, `9x16`, `reel`, `story` → 9:16; `45`, `4x5`, `post`, `feed` → 4:5. Digit tokens use word boundaries, so `IMG_2345.png` does **not** route as 4:5.
3. Input subfolder named `reels/`, `stories/` → 9:16; `posts/`, `feed/` → 4:5.
4. Otherwise both.

The reason is printed for every file, so you can see which rule fired.

---

## Is it AI-generated?

Every file you add gets an origin verdict, in the app and from the CLI:

```bash
python3 split_grids.py ./photos /tmp/none --check-origin
```

| Verdict | Meaning |
| --- | --- |
| **AI-generated** | The file declares it. C2PA `trainedAlgorithmicMedia`, a generator name in the metadata, or an embedded prompt/parameters record. |
| **Possibly AI** | No declaration, but several weak signals line up (generator-typical dimensions, no camera data). |
| **Camera photo** | Camera settings are recorded — make, model, lens, exposure, ISO, GPS. |
| **Origin unknown** | Nothing says anything either way. |

Evidence is listed individually and tagged by strength — `PROOF` for a
declaration, `CAMERA` for capture data, `WEAK` for circumstantial — so you can
see what the verdict rests on rather than trusting a score.

### What this cannot do

**It reads declarations. It does not analyse pixels.** Reliable pixel-level
detection of AI imagery is an unsolved problem, and a confident-looking guess
would be worse than no answer, so this tool does not make one.

That has two consequences worth understanding:

- **"Origin unknown" is not "not AI."** Strip the metadata from an AI image —
  with this tool, or by screenshotting it, or by uploading it almost anywhere —
  and it becomes indistinguishable to this check. Run the check *before* you
  clean a file, not after.
- **"Camera photo" is not proof of a photograph.** EXIF can be copied from a
  real photo onto a generated one.

### SynthID

Each file also reports whether it **declares** a SynthID watermark.

**A declaration is not the watermark.** SynthID is embedded in the pixels
themselves and survives metadata removal, re-encoding, cropping and scaling.
The declaration is metadata, so cleaning the file deletes the declaration while
the watermark stays exactly where it was.

There is no public SynthID decoder — the key is Google's. Only their SynthID
Detector can say whether pixels carry a mark. So:

- **"SynthID declared"** → it is almost certainly there.
- **"Not declared"** → tells you nothing about the pixels, only that no metadata
  mentioned it.

The app says this in place, next to the result, rather than burying it here.

---

## Remove metadata only

If you just want the metadata gone and nothing else touched — no splitting, no
cropping, no resizing, no re-encoding:

```bash
python3 split_grids.py ./photos ./clean --clean-only
```

It prints exactly what it found before removing it:

```
[2/3] photo.jpg (clean only)
   -  source: 800x600  JPEG  RGB  456 KB
 WARN metadata found (7 item(s)):
      COM (comment)                        [26 bytes]
      APP11 (JUMBF (C2PA))                 [69 bytes]
      APP1 (EXIF / XMP)                    [100 bytes]
      Pillow info: exif                    [96 bytes]
      C2PA claim data                      1 occurrence(s) of 'c2pa'
      -  lossless (JPEG segments rewritten)  456 KB -> 456 KB
      -  verified clean (no EXIF/XMP/IPTC/ICC/C2PA records)
      -  pixels untouched: still 800x600
```

**This is lossless.** JPEG, PNG and WebP are rewritten at the container level:
metadata records are dropped and the compressed image data is copied
byte-for-byte. Verified on all three — decoded pixels are bit-identical to the
original, and the JPEG entropy-coded scan and PNG `IDAT` streams hash the same
before and after. No generational quality loss, unlike re-saving.

| Format | How | Lossless |
| --- | --- | --- |
| JPEG | APP1–APP15 and COM segments dropped, scan copied verbatim | Yes |
| PNG | text/`eXIf`/`iCCP`/`tIME` chunks dropped, `IDAT` copied verbatim | Yes |
| WebP | `EXIF`/`XMP `/`ICCP` RIFF chunks dropped, VP8X flags cleared | Yes |
| other | re-encoded via Pillow | No |

Filenames and folder structure are preserved (`--layout flat` puts everything in
one folder instead). Add `--dry-run` to see the report without writing.

Videos in the same folder are still processed as videos — cleaning a video always
means re-encoding, so there is no lossless path for those.

---

## Video: cleaning and resizing

```bash
python3 split_grids.py ./clips ./out
```

`mp4`, `mov`, `m4v`, `webm`. Videos are **not** split into four and **not cropped by default**. They are inspected, stripped of metadata, scaled to fit Instagram's 1080×1920 box at their original aspect ratio, and re-encoded (H.264 high, yuv420p, CRF 18, AAC 192k, `+faststart`).

```bash
# higher quality (lower CRF is better; 18 is already near-transparent)
python3 split_grids.py ./clips ./out --video-crf 15

# rename outputs clip1.mp4, clip2.mp4 ...
python3 split_grids.py ./clips ./out --video-prefix clip
```

### Framing: `--video-fit`

| Mode | What it does | Picture lost? |
| --- | --- | --- |
| `none` **(default)** | Keeps the source aspect. Scales to fit inside 1080×1920. `--format` is ignored. | No |
| `pad` | Letterboxes the whole frame into 4:5 or 9:16 with black bars. | No |
| `crop` | Centre-crops to 4:5 or 9:16. | **Yes** |

With the default, aspect is preserved exactly:

| Source | Output |
| --- | --- |
| 720×1280 (9:16) | 1080×1920 |
| 1080×1920 (9:16) | 1080×1920 (unchanged) |
| 1920×1080 (16:9) | 1080×608 |
| 640×640 (1:1) | 1080×1080 |

```bash
python3 split_grids.py ./clips ./out --video-fit pad --format 916
python3 split_grids.py ./clips ./out --video-fit crop --format 916
```

---

## Video: removing a watermark

### Step 1 — find it

```bash
python3 split_grids.py ./clips ./out --remove-logo auto --logo-method propainter
```

**`auto` never paints on its own.** Detection is a guess, and painting the wrong region is destructive and slow to discover, so the run lists every candidate, writes a preview image with the boxes drawn on a frame, and stops short of filling anything. Metadata cleaning and scaling still happen, so the run isn't wasted:

```
WARN detection is a GUESS: posters, captions and lights look just like a
     watermark to it. Check the preview before letting anything paint.
     1: top-left      --remove-logo 79:202:114:85   <- would be used
     2: top-right     --remove-logo 523:220:108:73
     3: bottom-right  --remove-logo 564:1127:72:61
     4: bottom-left   --remove-logo 200:1142:22:27
     preview: ./out/_detected_myclip.png
WARN NOT removing anything: the box above was guessed, not given.
```

Open the preview, see which numbered box is actually the watermark, and use that one. In the example above the real mark is **3**, not the top-ranked **1** — that was a poster on the wall.

Why detection can't just get it right: it looks for a small, compact, still, bright blob in a frame corner. Posters, captions, ceiling lights and jewellery all match that description. It has no idea what a generator badge looks like.

`--detect-logo --dry-run` does the same inspection without writing any video.

If none of the candidates is right, read the coordinates off a frame yourself:

```bash
ffmpeg -i clip.mp4 -vf "select='eq(n\,10)',crop=180:180:530:1080,scale=540:540:flags=neighbor" -vframes 1 zoom.png
```

### Step 2 — remove it

```bash
python3 split_grids.py ./clips ./out --remove-logo 564:1127:72:61 --logo-method propainter
```

`X:Y:W:H` in source pixels. An explicit box is always acted on immediately — the confirmation step only applies to guesses.

To let a guess paint without reviewing it (not recommended):

```bash
python3 split_grids.py ./clips ./out --remove-logo auto --accept-detected --logo-method propainter
```

### Choosing `--logo-method`

| Method | Speed | Quality | Needs |
| --- | --- | --- | --- |
| `delogo` **(default)** | instant | invisible over smooth areas, **blurred smear over detail** | nothing |
| `temporal` | ~0.4 s/frame | recovers real pixels where the camera exposed them | camera/subject motion |
| `propainter` | ~1 s/frame | **best on any background** | `.propainter` install |

Measured on a watermark composited onto a known-clean clip (higher dB = closer to the untouched original; above ~40 dB is invisible):

| Background behind the mark | `delogo` |
| --- | --- |
| Smooth (sky, gradient, blur) | **49–51 dB — invisible** |
| Dense detail (foliage, texture) | **15 dB — no better than leaving it** |

On a panning shot over dense detail, `temporal` reached 37.8 dB median with 80% of frames above 32 dB, against `delogo`'s 18.6 dB.

On a real locked-off clip where a hand crossed the mark, only `propainter` produced a clean result — `delogo` smeared a band across the fingers on 62% of frames, and `temporal` had almost nothing to work with because the camera never moved.

**Rule of thumb:** smooth background → `delogo` is instant and perfect. Anything else → `propainter`.

### What each method reports

```
-  logo box (given): x=566 y=1127 w=66 h=68
-  surrounding detail 3.4 — clean fill expected
-  ProPainter: 240 frames, 226x228 region around the mark — this takes a few minutes
-  ProPainter: 240 frames rebuilt, region composited back
```

`temporal` additionally reports confidence, calibrated against ground truth (ring agreement correlates 0.81 with true accuracy; frames scoring ≥18 dB had median true accuracy 40.1 dB, those below 14.5 dB):

```
-  temporal inpaint: 120/120 frames rebuilt, 99 (82%) with a strong match
WARN 18% of frames had no well-matched source frame — inspect the output.
```

If ProPainter isn't installed or fails, the run warns and falls back to `delogo` rather than dropping the file.

---

## Video: cropping the watermark out instead

Painting over a watermark always involves some invention. Cropping doesn't — the mark simply isn't in frame, so nothing can look wrong.

```bash
python3 split_grids.py ./clips ./out --remove-logo 566:1127:66:68 --suggest-crop
```

```
OK   4:5:  --video-crop 0:0:720:900    (keeps 100% of width, ratio 0.8000)
OK   9:16: --video-crop 45:0:630:1120  (keeps 88% of width, ratio 0.5625)
```

Then:

```bash
python3 split_grids.py ./clips ./out --video-crop 45:0:630:1120
```

`--suggest-crop` reduces the target to its smallest integer form (1080×1920 → 9:16) and builds the crop as a whole multiple, so you get an **exact** ratio with even dimensions rather than a rounded approximation.

### Generating for this workflow

Veo's `aspectRatio` accepts only `16:9` or `9:16`, so you can't request a taller frame that crops down to a clean 9:16. What you *can* change is resolution — ask for **1080p**:

| Generate as | Crop to 9:16 | Keeps | Upscale to 1080×1920 |
| --- | --- | --- | --- |
| 9:16 @ 720p | `45:0:630:1120` | 88% width | 1.71× |
| **9:16 @ 1080p** | `72:0:936:1664` | 87% width | **1.15×** |
| 16:9 @ 1080p, centre crop | `663:0:594:1056` | 31% width | 1.82× |

Same ~12% crop either way, but starting from 1080 wide makes the final upscale nearly free. Don't use 16:9 — it clears the watermark but costs most of your width.

---

## What is and isn't removed

### Removed and verified

C2PA / Content Credentials manifests (the ISOBMFF `uuid` box `d8fec3d6-…`), XMP packets, `udta`/`meta`/`ilst` boxes, all container and stream tags, chapters. For images: EXIF, XMP, IPTC/Photoshop, ICC beyond sRGB, C2PA.

Every provenance item is listed **before** removal, then the output is re-scanned and confirmed:

```
WARN provenance / credentials found in source (14 item(s)):
     C2PA / Content Credentials manifest   jumbc2pa{"claim_generator":"Google/Veo 3"...
     container tag comment                 Generated with Google Gemini
     SynthID reference                     3 occurrence(s) of 'synthid'
-  metadata: removed all 14 item(s); output scan clean
```

ffmpeg runs with `-map_metadata -1 -map_chapters -1 -bitexact -empty_hdlr_name 1` so it doesn't write its own tags back, and a final in-place sweep converts leftover boxes to `free` padding and blanks the `avc1` compressor-name field. Box sizes are preserved during that sweep, so `stco` chunk offsets stay valid.

What remains is structural only: `major_brand`, `compatible_brands`, `language`, `vendor_id`.

### NOT removed: SynthID

**SynthID is not removed, and this tool does not claim to remove it.** It is not metadata — it's a pattern embedded in the pixels by a neural network, designed to survive re-encoding, cropping, scaling and compression. Removing the visible watermark does not affect it.

When a source declares SynthID, the run says so per file and again in the summary:

```
NOT REMOVED — SynthID pixel watermark
    reels/veo_clip.mp4
```

Stripping metadata does not make content undetectable as AI-generated, and platforms including Instagram have their own AI-disclosure rules that apply whatever the file carries.

---

## Output layout and naming

Images are named `image1.jpg`–`image4.jpg` per grid, in reading order (top-left, top-right, bottom-left, bottom-right). Videos are `video1.mp4`. The quadrant each image came from is shown in the log.

`--layout` controls structure:

**`grid`** (default) — one folder per source file:

```
out/sunset/image1.jpg
out/sunset/image2.jpg
out/harbour/image1.jpg
```

**`flat`** — everything in the output folder, numbering continuing across the batch:

```
out/image1.jpg … out/image28.jpg
```

**`mirror`** — like `grid`, but reproduces the input subfolders. Use when the same filename appears in `reels/` and `posts/`:

```
out/reels/sunset/image1.jpg
out/posts/sunset/image1.jpg
```

Under `grid`, two files sharing a name in different input subfolders resolve to the same output folder; the script warns and points at `--layout mirror`.

When a grid produces **both** ratios they'd collide on one name, so a suffix is added for that file only: `image1_45.jpg` and `image1_916.jpg`. A file routed to a single ratio just gets `image1.jpg`.

---

## All flags

### General

| Flag | Meaning |
| --- | --- |
| `--format {45,916,both}` | force output ratio; overrides filename and folder routing |
| `--layout {grid,flat,mirror}` | output folder structure (default `grid`) |
| `--name-prefix PREFIX` | image filename prefix (default `image`) |
| `--check-origin` | report AI-origin and SynthID evidence; reads only, writes nothing |
| `--clean-only` | inspect and strip metadata only; no splitting, cropping or resizing (lossless) |
| `--dry-run` | analyse and log everything, write nothing |
| `--quiet` | suppress per-file output; errors and summary still shown |
| `--no-color` | disable ANSI colour (also honours `NO_COLOR`) |

### Video

| Flag | Meaning |
| --- | --- |
| `--remove-logo X:Y:W:H` | remove a burned-in watermark from that box; `auto` to detect (lists candidates and stops, see above) |
| `--accept-detected` | let an auto-detected box be painted without you confirming it |
| `--logo-method {delogo,temporal,propainter}` | how to fill the box (default `delogo`) |
| `--detect-logo` | locate a static overlay and report it without removing |
| `--video-crop X:Y:W:H` | explicit crop applied before scaling |
| `--suggest-crop` | print the largest crop at the target ratio that excludes the logo box |
| `--video-fit {none,pad,crop}` | framing; default `none` crops nothing |
| `--video-crf N` | x264 quality, lower is better (default 18) |
| `--video-prefix PREFIX` | video filename prefix (default `video`) |

### Gutter detection (images)

| Flag | Default | Meaning |
| --- | --- | --- |
| `--white-threshold` | 243 | mean brightness (0–255) for a gutter line |
| `--var-threshold` | 6.0 | max std-dev along a gutter line |
| `--min-gutter` | 4 | minimum gutter thickness, px |
| `--gutter-tolerance` | 0.20 | how far off-centre a gutter may sit, as a fraction of the axis |

### Upscaling (images)

| Flag | Meaning |
| --- | --- |
| `--realesrgan-bin PATH` | explicit path to a Real-ESRGAN executable |
| `--realesrgan-model NAME` | model passed to `-n` (default `realesrgan-x4plus`) |
| `--realesrgan-models DIR` | models folder passed to `-m`; auto-detected otherwise |
| `--realesrgan-scale N` | native scale passed to `-s` (default 4) |
| `--no-realesrgan` | force the Lanczos + unsharp path |

Exit codes: `0` success, `1` at least one file failed, `2` bad arguments.

---

## How it works

### Gutter detection

Rows and columns are scanned for lines that are both near-white **and** low-variance across their full span, so a bright but textured panel edge doesn't register as a gutter. Runs touching an image edge are treated as the outer border; the interior run closest to the axis midpoint becomes the split. Detected positions are printed in pixels.

If no interior gutter is found, the script warns loudly and falls back to a 50/50 split on that axis. After slicing, each panel is scanned inward for leftover white, capped at 25% per side so a panel with a genuinely bright region isn't eaten.

### Upscaling

Targets need a ~2.1–2.7× enlarge. Real-ESRGAN is used if a working binary is found — availability is confirmed with a tiny probe render at startup, not just a `which` check, so an installed-but-broken binary falls back instead of failing the batch. The panel is rendered at the model's native scale then Lanczos-fit to the exact target. Otherwise: Lanczos + a light unsharp mask (radius 1.2, 60%, threshold 3).

### Metadata

Images are rebuilt from raw pixels before saving, then every written file is re-opened and its JPEG segment table parsed: anything other than APP0/JFIF is reported, as is any `c2pa`/`jumb`/XMP byte signature. Non-sRGB ICC profiles are converted to sRGB before being dropped, so the transform isn't silently lost.

### Output format

Images: JPEG, quality 95, `subsampling=0` (4:4:4), optimised, baseline.
Video: H.264 high profile, yuv420p, CRF 18, AAC 192k @ 48 kHz, `+faststart`.

---

## Troubleshooting

**"The watermark is still there."** `--remove-logo` is opt-in. A plain run only cleans metadata and rescales — it never touches pixels.

**Auto-detection picks the wrong thing.** Expected — it looks for a small, compact, still, bright blob in a frame corner, and captions, posters and ceiling lights all match. That is why `auto` stops instead of painting: read the numbered candidate list, open the preview image it writes, and re-run with the box that is actually the watermark.

**It removed the wrong thing.** Only possible if you passed `--accept-detected` or an incorrect explicit box. Check the preview from a plain `--remove-logo auto` run to get the right coordinates, and discard the bad output.

**The fill looks like a blurred smear.** That's `delogo` over a detailed background. Use `--logo-method propainter`, or crop the mark out with `--suggest-crop`.

**A halo is left around where the mark was.** The box is too tight — it must cover the anti-aliased rim, not just the bright core. Measured: 24.6 dB with a core-only box vs 51.6 dB once padded past the rim. But don't oversize either; every extra pixel is invented rather than real.

**Non-image files are skipped and counted.** A file that fails (corrupt, truncated, unreadable) is reported and the batch continues; failures are listed again in the summary:

```
summary
  grids processed : 7
  videos cleaned  : 1
  outputs written : 40
  non-images      : 1
  warnings        : 3
  failures        : 1
      broken.png — OSError: Truncated File Read
  upscaler        : Lanczos + unsharp mask
  logo removed    : 1 video(s)
  total time      : 2.23s
```

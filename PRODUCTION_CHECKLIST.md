# Production checklist — Framewipe

Framewipe is two things: a local processor and a static landing page. Treat them separately.

## Local app (processor)

- [ ] `python3 -m py_compile split_grids.py webapp.py`
- [ ] `python3 -m pytest -q` (from a venv with `requirements-dev.txt`)
- [ ] Flask test client: `GET /` contains `Framewipe`; `GET /api/capabilities` returns `"app": "Framewipe"`
- [ ] Bind is `127.0.0.1` (`webapp.py` `app.run(host="127.0.0.1", debug=False)`)
- [ ] `FRAMEWIPE_PORT` documented; default 8765
- [ ] `.venv` via `Framewipe.command`; never `pip install --user`
- [ ] ffmpeg via Homebrew for video
- [ ] Job TTL (~2h) and `POST /api/reset` delete temp files
- [ ] Path traversal on `/api/file` uses `pathlib.Path.relative_to`
- [ ] No analytics account, no Stripe, no cloud GPU

## Landing (`landing/`)

- [ ] Build: none. Pure HTML/CSS.
- [ ] Tests: open `landing/index.html` locally; check mobile width, contrast (`#121410` on `#f4f1ea`)
- [ ] Env: `FRAMEWIPE_SITE_URL` (canonical). Placeholder in HTML is `https://framewipe.com` — replace after purchase
- [ ] Domain: **not purchased in this repo**. Cloudflare Registrar ~$10.46/yr; Porkbun ~$11.08. Skip GoDaddy/Hostinger
- [ ] HTTPS: Cloudflare Pages provides HTTPS once a domain is attached
- [ ] SEO: title, description, canonical, Open Graph, Twitter, JSON-LD SoftwareApplication, `robots.txt`, `sitemap.xml`
- [ ] Favicon + `og.png` (1200×630)
- [ ] Responsive: `viewport` + max-width column
- [ ] Security: static files only; no secrets
- [ ] Performance: no JS required on the landing page

## Do not

- [ ] Do not deploy `webapp.py` to Fly, Render, or any public host
- [ ] Do not bind `0.0.0.0`
- [ ] Do not claim SynthID pixel removal

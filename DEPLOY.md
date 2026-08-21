# Deploy — Framewipe

You run these steps. This repo does not purchase a domain, push, or deploy.

Monthly cost: **$0** (Pages + local app). Domain: about **$10/year** after you buy it.

---

## 1. Landing on Cloudflare Pages (free)

From this repo, the site root is the `landing/` folder. No build command.

In the Cloudflare dashboard (you click these):

1. Workers & Pages → Create → Pages → Connect to Git (or Direct Upload).
2. Project name: `framewipe`.
3. If connecting Git: **Root directory** = `landing`. **Build command** = empty. **Output directory** = empty / `.`.
4. If Direct Upload:

```bash
# you run this locally, after installing wrangler if you want it
npx wrangler pages deploy landing --project-name=framewipe
```

5. After the domain exists, set the canonical URL. Replace `https://framewipe.com` in:

- `landing/index.html` (`<link rel="canonical">`, Open Graph, JSON-LD)
- `landing/sitemap.xml`
- `landing/robots.txt`

Or generate those three from `FRAMEWIPE_SITE_URL` at deploy time. The local Flask app does not need this variable.

---

## 2. Domain later — you buy it

**Name:** framewipe.com  
**Registrar (recommended):** [Cloudflare Registrar](https://www.cloudflare.com/products/registrar/) ≈ **$10.46/year**.  
**Alternative:** Porkbun ≈ $11.08/year.  
**Skip:** GoDaddy, Hostinger.

After purchase, attach the domain to the Pages project and enable the proxy (HTTPS is included). Point DNS at Pages as Cloudflare instructs. Do not buy it from this automation.

---

## 3. Local app (this machine)

```bash
brew install ffmpeg
chmod +x Framewipe.command
open Framewipe.command
```

First run creates `.venv` and installs `requirements.txt`. Browser opens `http://127.0.0.1:8765/` (or the next free port).

Optional `.env`:

```
FRAMEWIPE_PORT=8765
FRAMEWIPE_SITE_URL=
```

---

## 4. Do not put the processor on Fly or Render

The Flask app holds original media in a temp dir and shells out to ffmpeg / optional ProPainter. A public host would:

- See other people’s photos and videos (the whole point of Framewipe is that nothing is uploaded)
- Need a fat VM for ffmpeg and, if you enable it, GPU-ish ProPainter — that costs money every month

Keep the processor on 127.0.0.1. Only the static `landing/` folder belongs on Pages.

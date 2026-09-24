# Solix backend

FastAPI + MongoDB API for the [Solix Technologies site](https://github.com/akshatsingh-solix/Website)
(`frontend/` there). Deployed separately because GitHub Pages only serves
static files — it cannot run this service.

## What works without extra setup

Once deployed with a database and the four required secrets below, everything
works except two optional Emergent-platform integrations:

- **AI concierge chat** (`EMERGENT_LLM_KEY`) — without it, `/api/chat/stream`
  returns a clean 503 and the widget shows "temporarily unavailable" instead
  of crashing.
- **Outbound lead email alerts** (`EMERGENT_EMAIL_KEY`) — without it, leads
  still save to MongoDB and show up in the admin dashboard; only the email
  ping to your sales inbox is skipped.

Everything else — lead capture forms, the admin dashboard (`/admin`, login
`ADMIN_EMAIL`/`ADMIN_PASSWORD`), newsroom, press-release PDF downloads — works
fully with just MongoDB configured.

## Lead intelligence, content publishing and caching

- **Intent scoring**: consent-gated behaviour tracking (`POST /api/track`)
  scores each product; every form, trial sign-up and chat booking becomes one
  lead per email, tagged to the product line with the most intent and
  promoted to MQL on a hand-raise or score threshold. Model and tuning:
  [`docs/lead-intent-methodology.md`](docs/lead-intent-methodology.md).
- **Admin** (`/admin`): leadership dashboard, filterable leads with saved
  views, bulk updates, CSV/Excel export, lead timelines, and staff roles
  (admin, sales, content editor, read-only leadership).
- **Content publishing**: blogs, white papers, datasheets, case studies,
  webinars and marketing material with drafts, scheduling, versions, file
  uploads (stored in MongoDB) and gated downloads. Published items reach the
  site within about a minute. Optional `GITHUB_DEPLOY_TOKEN` also triggers a
  static rebuild so the content is baked into the site's pages.
- **Caching**: gzip, ETag/304 and `Cache-Control` on public content, a short
  in-process cache for published content and reports, and a 180-day TTL on
  raw events.

## Deploy (Render, free tier)

1. **Database**: create a free cluster at https://www.mongodb.com/cloud/atlas,
   add a database user, allow network access from anywhere (0.0.0.0/0), and
   copy the connection string.
2. In the [Render dashboard](https://dashboard.render.com), **New > Blueprint**,
   point it at this repo — `render.yaml` defines the service.
3. When prompted, fill in the secrets Render marks as required:
   - `MONGO_URL` — the Atlas connection string from step 1
   - `ADMIN_EMAIL` / `ADMIN_PASSWORD` — your admin login for `/admin`
   - (`JWT_SECRET` is generated for you; `DB_NAME` and `CORS_ORIGINS` default
     to sensible values — edit `render.yaml` if you need different ones)
4. Deploy. Render gives you a URL like `https://solix-backend.onrender.com`.
5. In the **Website** repo, add that URL as a repository variable named
   `BACKEND_URL` (Settings → Secrets and variables → Actions → Variables),
   then re-run the "Deploy frontend to GitHub Pages" workflow so the frontend
   build picks it up.

Any other Python host (Railway, Fly.io, a VM) works too — the `Dockerfile`
here builds and runs the same service; just set the same environment
variables (see `.env.example`).

## Local development

```bash
cp .env.example .env   # fill in MONGO_URL, JWT_SECRET, ADMIN_EMAIL, ADMIN_PASSWORD
pip install -r requirements.txt
uvicorn server:app --reload
```

## Content management, migration and gated asset emails

- **Built-in content**: Admin > Content > "Manage built-in content" takes the
  website's original articles, resources and press releases under CMS
  management (`POST /api/admin/content/import-builtin`). They keep their
  original dates; editing one replaces the built-in copy on the site, and
  unpublishing or archiving it hides it (`/api/content` lists these slugs as
  `withdrawn`).
- **Website migration** (`migrate.py`, Admin > Migrate): imports from a
  WordPress site (REST API), a sitemap, an RSS/Atom feed, a list of URLs or a
  CSV/JSON export. Preview first, then run as a background job with progress,
  a log and cancel. Original publish dates and source URLs are kept, types are
  detected from URLs and categories, and images and linked PDFs can be copied
  into the file store. `GET /api/admin/migrations/redirects?format=csv|nginx|apache`
  gives the 301 map for the old web server. Only public http(s) addresses are
  fetched.
- **Gated asset emails** (`delivery.py`): every download-form submission with a
  `resource_slug` emails the visitor a signed download link (and the file as
  an attachment when the transport allows and it is small enough). Built-in
  articles get a link that reopens the full article. Settings, a delivery log,
  resend and a test send live in Admin > Settings. Needs an email transport
  (SMTP, Resend or Emergent) plus `PUBLIC_API_URL` and `SITE_URL`; see
  `.env.example`.
- **Site settings** (`site_settings.py`): the announcement bar and the
  SOLIXEmpower promo, editable in Admin > Website (`GET /api/site`).

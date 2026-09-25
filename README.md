# Solix backend

FastAPI + MongoDB API for the [Solix Technologies site](https://github.com/akshatsingh-solix/Website)
(`frontend/` there). Deployed separately because GitHub Pages only serves
static files — it cannot run this service.

## What works without extra setup

Once deployed with a database and the four required secrets below, everything
works except two optional Emergent-platform integrations:

- **AI concierge chat (Sol)** — needs one free AI key such as
  `GROQ_API_KEY` (see "Sol" below). Without one, `/api/chat/stream` returns a
  clean 503 and the widget answers with its built-in scripted concierge.
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

## Sol, the AI concierge

Sol answers visitors in the site's chat widget on free-tier models.

- **Grounded answers.** `sol_knowledge.json` holds every product, solution,
  industry, article, press release, job, partner programme and service page
  of the site (~150 sections, each with its URL), generated in the Website
  repo by `frontend/scripts/sol-knowledge.js`; copy the regenerated file here
  after big site-content changes. Published CMS content is merged in
  automatically every 10 minutes (gated items: summary only). Each question
  is matched with BM25 keyword search in-process, and the best sections go
  into the prompt, so Sol quotes the site and links the page it drew on.
- **Tools.** `search_site`, `create_demo_request` and
  `request_expert_contact`; bookings and questions become scored leads with
  `source: chat` and trigger the sales alert.
- **Context.** The visitor's current page, the recent conversation (trimmed
  to a token budget) and their site language.
- **Failover.** Providers and models are tried in order; one that is rate
  limited, down or returns an empty reply is skipped and rested for a minute.
  If all fail, the widget falls back to its scripted concierge.
- **Limits.** Each request stays under `SOL_INPUT_TOKEN_BUDGET` (default
  4500) input tokens and `SOL_MAX_OUTPUT_TOKENS` (default 700), so it always
  fits Groq's free 8K tokens-per-minute cap. Visitors are rate-limited per
  session (20 per 5 min) and per IP (60 per hour).
- **Admin → Sol chats** (admin and sales roles): every conversation, its
  page, the model that answered, and whether it became a lead.
  `GET /api/chat/status` shows which providers are active.

Set at least one key (Groq alone is enough):

| Provider | Env var | Default models (override with) |
|---|---|---|
| Groq | `GROQ_API_KEY` | `openai/gpt-oss-120b,openai/gpt-oss-20b` (`SOL_GROQ_MODELS`) |
| Google Gemini | `GEMINI_API_KEY` | `gemini-2.5-flash,gemini-2.5-flash-lite` (`SOL_GEMINI_MODELS`) |
| Mistral | `MISTRAL_API_KEY` | `mistral-small-latest` (`SOL_MISTRAL_MODELS`) |
| OpenRouter | `OPENROUTER_API_KEY` | `CHAT_MODELS` (or `SOL_OPENROUTER_MODELS`) |
| OpenAI (paid) | `OPENAI_API_KEY` | `OPENAI_MODEL` (default `gpt-4o-mini`) |
| Any OpenAI-compatible server | `SOL_CUSTOM_BASE_URL`, `SOL_CUSTOM_API_KEY`, `SOL_CUSTOM_MODELS` | — |

`SOL_PROVIDER_ORDER` (default `gemini,groq,mistral,openrouter,openai,custom`)
sets the order among the providers that have keys. With Groq only, free
limits (per model: 30 requests/min, 1,000/day, 8K tokens/min, 200K
tokens/day) cover roughly 100+ visitor messages a day. Free tiers may use
prompts to improve their models; switch to a paid key (same variables)
before handling sensitive customer data.

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

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
